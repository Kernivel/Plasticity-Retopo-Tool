"""Which vertices a patch's side has to reproduce, and where they come from.

Two patches only weld along a shared boundary if they land on the *same
vertices*, not merely on the same count -- a neighbour committed as an n-gon
put its points where the boundary curves, and a grid resampling evenly to that
same count lands between them every time. So a side takes the neighbour's own
committed vertices, and the generator is told the count that reproduces them
(`geometry.resample_polyline_by_arclength` hands a polyline straight back when
asked for exactly as many points as it holds, so no generator needed changing).

Three things decide what a side may take:

- **what it borders.** A side can only match the Plasticity faces actually
  across it, which the mesh states per boundary segment. Proximity alone cannot
  tell those from a patch that merely passes nearby, and matching used to be
  proximity alone -- which is how a side ended up following a run of vertices
  tracing a loop through its neighbourhood instead of the edge it shares.
- **who asked.** An automatic match takes only an exact answer. A pin -- you
  pointed at the side, so you have said which neighbour you mean -- may reach
  one that has drifted, by `match_margin`.
- **whether the spans still allow it.** A grid has one count per *direction*,
  so two sides wanting different counts along one axis cannot both be honoured,
  and a count the user typed since releases the match rather than being
  overruled by it.

Kept out of `operators` for the same reason `patchprep` is, plus one more: the
overlay draws what a match would take, and a draw handler must never be able to
reach the operators module.
"""
import json
from typing import TYPE_CHECKING

from . import constants
from . import mesh_build

if TYPE_CHECKING:
    import bpy
    import mathutils

    from . import patchprep
    from . import state as state_mod

# A committed patch's boundary vertices, keyed by the face id owning them --
# what `mesh_build.committed_boundary_map` hands back.
CommittedMap = dict[int, "list[mathutils.Vector]"]
# One winning match per span key: the side, the points it takes, whether pinned.
Winners = dict[str, "tuple[SideReference, list[mathutils.Vector], bool]"]


class SideReference:
    """One side of the active patch, ready to be pointed at in the viewport.

    `span` is what the neighbour across this side put along it, or None when
    there is nothing to match -- that's the difference between a side the
    picker offers and one it greys out.
    """

    __slots__ = ("index", "loop", "in_loop", "points", "match_points",
                 "neighbours", "reason", "strict_points",
                 "match_world", "applied", "applied_points",
                 "outvoted", "tied_points", "tied_key")

    def __init__(
        self,
        index: int,
        loop: int,
        in_loop: int,
        points: "list[mathutils.Vector]",
        match_points: "list[mathutils.Vector] | None",
        neighbours: list[int] | None,
        reason: str = "",
        strict_points: "list[mathutils.Vector] | None" = None,
        match_world: "list[mathutils.Vector] | None" = None,
    ) -> None:
        # Points this side may be substituted with even though another side
        # won its span -- because the two want the *same* count. See
        # `_winning_matches`. None when it is not a co-winner; `tied_key` is
        # the span it ties on, so a side of another span with a coincidentally
        # equal count is never swept in with it.
        self.tied_points = None
        self.tied_key = ""
        # The same points in world space, for the overlay to draw. Kept here
        # rather than transformed at draw time: a draw handler runs on every
        # redraw and has no business recomputing what generation knew.
        self.match_world = match_world or []
        # Why this side can't be matched, when it can't -- an opaque refusal on
        # a side that visibly touches retopology is impossible to act on.
        self.reason = reason
        # What the *automatic* matching is allowed to take: found without the
        # picker's margin, so it never reaches for something merely nearby.
        # `match_points` is the generous answer, for a side you pointed at.
        self.strict_points = strict_points
        self.index = index      # flat index across every loop, in order
        self.loop = loop        # which boundary loop it belongs to
        self.in_loop = in_loop  # its index within that loop
        self.points = points    # world space, for drawing and hit-testing
        # The neighbour's own committed vertices along this side, in the source
        # object's local space -- the thing that actually gets matched. None
        # when there is nothing to match, or when the neighbour covers only
        # part of the side.
        self.match_points = match_points
        # Every Plasticity face across this side, most-covering first. The
        # match is confined to these; see _side_neighbours.
        self.neighbours = list(neighbours or [])
        # Whether this side's polyline was actually *replaced* this generation,
        # and by which points. "Could be matched" and "is being matched" are
        # different questions and the viewport used to answer only the first:
        # a side that lost a span collision, one the resolved span no longer
        # honours and one nobody asked to match all drew the same green as a
        # side whose vertices the preview is genuinely reproducing. Set by
        # `apply_side_matches`, which is the only place that knows.
        self.applied = False
        self.applied_points: "list[mathutils.Vector]" = []
        # It wanted a match and lost -- another side drove the same span, or
        # the span the user typed can no longer reproduce these points.
        self.outvoted = False

    @property
    def neighbour(self) -> int | None:
        """The face the picker names -- the one covering most of the side."""
        return self.neighbours[0] if self.neighbours else None

    @property
    def available(self) -> bool:
        return self.match_points is not None

    @property
    def span(self) -> int | None:
        """Segments the neighbour put along this side."""
        return len(self.match_points) - 1 if self.match_points else None


# Rebuilt every time a patch is generated, dropped by a reload. The overlay and
# the modal both read it; nothing persists it, because it holds Vectors and
# describes a preview that only exists while the session runs.
_active_sides: list[SideReference] = []


def active_sides() -> list[SideReference]:
    return _active_sides


def build_side_references(
    context: "bpy.types.Context",
    obj: "bpy.types.Object",
    prepared: "patchprep.PreparedPatch",
    face_id: int | None = None,
) -> list[SideReference]:
    """The active patch's sides, with the geometry each of them could match.

    Walks every loop, so a ring or a holed n-gon offers its hole's sides too --
    those border committed neighbours just as much as the outer ones do.

    Each side is matched **only against the patches it actually borders**
    (`SideReference.neighbours`). Matching used to run against every committed
    vertex in the result mesh and keep whatever fell within the tolerance, which
    is how a short side ended up following a loop around its neighbourhood
    instead of the edge it shares: proximity alone cannot tell "the patch across
    this edge" from "a patch that happens to pass nearby". The mesh already says
    which is which, per boundary segment.
    """
    global _active_sides

    state = context.scene.plasticity_retop
    matrix = obj.matrix_world
    # The patch being *generated*, which is not the same thing as the one the
    # scene currently calls active: picking a committed patch generates it
    # before recording it as active, so reading the active id here left the
    # patch's own committed geometry in its own pool and it matched itself --
    # a re-edit came back with whatever spans reproduced what was already there
    # instead of the ones it was committed with.
    if face_id is None:
        face_id = state.active_face_id
    # Grouped once for the whole patch: this walks the result mesh, and doing it
    # per side made a hover cost that times the side count.
    committed = mesh_build.committed_boundary_map(obj)

    # One reach for the whole patch, from its longest side. A neighbour's drift
    # is an absolute distance, so a per-side share of the margin left a short
    # side unable to see what the long side beside it matched without trouble.
    reference_length = max(
        (sum((b - a).length for a, b in zip(side, side[1:]))
         for loop_sides in prepared.loops_sides for side in loop_sides),
        default=0.0)

    # Corners nothing in the model agrees on get moved onto the ones a
    # neighbour already committed, *before* any side is measured against it.
    # Everything below then works as it always has -- the sides simply start
    # and end on the neighbour's own vertices instead of a quarter of the way
    # round a circle. See `_recut_arbitrary_loop`.
    if state.auto_match_neighbours:
        for loop_i, arbitrary in enumerate(prepared.loops_corners_arbitrary):
            if arbitrary:
                _recut_arbitrary_loop(state, prepared, loop_i, committed,
                                      face_id, reference_length)

    # Every side of the patch, so each match can be confined to the side it is
    # actually nearest -- see the `rivals` argument below.
    all_sides = [side for loop_sides in prepared.loops_sides for side in loop_sides]

    references = []
    index = 0
    for loop_i, loop_sides in enumerate(prepared.loops_sides):
        neighbours_of_side = (prepared.loops_neighbours[loop_i]
                              if loop_i < len(prepared.loops_neighbours) else [])
        for side_i, side in enumerate(loop_sides):
            neighbours = (neighbours_of_side[side_i]
                          if side_i < len(neighbours_of_side) else [])
            pool = _match_pool(committed, neighbours, face_id)

            # Two answers, two reaches -- but *one* idea of what makes two
            # points the same vertex. The strict tolerance is that idea (it is
            # the weld distance), and the generous one is only about how far
            # off the side a neighbour may have drifted; deduping the generous
            # answer at its own reach merges consecutive neighbour vertices and
            # halves the count a pin reproduces.
            strict = mesh_build.side_match_tolerance(
                state, side, reference_length=reference_length)
            rivals = [other for other in all_sides if other is not side]
            # `partial` on the picker's answer only. A neighbour covering
            # part of a side is completed at its own spacing (see
            # `match_side_to_points`), and that is a real decision about the
            # rest of the side -- so it is taken when you point at the side and
            # not on every side of every patch you hover past. Automatic
            # matching keeps the strict answer, as it does for the margin.
            match_points, reason = mesh_build.match_side_to_points(
                pool, side, mesh_build.side_match_tolerance(
                    state, side, margin=True, reference_length=reference_length),
                merge=strict, rivals=rivals, partial=True)
            strict_points, _strict_reason = mesh_build.match_side_to_points(
                pool, side, strict, merge=strict, rivals=rivals)
            if not pool:
                reason = _empty_pool_reason(neighbours, committed, face_id)
            references.append(SideReference(
                index=index, loop=loop_i, in_loop=side_i,
                points=[matrix @ point for point in side],
                match_points=match_points,
                neighbours=neighbours,
                reason=reason,
                strict_points=strict_points,
                match_world=[matrix @ point for point in (match_points or ())],
            ))
            index += 1

    _active_sides = references
    return references


def _recut_arbitrary_loop(
    state: "state_mod.RetopPatchState",
    prepared: "patchprep.PreparedPatch",
    loop_i: int,
    committed: CommittedMap,
    face_id: int | None,
    reference_length: float,
) -> bool:
    """Cut a cornerless loop where a committed neighbour put its vertices.

    A disc, a circular pocket floor, the cap of a cylinder: nothing on the
    boundary is a corner, so `sides.synthesise_corners` cuts it into four at
    the quarter points of its arc length. Those four are arbitrary -- the loop
    had to be split somewhere to have sides, and no other part of the model
    agrees on where. Which is exactly why matching could never work on one.

    The neighbour has its own vertices along that same circle, at its own
    phase, and the quarter points fall *between* them. The endpoint rule in
    `match_side_to_points` then asks each side for a committed vertex at a
    corner the neighbour has no reason to have one at, and whether it finds one
    is a coin toss per corner: measured on a truncated cone's cap, two of the
    four sides landed within 0.0026 of a rim vertex and matched, while the
    other two had theirs claimed by the side next door (it lies *on* that one)
    and refused with "neighbour stops short of this side's start". Nothing
    about the tolerances fixes that -- widening them far enough to swallow a
    whole vertex spacing is what the half-cell offset rule exists to prevent.

    So the loop is re-cut instead. The whole boundary is matched **once**, as
    the closed side it really is -- which is the path `_close_matched_ring`
    already handles, rotation and all -- and the sides are then carved out of
    the neighbour's own ring of points. Every corner lands on a neighbour
    vertex at distance zero, so the strict tolerance passes and *automatic*
    matching fires, where before even a hand pin refused half the sides.

    Two things this deliberately gives up, both of which the loop had nothing
    to lose in the first place:

    - the corner ids, blanked to `NO_SOURCE` because the new corners are not
      source vertices at all. They weld by proximity like every other boundary
      point, and span propagation out of this patch stops -- its corners were
      never B-rep vertices, so the pairs it would have registered named
      nothing any neighbour could look up.
    - the side count is *kept*, which is what makes this safe everywhere else:
      `find_generator` still sees four sides, and the commit path -- which
      re-prepares the patch and replays the same references by index -- still
      lines up.

    Only a loop `sides` flagged arbitrary comes here. A shape corner (the end
    of a strip, a slot's cap) is a fact about the boundary and is never moved.

    Returns whether it re-cut.
    """
    sides = prepared.loops_sides[loop_i]
    count = len(sides)
    if count < 2:
        return False

    per_side = (prepared.loops_neighbours[loop_i]
                if loop_i < len(prepared.loops_neighbours) else [])
    neighbours = list(dict.fromkeys(
        face for side_faces in per_side for face in side_faces))
    pool = _match_pool(committed, neighbours, face_id)
    if len(pool) <= count:
        return False

    # The loop as one closed polyline: consecutive sides share an endpoint.
    loop_points = list(sides[0])
    for side in sides[1:]:
        loop_points.extend(side[1:])
    if (loop_points[0] - loop_points[-1]).length > 1e-12:
        loop_points.append(loop_points[0].copy())

    # The strict answer, never the picker's margin: this runs unasked, on every
    # hover, and moving a patch's corners is not something to do on a neighbour
    # that merely passes nearby.
    strict = mesh_build.side_match_tolerance(
        state, loop_points, reference_length=reference_length)
    ring, _reason = mesh_build.match_side_to_points(
        pool, loop_points, strict, merge=strict)
    if ring is None or len(ring) <= count:
        return False

    closed = ring[:-1]  # `_close_matched_ring` repeats the first to close it
    n = len(closed)
    if n <= count:
        return False

    # Anchored on the point nearest the corner the loop already had, so the
    # cut moves as little as it can: a hover that re-cuts to a different
    # rotation every frame would be its own kind of broken.
    anchor = min(range(n), key=lambda i: (closed[i] - sides[0][0]).length)
    lengths = _opposed_segment_counts(n, count)
    if lengths is None:
        return False

    new_sides = []
    at = anchor
    for segments in lengths:
        piece = [closed[(at + step) % n] for step in range(segments + 1)]
        new_sides.append([point.copy() for point in piece])
        at += segments

    # Which faces each new side borders, carried over from whichever old side
    # it runs along. The cut points barely move, so this is a relabelling
    # rather than a re-derivation -- but it has to happen, or a side could be
    # matched against a patch it does not touch.
    new_neighbours = []
    for piece in new_sides:
        middle = piece[len(piece) // 2]
        nearest = min(
            range(count),
            key=lambda i: mesh_build._distance_to_polyline(middle, sides[i])[0])
        new_neighbours.append(list(per_side[nearest])
                              if nearest < len(per_side) else [])

    prepared.loops_sides[loop_i] = new_sides
    prepared.loops_corner_ids[loop_i] = [mesh_build.NO_SOURCE] * count
    if loop_i < len(prepared.loops_neighbours):
        prepared.loops_neighbours[loop_i] = new_neighbours
    return True


def _opposed_segment_counts(n: int, count: int) -> list[int] | None:
    """How to share `n` segments between `count` sides of a re-cut loop.

    As evenly as possible, but with **opposite sides equal** wherever the
    arithmetic allows, and that is the whole reason this is not one line. A
    grid has one span per direction, so sides 0 and 2 of a quad are the same
    number: hand them 12 and 13 and only one of the two can be honoured, the
    other is outvoted by `_winning_matches` and left on the CAD tessellation --
    a crack down one half of a disc that had just been cut to weld. Spreading
    the remainder over *pairs* costs nothing (the two counts still differ by at
    most one, exactly as before) and lets all four sides be reproduced.

    An odd remainder on an even side count cannot be paired, and neither can an
    odd side count; both fall back to spreading one at a time, which is no
    worse than what a boundary of that length could ever have offered.
    """
    if count < 2 or n < count:
        return None
    counts = [n // count] * count
    remainder = n - sum(counts)
    if count % 2 == 0 and remainder % 2 == 0:
        half = count // 2
        for k in range(remainder // 2):
            counts[k % half] += 1
            counts[k % half + half] += 1
    else:
        for k in range(remainder):
            counts[k % count] += 1
    return counts if all(value >= 1 for value in counts) else None


def _empty_pool_reason(
    neighbours: list[int], committed: CommittedMap, active_face_id: int | None
) -> str:
    """Why a side had nothing to look at -- which is not the same as having
    looked and found nothing. "No neighbour" on a side that plainly runs
    against a finished patch is the refusal nobody can act on.
    """
    others = [face_id for face_id in neighbours if face_id != active_face_id]
    if not others:
        return "nothing borders this side"
    if len(others) == 1:
        return f"patch {others[0]} isn't retopologized yet"
    return "none of this side's neighbours is retopologized yet"


def _match_pool(
    committed: CommittedMap, neighbours: list[int], active_face_id: int | None
) -> "list[mathutils.Vector]":
    """The committed vertices a side is allowed to match.

    Strictly the Plasticity faces across this side, plus any untracked
    retopology. Nothing else, ever -- and *nothing at all* when none of those
    faces has been committed yet, rather than falling back to the rest of the
    result mesh. Proximity cannot tell "the patch across this edge" from "a
    patch that happens to run close by", which is how a side used to collect a
    vertex run tracing a loop through its neighbourhood: a thin wall, a face
    stacked a fraction above another, two sheets meeting at a shallow angle all
    put committed vertices well inside the tolerance of a side they do not
    touch. The mesh already says, per boundary segment, which face is across.

    Untracked retopology (`NO_PATCH`) is the one thing that cannot be checked
    that way -- it predates patch ids and belongs to no named face -- so it
    stays available to every side. That is also the only case a mesh with no
    patch data at all can produce.
    """
    if not committed:
        return []

    wanted = [face_id for face_id in neighbours
              if face_id in committed and face_id != active_face_id]
    if mesh_build.NO_PATCH in committed:
        wanted.append(mesh_build.NO_PATCH)

    return mesh_build.flatten_boundary_points(committed, wanted, active_face_id)


def clear_side_references() -> None:
    global _active_sides
    _active_sides = []


# What a manual pin on a side means. Stored rather than the segment count it
# resolves to: the count is recomputed from live geometry every regeneration,
# so keeping a stale copy of it could only ever disagree.
PIN_NEIGHBOUR = "N"  # follow the committed patch across this side
# "Leave this side alone" -- and it has to be recorded, not merely absent.
# Automatic matching is on by default, so releasing a pin puts the automatic
# match straight back and the side stays green: clicking a matched side looked
# like it did nothing at all. This is what the click actually means.
PIN_EXCLUDED = "-"
PIN_KINDS = (PIN_NEIGHBOUR, PIN_EXCLUDED)


# --- which group each side of the active patch is in -------------------------
#
# A group is what a generator will eventually be handed as one side, so the
# *count* of them per loop is what picks the generator: five sides in four
# groups is a Quad. The number is chosen per side and stored per side.
#
# **Free, and checked rather than constrained.** An earlier version stored the
# demoted *corners* and let a click only merge a side into the one before it or
# split it back out -- always valid by construction, and unpredictable to use:
# nothing on screen said whether the next click would open a new group or join
# an existing one. So the number is now set directly, any number, and a
# grouping that cannot work is **reported** (`group_problems`) instead of being
# made unreachable. That is the same judgement `sides.corners_are_uniform` and
# the crack report already make: say what is wrong, do not guess.


def side_groups(state: "state_mod.RetopPatchState") -> dict[int, int]:
    """The group numbers the user has set, as {flat side index: number}.

    Only the sides actually changed are stored; everything else takes the
    default in `group_numbers`.
    """
    raw = getattr(state, "side_groups", "")
    if not raw:
        return {}
    try:
        stored = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(stored, dict):
        return {}
    numbers = {}
    for key, value in stored.items():
        try:
            numbers[int(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return numbers


def set_side_groups(state: "state_mod.RetopPatchState", numbers: dict[int, int]) -> None:
    state.side_groups = (json.dumps({str(k): v for k, v in sorted(numbers.items())})
                         if numbers else "")


def side_midpoint(points: list) -> object:
    """The point half way along a side, by arc length.

    Where the group bubble is anchored, and it lives here rather than in the
    overlay because the *hit test* has to agree with the drawing to the pixel:
    two implementations of "the middle of this side" would drift apart on a
    curved boundary, and a bubble you cannot click where you see it is worse
    than no bubble. By arc length rather than by index, or a densely
    tessellated end pulls the bubble into it. World space, so it is projected
    once per side -- and vector-agnostic, since `sidematch` imports no Blender
    at runtime.
    """
    if not points:
        return None
    if len(points) == 1:
        return points[0]
    spans = [(b - a).length for a, b in zip(points, points[1:])]
    total = sum(spans)
    if total <= 0.0:
        return points[0]
    walked = 0.0
    for (a, b), span in zip(zip(points, points[1:]), spans):
        if walked + span >= total * 0.5:
            t = 0.0 if span <= 0.0 else (total * 0.5 - walked) / span
            return a + (b - a) * t
        walked += span
    return points[-1]


def group_numbers(
    references: "list[SideReference]", state: "state_mod.RetopPatchState"
) -> dict[int, int]:
    """{flat side index: group number}, for every side of the patch.

    The default is one group per side, numbered from 1 **within each loop**: a
    ring's two rims are separate boundaries, and numbering them 1..7 across
    both would imply an order they have no reason to share.
    """
    stored = side_groups(state)
    numbers: dict[int, int] = {}
    for loop in sorted({reference.loop for reference in references}):
        ring = sorted((r for r in references if r.loop == loop),
                      key=lambda r: r.in_loop)
        for position, reference in enumerate(ring):
            numbers[reference.index] = stored.get(reference.index, position + 1)
    return numbers


def group_count_for(references: "list[SideReference]", loop: int) -> int:
    """How many sides the loop has -- the largest group number worth offering,
    since more groups than sides is not a thing a boundary can be cut into."""
    return sum(1 for reference in references if reference.loop == loop)


def group_runs(
    references: "list[SideReference]", numbers: dict[int, int]
) -> list[list[int]]:
    """The maximal runs of consecutive sides carrying the same number, per loop.

    What the overlay colours and what the side count is read off. A *valid*
    grouping has exactly one run per number; an invalid one has the same number
    in two places and so more runs than groups, which is precisely what
    `group_problems` reports.
    """
    runs: list[list[int]] = []
    for loop in sorted({reference.loop for reference in references}):
        ring = [r.index for r in sorted((r for r in references if r.loop == loop),
                                        key=lambda r: r.in_loop)]
        if not ring:
            continue
        # Start where the number changes, so a run never breaks at the
        # arbitrary point the half-edge walk happened to begin at.
        first = next((position for position, index in enumerate(ring)
                      if numbers.get(index) != numbers.get(ring[position - 1])), None)
        if first is None:
            runs.append(list(ring))  # every side the same number: one run
            continue
        ring = ring[first:] + ring[:first]
        current: list[int] = [ring[0]]
        for index in ring[1:]:
            if numbers.get(index) == numbers.get(current[-1]):
                current.append(index)
            else:
                runs.append(current)
                current = [index]
        runs.append(current)
    return runs


def loop_group_counts(
    references: "list[SideReference]", numbers: dict[int, int]
) -> dict[int, int]:
    """Distinct group numbers per loop -- the side count a generator is offered."""
    counts: dict[int, set[int]] = {}
    for reference in references:
        counts.setdefault(reference.loop, set()).add(numbers.get(reference.index, 0))
    return {loop: len(seen) for loop, seen in counts.items()}


def group_problems(
    references: "list[SideReference]", numbers: dict[int, int]
) -> "tuple[set[int], str]":
    """(the sides at fault, what is wrong) -- empty and "" when the grouping works.

    Two things can be wrong, and nothing else:

    - **A number in two places.** A group becomes one side of a Coons patch, so
      it has to be a contiguous arc of the boundary. `1,2,1` is not a patch.
    - **A loop left with one group.** That is one closed side, which no
      generator accepts (`find_generator` starts at Wedge 2).

    Gaps in the numbering are deliberately *not* a fault: `1,1,3,4` is three
    connected runs and a perfectly good three-sided patch. The numbers are
    labels, and complaining about a gap would be nagging about nothing.

    The sides come back so the viewport can ring the offending bubbles -- which
    is the half that can actually be acted on, since a side has no name on
    screen beyond the bubble sitting on it.
    """
    runs = group_runs(references, numbers)
    loop_of = {reference.index: reference.loop for reference in references}

    seen: dict[tuple[int, int], list[list[int]]] = {}
    for run in runs:
        key = (loop_of.get(run[0], 0), numbers.get(run[0], 0))
        seen.setdefault(key, []).append(run)

    at_fault: set[int] = set()
    messages: list[str] = []
    for (_loop, number), occurrences in sorted(seen.items()):
        if len(occurrences) > 1:
            for run in occurrences:
                at_fault.update(run)
            messages.append(
                f"Group {number} is in {len(occurrences)} separate places. A group becomes "
                "one side of the patch, so it has to be a connected run of the boundary: "
                f"give one of them a number no other side is using.")

    for loop, count in sorted(loop_group_counts(references, numbers).items()):
        if count < 2:
            at_fault.update(r.index for r in references if r.loop == loop)
            messages.append(
                "A boundary needs at least two groups, or it is one closed side and no "
                "generator takes it: split one side off with a number of its own.")

    return at_fault, " ".join(messages)


def side_override_map(state: "state_mod.RetopPatchState") -> dict[int, str]:
    """The manual per-side pins, as {flat index: PIN_*}."""
    if not state.side_overrides:
        return {}
    try:
        stored = json.loads(state.side_overrides)
    except ValueError:
        return {}

    pins = {}
    for key, value in stored.items():
        # Older sessions stored the resolved segment count here; anything
        # numeric means "follow the neighbour", which is all it ever meant.
        # A stored "S" is an older still: the CAD-tessellation pin, which no
        # longer exists. It falls through to None here and the side simply
        # matches, or doesn't, like any other.
        kind = value if value in PIN_KINDS else (
            PIN_NEIGHBOUR if isinstance(value, int) and value >= 1 else None)
        if kind is not None:
            pins[int(key)] = kind
    return pins


def store_side_overrides(
    state: "state_mod.RetopPatchState", overrides: dict[int, str]
) -> None:
    state.side_overrides = json.dumps({str(k): v for k, v in overrides.items()}) if overrides else ""


def span_key_for(generator_name: str, reference: SideReference) -> str:
    """Which span a match on this side drives.

    A grid has one count per *direction*, not per side: pinning a quad's bottom
    side pins its top one too, because they are the same span. Sides that share
    an answer are exactly the sides that collide, which is what the key is for.
    An n-gon has no spans at all -- every side carries its own segment count --
    so each gets a key of its own and nothing ever collides.
    """
    if generator_name in (constants.NGON, constants.NSIDE):
        # Both carry a segment count per side rather than per direction: an
        # n-gon because every side is its own edge run, an N-Side because side
        # `i` is the sum of the two spokes either side of it (see
        # generators/nside.py). Two of an N-Side's *can* still disagree, when
        # they meet at the same spoke -- but that is the allocation's answer to
        # give, not a collision to settle before it is asked.
        return f"side:{reference.index}"
    if generator_name == constants.RING:
        # A ring's two loops both feed "around", but they are not in
        # competition the way a quad's opposite sides are: the generator runs
        # one rung from outer[i] to inner[i], so both rims *can* be reproduced
        # at once -- as long as they agree on the count, which is exactly what
        # `_honours` checks once the span is resolved. Keyed per loop so both
        # get that chance; a shared key let the second rim's committed
        # neighbour be dropped even when it wanted the very same number.
        return f"span_u@{reference.loop}"
    if generator_name == constants.QUAD:
        return "span_u" if reference.in_loop % 2 == 0 else "span_v"
    if generator_name in constants.TWO_SPAN_GENERATORS:
        return "span_u"
    return "span"


def span_base(key: str) -> str:
    """The span a key drives, without the loop it was qualified by.

    Only a ring qualifies its key (`span_u@0`, `span_u@1`); everything else
    hands its own name straight back.
    """
    return key.split("@", 1)[0]


def _match_candidates(
    state: "state_mod.RetopPatchState", references: list[SideReference]
) -> "list[tuple[SideReference, list[mathutils.Vector], bool]]":
    """Every side that wants to be matched, with the points it would take.

    A pin uses the picker's generous margin -- pointing at a side is saying
    which neighbour you mean, so it may reach one that has drifted. Automatic
    matching only ever takes the exact answer, or it would reach for whatever
    happens to be nearby on sides nobody asked about.
    """
    pins = side_override_map(state)
    automatic = state.auto_match_neighbours

    candidates = []
    for reference in references:
        kind = pins.get(reference.index)
        if kind == PIN_EXCLUDED:
            continue  # asked for by hand; automatic matching does not override it
        if kind == PIN_NEIGHBOUR:
            points = reference.match_points
        elif automatic and reference.available:
            points = reference.strict_points
        else:
            continue
        if points and len(points) >= 2:
            candidates.append((reference, points, kind is not None))
    return candidates


def _winning_matches(
    candidates: "list[tuple[SideReference, list[mathutils.Vector], bool]]",
    generator_name: str,
) -> tuple[Winners, list[SideReference]]:
    """One match per span, since a grid cannot honour two counts in one
    direction.

    Two sides driving the same span used to both get substituted and the second
    one silently win the count, which left the loser's points resampled to a
    number that was not theirs -- the exact "the spans agree but the vertices
    don't" crack the matching exists to close. Only the winner is substituted
    now; the rest keep their own polyline and are told about it.

    A pin beats an automatic match, because it was asked for. Between two of a
    kind the denser one wins: it is the one that would lose the most detail.
    """
    by_key: "dict[str, list[tuple[tuple, SideReference, list[mathutils.Vector], bool]]]" = {}
    for reference, _points, _pinned in candidates:
        reference.outvoted = False   # re-decided every generation
        reference.tied_points = None
        reference.tied_key = ""
    for reference, points, pinned in candidates:
        key = span_key_for(generator_name, reference)
        rank = (1 if pinned else 0, len(points), -reference.index)
        by_key.setdefault(key, []).append((rank, reference, points, pinned))

    best = {}
    losers = []
    for key, entries in by_key.items():
        entries.sort(key=lambda entry: entry[0], reverse=True)
        rank, reference, points, pinned = entries[0]
        best[key] = (reference, points, pinned)
        for _rank, other, other_points, _pinned in entries[1:]:
            # Two sides driving one span is only a conflict when they want
            # *different* counts. A quad's opposite sides asking for the same
            # number can both be reproduced -- the grid puts that many segments
            # along the direction and each side lands on its own neighbour's
            # vertices. Outvoting one of them anyway left half a re-cut disc
            # welded and half of it on the CAD tessellation, which is a crack
            # down a boundary that had just been arranged to close.
            if len(other_points) == len(points):
                other.tied_points = other_points
                other.tied_key = key
            else:
                losers.append(other)
    for reference in losers:
        reference.outvoted = True
    return best, losers


def collect_side_matches(
    context: "bpy.types.Context", generator_name: str
) -> tuple[Winners, list[SideReference]]:
    """({span key: (side, points, pinned)}, [sides that lost a collision]).

    Everything the caller needs to decide spans *before* any side is rewritten:
    which sides want to be matched, what they would take, and which of them
    were outvoted because a grid cannot honour two counts in one direction.
    """
    state = context.scene.plasticity_retop
    return _winning_matches(
        _match_candidates(state, active_sides()), generator_name)


def _honours(
    key: str, points: "list[mathutils.Vector]", spans: dict[str, int] | None
) -> bool:
    """Whether the resolved spans still let this match reproduce its points.

    A substituted side only comes back vertex for vertex if the generator asks
    for exactly as many points as it was handed. Once the span driving that
    side says otherwise -- because the user typed one, or because the opposite
    side won the direction -- substituting would hand the generator a polyline
    it is about to resample anyway, moving the very vertices the match existed
    to land on. Better to leave the side as the CAD drew it and let the count
    the user asked for mean what it says.
    """
    if spans is None:
        return True
    if key.startswith("side:"):
        # An n-gon hands down no spans at all -- nothing there can disagree.
        # An N-Side does: its spoke allocation is what decides which sides it
        # can honour, and a side it could not is dropped here like any other.
        return key not in spans or spans[key] == len(points) - 1
    return spans.get(span_base(key)) == len(points) - 1


def apply_side_matches(
    context: "bpy.types.Context",
    obj: "bpy.types.Object",
    prepared: "patchprep.PreparedPatch",
    generator_name: str,
    spans: dict[str, int] | None = None,
    winners: Winners | None = None,
) -> tuple[dict[int, int], list[int]]:
    """Replace each matched side's polyline with the vertices it must reproduce.

    Nothing downstream needs to know: every generator resamples a side with
    `geometry.resample_polyline_by_arclength`, which hands a polyline straight
    back when asked for exactly as many points as it holds. So a matched side is
    reproduced vertex for vertex as long as the generator puts len-1 segments
    along it -- which is what the returned counts are for.

    `spans` is the finally-resolved {span key: count}; a match the spans no
    longer honour is skipped (see `_honours`). Given None, every match is taken,
    which is what a caller that has no spans to resolve wants.

    Returns ({flat side index: segments}, [sides that lost a collision]).
    """
    state = context.scene.plasticity_retop
    if winners is None:
        winners, losers = collect_side_matches(context, generator_name)
    else:
        losers = []

    counts = {}
    for reference in active_sides():
        reference.applied = False   # decided afresh every generation
        reference.applied_points = []
    for key, (reference, points, _pinned) in winners.items():
        if not _honours(key, points, spans):
            # It wanted a match the resolved span can no longer reproduce, so
            # the side keeps the boundary the CAD drew. Say so rather than
            # leaving it looking matched: that is the state the viewport had no
            # way of showing.
            reference.outvoted = True
            continue
        for side_reference, side_points in _with_ties(key, reference, points):
            original = prepared.loops_sides[side_reference.loop][side_reference.in_loop]
            prepared.loops_sides[side_reference.loop][side_reference.in_loop] = side_points
            counts[side_reference.index] = len(side_points) - 1
            side_reference.applied = True
            side_reference.applied_points = side_points
            _blank_moved_corner(state, prepared, side_reference, original, side_points)

    return counts, [reference.index for reference in losers]


def _with_ties(
    key: str, reference: SideReference, points: "list[mathutils.Vector]"
) -> "list[tuple[SideReference, list[mathutils.Vector]]]":
    """The winner of a span, plus any side that tied it on count.

    A tie is not a conflict: both sides get the number of segments the span
    resolved to, so both can be handed their own neighbour's vertices. Each
    keeps its *own* points -- they lie along different boundaries.
    """
    applied = [(reference, points)]
    for other in active_sides():
        if (other is not reference and other.tied_key == key
                and other.tied_points is not None
                and len(other.tied_points) == len(points)):
            applied.append((other, other.tied_points))
    return applied


def _blank_moved_corner(
    state: "state_mod.RetopPatchState",
    prepared: "patchprep.PreparedPatch",
    reference: SideReference,
    original: "list[mathutils.Vector]",
    points: "list[mathutils.Vector]",
) -> None:
    """Drop a corner id the match has moved off its source vertex.

    A corner welds *by identity*, so leaving the name on a point that has moved
    would make a later patch reuse a vertex that is no longer there, or drag
    this one onto it. Blanked, it welds by proximity like every other boundary
    point. Only a cornerless loop can get here: a real corner is a B-rep vertex
    the neighbour shares, so its match starts exactly on it.
    """
    tolerance = mesh_build.side_match_tolerance(state, original)
    if (points[0] - original[0]).length > tolerance:
        corner_ids = prepared.loops_corner_ids[reference.loop]
        if reference.in_loop < len(corner_ids):
            corner_ids[reference.in_loop] = mesh_build.NO_SOURCE


def status_of(
    reference: SideReference, pin_kind: str | None = None
) -> tuple[str, str]:
    """(what this side is doing, why) -- one short line each.

    Written once here because the viewport tooltip and the panel have to say
    the same thing, and it is the same thing the colour says. Three states, in
    the order they matter:

    **Matched** (green), **unmatched against a committed neighbour** (red) and
    **nothing to match** (grey). Only the middle one is a problem -- a side
    bordering finished retopology that is not reproducing it leaves a real
    crack, whether it lost a span collision, had its span typed over, or was
    released by hand. A side with nothing across it yet is the normal state of
    a model being retopologized and is never reported as a failure.
    """
    who = (f"patch {reference.neighbour}" if reference.neighbour is not None
           else "the committed neighbour")
    pinned = " (pinned)" if pin_kind and pin_kind != PIN_EXCLUDED else ""

    if reference.applied:
        return ("Matched",
                f"reproduces {who}'s vertices{pinned} — click to release")
    if reference.available:
        # Red in the viewport: there is retopology across this side and this
        # patch is not welding to it.
        crack = "Not matched — this edge will crack"
        if pin_kind == PIN_EXCLUDED:
            return (crack, "released by hand — click to match it again")
        if reference.outvoted:
            return (crack,
                    "another side drives the same span, or the span was typed by hand")
        return (crack, f"click to match it to {who}")
    if pin_kind == PIN_EXCLUDED:
        return ("Nothing to match along this edge",
                "released by hand, and nothing is committed across it either")
    return ("Nothing to match along this edge",
            reference.reason or "no committed neighbour here yet")


def applied_loops() -> set[int]:
    """Boundary loops whose points a match has replaced this generation.

    A ring has to know: a loop carrying a committed neighbour's own vertices
    must be reproduced exactly, so it may not be phase-aligned or resampled --
    doing that is what threw the match away and left the two rims half a step
    apart. See `generators.ring.generate`.
    """
    return {reference.loop for reference in active_sides() if reference.applied}


def applied_side_counts() -> dict[int, dict[int, int]]:
    """{loop: {side within that loop: segments}} for every side a match
    replaced this generation.

    `applied_loops` says *that* a rim carries a neighbour's vertices, which is
    all a rim of one cornerless side needs. A rim cut into several -- by
    isoparms, or by a corner the angle test found -- needs to know *which* of
    them: only the matched sides hold the neighbour's points, and re-allocating
    the loop's total by length hands them a count that is not theirs, which is
    the match thrown away. See `generators.ring.allocate_segments`.
    """
    counts: dict[int, dict[int, int]] = {}
    for reference in active_sides():
        if not reference.applied:
            continue
        counts.setdefault(reference.loop, {})[reference.in_loop] = max(
            1, len(reference.applied_points) - 1)
    return counts


def ngon_side_segments(
    prepared: "patchprep.PreparedPatch", matched_counts: dict[int, int]
) -> list[dict[int, int]]:
    """The matched counts, regrouped per loop the way the n-gon wants them."""
    per_loop = []
    index = 0
    for loop_i in range(len(prepared.loops_sides)):
        forced = {}
        for side_i in range(len(prepared.loops_sides[loop_i])):
            if index in matched_counts:
                forced[side_i] = matched_counts[index]
            index += 1
        per_loop.append(forced)
    return per_loop
