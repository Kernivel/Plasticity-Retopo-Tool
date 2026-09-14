"""What the CAD surface looks like underneath the triangles, for the viewport.

The bridge sends a triangle soup plus `groups`/`face_ids`. No edges, no
vertices, no surface parameters -- the Plasticity protocol carries none of it
(see patch_data). So a mesh imported through it shows as a uniform field of
triangles, and the two things worth seeing while retopologizing it are exactly
the two things that field hides:

**Where one CAD face ends and the next begins.** That *is* recoverable, and
exactly: a boundary half-edge (a, b) of one patch is matched by (b, a) of the
patch on the other side, so every B-rep edge is the maximal run of boundary
segments whose neighbouring face id does not change, and every B-rep vertex is
where it does. Those are facts about the model, not estimates.

**Which way the surface runs.** That is *not* recoverable. Plasticity's own
isoparametric curves come from each face's NURBS parameterisation, and none of
it crosses the bridge. What can be built instead is the flow implied by the
face's own boundary: split it into sides the way the generators do, run the
same Coons interpolation over it, and draw the resulting grid. On a fillet or a
swept face those lines land very close to the true isoparms, because both are
answering the same question about the same boundary -- but they are derived,
not imported, and the panel says so. They are also the more useful of the two
here: they show the topology the retopology would actually get.

Everything is cached per mesh, keyed on the same fingerprint `patch_data` uses.
A draw handler runs on every redraw, and none of this may be recomputed there.
"""
import array
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from . import generators
from . import geometry
from . import patch_data
from . import sides as sides_mod

if TYPE_CHECKING:
    import bpy
    import mathutils
    from mathutils.bvhtree import BVHTree

_T = TypeVar("_T")

# mesh name -> {key: (fingerprint, value)}. Each mesh keeps one entry per
# derived product, since they are asked for independently and cost differently.
_cache: dict[str, dict[str, tuple[patch_data.Fingerprint, Any]]] = {}
_CACHE_LIMIT = 4


def invalidate(mesh: "bpy.types.Mesh | None" = None) -> None:
    if mesh is None:
        _cache.clear()
        _selected_cache.clear()
    else:
        _cache.pop(mesh.name, None)
        _selected_cache.pop(mesh.name, None)


def _cached(
    mesh: "bpy.types.Mesh", key: str, build: Callable[[], _T],
    surfaces: bool = False,
) -> _T:
    """`build()`'s result, kept until the mesh changes under it.

    `surfaces=True` for anything derived from the mesh's own Plasticity
    surfaces rather than from the patches laid over them: it keys on the
    geometry alone, so building a composite -- which moves no vertex -- does
    not throw away a picture of the model that did not change.
    """
    fingerprint = (patch_data.geometry_fingerprint(mesh) if surfaces
                   else patch_data.mesh_fingerprint(mesh))
    entries = _cache.get(mesh.name)
    if entries is None:
        if len(_cache) >= _CACHE_LIMIT:
            _cache.pop(next(iter(_cache)))
        entries = _cache[mesh.name] = {}

    hit = entries.get(key)
    if hit is not None and hit[0] == fingerprint:
        return hit[1]

    value = build()
    entries[key] = (fingerprint, value)
    return value


def _structure(mesh: "bpy.types.Mesh", face_id: int | None) -> "patch_data.MeshPatches":
    """The analysis the CAD structure display should read.

    With no face id it describes the **model**, so it reads the mesh's own
    surfaces: a composite is this addon's decision about how to retopologize,
    and the edges the part was built from do not stop existing because a patch
    was laid across them. Asked about one face id it describes a **patch**, and
    then the composites are exactly what it has to honour -- that id may be one.
    """
    return (patch_data.analyse(mesh) if face_id is not None
            else patch_data.analyse_surfaces(mesh))


# --- B-rep edges and vertices ----------------------------------------------


def _edge_runs(
    loop: patch_data.Loop, neighbours: patch_data.Neighbours
) -> list[list[int]]:
    """Split one boundary loop into runs of constant neighbour.

    Each run is a list of positions *into the loop*, from one junction up to
    and including the next, so consecutive runs share their junction vertex --
    the same convention `sides.split_into_sides` uses, and for the same reason:
    a junction belongs to both edges meeting there.

    A loop whose neighbour never changes is one closed edge, and comes back as
    a single run that closes on itself.
    """
    count = len(loop)
    if count < 2 or not neighbours or len(neighbours) != count:
        return []

    junctions = sides_mod.detect_topological_corners(loop, neighbours)
    if not junctions:
        return [list(range(count)) + [0]]

    runs = []
    for k, start in enumerate(junctions):
        end = junctions[(k + 1) % len(junctions)]
        run = [start]
        walk = start
        while walk != end:
            walk = (walk + 1) % count
            run.append(walk)
        runs.append(run)
    return runs


def edge_polylines(
    mesh: "bpy.types.Mesh", face_id: int | None = None
) -> "list[list[mathutils.Vector]]":
    """Every B-rep edge of `mesh`, as a list of point polylines in local space.

    Each edge is emitted **once**, though both of the faces that share it walk
    it. Which of the two emits it is settled by face id rather than by
    remembering what has been seen: the lower id wins, an outer boundary (no
    face on the other side) always emits, and nothing has to be compared for
    equality -- two patches tessellated from the same CAD edge agree on their
    welded vertex indices, but a set of those is a much larger thing to carry
    around than one comparison.
    """
    def build() -> list[list["mathutils.Vector"]]:
        analysis = _structure(mesh, face_id)
        # Boundary loops are in welded index space, and a welded id is itself a
        # vertex index -- build_weld_map elects one of the coincident vertices
        # rather than inventing a new id -- so `positions` indexes directly.
        positions = analysis.positions
        polylines = []
        for owner, patch in analysis.patches.items():
            if face_id is not None and owner != face_id:
                continue
            for loop, neighbours in zip(patch.boundary_loops, patch.boundary_neighbours):
                for run in _edge_runs(loop, neighbours):
                    other = neighbours[run[0]]
                    # One patch of a pair draws their shared edge -- unless only
                    # one patch is being asked about, when there is no pair.
                    if face_id is None and other is not None and other < owner:
                        continue
                    polylines.append([positions[loop[i]] for i in run])
        return polylines

    # Keyed on the geometry alone when it describes the whole model, so a
    # composite written mid-pick does not rebuild a picture that did not change.
    return _cached(mesh, f"edges:{face_id}", build, surfaces=face_id is None)


def shared_edges(
    mesh: "bpy.types.Mesh"
) -> "list[tuple[int, int, list[mathutils.Vector]]]":
    """Every B-rep edge with a face on *both* sides, as (owner, other, points).

    `edge_polylines` throws the pair away because a drawing only needs the
    curve; this keeps it, because the question it answers is about the two
    patches rather than about the edge -- "are these two welded to each other
    along the edge they share". An outer boundary has no pair and is not here
    at all: nothing can crack along an edge with one face on it.

    Emitted once per pair, by the same `other < owner` rule and for the same
    reason: both faces walk their shared edge, and comparing ids is cheaper
    than remembering which runs have been seen.
    """
    def build() -> list[tuple[int, int, list["mathutils.Vector"]]]:
        analysis = patch_data.analyse(mesh)
        positions = analysis.positions
        pairs = []
        for owner, patch in analysis.patches.items():
            for loop, neighbours in zip(patch.boundary_loops, patch.boundary_neighbours):
                for run in _edge_runs(loop, neighbours):
                    other = neighbours[run[0]]
                    if other is None or other < owner:
                        continue
                    pairs.append(
                        (owner, other, [positions[loop[i]] for i in run]))
        return pairs

    return _cached(mesh, "shared_edges", build)


def edge_segments(
    mesh: "bpy.types.Mesh", face_id: int | None = None
) -> "list[mathutils.Vector]":
    """The same edges as a flat list of point pairs, ready for a LINES batch.

    One batch for the whole object rather than one per edge: a CAD part has
    hundreds of edges, and a draw call each is what makes an overlay stutter.
    """
    def build() -> list["mathutils.Vector"]:
        segments = []
        for polyline in edge_polylines(mesh, face_id):
            for a, b in zip(polyline, polyline[1:]):
                segments.append(a)
                segments.append(b)
        return segments

    # Keyed on the geometry alone when it describes the whole model, so a
    # composite written mid-pick does not rebuild a picture that did not change.
    return _cached(mesh, f"edge_segments:{face_id}", build, surfaces=face_id is None)


def patch_triangles(
    mesh: "bpy.types.Mesh", face_id: int
) -> "list[mathutils.Vector]":
    """One patch's polygons as a flat list of triangle corners, for a TRIS batch.

    What a *highlight* over a surface needs, as against the outline
    `edge_segments` gives: a patch picked out of a dense CAD part by its border
    alone is a border among hundreds of other borders.

    Fan-triangulated, which the bridge's triangle soup makes a no-op -- the
    same thing `geometry.build_bvh_with_polygon_map` does, and for the same
    reason: nothing here requires the triangulated export.
    """
    def build() -> list["mathutils.Vector"]:
        patch = patch_data.analyse(mesh).patches.get(face_id)
        if patch is None:
            return []
        points = []
        for poly_index in patch.poly_indices:
            corners = list(mesh.polygons[poly_index].vertices)
            for i in range(1, len(corners) - 1):
                points.append(mesh.vertices[corners[0]].co.copy())
                points.append(mesh.vertices[corners[i]].co.copy())
                points.append(mesh.vertices[corners[i + 1]].co.copy())
        return points

    return _cached(mesh, f"patch_triangles:{face_id}", build)


def brep_vertices(
    mesh: "bpy.types.Mesh", face_id: int | None = None
) -> "list[mathutils.Vector]":
    """The junctions between CAD edges -- genuine B-rep vertices.

    Where the face on the other side of the boundary changes, two CAD edges
    meet, and the vertex there is one the model itself put down. Every other
    boundary vertex is the mesher's.
    """
    def build() -> list["mathutils.Vector"]:
        analysis = _structure(mesh, face_id)
        positions = analysis.positions
        seen = set()
        points = []
        for owner, patch in analysis.patches.items():
            if face_id is not None and owner != face_id:
                continue
            for loop, neighbours in zip(patch.boundary_loops, patch.boundary_neighbours):
                for index in sides_mod.detect_topological_corners(loop, neighbours):
                    vertex = loop[index]
                    if vertex not in seen:
                        seen.add(vertex)
                        points.append(positions[vertex])
        return points

    # Keyed on the geometry alone when it describes the whole model, so a
    # composite written mid-pick does not rebuild a picture that did not change.
    return _cached(mesh, f"brep_vertices:{face_id}", build, surfaces=face_id is None)


# --- surface flow -----------------------------------------------------------

# How dense a patch's flow grid may get. The display is there to be read at a
# glance, and past this the lines stop being distinguishable from the surface.
MAX_FLOW_SPAN = 12


def _patch_flow(
    patch: patch_data.Patch,
    positions: patch_data.Positions,
    density: int,
    bvh: "BVHTree | None",
    angle_threshold: float,
) -> "tuple[list[mathutils.Vector], list[tuple[int, int]]]":
    """The flow grid of one patch, as (a, b) index pairs into a point list."""
    if not patch.boundary_loops:
        return [], []

    loops = patch_data.sort_loops_outer_first(patch.boundary_loops, positions)
    neighbours_of_loop = {id(loop): n for loop, n
                          in zip(patch.boundary_loops, patch.boundary_neighbours)}
    if len(loops) > 2:
        loops = loops[:1]

    loops_sides = []
    for loop in loops:
        corners = sides_mod.resolve_corners(
            loop, positions, angle_threshold=angle_threshold,
            neighbour_ids=neighbours_of_loop.get(id(loop)), method='ANGLE',
            allow_synthesis=(len(loops) == 1))
        side_indices = sides_mod.split_into_sides(
            loop, positions, angle_threshold=angle_threshold, corner_indices=corners)
        loops_sides.append(generators.base.resolve_side_points(side_indices, positions))

    span = max(1, min(int(density), MAX_FLOW_SPAN))
    settings = {"span_u": span, "span_v": span, "span": span}

    if len(loops_sides) == 2 and generators.ring.is_band(loops_sides):
        generator = generators.RING
        generation_input = loops_sides
        # A band's "around" is one count for the whole rim, so a plain span
        # would give it a handful of points and a twisted-looking ring.
        settings = dict(settings,
                        span_u=generators.ring.around_count(loops_sides, span * 4))
    else:
        # Not a band -- a plate with a small hole, say. A ring across it is
        # stretched the width of the plate, which says nothing true about how
        # the surface runs, so the outer boundary alone is drawn and the hole
        # is simply not described. Same judgement `_generate_for_face` makes.
        generator = generators.find_generator(len(loops_sides[0]))
        generation_input = loops_sides[0]
    if generator is None:
        return [], []

    try:
        result = generator.generate(generation_input, settings, bvh=bvh)
    except (ValueError, ZeroDivisionError):
        # A degenerate patch is not worth a traceback out of a display path.
        return [], []

    edges = set()
    for face in result.faces:
        count = len(face)
        for i in range(count):
            a = face[i]
            b = face[(i + 1) % count]
            if a != b:
                edges.add((a, b) if a < b else (b, a))
    return result.verts, sorted(edges)


def flow_segments(
    mesh: "bpy.types.Mesh",
    density: int = 2,
    angle_threshold: float = 135.0,
    face_id: int | None = None,
) -> "list[mathutils.Vector]":
    """Flow lines for every patch of `mesh`, as a flat list of point pairs.

    Built from the same generators the retopology uses, at a low span, and
    reprojected onto the surface through one shared BVH -- so what is drawn is
    the shape a patch would actually be filled with, not a flat lid over it.
    """
    def build() -> list["mathutils.Vector"]:
        analysis = patch_data.analyse(mesh)
        positions = analysis.positions
        bvh, _tri_poly = geometry.build_bvh_with_polygon_map(mesh)

        segments = []
        for owner, patch in analysis.patches.items():
            if face_id is not None and owner != face_id:
                continue
            verts, edges = _patch_flow(
                patch, positions, density, bvh, angle_threshold)
            for a, b in edges:
                segments.append(verts[a])
                segments.append(verts[b])
        return segments

    return _cached(mesh, f"flow:{density}:{angle_threshold}:{face_id}", build)


def patch_count(mesh: "bpy.types.Mesh") -> int:
    """How many CAD faces the mesh declares -- for the panel to size the cost."""
    return len(patch_data.analyse(mesh).patches)


# --- the raw bridge data, for the debug display -----------------------------


def integrity(mesh: "bpy.types.Mesh") -> patch_data.GroupReport:
    """`patch_data.group_report`, cached like everything else here.

    Asked for from a panel draw, which runs on every mouse move over the
    sidebar, so it may no more walk a mesh than a viewport handler may.
    """
    return _cached(mesh, "integrity", lambda: patch_data.group_report(mesh))


@dataclass
class PatchLabel:
    """One patch's raw bridge numbers, and where to write them on screen.

    `anchor` is the centre of one of the patch's own polygons -- the one nearest
    the mean of them all -- rather than the mean itself. The mean of a concave
    patch is outside it and the mean of an annulus is in its hole, which would
    put a label on a face it does not describe; a polygon centre is on the
    surface by construction. `normal` is that polygon's, and is what lets the
    far side of a closed part be culled instead of writing its labels over the
    near side.
    """
    face_id: int
    loop_start: int
    loop_count: int
    poly_count: int
    anchor: "mathutils.Vector"
    normal: "mathutils.Vector"


def _selection_fingerprint(mesh: "bpy.types.Mesh") -> int:
    """A CRC of which polygons are selected.

    Selection does not touch `patch_data.mesh_fingerprint` -- it is not
    geometry -- so scoping the labels to the selection needs its own key. One
    `foreach_get` plus one CRC, both C loops, which is the same budget the
    geometry fingerprint already spends per redraw.
    """
    count = len(mesh.polygons)
    flags = array.array("i", bytes(4 * count))
    if count:
        mesh.polygons.foreach_get("select", flags)
    return zlib.crc32(flags.tobytes())


def patch_labels(mesh: "bpy.types.Mesh") -> list[PatchLabel]:
    """Every patch's `face_id` / `loop_start` / `loop_count`, placed in space.

    Always the whole mesh, keyed on the geometry alone. Which of them to *draw*
    -- one under the cursor, the selected ones, all of them -- is a filter over
    a few hundred entries and belongs at the point of drawing: scoping this
    would put a second, far more volatile key on a cache whose expensive half
    (a scan of every polygon centre) does not depend on it at all.
    """
    return _cached(mesh, "labels", lambda: _build_patch_labels(mesh))


# mesh name -> (geometry fingerprint, selection crc, face ids). Same shape and
# same reason as `_label_cache`: the panel asks for this on every redraw of the
# sidebar, and walking the polygons there is no cheaper than doing it in a
# viewport handler.
_selected_cache: dict[str, tuple[patch_data.Fingerprint, int, set[int]]] = {}


def selected_face_ids(mesh: "bpy.types.Mesh") -> set[int]:
    """The face ids of the patches carrying a selected polygon.

    Empty while Blender holds the mesh in Edit Mode: selection flags are only
    written back to the mesh datablock on leaving it. That is a fact about
    Blender rather than something to work around, and the panel says so.
    """
    fingerprint = patch_data.mesh_fingerprint(mesh)
    selection = _selection_fingerprint(mesh)

    hit = _selected_cache.get(mesh.name)
    if hit is not None and hit[0] == fingerprint and hit[1] == selection:
        return hit[2]

    count = len(mesh.polygons)
    flags = array.array("i", bytes(4 * count))
    if count:
        mesh.polygons.foreach_get("select", flags)
    face_id_of_poly = patch_data.analyse(mesh).face_id_of_poly
    found = {face_id_of_poly[i] for i, on in enumerate(flags)
             if on and i < len(face_id_of_poly)}

    if len(_selected_cache) >= _CACHE_LIMIT:
        _selected_cache.pop(next(iter(_selected_cache)))
    _selected_cache[mesh.name] = (fingerprint, selection, found)
    return found


def _build_patch_labels(mesh: "bpy.types.Mesh") -> list[PatchLabel]:
    report = integrity(mesh)
    if not report.entries:
        return []

    count = len(mesh.polygons)
    centres = array.array("f", bytes(4 * 3 * count))
    if count:
        mesh.polygons.foreach_get("center", centres)

    labels = []
    for entry in report.entries:
        if entry.poly_start < 0 or entry.poly_count <= 0:
            # A group that does not land on a polygon boundary -- reported by
            # `integrity`, and there is nothing here to point at.
            continue
        indices = range(entry.poly_start, entry.poly_start + entry.poly_count)

        # The polygon nearest the patch's own average centre. Two passes over
        # the patch's polygons, no square roots in the first.
        mx = my = mz = 0.0
        for i in indices:
            mx += centres[i * 3]
            my += centres[i * 3 + 1]
            mz += centres[i * 3 + 2]
        n = entry.poly_count
        mx, my, mz = mx / n, my / n, mz / n

        best = entry.poly_start
        best_d = None
        for i in indices:
            dx = centres[i * 3] - mx
            dy = centres[i * 3 + 1] - my
            dz = centres[i * 3 + 2] - mz
            d = dx * dx + dy * dy + dz * dz
            if best_d is None or d < best_d:
                best_d = d
                best = i

        polygon = mesh.polygons[best]
        labels.append(PatchLabel(
            face_id=entry.face_id,
            loop_start=entry.loop_start,
            loop_count=entry.loop_count,
            poly_count=entry.poly_count,
            anchor=polygon.center.copy(),
            normal=polygon.normal.copy(),
        ))
    return labels


def world_segments(
    matrix: "mathutils.Matrix", points: "list[mathutils.Vector]"
) -> "list[mathutils.Vector]":
    """Local-space points through an object matrix, for a GPU batch."""
    return [matrix @ point for point in points]
