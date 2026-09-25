"""Band generator for patches bounded by TWO closed loops.

That covers two shapes at once:

- a face with a hole in it (a slot in a panel): outer boundary + hole boundary;
- a tube-like face with no corners at all (a fillet running all the way round,
  a cylinder wall): two rim loops.

Both are filled the same way -- a ring of quads running around the patch
(`span_u`, "around") and across the gap between the two loops (`span_v`,
"across"). Unlike the other generators this one is never chosen by side count;
`operators` picks it when a patch turns out to have two boundary loops.

How the two loops are paired decides whether the rungs run straight across;
see the note above `phase_align`.

Not done yet: the loops are paired by arc length, not by their corners, so a
hole shaped very differently from the outer boundary distorts the band. Spans
propagate out of a ring, not into it.
"""
import math
from typing import TYPE_CHECKING, Any

import mathutils

from .. import constants
from .. import geometry
from .base import Generator, GenerationResult

if TYPE_CHECKING:
    from mathutils.bvhtree import BVHTree

# One boundary loop, already split into sides and resolved to points: a ring
# takes two of them (outer first, then the hole).
Loop = list[list[mathutils.Vector]]


def polyline_length(points: list[mathutils.Vector]) -> float:
    return sum((b - a).length for a, b in zip(points, points[1:]))


def allocate_segments(
    lengths: list[float], total: int, pinned: dict[int, int] | None = None
) -> list[int]:
    """Split `total` segments among sides proportionally to `lengths`, with at
    least one segment per side (largest-remainder rounding).

    `total` is raised to the number of sides if smaller.

    `pinned` gives sides an exact count (a matched side must keep its own
    vertices). Only the remainder is shared among the other sides.
    A pin set that cannot fit is dropped whole: half of one is a crack.
    """
    n = len(lengths)
    if n == 0:
        return []

    pins = {index: int(count)
            for index, count in (pinned or {}).items()
            if 0 <= index < n and int(count) >= 1}
    free = [index for index in range(n) if index not in pins]
    fixed = sum(pins.values())

    total = max(int(total), n)
    if fixed + len(free) > total:
        pins, free, fixed = {}, list(range(n)), 0

    if not free:
        return [pins[index] for index in range(n)]

    remainder = total - fixed
    span_total = sum(lengths[index] for index in free)
    if span_total <= 0.0:
        raw = {index: remainder / len(free) for index in free}
    else:
        raw = {index: remainder * lengths[index] / span_total for index in free}

    alloc = [pins.get(index, 0) for index in range(n)]
    for index in free:
        alloc[index] = max(1, int(math.floor(raw[index])))
    diff = total - sum(alloc)

    if diff > 0:
        # Leftovers go to the sides with the largest dropped fraction.
        order = sorted(free, key=lambda i: raw[i] - math.floor(raw[i]), reverse=True)
        for k in range(diff):
            alloc[order[k % len(free)]] += 1
    while diff < 0:
        # Only a free side may give a segment back.
        reducible = [index for index in free if alloc[index] > 1]
        if not reducible:
            break
        alloc[max(reducible, key=lambda i: alloc[i])] -= 1
        diff += 1

    return alloc


def ring_from_sides(
    sides: list[list[mathutils.Vector]], total: int,
    pinned: dict[int, int] | None = None
) -> tuple[list[mathutils.Vector], list[int], list[int]]:
    """Resample a loop's sides into exactly `total` points walking around it.

    Returns (points, corner_indices, alloc). `corner_indices` are the positions
    of the patch corners in `points`.
    `pinned` is passed to `allocate_segments`. A matched side keeps its points.
    """
    alloc = allocate_segments([polyline_length(side) for side in sides], total,
                              pinned)

    points = []
    corner_indices = []
    for side, count in zip(sides, alloc):
        resampled = geometry.resample_polyline_by_arclength(side, count + 1)
        corner_indices.append(len(points))
        points.extend(resampled[:-1])  # the next side re-adds the shared corner

    return points, corner_indices, alloc


# "This corner has no vertex": emitted for a phased rim, whose points moved off
# the source vertices. `mesh_build` skips it.
NO_CORNER = -1

# --- is this really a band? --------------------------------------------------
#
# Two loops do not always make a band: a plate with a small hole is not one.
# A band has an even gap between its loops and comparable perimeters.
# Both limits are generous on purpose. See "Two boundary loops is not the same
# thing as a band" in CLAUDE.md.
BAND_GAP_SPREAD = 4.0       # widest gap over narrowest, sampled around the loop
BAND_PERIMETER_RATIO = 6.0  # outer perimeter over inner


def band_gaps(loops: list[Loop], samples: int = 16) -> list[float]:
    """Distance from a sample of outer-loop points to the nearest inner-loop
    point, walking the outer boundary."""
    outer = [p for side in loops[0] for p in side]
    inner = [p for side in loops[1] for p in side]
    if not outer or not inner:
        return []
    step = max(1, len(outer) // samples)
    return [min((p - q).length for q in inner) for p in outer[::step]]


def is_band(loops: list[Loop]) -> bool:
    """True when the two loops sit at a comparable distance all the way round."""
    if len(loops) != 2:
        return False

    gaps = band_gaps(loops)
    if len(gaps) < 3:
        return False
    widest = max(gaps)
    if widest <= 1e-12:
        return True  # the two loops coincide
    # Floored, so one sample on a point both loops share is not infinitely uneven.
    narrowest = max(min(gaps), widest * 1e-3)
    if widest > narrowest * BAND_GAP_SPREAD:
        return False

    outer = sum(polyline_length(side) for side in loops[0])
    inner = sum(polyline_length(side) for side in loops[1])
    if inner <= 1e-12:
        return False
    return max(outer, inner) <= min(outer, inner) * BAND_PERIMETER_RATIO


def loop_point_count(sides: list[list[mathutils.Vector]],
                     pinned: dict[int, int] | None = None) -> int:
    """How many distinct points a loop's sides already hold.

    A side of k+1 points contributes k. This is the count a matched loop must
    keep.
    `pinned` gives the count of each side a match replaced.
    """
    pins = pinned or {}
    return sum(pins.get(index, max(1, len(side) - 1))
               for index, side in enumerate(sides))


def around_count(loops: list[Loop], span_u: int) -> int:
    """Points around the ring for a given "around" span.

    Both loops get the same count, never fewer than either loop's side count.
    Shared with the commit path.
    """
    return max(int(span_u), len(loops[0]), len(loops[1]), 3)


# --- pairing the two loops --------------------------------------------------
#
# Every rung runs from outer[i] to inner[i], so how the loops are indexed
# against each other is the shape of the quads.
#
# `align_rings` searches whole index offsets, which leaves up to half a step of
# constant skew. `phase_align` removes it by choosing where the other loop is
# sampled from. Only on a cornerless rim: corners are welded by identity and
# must not move. See "A band's rungs must run straight across it" in CLAUDE.md.


def closed_points(side: list[mathutils.Vector]) -> list[mathutils.Vector]:
    """A closed side's points without the repeated closing vertex."""
    if len(side) > 1 and (side[0] - side[-1]).length < 1e-9:
        return list(side[:-1])
    return list(side)


def loop_area_vector(points: list[mathutils.Vector]) -> mathutils.Vector:
    """Newell's normal for a closed polyline: which way round it runs.

    Only its direction is meaningful.
    """
    normal = mathutils.Vector((0.0, 0.0, 0.0))
    n = len(points)
    for i in range(n):
        normal += points[i].cross(points[(i + 1) % n])
    return normal


def _segment_lengths(points: list[mathutils.Vector]) -> list[float]:
    """Length of every segment of the closed polyline, last wrapping to first."""
    n = len(points)
    return [(points[(i + 1) % n] - points[i]).length for i in range(n)]


def closest_arclength(points: list[mathutils.Vector], target: mathutils.Vector) -> float:
    """How far along the closed polyline the point nearest `target` sits."""
    n = len(points)
    best_distance = None
    best_at = 0.0
    travelled = 0.0
    for i in range(n):
        a, b = points[i], points[(i + 1) % n]
        segment = (b - a).length
        if segment > 1e-12:
            factor = (target - a).dot(b - a) / (segment * segment)
            factor = min(1.0, max(0.0, factor))
        else:
            factor = 0.0
        distance = (target - a.lerp(b, factor)).length
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_at = travelled + factor * segment
        travelled += segment
    return best_at


def rotate_closed(
    points: list[mathutils.Vector], distance: float
) -> list[mathutils.Vector]:
    """The same closed polyline, starting `distance` along it.

    Resampling anchors on the first point, so this is how a phase is applied.
    """
    lengths = _segment_lengths(points)
    total = sum(lengths)
    if total <= 1e-12:
        return list(points)
    distance %= total

    n = len(points)
    travelled = 0.0
    for i in range(n):
        if travelled + lengths[i] >= distance - 1e-12:
            remainder = distance - travelled
            factor = remainder / lengths[i] if lengths[i] > 1e-12 else 0.0
            start = points[i].lerp(points[(i + 1) % n], factor)
            rest = [points[(i + 1 + k) % n] for k in range(n)]
            # Drop a wrapped point on the new start: no zero-length segment.
            if rest and (rest[-1] - start).length < 1e-9:
                rest = rest[:-1]
            return [start] + rest
        travelled += lengths[i]
    return list(points)


def phase_align(
    outer: list[mathutils.Vector],
    inner_loop: list[mathutils.Vector],
    count: int,
    samples: int = 24,
) -> list[mathutils.Vector]:
    """Resample `inner_loop` into `count` points, each facing its outer partner.

    Each outer point's nearest arc length on the inner loop implies an offset.
    The phase is the circular mean of those offsets (0 and L are the same
    offset). Both directions are tried.
    """
    if len(inner_loop) < 3 or count < 3:
        return []

    best = None
    for candidate in (inner_loop, list(reversed(inner_loop))):
        lengths = _segment_lengths(candidate)
        total = sum(lengths)
        if total <= 1e-12:
            continue

        step = max(1, len(outer) // samples)
        accumulated = mathutils.Vector((0.0, 0.0))
        for i in range(0, len(outer), step):
            at = closest_arclength(candidate, outer[i])
            offset = (at - i * total / len(outer)) % total
            angle = 2.0 * math.pi * offset / total
            accumulated += mathutils.Vector((math.cos(angle), math.sin(angle)))

        if accumulated.length < 1e-9:
            # The offsets cancelled out: leave the phase at zero.
            phase = 0.0
        else:
            phase = (math.atan2(accumulated.y, accumulated.x)
                     % (2.0 * math.pi)) / (2.0 * math.pi) * total

        rotated = rotate_closed(candidate, phase)
        points = geometry.resample_polyline_by_arclength(
            rotated + [rotated[0]], count + 1)[:-1]
        cost = sum((outer[i] - points[i]).length
                   for i in range(0, len(outer), step))
        if best is None or cost < best[0]:
            best = (cost, points)

    return best[1] if best else []


def align_rings(
    outer: list[mathutils.Vector], inner: list[mathutils.Vector]
) -> tuple[list[mathutils.Vector], dict[int, int]]:
    """Re-index `inner` so that inner[i] faces outer[i].

    Recovers both the direction and the starting offset. Each candidate is
    scored on a subsample of the ring.

    Returns (aligned_points, position_of_original_index).
    """
    n = len(outer)
    samples = range(0, n, max(1, n // 16))

    def order_at(reverse: bool, offset: int, i: int) -> int:
        return (offset - i) % n if reverse else (offset + i) % n

    best = None
    for reverse in (False, True):
        for offset in range(n):
            cost = sum((outer[i] - inner[order_at(reverse, offset, i)]).length
                       for i in samples)
            if best is None or cost < best[0]:
                best = (cost, reverse, offset)

    _cost, reverse, offset = best
    order = [order_at(reverse, offset, i) for i in range(n)]
    position_of = {original: i for i, original in enumerate(order)}
    return [inner[original] for original in order], position_of


class RingGenerator(Generator):
    name = constants.RING

    def matches(self, num_sides: int) -> bool:
        return False  # chosen by loop count, never by side count

    def _target_edge(self, loops: list[Loop]) -> float:
        lengths = [(b - a).length
                   for sides in loops for side in sides for a, b in zip(side, side[1:])]
        return (sum(lengths) / len(lengths)) if lengths else 1.0

    def default_spans(self, loops: list[Loop]) -> dict[str, int]:
        outer_sides, inner_sides = loops[0], loops[1]
        target_edge = max(self._target_edge(loops), 1e-6)

        outer_points = [p for side in outer_sides for p in side]
        inner_points = [p for side in inner_sides for p in side]

        perimeter = (sum(polyline_length(side) for side in outer_sides)
                     + sum(polyline_length(side) for side in inner_sides)) * 0.5
        around = max(3, round(perimeter / target_edge))

        # How wide the band is: the typical distance from the outer boundary to
        # the nearest point of the hole, sampled rather than measured in full.
        step = max(1, len(outer_points) // 8)
        gaps = [min((p - q).length for q in inner_points)
                for p in outer_points[::step]] if inner_points else []
        across = max(1, round((sum(gaps) / len(gaps)) / target_edge)) if gaps else 1

        return {"span_u": around, "span_v": across}

    def generate(
        self,
        loops: list[Loop],
        span_settings: dict[str, Any],
        bvh: "BVHTree | None" = None,
    ) -> GenerationResult:
        if len(loops) != 2:
            raise ValueError("RingGenerator expects exactly two boundary loops")

        across = max(1, int(span_settings.get("span_v", 1)))

        # A locked loop carries a matched neighbour's vertices. It leads the
        # band, and the other loop is aligned onto it. See "A matched rim leads
        # the band" in CLAUDE.md.
        locked = {index for index in span_settings.get("locked_loops", ()) or ()
                  if index in (0, 1)}
        # {loop: {side within the loop: segments}} for every matched side.
        matched = span_settings.get("matched_sides") or {}
        pins_for = {index: dict(matched.get(index, {}) or {}) for index in (0, 1)}
        around = around_count(loops, span_settings.get("span_u", 1))
        for index in sorted(locked):
            # One count only. `sidematch._honours` already dropped disagreeing
            # matches.
            around = max(loop_point_count(loops[index], pins_for[index]),
                         len(loops[0]), len(loops[1]), 3)
            break

        # The locked loop leads, else the outer one.
        lead_index = 1 if (1 in locked and 0 not in locked) else 0
        free_index = 1 - lead_index
        lead_sides, free_sides = loops[lead_index], loops[free_index]
        outer_sides = loops[0]  # for the winding check further down

        lead, lead_corners, lead_alloc = ring_from_sides(
            lead_sides, around, pins_for[lead_index])
        n = len(lead)
        if n < 3:
            raise ValueError("Ring patch boundary is degenerate")

        # Only a cornerless, unlocked rim is phased (see the note above
        # phase_align).
        free_cornerless = len(free_sides) == 1 and free_index not in locked
        phased = (phase_align(lead, closed_points(free_sides[0]), n)
                  if free_cornerless else [])

        if phased:
            free = phased
            free_corners, free_alloc = [0], [n]
            free_position_of = {i: i for i in range(n)}
        else:
            free, free_corners, free_alloc = ring_from_sides(
                free_sides, around, pins_for[free_index])
            if len(free) != n:
                raise ValueError("Ring patch boundary is degenerate")
            free, free_position_of = align_rings(lead, free)

        # `align_rings` re-indexed the free loop: always look its corners up
        # through the map it returns.
        lead_position_of = {i: i for i in range(n)}
        if lead_index == 0:
            outer, outer_corners, outer_alloc = lead, lead_corners, lead_alloc
            inner, inner_corners, inner_alloc = free, free_corners, free_alloc
            outer_position_of, inner_position_of = lead_position_of, free_position_of
            outer_phased, inner_phased = False, bool(phased)
        else:
            outer, outer_corners, outer_alloc = free, free_corners, free_alloc
            inner, inner_corners, inner_alloc = lead, lead_corners, lead_alloc
            outer_position_of, inner_position_of = free_position_of, lead_position_of
            outer_phased, inner_phased = bool(phased), False

        verts = []
        uvs = []
        for r in range(across + 1):
            t = r / across
            # Annulus UVs, closed in UV space too.
            radius = 1.0 - 0.6 * t
            # Boundary rows stay where the loops put them, except a phased rim,
            # which is off the surface and gets reprojected like the interior.
            reproject = (0 < r < across
                         or (inner_phased and r == across)
                         or (outer_phased and r == 0))
            for i in range(n):
                point = outer[i].lerp(inner[i], t)
                if reproject and bvh is not None:
                    hit = bvh.find_nearest(point)
                    if hit and hit[0] is not None:
                        point = hit[0]
                verts.append(point)

                angle = 2.0 * math.pi * i / n
                uvs.append((0.5 + 0.5 * radius * math.cos(angle),
                            0.5 + 0.5 * radius * math.sin(angle)))

        def index_of(r: int, i: int) -> int:
            return r * n + (i % n)

        # The band faces the way the outer loop winds. Flip the quads when the
        # outer row came back reversed. See "Which way the band faces" in
        # CLAUDE.md.
        flipped = (loop_area_vector(outer).dot(
            loop_area_vector([point for side in outer_sides
                              for point in side[:-1]])) < 0.0)

        faces = []
        for r in range(across):
            for i in range(n):
                quad = (index_of(r, i), index_of(r, i + 1),
                        index_of(r + 1, i + 1), index_of(r + 1, i))
                faces.append(quad[::-1] if flipped else quad)

        # Outer loop corners first, then the hole's, as the caller expects.
        # A phased rim's corner is NO_CORNER, never dropped: the caller zips
        # this list with corner_source_ids by position.
        corner_local_indices = [
            NO_CORNER if outer_phased else index_of(0, outer_position_of[c])
            for c in outer_corners]
        corner_local_indices += [
            NO_CORNER if inner_phased else index_of(across, inner_position_of[c])
            for c in inner_corners]
        boundary_local_indices = [index_of(0, i) for i in range(n)]
        boundary_local_indices += [index_of(across, i) for i in range(n)]

        result = GenerationResult(verts, faces, uvs, corner_local_indices, boundary_local_indices)
        # Per-side counts, for the commit path to register.
        result.side_allocation = (outer_alloc, inner_alloc)
        return result
