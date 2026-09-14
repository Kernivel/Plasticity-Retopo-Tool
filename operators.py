import json
import sys

import bpy
import mathutils
from bpy_extras import view3d_utils

from . import cad_display
from . import constants
from . import patch_data
from . import geometry
from . import generators
from . import keymap
from . import mesh_build
from . import overlay
from . import patchprep
from . import sidematch
from . import state as state_mod
from . import tweak

# Whether a session modal is actually listening. Session *state* lives in the
# scene and outlives a reload or a crashed modal, so the two can disagree --
# which is what `session_is_running` exists to report (see the panel's reset).
_SESSION_RUNNING: bool = False

# Set by the undo/redo handler, consumed by the session modal on its next
# event. The handler may not touch a datablock -- Blender has just swapped the
# whole file state out from under it -- but the preview mesh it leaves behind
# is geometry for a patch that is no longer open, so somebody has to empty it.
# The modal is the one place that runs after the handler, owns the preview and
# is allowed to write to it.
_undo_needs_reconcile: bool = False


def resolve_session_object(
    obj: bpy.types.Object | None,
) -> bpy.types.Object | None:
    """The object a session should actually run on.

    Selecting `<Something>_Retop` and starting a session is asking to carry on
    retopologizing `Something` -- the result mesh has no patch data of its own
    and never will, so taking it literally can only fail. Everything else is
    returned untouched.
    """
    if obj is None:
        return None
    source = mesh_build.source_object_for_result(obj)
    return source if source is not None else obj


def _is_plasticity_mesh(obj: bpy.types.Object | None) -> bool:
    return bool(obj is not None and obj.type == 'MESH' and obj.data.get("face_ids"))


def _propagated_defaults(
    obj: bpy.types.Object,
    generator: generators.base.Generator,
    corner_source_ids: list[int],
    defaults: dict[str, int],
) -> tuple[dict[str, int], list[str]]:
    """Override `defaults` (span_u/span_v or span) with spans already used by
    committed neighboring patches sharing a side (see mesh_build's span
    registry), so adjacent patches naturally weld along their whole shared
    edge instead of only at corners. Returns (defaults, locked_keys) where
    locked_keys names which entries were overridden by propagation.
    """
    n = len(corner_source_ids)

    def side_span(i: int) -> int | None:
        a = corner_source_ids[i]
        b = corner_source_ids[(i + 1) % n]
        return mesh_build.lookup_propagated_span(obj, a, b)

    locked = []
    if generator.name == constants.QUAD:
        span_u = side_span(0)
        if span_u is None:
            span_u = side_span(2)
        span_v = side_span(1)
        if span_v is None:
            span_v = side_span(3)
        if span_u is not None:
            defaults["span_u"] = span_u
            locked.append("span_u")
        if span_v is not None:
            defaults["span_v"] = span_v
            locked.append("span_v")
    elif generator.name == constants.WEDGE:
        # both sides are linked, so either one determines the span
        span_u = side_span(0)
        if span_u is None:
            span_u = side_span(1)
        if span_u is not None:
            defaults["span_u"] = span_u
            locked.append("span_u")
    else:
        # Triangle / N-Side: one span shared by every side, so the first
        # already-committed neighbour edge decides it.
        for i in range(n):
            span = side_span(i)
            if span is not None:
                defaults["span"] = span
                locked.append("span")
                break

    return defaults, locked


def register_spans_for(
    context: bpy.types.Context,
    source_obj: bpy.types.Object,
    prepared: patchprep.PreparedPatch,
) -> None:
    """Record the span used along each side of a just-committed patch, so
    neighbouring patches pick it up (mesh_build's span registry).

    A ring registers each of its two loops separately: pairing corners
    cyclically across the whole flat list would invent a side running from the
    outer boundary to the hole. Its per-side counts come from the generator's
    own allocation, since "around" is one number spread over the sides.
    """
    state = context.scene.plasticity_retop
    if state.generator_name == generators.NGON.name:
        # An n-gon has no span, but it does have a segment count per side, and
        # that is what a neighbouring grid patch has to match to weld onto it.
        # Recomputed rather than carried over: same inputs, same result, and it
        # keeps the commit path from having to thread the allocation through.
        #
        # Per loop, like a ring: pairing corners cyclically across a flat list
        # of both loops' ids would invent a side running from the outer
        # boundary into the hole.
        # Same substitution the preview was built with, or the registry would
        # advertise a curvature count on a side that was matched to a neighbour.
        forced = sidematch.ngon_side_segments(
            prepared,
            sidematch.apply_side_matches(context, source_obj, prepared, generators.NGON.name)[0])
        for loop_i, (corner_ids, loop_sides) in enumerate(
                zip(prepared.loops_corner_ids, prepared.loops_sides)):
            if corner_ids:
                mesh_build.register_patch_spans(
                    source_obj, corner_ids,
                    generators.ngon.loop_allocation(
                        loop_sides, state.ngon_angle,
                        forced[loop_i] if loop_i < len(forced) else None))
        return

    # The same grouping generation used, or the registry describes a patch that
    # was not built: a pentagon committed as a Quad has four spans and five
    # corners, and pairing the five against the four is what raised IndexError
    # out of `register_patch_spans`. Read through the one helper, so the two can
    # only ever agree. Neither the n-gon nor the ring path above reaches here --
    # both are grouping-free, and both have already returned.
    groups = _side_groups_for(state, prepared, ngon=False)
    corner_ids = prepared.corner_source_ids
    if groups:
        # One entry per group, naming the corner it starts at. The corner
        # *inside* a group is deliberately not registered: what these pairs
        # describe is a shared boundary, and a group's boundary is the whole
        # merged run -- a neighbour across only part of it has no pair to look
        # up, which is the same propagation this gives up as the matching does.
        corner_ids = [prepared.loops_corner_ids[0][run[0]] for run in groups]

    if state.generator_name == constants.NSIDE:
        # Per side, from the same solve generation ran: the registry has to
        # advertise what the mesh actually got, and an N-Side's sides no longer
        # share one number. The winners are re-collected rather than carried
        # over, off the side references the preview left behind -- same patch,
        # same pins, same answer.
        winners, _outvoted = sidematch.collect_side_matches(context, constants.NSIDE)
        spokes, _refused = nside_allocation(len(corner_ids) or len(prepared.sides),
                                            state.span, winners)
        if corner_ids:
            mesh_build.register_patch_spans(
                source_obj, corner_ids, generators.nside.side_segments(spokes))
        return

    if prepared.is_ring:
        around = generators.ring.around_count(prepared.loops_sides, state.span_u)
        for corner_ids, loop_sides in zip(prepared.loops_corner_ids, prepared.loops_sides):
            lengths = [generators.ring.polyline_length(side) for side in loop_sides]
            alloc = generators.ring.allocate_segments(lengths, around)
            mesh_build.register_patch_spans(source_obj, corner_ids, alloc)
        return

    if corner_ids:
        mesh_build.register_patch_spans(
            source_obj, corner_ids, spans_per_side(state, len(corner_ids)))


def nside_allocation(
    num_sides: int,
    span: int,
    winners: "sidematch.Winners",
) -> tuple[list[int], list[int]]:
    """(spoke counts, side indices whose match cannot be honoured) for N-Side.

    An N-Side patch's sides are no longer one shared span: side `i` spans the
    two spokes either side of it, so several sides can reproduce several
    committed neighbours at once. What they cannot always do is *all* of them --
    two sides meeting at one spoke can want counts that disagree -- so the
    wanted counts are applied in the same order `_winning_matches` ranks them
    (a pin first, then the denser match) and whatever is left over is refused
    rather than approximated.

    Shared by generation and the commit path deliberately: the registry has to
    advertise the counts the mesh actually got, and the only way to be sure of
    that is for both to run the same solve on the same inputs.
    """
    ranked = []
    for key, (reference, points, pinned) in winners.items():
        if not key.startswith("side:"):
            continue
        ranked.append((1 if pinned else 0, len(points), reference.index,
                       len(points) - 1))
    ranked.sort(reverse=True)

    wanted = {}
    order = []
    for _pinned, _length, index, count in ranked:
        if 0 <= index < num_sides:
            wanted[index] = count
            order.append(index)

    default_half = max(1, generators.nside.even_span(span) // 2)
    return generators.nside.spoke_allocation(num_sides, default_half, wanted, order)


def spans_per_side(state: state_mod.RetopPatchState, num_sides: int) -> list[int]:
    """Span used along each side of the active patch, in boundary order --
    what gets recorded for propagation to neighbouring patches.
    """
    if state.generator_name == constants.QUAD:
        return [state.span_u, state.span_v, state.span_u, state.span_v]
    if state.generator_name == constants.WEDGE:
        return [state.span_u] * num_sides
    return [state.span] * num_sides


class PatchPreview:
    """What _generate_for_face produced, so callers don't juggle a 6-tuple."""

    __slots__ = ("generator", "num_sides", "num_loops", "spans", "corner_source_ids",
                 "propagated", "committed", "ngon")

    def __init__(
        self,
        generator: generators.base.Generator,
        num_sides: int,
        num_loops: int,
        spans: tuple[int, int, int],
        corner_source_ids: list[int],
        propagated: list[str],
        committed: bool,
        ngon: bool = False,
    ) -> None:
        self.ngon = ngon  # generated as a single n-gon rather than a span grid
        self.generator = generator
        self.num_sides = num_sides
        self.num_loops = num_loops  # boundary loops the patch has (2 = ring, >2 = unsupported)
        self.spans = spans  # (span_u, span_v, span)
        self.corner_source_ids = corner_source_ids
        self.propagated = propagated  # span keys taken from a committed neighbour
        self.committed = committed  # this patch is already in the result mesh (re-edit)
def adopt_side_reference(
    context: bpy.types.Context, flat_index: int, kind: str | None = None
) -> sidematch.SideReference | None:
    """Pin side `flat_index` to the vertices it should reproduce.

    One path for every generator: the pin is recorded, and regeneration
    substitutes those points into that side and sets whatever count reproduces
    them. Which span that turns out to be is the generator's business (see
    `span_key_for`).

    There is one thing a side can be pinned to: `sidematch.PIN_NEIGHBOUR`, the
    committed patch across it, which is what welds two patches together. A side
    with nothing committed across it cannot be pinned at all -- matching is
    binary, and there is no third state where a side follows something that is
    not a neighbour.

    **A click on a side that is already being matched turns the match off**
    (`sidematch.PIN_EXCLUDED`), rather than merely releasing the pin. Releasing
    is what this used to do, and with automatic matching on -- which is the
    default -- the automatic match put itself straight back on the next
    regeneration: the side stayed green and the click read as broken. Clicking
    an excluded side matches it again, so the gesture is a plain two-state
    toggle: matched or not.

    Returns the SideReference that was pinned, or None if it can't be.
    """
    state = context.scene.plasticity_retop
    references = sidematch.active_sides()
    if not 0 <= flat_index < len(references):
        return None
    reference = references[flat_index]

    if kind is None:
        kind = sidematch.PIN_NEIGHBOUR
    if kind == sidematch.PIN_NEIGHBOUR and not reference.available:
        return None

    overrides = sidematch.side_override_map(state)
    current = overrides.get(flat_index)
    if current == sidematch.PIN_EXCLUDED:
        overrides[flat_index] = kind          # released before: match it again
    elif current == kind or (current is None and reference.applied
                             and kind == sidematch.PIN_NEIGHBOUR):
        # Clicking what is already being matched, however it came to be matched.
        overrides[flat_index] = sidematch.PIN_EXCLUDED
    else:
        overrides[flat_index] = kind
    sidematch.store_side_overrides(state, overrides)
    regenerate_active_preview(context)
    return reference


def _ngon_wanted(
    state: state_mod.RetopPatchState,
    obj: bpy.types.Object,
    face_id: int,
    committed: bool,
) -> bool:
    """Whether the *mode* asks for an n-gon, before checking the patch can take
    one (see ngon_blocker).

    A patch already in the result mesh comes back the way it was committed --
    same rule as its spans, and for the same reason: hovering a finished patch
    must show what is actually there, not what the current mode would build.
    """
    if committed:
        stored = mesh_build.lookup_patch_settings(obj, face_id)
        if stored and stored.get("generator"):
            return stored.get("generator") == generators.NGON.name
    return state.ngon_mode


def ngon_blocker(
    state: state_mod.RetopPatchState,
    mesh: bpy.types.Mesh,
    face_id: int,
    num_loops: int | None = None,
) -> str:
    """Why this patch can't be an n-gon, or "" when it can.

    Two reasons, both hard: a curved face would get a flat lid over it, and
    more than one hole can't be bridged by the two-edge cut generate_holed
    makes (the pipeline only ever hands generators the outer loop past two).
    """
    if not patchprep.patch_is_planar(mesh, face_id, state.ngon_planar_tolerance):
        return "not a flat face"
    if num_loops is not None and num_loops > 2:
        return f"{num_loops} boundary loops"
    return ""


def _joined(subsides: "list[list[mathutils.Vector]]") -> "list[mathutils.Vector]":
    """Several consecutive sides as one polyline, the shared endpoints dropped.

    The end of one side and the start of the next are the same vertex; kept
    twice, every generator would see a zero-length segment there and
    `resample_polyline_by_arclength` would divide by it.
    """
    points: "list[mathutils.Vector]" = []
    for sub in subsides:
        points.extend(sub[:-1])
    points.append(subsides[-1][-1])
    return points


def _span_for_group(spans: dict[str, int], generator_name: str, position: int) -> int:
    """How many segments group `position` carries, read off the resolved spans
    through the same key the matching uses -- two ideas of which span drives a
    side is how a grid comes out with its two directions swapped."""
    key = sidematch.span_key_for(
        generator_name, sidematch.SideSlot(position, 0, position))
    return max(1, spans.get(key, spans.get(sidematch.span_base(key), 1)))


def _side_groups_for(
    state: "state_mod.RetopPatchState",
    prepared: "patchprep.PreparedPatch",
    ngon: bool,
) -> "list[list[int]] | None":
    """The user's grouping as runs of side indices, or None to use the sides.

    None whenever the grouping would change nothing or cannot be built: an
    n-gon or a ring (neither reaches `find_generator` by side count), a
    numbering that leaves every side on its own, and a numbering
    `sidematch.group_problems` has something to say about -- an unusable
    grouping is *kept* and reported rather than refused, so the generator has
    to go on building the patch the old way in the meantime.
    """
    if ngon or prepared.is_ring:
        return None
    slots = sidematch.side_slots(prepared)
    if not slots:
        return None
    numbers = sidematch.group_numbers(slots, state)
    at_fault, _message = sidematch.group_problems(slots, numbers)
    if at_fault:
        return None
    runs = sidematch.group_runs(slots, numbers)
    if len(runs) >= len(slots):
        return None  # nothing merged: the sides are already the groups
    return runs


def _group_pins(
    winners: "sidematch.Winners", groups: "list[list[int]]"
) -> "list[dict[int, int]]":
    """Per group, {position within it: segments} for its matched sub-sides.

    A match hands a side a committed neighbour's own vertices, so its count is
    not negotiable -- resampling it off them is the crack matching exists to
    close. Inside a merged group that count is only a *share* of the group's
    span, which is why those sides key their span per side (`span_key_for`)
    and end up here as pins on the allocation rather than as a vote on the
    direction.
    """
    wanted = {reference.index: len(points) - 1
              for _key, (reference, points, _pinned) in winners.items()}
    pins = []
    for run in groups:
        pins.append({position: wanted[index]
                     for position, index in enumerate(run) if index in wanted})
    return pins


def _generate_for_face(
    context: bpy.types.Context,
    obj: bpy.types.Object,
    face_id: int,
    span_overrides: dict[str, int] | None = None,
) -> "PatchPreview | None":
    """Shared core: prepare a patch, pick a generator, generate a result and
    push it into the preview object. Returns a PatchPreview on success, or None
    on failure (nothing reported here -- callers report, since CANCELLED vs.
    silently-ignored-during-hover differ).
    """
    state = context.scene.plasticity_retop
    mesh = obj.data

    # Committed state is needed before anything else: an n-gon patch has to come
    # back as an n-gon, and the corner method depends on which mode will run.
    committed = mesh_build.is_patch_committed(obj, face_id)
    wants_ngon = _ngon_wanted(state, obj, face_id, committed)
    blocker = ngon_blocker(state, mesh, face_id) if wants_ngon else ""
    ngon = wants_ngon and not blocker

    def prepare(for_ngon: bool) -> patchprep.PreparedPatch | None:
        return patchprep.prepare_patch(
            mesh, face_id, state.corner_angle_threshold,
            # Like every other distance in the panel: typed in state.length_unit.
            state_mod.to_blender_units(state, state.small_side_tolerance),
            state.corner_method_ngon if for_ngon else state.corner_method_spans)

    prepared = prepare(ngon)
    if prepared is None:
        return None

    if ngon:
        # The loop count only comes out of the preparation, so this second gate
        # can't be merged into the first one.
        blocker = ngon_blocker(state, mesh, face_id, prepared.num_loops)
        if blocker:
            ngon = False
            prepared = prepare(False)  # corners resolved for the wrong mode
            if prepared is None:
                return None

    # Two boundary loops is not the same thing as a band. A flat plate with a
    # small hole is an annulus too, and the Ring generator has to give both of
    # its loops the same point count -- so the hole ends up absurdly dense, the
    # outline absurdly coarse, and every quad stretched across the plate. The
    # n-gon fill (outer boundary plus hole, bridged) is the right topology for
    # a flat one, and it is already what pressing N would produce, so a
    # non-band takes it rather than being quietly ruined by a band.
    ring_note = ""
    if not ngon and prepared.is_ring and not generators.ring.is_band(prepared.loops_sides):
        if committed:
            pass  # a committed patch comes back as whatever it was built as
        elif blocker:
            ring_note = f"hole too small for a band, and {blocker}"
        else:
            ngon = True
            ring_note = "hole too small for a band -- filled as an n-gon"
            prepared = prepare(True)
            if prepared is None:
                return None

    state.ngon_available = not blocker
    state.ngon_unavailable_reason = blocker
    state.corner_warning = prepared.corner_warning
    state.generator_note = ring_note

    corner_source_ids = prepared.corner_source_ids

    # Several of the patch's sides gathered into one, if the user has said so.
    # This is what turns a five-sided face into the quad it usually wants to
    # be, and it has to be settled here because the group *count* is what picks
    # the generator.
    #
    # Single-loop span patches only, which is the case it exists for. A ring is
    # chosen by having two loops and pairs them itself, so a group count would
    # not reach `find_generator` at all; an n-gon follows its boundary whatever
    # the sides are called, so grouping it changes nothing. Neither is refused
    # -- the numbering is simply not read for them.
    groups = _side_groups_for(state, prepared, ngon)
    if groups:
        corner_source_ids = [prepared.loops_corner_ids[0][run[0]] for run in groups]

    # Which generator runs is settled before anything is substituted, and can
    # be: substitution swaps a side's *points*, never how many sides there are.
    # It has to be, because a grid has one span per direction, so resolving two
    # sides that want different counts needs to know which sides share one.
    if ngon:
        generator = generators.NGON
    elif prepared.is_ring:
        generator = generators.RING
    else:
        generator = generators.find_generator(len(groups) if groups
                                              else len(prepared.sides))
        if generator is None:
            return None

    sidematch.build_side_references(context, obj, prepared, face_id)
    # Collected here, applied further down: a match both *drives* a span and
    # depends on it, so the spans have to be settled before any side is
    # rewritten. The references have to exist first -- they are what holds the
    # neighbour's vertices.
    winners, outvoted = sidematch.collect_side_matches(context, generator.name)
    state.match_conflicts = len(outvoted)

    if ngon:
        # An n-gon carries a segment count per side, so no two matches can
        # disagree and there is nothing to resolve first.
        matched, _ = sidematch.apply_side_matches(context, obj, prepared, generator.name,
                                        winners=winners)
        settings = {"ngon_angle": state.ngon_angle,
                    "side_segments": sidematch.ngon_side_segments(prepared, matched)}
        if prepared.is_ring:
            # One hole: bridged to the outer boundary with two edges, giving
            # two n-gons (a Blender n-gon can't carry a hole on its own).
            result = generator.generate_holed(prepared.loops_sides, settings)
        else:
            settings = dict(settings, side_segments=settings["side_segments"][0])
            result = generator.generate(prepared.sides, settings)
        num_sides = len(corner_source_ids)
        mesh_build.update_preview_object(context, obj, result, corner_source_ids)
        return PatchPreview(generator, num_sides, prepared.num_loops,
                            (state.span_u, state.span_v, state.span),
                            corner_source_ids, [], committed, ngon=True)

    if prepared.is_ring:
        # Two boundary loops: fill the band between them instead of trying to
        # treat one of the loops as if it were the whole patch boundary.
        generation_input = prepared.loops_sides
        num_sides = len(corner_source_ids)
        defaults = state_mod.scale_default_spans(
            state, generator.default_spans(generation_input))
        # Propagation is per side; a ring's "around" span is one number for the
        # whole loop, so nothing is pulled in from neighbours here (it is still
        # pushed out to them on commit).
        propagated = []
    elif groups:
        # Concatenated without resampling for now: `default_spans` reads edge
        # lengths off the whole side, which the joined polyline already is. The
        # allocation that puts a grid vertex on each internal corner needs the
        # resolved span, so it runs further down.
        generation_input = [_joined([prepared.sides[i] for i in run]) for run in groups]
        num_sides = len(generation_input)
        defaults = state_mod.scale_default_spans(
            state, generator.default_spans(generation_input))
        defaults, propagated = _propagated_defaults(obj, generator, corner_source_ids, defaults)
    else:
        generation_input = prepared.sides
        num_sides = len(generation_input)
        # Resolution first, propagation second: a span taken from a committed
        # neighbour has to survive the preset, or the two patches stop welding.
        defaults = state_mod.scale_default_spans(
            state, generator.default_spans(generation_input))
        defaults, propagated = _propagated_defaults(obj, generator, corner_source_ids, defaults)

    # An already-committed patch comes back with the spans it was committed
    # with -- they beat both the computed defaults and propagation, which would
    # otherwise silently re-shape a patch the user had already tuned by hand.
    if committed:
        stored = mesh_build.lookup_patch_settings(obj, face_id)
        if stored:
            for key in ("span_u", "span_v", "span"):
                value = stored.get(key)
                if isinstance(value, int) and value >= 1:
                    defaults[key] = value

    span_u = defaults.get("span_u", state.span_u)
    span_v = defaults.get("span_v", state.span_v)
    span = defaults.get("span", state.span)
    if span_overrides:
        span_u = span_overrides.get("span_u", span_u)
        span_v = span_overrides.get("span_v", span_v)
        span = span_overrides.get("span", span)

    # A matched side only reproduces the neighbour's vertices if the generator
    # asks for exactly as many points as it was handed, so the match decides the
    # span that drives it. A grid has one span per *direction*, so pinning one
    # side pins the opposite one's count too -- that's the generator's model,
    # not a choice made here.
    #
    # A **pin** always decides; an **automatic** match only seeds the span the
    # first time the patch is generated. Otherwise scrolling the span on a side
    # that happens to border a committed neighbour would do nothing at all --
    # the match would put its own count straight back every regeneration, and
    # the control would look broken. Changing it away from the neighbour's
    # count instead drops that substitution: the two can no longer weld, which
    # is what asking for a different count means.
    #
    # Sorted, and weakest first, so the strongest match is the one that ends up
    # driving the span: a ring keys its two rims separately (both drive
    # "around"), so dict order would otherwise decide which committed neighbour
    # the band reproduces. `_honours` then drops whichever rim the resolved
    # count can no longer reproduce.
    for key, (_reference, points, pinned) in sorted(
            winners.items(), key=lambda item: (item[1][2], len(item[1][1]))):
        if key.startswith("side:"):
            continue
        if not (pinned or span_overrides is None):
            continue
        count = len(points) - 1
        base = sidematch.span_base(key)
        if base == "span_u":
            span_u = count
        elif base == "span_v":
            span_v = count
        else:
            span = count

    if groups:
        # A group of `n` sub-sides needs at least `n` segments, or one of them
        # gets none and its end corner -- a B-rep vertex a neighbour welds to
        # -- is no longer on our grid. Floored per span rather than globally,
        # so a quad whose U side is merged does not also inflate V.
        floors: dict[str, int] = {}
        group_pins = _group_pins(winners, groups)
        for position, run in enumerate(groups):
            key = sidematch.span_base(
                sidematch.span_key_for(generator.name, sidematch.SideSlot(
                    position, 0, position)))
            pins = group_pins[position]
            # Room for what the matched sub-sides must reproduce exactly, plus
            # one segment for each of the rest: below that the pin set cannot
            # fit and `allocate_group_segments` drops it whole, which is a
            # boundary that had been arranged to weld coming back cracked.
            needed = max(len(run), sum(pins.values()) + (len(run) - len(pins)))
            floors[key] = max(floors.get(key, 1), needed)
        span_u = max(span_u, floors.get("span_u", 1))
        span_v = max(span_v, floors.get("span_v", 1))
        span = max(span, floors.get("span", 1))

    spans = {"span_u": span_u, "span_v": span_v, "span": span}
    group_counts: "list[list[int]]" = []
    if groups:
        # Allocated once, here, because the counts the matching is checked
        # against have to be the very numbers the polylines are built from --
        # two calls to the allocator could only ever agree by luck. A sub-side
        # carrying a neighbour's vertices is pinned to its exact count; the
        # rest share out what is left by arc length.
        for position, run in enumerate(groups):
            group_counts.append(patchprep.allocate_group_segments(
                [prepared.sides[i] for i in run],
                _span_for_group(spans, generator.name, position),
                group_pins[position]))
        for run, counts in zip(groups, group_counts):
            if len(run) > 1:
                spans.update({f"side:{index}": count
                              for index, count in zip(run, counts)})
    nside_spokes = None
    if generator.name == constants.NSIDE:
        # An N-Side patch splits every side at a spoke, so with nothing matched
        # every side carries the same even count -- and a match moves only the
        # two spokes its own side sits between, which is what lets several
        # neighbours be reproduced on one patch. The allocation is settled here,
        # before any side is rewritten, and handed down as a per-side span so
        # `_honours` drops exactly the matches it could not fit.
        span = generators.nside.even_span(span)
        nside_spokes, _refused = nside_allocation(
            len(groups) if groups else len(prepared.sides), span, winners)
        segments_of = generators.nside.side_segments(nside_spokes)
        spans.update({f"side:{index}": count
                      for index, count in enumerate(segments_of)})

    sidematch.apply_side_matches(context, obj, prepared, generator.name, spans, winners=winners)

    if groups:
        # Now that the spans are final, each group is rebuilt with its share of
        # them allocated to its sub-sides as integers -- which is what puts a
        # grid vertex exactly on every corner *inside* the group. Built after
        # the substitution, so a single-side group that was matched still hands
        # its neighbour's own vertices through untouched.
        generation_input = [
            patchprep.group_side_points([prepared.sides[i] for i in run], counts)
            for run, counts in zip(groups, group_counts)]

    bvh = (geometry.build_bvh_for_polygons(mesh, prepared.patch.poly_indices)
           if state.reproject else None)
    span_settings = {"span_u": span_u, "span_v": span_v, "span": span}
    if nside_spokes is not None:
        span_settings["spokes"] = nside_spokes
    if prepared.is_ring:
        # Which loops now carry a neighbour's own vertices rather than a sample
        # of the CAD boundary. The ring has to know: a matched rim may not be
        # phase-aligned or resampled, or the match is thrown away and the two
        # rims come back half a step apart. See generators/ring.py.
        span_settings["locked_loops"] = sorted(sidematch.applied_loops())
        # And *which* sides of them, with the count each was handed: a rim cut
        # into several sides has only some of them carrying the neighbour's
        # vertices, and the allocation may not redistribute those away.
        span_settings["matched_sides"] = sidematch.applied_side_counts()
    result = generator.generate(generation_input, span_settings, bvh=bvh)

    # Cell shape, once the grid exists. Every generator here interpolates
    # between opposite sides, which stops being the right answer as soon as the
    # sides do not face each other -- a concave rim bunches the cells against
    # it, an acute corner collapses them -- and none of that is decided by the
    # boundary, so no choice of corners or spans can fix it. The boundary is
    # pinned, so this changes nothing a neighbour welds to, and it needs the
    # BVH: see `geometry.relax_interior_points` for why there is nothing safe
    # to do with reprojection off.
    if bvh is not None:
        geometry.relax_interior_points(
            result.verts, result.faces, result.boundary_local_indices,
            bvh, state.relax_iterations)

    mesh_build.update_preview_object(context, obj, result, corner_source_ids)
    return PatchPreview(generator, num_sides, prepared.num_loops, (span_u, span_v, span),
                        corner_source_ids, propagated, committed)


def regenerate_active_preview(context: bpy.types.Context) -> bool:
    """Re-run generation for the currently locked-in patch (state.active_face_id)
    using the current span settings. Used by the Update Preview operator and
    by the span/reproject property update callbacks (state.py) so dragging a
    slider updates the preview live instead of requiring a manual click.
    """
    state = context.scene.plasticity_retop
    if state.active_face_id == -1 or state.source_object_name not in bpy.data.objects:
        return False

    obj = bpy.data.objects[state.source_object_name]
    span_overrides = {"span_u": state.span_u, "span_v": state.span_v, "span": state.span}
    preview = _generate_for_face(context, obj, state.active_face_id, span_overrides)
    if preview is None:
        return False

    # Which generator ran can change under a live update -- toggling N-gon mode
    # is exactly that -- and the panel, the overlay and the commit path all read
    # the patch's shape from these.
    state.generator_name = preview.generator.name
    state.num_sides = preview.num_sides
    state.num_loops = preview.num_loops
    return True


def update_committed_count(
    context: bpy.types.Context, obj: bpy.types.Object | None
) -> None:
    """Refresh the panel's cached "N patches done" figure. Called whenever the
    result mesh changes, so the panel never has to walk it while redrawing.
    """
    state = context.scene.plasticity_retop
    state.committed_patch_count = len(mesh_build.committed_face_ids(obj)) if obj else 0


def begin_reedit(
    context: bpy.types.Context, obj: bpy.types.Object, face_id: int
) -> int:
    """Take patch `face_id`'s existing geometry out of the result mesh so the
    re-edit rebuilds it from nothing, and remember the snapshot that puts it
    back. Returns how many faces were removed.

    Removing on pick rather than on commit is what makes a re-edit legible: the
    old patch disappears the moment you click it, so "nothing was removed" shows
    up immediately instead of surfacing as two overlapping surfaces afterwards.
    """
    state = context.scene.plasticity_retop
    removed, backup = mesh_build.remove_patch_from_result(obj, face_id)
    state.reedit_removed_faces = removed
    state.reedit_backup_mesh = backup
    state.reedit_result_object = mesh_build.result_object_name_for(obj) if backup else ""
    update_committed_count(context, obj)
    print(f"[Plasticity Retop] Re-editing patch {face_id} of '{obj.name}': "
          f"removed {removed} existing face(s)")
    if removed:
        # Taking the patch out edits the result mesh and creates the snapshot
        # datablock: both belong in an undo step of their own.
        push_undo(f"Retop: re-edit patch {face_id}")
    return removed


def _clear_reedit(state: state_mod.RetopPatchState) -> None:
    state.editing_committed = False
    state.reedit_removed_faces = 0
    state.reedit_backup_mesh = ""
    state.reedit_result_object = ""


def keep_reedit_removal(context: bpy.types.Context) -> None:
    """The re-edit was committed: the snapshot of the old patch isn't needed."""
    state = context.scene.plasticity_retop
    if state.reedit_backup_mesh:
        mesh_build.drop_result_snapshot(state.reedit_backup_mesh)
    _clear_reedit(state)


def restore_reedit_removal(context: bpy.types.Context) -> None:
    """Put back the patch a re-edit took out (discard, Esc, leaving the object,
    ending the session): an uncommitted re-edit must never lose topology.
    """
    state = context.scene.plasticity_retop
    if state.reedit_backup_mesh:
        mesh_build.restore_result_snapshot(state.reedit_result_object, state.reedit_backup_mesh)
        update_committed_count(context, bpy.data.objects.get(state.session_object_name)
                               or bpy.data.objects.get(state.source_object_name))
    _clear_reedit(state)


def _is_own_scaffolding(obj: bpy.types.Object) -> bool:
    """True for objects this addon itself creates (the live preview and the
    committed result meshes). They sit right on top of the surface being
    picked -- the preview even sits slightly in front of it when Preview
    Offset is used -- so a raycast must look straight through them instead of
    treating them as an occluder.
    """
    return (obj.name == mesh_build.PREVIEW_OBJ_NAME
            or obj.name.endswith(mesh_build.RESULT_NAME_SUFFIX))


def _raycast_patch_ray(
    context: bpy.types.Context,
    ray_origin: mathutils.Vector,
    ray_direction: mathutils.Vector,
    space: bpy.types.SpaceView3D | None = None,
) -> tuple[bpy.types.Object | None, int | None, float | None]:
    """Cast `ray_origin`/`ray_direction` through the scene and return
    (hit_object, face_id, distance) for the first Plasticity mesh that is
    actually visible in this viewport, or (None, None, None). `distance` is
    measured in world units from `ray_origin`.

    Two classes of hit are skipped by re-casting from just past them rather
    than aborting the whole cast:

    - objects hidden in this viewport (Local View '/', eye/collection
      toggles): context.scene.ray_cast ignores per-viewport visibility, so
      without this it would happily pick a patch on an isolated-away object.
    - this addon's own preview/result meshes: they're coincident with (or
      pushed in front of) the very surface being hovered, so treating them as
      an occluder made the hover flicker -- the ray would hit the preview,
      report "no patch here", delete the preview, then hit the source mesh
      again on the next mouse move and rebuild it, over and over.

    Respects the Pick Max Distance setting (0 = unlimited).
    """
    depsgraph = context.evaluated_depsgraph_get()
    retop_state = context.scene.plasticity_retop
    max_distance = state_mod.to_blender_units(retop_state, retop_state.pick_max_distance)
    origin = ray_origin
    for _attempt in range(64):
        result, location, _normal, index, hit_obj, _matrix = context.scene.ray_cast(
            depsgraph, origin, ray_direction)

        if not result:
            return None, None, None

        distance = (location - ray_origin).length
        if max_distance > 0.0 and distance > max_distance:
            return None, None, None

        visible = hit_obj.visible_get(viewport=space) if space else hit_obj.visible_get()
        # A non-Plasticity mesh in the way used to abort the whole cast, which
        # made anything behind it unpickable -- a stand-in, a boolean cutter, a
        # block-out. It is an obstacle like the others, so look through it.
        if not visible or _is_own_scaffolding(hit_obj) or not _is_plasticity_mesh(hit_obj):
            # Step past this hit and keep looking along the same ray. Scaled to
            # the distance travelled: a fixed epsilon is either too small to
            # clear the surface at far range (the same hit repeats until the
            # attempts run out) or big enough to skip past a thin recess floor
            # on a small part.
            origin = location + ray_direction * max(1e-6, distance * 1e-5)
            continue

        face_id_of_poly = patch_data.analyse(hit_obj.data).face_id_of_poly
        if index < 0 or index >= len(face_id_of_poly):
            return None, None, None
        return hit_obj, face_id_of_poly[index], distance

    return None, None, None


def patch_hit_distance(
    ray_origin: mathutils.Vector,
    ray_direction: mathutils.Vector,
    obj: bpy.types.Object | None,
    face_id: int,
) -> float | None:
    """Distance from `ray_origin` to where the ray strikes patch `face_id` of
    `obj`, or None if the ray doesn't hit that particular patch. Used for the
    hover hysteresis in the modal picker.
    """
    if obj is None or obj.name not in bpy.data.objects:
        return None

    matrix_inv = obj.matrix_world.inverted()
    local_origin = matrix_inv @ ray_origin
    # direction transforms without translation
    local_dir = (matrix_inv.to_3x3() @ ray_direction).normalized()

    hit, location, _normal, index = obj.ray_cast(local_origin, local_dir)
    if not hit:
        return None

    face_id_of_poly = patch_data.analyse(obj.data).face_id_of_poly
    if index < 0 or index >= len(face_id_of_poly) or face_id_of_poly[index] != face_id:
        return None

    return ((obj.matrix_world @ location) - ray_origin).length


def viewport_region(
    context: bpy.types.Context,
) -> tuple[bpy.types.Region | None, bpy.types.RegionView3D | None]:
    """(region, region_3d) of the 3D viewport's WINDOW region, or (None, None).

    context.region / context.region_data can't be trusted inside a modal: when
    the operator was launched from the N-panel button, the current region is
    the UI one (and region_data is None), which silently produced an invalid
    ray -- so nothing was ever hit and clicks appeared to do nothing.
    """
    area = context.area
    if area is None or area.type != 'VIEW_3D':
        return None, None

    region = context.region
    if region is None or region.type != 'WINDOW':
        region = next((r for r in area.regions if r.type == 'WINDOW'), None)

    rv3d = context.region_data
    if rv3d is None:
        space = area.spaces.active
        rv3d = getattr(space, "region_3d", None)

    if region is None or rv3d is None:
        return None, None
    return region, rv3d


# Events the modal must never swallow when the cursor is outside the 3D view's
# WINDOW region -- which is *all* of them; the set is here so the rule can be
# asserted rather than only described. Clicks and scrolls are the obvious ones;
# mouse moves are the non-obvious part (Blender drives button highlighting from
# them, so eating MOUSEMOVE over the N-panel leaves the panel unclickable even
# while the clicks are let through), and the keys matter too, since digits
# collide with a field being typed into and Enter confirms the wrong thing.
PANEL_EVENTS = {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE', 'LEFTMOUSE', 'RIGHTMOUSE',
                'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE',
                'RET', 'NUMPAD_ENTER', 'ESC', 'TAB', 'BACK_SPACE',
                'M', 'N', 'X', 'ZERO', 'ONE', 'TWO', 'THREE', 'FOUR',
                'FIVE', 'SIX', 'SEVEN', 'EIGHT', 'NINE'}


# Regions drawn *over* the 3D view's WINDOW region. With Region Overlap on --
# Blender's default -- the WINDOW region spans the whole area and the N-panel,
# toolbar and headers float on top of it, so a point under the N-panel is
# genuinely inside the WINDOW region. Testing that region alone therefore says
# "over the viewport" while the pointer is over the panel, which is exactly how
# the modal ended up swallowing the panel's events.
OVERLAY_REGION_TYPES = {'UI', 'TOOLS', 'TOOL_PROPS', 'HEADER', 'TOOL_HEADER',
                        'NAV_BAR', 'FOOTER', 'ASSET_SHELF', 'ASSET_SHELF_HEADER',
                        'HUD', 'EXECUTE', 'CHANNELS'}


def point_in_region(region: bpy.types.Region | None, x: float, y: float) -> bool:
    """Whether a window-absolute point is inside `region`.

    Window-absolute because `context.region` is unreliable inside a modal (it
    can be the N-panel's), so the modal works from window coordinates and the
    region it resolved itself -- see viewport_region.
    """
    if region is None:
        return False
    local_x = x - region.x
    local_y = y - region.y
    return 0 <= local_x <= region.width and 0 <= local_y <= region.height


def point_in_viewport(
    area: bpy.types.Area | None,
    region: bpy.types.Region | None,
    x: float,
    y: float,
) -> bool:
    """Whether the point is over the 3D view *and not* over a panel floating
    on it. See OVERLAY_REGION_TYPES for why the second half is needed.
    """
    if not point_in_region(region, x, y):
        return False
    if area is None:
        return True
    for other in area.regions:
        # A collapsed region still exists, reporting a 1px size; it covers
        # nothing and must not veto the whole viewport.
        if (other.type in OVERLAY_REGION_TYPES
                and other.width > 1 and other.height > 1
                and point_in_region(other, x, y)):
            return False
    return True


def ray_from_event(
    context: bpy.types.Context, event: bpy.types.Event
) -> tuple[mathutils.Vector | None, mathutils.Vector | None]:
    """(ray_origin, ray_direction) under the mouse, or (None, None) when the
    cursor isn't over the 3D viewport.
    """
    return ray_from_window(context, event.mouse_x, event.mouse_y)


def ray_from_window(
    context: bpy.types.Context, mouse_x: float, mouse_y: float
) -> tuple[mathutils.Vector | None, mathutils.Vector | None]:
    """The same, from window-absolute coordinates rather than from an event.

    Window-absolute throughout, converted against the WINDOW region itself:
    `event.mouse_region_*` is relative to whichever region received the event,
    which may not be the one being cast into. Which is also why an operator the
    modal dispatches can use this at all -- it reads the pointer the modal left
    in `overlay.cursor_window` instead of an event it never gets.
    """
    region, rv3d = viewport_region(context)
    if region is None:
        return None, None

    x = mouse_x - region.x
    y = mouse_y - region.y
    if not (0 <= x <= region.width and 0 <= y <= region.height):
        return None, None  # cursor is outside the viewport (e.g. over the N-panel)

    coord = (x, y)
    return (view3d_utils.region_2d_to_origin_3d(region, rv3d, coord),
            view3d_utils.region_2d_to_vector_3d(region, rv3d, coord))


def _raycast_patch(
    context: bpy.types.Context, event: bpy.types.Event
) -> tuple[bpy.types.Object | None, int | None, float | None]:
    """Raycast under the mouse (Object Mode, viewport region) and return
    (hit_object, face_id, distance) for the first pickable Plasticity patch,
    or (None, None, None). See _raycast_patch_ray for the filtering rules.
    """
    ray_origin, ray_direction = ray_from_event(context, event)
    if ray_origin is None:
        return None, None, None

    return _raycast_patch_ray(context, ray_origin, ray_direction, space=context.space_data)


def nearest_side_to_cursor(
    context: bpy.types.Context, event: bpy.types.Event, max_pixels: float = 60.0
) -> int:
    """Flat index of the side nearest the cursor, or -1.

    Screen space, not a raycast: the sides are polylines lying exactly on the
    surface, so a ray would hit the surface beside them as often as the line
    itself. Distance to the projected segments is what "pointing at an edge"
    actually means on screen.
    """
    region, rv3d = viewport_region(context)
    if region is None or rv3d is None:
        return -1

    mouse = mathutils.Vector((event.mouse_x - region.x, event.mouse_y - region.y))
    best_index = -1
    best_distance = max_pixels

    for reference in sidematch.active_sides():
        projected = []
        for point in reference.points:
            screen = view3d_utils.location_3d_to_region_2d(region, rv3d, point)
            if screen is not None:
                projected.append(screen)
        for start, end in zip(projected, projected[1:]):
            distance = _distance_to_segment(mouse, start, end)
            if distance < best_distance:
                best_distance = distance
                best_index = reference.index

    return best_index


def _distance_to_segment(
    point: mathutils.Vector, start: mathutils.Vector, end: mathutils.Vector
) -> float:
    segment = end - start
    length_squared = segment.length_squared
    if length_squared < 1e-12:
        return (point - start).length
    t = max(0.0, min(1.0, (point - start).dot(segment) / length_squared))
    return (point - (start + segment * t)).length


# The corner set itself lives in `sidematch`, a leaf: the overlay reads it on
# every redraw and must never import this module, which imports it back. Only
# the hit test stays here, because it needs a region and a viewport.
def side_bubble_under_cursor(context: bpy.types.Context, event: bpy.types.Event) -> int:
    """Flat index of the side whose group bubble the cursor is on, or -1.

    The bubbles are screen-space discs, so this is a point-in-disc test rather
    than a distance to a polyline -- what the user is aiming at is the bubble,
    not the edge under it. Both the anchor and the radius come from the same
    places the drawing takes them (`sidematch.side_midpoint`,
    `overlay.GROUP_BUBBLE_SIZE`): a bubble that is not clickable where it is
    drawn is worse than no bubble at all.

    Nearest wins when two overlap, which they do on a short side of a dense
    part -- a click then takes the one whose centre is closer, which is the
    one the pointer is visibly on.
    """
    region, rv3d = viewport_region(context)
    if region is None or rv3d is None:
        return -1

    state = context.scene.plasticity_retop
    radius = overlay.GROUP_BUBBLE_SIZE * max(0.5, state.overlay_scale) / 2.0
    mouse = mathutils.Vector((event.mouse_x - region.x, event.mouse_y - region.y))

    best_index = -1
    best_distance = radius
    for reference in sidematch.active_sides():
        anchor = sidematch.side_midpoint(reference.points)
        if anchor is None:
            continue
        screen = view3d_utils.location_3d_to_region_2d(region, rv3d, anchor)
        if screen is None:
            continue
        distance = (mouse - screen).length
        if distance < best_distance:
            best_distance = distance
            best_index = reference.index
    return best_index


# --- One patch from several surfaces -----------------------------------------
#
# The assembly itself lives in `patch_data` (see the section there): these are
# the session's half of it -- what Shift+click gathers, and the two moments
# that write a composite to the mesh.


def surface_selection(state: state_mod.RetopPatchState) -> list[int]:
    """The surfaces Shift+click has gathered, in the order they were picked."""
    return patch_data.parse_surface_selection(
        getattr(state, "surface_selection", ""))


def set_surface_selection(
    state: state_mod.RetopPatchState, face_ids: "list[int]"
) -> None:
    state.surface_selection = patch_data.format_surface_selection(face_ids)


def toggle_patch_surface(
    context: bpy.types.Context, obj: bpy.types.Object, face_id: int
) -> tuple[bool, str]:
    """Add `face_id` to the surfaces this patch will be built from, or take it
    back out.

    Returns (changed, message) -- the message is the refusal when it did not.
    """
    state = context.scene.plasticity_retop
    selection = surface_selection(state)

    if face_id in selection:
        selection.remove(face_id)
        set_surface_selection(state, selection)
        return True, (f"Dropped surface {face_id} — {len(selection)} selected"
                      if selection else "Surface selection cleared")

    # A committed patch's faces carry *its* id, and the composite would carry a
    # new one: nothing would ever delete them again, so they would sit under
    # the new grid as a second surface. Refused rather than resolved --
    # stamping them over is a re-edit, and a re-edit is something you ask for.
    if mesh_build.is_patch_committed(obj, face_id):
        return False, (f"surface {face_id} is already retopologized — delete its "
                       "patch first (X while re-editing it)")

    analysis = patch_data.analyse(obj.data)
    patch = analysis.patches.get(face_id)
    if patch is None or not patch.boundary_loops:
        return False, f"surface {face_id} has no usable boundary"

    # Contiguity, checked as the selection is built rather than when the patch
    # is opened. Two reasons it is the right moment: the answer is cheap here
    # (the analysis is already in hand and the neighbours are one attribute
    # away), and a refusal at the click points at the surface that caused it,
    # while one at the end can only say the set is wrong. What it prevents is a
    # patch whose surfaces meet at a point -- a pinched boundary -- or not at
    # all, which comes back with two outer loops and is read as a band.
    if selection and not patch_data.patch_neighbour_ids(patch).intersection(selection):
        return False, ("that surface does not touch the selection — one patch has to "
                       "be one connected area")

    selection.append(face_id)
    set_surface_selection(state, selection)
    return True, f"{len(selection)} surfaces selected"


def build_composite(
    context: bpy.types.Context, obj: bpy.types.Object, face_ids: "list[int]"
) -> tuple[int | None, str]:
    """Write `face_ids` to the mesh as one composite patch and return its id.

    Adding a surface to a patch that is *already* a composite rebuilds it
    **flat**: the composites the selection was standing on are replaced by the
    one that now owns their surfaces, so a surface list is always raw
    Plasticity face ids and nothing has to follow a chain.
    """
    mesh = obj.data
    analysis = patch_data.analyse(mesh)

    surfaces: list[int] = []
    for face_id in face_ids:
        surfaces.extend(analysis.composites.get(face_id, [face_id]))
    surfaces = list(dict.fromkeys(surfaces))
    if len(surfaces) < 2:
        return None, "a patch needs at least two surfaces to be built from several"

    composites = patch_data.read_composites(mesh)
    # Allocated *before* the old composites are dropped, so an id that a result
    # mesh may still have faces stamped with is never handed out twice.
    composite_id = patch_data.next_composite_id(composites)
    for face_id in face_ids:
        composites.pop(face_id, None)
    composites[composite_id] = surfaces
    patch_data.write_composites(mesh, composites)
    return composite_id, f"One patch from {len(surfaces)} surfaces"


def split_composite(
    context: bpy.types.Context, obj: bpy.types.Object, composite_id: int
) -> tuple[bool, str]:
    """Take a composite patch apart, so its surfaces are patches again."""
    mesh = obj.data
    composites = patch_data.read_composites(mesh)
    surfaces = composites.get(composite_id)
    if not surfaces:
        return False, "that patch is a single surface already"

    # Committed, *or* open in a re-edit -- which is the same thing seen halfway
    # through: picking a committed patch takes its faces out of the result mesh
    # before anything else runs, so `is_patch_committed` says no exactly when
    # the button offering this is on screen. Either way those faces would be
    # left naming a patch that no longer exists, and nothing would ever delete
    # them again.
    state = context.scene.plasticity_retop
    reediting = (state.editing_committed and state.active_face_id == composite_id)
    if reediting or mesh_build.is_patch_committed(obj, composite_id):
        return False, ("it is already retopologized — delete the patch first "
                       f"({keymap.describe('delete_patch')} while re-editing it), or "
                       "its faces would be left naming a patch that no longer exists")

    del composites[composite_id]
    patch_data.write_composites(mesh, composites)
    return True, f"Split back into {len(surfaces)} patches"


def composite_surfaces(obj: bpy.types.Object | None, face_id: int) -> list[int]:
    """The Plasticity surfaces `face_id` is built from, or [] when it is a
    single one. What the panel reads to say how many a patch covers."""
    if obj is None or obj.type != 'MESH' or face_id >= 0:
        return []
    return list(patch_data.analyse(obj.data).composites.get(face_id, []))


def set_active_patch(
    context: bpy.types.Context, obj: bpy.types.Object, face_id: int
) -> tuple[str | None, int | None, list[str] | None]:
    """Generate a preview for `face_id` on `obj` and lock it in as the active
    patch (the state the N-panel's span controls act on). Returns
    (generator_name, num_sides, propagated_keys), or (None, None, None) if the
    patch can't be generated. Shared by the viewport picker and by tests, so
    both go through exactly one code path.
    """
    # A pin names a side *of the patch it was picked on*, by index. Carrying it
    # into the next patch would silently pin whichever side happened to land on
    # that index. Cleared here rather than in the modal so every caller --
    # viewport, panel, tests -- gets the same guarantee.
    state = context.scene.plasticity_retop
    state.side_overrides = ""
    state.hovered_side = -1
    # The corner set names sides by index too, so it is per patch for exactly
    # the same reason -- and the editor is closed rather than carried over: it
    # is a gesture on one patch, not a mode the session sits in.
    # Cleared, then put back from the record if this patch has one: a patch
    # committed as a Quad because two of its five sides were merged has to
    # reopen as that Quad, the same rule its spans and its n-gon mode follow.
    # Here rather than inside `_generate_for_face`, because the restore is
    # about *opening* a patch -- doing it on every regeneration would undo the
    # user's next change to the grouping.
    stored_settings = mesh_build.lookup_patch_settings(obj, face_id) or {}
    state.side_groups = str(stored_settings.get("side_groups", "") or "")
    state.group_warning = ""
    state.corner_edit = False
    state.side_groups_backup = ""
    state.hovered_bubble = -1
    # Which patch this one copied from, and which way round. Per patch, for the
    # same reason a pin is: carried over, the first click on the *next* patch
    # would come back swapped.
    state.copy_source_face_id = -1
    state.copy_source_swapped = False

    # Same reason as in enter_session_object: claim untracked pre-existing
    # retopology before deciding whether this patch is a re-edit. Cheap no-op
    # once every face carries its patch id.
    mesh_build.adopt_untracked_faces(obj)

    # Generate first, remove second: _generate_for_face reads the result mesh to
    # decide this is a re-edit and to recover the spans it was committed with.
    preview = _generate_for_face(context, obj, face_id)
    if preview is None:
        return None, None, None

    state.active_face_id = face_id
    state.generator_name = preview.generator.name
    state.num_sides = preview.num_sides
    state.num_loops = preview.num_loops
    state.source_object_name = obj.name
    state.span_u, state.span_v, state.span = preview.spans
    state.editing_committed = preview.committed
    if preview.committed:
        begin_reedit(context, obj, face_id)
    return preview.generator.name, preview.num_sides, preview.propagated


def session_is_running() -> bool:
    return _SESSION_RUNNING


# Re-exported so `operators.TWO_SPAN_GENERATORS` keeps working for the panel
# and the tests; the definition lives in `constants`, which the overlay can
# also reach without importing this module back.
TWO_SPAN_GENERATORS = constants.TWO_SPAN_GENERATORS

# Number-row and numpad digits, for typing a span directly.
DIGIT_KEYS: dict[str, str] = {}
for _d in range(10):
    DIGIT_KEYS[("ZERO", "ONE", "TWO", "THREE", "FOUR",
                "FIVE", "SIX", "SEVEN", "EIGHT", "NINE")[_d]] = str(_d)
    DIGIT_KEYS[f"NUMPAD_{_d}"] = str(_d)


def active_span_prop(state: state_mod.RetopPatchState) -> str:
    """Name of the span property the wheel/keyboard adjusts for the current
    patch: quads and wedges have two spans (Tab switches between them),
    everything else has a single one shared by all sides.
    """
    if state.generator_name in TWO_SPAN_GENERATORS:
        return "span_u" if state.span_axis == 'U' else "span_v"
    return "span"


def _clear_match_state(state: state_mod.RetopPatchState) -> None:
    """The side picker is per patch: its cache holds Vectors describing a
    preview, and its pins describe sides that patch had. `match_mode` is a
    preference, not patch state, so it survives."""
    sidematch.clear_side_references()
    state.hovered_side = -1
    state.side_overrides = ""
    state.copy_source_face_id = -1
    state.copy_source_swapped = False


def end_session(context: bpy.types.Context, push: bool = True) -> None:
    """Leave the current retop session entirely: drop any preview, stop
    highlighting the result mesh, and clear session/patch state. Safe to call
    when no modal is running (used to reset stale session state).

    `push=False` is for the one caller that is *itself* reacting to an undo:
    pushing a step straight after Ctrl+Z would truncate the redo branch the
    user just created, i.e. take away the Ctrl+Shift+Z that puts it back.
    """
    global _SESSION_RUNNING
    _SESSION_RUNNING = False

    state = context.scene.plasticity_retop
    # A hand-edit round trip that never got its Tab back: the snapping and
    # auto-merge settings it overwrote are the user's, not ours, and leaving
    # them rewritten by a mode that is no longer open is the rudest failure
    # available. Cheap and idempotent -- it is a no-op with no snapshot saved.
    tweak.restore_tool_settings(context)
    # The one place the preview object is actually freed: ending the session is
    # a deliberate moment, unlike a hover.
    mesh_build.remove_preview_object()
    overlay.hover_committed = False
    overlay.cursor_window = None
    # An in-flight re-edit is rolled back, never silently dropped: its patch was
    # removed from the result mesh on pick and was never re-committed.
    restore_reedit_removal(context)

    # Clear the state *before* refreshing: the look of every result mesh is
    # derived from session state, so refreshing first would just re-apply the
    # highlight we're trying to drop.
    state.session_active = False
    state.session_object_name = ""
    state.session_phase = 'OBJECT'
    state.active_face_id = -1
    state.generator_name = ""
    state.num_sides = 0
    state.editing_committed = False
    # Gathered surfaces describe a pick that never happened; nothing was written
    # to the mesh, so there is nothing to undo, only state to drop.
    state.surface_selection = ""
    _clear_match_state(state)

    mesh_build.refresh_result_appearance(context)
    if push:
        push_undo("Retop: end session")


def push_undo(message: str) -> None:
    """Give the objects a session just created their own undo step.

    Datablocks created between two undo steps are invisible to the one Ctrl+Z
    rolls back to, which is how the depsgraph ends up walking freed data. All
    of the session's ID creation happens at the two moments that call this.
    """
    if bpy.app.background:
        return  # no undo stack in --background, and the tests don't need one
    try:
        bpy.ops.ed.undo_push(message=message)
    except Exception:
        pass


def select_only(context: bpy.types.Context, obj: bpy.types.Object) -> None:
    """Make `obj` the selection and the active object.

    Everything Blender does "to the object" -- isolate, frame, the header, the
    properties editor -- reads the selection rather than anything this addon
    knows, so entering an object has to say so in the one language those
    commands speak. Failures are ignored on purpose: an object outside the
    current view layer cannot be selected, and that is not a reason to refuse
    the session.
    """
    try:
        for other in list(context.selected_objects):
            if other is not obj:
                other.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
    except (RuntimeError, ReferenceError):
        pass


def enter_session_object(
    context: bpy.types.Context, obj: bpy.types.Object | None
) -> None:
    """Enter `obj` for retopology: make sure its result mesh exists, highlight
    it, and move to the patch-picking phase.

    A result mesh resolves to its source first, so entering by way of the
    retopology -- selecting it in the outliner, say -- carries on where it left
    off instead of starting a session on a mesh with no patch data.
    """
    obj = resolve_session_object(obj)
    state = context.scene.plasticity_retop
    previous = bpy.data.objects.get(state.session_object_name)
    if previous is not None and previous is not obj:
        mesh_build.set_result_highlight(context, previous, False)

    mesh_build.ensure_result_object(context, obj)
    # Create the preview here too, rather than on the first hover: that keeps
    # every datablock this session needs inside the single undo step below.
    mesh_build.ensure_preview_object(context)
    # Retopology committed before per-face patch tracking existed has to be
    # claimed once, here, or its patches read as "never retopped": picking one
    # would quietly build a second grid on top of the first instead of
    # re-editing it.
    mesh_build.adopt_untracked_faces(obj)
    # Snapshots left by a re-edit that was undone, crashed or reloaded out from
    # under us: they carry a fake user, so nothing else would ever collect them.
    mesh_build.purge_stale_snapshots(keep_name=state.reedit_backup_mesh)
    update_committed_count(context, obj)
    # Entering an object *is* being in a session: don't rely on the caller
    # having set this first, or the highlight below resolves against stale
    # state and the result mesh silently stays un-highlighted.
    state.session_active = True
    state.session_object_name = obj.name
    state.session_phase = 'PATCH'
    mesh_build.set_result_highlight(context, obj, True)
    # Picking with the eyedropper also *selects* what was picked. Blender's own
    # object-scoped commands read the selection, not this addon's state, so
    # without it '/' isolates whatever happened to be selected before the
    # session started -- or nothing at all. Isolate is the one that matters
    # here (`sync_local_view` then pulls the retopology and the preview in with
    # it), but the same is true of every other thing Blender does to "the
    # object". Selection is not an ID, so this is safe outside an undo step;
    # it is inside the one below anyway.
    select_only(context, obj)
    # Starting a session while already isolated ('/') would otherwise create
    # the preview and result meshes outside the local view, i.e. invisible.
    mesh_build.sync_local_view(context)
    push_undo(f"Retop: enter {obj.name}")


def exit_session_object(context: bpy.types.Context) -> None:
    """Leave the current object but keep the session running, so the next
    click picks another Plasticity object (workflow step 4 -> 5).
    """
    state = context.scene.plasticity_retop
    mesh_build.clear_preview_object()
    restore_reedit_removal(context)  # same rule as end_session

    # Same ordering rule as end_session: state first, then refresh.
    state.session_object_name = ""
    state.session_phase = 'OBJECT'
    state.active_face_id = -1
    state.generator_name = ""
    state.num_sides = 0
    # Surfaces of the object being left: they mean nothing on the next one.
    state.surface_selection = ""
    _clear_match_state(state)

    mesh_build.refresh_result_appearance(context)


def _match_report(
    state: state_mod.RetopPatchState, reference: sidematch.SideReference
) -> str:
    """What a click on a side just did, in the status bar."""
    pins = sidematch.side_override_map(state)
    kind = pins.get(reference.index)
    if kind == sidematch.PIN_EXCLUDED:
        return f"Side {reference.in_loop} released -- not matched"
    if kind is None:
        return f"Side {reference.in_loop} released"
    neighbour = ("patch " + str(reference.neighbour)) if reference.neighbour is not None \
        else "its neighbour"
    conflict = ""
    if state.match_conflicts:
        conflict = (f" -- {state.match_conflicts} other side(s) wanted the same span "
                    "and were outvoted")
    return (f"Side {reference.in_loop} matched to {neighbour}: "
            f"{reference.span} segment(s){conflict}")


class RETOP_OT_session(bpy.types.Operator):
    bl_idname = "retop.session"
    bl_label = "Start Retop Session"
    bl_description = ("Run the retopology workflow in the viewport: click a Plasticity object to "
                       "enter it, click its surfaces one after another to retopologize them, "
                       "Esc to leave the object, Esc again to end the session")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        """Object Mode only.

        Started from Edit Mode the session used to run its whole entry path --
        create the result mesh, create the preview object, push an undo step --
        and *then* have the modal hand the viewport straight back, because it
        does nothing outside Object Mode. Two datablocks created from inside an
        edit session, for a session that never opened: that is the shape of the
        Ctrl+Z crash the undo invariant exists to prevent.
        """
        return context.mode == 'OBJECT'

    _hover_obj = None
    _hover_face_id = None
    _hover_generator_name = None
    _hover_num_sides = None
    _hover_num_loops = 1
    _hover_spans = None
    _hover_committed = False
    _hover_ngon = False
    _timer = None
    _last_phase = ""  # what the modal last set its cursor and status for
    # None until the first mouse move tells us which side of the region the
    # pointer is on; see _update_cursor.
    _cursor_in_viewport = None

    # phase -> (cursor, status line)
    _PHASE_UI = {
        'OBJECT': ('EYEDROPPER',
                   "Pick an object   |   Click: enter a Plasticity object   |   Esc: end session"),
        'PATCH': ('PAINT_CROSS',
                  "Pick a surface   |   Click: choose a patch (an already retopped one to "
                  "re-edit it)   |   Esc: leave this object"),
        'ADJUST': ('DEFAULT',
                   "Adjust spans in the Retop panel   |   Enter: commit   |   Esc: discard"),
        # Blender owns the viewport here, so the cursor is *restored* rather
        # than set (see _apply_phase_ui): the knife has its own, and a modal
        # cursor pinned on the window would sit on top of it.
        'TWEAK': ('DEFAULT',
                  "Hand-editing the retopology   |   K: knife   |   Ctrl+R: loop cut   |   "
                  "J: connect verts   |   G: move (snapped, auto-merge)   |   Tab: back to Retop"),
    }

    def _leave_for_other_mode(self, context: bpy.types.Context) -> None:
        """Drop back to picking an object because Blender left Object mode.

        Entering Edit Mode on the retopology is the normal way to hand-tweak
        it, and coming back to a session still claiming to be inside an object
        -- with a stale preview and a cursor to match -- is worse than starting
        the pick again.
        """
        state = context.scene.plasticity_retop
        if state.session_phase == 'OBJECT':
            return

        # The session put Blender in Edit Mode itself: that is the hand-edit
        # round trip, not somebody wandering off, and _modal_tweak owns both
        # ends of it. (_modal returns before reaching here in that phase; the
        # check is repeated because this reads as a mode-change policy and the
        # policy has an exception.)
        if state.session_phase == 'TWEAK':
            return

        # One exception, and it is not optional: a re-edit has that patch's
        # faces out of the result mesh and only a snapshot to put them back
        # with. If the mesh being edited *is* that result mesh, writing to it
        # from here is discarded the moment edit mode exits -- the patch would
        # be gone for good. Stay put; the panel says the session is paused.
        editing = getattr(context, "edit_object", None)
        if (state.editing_committed and state.reedit_result_object
                and editing is not None
                and editing.name == state.reedit_result_object):
            return

        exit_session_object(context)
        self._clear_hover(context)
        self._apply_phase_ui(context)
        self.report({'INFO'}, "Left the object: Blender is not in Object Mode")

    def _apply_phase_ui(self, context: bpy.types.Context) -> None:
        state = context.scene.plasticity_retop
        cursor, status = self._PHASE_UI.get(state.session_phase, ('DEFAULT', ""))
        # Only while the pointer is actually over the 3D view: a modal cursor is
        # set on the whole window, so setting it here unconditionally would put
        # the eyedropper over the panel's own buttons.
        if context.window and self._cursor_in_viewport is not False:
            if state.session_phase == 'TWEAK':
                # Blender's own tools draw the cursor while they have the
                # viewport; a modal cursor set on the window overrides theirs.
                context.window.cursor_modal_restore()
                self._cursor_in_viewport = None
            else:
                context.window.cursor_modal_set(cursor)
        if context.workspace:
            context.workspace.status_text_set(f"Retop — {status}")

    def _update_cursor(
        self, context: bpy.types.Context, over_viewport: bool
    ) -> None:
        """Session cursor over the 3D view, the normal one everywhere else.

        `cursor_modal_set` applies to the entire window, so without this the
        retop cursor sits over the N-panel and every other editor for as long
        as the session runs -- which reads as "the UI is not for you".
        """
        if over_viewport == self._cursor_in_viewport or context.window is None:
            return
        self._cursor_in_viewport = over_viewport
        if over_viewport:
            state = context.scene.plasticity_retop
            cursor, _status = self._PHASE_UI.get(state.session_phase, ('DEFAULT', ""))
            context.window.cursor_modal_set(cursor)
        else:
            context.window.cursor_modal_restore()

    def _set_hover(
        self, context: bpy.types.Context, obj: bpy.types.Object, face_id: int
    ) -> bool:
        preview = _generate_for_face(context, obj, face_id)
        if preview is None:
            return False
        self._hover_obj = obj
        self._hover_face_id = face_id
        self._hover_generator_name = preview.generator.name
        self._hover_num_sides = preview.num_sides
        self._hover_num_loops = preview.num_loops
        self._hover_spans = preview.spans
        self._hover_committed = preview.committed
        self._hover_ngon = preview.ngon
        # so the overlay can advertise "Re-edit patch" instead of "Pick surface"
        overlay.hover_committed = preview.committed
        return True

    def _open_composite(
        self, context: bpy.types.Context, selection: "list[int]"
    ) -> bool:
        """Write the pending selection to the mesh as one patch and open it.

        A composite that cannot then be generated is **taken back apart**: a
        patch nothing can open is worse than a refusal, and the mesh would be
        left carrying one with no way to reach it -- the only route back is the
        Split button on a patch that never opened.
        """
        state = context.scene.plasticity_retop
        obj = bpy.data.objects.get(state.session_object_name)
        if obj is None:
            return False

        composite_id, message = build_composite(context, obj, selection)
        if composite_id is None:
            self.report({'WARNING'}, f"Can't build one patch: {message}")
            return False

        # The hover preview is one surface's grid; the whole patch replaces it.
        # Cleared first, since set_active_patch regenerates.
        self._clear_hover(context)
        generator, _sides, _propagated = set_active_patch(context, obj, composite_id)
        if generator is None:
            split_composite(context, obj, composite_id)
            self.report({'WARNING'},
                        "Those surfaces have no usable boundary together — undone")
            return False

        state.session_phase = 'ADJUST'
        self._set_typed("")
        self._apply_phase_ui(context)
        # A datablock changed, so it gets its own step: Ctrl+Z takes the patch
        # back apart rather than rolling the session up to the one before.
        push_undo("Retop: one patch from several surfaces")
        self.report({'INFO'}, f"{message} — {generator}")
        return True

    def _clear_hover(self, context: bpy.types.Context) -> None:
        mesh_build.clear_preview_object()
        self._hover_obj = None
        self._hover_face_id = None
        self._hover_committed = False
        self._hover_ngon = False
        overlay.hover_committed = False

    def _keeps_current_hover(
        self,
        context: bpy.types.Context,
        event: bpy.types.Event,
        new_distance: float | None,
    ) -> bool:
        """True when the currently-hovered patch is still under the cursor at
        essentially the same depth as the newly-reported hit.

        Overlapping/coincident Plasticity surfaces make scene.ray_cast report
        whichever of them wins by a sub-epsilon margin, which alternates as
        the mouse moves and made the hover flip-flop between two patches
        (rebuilding the preview each frame = flicker). Sticking to the
        current patch unless the new one is *clearly* in front keeps the
        pick stable; the user can still reach the other surface by moving off
        the patch and back, or by hiding/isolating the one in the way.
        """
        if self._hover_obj is None or new_distance is None:
            return False

        ray_origin, ray_direction = ray_from_event(context, event)
        if ray_origin is None:
            return False

        current_distance = patch_hit_distance(ray_origin, ray_direction, self._hover_obj, self._hover_face_id)
        if current_distance is None:
            return False  # cursor genuinely left the current patch

        state = context.scene.plasticity_retop
        if state.pick_depth_tolerance > 0.0:
            tolerance = state_mod.to_blender_units(state, state.pick_depth_tolerance)
        else:
            # Automatic: proportional to view distance, which is already
            # scale-independent (a 1mm fillet is viewed from proportionally
            # closer than a 10m part), so no unit conversion applies.
            tolerance = max(1e-6, new_distance * 2e-3)
        return current_distance <= new_distance + tolerance

    def _modal_corners(
        self, context: bpy.types.Context, event: bpy.types.Event
    ) -> set[str] | None:
        """Mouse handling while the corner editor is open.

        Ahead of the side picker and independent of it: the editor is reached
        by pointing at a side, but it is not part of matching and must work
        with the side highlight turned off. It also takes the *plain* click,
        which is why `corner_toggle` is declared before `pin_neighbour` and
        `pin_neighbour` polls on the editor being closed -- both halves, so
        neither is load-bearing on its own.
        """
        state = context.scene.plasticity_retop

        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            state.hovered_bubble = side_bubble_under_cursor(context, event)
            # Neither of these means anything while corners are being chosen,
            # and leaving them set would paint a side green and outline a
            # patch in amber for gestures the editor has taken over.
            state.hovered_side = -1
            state.copy_hover_face_id = -1
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            # Dispatched rather than called, so the click stays a real binding
            # -- and through `_dispatch_bound`, which walks every action on the
            # event and runs the first whose poll passes. Taking the first
            # *match* would resolve every left click to `corner_toggle`.
            if self._dispatch_bound(context, event):
                return {'RUNNING_MODAL'}
            # Pointing at no corner: nothing to toggle, and emphatically not
            # the commit fallback -- committing from inside the editor would
            # bake a corner set the user was still choosing.
            return {'RUNNING_MODAL'}

        return None

    def _modal_match(
        self, context: bpy.types.Context, event: bpy.types.Event
    ) -> set[str] | None:
        """Mouse handling for the side highlight, while it is on. Returns a
        modal result to stop on, or None to let normal ADJUST handling have the
        event.

        Deliberately does *not* take Esc or the commit keys: the highlight is
        on by default, so swallowing Esc would mean the patch could no longer
        be discarded, and that trade is not worth one keystroke.
        """
        state = context.scene.plasticity_retop

        if event.type == 'MOUSEMOVE':
            state.hovered_side = nearest_side_to_cursor(context, event)
            # Ctrl+click is split by what is under the cursor -- a side opens
            # the corner editor, anything else copies a density -- so the
            # hover has to say which of the two it would be. An amber outline
            # over a hovered side would promise the copy and give the editor.
            state.copy_hover_face_id = (
                -1 if state.hovered_side != -1
                else _copy_source_under_cursor(context, event))
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            # A left click has only two jobs while adjusting: take the side
            # under the cursor, or -- pointing at nothing -- commit. So a
            # missed aim commits rather than doing nothing, same as right-click
            # and Enter.
            #
            # Only the *fallback* is hover-dependent, and only it stays here.
            # Taking the side is `retop.pin_side`, a normal binding resolved
            # like every other -- which is what makes the click remappable. It
            # was fixed only because it had been lumped in with a fallback it
            # never shared.
            if state.hovered_side == -1:
                return None
            bound = keymap.session_action_for(event)
            if bound == "pin_neighbour":
                self._run_bound_action(context, bound)
                return {'RUNNING_MODAL'}
            return None

        return None

    def _commit(self, context: bpy.types.Context) -> None:
        # commit_patch applies a half-typed span itself, so this is just the
        # guard: a click that lands on nothing must not raise on the poll.
        if mesh_build.has_preview():
            bpy.ops.retop.commit_patch()
        self._set_typed("")

    # Actions whose key must never reach Blender, even when they refuse. `X`
    # falling through during a session is `object.delete` -- it takes the CAD
    # object with it -- and `Tab` is `object.editmode_toggle`, which drops the
    # session out of the object it is in. Everything else is better off falling
    # through: `N` outside ADJUST should open the sidebar, and a right click
    # with nothing to commit should open the context menu.
    _MUST_CONSUME = ("delete_patch", "hand_edit")

    def _refusal(self, context: bpy.types.Context, action_id: str) -> str:
        if action_id == "delete_patch":
            return "Nothing to delete: this patch isn't committed yet"
        if action_id == "hand_edit":
            return tweak.can_tweak(context) or ""
        return ""

    def _run_bound_action(
        self, context: bpy.types.Context, action_id: str
    ) -> bool:
        """Run a session action's operator. Returns whether to consume the event.

        The modal dispatches these rather than letting them fall through to the
        keymap: an item in the 3D View keymap does not reliably beat a *mode*
        keymap, and the session's keys collide with those constantly. See
        keymap.py.
        """
        idname = keymap.operator_of(action_id)
        operator = getattr(bpy.ops.retop, idname.split(".", 1)[1], None)
        if operator is None:
            return False

        if not operator.poll():
            if action_id not in self._MUST_CONSUME:
                return False
            reason = self._refusal(context, action_id)
            if reason:
                self.report({'WARNING'}, reason)
            return True

        operator(**keymap.properties_of(action_id))
        return True

    def _dispatch_bound(
        self, context: bpy.types.Context, event: bpy.types.Event
    ) -> bool:
        """Run whichever action this key means right now. Returns whether the
        event was consumed.

        Several actions can share a key -- three share `TAB` -- so this walks
        them in declaration order and runs the first whose poll passes, which
        is what Blender itself does with keymap items. Taking the first *match*
        instead resolved every Tab to U/V, whose poll fails outside ADJUST, and
        the key then fell through to the keymap to be answered by whichever
        item happened to be registered first: it worked, but by an ordering
        nothing states, and in the OBJECT phase it reached Blender's own
        `object.editmode_toggle` -- Edit Mode on the CAD object, mid-session.

        When none of them polls, a key Blender claims is still consumed and the
        refusal reported (see `_MUST_CONSUME`).
        """
        candidates = keymap.session_actions_for(event)
        for action_id in candidates:
            if keymap.action_is_live(action_id):
                return self._run_bound_action(context, action_id)
        for action_id in candidates:
            if action_id in self._MUST_CONSUME:
                return self._run_bound_action(context, action_id)
        return False

    def _set_typed(self, value: str) -> None:
        # Scene state, not an attribute on this instance: the keys that clear
        # it -- U/V, N-gon mode, the span wheel -- are real operators now, and
        # an operator has no way to reach the running modal. The overlay reads
        # the same property.
        bpy.context.scene.plasticity_retop.typed_span = value

    def _flush_typed_span(self, context: bpy.types.Context) -> None:
        """Apply whatever number has been typed so far, if any."""
        state = context.scene.plasticity_retop
        if not state.typed_span:
            return
        value = int(state.typed_span)
        if value >= 1:
            setattr(state, active_span_prop(state), value)

    def _handle_typed_digit(
        self, context: bpy.types.Context, event: bpy.types.Event
    ) -> bool:
        """Digits type a span directly (a faster path than scrolling for big
        jumps); Backspace edits, Esc clears the entry. Returns True when the
        event was a numeric-entry key and has been consumed.
        """
        state = context.scene.plasticity_retop
        digit = DIGIT_KEYS.get(event.type)
        if digit is not None:
            # Cap the buffer so a stray keyboard repeat can't build an absurd
            # span and lock Blender up regenerating it.
            if len(state.typed_span) < 3:
                self._set_typed(state.typed_span + digit)
                self._flush_typed_span(context)
            return True

        if event.type == 'BACK_SPACE':
            self._set_typed(state.typed_span[:-1])
            self._flush_typed_span(context)
            return True

        return False

    def _finish(
        self, context: bpy.types.Context, report: str | None = None,
        push_undo_step: bool = True,
    ) -> set[str]:
        global _SESSION_RUNNING
        _SESSION_RUNNING = False

        if self._timer is not None:
            try:
                context.window_manager.event_timer_remove(self._timer)
            except (ValueError, ReferenceError, RuntimeError):
                pass
            self._timer = None
        if context.window:
            context.window.cursor_modal_restore()
        if context.workspace:
            context.workspace.status_text_set(None)
        overlay.disable()
        end_session(context, push=push_undo_step)
        if report:
            self.report({'INFO'}, report)
        return {'FINISHED'}

    def _reconcile_after_undo(self, context: bpy.types.Context) -> set[str] | None:
        """Catch up with what Ctrl+Z (or Ctrl+Shift+Z) just restored.

        The undo handler can only write scene properties -- it runs while
        Blender is still putting the file state back, so it must not touch a
        datablock. Everything else the step invalidated is dealt with here, on
        the first event after it: the preview mesh still holds the patch that
        was open, the hover still names a face on a mesh that may have changed
        under it, and a half-typed span belongs to a patch that is gone.

        Returns a modal return value when the session itself did not survive
        the step, else None.
        """
        state = context.scene.plasticity_retop

        # Undoing past "Retop: enter <object>" restores a file state from
        # before the session: no result mesh, no preview object, session_active
        # off. Carrying on there would leave a modal swallowing viewport events
        # for a session the panel no longer shows.
        if not state.session_active:
            # push_undo_step=False: pushing a step right after an undo throws
            # away the redo the user just made available.
            return self._finish(context, "Retop session ended by undo",
                                push_undo_step=False)

        self._clear_hover(context)  # also empties the preview object
        self._set_typed("")
        _clear_match_state(state)
        self._apply_phase_ui(context)
        return None

    def _modal_tweak(
        self, context: bpy.types.Context, event: bpy.types.Event
    ) -> set[str]:
        """Blender owns the viewport while the retopology is hand-edited.

        Everything passes through -- knife, loop cut, transform, the tool
        header, undo -- except the key that ends the round trip, which is
        dispatched here for the same reason every other session key is: its
        default is `Tab`, and `Tab` in Edit Mode belongs to
        `object.editmode_toggle` in a keymap ours would not reliably beat.
        Leaving that to chance means leaving Edit Mode *without* the repair.

        Blender leaving Edit Mode by some other route -- the mode dropdown, a
        script, an undo -- fires no event of its own, so the timer is what
        notices, and the repair happens once per trip whichever way it ended.
        """
        if context.mode == 'OBJECT':
            bpy.ops.retop.end_tweak()
            return {'RUNNING_MODAL'}

        if keymap.session_action_for(event) == "end_tweak":
            if bpy.ops.retop.end_tweak.poll():
                bpy.ops.retop.end_tweak()
                return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}

    def _in_viewport(self, context: bpy.types.Context) -> bool:
        return context.area is not None and context.area.type == 'VIEW_3D'

    def _cursor_over_viewport(
        self, context: bpy.types.Context, event: bpy.types.Event
    ) -> bool:
        """True only when the pointer is over the 3D view and clear of the
        panels floating on it."""
        region, _rv3d = viewport_region(context)
        return point_in_viewport(context.area, region, event.mouse_x, event.mouse_y)

    def modal(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        try:
            return self._modal(context, event)
        except Exception as exc:
            # Never die silently: an unhandled error used to leave the scene's
            # session_* state saying "in session" while no modal was listening,
            # so Esc and clicks did nothing at all.
            import traceback
            traceback.print_exc()
            self._finish(context)
            self.report({'ERROR'}, f"Retop session stopped: {exc}")
            return {'CANCELLED'}

    def _modal(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        global _undo_needs_reconcile

        state = context.scene.plasticity_retop

        if context.area:
            context.area.tag_redraw()

        if _undo_needs_reconcile:
            _undo_needs_reconcile = False
            finished = self._reconcile_after_undo(context)
            if finished is not None:
                return finished

        # Ending the session is the one thing an operator can ask for but not
        # do: the timer, the modal cursor and the draw handlers are this
        # instance's, and nothing else can tear them down. `retop.back` in the
        # OBJECT phase clears the flag; this is what acts on it.
        if not state.session_active:
            return self._finish(context, "Retop session ended")

        # The panel's Commit/Discard buttons clear active_face_id; when that
        # happens, drop straight back to picking the next surface so patches
        # can be retopologized one after another without relaunching.
        if state.session_phase == 'ADJUST' and state.active_face_id == -1:
            state.session_phase = 'PATCH'
            state.hovered_side = -1

        # Catch up with a phase the *keymap* changed. The session's keys are
        # real operators now, so `retop.back`, `retop.tweak_mesh` and the
        # panel's own buttons all move the phase without this instance being
        # told -- and each leaves a stale hover behind and a cursor and status
        # line describing the phase before. One check here covers every route
        # in, which is what a per-caller `_apply_phase_ui` never managed to.
        if state.session_phase != self._last_phase:
            self._last_phase = state.session_phase
            # Only on the way *out* of a patch. Entering ADJUST is the modal's
            # own click handler, and the hover it just built is the preview --
            # clearing it there would delete the geometry that was picked.
            if state.session_phase in {'PATCH', 'OBJECT'}:
                self._clear_hover(context)
            self._cursor_in_viewport = None
            self._apply_phase_ui(context)

        # Before the TIMER check, not after: leaving Edit Mode by the mode
        # dropdown fires no event of its own, so the timer is what notices.
        if state.session_phase == 'TWEAK':
            return self._modal_tweak(context, event)

        if event.type == 'TIMER':
            return {'PASS_THROUGH'}

        # Edit/Sculpt/Weight-paint mode: the session has no business in the
        # viewport there, and holding on to events would make the mode
        # unusable. Handled on the timer as well as on input, so the hand-back
        # happens the moment the mode changes rather than on the next click.
        if context.mode != 'OBJECT':
            self._leave_for_other_mode(context)
            return {'PASS_THROUGH'}

        # The cursor first, and before the area check: a modal cursor is set on
        # the whole *window*, so it has to be dropped as soon as the pointer
        # leaves the 3D view -- including for another editor entirely, which the
        # area check below returns on. viewport_region gives (None, None) there,
        # which point_in_region reads as "outside".
        over_viewport = self._cursor_over_viewport(context, event)
        self._update_cursor(context, over_viewport)
        # Where the tooltip goes. A draw handler has no event to read the
        # pointer from, so the modal leaves it where the overlay can find it --
        # the same arrangement as `overlay.hover_committed`, and cleared the
        # moment the pointer is elsewhere so a stale tooltip can't linger.
        overlay.cursor_window = ((event.mouse_x, event.mouse_y)
                                 if over_viewport else None)

        # The patch data display follows the cursor too, and its own modal is
        # starved while this one runs -- see `refresh_debug_hover`. Fed from the
        # move this modal already has, and a no-op unless that display is on.
        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            refresh_debug_hover(context, event.mouse_x, event.mouse_y)

        # Anything outside the 3D viewport (N-panel, properties, ...) must stay
        # fully interactive -- that's where spans get adjusted during ADJUST.
        if not self._in_viewport(context):
            return {'PASS_THROUGH'}

        # The N-panel lives *inside* the 3D view's area, so the check above
        # doesn't cover it: only the region test does.
        #
        # Everything passes through when the pointer is outside the 3D view --
        # every event, not a list of them. The panel has to stay fully usable
        # for as long as a session runs, and a modal that keeps *any* event
        # over it will eventually eat the one that mattered: mouse moves stop
        # buttons from highlighting, digits collide with a field being typed
        # into, Enter confirms the wrong thing. The cost is that the session's
        # keybinds need the pointer over the viewport, which is how Blender's
        # own region keymaps behave anyway.
        if not over_viewport:
            state.hovered_side = -1
            return {'PASS_THROUGH'}

        # The session's keys, resolved against the *live* KeyMapItems and run
        # from here. Dispatching rather than passing through is what makes them
        # reliable: an item in the 3D View keymap does not beat a mode keymap,
        # and `X` in Object Mode is `object.delete`. The items are still real
        # -- editable in Blender's own rows, listed in the keymap editor -- and
        # their polls still decide what a key means in this phase.
        #
        # Clicks are excluded: their meaning depends on what is under the
        # cursor, so the picker below resolves them instead.
        if event.type not in {'LEFTMOUSE', 'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            if self._dispatch_bound(context, event):
                return {'RUNNING_MODAL'}

        if state.session_phase == 'ADJUST':
            # The corner editor owns the mouse and the clicks for as long as it
            # is open, ahead of the side picker: it is reached through the
            # picker but is not part of it, and it has to work with the side
            # highlight off.
            if state.corner_edit:
                consumed = self._modal_corners(context, event)
                if consumed is not None:
                    return consumed

            # Match mode owns the mouse while it is on, so it is handled before
            # anything else in this phase.
            if state.match_mode:
                consumed = self._modal_match(context, event)
                if consumed is not None:
                    return consumed

            # Digits and Backspace are numeric entry, not a shortcut: they have
            # to stay instantaneous and they only make sense as a block, so
            # they are deliberately not remappable.
            if event.value == 'PRESS' and self._handle_typed_digit(context, event):
                return {'RUNNING_MODAL'}

            if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                # A click *with a binding on it* is a binding like any other,
                # and has to be dispatched before the fallback below can claim
                # it. Clicks are held out of the dispatch at the top of this
                # method because a plain one means "the thing under the
                # cursor", which only the picker can resolve -- but that
                # exclusion swallowed every modified click too, so Ctrl+click
                # committed the patch instead of copying a density and the
                # session left ADJUST with nothing done.
                #
                # Safe for the plain click as well: the picker above has
                # already had it, `pin_neighbour` polls on a side being under
                # the cursor, and with none there nothing runs and the fallback
                # still commits.
                if self._dispatch_bound(context, event):
                    return {'RUNNING_MODAL'}
                # Nothing else to select while adjusting a patch, so a click
                # that landed on nothing commits -- same as right-click and
                # Enter. Not a binding: it is what a click falls back to once
                # nothing else wanted it.
                self._commit(context)
                return {'RUNNING_MODAL'}

            # everything else (the session's keys, navigation, panel clicks)
            return {'PASS_THROUGH'}

        if event.type == 'MOUSEMOVE':
            if state.session_phase == 'OBJECT':
                return {'PASS_THROUGH'}

            session_obj = bpy.data.objects.get(state.session_object_name)
            obj, face_id, distance = _raycast_patch(context, event)
            # stay within the object being retopped
            if obj is not None and session_obj is not None and obj != session_obj:
                obj, face_id = None, None

            if obj is not None and face_id is not None:
                if face_id != self._hover_face_id or obj != self._hover_obj:
                    if not self._keeps_current_hover(context, event, distance):
                        if not self._set_hover(context, obj, face_id):
                            self._clear_hover(context)
            elif self._hover_face_id is not None:
                self._clear_hover(context)
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if state.session_phase == 'OBJECT':
                obj, _face_id, _distance = _raycast_patch(context, event)
                if obj is None:
                    return {'RUNNING_MODAL'}
                # Selecting and activating is `enter_session_object`'s job now,
                # and it does it on the *resolved* object -- setting it here as
                # well could activate a `<X>_Retop` the raycast handed back.
                enter_session_object(context, obj)
                self._apply_phase_ui(context)
                self.report({'INFO'}, f"Retopping {obj.name}")
                return {'RUNNING_MODAL'}

            # A click carrying a modifier is a binding -- Shift gathers the
            # surface under the cursor into the next patch -- and has to be
            # dispatched before the plain pick below claims it. Same arrangement
            # as in ADJUST, and safe for the plain click: nothing polls here.
            if self._dispatch_bound(context, event):
                return {'RUNNING_MODAL'}

            if self._hover_face_id is None:
                return {'RUNNING_MODAL'}

            # A gathered set of surfaces is opened by clicking any one of
            # them -- the selection is the gesture, and needing a second key to
            # confirm it would make the first click mean nothing on its own.
            # Clicking outside it opens that surface instead and drops the
            # selection, which is what abandoning it looks like.
            selection = surface_selection(state)
            if selection:
                if len(selection) > 1 and self._hover_face_id in selection:
                    # The selection is only dropped once the patch has opened:
                    # a refusal leaves it on screen to be adjusted, rather than
                    # making the user pick all of it again.
                    if self._open_composite(context, selection):
                        set_surface_selection(state, [])
                    return {'RUNNING_MODAL'}
                set_surface_selection(state, [])

            state.active_face_id = self._hover_face_id
            state.generator_name = self._hover_generator_name
            state.num_sides = self._hover_num_sides
            state.num_loops = self._hover_num_loops
            state.source_object_name = self._hover_obj.name
            state.span_u, state.span_v, state.span = self._hover_spans
            # Per-side matches belong to the patch they were picked on.
            state.side_overrides = ""
            state.hovered_side = -1
            # Re-editing a patch committed as an n-gon puts the session back
            # into n-gon mode, so panel and keybinds describe what is on screen.
            state.ngon_mode = self._hover_ngon
            state.editing_committed = self._hover_committed
            state.session_phase = 'ADJUST'
            self._set_typed("")
            self._apply_phase_ui(context)

            if self._hover_committed:
                # The hover preview already holds the regenerated grid, so the
                # old geometry can go now -- no need to rebuild anything.
                removed = begin_reedit(context, self._hover_obj, self._hover_face_id)
                self.report(
                    {'INFO'} if removed else {'WARNING'},
                    f"Re-editing patch {self._hover_face_id} "
                    f"({self._hover_generator_name}) — removed {removed} old face(s)")
            else:
                self.report({'INFO'},
                            f"Patch {self._hover_face_id} ({self._hover_generator_name})")
            return {'RUNNING_MODAL'}

        # let viewport navigation (orbit/pan/zoom) and the session's own
        # KeyMapItems through untouched
        return {'PASS_THROUGH'}

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        global _SESSION_RUNNING

        state = context.scene.plasticity_retop
        self._hover_obj = None
        self._hover_face_id = None
        self._hover_committed = False
        self._cursor_in_viewport = None

        # Clear anything a previous, interrupted session left behind.
        end_session(context)

        _SESSION_RUNNING = True
        state.session_active = True
        state.active_face_id = -1

        # Skip the object-picking step when the active object is already a
        # Plasticity mesh -- that's the common case after importing.
        active = resolve_session_object(context.active_object)
        if _is_plasticity_mesh(active):
            enter_session_object(context, active)
        else:
            state.session_object_name = ""
            state.session_phase = 'OBJECT'

        self._apply_phase_ui(context)
        overlay.enable()
        self._timer = context.window_manager.event_timer_add(0.1, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}


class RETOP_OT_end_session(bpy.types.Operator):
    bl_idname = "retop.end_session"
    bl_label = "Stop Retop Session"
    bl_description = ("End the retop session and clear its state. Also use this to recover if the "
                       "panel still shows a session but the viewport no longer responds to clicks "
                       "or Esc (e.g. after reloading the addon mid-session)")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.scene.plasticity_retop.session_active

    def execute(self, context: bpy.types.Context) -> set[str]:
        overlay.disable()
        end_session(context)
        if context.workspace:
            context.workspace.status_text_set(None)
        self.report({'INFO'}, "Retop session stopped")
        return {'FINISHED'}


class RETOP_OT_commit_patch(bpy.types.Operator):
    bl_idname = "retop.commit_patch"
    bl_label = "Commit Patch"
    bl_description = "Bake the current preview into the source object's retopology result mesh"
    # No 'UNDO': the step is pushed by hand at the end of execute() instead.
    # Blender would push one automatically, but only when the operator is run
    # from the UI -- and the common path is bpy.ops from inside the session
    # modal, which is exactly where the step was missing. Pushing it here
    # covers both, and one flag *plus* one explicit push would leave two
    # identical states on the stack, i.e. a Ctrl+Z that appears to do nothing.
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        state = context.scene.plasticity_retop
        return (not state.corner_edit and mesh_build.has_preview()
                and state.source_object_name in bpy.data.objects)

    def execute(self, context: bpy.types.Context) -> set[str]:
        state = context.scene.plasticity_retop
        # A number still being typed is part of the patch: applying it here
        # rather than in the caller means the panel's Commit button honours it
        # too, which it never did.
        if state.typed_span:
            value = int(state.typed_span)
            if value >= 1:
                setattr(state, active_span_prop(state), value)
            state.typed_span = ""

        source_obj = bpy.data.objects[state.source_object_name]
        face_id = state.active_face_id
        replacing = state.editing_committed

        # Recompute this patch's corner ids so their spans can be registered
        # for propagation to future neighboring patches (cheap: same lookup
        # generation already does, just no need to regenerate geometry here).
        # Same corner method the preview was generated with, or the spans
        # registered for propagation would describe sides that don't exist.
        prepared = patchprep.prepare_patch(
            source_obj.data, face_id, state.corner_angle_threshold,
            state.small_side_tolerance,
            state.corner_method_ngon if state.generator_name == generators.NGON.name
            else state.corner_method_spans)

        # Passing the face id is what lets a re-committed patch replace its own
        # previous faces instead of stacking a second grid on top of them.
        result_obj, error = mesh_build.commit_preview_to_result(context, source_obj, face_id=face_id)
        if error:
            self.report({'WARNING'}, error)
            return {'CANCELLED'}

        # Bookkeeping first, propagation second. The geometry is already in the
        # result mesh at this point, so anything that throws from here on leaves
        # the commit half-done -- and of the three, only these two matter for
        # correctness: without them the re-edit snapshot stays alive and the
        # panel's patch count goes stale. Span registration is additive.
        keep_reedit_removal(context)
        update_committed_count(context, source_obj)

        mesh_build.register_patch_settings(
            source_obj, face_id, state.span_u, state.span_v, state.span,
            state.generator_name, state.side_groups)
        if prepared is not None:
            register_spans_for(context, source_obj, prepared)

        # Note: the result highlight is owned by the session (it stays on for
        # as long as you're inside this object), so it is deliberately NOT
        # turned off here. Clearing active_face_id is what tells a running
        # session to go back to picking the next surface.
        state.active_face_id = -1
        state.generator_name = ""
        state.num_sides = 0

        verb = "Replaced" if replacing else "Committed"
        # One undo step per committed patch: that is what makes Ctrl+Z peel the
        # last patch off instead of rolling the whole session back. Without it
        # the nearest step below is "Retop: enter <obj>", so a single Ctrl+Z
        # went to the state *before* the session -- every patch gone at once.
        # It is also the step that owns the snapshot datablock
        # keep_reedit_removal just freed: freeing an ID between two undo steps
        # is what makes Ctrl+Z crash the depsgraph.
        push_undo(f"Retop: {verb.lower()} patch {face_id}")
        self.report({'INFO'}, f"{verb} patch {face_id} in {result_obj.name}")
        return {'FINISHED'}


class RETOP_OT_clear_preview(bpy.types.Operator):
    bl_idname = "retop.clear_preview"
    bl_label = "Clear Preview"
    bl_description = "Discard the current preview without committing it"
    bl_options = {'REGISTER'}  # only a restored re-edit pushes a step; see execute

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        state = context.scene.plasticity_retop
        # Also available with an empty preview while a re-edit is open, so its
        # removal can still be rolled back.
        return mesh_build.has_preview() or state.editing_committed

    def execute(self, context: bpy.types.Context) -> set[str]:
        mesh_build.clear_preview_object()
        state = context.scene.plasticity_retop
        restoring = bool(state.reedit_backup_mesh)
        # Discarding a re-edit puts the patch that was taken out on pick back
        # exactly as it was, so Esc can never lose committed topology.
        restore_reedit_removal(context)
        if restoring:
            # That put geometry back into the result mesh and freed the
            # snapshot datablock, so it gets its own step -- for the same
            # reason commit does. A discard with nothing to restore changed no
            # datablock at all and would only add a Ctrl+Z that does nothing.
            push_undo("Retop: discard re-edit")
        # Highlight stays on while the session is inside this object; clearing
        # active_face_id sends a running session back to picking a surface.
        state.active_face_id = -1
        state.generator_name = ""
        state.num_sides = 0
        return {'FINISHED'}


class RETOP_OT_delete_patch(bpy.types.Operator):
    bl_idname = "retop.delete_patch"
    bl_label = "Delete Patch"
    bl_description = ("Remove this patch's retopology for good instead of replacing it. "
                      "Only its own faces go: vertices a neighbouring patch still uses "
                      "survive, so the patches around it keep their welds")
    bl_options = {'REGISTER'}  # its undo step is pushed by hand, as for commit

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        state = context.scene.plasticity_retop
        # Only during a re-edit: picking a committed patch is what takes its
        # faces out, and deleting is simply choosing not to put anything back.
        return (state.editing_committed and not state.corner_edit
                and state.source_object_name in bpy.data.objects)

    def execute(self, context: bpy.types.Context) -> set[str]:
        state = context.scene.plasticity_retop
        source_obj = bpy.data.objects[state.source_object_name]
        face_id = state.active_face_id
        removed = state.reedit_removed_faces

        # The faces went when the patch was picked; deleting is keeping that
        # removal and committing nothing in their place. Dropping the snapshot
        # is what makes it permanent.
        keep_reedit_removal(context)
        mesh_build.clear_preview_object()
        mesh_build.forget_patch_settings(source_obj, face_id)
        update_committed_count(context, source_obj)

        # Creases are a property of the border *between* patches, so losing one
        # changes the shading of edges that belong to its neighbours.
        result_obj = bpy.data.objects.get(mesh_build.result_object_name_for(source_obj))
        if result_obj is not None:
            mesh_build.apply_result_shading(context, result_obj)

        state.active_face_id = -1
        state.generator_name = ""
        state.num_sides = 0

        # Same reasoning as commit: one step per change to the result mesh, and
        # the snapshot keep_reedit_removal freed has to belong to one.
        push_undo(f"Retop: delete patch {face_id}")
        self.report({'INFO'}, f"Deleted patch {face_id} ({removed} face(s))")
        return {'FINISHED'}


class RETOP_OT_pin_side(bpy.types.Operator):
    """Pin the side under the cursor to the vertices it should reproduce.

    A click follows the committed neighbour across the side; a side with
    nothing committed across it has nothing to follow and is refused. The modal
    hands the click over whenever a side is actually under the cursor -- the
    *fallback* (nothing under it, so commit) is what depends on the hover, not
    this.
    """
    bl_idname = "retop.pin_side"
    bl_label = "Pin Side"
    bl_description = "Match the side under the cursor to the committed neighbour across it"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        state = context.scene.plasticity_retop
        return (state.session_active and state.session_phase == 'ADJUST'
                and not state.corner_edit
                and state.match_mode and state.hovered_side != -1)

    def execute(self, context: bpy.types.Context) -> set[str]:
        state = context.scene.plasticity_retop
        adopted = adopt_side_reference(context, state.hovered_side)
        if adopted is None:
            references = sidematch.active_sides()
            reason = (references[state.hovered_side].reason
                      if 0 <= state.hovered_side < len(references) else "")
            self.report({'WARNING'},
                        f"Can't match this side: {reason or 'nothing committed along it'}")
            return {'CANCELLED'}
        self.report({'INFO'}, _match_report(state, adopted))
        return {'FINISHED'}


class RETOP_OT_edit_corners(bpy.types.Operator):
    """Open the corner editor on the patch being adjusted.

    Ctrl+click, sharing its binding with `retop.copy_patch_spans` and separated
    from it by what is under the cursor -- a side of this patch opens the
    editor, anything else copies a density. The same split the plain click
    already makes between matching a side and committing, and safe for the same
    reason: neither branch destroys anything, so a miss by a few pixels costs
    one re-click.
    """
    bl_idname = "retop.edit_corners"
    bl_label = "Edit Corners"
    bl_description = ("Choose which of this patch's corners are side boundaries. Turning one off "
                      "merges the two sides meeting there into one, which is what turns a "
                      "five-sided face into the quad it usually wants to be")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        state = context.scene.plasticity_retop
        return (state.session_active and state.session_phase == 'ADJUST'
                and not state.corner_edit and state.active_face_id != -1
                and state.hovered_side != -1)

    def execute(self, context: bpy.types.Context) -> set[str]:
        state = context.scene.plasticity_retop
        references = sidematch.active_sides()
        if len(references) < 3:
            self.report({'WARNING'},
                        "Nothing to choose: this patch has fewer than three corners")
            return {'CANCELLED'}
        # What Esc puts back. The edit is several clicks long, so it owes a way
        # out that neither commits the patch nor keeps a half-made corner set.
        state.side_groups_backup = state.side_groups
        state.corner_edit = True
        state.hovered_bubble = -1
        refresh_group_warning(context)
        self.report({'INFO'},
                    "Group editor: click a side's number to step it, Ctrl+click to step it "
                    "back, Enter to keep")
        return {'FINISHED'}


class RETOP_OT_toggle_corner(bpy.types.Operator):
    """Step the group number of the side under the cursor.

    **Free, and checked rather than constrained.** This used to merge the side
    into the group before it or split it back out -- always valid by
    construction, and unpredictable to use: nothing on screen said whether the
    next click would open a new group or join an existing one, so the one thing
    a bubble is for (reading the state before acting on it) did not extend to
    the click. Now the number is simply stepped, every value is reachable, and
    a grouping that cannot work is reported by `sidematch.group_problems` with
    the offending bubbles ringed.

    Wraps rather than clamping, and the ceiling is the loop's own side count:
    more groups than sides is not something a boundary can be cut into, and a
    click that does nothing at the end of the range reads as a broken control.
    """
    bl_idname = "retop.toggle_corner"
    bl_label = "Change Side Group"
    bl_description = ("Step the group number of the side under the cursor. A group becomes one "
                      "side of the patch, so it has to be a connected run of the boundary -- "
                      "the viewport says so when it is not")
    bl_options = {'REGISTER'}

    delta: bpy.props.IntProperty(name="Step", default=1)

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        state = context.scene.plasticity_retop
        return (state.session_active and state.corner_edit
                and state.hovered_bubble != -1)

    def execute(self, context: bpy.types.Context) -> set[str]:
        state = context.scene.plasticity_retop
        references = sidematch.active_sides()
        index = state.hovered_bubble
        if not (0 <= index < len(references)):
            return {'CANCELLED'}

        numbers = sidematch.group_numbers(references, state)
        ceiling = max(1, sidematch.group_count_for(references, references[index].loop))
        stepped = (numbers.get(index, 1) - 1 + (self.delta or 1)) % ceiling + 1

        stored = sidematch.side_groups(state)
        stored[index] = stepped
        sidematch.set_side_groups(state, stored)

        refresh_group_warning(context)
        counts = sidematch.loop_group_counts(
            references, sidematch.group_numbers(references, state))
        self.report({'INFO'}, state.group_warning or _corner_report(counts))
        return {'FINISHED'}


def refresh_group_warning(context: bpy.types.Context) -> None:
    """Re-read what is wrong with the grouping, for the panel and the overlay.

    Written to the scene rather than recomputed at draw time: a draw handler
    runs on every redraw, and the panel and the viewport have to agree about
    one grouping rather than each reaching their own conclusion.
    """
    state = context.scene.plasticity_retop
    references = sidematch.active_sides()
    if not references:
        state.group_warning = ""
        return
    _at_fault, message = sidematch.group_problems(
        references, sidematch.group_numbers(references, state))
    state.group_warning = message


def _corner_report(counts: dict[int, int]) -> str:
    """What the grouping now amounts to -- the generator it would pick.

    The count is the whole point of the gesture: it is what `find_generator`
    reads, so saying "4 groups" without saying "Quad" would report the input
    and withhold the answer.
    """
    if len(counts) == 1:
        groups = next(iter(counts.values()))
        generator = generators.find_generator(groups)
        name = generator.name if generator is not None else "no generator"
        return f"{groups} groups -> {name}"
    return " + ".join(f"loop {loop}: {groups} groups"
                      for loop, groups in sorted(counts.items()))


class RETOP_OT_corners_accept(bpy.types.Operator):
    """Keep the corner set and go back to adjusting the patch."""
    bl_idname = "retop.corners_accept"
    bl_label = "Keep Corner Set"
    bl_description = "Close the corner editor, keeping the corners as they are now"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        state = context.scene.plasticity_retop
        return state.session_active and state.corner_edit

    def execute(self, context: bpy.types.Context) -> set[str]:
        state = context.scene.plasticity_retop
        # The warning outlives the editor on purpose: an invalid grouping is
        # kept rather than refused, so the panel has to go on saying it cannot
        # be built. Refusing to close would be the modal nobody can get out of.
        _close_corner_editor(state)
        regenerate_active_preview(context)
        if state.group_warning:
            self.report({'WARNING'}, state.group_warning)
        return {'FINISHED'}


class RETOP_OT_corners_cancel(bpy.types.Operator):
    """Put the corner set back as it was and close the editor."""
    bl_idname = "retop.corners_cancel"
    bl_label = "Cancel Corner Edit"
    bl_description = "Close the corner editor, restoring the corners it was opened with"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        state = context.scene.plasticity_retop
        return state.session_active and state.corner_edit

    def execute(self, context: bpy.types.Context) -> set[str]:
        state = context.scene.plasticity_retop
        state.side_groups = state.side_groups_backup
        _close_corner_editor(state)
        refresh_group_warning(context)
        regenerate_active_preview(context)
        return {'FINISHED'}


def _close_corner_editor(state: "state_mod.RetopPatchState") -> None:
    state.corner_edit = False
    state.side_groups_backup = ""
    state.hovered_bubble = -1


def _copy_source_under_cursor(
    context: bpy.types.Context, event: bpy.types.Event
) -> int:
    """The committed patch the cursor is over, or -1.

    What the overlay outlines to say "this one has a density you can take". A
    raycast per mouse move, which the PATCH phase already pays for and which
    nothing here caches: it is the *outline* that would be expensive, and that
    comes from `cad_display`, cached per mesh like everything else a draw
    handler reads.

    Only ever a different patch from the one being adjusted, and only one that
    has been committed -- whether its generator agrees is left to the overlay,
    which has room to say why not.
    """
    state = context.scene.plasticity_retop
    session_obj = bpy.data.objects.get(state.session_object_name)
    if session_obj is None:
        return -1

    obj, face_id, _distance = _raycast_patch(context, event)
    if obj is None or obj != session_obj or face_id is None:
        return -1
    if face_id == state.active_face_id:
        return -1
    stored = mesh_build.lookup_patch_settings(session_obj, face_id)
    return face_id if stored else -1


def copy_spans_from(
    context: bpy.types.Context, face_id: int
) -> tuple[bool, str]:
    """Give the patch being adjusted the spans `face_id` was committed with.

    The generator each patch was built by is recorded at commit
    (`mesh_build.register_patch_settings`), which is what makes this answerable
    at all: a Ring's two counts mean *around* and *across*, an N-Side's single
    one means segments per side, and a number copied between two different
    generators would be a number with a different meaning. So the spans travel
    only between patches built the same way, and the refusal names both.

    Returns (done, message) -- the message is worth saying either way, since
    "nothing happened" is what a silently refused click looks like.
    """
    state = context.scene.plasticity_retop
    if face_id == state.active_face_id:
        return False, "that is this patch"

    obj = bpy.data.objects.get(state.session_object_name)
    if obj is None:
        return False, "no session object"

    stored = mesh_build.lookup_patch_settings(obj, face_id)
    if not stored:
        return False, f"patch {face_id} has not been committed yet"

    source_generator = stored.get("generator") or ""
    if source_generator != state.generator_name:
        return False, (f"patch {face_id} is a {source_generator or 'different'}, "
                       f"this one is a {state.generator_name or 'different patch'}")
    # The viewport promised this before the click; `copy_source_status` is what
    # it promised with, and it reads the same record.


    # **Clicking the same patch again exchanges the two spans.** Which of a
    # quad's directions is U comes from where its boundary walk started, and
    # nothing in either patch says how one's U relates to the other's -- so a
    # copy lands rotated about half the time and there is no way to work out in
    # advance which half. A second click is that answer: one gesture, two
    # states, the same shape as clicking a matched side to release it. A ring
    # never needs it (around is around), and a single-span generator has
    # nothing to exchange, which is said rather than silently ignored.
    swap = (face_id == state.copy_source_face_id) and not state.copy_source_swapped
    two_spans = source_generator in constants.TWO_SPAN_GENERATORS
    if swap and not two_spans:
        return False, f"a {source_generator} has one span, so there is nothing to swap"

    values = {key: stored.get(key) for key in ("span_u", "span_v", "span")}
    if swap:
        values["span_u"], values["span_v"] = values["span_v"], values["span_u"]

    # Every span in the record, exactly as a re-edit restores them: which of
    # them the generator reads is the generator's business, and the ones it
    # does not read cost nothing.
    changed = []
    for key in ("span_u", "span_v", "span"):
        value = values.get(key)
        if not (isinstance(value, int) and value >= 1):
            continue
        if getattr(state, key, None) == value:
            continue
        # Assigning fires the property's update callback, which regenerates the
        # preview -- the same path the wheel takes.
        setattr(state, key, value)
        changed.append(f"{key[-1].upper()}={value}" if key != "span" else f"span={value}")

    state.copy_source_face_id = face_id
    state.copy_source_swapped = swap

    swapped_note = " (swapped)" if swap else ""
    if not changed:
        return True, f"already the same density as patch {face_id}{swapped_note}"
    return True, (f"copied from patch {face_id}{swapped_note}: " + ", ".join(changed))


class RETOP_OT_copy_patch_spans(bpy.types.Operator):
    """Copy the density of another committed patch onto the one being adjusted.

    Point at finished retopology and take its spans. Propagation already does
    this across a *shared boundary*, where the two patches have to agree or
    they crack; this is the other half -- two patches that never touch, a ring
    here and a ring there, which want the same density for no reason the mesh
    can work out on its own.
    """
    bl_idname = "retop.copy_patch_spans"
    bl_label = "Copy Patch Density"
    bl_description = ("Copy the spans of the committed patch under the cursor onto the patch "
                      "being adjusted. Only between patches built by the same generator: a "
                      "Ring's counts mean around and across, an N-Side's means segments per "
                      "side, and the same number means something else in each")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        state = context.scene.plasticity_retop
        # Ctrl+click is split by what is under the cursor, exactly as the
        # plain click is: a *side* of the patch being adjusted opens the corner
        # editor (`retop.edit_corners`, declared first), anything else copies
        # the density of the patch the ray finds. So this one stands down
        # whenever a side is hovered -- without which the editor could never be
        # reached, since both are the same event.
        return (state.session_active and state.session_phase == 'ADJUST'
                and not state.corner_edit and state.hovered_side == -1
                and state.active_face_id != -1)

    def execute(self, context: bpy.types.Context) -> set[str]:
        # The pointer the modal left behind: an operator it dispatches never
        # sees the event, the same arrangement the side tooltip uses.
        cursor = overlay.cursor_window
        if cursor is None:
            self.report({'WARNING'}, "Cursor is not over the viewport")
            return {'CANCELLED'}

        origin, direction = ray_from_window(context, cursor[0], cursor[1])
        if origin is None:
            self.report({'WARNING'}, "Cursor is not over the viewport")
            return {'CANCELLED'}

        _obj, face_id, _distance = _raycast_patch_ray(
            context, origin, direction, space=context.space_data)
        if face_id is None:
            self.report({'WARNING'}, "No patch under the cursor to copy from")
            return {'CANCELLED'}

        done, message = copy_spans_from(context, face_id)
        self.report({'INFO'} if done else {'WARNING'},
                    message if done else f"Can't copy density: {message}")
        return {'FINISHED'} if done else {'CANCELLED'}


class RETOP_OT_toggle_surface(bpy.types.Operator):
    """Gather the surface under the cursor into the next patch, or drop it.

    One Plasticity face is one patch, which is the input contract and not
    always the right unit of work: a boss with two fillet rings around it is
    five surfaces that want a single sheet of retopology across them.
    Shift+click marks the ones that should be filled together; clicking any of
    them opens them as one patch, with the borders between them gone.
    """
    bl_idname = "retop.toggle_surface"
    bl_label = "Add Surface To Patch"
    bl_description = ("Add the surface under the cursor to the ones the next patch will be "
                      "built from, or take it back out. Click any selected surface to open "
                      "them as one patch")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _in_phase(context, 'PATCH')

    def execute(self, context: bpy.types.Context) -> set[str]:
        state = context.scene.plasticity_retop
        obj = bpy.data.objects.get(state.session_object_name)
        if obj is None:
            return {'CANCELLED'}

        # The pointer the modal left behind: an operator it dispatches never
        # gets the event, the same arrangement `copy_patch_spans` has.
        cursor = overlay.cursor_window
        origin, direction = (ray_from_window(context, cursor[0], cursor[1])
                             if cursor is not None else (None, None))
        if origin is None:
            self.report({'WARNING'}, "Cursor is not over the viewport")
            return {'CANCELLED'}

        hit_obj, face_id, _distance = _raycast_patch_ray(
            context, origin, direction, space=context.space_data)
        if face_id is None or hit_obj != obj:
            self.report({'WARNING'}, "No surface of this object under the cursor")
            return {'CANCELLED'}

        changed, message = toggle_patch_surface(context, obj, face_id)
        self.report({'INFO'} if changed else {'WARNING'},
                    message if changed else f"Can't add that surface: {message}")
        return {'FINISHED'} if changed else {'CANCELLED'}


class RETOP_OT_split_patch(bpy.types.Operator):
    """Take the active patch back apart into the CAD surfaces it was built
    from. The way back, and the reason building one is not destructive.
    """
    bl_idname = "retop.split_patch"
    bl_label = "Split Into Surfaces"
    bl_description = ("Take this patch back apart into the Plasticity surfaces it was built "
                      "from, each becoming a patch of its own again")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        state = context.scene.plasticity_retop
        return (state.session_active
                and state.active_face_id <= patch_data.COMPOSITE_ID_BASE)

    def execute(self, context: bpy.types.Context) -> set[str]:
        state = context.scene.plasticity_retop
        obj = bpy.data.objects.get(state.source_object_name
                                   or state.session_object_name)
        if obj is None:
            return {'CANCELLED'}

        done, message = split_composite(context, obj, state.active_face_id)
        if not done:
            self.report({'WARNING'}, f"Can't split this patch: {message}")
            return {'CANCELLED'}

        # The patch it was open on no longer exists, so the session goes back
        # to picking one -- which is what clearing active_face_id tells the
        # modal to do.
        if bpy.ops.retop.clear_preview.poll():
            bpy.ops.retop.clear_preview()
        state.active_face_id = -1
        state.session_phase = 'PATCH'
        push_undo("Retop: split patch into surfaces")
        self.report({'INFO'}, message)
        return {'FINISHED'}


class RETOP_OT_tweak_mesh(bpy.types.Operator):
    """Hand the result mesh to Blender's Edit Mode, set up for retopology.

    The session keeps running throughout -- the modal simply passes everything
    through until Tab (or the mode dropdown) brings the viewport back. See
    tweak.py for why this is a round trip into Blender rather than a pair of
    object-mode tools.
    """
    bl_idname = "retop.tweak_mesh"
    bl_label = "Hand-Edit Retopology"
    bl_description = ("Open <Object>_Retop in Blender's Edit Mode with vertex snapping and "
                       "auto-merge already set up, to fix by hand what the generators got wrong: "
                       "K knife, Ctrl+R loop cut, J connect vertices, G to drag a vertex onto its "
                       "twin. Tab comes back to the session")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return tweak.can_tweak(context) is None

    def execute(self, context: bpy.types.Context) -> set[str]:
        error = tweak.enter_tweak(context)
        if error:
            self.report({'WARNING'}, error)
            return {'CANCELLED'}
        self.report({'INFO'},
                    "Hand-edit: K knife, Ctrl+R loop cut, J connect, G move (snapped), "
                    "Tab back to Retop")
        return {'FINISHED'}


class RETOP_OT_end_tweak(bpy.types.Operator):
    """Take the viewport back from Edit Mode and reconcile the hand edits.

    The repair is the whole reason this is an operator rather than a plain
    `object.mode_set`: Blender knows nothing about `retop_patch_face_id` or
    `retop_source_vid`, so a knife cut leaves faces no patch owns and vertices
    claiming to be CAD corners they are nowhere near. See
    `mesh_build.repair_manual_edits`.
    """
    bl_idname = "retop.end_tweak"
    bl_label = "Back to Retop"
    bl_description = ("Leave Edit Mode, put the snapping settings back as they were, and let the "
                       "addon re-adopt the faces and drop the stray corner ids the hand edits "
                       "left behind")
    # REGISTER without UNDO, like commit and delete: the step below is pushed
    # by hand, and Blender pushing one of its own for an OPTYPE_UNDO operator
    # run from the UI would put two identical states on the stack.
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.scene.plasticity_retop.session_phase == 'TWEAK'

    def execute(self, context: bpy.types.Context) -> set[str]:
        adopted, cleared = tweak.exit_tweak(context)
        state = context.scene.plasticity_retop
        update_committed_count(
            context, bpy.data.objects.get(state.session_object_name))
        # The repair writes to the result mesh, so it gets its own step -- same
        # rule as a commit or a delete. Blender pushed one for leaving Edit
        # Mode, but that one predates the attributes fixed above.
        push_undo("Retop: hand edits")
        if adopted or cleared:
            self.report({'INFO'},
                        f"Back in Retop — {adopted} new face(s) tracked, "
                        f"{cleared} stray corner id(s) cleared")
        else:
            self.report({'INFO'}, "Back in Retop")
        return {'FINISHED'}


def _global_keys_live(context: bpy.types.Context) -> bool:
    """Whether the addon's GLOBAL keys mean anything right now.

    They are dispatched by Blender rather than by the modal, so unlike the
    session's keys they are offered on every press from the moment the addon is
    installed -- and '/' , Alt+X and V are keys other addons want too
    (Hard Ops' own Alt+X is where this one's was borrowed from). Claiming a key
    an addon is not currently being used for is how an addon ends up having to
    be disabled to get its keys back, so by default these polls fail with no
    session open and Blender hands the event on to whoever else bound it.

    `keymap.global_keys_outside_session` is the way back for anyone who wants
    the isolate and the mirror between sessions. Panel buttons are unaffected:
    they call `retop.mirror_axis` and `retop.apply_mirror`, which are not bound
    to anything and stay polled on having a result mesh.
    """
    if keymap.global_keys_outside_session():
        return True
    return bool(context.scene.plasticity_retop.session_active)


class RETOP_OT_mirror_axis(bpy.types.Operator):
    """Turn the retopology's mirror on or off for one axis.

    Symmetry is a Mirror modifier on `<Object>_Retop`, planed on the *source*
    object's origin -- see mesh_build's symmetry section for why a modifier and
    not baked geometry.
    """
    bl_idname = "retop.mirror_axis"
    bl_label = "Mirror Axis"
    bl_description = ("Mirror the retopology across this axis of the source object's origin. "
                       "Press again to turn it off. The mirrored half is a modifier, not real "
                       "geometry: it can't be picked or re-edited until you apply it")
    bl_options = {'REGISTER'}

    axis: bpy.props.EnumProperty(
        name="Axis",
        items=[(a, a, f"Mirror across the {a} axis") for a in mesh_build.MIRROR_AXES],
        default='X',
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        _source, result = mesh_build.mirror_target(context)
        return result is not None

    def execute(self, context: bpy.types.Context) -> set[str]:
        source, result = mesh_build.mirror_target(context)
        if source is None:
            self.report({'WARNING'}, "No Plasticity object to mirror")
            return {'CANCELLED'}
        if result is None:
            self.report({'WARNING'},
                        f"Nothing to mirror yet: '{source.name}' has no committed patch")
            return {'CANCELLED'}

        axes = mesh_build.toggle_mirror_axis(context, source, result, self.axis)
        on = [a for a, enabled in zip(mesh_build.MIRROR_AXES, axes) if enabled]
        push_undo(f"Retop: mirror {self.axis}")
        self.report({'INFO'},
                    f"Mirror: {' + '.join(on)}" if on else "Mirror off")
        return {'FINISHED'}


def _tag_viewports_redraw(context: bpy.types.Context) -> None:
    """Redraw every 3D view, so a POST_PIXEL overlay follows the pointer.

    Every viewport rather than `context.area`: a modal's context area is
    whichever one the operator was invoked from, and the gizmo is drawn in
    whichever region the pointer is actually over.
    """
    window = getattr(context, "window", None)
    screen = getattr(window, "screen", None) if window else None
    for area in (screen.areas if screen else ()):
        if area.type == 'VIEW_3D':
            area.tag_redraw()


class RETOP_OT_mirror(bpy.types.Operator):
    """Alt+X, then X / Y / Z: arm the axis prompt and toggle what it picks.

    Modelled on Hard Ops, and a modal rather than three bindings because that
    is the reflex the key is borrowed from. It sits *above* the session modal
    while it runs, so the axis keys can't collide with the session's own X
    (delete patch) -- this operator sees them first.
    """
    bl_idname = "retop.mirror"
    bl_label = "Mirror Retopology"
    bl_description = ("Mirror the retopology across the source object's origin: press Alt+X, then "
                       "X, Y or Z to toggle that axis. Esc cancels")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if not _global_keys_live(context):
            return False  # Alt+X goes back to Hard Ops, or to whoever else has it
        _source, result = mesh_build.mirror_target(context)
        return result is not None

    def _restore_status(self, context: bpy.types.Context) -> None:
        """Give the status bar back to whoever had it.

        The session writes its phase there and only rewrites it on a phase
        change, so clearing the line unconditionally would leave the session
        running with a blank status until the next transition.
        """
        if context.workspace is None:
            return
        state = context.scene.plasticity_retop
        if state.session_active and session_is_running():
            _cursor, status = RETOP_OT_session._PHASE_UI.get(
                state.session_phase, ('DEFAULT', ""))
            context.workspace.status_text_set(f"Retop — {status}")
        else:
            context.workspace.status_text_set(None)

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        source, result = mesh_build.mirror_target(context)
        if result is None:
            self.report({'WARNING'},
                        f"Nothing to mirror yet: '{source.name}' has no committed patch"
                        if source is not None else "No Plasticity object to mirror")
            return {'CANCELLED'}

        axes = mesh_build.mirror_axes(result)
        on = [a for a, enabled in zip(mesh_build.MIRROR_AXES, axes) if enabled]
        if context.workspace:
            context.workspace.status_text_set(
                f"Mirror {result.name}   |   X / Y / Z: toggle an axis   |   Esc: cancel"
                + (f"   |   now: {' + '.join(on)}" if on else "   |   now: off"))
        # And under the cursor as well, where the question was asked. The status
        # bar is at the bottom of the window: with a hand on the keyboard and
        # eyes on the part, it is the one place nobody looks. The gizmo also
        # says which axes are *already* mirrored, which the key alone cannot --
        # that state lives on the modifier.
        overlay.mirror_state = tuple(axes)
        overlay.mirror_cursor = (event.mouse_x, event.mouse_y)
        overlay.enable_mirror_gizmo()
        _tag_viewports_redraw(context)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        # Alt is still held from the binding that got us here, so its release
        # arrives first; the same goes for any other modifier the user lets go
        # of. Cancelling on those would make the prompt impossible to reach.
        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            # The gizmo follows the pointer: it is drawn where the question is
            # being asked, and a picker that stays behind reads as stale UI.
            overlay.mirror_cursor = (event.mouse_x, event.mouse_y)
            _tag_viewports_redraw(context)
            return {'RUNNING_MODAL'}
        if event.value != 'PRESS' or event.type in {
                'TIMER',
                'LEFT_ALT', 'RIGHT_ALT', 'LEFT_SHIFT', 'RIGHT_SHIFT',
                'LEFT_CTRL', 'RIGHT_CTRL', 'OSKEY'}:
            return {'RUNNING_MODAL'}

        if event.type in mesh_build.MIRROR_AXES:
            axis = event.type
            self._finish(context)
            bpy.ops.retop.mirror_axis(axis=axis)
            return {'FINISHED'}

        # Anything else cancels rather than being swallowed: an armed prompt
        # nobody can get out of is worse than one that gives up easily.
        self._finish(context)
        if event.type not in {'ESC', 'RIGHTMOUSE'}:
            self.report({'INFO'}, "Mirror cancelled — X, Y or Z picks an axis")
        return {'CANCELLED'}

    def _finish(self, context: bpy.types.Context) -> None:
        """Every exit goes through here: a draw handler nobody owns is the one
        kind of leak the version string cannot report."""
        overlay.disable_mirror_gizmo()
        _tag_viewports_redraw(context)
        self._restore_status(context)


class RETOP_OT_reset_corner_methods(bpy.types.Operator):
    """Put both corner-detection methods back on their defaults.

    The two enums are a developer's knob (see the panel), so the rows that edit
    them are hidden unless Developer Mode is on -- but the values live on the
    scene, so a file saved with a non-default would otherwise be stuck on it
    with no control anywhere to reach. This is the way back, and the panel only
    offers it when there is something to undo.
    """
    bl_idname = "retop.reset_corner_methods"
    bl_label = "Reset Corner Detection"
    bl_description = ("Put corner detection back to Angle for grid generators and Both for n-gon "
                      "mode -- the defaults, which are what the fixture measures as the better "
                      "answer on every shape where the choice makes any difference at all")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context: bpy.types.Context) -> set[str]:
        state = context.scene.plasticity_retop
        state.corner_method_spans = 'ANGLE'
        state.corner_method_ngon = 'BOTH'
        self.report({'INFO'}, "Corner detection: Angle for grids, Both for n-gons")
        return {'FINISHED'}


class RETOP_OT_apply_mirror(bpy.types.Operator):
    """Bake the mirror into real geometry, mirrored faces left untracked."""
    bl_idname = "retop.apply_mirror"
    bl_label = "Apply Mirror"
    bl_description = ("Turn the mirrored half into real geometry. The copies are left untracked, "
                       "so re-editing a patch can't delete them along with the original; entering "
                       "the object again hands each one to the Plasticity face it sits on")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        _source, result = mesh_build.mirror_target(context)
        return (result is not None
                and result.modifiers.get(mesh_build.MIRROR_MODIFIER_NAME) is not None
                and context.mode == 'OBJECT')

    def execute(self, context: bpy.types.Context) -> set[str]:
        source, result = mesh_build.mirror_target(context)
        if result is None:
            self.report({'WARNING'}, "No retopology to apply a mirror to")
            return {'CANCELLED'}

        added, error = mesh_build.bake_mirror(context, result)
        if error:
            self.report({'WARNING'}, error)
            return {'CANCELLED'}

        if source is not None:
            update_committed_count(context, source)
        push_undo("Retop: apply mirror")
        self.report({'INFO'},
                    f"Mirror applied — {added} face(s) added, untracked until the "
                    f"object is entered again")
        return {'FINISHED'}


class RETOP_OT_toggle_see_through(bpy.types.Operator):
    bl_idname = "retop.toggle_see_through"
    bl_label = "Retopo Through Meshes"
    bl_description = ("Toggle whether the retopology draws over everything else or is occluded "
                      "like any other object. Seeing it through the CAD surface is what you want "
                      "while building it; switching that off is the only way to check it sits on "
                      "the surface rather than floating off it")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _global_keys_live(context)

    def execute(self, context: bpy.types.Context) -> set[str]:
        state = context.scene.plasticity_retop
        state.result_see_through = not state.result_see_through
        # The property's own update callback re-applies the look.
        self.report({'INFO'},
                    "Retopo drawn through meshes" if state.result_see_through
                    else "Retopo occluded like any object")
        return {'FINISHED'}


class RETOP_OT_local_view(bpy.types.Operator):
    """Blender's Local View, extended to keep the retopology in view."""

    bl_idname = "retop.local_view"
    bl_label = "Isolate (Keep Retopology)"
    bl_description = ("Toggle Local View like '/' does, then pull the isolated object's "
                      "retopology mesh and the live preview into it, so isolating a CAD "
                      "surface doesn't hide the very geometry you're building on it")
    bl_options = {'REGISTER'}

    frame_selected: bpy.props.BoolProperty(
        name="Frame Selected", default=True,
        description="Move the view to frame the isolated objects",
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if not _global_keys_live(context):
            return False  # '/' is plain view3d.localview again, one handler down
        space = context.space_data
        return space is not None and space.type == 'VIEW_3D'

    def execute(self, context: bpy.types.Context) -> set[str]:
        # Delegate the actual toggle: matching Blender's own selection rules,
        # framing and undo behaviour by hand is a losing game. When the setting
        # is off, sync_local_view is a no-op and '/' behaves exactly as usual.
        bpy.ops.view3d.localview('INVOKE_DEFAULT', frame_selected=self.frame_selected)

        space = context.space_data
        if space.local_view is None:
            return {'FINISHED'}  # we just left local view, nothing to add

        added = mesh_build.sync_local_view(context)
        if added:
            self.report({'INFO'}, f"Isolated with {added} retop object(s)")
        return {'FINISHED'}


def _perform_reload() -> None:
    """The actual unregister/reload/register cycle, run from a bpy.app.timers
    callback (see RETOP_OT_reload_addon) -- i.e. on Blender's next event loop
    tick, never from inside a still-running operator call. Unregistering an
    operator's own class while its execute() is still on the call stack is
    what crashed Blender the first time this was tried inline; deferring it
    like this is the standard safe pattern for a self-reloading addon.
    """
    import importlib
    from . import (
        ui,
        prefs as prefs_mod,
        version as version_mod,
        constants as constants_mod,
        patch_data as patch_data_mod,
        sides as sides_mod2,
        geometry as geometry_mod,
        generators as generators_mod,
        cad_display as cad_display_mod,
        state as state_mod,
        mesh_build as mesh_build_mod,
        patchprep as patchprep_mod,
        sidematch as sidematch_mod,
        keymap as keymap_mod,
        overlay as overlay_mod,
    )
    # Submodules are collected from sys.modules, never listed by hand.
    # Reloading a package re-runs its imports, but those resolve straight out
    # of sys.modules, so a module missing from the reload list keeps running
    # its old code -- and the mismatch surfaces as an AttributeError from a
    # *reloaded* module calling a function the stale one doesn't have yet.
    # That is precisely what a hand-written list did when generators/ngon.py
    # was added, hence this sweep.
    package_name = __name__.rpartition(".")[0]
    generator_prefix = f"{package_name}.generators."
    generator_modules = [module for name, module in sorted(sys.modules.items())
                         if name.startswith(generator_prefix) and module is not None]

    # Reloading unregisters the session operator's class, which kills any
    # running modal without it ever reaching _finish. Tear the session down
    # first so the scene isn't left claiming to be in a session that no longer
    # listens to anything.
    try:
        end_session(bpy.context)
        if bpy.context.workspace:
            bpy.context.workspace.status_text_set(None)
    except Exception:
        pass

    # Nothing is saved and restored around the property group here, and that is
    # deliberate: Blender keeps a PropertyGroup's values as ID properties on
    # the scene, keyed by name, so deleting Scene.plasticity_retop and
    # re-declaring it re-attaches to the same stored data. Every setting comes
    # through untouched. tests/test_reload.py asserts it rather than trusting
    # it -- it is a fact about Blender's storage, not about this code.
    ui.unregister()
    prefs_mod.unregister()
    unregister()
    state_mod.unregister()

    # Order matters for the listed ones: each is reloaded before whatever
    # imports it, so nothing is left holding a reference into a dead module.
    ordered = ([version_mod, constants_mod, patch_data_mod, sides_mod2, geometry_mod]
               + generator_modules
               + [generators_mod, cad_display_mod, mesh_build_mod,
                  patchprep_mod, sidematch_mod, keymap_mod, overlay_mod,
                  state_mod])

    reloaded = set()
    for module in ordered:
        importlib.reload(module)
        reloaded.add(module.__name__)

    # Anything else the package has picked up since -- a module nobody thought
    # to add above -- is reloaded too rather than silently left stale.
    last = (__name__, prefs_mod.__name__, ui.__name__)
    for name, module in sorted(sys.modules.items()):
        if (name.startswith(f"{package_name}.") and module is not None
                and name not in reloaded and name not in last):
            importlib.reload(module)

    importlib.reload(sys.modules[__name__])  # this operators module itself
    importlib.reload(prefs_mod)
    importlib.reload(ui)

    state_mod.register()
    sys.modules[__name__].register()
    # After the operators, like register(): the preferences page draws the
    # keymap items they just registered.
    prefs_mod.register()
    ui.register()

    print(f"[Plasticity Retop] Reloaded: v{version_mod.ADDON_VERSION} ({version_mod.BUILD_ID})")
    return None  # one-shot timer, don't reschedule


# --- the session's keys, as operators -------------------------------------
#
# Each of these used to be a branch inside `_modal`. They are operators with a
# real KeyMapItem now (see keymap.py), which is what puts them in Blender's own
# keymap editor instead of a hand-rolled one -- and it is their `poll` that
# decides whether the key means anything, because an item in the 3D View keymap
# fires whether a session is running or not.
#
# None of them touches the modal instance. Everything they need is scene state,
# which is why `typed_span` moved there; the modal notices a phase change on
# its next event and catches its own bookkeeping up.


def _in_phase(context: bpy.types.Context, *phases: str,
              during_corner_edit: bool = False) -> bool:
    """Whether a session is in one of `phases` -- and not in the corner editor.

    The editor is a sub-state of ADJUST rather than a phase of its own (the
    patch is still open, so every `session_phase == 'ADJUST'` test in the
    overlay must stay true), which means the exclusion has to live somewhere
    the polls share. Here: one line, and every action below is inert for the
    length of the edit without restating it. `during_corner_edit=True` is for
    the two that have no reason to be -- the CAD edge and surface flow
    displays, which are read *while* choosing corners.
    """
    state = context.scene.plasticity_retop
    if not (state.session_active and state.session_phase in phases):
        return False
    return during_corner_edit or not state.corner_edit


class RETOP_OT_nudge_span(bpy.types.Operator):
    """Step the span the wheel drives, or the N-gon's detail."""
    bl_idname = "retop.nudge_span"
    bl_label = "Span +/-"
    bl_description = ("Add or remove a segment on the span being adjusted. In N-gon mode there is "
                       "no span, so the same gesture drives the detail angle instead")
    bl_options = {'REGISTER'}

    delta: bpy.props.IntProperty(name="Delta", default=1)

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _in_phase(context, 'ADJUST')

    def execute(self, context: bpy.types.Context) -> set[str]:
        state = context.scene.plasticity_retop
        state.typed_span = ""  # scrolling takes over from a half-typed number
        if state.ngon_mode:
            # An n-gon has no span to step, but it does have a density, and
            # that is `ngon_angle` -- *inverted*, since it is degrees of
            # boundary turn per kept vertex, so scrolling up lowers it.
            # Multiplicative because the setting is: a 2 degree step is nothing
            # at 90 and everything at 4.
            factor = 1.25
            angle = (state.ngon_angle / factor if self.delta > 0
                     else state.ngon_angle * factor)
            state.ngon_angle = max(1.0, min(180.0, round(angle, 1)))
        else:
            prop = active_span_prop(state)
            # Assigning fires the property's update callback, which regenerates
            # the preview live.
            setattr(state, prop, max(1, getattr(state, prop) + self.delta))
        return {'FINISHED'}


class RETOP_OT_toggle_span_axis(bpy.types.Operator):
    """Switch which span the wheel and the digits drive."""
    bl_idname = "retop.toggle_span_axis"
    bl_label = "U / V Direction"
    bl_description = "Switch between the U and V span, on a quad or a wedge"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _in_phase(context, 'ADJUST')

    def execute(self, context: bpy.types.Context) -> set[str]:
        state = context.scene.plasticity_retop
        if state.generator_name not in TWO_SPAN_GENERATORS:
            # A key that does nothing and reports nothing reads as a broken
            # key, which is how the silent version of this got reported as "the
            # addon captures Tab".
            self.report({'INFO'},
                        f"{state.generator_name or 'This generator'} has a single span: "
                        f"no direction to switch")
            return {'CANCELLED'}
        state.span_axis = 'V' if state.span_axis == 'U' else 'U'
        state.typed_span = ""  # the number being typed applied to the other span
        return {'FINISHED'}


class RETOP_OT_toggle_ngon(bpy.types.Operator):
    """Fill a flat patch with one face following its boundary."""
    bl_idname = "retop.toggle_ngon"
    bl_label = "N-gon Mode"
    bl_description = ("Fill a flat patch with a single face following its boundary, instead of a "
                       "span grid. Only where the patch can take one -- a curved face would get a "
                       "flat lid over it")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _in_phase(context, 'ADJUST')

    def execute(self, context: bpy.types.Context) -> set[str]:
        state = context.scene.plasticity_retop
        if not state.ngon_mode and not state.ngon_available:
            self.report({'WARNING'},
                        f"N-gon mode not available here: {state.ngon_unavailable_reason}")
            return {'CANCELLED'}
        # The property's own update callback regenerates the preview and
        # refreshes generator_name, so the panel and overlay follow.
        state.ngon_mode = not state.ngon_mode
        state.typed_span = ""
        return {'FINISHED'}


class RETOP_OT_toggle_match_mode(bpy.types.Operator):
    """Show or hide which sides can be matched to a committed neighbour."""
    bl_idname = "retop.toggle_match_mode"
    bl_label = "Side Highlight"
    bl_description = ("Highlight the sides of the patch being adjusted, so clicking one matches "
                       "its committed neighbour's vertices")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _in_phase(context, 'ADJUST')

    def execute(self, context: bpy.types.Context) -> set[str]:
        state = context.scene.plasticity_retop
        state.match_mode = not state.match_mode
        state.hovered_side = -1
        state.typed_span = ""
        return {'FINISHED'}


class RETOP_OT_toggle_cad_edges(bpy.types.Operator):
    """The borders between CAD faces, drawn over the source surface."""
    bl_idname = "retop.toggle_cad_edges"
    bl_label = "Plasticity Edges"
    bl_description = ("Draw the Plasticity edges -- the borders between CAD faces -- over the "
                       "source surface. Read while choosing a surface as much as while adjusting "
                       "one, so it works in every phase")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _in_phase(context, 'OBJECT', 'PATCH', 'ADJUST',
                         during_corner_edit=True)

    def execute(self, context: bpy.types.Context) -> set[str]:
        state = context.scene.plasticity_retop
        state.show_cad_edges = not state.show_cad_edges
        self.report({'INFO'},
                    "Plasticity edges: " + ("on" if state.show_cad_edges else "off"))
        return {'FINISHED'}


class RETOP_OT_toggle_surface_flow(bpy.types.Operator):
    """The grid each CAD face would be retopologized into."""
    bl_idname = "retop.toggle_surface_flow"
    bl_label = "Surface Flow"
    bl_description = ("Draw the grid each CAD face would be retopologized into, at a low density. "
                       "Derived from each face's boundary -- the bridge carries no surface "
                       "parameters, so these are not Plasticity's own isoparms")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _in_phase(context, 'OBJECT', 'PATCH', 'ADJUST',
                         during_corner_edit=True)

    def execute(self, context: bpy.types.Context) -> set[str]:
        state = context.scene.plasticity_retop
        state.show_surface_flow = not state.show_surface_flow
        self.report({'INFO'},
                    "Surface flow: " + ("on" if state.show_surface_flow else "off"))
        return {'FINISHED'}


def _patch_hover_wanted(context: bpy.types.Context) -> bool:
    """Whether the patch data display wants the hover modal running.

    `context.scene` through a getattr, not straight: `register` calls this by
    way of `sync_patch_hover`, and Blender registers an addon under a
    **restricted** context -- a `_RestrictContext` with no `scene` on it at
    all, because no file is loaded yet. Reaching for it there raised
    `'_RestrictContext' object has no attribute 'scene'` and took the whole
    registration with it, so enabling the addon (or deploying and re-enabling
    it) failed outright while the very same code was fine once running. There
    is nothing to want at that moment anyway: the scene the toggle lives on
    arrives with `load_post`, which calls this again.
    """
    scene = getattr(context, "scene", None)
    state = getattr(scene, "plasticity_retop", None)
    return bool(state is not None
                and getattr(state, "debug_patch_ids", False)
                and getattr(state, "debug_patch_scope", 'HOVER') == 'HOVER')


def _start_patch_hover() -> None:
    """Invoke the hover modal in the first 3D viewport there is.

    Run from a timer, so `bpy.context` is the real one but there is no area to
    invoke into -- a modal operator has to be given a window and a VIEW_3D area
    or it has nothing to register its handler on.
    """
    if RETOP_OT_patch_hover.running or not _patch_hover_wanted(bpy.context):
        return
    window = next((w for w in bpy.context.window_manager.windows), None)
    if window is None:
        return
    area = next((a for a in window.screen.areas if a.type == 'VIEW_3D'), None)
    if area is None:
        # No viewport to hover over. Nothing to start, and nothing is wrong:
        # the next sync (a toggle, a file load) will try again.
        return
    try:
        with bpy.context.temp_override(window=window, area=area):
            bpy.ops.retop.patch_hover('INVOKE_DEFAULT')
    except (RuntimeError, AttributeError):
        # No context to invoke into (headless, or mid-load). The display simply
        # shows nothing until something asks again.
        pass


def sync_patch_hover() -> None:
    """Bring the hover modal in line with the scene properties.

    Called from the property update and from `load_post`. The modal stops
    itself when the toggle goes off -- it checks on every event -- so this only
    ever has to handle the starting half.
    """
    if not _patch_hover_wanted(bpy.context):
        return
    if bpy.app.timers.is_registered(_start_patch_hover):
        return
    bpy.app.timers.register(_start_patch_hover, first_interval=0.0)


def refresh_debug_hover(
    context: bpy.types.Context, mouse_x: float, mouse_y: float
) -> None:
    """Point the patch data display at whatever is under (mouse_x, mouse_y).

    Shared by `RETOP_OT_patch_hover` and by the **session** modal, which has to
    feed it: `_modal` answers a mouse move with RUNNING_MODAL, and a modal added
    to the stack before it never sees the event -- so a display switched on
    before a session started would freeze for the length of it, which is exactly
    when the raw face ids are being read. One raycast, and only while the
    display is on and following the cursor: `_patch_hover_wanted` is the first
    thing checked.

    Off the viewport the hover is *dropped* rather than left standing, or it
    would go on naming a patch the cursor is nowhere near.
    """
    if not _patch_hover_wanted(context):
        return

    area = context.area
    region, _rv3d = viewport_region(context)
    found = None
    if (area is not None and area.type == 'VIEW_3D'
            and point_in_viewport(area, region, mouse_x, mouse_y)):
        origin, direction = ray_from_window(context, mouse_x, mouse_y)
        if origin is not None and direction is not None:
            hit_obj, face_id, _distance = _raycast_patch_ray(
                context, origin, direction, area.spaces.active)
            if hit_obj is not None and face_id is not None:
                found = (hit_obj.name, face_id)

    if found != overlay.debug_hover:
        overlay.debug_hover = found
        _tag_viewports_redraw(context)


class RETOP_OT_patch_hover(bpy.types.Operator):
    """Track the cursor so the patch debug display can follow it.

    A draw handler is given no event, so hovering cannot be read from one --
    the session solves this by having its own modal leave the position in
    `overlay.cursor_window`, and this is the same arrangement for the debug
    display, which has to work with no session at all.

    It reads and never writes: every event is passed straight back to Blender,
    so this can sit under the knife, the loop cut, a transform or Blender's own
    selection without taking anything from them. The one thing it must not do
    is outlive its toggle, so that is checked on every event rather than
    trusted to whoever turned it off.

    The hover lands in a module global on `overlay` rather than in a scene
    property: a scene property written on every mouse move marks the file as
    modified, and nothing about looking at a mesh should do that.
    """
    bl_idname = "retop.patch_hover"
    bl_label = "Follow Cursor for Patch Data"
    bl_options = {'REGISTER'}

    # Not an annotation: a class attribute on a registered class is read by the
    # registration walk, and this is ours, not Blender's. One instance at a
    # time is the whole point -- two would fight over the same global.
    running = False

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return not cls.running and _patch_hover_wanted(context)

    def invoke(self, context: bpy.types.Context, _event: bpy.types.Event) -> set[str]:
        RETOP_OT_patch_hover.running = True
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _finish(self, context: bpy.types.Context) -> set[str]:
        RETOP_OT_patch_hover.running = False
        overlay.debug_hover = None
        _tag_viewports_redraw(context)
        return {'CANCELLED'}

    def modal(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        if not _patch_hover_wanted(context):
            return self._finish(context)

        if event.type not in ('MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'):
            return {'PASS_THROUGH'}

        refresh_debug_hover(context, event.mouse_x, event.mouse_y)
        return {'PASS_THROUGH'}


class RETOP_OT_print_patch_data(bpy.types.Operator):
    """The whole groups/face_ids table, to the system console."""
    bl_idname = "retop.print_patch_data"
    bl_label = "Print Patch Data to Console"
    bl_description = ("Write every patch's face id and [loop_start, loop_count] range to the "
                       "system console, with any problems found in them. A part has hundreds "
                       "of faces, which is a console's job rather than a sidebar's")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        obj = context.active_object
        return obj is not None and obj.type == 'MESH'

    def execute(self, context: bpy.types.Context) -> set[str]:
        obj = context.active_object
        mesh = obj.data
        report = cad_display.integrity(mesh)

        print(f"\n=== Plasticity patch data: {obj.name} ({mesh.name}) ===")
        sizes = ", ".join(f"{n}-gon x{count}" if n != 3 else f"tri x{count}"
                          for n, count in sorted(report.polygon_sizes.items()))
        print(f"  {len(report.entries)} patches, {report.loop_total} loops, "
              f"{len(mesh.polygons)} polygons ({sizes or 'none'})")
        print(f"  triangulated: {report.triangulated}")
        print(f"  {'face_id':>10} {'loop_start':>11} {'loop_count':>11} "
              f"{'polys':>7} {'first poly':>11}")
        for entry in report.entries:
            print(f"  {entry.face_id:>10} {entry.loop_start:>11} "
                  f"{entry.loop_count:>11} {entry.poly_count:>7} "
                  f"{entry.poly_start:>11}")

        if report.problems:
            print(f"  --- {len(report.problems)} problem(s) ---")
            for problem in report.problems:
                print(f"  ! {problem}")
            self.report({'WARNING'},
                        f"{len(report.problems)} problem(s) -- see the console")
        else:
            self.report({'INFO'},
                        f"{len(report.entries)} patches written to the console")
        return {'FINISHED'}


class RETOP_OT_back(bpy.types.Operator):
    """One step out per press: clear typing, discard, leave the object, end.

    A single operator rather than one per phase because it is a single idea --
    "back out of whatever I am in" -- and because a keymap the user reads
    should have one Esc in it, not four with mutually exclusive polls.
    """
    bl_idname = "retop.back"
    bl_label = "Discard / Back Out"
    bl_description = ("Step back out: clear a half-typed span, then discard the patch, then leave "
                       "the object, then end the session -- one step per press")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _in_phase(context, 'OBJECT', 'PATCH', 'ADJUST')

    def execute(self, context: bpy.types.Context) -> set[str]:
        state = context.scene.plasticity_retop

        if state.session_phase == 'ADJUST':
            # A first press only cancels a half-typed number, so a typo doesn't
            # throw away the patch itself.
            if state.typed_span:
                state.typed_span = ""
                return {'FINISHED'}
            # Guarded: there is nothing to discard on a patch whose preview
            # failed to generate, and backing out of *that* is exactly when you
            # need this key to work. The modal never hit it because it only
            # reached this branch with a preview on screen.
            if bpy.ops.retop.clear_preview.poll():
                bpy.ops.retop.clear_preview()
            state.session_phase = 'PATCH'
            state.active_face_id = -1
            return {'FINISHED'}

        if state.session_phase == 'PATCH':
            # Gathered surfaces are a step in, so Esc is a step out of them --
            # the same rule that makes the first Esc in ADJUST clear a
            # half-typed span rather than throw the patch away.
            if surface_selection(state):
                set_surface_selection(state, [])
                return {'FINISHED'}
            exit_session_object(context)
            return {'FINISHED'}

        # OBJECT: nothing left to back out of but the session itself. Clearing
        # session_active is what the modal watches to finish -- it owns the
        # timer, the cursor and the draw handlers, and none of those is this
        # operator's to tear down.
        state.session_active = False
        return {'FINISHED'}


class RETOP_OT_open_keymap_prefs(bpy.types.Operator):
    """Open the addon's preferences page, where the keybind rows live."""
    bl_idname = "retop.open_keymap_prefs"
    bl_label = "Edit Keybinds"
    bl_description = ("Open this addon's preferences, where every key is a normal Blender keymap "
                       "row: click the key field and press a new one. The same items are under "
                       "Preferences > Keymap > Add-ons > 3D View")
    bl_options = {'REGISTER'}

    def execute(self, context: bpy.types.Context) -> set[str]:
        module = __package__
        try:
            bpy.ops.preferences.addon_show(module=module)
        except (RuntimeError, TypeError):
            # Imported directly rather than installed as an addon (the tests
            # do this), so there is no addon entry to show. Falling back to the
            # keymap section still gets the user to the same items.
            try:
                bpy.ops.screen.userpref_show('INVOKE_DEFAULT')
                context.preferences.active_section = 'KEYMAP'
            except (RuntimeError, TypeError):
                self.report({'WARNING'},
                            "Could not open Preferences — find the keys under "
                            "Preferences > Keymap > Add-ons > 3D View")
                return {'CANCELLED'}
        return {'FINISHED'}


class RETOP_OT_reload_addon(bpy.types.Operator):
    bl_idname = "retop.reload_addon"
    bl_label = "Reload Retop Addon Only"
    bl_description = ("Reload just this addon's Python modules from disk. More reliable than "
                       "Blender's global Reload Scripts, which can silently fail to finish if any "
                       "other installed addon errors partway through its own reload")
    bl_options = {'REGISTER'}

    def execute(self, context: bpy.types.Context) -> set[str]:
        # Schedule the reload for the next event loop tick instead of doing it
        # inline: unregistering this very operator's class while its execute()
        # is still running is unsafe (see _perform_reload's docstring).
        bpy.app.timers.register(_perform_reload, first_interval=0.0)
        self.report({'INFO'}, "Reloading Plasticity Retop...")
        return {'FINISHED'}


@bpy.app.handlers.persistent
def _on_undo_redo(
    scene: bpy.types.Scene, _depsgraph: bpy.types.Depsgraph | None = None
) -> None:
    """Bring session state back in line with what undo just restored.

    An undo step can put the result mesh back to a state that has nothing to do
    with the patch currently open: the faces a re-edit removed may be back, the
    snapshot taken to restore them may be gone, the object being retopped may
    not exist any more. Anything acted on afterwards would be acting on stale
    references, so the active patch is simply dropped and the session returns to
    picking.

    Deliberately touches scene properties only -- no datablock is created,
    freed or edited from a handler. Everything that needs one (the preview mesh
    the step left holding a patch that is no longer open, most of all) is
    deferred to the modal through _undo_needs_reconcile.
    """
    global _undo_needs_reconcile

    state = getattr(scene, "plasticity_retop", None)
    if state is None:
        return

    # Whatever the step restored, the running modal's own idea of the world is
    # now a guess: its hovered patch, the preview geometry sitting in the
    # viewport and the side references cached for the overlay all describe a
    # mesh state that is gone. The modal picks this up on its next event.
    _undo_needs_reconcile = True

    state.active_face_id = -1
    state.generator_name = ""
    state.num_sides = 0
    state.num_loops = 1
    # The snapshot belongs to a mesh state undo has just replaced; restoring it
    # later would overwrite whatever the user undid back to.
    state.editing_committed = False
    state.reedit_removed_faces = 0
    state.reedit_backup_mesh = ""
    state.reedit_result_object = ""

    if state.session_active:
        session_obj = bpy.data.objects.get(state.session_object_name)
        if session_obj is None:
            state.session_object_name = ""
            state.session_phase = 'OBJECT'
            state.committed_patch_count = 0
        elif state.session_phase == 'ADJUST':
            state.session_phase = 'PATCH'


@bpy.app.handlers.persistent
def _on_load_post(_path: str = "") -> None:
    """Restart the patch hover modal for a file that had it switched on.

    A modal does not survive a file load, but the scene property that asks for
    one does -- Blender stores it with the rest of the scene. Without this, a
    file saved with the debug display on reopens with the display frozen on
    whatever the cursor last pointed at, which reads as an overlay that has
    stopped working rather than as a modal that is no longer there.
    """
    overlay.debug_hover = None
    sync_patch_hover()


_HANDLERS = (
    ("undo_post", "_on_undo_redo"),
    ("redo_post", "_on_undo_redo"),
    ("load_post", "_on_load_post"),
)


def _register_handlers() -> None:
    _unregister_handlers()  # never stack duplicates across an addon reload
    bpy.app.handlers.undo_post.append(_on_undo_redo)
    bpy.app.handlers.redo_post.append(_on_undo_redo)
    bpy.app.handlers.load_post.append(_on_load_post)


def _unregister_handlers() -> None:
    for list_name, function_name in _HANDLERS:
        handlers = getattr(bpy.app.handlers, list_name)
        for handler in list(handlers):
            # By name: a module reload leaves the previous function object
            # registered, and it is no longer identical to this one.
            if getattr(handler, "__name__", "") == function_name:
                handlers.remove(handler)


CLASSES = (
    RETOP_OT_session,
    RETOP_OT_end_session,
    RETOP_OT_commit_patch,
    RETOP_OT_clear_preview,
    RETOP_OT_delete_patch,
    RETOP_OT_pin_side,
    RETOP_OT_edit_corners,
    RETOP_OT_toggle_corner,
    RETOP_OT_corners_accept,
    RETOP_OT_corners_cancel,
    RETOP_OT_copy_patch_spans,
    RETOP_OT_toggle_surface,
    RETOP_OT_split_patch,
    RETOP_OT_tweak_mesh,
    RETOP_OT_end_tweak,
    RETOP_OT_mirror_axis,
    RETOP_OT_mirror,
    RETOP_OT_apply_mirror,
    RETOP_OT_toggle_see_through,
    RETOP_OT_local_view,
    RETOP_OT_nudge_span,
    RETOP_OT_toggle_span_axis,
    RETOP_OT_toggle_ngon,
    RETOP_OT_toggle_match_mode,
    RETOP_OT_toggle_cad_edges,
    RETOP_OT_toggle_surface_flow,
    RETOP_OT_patch_hover,
    RETOP_OT_print_patch_data,
    RETOP_OT_back,
    RETOP_OT_open_keymap_prefs,
    RETOP_OT_reset_corner_methods,
    RETOP_OT_reload_addon,
)


_addon_keymaps: list[tuple[bpy.types.KeyMap, bpy.types.KeyMapItem]] = []


def _register_keymaps() -> None:
    """Register every action in `keymap.ACTIONS` as a real KeyMapItem.

    All of them, session keys included: that is what puts them in Blender's own
    keymap editor and in the addon's preferences page, and what makes Blender
    -- rather than a hand-rolled table -- own the editing, the conflict display
    and the persistence. Each operator's `poll` is what decides whether the key
    means anything right now, since an item in the 3D View keymap fires whether
    a session is running or not.

    Three items share `TAB` on purpose (U/V, hand-edit, back from hand-edit)
    and their polls are mutually exclusive by phase. Blender walks the items
    and runs the first whose poll passes, which is exactly the behaviour the
    modal used to spell out.

    The '/' override can't follow a scene property, so when "Keep Retopo in
    Isolate" is off RETOP_OT_local_view just forwards to view3d.localview and
    nothing about '/' changes. Its poll does follow the *session*, as every
    GLOBAL key's now does (`_global_keys_live`): outside one the item is
    skipped and the key belongs to Blender and to other addons again. Alt+X is the
    mirror, as in Hard Ops -- the reflex the key is borrowed from, and symmetry
    is reached for far more often than the x-ray, which is why the x-ray sits
    on V. *Not* Alt+Z: that is Blender's own viewport X-ray and taking it
    over cost more than it gave, and it is a different question anyway.
    """
    _unregister_keymaps()
    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig is None:
        return  # background/headless Blender has no addon keyconfig

    km = keyconfig.keymaps.new(name='3D View', space_type='VIEW_3D')
    for action_id in keymap.ACTION_IDS:
        operator = keymap.operator_of(action_id)
        for binding in keymap.default_bindings(action_id):
            kmi = km.keymap_items.new(
                operator, binding["type"], 'PRESS',
                ctrl=bool(binding.get("ctrl")),
                shift=bool(binding.get("shift")),
                alt=bool(binding.get("alt")))
            for name, value in keymap.properties_of(action_id).items():
                setattr(kmi.properties, name, value)
            _addon_keymaps.append((km, kmi))
            keymap.remember(action_id, kmi)


def _unregister_keymaps() -> None:
    for km, kmi in _addon_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass  # the keymap can already be gone on a full reload
    _addon_keymaps.clear()
    # The overlay reads live items through this to name its keys; leaving it
    # holding freed ones would have a draw handler dereferencing them.
    keymap.forget_all()


def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    _register_handlers()
    _register_keymaps()
    # Installed for the life of the addon, unlike the session's overlay: the
    # patch data display describes an *imported* mesh, which is something you
    # look at before any session exists. Its callback early-outs on the scene
    # toggle, so the cost when it is off is one getattr per redraw -- and it can
    # never be found missing after a file load, which is what arming a handler
    # from a property update would risk.
    overlay.enable_patch_debug()
    # A reload unregisters the operator class out from under any running modal
    # instance, so the flag is cleared on the way in rather than trusted; then
    # a file that had the hover on gets it back.
    RETOP_OT_patch_hover.running = False
    sync_patch_hover()


def unregister() -> None:
    # A session could still be running (e.g. Reload Addon Only was clicked
    # mid-session); leaving its draw handler behind would leak an overlay that
    # nothing can remove afterwards.
    overlay.disable()
    overlay.disable_patch_debug()
    overlay.debug_hover = None
    RETOP_OT_patch_hover.running = False
    _unregister_keymaps()
    _unregister_handlers()
    for cls in reversed(CLASSES):
        # Tolerant for the same reason `state._drop` is: `__init__.register`
        # unwinds a failed registration through here, and the classes after the
        # one that raised were never registered at all.
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
