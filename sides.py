"""Corner detection and side-splitting for a patch boundary loop.

- **angle**: where the boundary turns sharper than a threshold. Misses gentle
  features such as a shallow chamfer.
- **topology**: where the neighbouring patch changes, i.e. a real B-rep
  vertex. Misses a face bordered by a single neighbour.
- **synthesised**: a boundary with none gets corners from its shape, or four
  by arc length for a circle. A face with a single side cannot be generated.

`resolve_corners` combines them. The method is set per mode
(`corner_method_spans`, `corner_method_ngon`).
See "Corners come from two tests" in CLAUDE.md.
"""
import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Annotations only: this module stays free of Blender imports.
    import mathutils

# A closed boundary loop, as vertex indices, plus the table those index into.
Loop = list[int]
Positions = dict[int, "mathutils.Vector"]
# A side is the run of vertex indices from one corner up to and including the
# next; consecutive sides share their corner.
Side = list[int]


def _angle_at(
    prev_co: "mathutils.Vector", co: "mathutils.Vector", next_co: "mathutils.Vector"
) -> float:
    """Interior turning angle (degrees) of the boundary polyline at `co`.

    180 degrees means dead straight, smaller values mean a sharper corner.
    """
    v_in = co - prev_co
    v_out = next_co - co
    if v_in.length < 1e-9 or v_out.length < 1e-9:
        return 180.0
    v_in = v_in.normalized()
    v_out = v_out.normalized()
    dot = max(-1.0, min(1.0, v_in.dot(v_out)))
    deviation = math.degrees(math.acos(dot))  # 0 = straight, 180 = full reversal
    return 180.0 - deviation


def detect_corners(
    loop: Loop, positions: Positions, angle_threshold: float = 135.0
) -> list[int]:
    """Return the indices (into `loop`) of the vertices considered corners.

    `positions` maps vertex index -> mathutils.Vector (world/object space).
    A vertex is a corner if the boundary polyline turns sharper than
    `angle_threshold` degrees there.
    """
    n = len(loop)
    corners = []
    for i in range(n):
        prev_v = loop[(i - 1) % n]
        cur_v = loop[i]
        next_v = loop[(i + 1) % n]
        angle = _angle_at(positions[prev_v], positions[cur_v], positions[next_v])
        if angle < angle_threshold:
            corners.append(i)
    return corners


def detect_topological_corners(
    loop: Loop, neighbour_ids: list[int | None] | None
) -> list[int]:
    """Indices (into `loop`) where the patch on the other side changes.

    `neighbour_ids[i]` is the face across segment loop[i] -> loop[i+1], so the
    vertex loop[i] is a junction when the segment arriving at it and the
    segment leaving it face different patches.
    """
    count = len(loop)
    if not neighbour_ids or len(neighbour_ids) != count:
        return []
    return [i for i in range(count)
            if neighbour_ids[(i - 1) % count] != neighbour_ids[i]]


def deviations(loop: Loop, positions: Positions) -> list[float]:
    """How far the boundary bends at each vertex, in degrees.

    0 is dead straight, 90 is a square corner. The complement of `_angle_at`.
    """
    n = len(loop)
    return [180.0 - _angle_at(positions[loop[(i - 1) % n]],
                              positions[loop[i]],
                              positions[loop[(i + 1) % n]])
            for i in range(n)]


# A corner list is reduced only where the candidates, sorted by how much they
# bend, drop by at least this ratio from one to the next. A ratio, never an
# absolute angle: a real polygon has no such drop.
CORNER_CLIFF = 1.5
# Cutting down to a triangle takes a much clearer drop.
CORNER_CLIFF_TO_TRIANGLE = 2.5
# Never reduce below three corners.
MIN_DOMINANT_CORNERS = 3


def dominant_corners(
    loop: Loop,
    positions: Positions,
    corners: list[int],
    protected: "set[int] | tuple[int, ...]" = (),
) -> list[int]:
    """Drop the corners that are only noise, when the patch has too many.

    Cuts only where the ranking shows a cliff (CORNER_CLIFF). A boundary with
    none, like a real hexagon, is returned untouched.
    `protected` corners (the topological ones) are always kept and never ranked.
    Only runs on five or more candidates.
    """
    if len(corners) <= 4:
        return list(corners)

    turn = deviations(loop, positions)
    exempt = set(protected)
    ranked = sorted((index for index in corners if index not in exempt),
                    key=lambda index: turn[index], reverse=True)
    if not ranked:
        return list(corners)

    # `keep` ranked candidates survive; the cliff is between ranked[keep - 1]
    # and ranked[keep]. Tried nearest-to-four first, so a quad wins a tie.
    order = sorted(
        (keep for keep in range(1, len(ranked))
         if keep + len(exempt) >= MIN_DOMINANT_CORNERS),
        key=lambda keep: (abs(keep + len(exempt) - 4), -(keep + len(exempt))))

    cut = None
    best_ratio = 0.0
    for keep in order:
        total_kept = keep + len(exempt)
        needed = CORNER_CLIFF_TO_TRIANGLE if total_kept <= 3 else CORNER_CLIFF
        above = turn[ranked[keep - 1]]
        below = turn[ranked[keep]]
        ratio = above / below if below > 1e-9 else float("inf")
        # Strictly greater, so the earlier candidate in `order` wins a tie.
        if ratio >= needed and ratio > best_ratio:
            best_ratio = ratio
            cut = keep

    if cut is None:
        return list(corners)  # no cliff: every candidate is as real as the rest
    return sorted(exempt | set(ranked[:cut]))


# How much the boundary's turn may vary and still count as uniform.
UNIFORM_TURN_SPREAD = 1.6


def corners_are_uniform(loop: Loop, positions: Positions, corners: list[int]) -> bool:
    """True when the flagged corners are indistinguishable from the rest of the
    boundary -- every vertex bending about the same amount.

    A coarse circle and a real octagon look the same, so this only reports.
    The panel suggests raising the threshold. Never guess.
    """
    if len(corners) < 5 or len(corners) < len(loop):
        return False
    turn = sorted(deviations(loop, positions))
    if not turn or turn[-1] <= 1e-6:
        return False
    # Between two quantiles, never peak over median: a bimodal set (square
    # corners plus shallow kinks) would read as flat.
    last = len(turn) - 1
    low = turn[int(0.1 * last)]
    high = turn[int(0.9 * last)]
    return high < max(low, 1e-9) * UNIFORM_TURN_SPREAD


FALLBACK_CORNER_COUNT = 4


# The window a cornerless boundary's turn is measured over, as a share of its
# perimeter.
SHAPE_WINDOW = 0.06
# Peaks must stand this far above the average turn, or the boundary is a circle.
SHAPE_CONTRAST = 1.35
# Peaks nearer than this along the perimeter are the same feature seen twice.
SHAPE_SEPARATION = 0.12


def _cumulative_lengths(
    loop: Loop, positions: Positions
) -> tuple[list[float], float]:
    """Arc length at each vertex, plus the total, walking the closed loop."""
    n = len(loop)
    cumulative = [0.0]
    for i in range(n):
        cumulative.append(
            cumulative[-1] + (positions[loop[(i + 1) % n]] - positions[loop[i]]).length)
    return cumulative, cumulative[-1]


def _index_at_offset(
    cumulative: list[float], total: float, index: int, offset: float
) -> int:
    """The vertex roughly `offset` of arc length away from `index`."""
    n = len(cumulative) - 1
    target = (cumulative[index] + offset) % total
    return min(range(n), key=lambda i: min(abs(cumulative[i] - target),
                                           total - abs(cumulative[i] - target)))


def shape_turns(loop: Loop, positions: Positions) -> list[float]:
    """How much the boundary turns at each vertex, measured over SHAPE_WINDOW.

    Measured between a window before and a window after the vertex, so it reads
    the shape rather than the tessellation.
    """
    n = len(loop)
    cumulative, total = _cumulative_lengths(loop, positions)
    if total < 1e-12 or n < 4:
        return [0.0] * n

    window = total * SHAPE_WINDOW
    turns = []
    for i in range(n):
        before = positions[loop[_index_at_offset(cumulative, total, i, -window)]]
        here = positions[loop[i]]
        after = positions[loop[_index_at_offset(cumulative, total, i, window)]]
        turns.append(180.0 - _angle_at(before, here, after))
    return turns


def shape_corners(loop: Loop, positions: Positions) -> list[int]:
    """Corners recovered from the boundary's *shape*, or [] if it has none.

    Finds e.g. the two ends of a strip that no vertex marks sharply.
    Returns up to four, which picks the generator: two make a Wedge, three a
    Triangle, four a Quad. A circle gets [].
    See "A boundary with no corner still has a shape" in CLAUDE.md.
    """
    n = len(loop)
    turns = shape_turns(loop, positions)
    if not any(turns):
        return []

    strongest = max(turns)
    average = sum(turns) / n
    # A boundary that turns the same everywhere is a circle.
    if strongest < 1.0 or strongest < average * SHAPE_CONTRAST:
        return []

    cumulative, total = _cumulative_lengths(loop, positions)
    separation = total * SHAPE_SEPARATION

    def far_enough(candidate: int, chosen: list[int]) -> bool:
        for other in chosen:
            gap = abs(cumulative[candidate] - cumulative[other])
            if min(gap, total - gap) < separation:
                return False
        return True

    window = total * SHAPE_WINDOW

    def is_local_max(index: int) -> bool:
        """Whether the turn peaks here within the window, not merely is high."""
        here = turns[index]
        step = index
        while True:
            step = (step + 1) % n
            gap = abs(cumulative[step] - cumulative[index])
            if min(gap, total - gap) > window or step == index:
                break
            if turns[step] > here + 1e-9:
                return False
        step = index
        while True:
            step = (step - 1) % n
            gap = abs(cumulative[step] - cumulative[index])
            if min(gap, total - gap) > window or step == index:
                break
            if turns[step] > here + 1e-9:
                return False
        return True

    chosen = []
    for index in sorted(range(n), key=lambda i: turns[i], reverse=True):
        if turns[index] < strongest * 0.4:
            break  # everything below this is the flat part of the boundary
        if is_local_max(index) and far_enough(index, chosen):
            chosen.append(index)
        if len(chosen) == FALLBACK_CORNER_COUNT:
            break

    # One corner cannot split a loop into sides, so it is no better than none.
    return sorted(chosen) if len(chosen) >= 2 else []


def synthesise_corners(
    loop: Loop, positions: Positions, count: int = FALLBACK_CORNER_COUNT
) -> list[int]:
    """Corners for a boundary that has none to detect. See the detail form."""
    return synthesise_corners_detail(loop, positions, count)[0]


def synthesise_corners_detail(
    loop: Loop, positions: Positions, count: int = FALLBACK_CORNER_COUNT
) -> tuple[list[int], bool]:
    """(corners, whether they are *arbitrary*) for a boundary with none.

    Asks `shape_corners` first. Only a circle falls back to `count` points
    spread by arc length (never by index: tessellation is not uniform).

    Only that fallback is reported as arbitrary. `sidematch` may then move
    those corners onto a neighbour's vertices (`_recut_arbitrary_loop`).
    """
    n = len(loop)
    if n <= count:
        return list(range(n)), False

    from_shape = shape_corners(loop, positions)
    if from_shape:
        return from_shape, False

    cumulative, total = _cumulative_lengths(loop, positions)
    if total < 1e-12:
        return list(range(count)), True

    corners = []
    for k in range(count):
        target = total * k / count
        index = min(range(n), key=lambda i: abs(cumulative[i] - target))
        if index not in corners:
            corners.append(index)
    return sorted(corners), True


def complete_corners(
    loop: Loop,
    positions: Positions,
    corners: list[int],
    count: int = FALLBACK_CORNER_COUNT,
) -> list[int]:
    """Add corners until the loop has enough of them to be split into sides.

    One corner gives one side, which no generator accepts. The existing corner
    is kept and the rest are spread by arc length from it.
    """
    n = len(loop)
    if n <= count:
        return list(range(n))
    if len(corners) >= count:
        return sorted(corners)

    cumulative, total = _cumulative_lengths(loop, positions)
    if total < 1e-12:
        return sorted(set(corners) | set(range(count)))

    anchor = cumulative[corners[0]] if corners else 0.0
    chosen = list(corners)
    for k in range(1, count):
        target = (anchor + total * k / count) % total
        index = min(range(n),
                    key=lambda i: min(abs(cumulative[i] - target),
                                      total - abs(cumulative[i] - target)))
        if index not in chosen:
            chosen.append(index)
    return sorted(chosen)


def resolve_corners(
    loop: Loop,
    positions: Positions,
    angle_threshold: float = 135.0,
    neighbour_ids: list[int | None] | None = None,
    method: str = 'BOTH',
    allow_synthesis: bool = True,
) -> list[int]:
    """Corner indices for `loop`. See `resolve_corners_detail`."""
    return resolve_corners_detail(loop, positions, angle_threshold,
                                  neighbour_ids, method, allow_synthesis)[0]


def resolve_corners_detail(
    loop: Loop,
    positions: Positions,
    angle_threshold: float = 135.0,
    neighbour_ids: list[int | None] | None = None,
    method: str = 'BOTH',
    allow_synthesis: bool = True,
) -> tuple[list[int], bool]:
    """(corner indices, whether they are arbitrary) under the chosen method.

    The second value is True only for the quarter points of a circle
    (`synthesise_corners_detail`).

    'TOPOLOGY' falls back to the angle test when the boundary has no junction.
    When neither test finds anything, corners are synthesised.
    A ring passes `allow_synthesis=False`: invented corners on its two loops
    would shear the band.
    """
    angle_corners = set(detect_corners(loop, positions, angle_threshold))
    topo_corners = set(detect_topological_corners(loop, neighbour_ids))

    if method == 'ANGLE':
        corners = sorted(angle_corners)
    elif method == 'TOPOLOGY':
        # Junctions exactly as found, never ranked.
        return _fill_out(loop, positions, sorted(topo_corners) or sorted(angle_corners),
                         allow_synthesis)
    else:
        corners = sorted(angle_corners | topo_corners)

    corners = dominant_corners(loop, positions, corners, protected=topo_corners)
    return _fill_out(loop, positions, corners, allow_synthesis)


def _fill_out(
    loop: Loop, positions: Positions, corners: list[int], allow_synthesis: bool
) -> tuple[list[int], bool]:
    """Corners as resolved, topped up to a usable count when allowed.

    Fewer than two corners means fewer than two sides, which no generator
    takes. A ring turns `allow_synthesis` off.
    """
    if len(corners) >= 2 or not allow_synthesis:
        return corners, False
    if corners:
        # Anchored on a real corner, so not arbitrary.
        return complete_corners(loop, positions, corners), False
    return synthesise_corners_detail(loop, positions)


def split_into_sides(
    loop: Loop,
    positions: Positions,
    angle_threshold: float = 135.0,
    corner_indices: list[int] | None = None,
) -> list[Side]:
    """Split a closed boundary loop into sides at corner vertices.

    Returns a list of sides; each side is a list of vertex indices from one
    corner up to and including the next corner (so consecutive sides share
    their corner vertex, as expected for a boundary polygon).

    `corner_indices`, if given, replaces the angle test (the addon passes the
    set from `resolve_corners`).
    """
    n = len(loop)
    if n < 2:
        return [list(loop)]

    if corner_indices is None:
        corner_positions = set(detect_corners(loop, positions, angle_threshold))
    else:
        corner_positions = set(corner_indices)

    corner_positions = sorted(corner_positions)

    if not corner_positions:
        # No corner: the whole loop is one side.
        return [loop + [loop[0]]]

    sides = []
    for k in range(len(corner_positions)):
        start_i = corner_positions[k]
        end_i = corner_positions[(k + 1) % len(corner_positions)]
        side = []
        i = start_i
        while True:
            side.append(loop[i])
            if i == end_i:
                break
            i = (i + 1) % n
        sides.append(side)

    return sides


def merge_small_sides(
    index_sides: list[Side], positions: Positions, tolerance: float
) -> list[Side]:
    """Merge boundary sides shorter than `tolerance` into their next
    neighbor, repeatedly, until none remain below the threshold (or only a
    minimal 3-sided patch is left). Operates on vertex-index sides, as
    returned by split_into_sides.
    """
    if tolerance <= 0 or len(index_sides) <= 3:
        return index_sides

    sides = [list(s) for s in index_sides]

    def length(s: Side) -> float:
        return sum((positions[b] - positions[a]).length for a, b in zip(s, s[1:]))

    while len(sides) > 3:
        lengths = [length(s) for s in sides]
        i = min(range(len(sides)), key=lambda k: lengths[k])
        if lengths[i] >= tolerance:
            break
        n = len(sides)
        nxt = (i + 1) % n
        merged = sides[i][:-1] + sides[nxt]
        new_sides = []
        for k in range(n):
            if k == i:
                continue
            new_sides.append(merged if k == nxt else sides[k])
        sides = new_sides

    return sides
