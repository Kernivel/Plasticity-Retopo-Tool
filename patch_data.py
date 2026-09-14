"""Parsing of Plasticity "patches" (CAD faces) out of the triangulated mesh
produced by the plasticity-blender-addon bridge.

The bridge stores two custom properties each imported mesh:
  mesh["groups"]    -- flat list of [loop_start, loop_count] pairs, in polygon order
  mesh["face_ids"]  -- one Plasticity face id per group, same order as the pairs

This means that len(groups) == 2*len(face_ids), with each pair in group matching a value in face_ids.

A "patch" is the set of triangles that share one face_id. This module rebuilds,
for a given mesh, the polygon->face_id mapping and the boundary loop(s) of each
patch (the edges where a patch touches a different patch or the outer boundary
of the solid).

**Everything here is cached per mesh** (see `analyse`). The parse walks every
polygon, builds a KD-tree over the vertices that can carry a duplicate and a
directed-half-edge table over every triangle corner -- and the session used to redo all of it on *every mouse
move*, because hovering a patch re-prepares it. On a CAD part of any size that
is the difference between a smooth hover and a stuttering one. Nothing in a
Plasticity mesh changes while it is being retopologized, so the result is keyed
by a fingerprint of the mesh's own contents and reused until that changes.
"""
import array
import json
import zlib
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Annotations only: this module is pure mesh parsing and pulls the heavy
    # Blender types in lazily, where it needs them at all.
    import bpy
    import mathutils

NO_NEIGHBOUR = None  # a boundary half-edge with no polygon on the other side

# A boundary loop is an ordered list of (welded) vertex indices; the face id
# across each of its segments is None where the patch borders nothing.
Loop = list[int]
Neighbours = list[int | None]
# vertex index -> position in the mesh's local space
Positions = dict[int, "mathutils.Vector"]
# What `mesh_fingerprint` returns -- compared, never inspected.
Fingerprint = tuple[int, int, int, int, int, int]


@dataclass
class Patch:
    face_id: int
    poly_indices: list[int]  # polygon indices belonging to this patch
    # each boundary loop is an ordered list of vertex indices (loop[i] -> loop[i+1] is a boundary edge)
    boundary_loops: list[Loop] = field(default_factory=list)
    # per loop, the face id on the other side of each boundary segment:
    # boundary_neighbours[k][i] faces segment loop[i] -> loop[(i + 1) % n].
    # NO_NEIGHBOUR where the patch borders nothing (an open solid).
    boundary_neighbours: list[Neighbours] = field(default_factory=list)


def polygon_face_ids(mesh: "bpy.types.Mesh") -> tuple[list[int], list[int]]:
    """Return a list mapping polygon index -> face_id, using mesh['groups']/['face_ids'].

    Mirrors the group-walking logic used by the bridge itself when it writes
    these attributes (handler.py: safe_mesh_import_data), so it stays correct
    even though group ranges are expressed in loop-index space while we index
    by polygon.
    """
    groups = mesh.get("groups")
    face_ids = mesh.get("face_ids")
    n_polys = len(mesh.polygons)

    if not groups or not face_ids:
        # No patch metadata (e.g. a plain, non-Plasticity mesh): treat everything
        # as a single patch so callers still have something to work with.
        return [0] * n_polys, [-1] if n_polys else []

    result = [0] * n_polys
    group_idx = 0
    group_start = groups[0]
    group_count = groups[1]

    for poly in mesh.polygons:
        while group_idx + 1 < len(face_ids) and poly.loop_start >= group_start + group_count:
            group_idx += 1
            group_start = groups[group_idx * 2]
            group_count = groups[group_idx * 2 + 1]
        result[poly.index] = face_ids[group_idx]

    return result, list(face_ids)


# --- One patch from several CAD faces ----------------------------------------
#
# One Plasticity face is one patch, and that is the whole input contract -- but
# a CAD model is cut into faces by the modelling history, not by what wants a
# single grid over it. A boss with two fillet rings around it is five surfaces
# that the retopology has every reason to cross in one sheet, and the pipeline
# already knows how to do that: `compute_boundary_loops` cancels every directed
# edge whose reverse is emitted by the *same* patch, which is how a face's own
# triangulation edges disappear. Hand it the union of several faces' polygons
# and their shared borders cancel by exactly the same rule.
#
# A patch built that way is a **composite**, and it is assembled here, at the
# parse, so that nothing downstream learns about it: corners, sides,
# `find_generator`, matching, the commit, the re-edit, the crack report and
# `cad_display` all keep seeing one patch with one id. Doing it at generation
# time instead would mean teaching every one of them that an id can stand for a
# set.
#
# The id a composite carries is synthetic and negative, below `NO_PATCH` (-1),
# so it can never collide with a Plasticity face id nor with the sentinel the
# result mesh already stamps on unclaimed faces. It is stored, because
# `PATCH_ID_ATTR` on committed faces names it and has to keep meaning the same
# patch across sessions and file loads.
COMPOSITE_ID_BASE = -1000
# Mesh custom property holding them, as JSON: {"-1000": [12, 13, 14]}. On the
# mesh rather than the object because that is what `analyse` is keyed by and
# what the bridge writes `groups`/`face_ids` on -- a composite is a statement
# about the same data those are.
COMPOSITE_PROP = "retop_composites"


def read_composites(mesh: "bpy.types.Mesh") -> dict[int, list[int]]:
    """The composites stored on `mesh`, as {composite id: [surface face ids]}.

    Surfaces are always *raw* Plasticity face ids: adding one more to a
    composite rebuilds it flat rather than nesting, so nothing here has to
    resolve a chain.
    """
    raw = mesh.get(COMPOSITE_PROP)
    if not raw:
        return {}
    try:
        stored = json.loads(raw)
    except (TypeError, ValueError):
        return {}  # hand-edited or written by something else: not our business
    composites: dict[int, list[int]] = {}
    for key, surfaces in (stored or {}).items():
        try:
            composites[int(key)] = [int(surface) for surface in surfaces]
        except (TypeError, ValueError):
            continue
    return composites


def write_composites(
    mesh: "bpy.types.Mesh", composites: dict[int, list[int]]
) -> None:
    """Store `composites` on `mesh`, or drop the property when there are none.

    The cache invalidates itself: `mesh_fingerprint` reads this property, so a
    composite written here is picked up by the next `analyse` without anyone
    having to remember to call `invalidate`.
    """
    if composites:
        mesh[COMPOSITE_PROP] = json.dumps(
            {str(key): list(surfaces) for key, surfaces in sorted(composites.items())})
    elif mesh.get(COMPOSITE_PROP) is not None:
        del mesh[COMPOSITE_PROP]


def next_composite_id(composites: dict[int, list[int]]) -> int:
    """An id no existing composite uses. Allocated downwards from
    COMPOSITE_ID_BASE so it stays stable once written -- deriving it from a
    position in the table would renumber every other one the moment a composite
    was taken apart."""
    return min([COMPOSITE_ID_BASE + 1] + list(composites)) - 1


def applicable_composites(
    mesh: "bpy.types.Mesh", face_ids: Sequence[int]
) -> tuple[dict[int, list[int]], list[int]]:
    """Split the stored composites into the ones this mesh can still honour and
    the ids of the ones it cannot.

    A composite needs two or more surfaces and every one of them still declared
    by the mesh. A re-export renumbers every Plasticity face id even when no
    vertex moves, so a stored composite *will* stop applying one day -- and one
    that quietly stops applying looks exactly like an addon that forgot it, so
    the ones that fell through are handed back for the panel to report rather
    than dropped in silence.
    """
    composites = read_composites(mesh)
    if not composites:
        return {}, []

    declared = set(face_ids)
    claimed: set[int] = set()
    applicable: dict[int, list[int]] = {}
    dropped: list[int] = []
    for composite_id, surfaces in sorted(composites.items(), reverse=True):
        unique = list(dict.fromkeys(surfaces))
        # `claimed` is not defensive book-keeping: two composites naming one
        # face would put its polygons in both, and the second would silently win.
        if len(unique) < 2 or not declared.issuperset(unique) or claimed.intersection(unique):
            dropped.append(composite_id)
            continue
        claimed.update(unique)
        applicable[composite_id] = unique
    return applicable, dropped


def parse_surface_selection(raw: str) -> list[int]:
    """The face ids in a stored surface selection, in the order they were picked.

    Here rather than in `operators` because the *overlay* has to read the same
    string to draw it, and two parsers of one format is how they come to
    disagree. Pure text: this module still pulls in no Blender.
    """
    if not raw:
        return []
    try:
        return [int(value) for value in json.loads(raw)]
    except (TypeError, ValueError):
        return []


def format_surface_selection(face_ids: "Sequence[int]") -> str:
    """The inverse. Empty for an empty selection, so the property reads false."""
    return json.dumps([int(value) for value in face_ids]) if face_ids else ""


def patch_neighbour_ids(patch: Patch) -> set[int]:
    """Every face id this patch borders. What contiguity is checked against: a
    composite whose surfaces only touch at a point has a pinched boundary, and
    one whose surfaces do not touch at all has two outer loops, which the whole
    loop-count pipeline reads as a band."""
    return {neighbour for loop in patch.boundary_neighbours
            for neighbour in loop if neighbour is not NO_NEIGHBOUR}


def build_patches(
    mesh: "bpy.types.Mesh",
    composites: dict[int, list[int]] | None = None,
) -> tuple[dict[int, Patch], list[int], list[int]]:
    """Return ({face_id: Patch} with poly_indices filled in and boundary_loops
    still empty, polygon->face-id map, every declared face id).

    `composites` folds several Plasticity faces into one patch -- see the
    section above. The remap is applied to the polygon->face-id map itself
    rather than to the grouping alone, so `build_directed_owners` carries the
    composite ids too and every later reader (the neighbour of a boundary
    segment, the topological corner test, `cad_display`) is consistent with it
    for free.
    """
    face_id_of_poly, face_ids = polygon_face_ids(mesh)

    if composites:
        owner = {surface: composite_id
                 for composite_id, surfaces in composites.items()
                 for surface in surfaces}
        face_id_of_poly = [owner.get(fid, fid) for fid in face_id_of_poly]
        face_ids = list(dict.fromkeys(owner.get(fid, fid) for fid in face_ids))

    patches = {}
    for poly in mesh.polygons:
        fid = face_id_of_poly[poly.index]
        patch = patches.get(fid)
        if patch is None:
            patch = Patch(face_id=fid, poly_indices=[])
            patches[fid] = patch
        patch.poly_indices.append(poly.index)

    return patches, face_id_of_poly, face_ids


@dataclass
class GroupEntry:
    """One `[loop_start, loop_count]` pair, the face id it carries, and the
    polygons that pair turns out to cover.

    `poly_start`/`poly_count` are derived rather than stored by the bridge: the
    ranges are expressed in *loop* index space, and one group spans however many
    polygons the tessellation of that CAD face took.
    """
    face_id: int
    loop_start: int
    loop_count: int
    poly_start: int
    poly_count: int


@dataclass
class GroupReport:
    """What `mesh["groups"]`/`["face_ids"]` say, and whether they still fit.

    The ranges are read back rather than trusted because `polygon_face_ids`
    walks them positionally: it advances to the next group when a polygon's
    `loop_start` passes the current group's end. That is correct exactly while
    the ranges still tile the mesh's loop array on polygon boundaries -- which
    they do as imported, and stop doing the moment anything re-topologizes the
    mesh underneath them (a triangulate, a decimate, a join, a modifier
    applied). Nothing raises when they don't: polygons are simply assigned to
    the wrong CAD face, so every patch boundary in the file is quietly wrong.
    That is the one failure here worth reporting without being asked.
    """
    entries: list[GroupEntry]
    problems: list[str]
    # polygon size (vertices) -> how many polygons have it. The bridge offers an
    # untriangulated export, so this says which was used rather than assuming.
    polygon_sizes: dict[int, int]
    loop_total: int

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def triangulated(self) -> bool:
        """Whether every polygon is a triangle. Vacuously true for an empty mesh.

        Nothing here requires it -- `polygon_face_ids` walks loop ranges and
        `geometry.build_bvh_with_polygon_map` fan-triangulates whatever it is
        given -- so this is reported, never enforced.
        """
        return set(self.polygon_sizes) <= {3}


def group_report(mesh: "bpy.types.Mesh") -> GroupReport:
    """Read `groups`/`face_ids` back out of `mesh`, with their integrity.

    Walks the mesh once, at C speed where it can: the polygon loop ranges come
    out through `foreach_get` rather than attribute access per polygon, since a
    real part has tens of thousands of them and this is asked for from a panel.
    """
    groups = list(mesh.get("groups") or ())
    face_ids = list(mesh.get("face_ids") or ())
    loop_total = len(mesh.loops)

    n_polys = len(mesh.polygons)
    starts = array.array("i", bytes(4 * n_polys))
    totals = array.array("i", bytes(4 * n_polys))
    if n_polys:
        mesh.polygons.foreach_get("loop_start", starts)
        mesh.polygons.foreach_get("loop_total", totals)
    sizes = dict(Counter(totals))

    problems: list[str] = []
    if not groups or not face_ids:
        problems.append(
            "No groups/face_ids on this mesh -- not a Plasticity import, or the "
            "custom properties were lost")
        return GroupReport([], problems, sizes, loop_total)

    if len(groups) != 2 * len(face_ids):
        problems.append(
            f"{len(groups)} group values for {len(face_ids)} face ids "
            f"(expected {2 * len(face_ids)})")

    # Which loop indices a polygon actually begins at. A group range that starts
    # or ends anywhere else cannot be walked back to a whole polygon, which is
    # the corruption worth naming -- `polygon_face_ids` would hand that
    # polygon's triangles to whichever face the range happens to straddle.
    poly_at_loop = {int(start): index for index, start in enumerate(starts)}

    entries: list[GroupEntry] = []
    expected = 0
    pairs = min(len(face_ids), len(groups) // 2)
    for i in range(pairs):
        face_id = int(face_ids[i])
        start = int(groups[i * 2])
        count = int(groups[i * 2 + 1])
        end = start + count

        if count <= 0:
            problems.append(f"Face {face_id}: empty group (loop_count={count})")
        if start != expected:
            kind = "gap" if start > expected else "overlap"
            problems.append(
                f"Face {face_id}: {kind} before it -- starts at loop {start}, "
                f"the previous group ended at {expected}")
        if start not in poly_at_loop:
            problems.append(
                f"Face {face_id}: loop_start {start} is not the start of any "
                f"polygon -- the mesh has been re-topologized since import")
        if end != loop_total and end not in poly_at_loop:
            problems.append(
                f"Face {face_id}: its range ends at loop {end}, mid-polygon")

        poly_start = poly_at_loop.get(start, -1)
        poly_end = poly_at_loop.get(end, n_polys)
        poly_count = max(0, poly_end - poly_start) if poly_start >= 0 else 0

        entries.append(GroupEntry(
            face_id=face_id, loop_start=start, loop_count=count,
            poly_start=poly_start, poly_count=poly_count))
        expected = end

    if expected != loop_total:
        problems.append(
            f"The groups cover {expected} loops, the mesh has {loop_total}")

    duplicates = [fid for fid, n in Counter(e.face_id for e in entries).items() if n > 1]
    if duplicates:
        # Not fatal -- `build_patches` merges them into one patch, which is
        # probably what was meant -- but the bridge emits one group per face, so
        # this says the file is not what this code was written against.
        problems.append(
            "Face id repeated across groups: "
            + ", ".join(str(fid) for fid in duplicates[:6]))

    return GroupReport(entries, problems, sizes, loop_total)


def _edge_key(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def weld_candidates(mesh: "bpy.types.Mesh") -> Sequence[int] | None:
    """The vertices a weld could possibly need to merge: those lying on an
    edge that Blender's own connectivity leaves unshared (fewer than two
    polygons on it). Returned as a sorted index array.

    A vertex strictly inside a patch, whose every edge already has a polygon
    on both sides, is by construction not coincident with anything -- two
    triangles sharing an edge share its vertex indices, so there is no second
    copy of that point to merge it with. Only the borders between patches,
    which the bridge tessellates once per face, put two vertices at the same
    position.

    Returns None when *every* vertex qualifies, which is the caller's signal
    to skip the filtering entirely. That is what a fully unwelded soup looks
    like -- every edge carries one polygon -- and it is why scoping the weld
    is safe whatever the bridge turns out to emit: a mesh with no native
    interior sharing degrades exactly to the old whole-mesh behaviour.

    Every step is `foreach_get` plus a numpy reduction, because the Python
    version of this cost more than the KD-tree it exists to shrink: counting
    edge uses by hand is one interpreted iteration per triangle *corner*,
    which on a real part is more work than welding the whole mesh. Without
    numpy the answer is None -- filtering nothing is always correct, only
    slower.
    """
    try:
        import numpy as np
    except ImportError:
        return None

    n_verts = len(mesh.vertices)
    n_edges = len(mesh.edges)
    n_loops = len(mesh.loops)
    if not n_edges or not n_loops:
        return None

    try:
        edge_of_loop = np.empty(n_loops, dtype=np.int32)
        mesh.loops.foreach_get("edge_index", edge_of_loop)
        edge_verts = np.empty(n_edges * 2, dtype=np.int32)
        mesh.edges.foreach_get("vertices", edge_verts)
    except (AttributeError, TypeError, RuntimeError, ValueError):
        # A Blender without `MeshLoop.edge_index`: weld everything rather than
        # guess at the connectivity.
        return None

    uses = np.bincount(edge_of_loop, minlength=n_edges)
    free = uses < 2
    if not free.any():
        return None

    on_free_edge = np.zeros(n_verts, dtype=bool)
    on_free_edge[edge_verts.reshape(-1, 2)[free].ravel()] = True

    if on_free_edge.all():
        return None
    return np.flatnonzero(on_free_edge)


def shortest_edge(mesh: "bpy.types.Mesh") -> float:
    """The shortest non-degenerate edge of `mesh`, or infinity if it has none.

    What the weld may not reach across. Computed with `foreach_get` plus a
    numpy reduction where numpy is there, since it runs on every parse and the
    Python version is one interpreted iteration per edge; without numpy the
    answer is infinity, which is the previous behaviour exactly -- an uncapped
    epsilon, only correct on a part whose features are all larger than it.
    """
    n_edges = len(mesh.edges)
    n_verts = len(mesh.vertices)
    if not n_edges or not n_verts:
        return float("inf")
    try:
        import numpy as np
    except ImportError:
        return float("inf")

    coords = np.empty(n_verts * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", coords)
    edges = np.empty(n_edges * 2, dtype=np.int32)
    mesh.edges.foreach_get("vertices", edges)

    pairs = coords.reshape(-1, 3)[edges.reshape(-1, 2)]
    lengths = np.linalg.norm(pairs[:, 0] - pairs[:, 1], axis=1)
    real = lengths[lengths > 0.0]
    return float(real.min()) if real.size else float("inf")


def build_weld_map(mesh: "bpy.types.Mesh", epsilon: float = 1e-5) -> list[int]:
    """Return a list mapping raw vertex index -> canonical "welded" vertex
    index, merging vertices within `epsilon` of each other.

    The Plasticity bridge tessellates each CAD face independently, so the two
    faces meeting along a B-rep edge each carry their own copy of every vertex
    on it. Boundary detection has to treat those copies as one point, or every
    patch border reads as two unrelated free edges -- and, on a mesh whose
    interior is *also* unshared, every internal triangulation edge looks like a
    patch boundary too. This mirrors a "Merge by Distance" pass without
    modifying the mesh.

    Only `weld_candidates` takes part: an interior vertex has nothing to merge
    with, and leaving it out of the KD-tree both costs less and removes any
    chance that proximity alone collapses two distinct points on a densely
    tessellated fillet. When nothing can be excluded the whole mesh goes in,
    which is the previous behaviour exactly.

    **The epsilon is capped by the mesh's own shortest edge, and that cap is
    not a refinement.** An edge is the mesh saying outright that its two ends
    are distinct points of the surface; welding across one destroys it. The
    triangle carrying it goes degenerate, its two directed corners are dropped
    as `a == b`, and the patch's directed boundary -- balanced by construction,
    one outgoing and one incoming per polygon corner -- stops being balanced.
    `compute_boundary_loops` then walks into a dead end, hands back an *open
    chain* as if it were a loop, and every reader closes it with `% n`: a chord
    from its last vertex straight back to its first, drawn across a face the
    model never divided, with the patch reporting five or seven boundary loops
    where it has one. A part whose smallest features sit at the epsilon
    triggers that readily -- on one, 205 real edges were collapsed in a single
    object and 101 of its 245 loops came back open.

    Capping rather than testing adjacency pair by pair, because the pairwise
    test cannot answer this: when two genuinely distinct positions both fall
    inside the epsilon, the cluster has to be *split* by position, and which
    copy is edge-joined to which is an accident of how the bridge happened to
    emit the triangles. Half the shortest edge is the largest radius at which
    no cluster can span two of them. The copies this exists to merge are
    unaffected: they are the same double rounded the same way twice, so they
    sit at a distance of zero, not of the epsilon.

    `epsilon` is absolute, in the mesh's own local units. See the note in
    CLAUDE.md: on a part far from the origin it is close enough to the float32
    ulp that two faces' copies of a shared vertex can fail to meet.
    """
    from mathutils.kdtree import KDTree

    n = len(mesh.vertices)
    weld_id = list(range(n))

    candidates = weld_candidates(mesh)
    considered = range(n) if candidates is None else candidates
    if len(considered) < 2:
        return weld_id

    epsilon = min(epsilon, shortest_edge(mesh) * 0.5)
    if epsilon <= 0.0:
        return weld_id

    coords = mesh.vertices
    kd = KDTree(len(considered))
    for i in considered:
        kd.insert(coords[i].co, int(i))
    kd.balance()

    claimed = bytearray(n)
    for i in considered:
        i = int(i)
        if claimed[i]:
            continue
        claimed[i] = 1
        for _co, idx, _dist in kd.find_range(coords[i].co, epsilon):
            if not claimed[idx]:
                claimed[idx] = 1
                weld_id[idx] = i

    return weld_id


def build_directed_owners(
    mesh: "bpy.types.Mesh",
    face_id_of_poly: list[int],
    weld_map: Sequence[int] | None = None,
) -> dict[tuple[int, int], int]:
    """(a, b) -> face id of the polygon that traverses that directed edge.

    This is the closest thing to edge identity the bridge makes available. The
    protocol carries no edge ids at all (only vertices/faces/normals/groups/
    face_ids), but a patch's boundary half-edge (a, b) is matched by the
    neighbouring patch's (b, a), so the *neighbour* of every boundary segment
    is recoverable -- and the vertex where that neighbour changes is a genuine
    B-rep vertex, the junction between two CAD edges.

    Built once per mesh and shared by every patch, since it is global.
    """
    if weld_map is None:
        weld_map = range(len(mesh.vertices))

    owners = {}
    for poly in mesh.polygons:
        face_id = face_id_of_poly[poly.index]
        verts = [weld_map[vi] for vi in poly.vertices]
        count = len(verts)
        for i in range(count):
            a = verts[i]
            b = verts[(i + 1) % count]
            if a != b:
                owners[(a, b)] = face_id
    return owners


def boundary_neighbours_for_loop(
    loop: Loop, face_id: int, directed_owners: dict[tuple[int, int], int]
) -> Neighbours:
    """The face id across each segment of `loop`, segment i being
    loop[i] -> loop[(i + 1) % n]. `face_id` is the patch's own id, used to
    reject a match with itself (which a pinched boundary can produce).
    """
    count = len(loop)
    neighbours = []
    for i in range(count):
        a = loop[i]
        b = loop[(i + 1) % count]
        # The neighbour traverses the same edge the other way round.
        owner = directed_owners.get((b, a), NO_NEIGHBOUR)
        neighbours.append(NO_NEIGHBOUR if owner == face_id else owner)
    return neighbours


def compute_boundary_loops(
    mesh: "bpy.types.Mesh",
    patch: Patch,
    face_id_of_poly: list[int],
    weld_map: Sequence[int] | None = None,
    directed_owners: dict[tuple[int, int], int] | None = None,
) -> list[Loop]:
    """Fill patch.boundary_loops from patch.poly_indices.

    A boundary edge of the patch is an edge used by exactly one polygon of the
    patch on one winding direction and by no other polygon of the *same*
    patch on the other winding direction (i.e. it's a patch-to-patch or
    patch-to-void border, not an internal triangulation edge).

    `weld_map`, if given, maps each raw vertex index to a canonical index
    shared by all vertices at (nearly) the same position -- required for
    correct results on the Plasticity bridge's unwelded triangle soups (see
    build_weld_map). The returned loop indices are expressed in this same
    canonical space.
    """
    if weld_map is None:
        weld_map = range(len(mesh.vertices))  # identity mapping

    # directed_edge (a, b) -> True if some polygon of the patch has this
    # ordered edge in its loop (i.e. traverses a -> b along the polygon winding)
    directed_present = set()

    for poly_idx in patch.poly_indices:
        poly = mesh.polygons[poly_idx]
        verts = [weld_map[vi] for vi in poly.vertices]
        n = len(verts)
        for i in range(n):
            a = verts[i]
            b = verts[(i + 1) % n]
            if a == b:
                continue  # degenerate edge after welding
            directed_present.add((a, b))

    # A directed edge (a, b) is a boundary half-edge of the patch if the
    # reverse (b, a) is not also emitted by another polygon of this same
    # patch (that would mean it's shared internally, i.e. both sides belong
    # to the patch and cancel out).
    # One vertex can carry more than one outgoing boundary half-edge -- a
    # boundary that touches itself at a point -- so this is a multimap. Keyed
    # by a single target, the second half-edge was dropped and the walk that
    # needed it died.
    outgoing: dict[int, list[int]] = {}
    for (a, b) in directed_present:
        if (b, a) not in directed_present:
            outgoing.setdefault(a, []).append(b)

    # Walk the half-edges into closed loops.
    #
    # A patch's directed boundary is balanced -- each polygon contributes one
    # outgoing and one incoming at every corner, and cancelling a pair removes
    # one of each at both ends -- so it decomposes into closed cycles and every
    # walk returns to where it started. A chain that does *not* is the mesh
    # telling us something is wrong with it, and it must not be handed back as
    # a loop: every reader closes a loop with `% n`, so an open chain draws a
    # chord from its last vertex to its first, straight across a face the model
    # never divided, and counts as an extra boundary loop besides. Dropping the
    # fragment loses part of one patch's border; keeping it invents geometry
    # across the whole part. The usual cause was the weld collapsing a real
    # edge, which `build_weld_map` no longer does.
    loops = []
    while outgoing:
        start = next(iter(outgoing))
        loop = [start]
        current = start
        closed = False
        while True:
            targets = outgoing.get(current)
            if not targets:
                break
            nxt = targets.pop()
            if not targets:
                del outgoing[current]
            if nxt == start:
                closed = True
                break
            loop.append(nxt)
            current = nxt
        if closed:
            loops.append(loop)

    patch.boundary_loops = loops
    patch.boundary_neighbours = (
        [boundary_neighbours_for_loop(loop, patch.face_id, directed_owners)
         for loop in loops]
        if directed_owners is not None else []
    )
    return loops


# How far a boundary segment may sit off a foreign one before the two stop
# being the same CAD edge, as a share of the whole mesh's extent.
#
# Of the *extent*, not of the segment's own length, and that is the whole of
# it. Two faces sharing a CAD edge put their boundaries on the same curve, so
# the only thing between their chords is float rounding, which scales with
# coordinate magnitude and not with feature size. Measured across four objects
# of a real part, a genuine shared border sits within 1e-7 of the extent at the
# 95th percentile -- coincident, in other words. Scaling by the segment instead
# lets a long one reach a long way *sideways*, which answers a different
# question: a separate sheet 0.02 above a 4-unit edge is 0.5% of that edge and
# would be taken as its neighbour (tests/test_match_specificity.py builds
# exactly that stack-up). At a share of the extent it is 4e-3, four hundred
# times outside this limit, while every real border is two orders inside it.
NEIGHBOUR_GAP_RATIO = 1e-5
# How many sample points the boundary index may hold. Segments are sampled at
# one *uniform* spacing rather than a few points each, so that the query radius
# is a constant and every lookup returns a handful of hits instead of however
# many happen to lie within a multiple of the segment's own length. That
# distinction is the difference between 8 seconds and a tenth of one on a
# 35k-polygon object: a long straight edge searched at four times its own
# length swept a large part of the mesh, once per orphan.
NEIGHBOUR_INDEX_POINTS = 400_000


def _point_segment_distance(
    point: "mathutils.Vector", a: "mathutils.Vector", b: "mathutils.Vector"
) -> float:
    direction = b - a
    length_squared = direction.length_squared
    if length_squared <= 0.0:
        return (point - a).length
    t = max(0.0, min(1.0, (point - a).dot(direction) / length_squared))
    return (point - (a + direction * t)).length


def resolve_neighbours_by_geometry(
    patches: dict[int, Patch], positions: Positions
) -> int:
    """Name the face across every boundary segment the half-edge pairing missed.

    `boundary_neighbours_for_loop` finds the neighbour by looking for the same
    edge walked the other way, which is exact and free -- and which requires
    the two faces to have put the *same vertices* on the CAD edge they share.
    Plasticity tessellates each face on its own, and on a real part it does not
    always agree with itself: the finer side drops vertices in the middle of
    the coarser side's segments, so the reversed half-edge is not there and the
    segment reports no neighbour at all.

    That reads downstream as an open boundary, and an open boundary is not a
    quiet degradation. `detect_topological_corners` fires wherever the
    neighbour changes, so every one of those segments becomes a phantom B-rep
    vertex; the side count is what picks the generator, so the patch is filled
    by the wrong one; and `cad_display` draws the shared border as a string of
    unrelated edges. Measured on one CAD part, 2327 of 4857 boundary segments
    of a single object -- half of them -- came back unmatched this way.

    So the pairing falls back to geometry for exactly those: the neighbour is
    the patch whose own boundary segment this one *lies along*. Nothing here
    touches a segment that already found its opposite, and nothing is built at
    all when none of them missed -- a cleanly tessellated mesh pays only the
    scan that finds nothing to do.

    Mutates `boundary_neighbours` in place and returns how many it filled in.
    A segment with genuinely nothing across it (a real open boundary) is left
    alone, which is the honest answer rather than the nearest one.
    """
    from mathutils.kdtree import KDTree

    if not positions:
        return 0
    if not any(neighbour is None
               for patch in patches.values()
               for neighbours in patch.boundary_neighbours
               for neighbour in neighbours):
        return 0

    # (owner, a, b) for every boundary segment of every patch.
    segments: list[tuple[int, "mathutils.Vector", "mathutils.Vector"]] = []
    for owner, patch in patches.items():
        for loop in patch.boundary_loops:
            count = len(loop)
            for i in range(count):
                a = positions[loop[i]]
                b = positions[loop[(i + 1) % count]]
                if (b - a).length_squared > 0.0:
                    segments.append((owner, a, b))
    if not segments:
        return 0

    points = positions.values()
    low = [min(p[axis] for p in points) for axis in range(3)]
    high = [max(p[axis] for p in points) for axis in range(3)]
    extent = sum((high[axis] - low[axis]) ** 2 for axis in range(3)) ** 0.5
    limit = extent * NEIGHBOUR_GAP_RATIO
    if limit <= 0.0:
        return 0

    # Sample every segment at one spacing, so the index resolves the boundary
    # evenly however coarsely any single face was tessellated. The median
    # segment is the natural choice -- it is what the mesher itself settled on
    # -- floored so a part with a few very long edges cannot blow the index up.
    lengths = sorted((b - a).length for _owner, a, b in segments)
    total_length = sum(lengths)
    spacing = max(lengths[len(lengths) // 2],
                  total_length / NEIGHBOUR_INDEX_POINTS)
    if spacing <= 0.0:
        return 0

    samples = []
    for index, (_owner, a, b) in enumerate(segments):
        direction = b - a
        steps = max(1, int(direction.length / spacing) + 1)
        for step in range(steps + 1):
            samples.append((a + direction * (step / steps), index))

    tree = KDTree(len(samples))
    for point, index in samples:
        tree.insert(point, index)
    tree.balance()

    # A midpoint lying on a foreign segment is within `limit` of it, and the
    # nearest sample on that segment is at most half a spacing further along.
    reach = spacing * 0.5 + limit

    resolved = 0
    for owner, patch in patches.items():
        for loop, neighbours in zip(patch.boundary_loops, patch.boundary_neighbours):
            count = len(loop)
            for i, neighbour in enumerate(neighbours):
                if neighbour is not NO_NEIGHBOUR:
                    continue
                a = positions[loop[i]]
                b = positions[loop[(i + 1) % count]]
                if (b - a).length_squared <= 0.0:
                    continue
                midpoint = (a + b) * 0.5
                best_gap = limit
                best_owner = NO_NEIGHBOUR
                seen = set()
                for _co, index, _dist in tree.find_range(midpoint, reach):
                    if index in seen:
                        continue
                    seen.add(index)
                    other, other_a, other_b = segments[index]
                    if other == owner:
                        continue
                    gap = _point_segment_distance(midpoint, other_a, other_b)
                    if gap <= best_gap:
                        best_gap = gap
                        best_owner = other
                if best_owner is not NO_NEIGHBOUR:
                    neighbours[i] = best_owner
                    resolved += 1
    return resolved


def loop_extent(loop: Loop, positions: Positions) -> float:
    """Bounding-box diagonal of a boundary loop, in the mesh's own units."""
    if not loop:
        return 0.0
    xs = [positions[vi] for vi in loop]
    lo = [min(p[axis] for p in xs) for axis in range(3)]
    hi = [max(p[axis] for p in xs) for axis in range(3)]
    return sum((hi[axis] - lo[axis]) ** 2 for axis in range(3)) ** 0.5


def sort_loops_outer_first(loops: list[Loop], positions: Positions) -> list[Loop]:
    """Order a patch's boundary loops with the outer one first.

    compute_boundary_loops walks a *set* of half-edges, so the order it returns
    depends on hash iteration -- on a patch with a hole, "the first loop" could
    just as easily be the hole. Anything picking a single loop must go through
    here, or it silently retopologizes the hole instead of the face. The outer
    boundary is the one enclosing the others, so it has the largest extent.
    """
    return sorted(loops, key=lambda loop: loop_extent(loop, positions), reverse=True)


@dataclass
class MeshPatches:
    """Everything one parse of a mesh produces, cached as a unit.

    Handed out read-only: callers may look at any of it, but anything that
    wants to *change* a patch must copy first, or the next hover inherits the
    edit. `positions` in particular is shared, which is why
    `generators.base.resolve_side_points` copies each point it hands on.
    """
    patches: dict[int, Patch]      # face_id -> Patch, boundary loops computed
    face_id_of_poly: list[int]     # polygon index -> face id
    face_ids: list[int]            # every face id the mesh declares, in group order
    weld_map: list[int]            # raw vertex index -> canonical welded index
    # (a, b) -> face id traversing that directed edge
    directed_owners: dict[tuple[int, int], int]
    positions: Positions           # vertex index -> mesh local space
    # composite patch id -> the Plasticity surfaces it stands for. Empty on a
    # mesh nobody has built one on, which is every mesh until asked.
    composites: dict[int, list[int]] = field(default_factory=dict)
    # Composites that could not be applied -- a surface the mesh no longer
    # declares, which is what a re-export leaves behind. Reported, never kept.
    dropped_composites: list[int] = field(default_factory=list)


def mesh_fingerprint(mesh: "bpy.types.Mesh") -> Fingerprint:
    """A cheap value that changes whenever the mesh's geometry does.

    Counts alone are not enough: the bridge re-imports into the *same*
    datablock, and a moved vertex with the topology untouched has to invalidate
    too. So the vertex coordinates go through a CRC -- one C-level
    `foreach_get` plus one CRC pass, which is orders of magnitude cheaper than
    the parse it guards (a KD-tree over the same vertices, and a Python loop
    over every triangle corner).
    """
    # The composites are part of what `analyse` produces, so they are part of
    # what invalidates it: joining two surfaces moves no vertex, and without
    # this the cached analysis would keep handing back the patches from before.
    composites = str(mesh.get(COMPOSITE_PROP) or "")
    return geometry_fingerprint(mesh) + (zlib.crc32(composites.encode("utf-8")),)


def geometry_fingerprint(mesh: "bpy.types.Mesh") -> "tuple[int, int, int, int, int]":
    """The same, minus the composites -- what the mesh's own surfaces depend on.

    Kept apart so that building a composite does not invalidate everything that
    describes the *model*: the B-rep edges and vertices `cad_display` draws are
    the same curves before and after, and rebuilding them on every Shift+click
    would be a full re-parse for a picture that did not change.
    """
    count = len(mesh.vertices)
    coords = array.array("f", bytes(4 * 3 * count))
    if count:
        mesh.vertices.foreach_get("co", coords)
    return (count, len(mesh.polygons), len(mesh.loops),
            len(mesh.get("face_ids") or ()), zlib.crc32(coords.tobytes()))


# mesh name -> (fingerprint, MeshPatches). Keyed by name rather than by the
# datablock so a dead mesh can never keep itself alive through this dict; a
# stale entry under a reused name is caught by the fingerprint anyway.
_cache: dict[str, tuple[Fingerprint, "MeshPatches"]] = {}
# The same, with no composite applied -- see `analyse_surfaces`. A second slot
# rather than a second dict keyed differently, so `invalidate` has one place to
# clear and the limit still counts meshes rather than parses.
_surface_cache: dict[str, tuple["tuple[int, ...]", "MeshPatches"]] = {}
_CACHE_LIMIT = 8  # a session works on one object; a few neighbours is plenty


def invalidate(mesh: "bpy.types.Mesh | None" = None) -> None:
    """Drop the cached parse of `mesh`, or of everything when given nothing.

    Only needed when a mesh changes in a way the fingerprint cannot see -- it
    sees geometry, not the `groups`/`face_ids` custom properties being rewritten
    with identical counts. Called on addon reload and when a session ends.
    """
    if mesh is None:
        _cache.clear()
        _surface_cache.clear()
    else:
        _cache.pop(mesh.name, None)
        _surface_cache.pop(mesh.name, None)


def analyse(mesh: "bpy.types.Mesh", weld_epsilon: float = 1e-5) -> MeshPatches:
    """The full parse of `mesh`, from cache when the mesh has not changed.

    This is the entry point everything else should use: patches, their boundary
    loops, the polygon->face-id map, the weld map, the directed-owner table and
    the vertex positions all come out of one pass and are consistent with each
    other by construction.
    """
    fingerprint = mesh_fingerprint(mesh)
    cached = _cache.get(mesh.name)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]

    # The declared ids straight off the mesh, not through `polygon_face_ids`:
    # that walks every polygon, and all this needs is the list the bridge wrote.
    composites, dropped_composites = applicable_composites(
        mesh, mesh.get("face_ids") or ())
    analysis = _parse(mesh, weld_epsilon, composites, dropped_composites)

    if len(_cache) >= _CACHE_LIMIT:
        _cache.pop(next(iter(_cache)))
    _cache[mesh.name] = (fingerprint, analysis)
    return analysis


def analyse_surfaces(mesh: "bpy.types.Mesh", weld_epsilon: float = 1e-5) -> MeshPatches:
    """The same parse with **no composite applied**: one patch per Plasticity
    surface, as the bridge wrote them.

    What describes the *model* rather than the work reads this -- the B-rep
    edges and vertices `cad_display` draws, and the surface picker, which has
    to name the individual surface under the cursor even when it is already
    part of a patch. A composite is this addon's decision; the edges the model
    was built from do not stop existing because a patch was laid across them,
    and an overlay that says they did is reporting the wrong thing.

    On a mesh with no composite it hands back `analyse`'s own result, so
    nothing pays for a second parse until someone builds one.
    """
    composites, _dropped = applicable_composites(mesh, mesh.get("face_ids") or ())
    if not composites:
        return analyse(mesh, weld_epsilon)

    fingerprint = geometry_fingerprint(mesh)
    cached = _surface_cache.get(mesh.name)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]

    analysis = _parse(mesh, weld_epsilon, {}, [])

    if len(_surface_cache) >= _CACHE_LIMIT:
        _surface_cache.pop(next(iter(_surface_cache)))
    _surface_cache[mesh.name] = (fingerprint, analysis)
    return analysis


def _parse(
    mesh: "bpy.types.Mesh",
    weld_epsilon: float,
    composites: dict[int, list[int]],
    dropped_composites: list[int],
) -> MeshPatches:
    """One full parse. Shared by `analyse` and `analyse_surfaces`, which differ
    only in whether the composites are folded in -- everything below is the
    same work either way, and two copies of it would drift."""
    patches, face_id_of_poly, face_ids = build_patches(mesh, composites)
    weld_map = build_weld_map(mesh, weld_epsilon)
    directed_owners = build_directed_owners(mesh, face_id_of_poly, weld_map)
    for patch in patches.values():
        compute_boundary_loops(mesh, patch, face_id_of_poly, weld_map, directed_owners)

    positions = {v.index: v.co.copy() for v in mesh.vertices}
    # Only after every patch has its loops: the fallback pairs a segment with
    # another patch's segment, so it needs all of them to exist first.
    resolve_neighbours_by_geometry(patches, positions)

    return MeshPatches(
        patches=patches,
        face_id_of_poly=face_id_of_poly,
        face_ids=face_ids,
        weld_map=weld_map,
        directed_owners=directed_owners,
        positions=positions,
        composites=composites,
        dropped_composites=dropped_composites,
    )


def get_patches_with_boundaries(
    mesh: "bpy.types.Mesh", weld_epsilon: float = 1e-5
) -> dict[int, Patch]:
    """{face_id: Patch} with boundary loops computed. Thin wrapper over
    `analyse` for callers that only want the patches.
    """
    return analyse(mesh, weld_epsilon).patches
