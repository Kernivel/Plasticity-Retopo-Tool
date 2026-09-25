"""Parsing of Plasticity "patches" (CAD faces) out of the triangulated mesh
produced by the plasticity-blender-addon bridge.

The bridge stores two custom properties on each imported mesh:
  mesh["groups"]    -- flat list of [loop_start, loop_count] pairs, in polygon order
  mesh["face_ids"]  -- one Plasticity face id per group, same order as the pairs

So len(groups) == 2 * len(face_ids).

A "patch" is the set of polygons sharing one face id. This module rebuilds the
polygon -> face id map and each patch's boundary loops.

Everything is cached per mesh, keyed on a fingerprint of its contents (see
`analyse`). Never re-parse on a hover.
"""
import array
import json
import zlib
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Annotations only: this module imports Blender lazily.
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
Fingerprint = tuple[int, int, int, int, int, int, int]


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

    Walks the groups the way the bridge writes them (handler.py:
    safe_mesh_import_data): ranges are in loop-index space.
    """
    groups = mesh.get("groups")
    face_ids = mesh.get("face_ids")
    n_polys = len(mesh.polygons)

    if not groups or not face_ids:
        # No patch data: the whole mesh is one patch.
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
# A composite is one patch over several Plasticity surfaces. It is assembled
# here, at the parse, so everything downstream sees one patch with one id.
# The borders between its surfaces cancel like triangulation edges do.
# See "One patch can cover several CAD surfaces" in CLAUDE.md.
#
# Its id is synthetic and negative, at or below this, so it never collides with
# a Plasticity face id or NO_PATCH (-1).
COMPOSITE_ID_BASE = -1000
# Mesh custom property holding them, as JSON: {"-1000": [12, 13, 14]}.
COMPOSITE_PROP = "retop_composites"
# One point inside each surface of each composite, in mesh local space,
# parallel to its surface list: {"-1000": [[x, y, z], ...]}.
# Lets `mesh_build.reanchor_composites` find the surfaces again after Plasticity
# renames them.
# Kept apart from COMPOSITE_PROP, which `mesh_fingerprint` reads.
COMPOSITE_ANCHORS_PROP = "retop_composite_anchors"


def read_composites(mesh: "bpy.types.Mesh") -> dict[int, list[int]]:
    """The composites stored on `mesh`, as {composite id: [surface face ids]}.

    Surfaces are always raw Plasticity face ids, never nested composites.
    """
    raw = mesh.get(COMPOSITE_PROP)
    if not raw:
        return {}
    try:
        stored = json.loads(raw)
    except (TypeError, ValueError):
        return {}  # not ours to read
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

    `mesh_fingerprint` reads this property, so the cache invalidates itself.
    Also writes the anchors.
    """
    if composites:
        mesh[COMPOSITE_PROP] = json.dumps(
            {str(key): list(surfaces) for key, surfaces in sorted(composites.items())})
    elif mesh.get(COMPOSITE_PROP) is not None:
        del mesh[COMPOSITE_PROP]
    _write_composite_anchors(mesh, composites)


def read_composite_anchors(
    mesh: "bpy.types.Mesh",
) -> dict[int, list[tuple[float, float, float]]]:
    """The stored anchors: {composite id: [one point per surface]}."""
    raw = mesh.get(COMPOSITE_ANCHORS_PROP)
    if not raw:
        return {}
    try:
        stored = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    anchors: dict[int, list[tuple[float, float, float]]] = {}
    for key, points in (stored or {}).items():
        try:
            anchors[int(key)] = [(float(p[0]), float(p[1]), float(p[2])) for p in points]
        except (TypeError, ValueError, IndexError):
            continue
    return anchors


def surface_anchor_points(
    mesh: "bpy.types.Mesh", surfaces: "Sequence[int]"
) -> list[tuple[float, float, float]] | None:
    """A point strictly inside each of `surfaces`, or None if one has no polygon.

    The centre of the polygon nearest the surface's mean, never the mean itself:
    that can lie outside a concave surface.
    """
    face_id_of_poly, _declared = polygon_face_ids(mesh)
    wanted = set(surfaces)
    centres: dict[int, list[tuple[float, float, float]]] = {s: [] for s in wanted}
    for poly in mesh.polygons:
        face_id = face_id_of_poly[poly.index]
        if face_id in wanted:
            centres[face_id].append(tuple(poly.center))
    points = []
    for surface in surfaces:
        own = centres.get(surface)
        if not own:
            return None
        count = len(own)
        mean = tuple(sum(c[i] for c in own) / count for i in range(3))
        points.append(min(own, key=lambda c: sum((c[i] - mean[i]) ** 2 for i in range(3))))
    return points


def _write_composite_anchors(
    mesh: "bpy.types.Mesh", composites: dict[int, list[int]]
) -> None:
    """Anchor every composite whose surfaces the mesh still declares.

    A composite that no longer applies keeps its previous anchors.
    """
    previous = read_composite_anchors(mesh)
    declared = set(mesh.get("face_ids") or ())
    anchors: dict[int, list[tuple[float, float, float]]] = {}
    for composite_id, surfaces in composites.items():
        points = None
        if declared.issuperset(surfaces):
            points = surface_anchor_points(mesh, surfaces)
        if points is None:
            points = previous.get(composite_id)
        if points is not None and len(points) == len(surfaces):
            anchors[composite_id] = points
    if anchors:
        mesh[COMPOSITE_ANCHORS_PROP] = json.dumps(
            {str(key): [list(p) for p in points] for key, points in sorted(anchors.items())})
    elif mesh.get(COMPOSITE_ANCHORS_PROP) is not None:
        del mesh[COMPOSITE_ANCHORS_PROP]


def next_composite_id(composites: dict[int, list[int]]) -> int:
    """An id no existing composite uses, allocated downwards from
    COMPOSITE_ID_BASE. Never reuse or renumber an id."""
    return min([COMPOSITE_ID_BASE + 1] + list(composites)) - 1


def applicable_composites(
    mesh: "bpy.types.Mesh", face_ids: Sequence[int]
) -> tuple[dict[int, list[int]], list[int]]:
    """Split the stored composites into the ones this mesh can still honour and
    the ids of the ones it cannot.

    A composite needs two or more surfaces, all declared by the mesh and
    claimed by no other composite. Dropped ones are returned for the panel to
    report.
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
        # Two composites naming one face: the later one is dropped.
        if len(unique) < 2 or not declared.issuperset(unique) or claimed.intersection(unique):
            dropped.append(composite_id)
            continue
        claimed.update(unique)
        applicable[composite_id] = unique
    return applicable, dropped


def parse_surface_selection(raw: str) -> list[int]:
    """The face ids in a stored surface selection, in the order they were picked.

    Here so the overlay and the operators share one parser.
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
    """Every face id this patch borders. Used to check a composite is
    contiguous."""
    return {neighbour for loop in patch.boundary_neighbours
            for neighbour in loop if neighbour is not NO_NEIGHBOUR}


def build_patches(
    mesh: "bpy.types.Mesh",
    composites: dict[int, list[int]] | None = None,
) -> tuple[dict[int, Patch], list[int], list[int]]:
    """Return ({face_id: Patch} with poly_indices filled in and boundary_loops
    still empty, polygon->face-id map, every declared face id).

    `composites` folds several faces into one patch. The remap is applied to
    the polygon -> face id map itself, so every later reader agrees with it.
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

    `poly_start`/`poly_count` are derived: the bridge stores loop ranges.
    """
    face_id: int
    loop_start: int
    loop_count: int
    poly_start: int
    poly_count: int


@dataclass
class GroupReport:
    """What `mesh["groups"]`/`["face_ids"]` say, and whether they still fit.

    The ranges must tile the loop array on polygon boundaries. Anything that
    re-topologizes the mesh after import breaks that silently, and
    `polygon_face_ids` then assigns polygons to the wrong faces.
    """
    entries: list[GroupEntry]
    problems: list[str]
    # polygon size (vertices) -> how many polygons have it.
    polygon_sizes: dict[int, int]
    loop_total: int

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def triangulated(self) -> bool:
        """Whether every polygon is a triangle. Vacuously true for an empty mesh.

        Reported, never required.
        """
        return set(self.polygon_sizes) <= {3}


def group_report(mesh: "bpy.types.Mesh") -> GroupReport:
    """Read `groups`/`face_ids` back out of `mesh`, with their integrity.

    Uses `foreach_get`: a panel asks for this.
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

    # The loop index each polygon starts at. A range starting or ending
    # anywhere else is corrupt.
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
        # Not fatal (`build_patches` merges them), but the bridge emits one
        # group per face.
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

    An interior vertex already shares its index with its neighbours, so it has
    no duplicate to merge.

    Returns None when every vertex qualifies (a fully unwelded soup), or
    without numpy: the caller then welds the whole mesh.
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
        # No `MeshLoop.edge_index`: weld everything.
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

    What the weld may not reach across. Infinity without numpy.
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

    The bridge tessellates each face separately, so a shared border has two
    copies of every vertex. Mirrors a Merge by Distance without modifying the
    mesh. Only `weld_candidates` take part.

    The epsilon is capped at half the shortest edge: welding across a real edge
    breaks the boundary walk.

    `epsilon` is absolute, in the mesh's local units. See "The weld may never
    reach across a real edge" in CLAUDE.md.
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

    A patch's boundary half-edge (a, b) is matched by its neighbour's (b, a),
    which names the neighbour. The bridge sends no edge data.
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

    A boundary half-edge is one whose reverse no other polygon of the same
    patch emits.

    `weld_map` maps raw vertex indices to welded ones (`build_weld_map`). The
    loops are returned in welded index space.
    """
    if weld_map is None:
        weld_map = range(len(mesh.vertices))  # identity mapping

    # Every directed edge (a, b) some polygon of the patch traverses.
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

    # Boundary half-edges: those whose reverse is absent.
    # A multimap: a vertex can carry several outgoing boundary half-edges.
    outgoing: dict[int, list[int]] = {}
    for (a, b) in directed_present:
        if (b, a) not in directed_present:
            outgoing.setdefault(a, []).append(b)

    # Walk the half-edges into closed loops. Only closed loops are returned:
    # an open chain would be closed with `% n` by every reader, drawing a chord
    # across the face.
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


# How far a boundary segment may sit off a foreign one and still be the same
# CAD edge, as a share of the mesh's extent. Never of the segment's own length:
# a long segment would then reach a separate sheet nearby.
# See "A neighbour missed by half-edge pairing" in CLAUDE.md.
NEIGHBOUR_GAP_RATIO = 1e-5
# Max sample points in the boundary index. Segments are sampled at one uniform
# spacing, so the query radius stays constant.
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

    Half-edge pairing fails where two faces tessellate a shared edge
    differently (a T-junction). The neighbour is then the patch whose own
    boundary segment this one lies along.

    Only touches unpaired segments, and builds nothing when there are none.
    A real open boundary is left alone.
    Mutates `boundary_neighbours` in place and returns how many it filled in.
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

    # One spacing for every segment: the median segment length, floored so
    # the index stays bounded.
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
    """Order a patch's boundary loops with the outer one (largest extent) first.

    Loop order out of compute_boundary_loops is random: anything picking a
    single loop must go through here.
    """
    return sorted(loops, key=lambda loop: loop_extent(loop, positions), reverse=True)


@dataclass
class MeshPatches:
    """Everything one parse of a mesh produces, cached as a unit.

    Shared and read-only: copy before changing anything.
    """
    patches: dict[int, Patch]      # face_id -> Patch, boundary loops computed
    face_id_of_poly: list[int]     # polygon index -> face id
    face_ids: list[int]            # every face id the mesh declares, in group order
    weld_map: list[int]            # raw vertex index -> canonical welded index
    # (a, b) -> face id traversing that directed edge
    directed_owners: dict[tuple[int, int], int]
    positions: Positions           # vertex index -> mesh local space
    # composite patch id -> the Plasticity surfaces it stands for.
    composites: dict[int, list[int]] = field(default_factory=dict)
    # Composites that could not be applied. Reported by the panel.
    dropped_composites: list[int] = field(default_factory=list)


def mesh_fingerprint(mesh: "bpy.types.Mesh") -> Fingerprint:
    """A cheap value that changes whenever the mesh or its composites do.

    A CRC of the vertex coordinates and ids, since the bridge re-imports into
    the same datablock.
    """
    # Composites are included: writing one moves no vertex.
    composites = str(mesh.get(COMPOSITE_PROP) or "")
    return geometry_fingerprint(mesh) + (zlib.crc32(composites.encode("utf-8")),)


def geometry_fingerprint(mesh: "bpy.types.Mesh") -> "tuple[int, int, int, int, int, int]":
    """The same, minus the composites -- what the mesh's own surfaces depend on.

    Used by what describes the model itself, which a composite does not change.
    Includes the face ids and groups: Plasticity can rename faces without
    moving a vertex.
    """
    count = len(mesh.vertices)
    coords = array.array("f", bytes(4 * 3 * count))
    if count:
        mesh.vertices.foreach_get("co", coords)
    face_ids = mesh.get("face_ids") or ()
    ids = array.array("q", face_ids)
    ids.extend(mesh.get("groups") or ())
    return (count, len(mesh.polygons), len(mesh.loops),
            len(face_ids), zlib.crc32(coords.tobytes()), zlib.crc32(ids.tobytes()))


# mesh name -> (fingerprint, MeshPatches). Keyed by name, never by datablock.
_cache: dict[str, tuple[Fingerprint, "MeshPatches"]] = {}
# The same, with no composite applied (`analyse_surfaces`).
_surface_cache: dict[str, tuple["tuple[int, ...]", "MeshPatches"]] = {}
_CACHE_LIMIT = 8  # a session works on one object; a few neighbours is plenty


def invalidate(mesh: "bpy.types.Mesh | None" = None) -> None:
    """Drop the cached parse of `mesh`, or of everything when given nothing.

    Called on addon reload and when a session ends.
    """
    if mesh is None:
        _cache.clear()
        _surface_cache.clear()
    else:
        _cache.pop(mesh.name, None)
        _surface_cache.pop(mesh.name, None)


def analyse(mesh: "bpy.types.Mesh", weld_epsilon: float = 1e-5) -> MeshPatches:
    """The full parse of `mesh`, from cache when the mesh has not changed.

    The single entry point: everything in the result comes from one pass.
    """
    fingerprint = mesh_fingerprint(mesh)
    cached = _cache.get(mesh.name)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]

    # The declared ids straight off the mesh: no polygon walk needed.
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

    For what describes the model: CAD edges and vertices, the surface picker.
    Returns `analyse`'s own result when the mesh has no composite.
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
    """One full parse, shared by `analyse` and `analyse_surfaces`."""
    patches, face_id_of_poly, face_ids = build_patches(mesh, composites)
    weld_map = build_weld_map(mesh, weld_epsilon)
    directed_owners = build_directed_owners(mesh, face_id_of_poly, weld_map)
    for patch in patches.values():
        compute_boundary_loops(mesh, patch, face_id_of_poly, weld_map, directed_owners)

    positions = {v.index: v.co.copy() for v in mesh.vertices}
    # After every patch has its loops: the fallback needs all of them.
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
