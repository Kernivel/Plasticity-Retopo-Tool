"""Which vertices a patch's side has to reproduce, and where they come from.

A matched side takes the neighbour's own committed vertices, not just their
count. Every generator reproduces them unchanged, since
`geometry.resample_polyline_by_arclength` returns a polyline untouched when the
count already matches.

- A side only matches the faces it actually borders (`_match_pool`).
- Automatic matching takes only an exact answer. A pinned side may reach
  further, by `match_margin`.
- A grid has one count per direction: a match the resolved span cannot
  reproduce is dropped (`_honours`).

A leaf module: the overlay reads it and must never reach `operators`.
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

    `span` is the neighbour's segment count along this side, or None when there
    is nothing to match.
    """

    __slots__ = ("index", "loop", "in_loop", "group", "grouped", "points", "match_points",
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
        # Points this side takes although another side won its span, because
        # both want the same count (`_winning_matches`). `tied_key` is that span.
        self.tied_points = None
        self.tied_key = ""
        # The same points in world space, for the overlay. Computed here, never
        # at draw time.
        self.match_world = match_world or []
        # Why this side cannot be matched, or "".
        self.reason = reason
        # What automatic matching may take: found without the margin.
        # `match_points` is the generous answer, for a pinned side.
        self.strict_points = strict_points
        # This side's 0-based group within its loop. Set by
        # `build_side_references`.
        self.group = in_loop
        # Whether that group holds several sides. Such a side keys its span
        # per side (`span_key_for`).
        self.grouped = False
        self.index = index      # flat index across every loop, in order
        self.loop = loop        # which boundary loop it belongs to
        self.in_loop = in_loop  # its index within that loop
        self.points = points    # world space, for drawing and hit-testing
        # The neighbour's committed vertices along this side, in the source's
        # local space, or None.
        self.match_points = match_points
        # Every Plasticity face across this side, most-covering first
        # (`patchprep.side_neighbours`). The match is confined to these.
        self.neighbours = list(neighbours or [])
        # Whether this side's polyline was replaced this generation, and by
        # which points. Set by `apply_side_matches`. Not the same as `available`.
        self.applied = False
        self.applied_points: "list[mathutils.Vector]" = []
        # It wanted a match and lost: a span collision or a typed span.
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


# Rebuilt on every generation, empty after a reload. Read by the overlay and
# the modal.
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

    Walks every loop, holes included. Each side is matched only against the
    patches it borders.
    """
    global _active_sides

    state = context.scene.plasticity_retop
    matrix = obj.matrix_world
    # The patch being generated, which may not be the active one yet: it must
    # never match its own committed geometry.
    if face_id is None:
        face_id = state.active_face_id
    # Once for the whole patch: it walks the result mesh.
    committed = mesh_build.committed_boundary_map(obj)

    # One reach for the whole patch, from its longest side.
    reference_length = max(
        (sum((b - a).length for a, b in zip(side, side[1:]))
         for loop_sides in prepared.loops_sides for side in loop_sides),
        default=0.0)

    # Move arbitrary corners onto a neighbour's vertices first
    # (`_recut_arbitrary_loop`).
    if state.auto_match_neighbours:
        for loop_i, arbitrary in enumerate(prepared.loops_corners_arbitrary):
            if arbitrary:
                _recut_arbitrary_loop(state, prepared, loop_i, committed,
                                      face_id, reference_length)

    # Every side, so each match is confined to the side it is nearest (`rivals`).
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

            # Two reaches, but always dedupe at the strict tolerance: deduping
            # at the generous one merges consecutive neighbour vertices.
            strict = mesh_build.side_match_tolerance(
                state, side, reference_length=reference_length)
            rivals = [other for other in all_sides if other is not side]
            # `partial` on the picker's answer only: completing a partly covered
            # side is a decision, never taken automatically.
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

    # Which group each side lands in, for `span_key_for`.
    assign_groups(references, group_numbers(references, state))

    _active_sides = references
    return references


def assign_groups(references: list[SideReference], numbers: dict[int, int]) -> None:
    """Fill in `SideReference.group` -- the 0-based group index within the loop."""
    position_in_loop: dict[int, int] = {}
    for run in group_runs(references, numbers):
        by_index = {reference.index: reference for reference in references}
        loop = by_index[run[0]].loop
        position = position_in_loop.get(loop, 0)
        position_in_loop[loop] = position + 1
        for index in run:
            by_index[index].group = position
            by_index[index].grouped = len(run) > 1


def _recut_arbitrary_loop(
    state: "state_mod.RetopPatchState",
    prepared: "patchprep.PreparedPatch",
    loop_i: int,
    committed: CommittedMap,
    face_id: int | None,
    reference_length: float,
) -> bool:
    """Cut a cornerless loop where a committed neighbour put its vertices.

    For a loop whose corners are arbitrary (the quarter points of a circle).
    The whole loop is matched once as a closed side, then its sides are carved
    out of the neighbour's ring of points, so every corner lands on a neighbour
    vertex.

    The side count is kept. Corner ids are blanked to `NO_SOURCE`.
    A shape corner is never moved.
    See "A corner nothing agrees on" in CLAUDE.md.

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

    # The strict answer only: this runs unasked, on every hover.
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

    # Anchored on the point nearest the existing first corner, so the cut is
    # stable across hovers.
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

    # Each new side borders the faces of the old side it runs along.
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

    As evenly as possible, with opposite sides equal where the arithmetic
    allows: a grid has one span per direction, so opposite sides must agree to
    both be matched.
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
    """Why a side had nothing to match against, naming the patch it waits for.
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

    Only the Plasticity faces across this side, plus untracked retopology
    (`NO_PATCH`, which belongs to no face). Empty when none of those faces is
    committed: never fall back to proximity over the whole mesh.
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


# What a manual pin on a side means. The kind is stored, never the count.
PIN_NEIGHBOUR = "N"  # follow the committed patch across this side
# "Leave this side alone". Must be recorded, or automatic matching would put
# the match straight back.
PIN_EXCLUDED = "-"
PIN_KINDS = (PIN_NEIGHBOUR, PIN_EXCLUDED)


# --- which group each side of the active patch is in -------------------------
#
# A group is handed to the generator as one side, so the group count picks the
# generator: five sides in four groups is a Quad.
# Any number can be set. A grouping that cannot work is reported
# (`group_problems`), never prevented.


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


class SideSlot:
    """Just enough of a side for the grouping: where it is, and which loop in.

    The grouping is needed before the `SideReference`s exist, since it picks
    the generator.
    """

    __slots__ = ("index", "loop", "in_loop")

    def __init__(self, index: int, loop: int, in_loop: int) -> None:
        self.index = index
        self.loop = loop
        self.in_loop = in_loop


def side_slots(prepared: "patchprep.PreparedPatch") -> list[SideSlot]:
    """One slot per side of `prepared`, flat-indexed exactly as
    `build_side_references` does."""
    slots = []
    index = 0
    for loop_i, loop_sides in enumerate(prepared.loops_sides):
        for side_i in range(len(loop_sides)):
            slots.append(SideSlot(index, loop_i, side_i))
            index += 1
    return slots


def side_midpoint(points: list) -> object:
    """The point half way along a side, by arc length.

    Anchors the group bubble. Shared by the drawing and the hit test, so both
    agree.
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

    The default is one group per side, numbered from 1 within each loop.
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
    """How many sides the loop has: the largest group number offered."""
    return sum(1 for reference in references if reference.loop == loop)


def group_runs(
    references: "list[SideReference]", numbers: dict[int, int]
) -> list[list[int]]:
    """The maximal runs of consecutive sides carrying the same number, per loop.

    A valid grouping has exactly one run per number.
    """
    runs: list[list[int]] = []
    for loop in sorted({reference.loop for reference in references}):
        ring = [r.index for r in sorted((r for r in references if r.loop == loop),
                                        key=lambda r: r.in_loop)]
        if not ring:
            continue
        # Start where the number changes, so no run breaks at the loop's start.
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

    - A number in two places: a group must be a contiguous run.
    - A loop left with one group: no generator takes one side.

    A gap in the numbering is not a fault. The sides come back so the viewport
    can ring their bubbles.
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
        # Older files: a number means PIN_NEIGHBOUR; "S" (a removed pin kind)
        # is ignored.
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

    Sides with the same key collide. A grid has one count per direction; an
    n-gon or N-Side has one per side; a ring one per loop.
    """
    # The group, not the raw side: the generator counts groups.
    position = getattr(reference, "group", None)
    if position is None:
        position = reference.in_loop
    if getattr(reference, "grouped", False):
        # Inside a merged group: its count is its share of the group's span
        # (`patchprep.allocate_group_segments`), so it is keyed per side.
        return f"side:{reference.index}"
    if generator_name in (constants.NGON, constants.NSIDE):
        # A count per side. For an N-Side the spoke allocation settles any
        # disagreement.
        return f"side:{position}"
    if generator_name == constants.RING:
        # Keyed per loop: both rims can be matched if they agree on the count
        # (`_honours` checks it).
        return f"span_u@{reference.loop}"
    if generator_name == constants.QUAD:
        return "span_u" if position % 2 == 0 else "span_v"
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

    A pin uses the generous margin. Automatic matching takes only the exact
    answer.
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

    A pin beats an automatic match, then the denser one wins. Only the winner
    is substituted; the losers keep their own polyline and are flagged.
    A side wanting the same count as the winner is a tie, not a loser.
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
            # Only a different count is a conflict. The same count is a tie.
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

    Called before any side is rewritten, so the spans can be decided first.
    """
    state = context.scene.plasticity_retop
    return _winning_matches(
        _match_candidates(state, active_sides()), generator_name)


def _honours(
    key: str, points: "list[mathutils.Vector]", spans: dict[str, int] | None
) -> bool:
    """Whether the resolved spans still let this match reproduce its points.

    Only if the span equals the match's segment count. Otherwise the side keeps
    its CAD boundary, and a typed span means what it says.
    """
    if spans is None:
        return True
    if key.startswith("side:"):
        # No span for this side (an n-gon): nothing can disagree.
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

    A generator reproduces a matched side exactly as long as it puts len-1
    segments along it: that is what the returned counts are for.

    `spans` is the resolved {span key: count}. A match it no longer honours is
    skipped (`_honours`). With None, every match is taken.

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
            # The resolved span cannot reproduce it: the side keeps its CAD
            # boundary and is flagged.
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

    Each tied side keeps its own points.
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

    A corner welds by identity, so a moved one must lose its id.
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

    Shared by the viewport tooltip and the panel.
    Matched (green), unmatched against a committed neighbour (red, a crack),
    nothing to match (grey, normal).
    """
    who = (f"patch {reference.neighbour}" if reference.neighbour is not None
           else "the committed neighbour")
    pinned = " (pinned)" if pin_kind and pin_kind != PIN_EXCLUDED else ""

    if reference.applied:
        return ("Matched",
                f"reproduces {who}'s vertices{pinned} — click to release")
    if reference.available:
        # Red in the viewport.
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

    A ring leads with such a loop and never resamples it
    (`generators.ring.generate`).
    """
    return {reference.loop for reference in active_sides() if reference.applied}


def applied_side_counts() -> dict[int, dict[int, int]]:
    """{loop: {side within that loop: segments}} for every side a match
    replaced this generation.

    For a rim cut into several sides: only the matched ones are pinned
    (`generators.ring.allocate_segments`).
    """
    counts: dict[int, dict[int, int]] = {}
    for reference in active_sides():
        if not reference.applied:
            continue
        counts.setdefault(reference.loop, {})[reference.in_loop] = max(
            1, len(reference.applied_points) - 1)
    return counts


def ngon_group_key(run: "list[int]") -> str:
    """The key an n-gon group's vertex count is stored under: its sides.

    The sides, never the group number, so a regrouping stops a count applying.
    """
    return ",".join(str(index) for index in sorted(run))


def ngon_group_counts(state: "state_mod.RetopPatchState") -> dict[str, int]:
    """{group key: segments} the user set with Ctrl+wheel in n-gon mode."""
    raw = getattr(state, "ngon_group_counts", "")
    if not raw:
        return {}
    try:
        stored = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(stored, dict):
        return {}
    counts = {}
    for key, value in stored.items():
        try:
            counts[str(key)] = max(1, int(value))
        except (TypeError, ValueError):
            continue
    return counts


def set_ngon_group_counts(
    state: "state_mod.RetopPatchState", counts: dict[str, int]
) -> None:
    state.ngon_group_counts = (json.dumps(dict(sorted(counts.items())))
                               if counts else "")


def ngon_runs(
    references: "list[SideReference] | list[SideSlot]",
    state: "state_mod.RetopPatchState",
) -> "list[list[int]]":
    """The side runs an n-gon's vertex counts apply to.

    The user's grouping when it is valid, else one run per side.
    """
    numbers = group_numbers(references, state)
    at_fault, _message = group_problems(references, numbers)
    if at_fault:
        return [[reference.index] for reference in references]
    return group_runs(references, numbers)


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
