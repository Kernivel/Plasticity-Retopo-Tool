"""Turning one Plasticity face into something a generator can fill.

The step between `patch_data` (which says where a patch's boundary is) and
`generators` (which fills what it is handed): resolve the boundary's corners,
split it into sides, and answer the two questions the pipeline asks about a
patch before choosing anything -- how many boundary loops it has, and whether
it is flat.

Kept out of `operators` because nothing here needs a session, an operator or a
preview object. It reads a mesh and returns a value, which is also what makes
it the part worth testing directly.
"""
import math
from typing import TYPE_CHECKING

import mathutils

from . import generators
from . import geometry
from . import patch_data
from . import sides as sides_mod

if TYPE_CHECKING:
    import bpy

# One boundary loop's sides, already resolved to points.
LoopSides = list[list[mathutils.Vector]]


class PreparedPatch:
    """A patch's boundary, split into sides, one entry per boundary loop.

    Most patches have a single loop. Two loops means a band: a face with a hole
    in it, or a tube-like face with two rims -- see generators/ring.py. More
    than two (several holes) isn't handled: only the outer loop is used, and
    `num_loops` is what the panel warns from.
    """

    __slots__ = ("patch", "loops_sides", "loops_corner_ids", "num_loops",
                 "loops_neighbours", "corner_warning", "loops_corners_arbitrary")

    def __init__(
        self,
        patch: patch_data.Patch,
        loops_sides: list[LoopSides],
        loops_corner_ids: list[list[int]],
        num_loops: int,
        loops_neighbours: list[list[list[int]]] | None = None,
        corner_warning: str = "",
        loops_corners_arbitrary: list[bool] | None = None,
    ) -> None:
        # Per loop: whether its corners are the four quarter points of a circle
        # rather than anything the boundary actually states. Only that case is
        # flagged -- see sides.synthesise_corners_detail. `sidematch` is
        # allowed to cut such a loop somewhere else entirely, which is what
        # lets a disc take the vertices of the ring committed around it.
        self.loops_corners_arbitrary = list(loops_corners_arbitrary or [])
        # Why this patch's side count should not be trusted, or "". Set when
        # the angle test flagged *every* boundary vertex, which means the
        # threshold is doing nothing useful here -- see sides.corners_are_uniform.
        self.corner_warning = corner_warning
        self.patch = patch
        self.loops_sides = loops_sides  # [[side_points, ...], ...], outer loop first
        self.loops_corner_ids = loops_corner_ids  # source vertex id per corner, same order
        self.num_loops = num_loops
        # Face id across each *side*, per loop -- which patch the picker names
        # when you point at a boundary. Not the same list as
        # patch.boundary_neighbours, which is per boundary segment.
        self.loops_neighbours = loops_neighbours or []

    @property
    def is_ring(self) -> bool:
        return len(self.loops_sides) == 2

    @property
    def sides(self) -> LoopSides:
        """Sides of the outer loop -- what the single-loop generators take."""
        return self.loops_sides[0]

    @property
    def corner_source_ids(self) -> list[int]:
        """Every corner, outer loop first, matching the order generators fill
        GenerationResult.corner_local_indices in."""
        return [vid for loop_ids in self.loops_corner_ids for vid in loop_ids]


def prepare_patch(
    mesh: "bpy.types.Mesh",
    face_id: int,
    angle_threshold: float,
    small_side_tolerance: float,
    corner_method: str = 'BOTH',
) -> PreparedPatch | None:
    """Split patch `face_id`'s boundary into sides. Returns a PreparedPatch, or
    None if the patch has no usable boundary.
    """
    analysis = patch_data.analyse(mesh)
    patch = analysis.patches.get(face_id)
    if patch is None or not patch.boundary_loops:
        return None

    # Shared with every other caller this frame -- read it, never write to it.
    # `resolve_side_points` copies each point it takes out of here.
    positions = analysis.positions
    # The neighbour list is per loop, in the loop order the patch stores, so it
    # has to be reordered with the loops -- sorting one and not the other would
    # hand a hole's neighbours to the outer boundary.
    neighbours_of_loop = {id(loop): n for loop, n
                          in zip(patch.boundary_loops, patch.boundary_neighbours)}
    # Outer loop first: which loop comes out of compute_boundary_loops first is
    # hash order, so without this a holed face can be retopped on its hole.
    loops = patch_data.sort_loops_outer_first(patch.boundary_loops, positions)
    num_loops = len(loops)
    if num_loops > 2:
        loops = loops[:1]  # several holes: fall back to the outer boundary alone

    loops_sides = []
    loops_corner_ids = []
    loops_neighbours = []
    loops_arbitrary = []
    corner_warning = ""
    for loop in loops:
        segment_neighbours = neighbours_of_loop.get(id(loop))
        corners, arbitrary = sides_mod.resolve_corners_detail(
            loop, positions, angle_threshold=angle_threshold,
            neighbour_ids=segment_neighbours, method=corner_method,
            # Only a single-loop patch needs corners invented for it: that
            # is the one that would otherwise have no generator at all. A
            # ring goes straight to its own generator and pairs its two
            # loops itself, so corners it never asked for only get in the
            # way -- see resolve_corners.
            allow_synthesis=(num_loops == 1))
        loops_arbitrary.append(arbitrary)
        if not corner_warning and sides_mod.corners_are_uniform(
                loop, positions, sides_mod.detect_corners(loop, positions, angle_threshold)):
            corner_warning = (f"every boundary vertex is a corner ({len(loop)}) -- "
                              "raise the Corner Angle Threshold")
        side_indices = sides_mod.split_into_sides(
            loop, positions, angle_threshold=angle_threshold, corner_indices=corners)
        side_indices = sides_mod.merge_small_sides(side_indices, positions, small_side_tolerance)
        loops_sides.append(generators.base.resolve_side_points(side_indices, positions))
        # corner k = first vertex of side k
        loops_corner_ids.append([side[0] for side in side_indices])
        loops_neighbours.append(side_neighbours(loop, side_indices, segment_neighbours))

    return PreparedPatch(patch, loops_sides, loops_corner_ids, num_loops,
                         loops_neighbours, corner_warning, loops_arbitrary)


def group_side_points(
    subsides: "list[list[mathutils.Vector]]",
    counts: list[int],
) -> "list[mathutils.Vector]":
    """One polyline for a group, carrying exactly `total` segments.

    A group is several of the patch's real sides in a row, and the corner
    *between* two of them is still a B-rep vertex that a neighbouring patch
    welds to. Handing the generator the concatenated polyline and letting it
    resample evenly would slide the grid straight past that corner: our patch
    would have no vertex where the neighbour has one, which is a T-junction on
    a shared edge -- the very thing `nside`'s midpoints were taken off the
    boundary to stop doing.

    So the span is allocated to the sub-sides as integers first, each is
    resampled to its own share, and the results are concatenated. The corner
    between two of them is then a grid vertex by construction. The generator
    changes nothing afterwards: `resample_polyline_by_arclength` hands back the
    points it was given when the count already matches, which is the same thing
    that makes a matched side work.

    `counts` is the allocation, computed once by the caller: the spans the
    matching is checked against have to be the very numbers the polyline is
    built from, and two calls to the allocator could only ever agree by luck.
    """
    points: list = []
    for sub, count in zip(subsides, counts):
        resampled = geometry.resample_polyline_by_arclength(sub, count + 1)
        points.extend(resampled[:-1])
    points.append(subsides[-1][-1])
    return points


def allocate_group_segments(
    subsides: "list[list[mathutils.Vector]]",
    total: int,
    pinned: dict[int, int] | None = None,
) -> list[int]:
    """`total` segments shared out over the sub-sides, by arc length.

    At least one each, because a sub-side with none has lost its end corner,
    which is the whole reason this exists.

    `pinned` fixes a sub-side's count: that is a side carrying a committed
    neighbour's own vertices, which may not be resampled off them. The caller
    floors the group's span at `sum(pinned) + free`, so a pin set normally
    fits; when it cannot -- the span was typed down since -- the pins are
    dropped **whole**, exactly as `ring.allocate_segments` does and for the
    same reason: half a pin set is half a weld, which is a crack down a
    boundary that had just been arranged to close.
    """
    count = len(subsides)
    if count == 0:
        return []
    if count == 1:
        return [max(1, total)]

    pinned = dict(pinned or {})
    if pinned:
        fixed = sum(pinned.values())
        if fixed > total - (count - len(pinned)) or any(value < 1 for value in pinned.values()):
            pinned = {}

    lengths = [max(1e-9, sum((b - a).length for a, b in zip(sub, sub[1:])))
               for sub in subsides]
    free = [i for i in range(count) if i not in pinned]
    remaining = max(len(free), total - sum(pinned.values()))
    free_length = sum(lengths[i] for i in free) or 1.0

    counts = [0] * count
    for index, value in pinned.items():
        counts[index] = value

    # Largest remainder: floor each share, then hand the leftover segments to
    # whichever sub-sides were cut shortest by the flooring. Anything else
    # gives the last one in the list every rounding error.
    shares = {i: remaining * lengths[i] / free_length for i in free}
    for i in free:
        counts[i] = max(1, int(shares[i]))
    leftover = remaining - sum(counts[i] for i in free)
    order = sorted(free, key=lambda i: shares[i] - int(shares[i]), reverse=True)
    position = 0
    while leftover > 0 and order:
        counts[order[position % len(order)]] += 1
        leftover -= 1
        position += 1
    while leftover < 0:
        # Over-allocated by the floor of 1: take back from the longest that can
        # spare a segment, never from one that is down to its last.
        spare = [i for i in order if counts[i] > 1]
        if not spare:
            break
        counts[max(spare, key=lambda i: counts[i])] -= 1
        leftover += 1
    return counts


def side_neighbours(
    loop: patch_data.Loop,
    side_indices: list[sides_mod.Side],
    segment_neighbours: patch_data.Neighbours | None,
) -> list[list[int]]:
    """The Plasticity faces on the other side of each side, most-covering first.

    A side spans several boundary segments, and they don't have to agree: only
    topological corners are guaranteed to fall on a change of neighbour, so an
    angle-split side can straddle two of them. So this returns a *list* per
    side rather than a single id, and both halves of it matter:

    - `[0]` is the majority face, which is what the picker names in its report;
    - the whole list is the only geometry a match on that side may look at.

    That second one is what stops the picker reaching across the patch. Matching
    used to be pure proximity against every committed vertex in the result mesh,
    so a short side would happily collect points off a patch it does not touch
    at all -- anything that passed within the tolerance -- and hand back a
    vertex run tracing a loop around the neighbourhood instead of the shared
    edge. A side can only weld to what it actually borders, and the mesh says
    outright what that is.

    An empty list means the side borders nothing committed-able: an open edge
    of the solid, or a mesh with no patch data to read.
    """
    if not segment_neighbours or len(segment_neighbours) != len(loop):
        return [[] for _ in side_indices]

    count = len(loop)
    segment_of = {(loop[i], loop[(i + 1) % count]): i for i in range(count)}

    result = []
    for side in side_indices:
        tally = {}
        for a, b in zip(side, side[1:]):
            segment = segment_of.get((a, b))
            if segment is None:
                continue
            neighbour = segment_neighbours[segment]
            tally[neighbour] = tally.get(neighbour, 0) + 1
        tally.pop(patch_data.NO_NEIGHBOUR, None)
        result.append(sorted(tally, key=tally.get, reverse=True))
    return result


def patch_is_planar(
    mesh: "bpy.types.Mesh", face_id: int, tolerance_degrees: float
) -> bool:
    """True when every polygon of the patch faces (nearly) the same way.

    Deliberately cheap -- polygon normals only, no boundary walk or KD-tree --
    because it gates n-gon mode on every hover. A CAD plane tessellates into
    coplanar triangles, so its deviation is ~0; a bevel or fillet fans around
    and blows past any sane tolerance.
    """
    face_id_of_poly = patch_data.analyse(mesh).face_id_of_poly
    normals = [mesh.polygons[i].normal for i, fid in enumerate(face_id_of_poly)
               if fid == face_id]
    if not normals:
        return False

    average = mathutils.Vector((0.0, 0.0, 0.0))
    for normal in normals:
        average += normal
    if average.length < 1e-9:
        return False  # normals cancelling out is as non-flat as it gets
    average.normalize()

    limit = math.radians(tolerance_degrees)
    return all(normal.length > 1e-9 and average.angle(normal, 0.0) <= limit
               for normal in normals)
