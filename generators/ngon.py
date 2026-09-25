"""Single-n-gon patch: no interior grid, no spans. For flat faces.

The boundary is selected, never resampled: a vertex is kept every
`ngon_angle` degrees of turn (`side_points`). Straight sides collapse to their
corners, curves stay round, and a chamfer keeps its own vertex.

A face with holes is filled by `generate_holed`: each hole is bridged with two
edges into the face that contains it, so `k` holes give `k + 1` n-gons.
Every bridge is checked (`_bridge_is_clear`) before it is drawn.

A mode the user toggles, never selected by side count.
"""
import math
from typing import TYPE_CHECKING, Any

import mathutils

from .. import constants
from .. import geometry
from .base import Generator, GenerationResult

if TYPE_CHECKING:
    from mathutils.bvhtree import BVHTree

DEFAULT_ANGLE = 20.0  # degrees of boundary turn per kept point

# Turns below this are ignored, so tessellation noise never accumulates along a
# straight edge.
TURN_EPSILON = 0.05


def turn_at(
    prev_co: mathutils.Vector, co: mathutils.Vector, next_co: mathutils.Vector
) -> float:
    """How much the boundary deviates from straight at `co`, in degrees.
    0 is dead straight; a 90 degree corner returns 90.
    """
    incoming = co - prev_co
    outgoing = next_co - co
    if incoming.length < 1e-12 or outgoing.length < 1e-12:
        return 0.0
    return math.degrees(incoming.angle(outgoing, 0.0))


def side_turn_degrees(points: list[mathutils.Vector]) -> float:
    """Total turning angle along a polyline, in degrees.

    0 for a straight side however long it is, 180 for a half circle.
    """
    return sum(turn_at(a, b, c) for a, b, c in zip(points, points[1:], points[2:]))


def side_points(
    points: list[mathutils.Vector], angle_per_segment: float
) -> list[mathutils.Vector]:
    """The boundary vertices this side keeps, first and last always included.

    Keeps a vertex every time the accumulated turn reaches `angle_per_segment`
    degrees. A straight run collapses to its two corners, a curve keeps a vertex
    every `angle_per_segment` degrees, and a lone kink keeps its own vertex.
    Features shallower than `angle_per_segment` are dropped.
    """
    if len(points) <= 2:
        return list(points)

    angle_per_segment = max(1e-3, float(angle_per_segment))
    kept = [points[0]]
    accumulated = 0.0
    for i in range(1, len(points) - 1):
        turn = turn_at(points[i - 1], points[i], points[i + 1])
        if turn < TURN_EPSILON:
            continue
        accumulated += turn
        if accumulated >= angle_per_segment - 1e-9:
            kept.append(points[i])
            accumulated = 0.0
    kept.append(points[-1])
    return kept


def side_segments(points: list[mathutils.Vector], angle_per_segment: float) -> int:
    """How many segments a side ends up with -- never fewer than one, since a
    side always keeps its two corners.
    """
    return max(1, len(side_points(points, angle_per_segment)) - 1)


def loop_points(
    loop_sides: list[list[mathutils.Vector]],
    angle_per_segment: float,
    forced_segments: dict[int, int] | None = None,
) -> tuple[list[mathutils.Vector], list[int], list[int]]:
    """Walk one boundary loop's sides and return
    (points, corner_indices, segments_per_side).

    `points` is a closed ring of distinct points; `corner_indices` says where
    each side starts in it.

    `forced_segments` maps a side's index to an exact segment count. That side
    is resampled by arc length instead of by curvature.
    """
    forced_segments = forced_segments or {}
    points = []
    corners = []
    allocation = []
    for index, side in enumerate(loop_sides):
        forced = forced_segments.get(index)
        if forced:
            kept = geometry.resample_polyline_by_arclength(side, max(1, int(forced)) + 1)
        else:
            kept = side_points(side, angle_per_segment)
        allocation.append(max(1, len(kept) - 1))
        corners.append(len(points))
        points.extend(point.copy() for point in kept[:-1])
    return points, corners, allocation


def loop_allocation(
    loop_sides: list[list[mathutils.Vector]],
    angle_per_segment: float,
    forced_segments: dict[int, int] | None = None,
) -> list[int]:
    """Segments per side for one loop -- what the commit path registers."""
    return loop_points(loop_sides, angle_per_segment, forced_segments)[2]


def _arc(start: int, end: int, count: int) -> list[int]:
    """Indices from `start` forward to `end` inclusive, wrapping at `count`."""
    walk = [start]
    current = start
    while current != end:
        current = (current + 1) % count
        walk.append(current)
    return walk


def _plane_frame(
    points: list[mathutils.Vector],
) -> tuple[mathutils.Vector, mathutils.Vector, mathutils.Vector]:
    """(origin, tangent, bitangent) of a best-fit plane through `points`,
    by Newell's method.
    """
    normal = mathutils.Vector((0.0, 0.0, 0.0))
    count = len(points)
    for i, current in enumerate(points):
        nxt = points[(i + 1) % count]
        normal.x += (current.y - nxt.y) * (current.z + nxt.z)
        normal.y += (current.z - nxt.z) * (current.x + nxt.x)
        normal.z += (current.x - nxt.x) * (current.y + nxt.y)
    if normal.length < 1e-12:
        normal = mathutils.Vector((0.0, 0.0, 1.0))
    normal.normalize()

    # Any vector not parallel to the normal will do: UV rotation is arbitrary.
    reference = (mathutils.Vector((1.0, 0.0, 0.0))
                 if abs(normal.x) < 0.9 else mathutils.Vector((0.0, 1.0, 0.0)))
    tangent = (reference - normal * reference.dot(normal)).normalized()
    return points[0], tangent, normal.cross(tangent)


def _flatten(
    points: list[mathutils.Vector],
    frame: tuple[mathutils.Vector, mathutils.Vector, mathutils.Vector],
) -> list[tuple[float, float]]:
    """`points` in the plane frame's own 2D coordinates."""
    origin, tangent, bitangent = frame
    return [((p - origin).dot(tangent), (p - origin).dot(bitangent)) for p in points]


def _plane_uvs(points: list[mathutils.Vector]) -> list[tuple[float, float]]:
    """UVs from a best-fit plane through the boundary, scaled into 0..1."""
    raw = _flatten(points, _plane_frame(points))
    min_u = min(u for u, _ in raw)
    max_u = max(u for u, _ in raw)
    min_v = min(v for _, v in raw)
    max_v = max(v for _, v in raw)
    span_u = max(max_u - min_u, 1e-9)
    span_v = max(max_v - min_v, 1e-9)
    return [((u - min_u) / span_u, (v - min_v) / span_v) for u, v in raw]


# One segment in the patch's own plane: the two ends of a bridge already drawn.
Segment2D = tuple[tuple[float, float], tuple[float, float]]


# --- the 2D predicates a bridge is checked against -------------------------
#
# All of this runs in the patch's own plane: n-gon mode is only offered on a
# flat face.

def _side_of(
    a: tuple[float, float], b: tuple[float, float], p: tuple[float, float]
) -> float:
    """Twice the signed area of a-b-p: which side of a->b the point p is on."""
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def _segments_cross(
    a: tuple[float, float], b: tuple[float, float],
    c: tuple[float, float], d: tuple[float, float],
) -> bool:
    """Whether a-b and c-d cross *properly*, each strictly straddling the other.

    Touching at a shared endpoint is not a crossing: every bridge shares its
    endpoints with boundary edges.
    """
    d1 = _side_of(a, b, c)
    d2 = _side_of(a, b, d)
    d3 = _side_of(c, d, a)
    d4 = _side_of(c, d, b)
    if d1 == 0.0 or d2 == 0.0 or d3 == 0.0 or d4 == 0.0:
        return False
    return ((d1 > 0.0) != (d2 > 0.0)) and ((d3 > 0.0) != (d4 > 0.0))


def _point_inside(
    point: tuple[float, float], polygon: list[tuple[float, float]]
) -> bool:
    """Ray casting along +u, counting the edges the ray crosses."""
    inside = False
    count = len(polygon)
    for i in range(count):
        a = polygon[i]
        b = polygon[(i + 1) % count]
        if (a[1] > point[1]) != (b[1] > point[1]):
            t = (point[1] - a[1]) / (b[1] - a[1])
            if point[0] < a[0] + t * (b[0] - a[0]):
                inside = not inside
    return inside


def _bridge_is_clear(
    start: tuple[float, float],
    end: tuple[float, float],
    loops_uv: list[list[tuple[float, float]]],
    drawn: list[Segment2D],
) -> bool:
    """Whether a bridge from `start` to `end` stays inside the region.

    It must cross no boundary edge and no bridge already drawn.
    Its midpoint must be inside the outer loop and outside every hole: a bridge
    can cross no edge and still run outside a concave face.
    """
    midpoint = ((start[0] + end[0]) * 0.5, (start[1] + end[1]) * 0.5)
    if not _point_inside(midpoint, loops_uv[0]):
        return False
    for hole in loops_uv[1:]:
        if _point_inside(midpoint, hole):
            return False
    for loop in loops_uv:
        count = len(loop)
        for i in range(count):
            if _segments_cross(start, end, loop[i], loop[(i + 1) % count]):
                return False
    for a, b in drawn:
        if _segments_cross(start, end, a, b):
            return False
    return True


def _containing_face(
    point: tuple[float, float],
    faces: list[list[int]],
    flat: list[tuple[float, float]],
) -> int:
    """Which of the faces built so far encloses `point`.

    Falls back to the first face rather than raising: a visibly wrong cut beats
    a patch that refuses to generate.
    """
    for index, cycle in enumerate(faces):
        if _point_inside(point, [flat[v] for v in cycle]):
            return index
    return 0


# Cap on the bridge search, which runs on every hover.
MAX_BRIDGE_CANDIDATES = 400


def _cyclic_gap(a: int, b: int, count: int) -> int:
    """How far apart two positions are around a cycle, the short way round."""
    difference = abs(a - b) % count
    return min(difference, count - difference)


def _pairs_by_length(
    face_uv: list[tuple[float, float]], hole_uv: list[tuple[float, float]]
) -> list[tuple[int, int]]:
    """Every (face position, hole position) pair, shortest first."""
    pairs = []
    for i, a in enumerate(face_uv):
        for j, b in enumerate(hole_uv):
            pairs.append(((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2, i, j))
    pairs.sort()
    return [(i, j) for _, i, j in pairs]


def find_bridges(
    face_uv: list[tuple[float, float]],
    hole_uv: list[tuple[float, float]],
    loops_uv: list[list[tuple[float, float]]],
    drawn: list[Segment2D],
) -> tuple[tuple[int, int], tuple[int, int]]:
    """The two bridges cutting `hole_uv` into `face_uv`, as positions in each.

    Tries the nearest pair, then roughly opposite it, and keeps them when both
    are clear. Otherwise searches the pairs by length.
    When nothing is clear, returns the heuristic anyway: a visible bowtie beats
    a face that was never emitted.
    """
    n_face = len(face_uv)
    n_hole = len(hole_uv)
    candidates = _pairs_by_length(face_uv, hole_uv)

    def clear(i: int, j: int, extra: "list[Segment2D] | None" = None) -> bool:
        return _bridge_is_clear(face_uv[i], hole_uv[j], loops_uv,
                                drawn + (extra or []))

    i_a, j_a = candidates[0]
    i_b = (i_a + n_face // 2) % n_face
    j_b = min(range(n_hole),
              key=lambda j: (hole_uv[j][0] - face_uv[i_b][0]) ** 2
              + (hole_uv[j][1] - face_uv[i_b][1]) ** 2)
    if j_b == j_a:
        # The whole hole is nearest to one point: any second attachment will do.
        j_b = (j_a + n_hole // 2) % n_hole
    heuristic = ((i_a, j_a), (i_b, j_b))

    first = None
    if clear(i_a, j_a):
        first = (i_a, j_a)
        if i_b != i_a and j_b != j_a and clear(i_b, j_b,
                                               [(face_uv[i_a], hole_uv[j_a])]):
            return heuristic

    candidates = candidates[:MAX_BRIDGE_CANDIDATES]
    if first is None:
        for i, j in candidates:
            if clear(i, j):
                first = (i, j)
                break
    if first is None:
        return heuristic

    # Keep the second bridge a quarter of the way round from the first, so
    # neither face is a sliver. Relaxed when nothing that far works.
    for gap_face, gap_hole in ((max(1, n_face // 4), max(1, n_hole // 4)), (1, 1)):
        for i, j in candidates:
            if _cyclic_gap(i, first[0], n_face) < gap_face:
                continue
            if _cyclic_gap(j, first[1], n_hole) < gap_hole:
                continue
            if clear(i, j, [(face_uv[first[0]], hole_uv[first[1]])]):
                return first, (i, j)
    return heuristic


class NgonGenerator(Generator):
    name = constants.NGON

    def matches(self, num_sides: int) -> bool:
        # Never picked by side count -- see the module docstring.
        return False

    def default_spans(self, sides: list[list[mathutils.Vector]]) -> dict[str, int]:
        return {}

    def generate(
        self,
        sides: list[list[mathutils.Vector]],
        span_settings: dict[str, Any],
        bvh: "BVHTree | None" = None,
    ) -> GenerationResult:
        """`sides` are the outer loop's sides in boundary order.
        `bvh` is ignored: every vertex is already on the boundary.
        """
        if not sides:
            raise ValueError("NgonGenerator needs at least one side")

        angle = span_settings.get("ngon_angle", DEFAULT_ANGLE)
        verts, corner_local_indices, allocation = loop_points(
            sides, angle, span_settings.get("side_segments"))

        if len(verts) < 3:
            raise ValueError("N-gon needs at least 3 boundary points")

        uvs = _plane_uvs(verts)
        boundary_local_indices = list(range(len(verts)))
        faces = [tuple(range(len(verts)))]

        result = GenerationResult(verts, faces, uvs,
                                  corner_local_indices, boundary_local_indices)
        result.side_allocation = allocation
        return result

    def generate_holed(
        self,
        loops_sides: list[list[list[mathutils.Vector]]],
        span_settings: dict[str, Any],
        bvh: "BVHTree | None" = None,
    ) -> GenerationResult:
        """Fill a face with one or more holes: `k` holes come back as `k + 1`
        n-gons, joined by two bridge edges each.

        `loops_sides` is [outer_sides, hole_sides, ...], outer first.
        Corner indices are emitted in that loop order, to match
        `PreparedPatch.corner_source_ids`.

        Each hole is cut into the face already built that contains it.
        """
        if len(loops_sides) < 2:
            raise ValueError("generate_holed expects an outer loop and at least one hole")

        angle = span_settings.get("ngon_angle", DEFAULT_ANGLE)
        # One override map per loop, in the same order as loops_sides.
        forced = span_settings.get("side_segments") or []

        verts: list[mathutils.Vector] = []
        corner_local_indices: list[int] = []
        allocation: list[int] = []
        # Each loop as positions into `verts`, outer first.
        rings: list[list[int]] = []
        for loop_i, loop_sides in enumerate(loops_sides):
            points, corners, loop_allocation = loop_points(
                loop_sides, angle, forced[loop_i] if loop_i < len(forced) else None)
            if len(points) < 3:
                raise ValueError("N-gon with a hole needs 3+ points on each boundary")
            offset = len(verts)
            rings.append([offset + k for k in range(len(points))])
            corner_local_indices.extend(offset + c for c in corners)
            allocation.extend(loop_allocation)
            verts.extend(points)

        frame = _plane_frame(verts)
        flat = _flatten(verts, frame)
        loops_uv = [[flat[v] for v in ring] for ring in rings]

        # A face is a cycle of vertex indices; the outer loop is the only one
        # until the first hole is cut into it.
        faces: list[list[int]] = [list(rings[0])]
        drawn: list[Segment2D] = []

        for hole_i, hole in enumerate(rings[1:], start=1):
            hole_uv = loops_uv[hole_i]
            target = _containing_face(hole_uv[0], faces, flat)
            cycle = faces[target]
            face_uv = [flat[v] for v in cycle]
            (a1, b1), (a2, b2) = find_bridges(face_uv, hole_uv, loops_uv, drawn)
            drawn.append((face_uv[a1], hole_uv[b1]))
            drawn.append((face_uv[a2], hole_uv[b2]))

            # A hole is wound opposite to the outer loop, so walk both forward.
            n_face = len(cycle)
            n_hole = len(hole)
            faces[target] = ([cycle[k] for k in _arc(a1, a2, n_face)]
                             + [hole[k] for k in _arc(b2, b1, n_hole)])
            faces.append([cycle[k] for k in _arc(a2, a1, n_face)]
                         + [hole[k] for k in _arc(b1, b2, n_hole)])

        result = GenerationResult(verts, [tuple(face) for face in faces],
                                  _plane_uvs(verts), corner_local_indices,
                                  list(range(len(verts))))
        result.side_allocation = allocation
        return result
