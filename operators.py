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

# Whether a session modal is actually listening. Session state lives in the
# scene and can outlive it (`session_is_running`).
_SESSION_RUNNING: bool = False

# Set by the undo/redo handler, consumed by the modal on its next event: the
# handler may not touch a datablock. See "The undo handler defers" in CLAUDE.md.
_undo_needs_reconcile: bool = False


def resolve_session_object(
    obj: bpy.types.Object | None,
) -> bpy.types.Object | None:
    """The object a session should actually run on.

    `<Something>_Retop` resolves to `Something`. Anything else is returned
    untouched.
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
    """Override `defaults` with the spans committed neighbours used along a
    shared side (mesh_build's span registry). Returns (defaults, locked_keys).
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
        # Both sides share one span.
        span_u = side_span(0)
        if span_u is None:
            span_u = side_span(1)
        if span_u is not None:
            defaults["span_u"] = span_u
            locked.append("span_u")
    else:
        # One span for every side: the first committed neighbour decides it.
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
    """Record the span along each side of a just-committed patch, in
    mesh_build's span registry.

    A ring and an n-gon register each loop separately: pairing corners across
    loops would invent a side from the outer boundary to a hole.
    """
    state = context.scene.plasticity_retop
    if state.generator_name == generators.NGON.name:
        # An n-gon's segment count per side, recomputed with the same matches
        # the preview was built with.
        matched = sidematch.apply_side_matches(
            context, source_obj, prepared, generators.NGON.name)[0]
        forced = ngon_forced_segments(state, prepared, matched)
        for loop_i, (corner_ids, loop_sides) in enumerate(
                zip(prepared.loops_corner_ids, prepared.loops_sides)):
            if corner_ids:
                mesh_build.register_patch_spans(
                    source_obj, corner_ids,
                    generators.ngon.loop_allocation(
                        loop_sides, state.ngon_angle,
                        forced[loop_i] if loop_i < len(forced) else None))
        return

    # The same grouping as generation (`_side_groups_for`), or the registry
    # describes a patch that was not built.
    groups = _side_groups_for(state, prepared, ngon=False)
    corner_ids = prepared.corner_source_ids
    if groups:
        # One entry per group, at the corner it starts at. Corners inside a
        # group are not registered.
        corner_ids = [prepared.loops_corner_ids[0][run[0]] for run in groups]

    if state.generator_name == constants.NSIDE:
        # Per side, from the same spoke solve as generation (`nside_allocation`).
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

    Wanted counts are applied in `_winning_matches` order (a pin first, then
    the denser match). What cannot fit is refused, never approximated.
    Shared by generation and the commit path, so both get the same answer.
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
    """Span along each side of the active patch, in boundary order, for the
    span registry.
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
        self.num_loops = num_loops  # boundary loops the patch has (2 = ring, >2 = n-gon only)
        self.spans = spans  # (span_u, span_v, span)
        self.corner_source_ids = corner_source_ids
        self.propagated = propagated  # span keys taken from a committed neighbour
        self.committed = committed  # this patch is already in the result mesh (re-edit)
def adopt_side_reference(
    context: bpy.types.Context, flat_index: int, kind: str | None = None
) -> sidematch.SideReference | None:
    """Pin side `flat_index` to the vertices it should reproduce.

    A side can only be pinned to the committed patch across it
    (`sidematch.PIN_NEIGHBOUR`). A click on a matched side turns the match off
    (`sidematch.PIN_EXCLUDED`), never just releases the pin: automatic matching
    would put it straight back. A two-state toggle.

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

    A committed patch comes back the way it was committed.
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

    Only one reason: a face that is not flat. `num_loops` blocks nothing.
    """
    if not patchprep.patch_is_planar(mesh, face_id, state.ngon_planar_tolerance):
        return "not a flat face"
    return ""


def _joined(subsides: "list[list[mathutils.Vector]]") -> "list[mathutils.Vector]":
    """Several consecutive sides as one polyline, the shared endpoints dropped.

    Never keep the shared vertex twice: that is a zero-length segment.
    """
    points: "list[mathutils.Vector]" = []
    for sub in subsides:
        points.extend(sub[:-1])
    points.append(subsides[-1][-1])
    return points


# Segments per side of the n-gon last generated, by flat side index: where
# Ctrl+wheel over a side starts from.
_ngon_allocation: dict[int, int] = {}


def ngon_forced_segments(
    state: "state_mod.RetopPatchState",
    prepared: "patchprep.PreparedPatch",
    matched: dict[int, int],
) -> "list[dict[int, int]]":
    """Per loop, {side in loop: segments} for the n-gon fill.

    The matched sides first, then the counts set with Ctrl+wheel, shared over a
    group's sides by `patchprep.allocate_group_segments`. A lone matched side
    keeps its match.
    Shared by generation and the commit path.
    """
    forced = sidematch.ngon_side_segments(prepared, matched)
    counts = sidematch.ngon_group_counts(state)
    if not counts:
        return forced
    slots = sidematch.side_slots(prepared)
    slot_of = {slot.index: slot for slot in slots}
    for run in sidematch.ngon_runs(slots, state):
        total = counts.get(sidematch.ngon_group_key(run))
        if total is None:
            continue
        if len(run) == 1 and run[0] in matched:
            continue
        loop = slot_of[run[0]].loop
        if loop >= len(forced):
            continue
        subsides = [prepared.loops_sides[loop][slot_of[i].in_loop] for i in run]
        pins = {position: matched[i] for position, i in enumerate(run) if i in matched}
        floor = sum(pins.values()) + len(run) - len(pins)
        allocation = patchprep.allocate_group_segments(
            subsides, max(total, floor), pins)
        for index, count in zip(run, allocation):
            forced[loop][slot_of[index].in_loop] = count
    return forced


def _remember_ngon_allocation(
    state: "state_mod.RetopPatchState",
    prepared: "patchprep.PreparedPatch",
    forced: "list[dict[int, int]]",
) -> None:
    _ngon_allocation.clear()
    index = 0
    for loop_i, loop_sides in enumerate(prepared.loops_sides):
        allocation = generators.ngon.loop_allocation(
            loop_sides, state.ngon_angle,
            forced[loop_i] if loop_i < len(forced) else None)
        for count in allocation:
            _ngon_allocation[index] = count
            index += 1


def nudge_ngon_side(
    context: bpy.types.Context, index: int, delta: int
) -> tuple[bool, str]:
    """Add or remove vertices on the n-gon group holding side `index`.

    Returns (changed, message).
    """
    state = context.scene.plasticity_retop
    references = sidematch.active_sides()
    if not (0 <= index < len(references)):
        return False, "No side under the cursor"
    run = next((run for run in sidematch.ngon_runs(references, state) if index in run),
               [index])
    matched = {i: len(references[i].applied_points) - 1
               for i in run if references[i].applied and references[i].applied_points}
    if len(run) == 1 and index in matched:
        return False, ("This side copies its neighbour's vertices. "
                       "Click it to release the match first")

    key = sidematch.ngon_group_key(run)
    counts = sidematch.ngon_group_counts(state)
    current = counts.get(key)
    if current is None:
        current = sum(_ngon_allocation.get(i, 1) for i in run)
    floor = sum(matched.values()) + len(run) - len(matched)
    wanted = max(floor, current + (1 if delta > 0 else -1))
    counts[key] = wanted
    sidematch.set_ngon_group_counts(state, counts)
    regenerate_active_preview(context)
    what = "side" if len(run) == 1 else f"group of {len(run)} sides"
    return True, f"{wanted} segments on this {what}"


def _span_for_group(spans: dict[str, int], generator_name: str, position: int) -> int:
    """How many segments group `position` carries, through the same span key
    the matching uses (`sidematch.span_key_for`)."""
    key = sidematch.span_key_for(
        generator_name, sidematch.SideSlot(position, 0, position))
    return max(1, spans.get(key, spans.get(sidematch.span_base(key), 1)))


def _side_groups_for(
    state: "state_mod.RetopPatchState",
    prepared: "patchprep.PreparedPatch",
    ngon: bool,
) -> "list[list[int]] | None":
    """The user's grouping as runs of side indices, or None to use the sides.

    None for an n-gon or a ring, when nothing is merged, or when the grouping
    is invalid (it is reported, and the sides are used meanwhile).
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

    A matched side's count is fixed: it becomes a pin on the group's
    allocation.
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
    """Prepare a patch, pick a generator, generate and push the result into the
    preview object. Returns a PatchPreview, or None on failure. Callers report.
    """
    state = context.scene.plasticity_retop
    mesh = obj.data

    # Decide the mode first: the corner method depends on it.
    committed = mesh_build.is_patch_committed(obj, face_id)
    wants_ngon = _ngon_wanted(state, obj, face_id, committed)
    blocker = ngon_blocker(state, mesh, face_id) if wants_ngon else ""
    ngon = wants_ngon and not blocker

    def prepare(for_ngon: bool) -> patchprep.PreparedPatch | None:
        return patchprep.prepare_patch(
            mesh, face_id, state.corner_angle_threshold,
            # Typed in state.length_unit.
            state_mod.to_blender_units(state, state.small_side_tolerance),
            state.corner_method_ngon if for_ngon else state.corner_method_spans,
            # A span generator gets the outer boundary alone.
            keep_holes=for_ngon)

    prepared = prepare(ngon)
    if prepared is None:
        return None

    # Two loops that are not a band (a plate with a small hole) go to the n-gon
    # fill. See "Two boundary loops is not the same thing as a band".
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

    # Several holes go to the n-gon fill: a span generator would cover them.
    if not ngon and prepared.num_loops > 2:
        blocker = ngon_blocker(state, mesh, face_id)
        holes = prepared.num_loops - 1
        if committed:
            pass  # a committed patch comes back as whatever it was built as
        elif blocker:
            ring_note = f"{holes} holes, and {blocker}"
        else:
            ngon = True
            ring_note = f"{holes} holes -- filled as an n-gon"
            prepared = prepare(True)
            if prepared is None:
                return None

    state.ngon_available = not blocker
    state.ngon_unavailable_reason = blocker
    state.corner_warning = prepared.corner_warning
    state.generator_note = ring_note

    corner_source_ids = prepared.corner_source_ids

    # The user's side grouping, settled before the generator: the group count
    # picks it. Single-loop span patches only.
    groups = _side_groups_for(state, prepared, ngon)
    if groups:
        corner_source_ids = [prepared.loops_corner_ids[0][run[0]] for run in groups]

    # The generator is chosen before any side is substituted: matches are
    # resolved per span, which depends on the generator.
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
    # Collected now, applied once the spans are settled: a match both drives a
    # span and depends on it.
    winners, outvoted = sidematch.collect_side_matches(context, generator.name)
    state.match_conflicts = len(outvoted)

    if ngon:
        # One count per side: no match can conflict.
        matched, _ = sidematch.apply_side_matches(context, obj, prepared, generator.name,
                                        winners=winners)
        forced = ngon_forced_segments(state, prepared, matched)
        _remember_ngon_allocation(state, prepared, forced)
        settings = {"ngon_angle": state.ngon_angle, "side_segments": forced}
        if prepared.has_holes:
            # One more n-gon per hole.
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
        # Two boundary loops: fill the band between them.
        generation_input = prepared.loops_sides
        num_sides = len(corner_source_ids)
        defaults = state_mod.scale_default_spans(
            state, generator.default_spans(generation_input))
        # Spans are not propagated into a ring, only out of it.
        propagated = []
    elif groups:
        # Joined for `default_spans` only. The real per-sub-side allocation
        # needs the resolved span, further down.
        generation_input = [_joined([prepared.sides[i] for i in run]) for run in groups]
        num_sides = len(generation_input)
        defaults = state_mod.scale_default_spans(
            state, generator.default_spans(generation_input))
        defaults, propagated = _propagated_defaults(obj, generator, corner_source_ids, defaults)
    else:
        generation_input = prepared.sides
        num_sides = len(generation_input)
        # Resolution first, then propagation: a propagated span is never scaled.
        defaults = state_mod.scale_default_spans(
            state, generator.default_spans(generation_input))
        defaults, propagated = _propagated_defaults(obj, generator, corner_source_ids, defaults)

    # A committed patch comes back with its committed spans, which beat both
    # the defaults and propagation.
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

    # A match sets the span that drives it. A pin always decides; an automatic
    # match only seeds the span on first generation, so a typed span wins.
    # See "An automatic match seeds a span; a pin decides it" in CLAUDE.md.
    # Weakest first, so the strongest match ends up driving the span.
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
        # A group of `n` sub-sides needs at least `n` segments. Floored per
        # span, never globally.
        floors: dict[str, int] = {}
        group_pins = _group_pins(winners, groups)
        for position, run in enumerate(groups):
            key = sidematch.span_base(
                sidematch.span_key_for(generator.name, sidematch.SideSlot(
                    position, 0, position)))
            pins = group_pins[position]
            # Room for the pins, plus one segment per other sub-side.
            needed = max(len(run), sum(pins.values()) + (len(run) - len(pins)))
            floors[key] = max(floors.get(key, 1), needed)
        span_u = max(span_u, floors.get("span_u", 1))
        span_v = max(span_v, floors.get("span_v", 1))
        span = max(span, floors.get("span", 1))

    spans = {"span_u": span_u, "span_v": span_v, "span": span}
    group_counts: "list[list[int]]" = []
    if groups:
        # Allocated once, here: the matching and the polylines must use the
        # same counts.
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
        # The spoke allocation, as per-side spans, so `_honours` drops exactly
        # the matches it could not fit.
        span = generators.nside.even_span(span)
        nside_spokes, _refused = nside_allocation(
            len(groups) if groups else len(prepared.sides), span, winners)
        segments_of = generators.nside.side_segments(nside_spokes)
        spans.update({f"side:{index}": count
                      for index, count in enumerate(segments_of)})

    sidematch.apply_side_matches(context, obj, prepared, generator.name, spans, winners=winners)

    if groups:
        # Rebuild each group from its sub-sides' counts, after the substitution.
        generation_input = [
            patchprep.group_side_points([prepared.sides[i] for i in run], counts)
            for run, counts in zip(groups, group_counts)]

    bvh = (geometry.build_bvh_for_polygons(mesh, prepared.patch.poly_indices)
           if state.reproject else None)
    span_settings = {"span_u": span_u, "span_v": span_v, "span": span}
    if nside_spokes is not None:
        span_settings["spokes"] = nside_spokes
    if prepared.is_ring:
        # The loops carrying a neighbour's vertices: never phased or resampled.
        span_settings["locked_loops"] = sorted(sidematch.applied_loops())
        # And which of their sides, with each one's count.
        span_settings["matched_sides"] = sidematch.applied_side_counts()
    result = generator.generate(generation_input, span_settings, bvh=bvh)

    # Relax the interior; the boundary stays put. Needs the BVH.
    if bvh is not None:
        geometry.relax_interior_points(
            result.verts, result.faces, result.boundary_local_indices,
            bvh, state.relax_iterations)

    mesh_build.update_preview_object(context, obj, result, corner_source_ids)
    return PatchPreview(generator, num_sides, prepared.num_loops, (span_u, span_v, span),
                        corner_source_ids, propagated, committed)


def regenerate_active_preview(context: bpy.types.Context) -> bool:
    """Re-run generation for the active patch with the current settings.
    Called by the property update callbacks, so sliders update live.
    """
    state = context.scene.plasticity_retop
    if state.active_face_id == -1 or state.source_object_name not in bpy.data.objects:
        return False

    obj = bpy.data.objects[state.source_object_name]
    span_overrides = {"span_u": state.span_u, "span_v": state.span_v, "span": state.span}
    preview = _generate_for_face(context, obj, state.active_face_id, span_overrides)
    if preview is None:
        return False

    # The generator can change on a live update (N-gon mode).
    state.generator_name = preview.generator.name
    state.num_sides = preview.num_sides
    state.num_loops = preview.num_loops
    return True


def update_committed_count(
    context: bpy.types.Context, obj: bpy.types.Object | None
) -> None:
    """Refresh the panel's cached count of committed patches.
    Call whenever the result mesh changes.
    """
    state = context.scene.plasticity_retop
    state.committed_patch_count = len(mesh_build.committed_face_ids(obj)) if obj else 0


def begin_reedit(
    context: bpy.types.Context, obj: bpy.types.Object, face_id: int
) -> int:
    """Take patch `face_id`'s existing geometry out of the result mesh so the
    re-edit rebuilds it from nothing, and remember the snapshot that puts it
    back. Returns how many faces were removed.

    Removed on pick, never on commit, so a failure shows at once.
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
        # It edited the result mesh and created a datablock: push an undo step.
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
    """Put back the patch a re-edit took out. Every exit but a commit must
    call this.
    """
    state = context.scene.plasticity_retop
    if state.reedit_backup_mesh:
        mesh_build.restore_result_snapshot(state.reedit_result_object, state.reedit_backup_mesh)
        update_committed_count(context, bpy.data.objects.get(state.session_object_name)
                               or bpy.data.objects.get(state.source_object_name))
    _clear_reedit(state)


def _is_own_scaffolding(obj: bpy.types.Object) -> bool:
    """True for the preview and the result meshes: a raycast looks through
    them.
    """
    return (obj.name == mesh_build.PREVIEW_OBJ_NAME
            or obj.name.endswith(mesh_build.RESULT_NAME_SUFFIX))


def _raycast_patch_ray(
    context: bpy.types.Context,
    ray_origin: mathutils.Vector,
    ray_direction: mathutils.Vector,
    space: bpy.types.SpaceView3D | None = None,
    surfaces: bool = False,
) -> tuple[bpy.types.Object | None, int | None, float | None]:
    """Cast `ray_origin`/`ray_direction` through the scene and return
    (hit_object, face_id, distance) for the first Plasticity mesh that is
    actually visible in this viewport, or (None, None, None). `distance` is
    measured in world units from `ray_origin`.

    `surfaces=True` names the raw Plasticity surface, not the patch over it.

    Looks through, never stops at: objects hidden in this viewport, this
    addon's own meshes, and non-Plasticity meshes.
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
        # Look through anything that is not a visible Plasticity mesh.
        if not visible or _is_own_scaffolding(hit_obj) or not _is_plasticity_mesh(hit_obj):
            # Step past the hit, by a share of the distance travelled.
            origin = location + ray_direction * max(1e-6, distance * 1e-5)
            continue

        analysis = (patch_data.analyse_surfaces(hit_obj.data) if surfaces
                    else patch_data.analyse(hit_obj.data))
        face_id_of_poly = analysis.face_id_of_poly
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
    """Distance from `ray_origin` to where the ray hits patch `face_id` of
    `obj`, or None. For the hover hysteresis.
    """
    if obj is None or obj.name not in bpy.data.objects:
        return None

    matrix_inv = obj.matrix_world.inverted()
    local_origin = matrix_inv @ ray_origin
    # A direction transforms without translation.
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

    Never trust context.region inside a modal: it may be the N-panel's.
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


# Events the modal must never swallow outside the viewport. In fact it passes
# every event through there; this set lets the tests assert it.
PANEL_EVENTS = {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE', 'LEFTMOUSE', 'RIGHTMOUSE',
                'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE',
                'RET', 'NUMPAD_ENTER', 'ESC', 'TAB', 'BACK_SPACE',
                'M', 'N', 'X', 'ZERO', 'ONE', 'TWO', 'THREE', 'FOUR',
                'FIVE', 'SIX', 'SEVEN', 'EIGHT', 'NINE'}


# Regions drawn over the WINDOW region. With Region Overlap on, a point under
# the N-panel is also inside WINDOW, so these must be subtracted.
OVERLAY_REGION_TYPES = {'UI', 'TOOLS', 'TOOL_PROPS', 'HEADER', 'TOOL_HEADER',
                        'NAV_BAR', 'FOOTER', 'ASSET_SHELF', 'ASSET_SHELF_HEADER',
                        'HUD', 'EXECUTE', 'CHANNELS'}


def point_in_region(region: bpy.types.Region | None, x: float, y: float) -> bool:
    """Whether a window-absolute point is inside `region`.

    Window-absolute: `context.region` is unreliable in a modal.
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
        # A collapsed region reports 1px and covers nothing.
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

    Window-absolute, converted against the WINDOW region: `event.mouse_region_*`
    may belong to another region. Operators the modal dispatches pass
    `overlay.cursor_window`.
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

    In screen space: a raycast would hit the surface beside the line.
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


# The grouping lives in `sidematch`. Only the hit test is here: it needs a
# region.
def side_bubble_under_cursor(context: bpy.types.Context, event: bpy.types.Event) -> int:
    """Flat index of the side whose group bubble the cursor is on, or -1.

    A point-in-disc test, with the same anchor and radius as the drawing
    (`sidematch.side_midpoint`, `overlay.GROUP_BUBBLE_SIZE`). The nearest wins
    when two overlap.
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
# The assembly lives in `patch_data`. This is the session's half: what
# Shift+click gathers, and writing a composite to the mesh.


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
        refresh_pending_composite(context, obj)
        mesh_build.refresh_preview_appearance(context)
        return True, (f"Dropped surface {face_id} — {len(selection)} selected"
                      if selection else "Surface selection cleared")

    # Already inside another composite: it must be split first.
    for other, surfaces in patch_data.read_composites(obj.data).items():
        if other != state.pending_composite_id and face_id in surfaces:
            return False, (f"surface {face_id} is already part of another patch — "
                           "split that one apart first")

    # Already committed: its faces would never be deleted under the new id.
    if mesh_build.is_patch_committed(obj, face_id):
        return False, (f"surface {face_id} is already retopologized — delete its "
                       "patch first (X while re-editing it)")

    # The raw surfaces: the selection is kept in surface ids.
    analysis = patch_data.analyse_surfaces(obj.data)
    patch = analysis.patches.get(face_id)
    if patch is None or not patch.boundary_loops:
        return False, f"surface {face_id} has no usable boundary"

    # Contiguity is checked at each click, so the refusal names the surface.
    if selection and not patch_data.patch_neighbour_ids(patch).intersection(selection):
        return False, ("that surface does not touch the selection — one patch has to "
                       "be one connected area")

    selection.append(face_id)
    set_surface_selection(state, selection)
    refresh_pending_composite(context, obj)
    mesh_build.refresh_preview_appearance(context)
    return True, f"{len(selection)} surfaces selected"


def refresh_pending_composite(
    context: bpy.types.Context, obj: bpy.types.Object
) -> int:
    """Rebuild the patch the picked surfaces make, and preview it. Returns its
    id, or -1 when fewer than two are picked.

    From the second pick on, the selection is a composite written to the mesh.
    Every way out of the pick must call `discard_pending_composite`.
    """
    state = context.scene.plasticity_retop
    # Always taken apart first, then rebuilt from the surface ids.
    _drop_pending_composite(context, obj)

    selection = surface_selection(state)
    if len(selection) < 2:
        mesh_build.clear_preview_object()
        return -1

    composite_id, _message = build_composite(context, obj, selection)
    if composite_id is None:
        return -1

    state.pending_composite_id = composite_id
    if _generate_for_face(context, obj, composite_id) is None:
        # Nothing can be built over them. The selection stays.
        mesh_build.clear_preview_object()
    return composite_id


def _drop_pending_composite(
    context: bpy.types.Context, obj: bpy.types.Object | None
) -> None:
    """Take the pending composite off the mesh, if there is one."""
    state = context.scene.plasticity_retop
    composite_id = state.pending_composite_id
    state.pending_composite_id = -1
    if composite_id == -1 or obj is None or obj.type != 'MESH':
        return
    composites = patch_data.read_composites(obj.data)
    if composites.pop(composite_id, None) is not None:
        patch_data.write_composites(obj.data, composites)


def discard_pending_composite(context: bpy.types.Context) -> None:
    """Drop the picked surfaces and everything built from them.

    Called by every exit that does not open the patch. No undo step: only a
    mesh property changes.
    """
    state = context.scene.plasticity_retop
    obj = bpy.data.objects.get(state.session_object_name)
    _drop_pending_composite(context, obj)
    state.surface_selection = ""
    mesh_build.refresh_preview_appearance(context)


def build_composite(
    context: bpy.types.Context, obj: bpy.types.Object, face_ids: "list[int]"
) -> tuple[int | None, str]:
    """Write `face_ids` to the mesh as one composite patch and return its id.

    Always flat: an existing composite in the selection is replaced by the new
    one.
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
    # Allocated before the old ones are dropped: never reissue an id.
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

    # Refused while committed or open in a re-edit: its faces would name a
    # patch that no longer exists.
    state = context.scene.plasticity_retop
    reediting = (state.editing_committed and state.active_face_id == composite_id)
    if reediting or mesh_build.is_patch_committed(obj, composite_id):
        return False, ("it is already retopologized — delete the patch first "
                       f"({keymap.describe('delete_patch')} while re-editing it), or "
                       "its faces would be left naming a patch that no longer exists")

    del composites[composite_id]
    patch_data.write_composites(mesh, composites)
    return True, f"Split back into {len(surfaces)} patches"


def dissolve_composite(obj: bpy.types.Object, composite_id: int) -> int:
    """Take a composite off the mesh unconditionally. Returns how many surfaces
    it covered, or 0 if it was not one.

    `split_composite` without the rules, for a caller that has just deleted
    the patch's faces.
    """
    composites = patch_data.read_composites(obj.data)
    surfaces = composites.pop(composite_id, None)
    if surfaces is None:
        return 0
    patch_data.write_composites(obj.data, composites)
    return len(surfaces)


def surface_under_cursor(
    context: bpy.types.Context,
    event: bpy.types.Event,
    obj: bpy.types.Object | None,
    face_id: int | None,
) -> int:
    """The mesh's own surface under the cursor, or -1.

    `obj`/`face_id` are the hovered patch. A second raycast only when the mesh
    has a composite.
    """
    if obj is None or face_id is None:
        return -1
    if not patch_data.analyse(obj.data).composites:
        return face_id

    origin, direction = ray_from_event(context, event)
    if origin is None:
        return -1
    hit_obj, surface_id, _distance = _raycast_patch_ray(
        context, origin, direction, space=context.space_data, surfaces=True)
    return surface_id if hit_obj is obj and surface_id is not None else -1


def composite_surfaces(obj: bpy.types.Object | None, face_id: int) -> list[int]:
    """The Plasticity surfaces `face_id` is built from, or [] for a single one."""
    if obj is None or obj.type != 'MESH' or face_id >= 0:
        return []
    return list(patch_data.analyse(obj.data).composites.get(face_id, []))


def load_patch_choices(
    state: "state_mod.RetopPatchState", obj: bpy.types.Object, face_id: int
) -> None:
    """Put back the per-patch choices `face_id` was committed with, or clear
    them: the side grouping and the n-gon side counts.

    Both name sides by index, so they are per patch. Called when a patch is
    opened and by the hover.
    """
    stored_settings = mesh_build.lookup_patch_settings(obj, face_id) or {}
    state.side_groups = str(stored_settings.get("side_groups", "") or "")
    state.ngon_group_counts = str(stored_settings.get("ngon_group_counts", "") or "")
    state.group_warning = ""
    state.corner_edit = False
    state.side_groups_backup = ""
    state.hovered_bubble = -1


def set_active_patch(
    context: bpy.types.Context, obj: bpy.types.Object, face_id: int
) -> tuple[str | None, int | None, list[str] | None]:
    """Generate a preview for `face_id` on `obj` and lock it in as the active
    patch. Returns (generator_name, num_sides, propagated_keys), or
    (None, None, None). The one code path for the picker and the tests.
    """
    # Pins name sides by index: per patch, cleared here for every caller.
    state = context.scene.plasticity_retop
    state.side_overrides = ""
    state.hovered_side = -1
    # The grouping too, restored from the patch's record if it has one. Here,
    # never in `_generate_for_face`, or each regeneration would undo edits.
    load_patch_choices(state, obj, face_id)
    # The copy source is per patch too.
    state.copy_source_face_id = -1
    state.copy_source_swapped = False

    # Correct the tracking before deciding whether this patch is a re-edit.
    # Also here, since the bridge can re-send the part mid-session.
    mesh_build.reconcile_patch_tracking(context, obj)

    # Generate first, remove second: generation reads the committed faces.
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


# Re-exported for the panel and the tests. Defined in `constants`.
TWO_SPAN_GENERATORS = constants.TWO_SPAN_GENERATORS

# Number-row and numpad digits, for typing a span directly.
DIGIT_KEYS: dict[str, str] = {}
for _d in range(10):
    DIGIT_KEYS[("ZERO", "ONE", "TWO", "THREE", "FOUR",
                "FIVE", "SIX", "SEVEN", "EIGHT", "NINE")[_d]] = str(_d)
    DIGIT_KEYS[f"NUMPAD_{_d}"] = str(_d)


def active_span_prop(state: state_mod.RetopPatchState) -> str:
    """The span property the wheel and the digits adjust on the active patch.
    """
    if state.generator_name in TWO_SPAN_GENERATORS:
        return "span_u" if state.span_axis == 'U' else "span_v"
    return "span"


def _clear_match_state(state: state_mod.RetopPatchState) -> None:
    """Clear the per-patch side picker state. `match_mode` is a preference and
    survives."""
    sidematch.clear_side_references()
    state.hovered_side = -1
    state.side_overrides = ""
    state.copy_source_face_id = -1
    state.copy_source_swapped = False


def end_session(context: bpy.types.Context, push: bool = True) -> None:
    """End the session: drop the preview, the highlight and all session state.
    Safe with no modal running.

    `push=False` when reacting to an undo: a push would discard the redo.
    """
    global _SESSION_RUNNING
    _SESSION_RUNNING = False

    state = context.scene.plasticity_retop
    # Restore the tool settings of an unfinished hand-edit. Idempotent.
    tweak.restore_tool_settings(context)
    # The only place the preview object is freed.
    mesh_build.remove_preview_object()
    overlay.hover_committed = False
    overlay.cursor_window = None
    # Roll back an unfinished re-edit.
    restore_reedit_removal(context)
    # And a pending composite, before the state that names the object is
    # cleared.
    discard_pending_composite(context)

    # Clear the state before refreshing: the look derives from it.
    state.session_active = False
    state.session_object_name = ""
    state.session_phase = 'OBJECT'
    state.active_face_id = -1
    state.generator_name = ""
    state.num_sides = 0
    state.editing_committed = False
    state.surface_hover_face_id = -1
    _clear_match_state(state)

    mesh_build.refresh_result_appearance(context)
    if push:
        push_undo("Retop: end session")


def push_undo(message: str) -> None:
    """Give the objects a session just created their own undo step.

    Every moment that creates or frees an ID, or writes the result mesh, must
    call this.
    """
    if bpy.app.background:
        return  # no undo stack in --background
    try:
        bpy.ops.ed.undo_push(message=message)
    except Exception:
        pass


def select_only(context: bpy.types.Context, obj: bpy.types.Object) -> None:
    """Make `obj` the selection and the active object.

    Blender's own object commands (isolate, frame) read the selection.
    Failures are ignored: an object outside the view layer cannot be selected.
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

    A result mesh resolves to its source first.
    """
    obj = resolve_session_object(obj)
    state = context.scene.plasticity_retop
    previous = bpy.data.objects.get(state.session_object_name)
    if previous is not None and previous is not obj:
        mesh_build.set_result_highlight(context, previous, False)

    mesh_build.ensure_result_object(context, obj)
    # Create the preview here, never on hover: inside the undo step below.
    mesh_build.ensure_preview_object(context)
    # Give every result face the patch id of the surface it sits on.
    # A missing or stale id makes a committed patch read as never retopped.
    mesh_build.reconcile_patch_tracking(context, obj)
    # Collect snapshots left by an interrupted re-edit.
    mesh_build.purge_stale_snapshots(keep_name=state.reedit_backup_mesh)
    update_committed_count(context, obj)
    # Set here, never rely on the caller: the highlight reads it.
    state.session_active = True
    state.session_object_name = obj.name
    state.session_phase = 'PATCH'
    mesh_build.set_result_highlight(context, obj, True)
    # Select what was picked, so Blender's own object commands apply to it.
    select_only(context, obj)
    # Pull the new meshes into an active Local View.
    mesh_build.sync_local_view(context)
    push_undo(f"Retop: enter {obj.name}")


def exit_session_object(context: bpy.types.Context) -> None:
    """Leave the current object but keep the session running.
    """
    state = context.scene.plasticity_retop
    mesh_build.clear_preview_object()
    restore_reedit_removal(context)  # same rule as end_session
    # And the pending composite.
    discard_pending_composite(context)

    # Same ordering rule as end_session: state first, then refresh.
    state.session_object_name = ""
    state.session_phase = 'OBJECT'
    state.active_face_id = -1
    state.generator_name = ""
    state.num_sides = 0
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
        """Object Mode only: the entry path creates datablocks."""
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
    # None until the first mouse move (see _update_cursor).
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
        # The cursor is restored, never set, here (see _apply_phase_ui).
        'TWEAK': ('DEFAULT',
                  "Hand-editing the retopology   |   K: knife   |   Ctrl+R: loop cut   |   "
                  "J: connect verts   |   G: move (snapped, auto-merge)   |   Tab: back to Retop"),
    }

    def _leave_for_other_mode(self, context: bpy.types.Context) -> None:
        """Drop back to picking an object because Blender left Object mode."""
        state = context.scene.plasticity_retop
        if state.session_phase == 'OBJECT':
            return

        # The hand-edit round trip: _modal_tweak owns it.
        if state.session_phase == 'TWEAK':
            return

        # Stay put when the mesh in Edit Mode is the one a re-edit took faces
        # out of: restoring them now would be lost when Edit Mode exits.
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
        # Only over the 3D view: a modal cursor applies to the whole window.
        if context.window and self._cursor_in_viewport is not False:
            if state.session_phase == 'TWEAK':
                # Blender's tools draw their own cursor here.
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

        `cursor_modal_set` applies to the whole window.
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
        # While surfaces are being picked, record the hover but never touch the
        # preview: it shows the patch they make.
        state = context.scene.plasticity_retop
        if state.pending_composite_id != -1 or surface_selection(state):
            self._hover_obj = obj
            self._hover_face_id = face_id
            return True
        load_patch_choices(state, obj, face_id)
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
        # For the overlay's "Re-edit patch" hint.
        overlay.hover_committed = preview.committed
        return True

    def _open_composite(self, context: bpy.types.Context, composite_id: int) -> bool:
        """Lock in the patch the picked surfaces already make.

        A composite that cannot be generated is taken back apart.
        """
        state = context.scene.plasticity_retop
        obj = bpy.data.objects.get(state.session_object_name)
        if obj is None:
            return False

        surfaces = len(surface_selection(state))
        generator, _sides, _propagated = set_active_patch(context, obj, composite_id)
        if generator is None:
            discard_pending_composite(context)
            self._clear_hover(context)
            self.report({'WARNING'},
                        "Those surfaces have no usable boundary together — undone")
            return False

        # The composite is the patch now: it is no longer pending.
        state.pending_composite_id = -1
        set_surface_selection(state, [])
        state.session_phase = 'ADJUST'
        mesh_build.refresh_preview_appearance(context)
        self._set_typed("")
        self._apply_phase_ui(context)
        # Its own undo step.
        push_undo("Retop: one patch from several surfaces")
        self.report({'INFO'}, f"One patch over {surfaces} surfaces — {generator}")
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

        Hysteresis: coincident surfaces would otherwise flip the hover on every
        mouse move.
        """
        if self._hover_obj is None or new_distance is None:
            return False

        ray_origin, ray_direction = ray_from_event(context, event)
        if ray_origin is None:
            return False

        current_distance = patch_hit_distance(ray_origin, ray_direction, self._hover_obj, self._hover_face_id)
        if current_distance is None:
            return False  # the cursor left the current patch

        state = context.scene.plasticity_retop
        if state.pick_depth_tolerance > 0.0:
            tolerance = state_mod.to_blender_units(state, state.pick_depth_tolerance)
        else:
            # Automatic: proportional to view distance.
            tolerance = max(1e-6, new_distance * 2e-3)
        return current_distance <= new_distance + tolerance

    def _modal_corners(
        self, context: bpy.types.Context, event: bpy.types.Event
    ) -> set[str] | None:
        """Mouse handling while the corner editor is open.

        Ahead of the side picker, and independent of the side highlight.
        """
        state = context.scene.plasticity_retop

        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            state.hovered_bubble = side_bubble_under_cursor(context, event)
            # The side and copy hovers mean nothing here.
            state.hovered_side = -1
            state.copy_hover_face_id = -1
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            # Through `_dispatch_bound`: the first action whose poll passes.
            if self._dispatch_bound(context, event):
                return {'RUNNING_MODAL'}
            # On nothing: never fall back to commit from inside the editor.
            return {'RUNNING_MODAL'}

        return None

    def _modal_match(
        self, context: bpy.types.Context, event: bpy.types.Event
    ) -> set[str] | None:
        """Mouse handling for the side highlight, while it is on. Returns a
        modal result, or None to let normal ADJUST handling have the event.
        Never takes Esc or the commit keys.
        """
        state = context.scene.plasticity_retop

        if event.type == 'MOUSEMOVE':
            state.hovered_side = nearest_side_to_cursor(context, event)
            # No copy outline over a side: Ctrl+click there opens the editor.
            state.copy_hover_face_id = (
                -1 if state.hovered_side != -1
                else _copy_source_under_cursor(context, event))
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            # On a side: the `pin_neighbour` binding. On nothing: fall through
            # to the commit fallback.
            if state.hovered_side == -1:
                return None
            bound = keymap.session_action_for(event)
            if bound == "pin_neighbour":
                self._run_bound_action(context, bound)
                return {'RUNNING_MODAL'}
            return None

        return None

    def _commit(self, context: bpy.types.Context) -> None:
        # Guard: the operator's poll would raise with no preview.
        if mesh_build.has_preview():
            bpy.ops.retop.commit_patch()
        self._set_typed("")

    # Actions whose key must never reach Blender, even when refused: `X` would
    # be `object.delete`, `Tab` `object.editmode_toggle`. Every other refused
    # key falls through.
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

        See keymap.py for why the modal dispatches these.
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

        Runs the first action on the key whose poll passes, never merely the
        first match. When none polls, a `_MUST_CONSUME` key is still consumed
        and the refusal reported.
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
        # Scene state: the operators that clear it cannot reach the modal.
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
        """Digits type a span directly; Backspace edits. Returns True when the
        event was consumed.
        """
        state = context.scene.plasticity_retop
        digit = DIGIT_KEYS.get(event.type)
        if digit is not None:
            # Capped, so a key repeat cannot build an absurd span.
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

        The undo handler may only write scene properties; the preview, the
        hover and a typed span are dealt with here.

        Returns a modal return value when the session did not survive the step,
        else None.
        """
        state = context.scene.plasticity_retop

        # Undone past the session's start: end the modal.
        if not state.session_active:
            # Never push a step right after an undo: it discards the redo.
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

        Everything passes through except the key that ends the round trip.
        Leaving Edit Mode another way fires no event: the timer notices, and the
        repair still runs once.
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
            # Never die silently: end the session cleanly and report.
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

        # `retop.back` asks for the end by clearing the flag; only the modal
        # can tear down its timer, cursor and draw handlers.
        if not state.session_active:
            return self._finish(context, "Retop session ended")

        # Commit and Discard clear active_face_id: back to picking.
        if state.session_phase == 'ADJUST' and state.active_face_id == -1:
            state.session_phase = 'PATCH'
            state.hovered_side = -1

        # Catch up with a phase changed elsewhere (an operator, the panel).
        if state.session_phase != self._last_phase:
            self._last_phase = state.session_phase
            # Only when leaving a patch: entering ADJUST keeps the preview.
            if state.session_phase in {'PATCH', 'OBJECT'}:
                self._clear_hover(context)
                # Otherwise stale until the next mouse move.
                state.surface_hover_face_id = -1
            self._cursor_in_viewport = None
            self._apply_phase_ui(context)
            mesh_build.refresh_preview_appearance(context)

        # Before the timer check: the timer is what notices Edit Mode ending.
        if state.session_phase == 'TWEAK':
            return self._modal_tweak(context, event)

        if event.type == 'TIMER':
            return {'PASS_THROUGH'}

        # Outside Object Mode, the viewport is Blender's.
        if context.mode != 'OBJECT':
            self._leave_for_other_mode(context)
            return {'PASS_THROUGH'}

        # The cursor first, before the area check: it applies to the window.
        over_viewport = self._cursor_over_viewport(context, event)
        self._update_cursor(context, over_viewport)
        # For the tooltips: a draw handler has no event.
        overlay.cursor_window = ((event.mouse_x, event.mouse_y)
                                 if over_viewport else None)

        # Feed the debug display's hover, starved while this modal runs.
        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            refresh_debug_hover(context, event.mouse_x, event.mouse_y)

        # Other editors stay fully interactive.
        if not self._in_viewport(context):
            return {'PASS_THROUGH'}

        # Every event passes through off the viewport, including over the
        # N-panel inside the same area. See "The modal swallows nothing outside
        # the 3D view's WINDOW region" in CLAUDE.md.
        if not over_viewport:
            state.hovered_side = -1
            state.surface_hover_face_id = -1
            return {'PASS_THROUGH'}

        # The session's keys, dispatched from here (see keymap.py). Clicks are
        # resolved by the picker below.
        if event.type not in {'LEFTMOUSE', 'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            if self._dispatch_bound(context, event):
                return {'RUNNING_MODAL'}

        if state.session_phase == 'ADJUST':
            # The corner editor owns the mouse while open.
            if state.corner_edit:
                consumed = self._modal_corners(context, event)
                if consumed is not None:
                    return consumed

            # Then the side picker, while the highlight is on.
            if state.match_mode:
                consumed = self._modal_match(context, event)
                if consumed is not None:
                    return consumed

            # Numeric entry, not remappable.
            if event.value == 'PRESS' and self._handle_typed_digit(context, event):
                return {'RUNNING_MODAL'}

            if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                # A modified click is a binding: dispatch it before the commit
                # fallback.
                if self._dispatch_bound(context, event):
                    return {'RUNNING_MODAL'}
                # A click on nothing commits.
                self._commit(context)
                return {'RUNNING_MODAL'}

            # Everything else passes through.
            return {'PASS_THROUGH'}

        if event.type == 'MOUSEMOVE':
            if state.session_phase == 'OBJECT':
                return {'PASS_THROUGH'}

            session_obj = bpy.data.objects.get(state.session_object_name)
            obj, face_id, distance = _raycast_patch(context, event)
            # Stay within the object being retopped.
            if obj is not None and session_obj is not None and obj != session_obj:
                obj, face_id = None, None

            # What Shift+click would take, while Shift is held.
            was_picking = mesh_build.surface_pick_open(state)
            state.surface_hover_face_id = (
                surface_under_cursor(context, event, obj, face_id)
                if event.shift else -1)
            if mesh_build.surface_pick_open(state) != was_picking:
                mesh_build.refresh_preview_appearance(context)

            if obj is not None and face_id is not None:
                if face_id != self._hover_face_id or obj != self._hover_obj:
                    if not self._keeps_current_hover(context, event, distance):
                        if not self._set_hover(context, obj, face_id):
                            self._clear_hover(context)
            elif self._hover_face_id is not None:
                if surface_selection(state):
                    # Keep the pick's preview when leaving the mesh.
                    self._hover_obj = None
                    self._hover_face_id = None
                else:
                    self._clear_hover(context)
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if state.session_phase == 'OBJECT':
                obj, _face_id, _distance = _raycast_patch(context, event)
                if obj is None:
                    return {'RUNNING_MODAL'}
                # `enter_session_object` selects the resolved object.
                enter_session_object(context, obj)
                self._apply_phase_ui(context)
                self.report({'INFO'}, f"Retopping {obj.name}")
                return {'RUNNING_MODAL'}

            # A modified click is a binding (Shift gathers a surface).
            if self._dispatch_bound(context, event):
                return {'RUNNING_MODAL'}

            if self._hover_face_id is None:
                # A click on empty space abandons a pick, like Esc does.
                if surface_selection(state):
                    discard_pending_composite(context)
                    self._clear_hover(context)
                    self.report({'INFO'}, "Surface selection cleared")
                return {'RUNNING_MODAL'}

            # Clicking a gathered surface opens them all; clicking another
            # abandons the pick and opens that one.
            pending = state.pending_composite_id
            if pending != -1:
                # Rebuild the hover after abandoning: it was held.
                if self._hover_face_id == pending:
                    self._open_composite(context, pending)
                    return {'RUNNING_MODAL'}
                discard_pending_composite(context)
                if (self._hover_obj is None or self._hover_face_id is None
                        or not self._set_hover(context, self._hover_obj,
                                               self._hover_face_id)):
                    self._clear_hover(context)
                    return {'RUNNING_MODAL'}
            elif surface_selection(state):
                # One surface picked: abandon it and rebuild the hover.
                set_surface_selection(state, [])
                mesh_build.refresh_preview_appearance(context)
                if (self._hover_obj is None
                        or not self._set_hover(context, self._hover_obj,
                                               self._hover_face_id)):
                    self._clear_hover(context)
                    return {'RUNNING_MODAL'}

            state.active_face_id = self._hover_face_id
            state.generator_name = self._hover_generator_name
            state.num_sides = self._hover_num_sides
            state.num_loops = self._hover_num_loops
            state.source_object_name = self._hover_obj.name
            state.span_u, state.span_v, state.span = self._hover_spans
            # Per-side matches belong to the patch they were picked on.
            state.side_overrides = ""
            state.hovered_side = -1
            # A patch committed as an n-gon reopens in n-gon mode.
            state.ngon_mode = self._hover_ngon
            state.editing_committed = self._hover_committed
            state.session_phase = 'ADJUST'
            self._set_typed("")
            self._apply_phase_ui(context)

            if self._hover_committed:
                # The hover already built the grid: remove the old faces.
                removed = begin_reedit(context, self._hover_obj, self._hover_face_id)
                self.report(
                    {'INFO'} if removed else {'WARNING'},
                    f"Re-editing patch {self._hover_face_id} "
                    f"({self._hover_generator_name}) — removed {removed} old face(s)")
            else:
                self.report({'INFO'},
                            f"Patch {self._hover_face_id} ({self._hover_generator_name})")
            return {'RUNNING_MODAL'}

        # Navigation passes through.
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

        # Skip the object pick when the active object is a Plasticity mesh.
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
    # Never 'UNDO': the step is pushed by hand in execute(). Both would leave
    # two identical states on the stack.
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        state = context.scene.plasticity_retop
        return (not state.corner_edit and mesh_build.has_preview()
                and state.source_object_name in bpy.data.objects)

    def execute(self, context: bpy.types.Context) -> set[str]:
        state = context.scene.plasticity_retop
        # Apply a span still being typed, for every caller.
        if state.typed_span:
            value = int(state.typed_span)
            if value >= 1:
                setattr(state, active_span_prop(state), value)
            state.typed_span = ""

        source_obj = bpy.data.objects[state.source_object_name]
        face_id = state.active_face_id
        replacing = state.editing_committed

        # Re-prepare the patch to register its spans, with the same corner
        # method as the preview.
        ngon_committed = state.generator_name == generators.NGON.name
        prepared = patchprep.prepare_patch(
            source_obj.data, face_id, state.corner_angle_threshold,
            state.small_side_tolerance,
            state.corner_method_ngon if ngon_committed else state.corner_method_spans,
            # The same loops generation was handed.
            keep_holes=ngon_committed)

        # The face id lets a re-commit replace the patch's previous faces.
        result_obj, error = mesh_build.commit_preview_to_result(context, source_obj, face_id=face_id)
        if error:
            self.report({'WARNING'}, error)
            return {'CANCELLED'}

        # Bookkeeping first, then span registration.
        keep_reedit_removal(context)
        update_committed_count(context, source_obj)

        mesh_build.register_patch_settings(
            source_obj, face_id, state.span_u, state.span_v, state.span,
            state.generator_name, state.side_groups,
            state.ngon_group_counts if ngon_committed else "")
        if prepared is not None:
            register_spans_for(context, source_obj, prepared)

        # Clearing active_face_id sends the session back to picking.
        state.active_face_id = -1
        state.generator_name = ""
        state.num_sides = 0

        verb = "Replaced" if replacing else "Committed"
        # One undo step per committed patch. See "One undo step per committed
        # patch" in CLAUDE.md.
        push_undo(f"Retop: {verb.lower()} patch {face_id}")
        self.report({'INFO'}, f"{verb} patch {face_id} in {result_obj.name}")
        return {'FINISHED'}


class RETOP_OT_clear_preview(bpy.types.Operator):
    bl_idname = "retop.clear_preview"
    bl_label = "Clear Preview"
    bl_description = "Discard the current preview without committing it"
    bl_options = {'REGISTER'}  # only a restored re-edit pushes a step

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        state = context.scene.plasticity_retop
        # Also during a re-edit with an empty preview, to roll it back.
        return mesh_build.has_preview() or state.editing_committed

    def execute(self, context: bpy.types.Context) -> set[str]:
        mesh_build.clear_preview_object()
        state = context.scene.plasticity_retop
        restoring = bool(state.reedit_backup_mesh)
        # Put back a re-edited patch as it was.
        restore_reedit_removal(context)
        if restoring:
            # A restore changed the result mesh: push a step. Never push one
            # for a discard that changed nothing.
            push_undo("Retop: discard re-edit")
        # Clearing active_face_id sends the session back to picking.
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
    bl_options = {'REGISTER'}  # the undo step is pushed by hand

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        state = context.scene.plasticity_retop
        # Only during a re-edit: the faces are already out.
        return (state.editing_committed and not state.corner_edit
                and state.source_object_name in bpy.data.objects)

    def execute(self, context: bpy.types.Context) -> set[str]:
        state = context.scene.plasticity_retop
        source_obj = bpy.data.objects[state.source_object_name]
        face_id = state.active_face_id
        removed = state.reedit_removed_faces

        # Keep the removal: drop the snapshot.
        keep_reedit_removal(context)
        mesh_build.clear_preview_object()
        mesh_build.forget_patch_settings(source_obj, face_id)
        # A composite is dissolved too: nothing could reach it afterwards.
        surfaces = dissolve_composite(source_obj, face_id)
        update_committed_count(context, source_obj)

        # Re-shade: the neighbours' borders changed.
        result_obj = bpy.data.objects.get(mesh_build.result_object_name_for(source_obj))
        if result_obj is not None:
            mesh_build.apply_result_shading(context, result_obj)

        state.active_face_id = -1
        state.generator_name = ""
        state.num_sides = 0

        # One undo step, as for commit.
        push_undo(f"Retop: delete patch {face_id}")
        self.report({'INFO'},
                    f"Deleted patch {face_id} ({removed} face(s))"
                    + (f" — back to {surfaces} surfaces" if surfaces else ""))
        return {'FINISHED'}


class RETOP_OT_pin_side(bpy.types.Operator):
    """Pin the side under the cursor to the vertices it should reproduce.

    A side with nothing committed across it is refused.
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

    Ctrl+click on a side. On anything else, the same binding copies a density
    (`retop.copy_patch_spans`).
    """
    bl_idname = "retop.edit_corners"
    bl_label = "Edit Corners"
    bl_description = ("Group this patch's sides: each side gets a number, and consecutive sides "
                      "with the same number become one side of the patch. That is how a "
                      "five-sided face becomes the quad it usually wants to be")
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
        # What Esc puts back.
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

    Any number is reachable; an invalid grouping is reported, never prevented.
    Wraps at the loop's side count, never clamps.
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
        self.report({'INFO'}, state.group_warning
                    or _corner_report(counts, state.ngon_mode))
        return {'FINISHED'}


def refresh_group_warning(context: bpy.types.Context) -> None:
    """Re-read what is wrong with the grouping, for the panel and the overlay.

    Written to the scene, so the panel and the overlay read the same answer.
    """
    state = context.scene.plasticity_retop
    references = sidematch.active_sides()
    if not references:
        state.group_warning = ""
        return
    _at_fault, message = sidematch.group_problems(
        references, sidematch.group_numbers(references, state))
    state.group_warning = message


def _corner_report(counts: dict[int, int], ngon: bool = False) -> str:
    """What the grouping now amounts to -- the generator it would pick."""
    if ngon:
        total = sum(counts.values())
        return (f"{total} groups -- "
                f"{overlay._pair_label('span_more', 'span_less')} over one sets its vertices")
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
        # Always closes; an invalid grouping stays reported.
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

    Never the patch being adjusted. Whether the generators agree is left to
    the overlay.
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

    Only between patches built by the same generator: the spans mean
    different things otherwise.

    Returns (done, message).
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
    # Clicking the same patch again swaps U and V: which direction is U is
    # arbitrary on each patch.
    swap = (face_id == state.copy_source_face_id) and not state.copy_source_swapped
    two_spans = source_generator in constants.TWO_SPAN_GENERATORS
    if swap and not two_spans:
        return False, f"a {source_generator} has one span, so there is nothing to swap"

    values = {key: stored.get(key) for key in ("span_u", "span_v", "span")}
    if swap:
        values["span_u"], values["span_v"] = values["span_v"], values["span_u"]

    # Every span in the record, as a re-edit restores them.
    changed = []
    for key in ("span_u", "span_v", "span"):
        value = values.get(key)
        if not (isinstance(value, int) and value >= 1):
            continue
        if getattr(state, key, None) == value:
            continue
        # Assigning regenerates the preview through the update callback.
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

    For patches that do not touch: propagation covers shared boundaries.
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
        # Stands down over a side: Ctrl+click there opens the corner editor.
        return (state.session_active and state.session_phase == 'ADJUST'
                and not state.corner_edit and state.hovered_side == -1
                and state.active_face_id != -1)

    def execute(self, context: bpy.types.Context) -> set[str]:
        # The pointer the modal left: a dispatched operator gets no event.
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

    Clicking any gathered surface opens them all as one patch.
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

        # The pointer the modal left: a dispatched operator gets no event.
        cursor = overlay.cursor_window
        origin, direction = (ray_from_window(context, cursor[0], cursor[1])
                             if cursor is not None else (None, None))
        if origin is None:
            self.report({'WARNING'}, "Cursor is not over the viewport")
            return {'CANCELLED'}

        # The raw surface, not the patch over it.
        hit_obj, face_id, _distance = _raycast_patch_ray(
            context, origin, direction, space=context.space_data, surfaces=True)
        if face_id is None or hit_obj != obj:
            self.report({'WARNING'}, "No surface of this object under the cursor")
            return {'CANCELLED'}

        changed, message = toggle_patch_surface(context, obj, face_id)
        self.report({'INFO'} if changed else {'WARNING'},
                    message if changed else f"Can't add that surface: {message}")
        return {'FINISHED'} if changed else {'CANCELLED'}


class RETOP_OT_split_patch(bpy.types.Operator):
    """Take the active patch back apart into the CAD surfaces it was built
    from."""
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

        # The open patch no longer exists: back to picking.
        if bpy.ops.retop.clear_preview.poll():
            bpy.ops.retop.clear_preview()
        state.active_face_id = -1
        state.session_phase = 'PATCH'
        push_undo("Retop: split patch into surfaces")
        self.report({'INFO'}, message)
        return {'FINISHED'}


class RETOP_OT_tweak_mesh(bpy.types.Operator):
    """Hand the result mesh to Blender's Edit Mode, set up for retopology.

    The session keeps running; see tweak.py.
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

    Runs `mesh_build.repair_manual_edits`, which a plain mode switch would skip.
    """
    bl_idname = "retop.end_tweak"
    bl_label = "Back to Retop"
    bl_description = ("Leave Edit Mode, put the snapping settings back as they were, and let the "
                       "addon re-adopt the faces and drop the stray corner ids the hand edits "
                       "left behind")
    # Never 'UNDO': the step is pushed by hand, like commit and delete.
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.scene.plasticity_retop.session_phase == 'TWEAK'

    def execute(self, context: bpy.types.Context) -> set[str]:
        adopted, cleared = tweak.exit_tweak(context)
        state = context.scene.plasticity_retop
        update_committed_count(
            context, bpy.data.objects.get(state.session_object_name))
        # The repair wrote to the result mesh: its own undo step.
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

    With no session, these polls fail and Blender passes the key on, unless
    `keymap.global_keys_outside_session` is on. Panel buttons are unaffected.
    """
    if keymap.global_keys_outside_session():
        return True
    return bool(context.scene.plasticity_retop.session_active)


class RETOP_OT_mirror_axis(bpy.types.Operator):
    """Turn the retopology's mirror on or off for one axis.

    A Mirror modifier on `<Object>_Retop` (see mesh_build's symmetry section).
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

    Every viewport: the pointer may be over another one than the modal's.
    """
    window = getattr(context, "window", None)
    screen = getattr(window, "screen", None) if window else None
    for area in (screen.areas if screen else ()):
        if area.type == 'VIEW_3D':
            area.tag_redraw()


class RETOP_OT_mirror(bpy.types.Operator):
    """Alt+X, then X / Y / Z: arm the axis prompt and toggle what it picks.

    A modal above the session's, so it sees the axis keys first.
    """
    bl_idname = "retop.mirror"
    bl_label = "Mirror Retopology"
    bl_description = ("Mirror the retopology across the source object's origin: press Alt+X, then "
                       "X, Y or Z to toggle that axis. Esc cancels")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if not _global_keys_live(context):
            return False  # the key passes to whoever else binds it
        _source, result = mesh_build.mirror_target(context)
        return result is not None

    def _restore_status(self, context: bpy.types.Context) -> None:
        """Give the status bar back to whoever had it.

        Puts the session's line back when a session is running.
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
        # And a gizmo under the cursor, showing which axes are already on.
        overlay.mirror_state = tuple(axes)
        overlay.mirror_cursor = (event.mouse_x, event.mouse_y)
        overlay.enable_mirror_gizmo()
        _tag_viewports_redraw(context)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        # Ignore modifier releases: Alt is still held from the binding.
        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            # The gizmo follows the pointer.
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

        # Anything else cancels.
        self._finish(context)
        if event.type not in {'ESC', 'RIGHTMOUSE'}:
            self.report({'INFO'}, "Mirror cancelled — X, Y or Z picks an axis")
        return {'CANCELLED'}

    def _finish(self, context: bpy.types.Context) -> None:
        """Every exit must go through here, or the draw handler leaks."""
        overlay.disable_mirror_gizmo()
        _tag_viewports_redraw(context)
        self._restore_status(context)


class RETOP_OT_reset_corner_methods(bpy.types.Operator):
    """Put both corner-detection methods back on their defaults.

    The rows are hidden outside Developer Mode: this is the way back from a
    stored non-default.
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
        # The update callback re-applies the look.
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
            return False  # '/' falls through to view3d.localview
        space = context.space_data
        return space is not None and space.type == 'VIEW_3D'

    def execute(self, context: bpy.types.Context) -> set[str]:
        # Delegate the toggle to Blender's own operator.
        bpy.ops.view3d.localview('INVOKE_DEFAULT', frame_selected=self.frame_selected)

        space = context.space_data
        if space.local_view is None:
            return {'FINISHED'}  # just left local view

        added = mesh_build.sync_local_view(context)
        if added:
            self.report({'INFO'}, f"Isolated with {added} retop object(s)")
        return {'FINISHED'}


def _perform_reload() -> None:
    """The unregister/reload/register cycle, run from a bpy.app.timers
    callback (RETOP_OT_reload_addon). Never call it from inside an operator:
    unregistering its own class crashes Blender.
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
    # Submodules are collected from sys.modules, never listed by hand: a
    # missing one keeps its old code.
    package_name = __name__.rpartition(".")[0]
    generator_prefix = f"{package_name}.generators."
    generator_modules = [module for name, module in sorted(sys.modules.items())
                         if name.startswith(generator_prefix) and module is not None]

    # End the session first: reloading kills the modal without _finish.
    try:
        end_session(bpy.context)
        if bpy.context.workspace:
            bpy.context.workspace.status_text_set(None)
    except Exception:
        pass

    # Settings survive without saving them: Blender keeps them on the scene by
    # name (tests/test_reload.py).
    ui.unregister()
    prefs_mod.unregister()
    unregister()
    state_mod.unregister()

    # Each module is reloaded before whatever imports it.
    ordered = ([version_mod, constants_mod, patch_data_mod, sides_mod2, geometry_mod]
               + generator_modules
               + [generators_mod, cad_display_mod, mesh_build_mod,
                  patchprep_mod, sidematch_mod, keymap_mod, overlay_mod,
                  state_mod])

    reloaded = set()
    for module in ordered:
        importlib.reload(module)
        reloaded.add(module.__name__)

    # Then any other submodule, so none stays stale.
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
    # After the operators: the preferences page draws their keymap items.
    prefs_mod.register()
    ui.register()

    print(f"[Plasticity Retop] Reloaded: v{version_mod.ADDON_VERSION} ({version_mod.BUILD_ID})")
    return None  # one-shot timer


# --- the session's keys, as operators -------------------------------------
#
# Each key is an operator with a real KeyMapItem (see keymap.py). Its poll
# decides whether the key means anything.
# None of them touches the modal instance: only scene state.


def _in_phase(context: bpy.types.Context, *phases: str,
              during_corner_edit: bool = False) -> bool:
    """Whether a session is in one of `phases`, and not in the corner editor
    (a sub-state of ADJUST). `during_corner_edit=True` allows the editor.
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
        state.typed_span = ""  # scrolling replaces a half-typed number
        if state.ngon_mode and state.hovered_side != -1:
            # Over a side: that side's (or group's) vertex count.
            changed, message = nudge_ngon_side(context, state.hovered_side, self.delta)
            self.report({'INFO'} if changed else {'WARNING'}, message)
            return {'FINISHED'} if changed else {'CANCELLED'}
        if state.ngon_mode:
            # Otherwise `ngon_angle`: inverted, and multiplicative.
            factor = 1.25
            angle = (state.ngon_angle / factor if self.delta > 0
                     else state.ngon_angle * factor)
            state.ngon_angle = max(1.0, min(180.0, round(angle, 1)))
        else:
            prop = active_span_prop(state)
            # The update callback regenerates the preview.
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
            # Say why nothing happens.
            self.report({'INFO'},
                        f"{state.generator_name or 'This generator'} has a single span: "
                        f"no direction to switch")
            return {'CANCELLED'}
        state.span_axis = 'V' if state.span_axis == 'U' else 'U'
        state.typed_span = ""  # it was being typed for the other span
        return {'FINISHED'}


class RETOP_OT_reset_ngon_counts(bpy.types.Operator):
    """Drop the vertex counts set on the n-gon's sides."""
    bl_idname = "retop.reset_ngon_counts"
    bl_label = "Reset Side Counts"
    bl_description = ("Forget the vertex counts set with Ctrl+Scroll over the n-gon's sides, so "
                      "every side follows the detail angle again")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        state = context.scene.plasticity_retop
        return _in_phase(context, 'ADJUST') and bool(state.ngon_group_counts)

    def execute(self, context: bpy.types.Context) -> set[str]:
        state = context.scene.plasticity_retop
        state.ngon_group_counts = ""
        regenerate_active_preview(context)
        self.report({'INFO'}, "Side counts reset")
        return {'FINISHED'}


class RETOP_OT_reset_sharp_edges(bpy.types.Operator):
    """Forget the sharp edges marked or cleared by hand."""
    bl_idname = "retop.reset_sharp_edges"
    bl_label = "Reset Hand-Set Sharp Edges"
    bl_description = ("Sharp edges you mark or clear yourself are kept when the retopology is "
                      "re-shaded. This forgets them, so every crease follows the angle again")
    bl_options = {'REGISTER'}

    def execute(self, context: bpy.types.Context) -> set[str]:
        if context.mode != 'OBJECT':
            self.report({'WARNING'}, "Leave Edit Mode first")
            return {'CANCELLED'}
        count = mesh_build.reset_sharp_overrides(context)
        push_undo("Retop: reset hand-set sharp edges")
        self.report({'INFO'}, f"Reset {count} hand-set edge(s)")
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
        # The update callback regenerates the preview.
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

    Always read `context.scene` through getattr: at registration the context
    is restricted and has no scene. `load_post` asks again later.
    """
    scene = getattr(context, "scene", None)
    state = getattr(scene, "plasticity_retop", None)
    return bool(state is not None
                and getattr(state, "debug_patch_ids", False)
                and getattr(state, "debug_patch_scope", 'HOVER') == 'HOVER')


def _start_patch_hover() -> None:
    """Invoke the hover modal in the first 3D viewport there is.

    Run from a timer, so it supplies the window and area itself.
    """
    if RETOP_OT_patch_hover.running or not _patch_hover_wanted(bpy.context):
        return
    window = next((w for w in bpy.context.window_manager.windows), None)
    if window is None:
        return
    area = next((a for a in window.screen.areas if a.type == 'VIEW_3D'), None)
    if area is None:
        # No viewport: the next sync tries again.
        return
    try:
        with bpy.context.temp_override(window=window, area=area):
            bpy.ops.retop.patch_hover('INVOKE_DEFAULT')
    except (RuntimeError, AttributeError):
        # No context to invoke into (headless, or mid-load).
        pass


def sync_patch_hover() -> None:
    """Bring the hover modal in line with the scene properties.

    Only starts it: the modal stops itself when the toggle goes off.
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

    Shared by `RETOP_OT_patch_hover` and the session modal, which starves the
    former of mouse moves. Off the viewport the hover is dropped.
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

    Works with no session. Passes every event through, and checks its toggle
    on every event. Writes `overlay.debug_hover`, never a scene property.
    """
    bl_idname = "retop.patch_hover"
    bl_label = "Follow Cursor for Patch Data"
    bl_options = {'REGISTER'}

    # Never annotated: registration reads class-body annotations. One instance
    # at a time.
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
    """One step out per press: clear typing, discard, leave the object, end."""
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
            # A first press only cancels a half-typed number.
            if state.typed_span:
                state.typed_span = ""
                return {'FINISHED'}
            # Guarded: a failing poll would raise out of the modal.
            if bpy.ops.retop.clear_preview.poll():
                bpy.ops.retop.clear_preview()
            state.session_phase = 'PATCH'
            state.active_face_id = -1
            return {'FINISHED'}

        if state.session_phase == 'PATCH':
            # A surface pick is a step in: Esc drops it first.
            if surface_selection(state):
                discard_pending_composite(context)
                # Guarded: the preview may already be empty.
                if bpy.ops.retop.clear_preview.poll():
                    bpy.ops.retop.clear_preview()
                return {'FINISHED'}
            exit_session_object(context)
            return {'FINISHED'}

        # OBJECT: ask the modal to end the session.
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
            # No addon entry (a plain import): open the keymap section.
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
        # Deferred: never unregister this class from its own execute().
        bpy.app.timers.register(_perform_reload, first_interval=0.0)
        self.report({'INFO'}, "Reloading Plasticity Retop...")
        return {'FINISHED'}


@bpy.app.handlers.persistent
def _on_undo_redo(
    scene: bpy.types.Scene, _depsgraph: bpy.types.Depsgraph | None = None
) -> None:
    """Bring session state back in line with what undo just restored.

    Drops the active patch and returns to picking. Scene properties only:
    anything touching a datablock is deferred to the modal
    (_undo_needs_reconcile).
    """
    global _undo_needs_reconcile

    state = getattr(scene, "plasticity_retop", None)
    if state is None:
        return

    # The modal reconciles on its next event.
    _undo_needs_reconcile = True

    # The undo already restored the mesh: only drop the pick's record.
    state.pending_composite_id = -1
    state.surface_selection = ""
    state.active_face_id = -1
    state.generator_name = ""
    state.num_sides = 0
    state.num_loops = 1
    # Never restore a snapshot from before the undo.
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

    A modal does not survive a file load, but the property asking for it does.
    """
    overlay.debug_hover = None
    sync_patch_hover()


_HANDLERS = (
    ("undo_post", "_on_undo_redo"),
    ("redo_post", "_on_undo_redo"),
    ("load_post", "_on_load_post"),
)


def _register_handlers() -> None:
    _unregister_handlers()  # never stack duplicates across a reload
    bpy.app.handlers.undo_post.append(_on_undo_redo)
    bpy.app.handlers.redo_post.append(_on_undo_redo)
    bpy.app.handlers.load_post.append(_on_load_post)


def _unregister_handlers() -> None:
    for list_name, function_name in _HANDLERS:
        handlers = getattr(bpy.app.handlers, list_name)
        for handler in list(handlers):
            # By name: a reload leaves the previous function object registered.
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
    RETOP_OT_reset_ngon_counts,
    RETOP_OT_reset_sharp_edges,
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

    Session keys included. Each operator's poll decides whether its key means
    anything right now. See "Keys" in CLAUDE.md.
    """
    _unregister_keymaps()
    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig is None:
        return  # no addon keyconfig in --background

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
            pass  # the keymap may already be gone on a reload
    _addon_keymaps.clear()
    # Never leave the overlay holding freed items.
    keymap.forget_all()


def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    _register_handlers()
    _register_keymaps()
    # The debug display's handler stays installed for the life of the addon.
    overlay.enable_patch_debug()
    # Never trust the flag after a reload.
    RETOP_OT_patch_hover.running = False
    sync_patch_hover()


def unregister() -> None:
    # A session may still be running: never leak its draw handlers.
    overlay.disable()
    overlay.disable_patch_debug()
    overlay.debug_hover = None
    RETOP_OT_patch_hover.running = False
    _unregister_keymaps()
    _unregister_handlers()
    for cls in reversed(CLASSES):
        # Tolerant: a failed registration unwinds through here.
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
