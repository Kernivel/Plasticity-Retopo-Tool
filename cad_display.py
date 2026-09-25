"""What the CAD surface looks like underneath the triangles, for the viewport.

- **B-rep edges and vertices** are exact: an edge is a run of boundary
  segments with the same neighbouring face id, a vertex is where it changes.
- **Surface flow** is derived, not imported: the grid a patch would be filled
  with, at a low span. The bridge sends no surface parameters.

Everything is cached per mesh on the `patch_data` fingerprint. A draw handler
must never recompute any of it.
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

# mesh name -> {key: (fingerprint, value)}, one entry per derived product.
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

    `surfaces=True` for anything derived from the raw Plasticity surfaces: it
    keys on the geometry alone, so writing a composite does not invalidate it.
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

    With no face id: the model's own surfaces, ignoring composites.
    With a face id: the patches, since that id may be a composite.
    """
    return (patch_data.analyse(mesh) if face_id is not None
            else patch_data.analyse_surfaces(mesh))


# --- B-rep edges and vertices ----------------------------------------------


def _edge_runs(
    loop: patch_data.Loop, neighbours: patch_data.Neighbours
) -> list[list[int]]:
    """Split one boundary loop into runs of constant neighbour.

    Each run is a list of positions into the loop, from one junction up to and
    including the next. A loop whose neighbour never changes is one closed run.
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

    Each edge is emitted once: by the lower face id of the two sharing it.
    An outer boundary always emits.
    """
    def build() -> list[list["mathutils.Vector"]]:
        analysis = _structure(mesh, face_id)
        # A welded id is itself a vertex index, so `positions` indexes directly.
        positions = analysis.positions
        polylines = []
        for owner, patch in analysis.patches.items():
            if face_id is not None and owner != face_id:
                continue
            for loop, neighbours in zip(patch.boundary_loops, patch.boundary_neighbours):
                for run in _edge_runs(loop, neighbours):
                    other = neighbours[run[0]]
                    # The lower id of a pair draws it, unless one patch was asked.
                    if face_id is None and other is not None and other < owner:
                        continue
                    polylines.append([positions[loop[i]] for i in run])
        return polylines

    return _cached(mesh, f"edges:{face_id}", build, surfaces=face_id is None)


def shared_edges(
    mesh: "bpy.types.Mesh"
) -> "list[tuple[int, int, list[mathutils.Vector]]]":
    """Every B-rep edge with a face on *both* sides, as (owner, other, points).

    For the crack report. Outer boundaries are left out. Emitted once per pair,
    by the same rule as `edge_polylines`.
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

    One batch for the whole object, never one per edge.
    """
    def build() -> list["mathutils.Vector"]:
        segments = []
        for polyline in edge_polylines(mesh, face_id):
            for a, b in zip(polyline, polyline[1:]):
                segments.append(a)
                segments.append(b)
        return segments

    return _cached(mesh, f"edge_segments:{face_id}", build, surfaces=face_id is None)


def patch_triangles(
    mesh: "bpy.types.Mesh", face_id: int, surfaces: bool = False
) -> "list[mathutils.Vector]":
    """One patch's polygons as a flat list of triangle corners, for a TRIS batch.

    For a tinted highlight. `surfaces=True` reads the raw surfaces, so
    `face_id` can be one a composite has absorbed.
    Fan-triangulated: an untriangulated export works too.
    """
    def build() -> list["mathutils.Vector"]:
        analysis = (patch_data.analyse_surfaces(mesh) if surfaces
                    else patch_data.analyse(mesh))
        patch = analysis.patches.get(face_id)
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

    return _cached(mesh, f"patch_triangles:{surfaces}:{face_id}", build,
                   surfaces=surfaces)


def brep_vertices(
    mesh: "bpy.types.Mesh", face_id: int | None = None
) -> "list[mathutils.Vector]":
    """The junctions between CAD edges -- genuine B-rep vertices.

    Where the face on the other side of the boundary changes.
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

    return _cached(mesh, f"brep_vertices:{face_id}", build, surfaces=face_id is None)


# --- surface flow -----------------------------------------------------------

# Max span of a patch's flow grid.
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
        # A band's "around" covers the whole rim, so it needs a larger span.
        settings = dict(settings,
                        span_u=generators.ring.around_count(loops_sides, span * 4))
    else:
        # Not a band: draw the outer boundary alone, as `_generate_for_face`
        # would fill it.
        generator = generators.find_generator(len(loops_sides[0]))
        generation_input = loops_sides[0]
    if generator is None:
        return [], []

    try:
        result = generator.generate(generation_input, settings, bvh=bvh)
    except (ValueError, ZeroDivisionError):
        # A degenerate patch: draw nothing.
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

    Built from the same generators at a low span, reprojected onto the surface.
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

    Cached: a panel draw may not walk a mesh either.
    """
    return _cached(mesh, "integrity", lambda: patch_data.group_report(mesh))


@dataclass
class PatchLabel:
    """One patch's raw bridge numbers, and where to write them on screen.

    `anchor` is the centre of the patch polygon nearest the mean of them all,
    never the mean itself, which can lie outside a concave patch.
    `normal` is that polygon's, for back-face culling.
    """
    face_id: int
    loop_start: int
    loop_count: int
    poly_count: int
    anchor: "mathutils.Vector"
    normal: "mathutils.Vector"


def _selection_fingerprint(mesh: "bpy.types.Mesh") -> int:
    """A CRC of which polygons are selected.

    Selection is not in `patch_data.mesh_fingerprint`, so it needs its own key.
    """
    count = len(mesh.polygons)
    flags = array.array("i", bytes(4 * count))
    if count:
        mesh.polygons.foreach_get("select", flags)
    return zlib.crc32(flags.tobytes())


def patch_labels(mesh: "bpy.types.Mesh") -> list[PatchLabel]:
    """Every patch's `face_id` / `loop_start` / `loop_count`, placed in space.

    Always the whole mesh. Which ones to draw is filtered at draw time.
    """
    return _cached(mesh, "labels", lambda: _build_patch_labels(mesh))


# mesh name -> (fingerprint, selection crc, face ids). The panel asks on every
# redraw.
_selected_cache: dict[str, tuple[patch_data.Fingerprint, int, set[int]]] = {}


def selected_face_ids(mesh: "bpy.types.Mesh") -> set[int]:
    """The face ids of the patches carrying a selected polygon.

    Empty in Edit Mode: Blender writes selection back only on leaving it.
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
            # Not on a polygon boundary: `integrity` reports it.
            continue
        indices = range(entry.poly_start, entry.poly_start + entry.poly_count)

        # The polygon nearest the patch's average centre.
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
