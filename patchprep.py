"""Turning one Plasticity face into something a generator can fill.

Resolves a boundary's corners, splits it into sides, counts its loops and
tells whether the patch is flat.

A leaf module: needs no session, operator or preview object.
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

    Two loops is a band (generators/ring.py). More than two is several holes,
    which only the n-gon fill takes. For a span generator the holes are dropped
    (`keep_holes`). `num_loops` always counts every loop the patch has.
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
        # Per loop: whether its corners are arbitrary (the quarter points of a
        # circle). `sidematch` may move those.
        self.loops_corners_arbitrary = list(loops_corners_arbitrary or [])
        # Why this patch's side count should not be trusted, or "".
        # See sides.corners_are_uniform.
        self.corner_warning = corner_warning
        self.patch = patch
        self.loops_sides = loops_sides  # [[side_points, ...], ...], outer loop first
        self.loops_corner_ids = loops_corner_ids  # source vertex id per corner, same order
        self.num_loops = num_loops
        # Face ids across each side, per loop (`side_neighbours`). Not
        # patch.boundary_neighbours, which is per segment.
        self.loops_neighbours = loops_neighbours or []

    @property
    def is_ring(self) -> bool:
        return len(self.loops_sides) == 2

    @property
    def has_holes(self) -> bool:
        """Whether the outer boundary encloses any hole. Not `is_ring`, which
        is exactly two loops."""
        return len(self.loops_sides) > 1

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
    keep_holes: bool = False,
) -> PreparedPatch | None:
    """Split patch `face_id`'s boundary into sides. Returns a PreparedPatch, or
    None if the patch has no usable boundary.

    `keep_holes` keeps every boundary loop (n-gon fill only). Without it, a face
    with several holes comes back as its outer boundary alone.
    """
    analysis = patch_data.analyse(mesh)
    patch = analysis.patches.get(face_id)
    if patch is None or not patch.boundary_loops:
        return None

    # Shared and read-only: never write to it.
    positions = analysis.positions
    # Neighbours are per loop: they must be reordered with the loops.
    neighbours_of_loop = {id(loop): n for loop, n
                          in zip(patch.boundary_loops, patch.boundary_neighbours)}
    # Outer loop first: loop order out of compute_boundary_loops is random.
    loops = patch_data.sort_loops_outer_first(patch.boundary_loops, positions)
    num_loops = len(loops)
    if num_loops > 2 and not keep_holes:
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
            # Only a single-loop patch gets corners invented for it.
            allow_synthesis=(len(loops) == 1))
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

    Each sub-side is resampled to its own count, then concatenated, so the
    corner between two sub-sides stays a vertex a neighbour can weld to.
    Never resample the whole group evenly: that leaves a T-junction.

    `counts` comes from `allocate_group_segments`, called once by the caller.
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

    At least one each, so every sub-side keeps its end corner.

    `pinned` fixes a sub-side's count (a matched side). A pin set that cannot
    fit is dropped whole: half of one is a crack.
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

    # Largest remainder: floor each share, then give the leftovers to the
    # sub-sides that lost the most to the flooring.
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
        # Over-allocated by the floor of 1: take back from the longest, never
        # below one segment.
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

    A side can border several faces.
    `[0]` is the majority face, which the picker names.
    The whole list is the only geometry a match on that side may use.
    An empty list means an open edge, or no patch data.
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

    Polygon normals only: it runs on every hover.
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
        return False  # normals cancel out
    average.normalize()

    limit = math.radians(tolerance_degrees)
    return all(normal.length > 1e-9 and average.angle(normal, 0.0) <= limit
               for normal in normals)
