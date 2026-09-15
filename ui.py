import textwrap
from typing import TYPE_CHECKING

import bpy

from . import bridge
from . import cad_display
from . import constants
from . import keymap
from . import mesh_build
from . import operators
from . import overlay
from . import patch_data
from . import prefs
from . import sidematch
from . import version

if TYPE_CHECKING:
    from . import patch_data
    from . import state as state_mod


def _section(
    layout: bpy.types.UILayout,
    state: "state_mod.RetopPatchState",
    prop_name: str,
    label: str,
    icon: str = 'NONE',
) -> bpy.types.UILayout | None:
    """Collapsible section header. Returns the body layout when open, else None."""
    box = layout.box()
    header = box.row(align=True)
    header.prop(
        state, prop_name, text="", emboss=False,
        icon='TRIA_DOWN' if getattr(state, prop_name) else 'TRIA_RIGHT',
    )
    header.label(text=label, icon=icon)
    return box if getattr(state, prop_name) else None


def _draw_session(
    layout: bpy.types.UILayout,
    context: bpy.types.Context,
    state: "state_mod.RetopPatchState",
) -> None:
    """Session control: start/stop, current phase, and where commits land."""
    box = layout.box()
    stale = state.session_active and not operators.session_is_running()

    if stale:
        # Scene says "in session" but no modal is listening (addon reloaded
        # or the modal was interrupted): clicks and Esc would do nothing.
        col = box.column()
        col.alert = True
        col.label(text="Session interrupted", icon='ERROR')
        col.label(text="The viewport is not listening anymore.")
        box.operator("retop.end_session", text="Reset Session State", icon='X')
        box.operator("retop.session", text="Start Retop Session", icon='PLAY')
        return

    if not state.session_active:
        if context.mode != 'OBJECT':
            # The operator's poll already greys the button out; without this
            # the panel offers a dead button and no reason for it.
            col = box.column(align=True)
            col.label(text=f"Leave {context.mode.replace('_', ' ').title()} to start",
                      icon='INFO')
            col.enabled = True
        box.operator("retop.session", text="Start Retop Session", icon='PLAY')
        box.label(text="Click an object, then its surfaces", icon='INFO')
        return

    if state.session_phase == 'TWEAK':
        # Before the mode check below: Blender *is* in Edit Mode here, and it
        # is there because the session put it there. Saying "paused" would be
        # exactly backwards.
        col = box.column(align=True)
        col.label(text="Hand-editing", icon='EDITMODE_HLT')
        col.label(text=f"In: {state.session_object_name}")
        col.label(text="K knife · Ctrl+R loop · J connect · G move")
        box.operator("retop.end_tweak",
                     text=f"Back to Retop ({keymap.describe('end_tweak')})",
                     icon='LOOP_BACK')
        _draw_tweak_settings(box.column(align=True), state)
        return

    if context.mode != 'OBJECT':
        # The modal hands every event back in another mode, so the session is
        # doing nothing at all until Blender returns to Object Mode.
        paused = box.column(align=True)
        paused.label(text=f"Paused — Blender is in {context.mode.replace('_', ' ').title()}",
                     icon='INFO')
        paused.label(text="Back in Object Mode, pick an object again.")
        box.operator("retop.end_session", text="Stop Session", icon='X')
        return

    phase = state.session_phase
    if phase == 'OBJECT':
        box.label(text="Pick an object", icon='EYEDROPPER')
        box.label(text="Click a Plasticity object in the viewport")
        # Tab is the session's here too, on the *selected* object's retopology.
        # Greyed out by its own poll when nothing selected has one, rather than
        # hidden: the key is consumed in this phase either way, and a button
        # that disappears reads as a feature that doesn't exist.
        box.operator("retop.tweak_mesh",
                     text=f"Hand-Edit Selected ({keymap.describe('hand_edit')})",
                     icon='EDITMODE_HLT')
    elif phase == 'PATCH':
        box.label(text="Pick a surface", icon='RESTRICT_SELECT_OFF')
        # Not offered while a patch is open for adjustment, for the same reason
        # Tab is not the trip there: that patch has its faces out of the result
        # mesh, and Edit Mode would discard the snapshot that puts them back.
        box.operator("retop.tweak_mesh",
                     text=f"Hand-Edit Mesh ({keymap.describe('hand_edit')})",
                     icon='EDITMODE_HLT')
        box.label(text=f"In: {state.session_object_name}")
        session_obj = bpy.data.objects.get(state.session_object_name)
        if session_obj is not None:
            # Which mesh commits actually land in, and what's already in it: if
            # a retopology is visible in the viewport but this says 0 faces, it
            # belongs to a *different* result object (e.g. the source was
            # renamed/re-imported since) and can't be re-edited from here.
            result = bpy.data.objects.get(mesh_build.result_object_name_for(session_obj))
            if result is not None:
                done = state.committed_patch_count
                box.label(text=f"→ {result.name}: {len(result.data.polygons)} faces, "
                               f"{done} patch(es)", icon='OUTLINER_OB_MESH')
                if done:
                    box.label(text="Click a done patch to re-edit it", icon='FILE_REFRESH')
        _draw_surface_block(box, state)
        box.label(text=f"{keymap.describe('back')}: leave this object")
    else:
        box.label(text="Adjust & commit", icon='TOOL_SETTINGS')
        box.label(text=f"In: {state.session_object_name}")
        _draw_composite_patch(box, state)
    box.operator("retop.end_session", text="Stop Session", icon='X')


def _draw_surface_block(
    layout: bpy.types.UILayout, state: "state_mod.RetopPatchState"
) -> None:
    """The surfaces gathered for the next patch, while they are being picked.

    One Plasticity face is one patch, and a CAD model is cut into faces by the
    modelling history rather than by what wants a single grid across it. This
    is the line that says the gesture exists -- it is behind a modifier on a
    click, which is exactly the kind of thing nobody finds on their own.
    """
    selection = operators.surface_selection(state)
    if not selection:
        layout.label(
            text=f"{keymap.describe('add_surface')}: one patch, several surfaces")
        return

    picked = layout.box().column(align=True)
    picked.label(text=f"{len(selection)} surfaces picked", icon='SELECT_EXTEND')
    picked.label(text="Click any of them to open as one patch")
    picked.label(text=f"{keymap.describe('back')}: cancel")


def _draw_composite_patch(
    layout: bpy.types.UILayout, state: "state_mod.RetopPatchState"
) -> None:
    """How many CAD surfaces the patch being adjusted covers, when it covers
    more than one, and the way back out of that."""
    obj = bpy.data.objects.get(state.source_object_name or state.session_object_name)
    surfaces = operators.composite_surfaces(obj, state.active_face_id)
    if not surfaces:
        return
    composite = layout.column(align=True)
    composite.label(text=f"One patch over {len(surfaces)} surfaces",
                    icon='SELECT_EXTEND')
    composite.operator("retop.split_patch", text="Split Into Surfaces",
                       icon='MOD_EXPLODE')


def _draw_warnings(
    layout: bpy.types.UILayout,
    context: bpy.types.Context,
    obj: bpy.types.Object | None,
) -> None:
    """Conditions that make retopology silently wrong. Drawn on every tab: they
    only ever appear when something is actually broken.
    """
    # A result mesh whose source object is gone can't be re-edited: commits
    # for the renamed/re-imported object go to a new one, and the two show up
    # as overlapping surfaces.
    orphans = mesh_build.orphan_result_objects(context)
    if orphans:
        warn = layout.box().column(align=True)
        warn.alert = True
        warn.label(text="Retopology with no source object", icon='ERROR')
        for orphan in orphans[:4]:
            missing = orphan.name[:-len(mesh_build.RESULT_NAME_SUFFIX)]
            warn.label(text=f"{orphan.name} — '{missing}' is gone")
        warn.label(text="Rename it to <YourObject>_Retop to re-edit it.")

    # In a hand-edit the active object *is* the result mesh, on purpose. The
    # "this is the retopology of X, start a session on X" box below would be
    # answering a question nobody asked.
    if context.scene.plasticity_retop.session_phase == 'TWEAK':
        return

    if obj is not None and obj.type == 'MESH' and obj.data.get("face_ids"):
        _draw_group_integrity(layout, obj)
        _draw_dropped_composites(layout, obj)

    if obj is None or obj.type != 'MESH' or obj.data.get("face_ids"):
        return

    # Selecting the retopology and finding "no Plasticity face data" is a
    # non-answer: it never has any, and what the user means is the object it
    # was built from. Say so, and offer that.
    source = mesh_build.source_object_for_result(obj)
    if source is not None:
        info = layout.box().column(align=True)
        info.label(text=f"Retopology of '{source.name}'", icon='OUTLINER_OB_MESH')
        info.operator("retop.session", text=f"Retop {source.name}", icon='PLAY')
        return

    # Patch data lives in the mesh itself (mesh["face_ids"]/["groups"]), written
    # at import time -- the Plasticity bridge does NOT need to stay connected
    # afterwards. A mesh without it simply can't be retopped.
    warn = layout.box().column()
    warn.alert = True
    warn.label(text=f"'{obj.name}' has no Plasticity face data", icon='ERROR')
    warn.label(text="Re-import it through the bridge.")


def _draw_dropped_composites(
    layout: bpy.types.UILayout, obj: bpy.types.Object
) -> None:
    """Say so when a patch built from several surfaces no longer applies here.

    A re-export renumbers every Plasticity face id even when no vertex moves,
    so one recorded before it names surfaces that are gone. Silently ignoring
    it looks exactly like an addon that forgot, and the retopology that comes
    out is a patch per surface again -- correct geometry answering a question
    nobody asked.
    """
    dropped = patch_data.analyse(obj.data).dropped_composites
    if not dropped:
        return
    warn = layout.box().column(align=True)
    warn.alert = True
    warn.label(text=f"{len(dropped)} multi-surface patch(es) no longer apply",
               icon='ERROR')
    for line in _wrapped("Their surfaces are gone from this mesh — most likely it "
                         "was re-exported, which renumbers every face id. Pick them "
                         "again."):
        warn.label(text=line)


def _wrapped(text: str, width: int = 46) -> list[str]:
    """`text` broken into panel-width lines.

    A `label` does not wrap and the N-panel is narrow, so a sentence handed to
    one whole is a sentence read up to the edge and no further -- which for an
    explanation of what to change is the same as not showing it.
    """
    return textwrap.wrap(text, width) or [text]


def _draw_group_integrity(
    layout: bpy.types.UILayout, obj: bpy.types.Object
) -> None:
    """Say so when `groups`/`face_ids` no longer describe the mesh they are on.

    Shown to everyone, not behind Developer Mode, and only when something is
    actually wrong -- the same judgement the crack report makes. `polygon_face_ids`
    walks the group ranges positionally and cannot detect that they have stopped
    lining up with the polygons: it simply hands a polygon's triangles to
    whichever CAD face the range it falls in happens to name. Every patch in the
    file is then wrong, with nothing anywhere saying why, which is the one
    failure in the input contract worth reporting unasked.

    The usual cause is the mesh having been re-topologized after import --
    triangulated, decimated, joined, or a modifier applied -- since the bridge
    writes these once and nothing updates them afterwards.
    """
    report = cad_display.integrity(obj.data)
    if report.ok:
        return

    warn = layout.box().column(align=True)
    warn.alert = True
    warn.label(text="Patch data does not match this mesh", icon='ERROR')
    for problem in report.problems[:3]:
        warn.label(text=problem)
    if len(report.problems) > 3:
        warn.label(text=f"...and {len(report.problems) - 3} more")
    warn.label(text="Re-import it, or undo what changed the mesh.")
    warn.operator("retop.print_patch_data", icon='CONSOLE')


def _draw_active_patch(
    layout: bpy.types.UILayout, state: "state_mod.RetopPatchState"
) -> None:
    """The picked patch and its spans. Drawn above the tabs, not inside one:
    losing the span controls because you switched to the Display tab in the
    middle of an adjustment would be the panel fighting the workflow.
    """
    box = layout.box()
    if state.generator_name == constants.RING:
        box.label(text=f"Face {state.active_face_id} — Ring "
                       f"(2 loops, {state.num_sides} corners)")
    else:
        box.label(text=f"Face {state.active_face_id} — {state.generator_name} "
                       f"({state.num_sides} sides)")

    if state.generator_note:
        box.label(text=state.generator_note, icon='INFO')

    if state.corner_warning:
        warn = box.column(align=True)
        warn.alert = True
        warn.label(text="Corner detection is unsure here", icon='ERROR')
        warn.label(text=state.corner_warning)

    # The group editor keeps an unusable grouping rather than refusing it, so
    # the complaint has to outlive the editor: shown here whether or not it is
    # open, in the same block as the other standing warnings.
    if state.group_warning:
        warn = box.column(align=True)
        warn.alert = True
        warn.label(text="Side groups can't be built", icon='ERROR')
        for line in _wrapped(state.group_warning):
            warn.label(text=line)

    if state.num_loops > 2 and state.generator_name != constants.NGON:
        # More than one hole. The n-gon fill bridges every one of them; no span
        # generator paves anything but a single outline, so under one of those
        # the holes have been covered over and only N will get them back.
        warn = box.column(align=True)
        warn.alert = True
        warn.label(text=f"{state.num_loops} boundary loops — holes ignored", icon='ERROR')
        warn.label(text="Press N to fill it as an n-gon, holes and all."
                   if state.ngon_available
                   else f"N-gon would take them, but: {state.ngon_unavailable_reason}")

    if state.editing_committed:
        # Re-edit: the old patch has already been taken out of the result mesh,
        # and Discard puts it back untouched.
        info = box.column(align=True)
        info.label(text="Re-editing a committed patch", icon='FILE_REFRESH')
        if state.reedit_removed_faces:
            info.label(text=f"Old patch removed ({state.reedit_removed_faces} faces)")
            info.label(text="Discard restores it. Neighbours keep their spans.")
            delete = box.row(align=True)
            delete.alert = True
            delete.operator("retop.delete_patch", text="Delete Patch (X)", icon='TRASH')
        else:
            # Nothing was found to remove: committing would leave the old
            # geometry in place, overlapping the new grid.
            info.alert = True
            info.label(text="Could not find its old faces to remove", icon='ERROR')
            info.label(text="Committing will overlap the existing surface.")

    _draw_match_block(box, state)

    # N-gon is a mode, not a generator the side count selects, so it gets its
    # own toggle here rather than appearing in the list of patch types.
    mode_row = box.row(align=True)
    mode_row.enabled = state.ngon_available or state.ngon_mode
    mode_row.prop(state, "ngon_mode", text="N-gon (N)", toggle=True, icon='MESH_PLANE')
    if not state.ngon_available:
        note = box.column(align=True)
        note.label(text=f"N-gon unavailable: {state.ngon_unavailable_reason}", icon='INFO')

    if state.ngon_mode and state.ngon_available:
        # `ngon_angle` and nothing else: it is this patch's resolution, the
        # thing Ctrl+wheel drives, and it belongs beside the commit button for
        # the same reason a span does. The vertex dots are a *display*
        # setting -- they change nothing about the mesh -- and they already sit
        # in the settings tab, so having them here too made the patch block
        # look as though the mode changed more than it does.
        box.prop(state, "ngon_angle")
        if state.num_loops > 1:
            holes = state.num_loops - 1
            box.label(text=f"{holes} hole{'s' if holes > 1 else ''} bridged "
                           f"({state.num_loops} n-gons)", icon='MESH_TORUS')
        row = box.row(align=True)
        row.operator("retop.commit_patch",
                     text="Replace" if state.editing_committed else "Commit",
                     icon='CHECKMARK')
        row.operator("retop.clear_preview", text="Discard", icon='X')
        return

    col = box.column(align=True)
    if state.generator_name in operators.TWO_SPAN_GENERATORS:
        u_label, v_label = constants.span_labels(state.generator_name)
        col.prop(state, "span_u", text=u_label)
        col.prop(state, "span_v", text=v_label)
        row = box.row(align=True)
        row.label(text="Ctrl+wheel/keys adjust:")
        row.prop(state, "span_axis", expand=True)
    else:
        col.prop(state, "span")

    box.prop(state, "reproject")
    relax = box.row()
    # The relaxation is a step-then-project loop, so with reprojection off there
    # is no surface to land on and it would pull the interior inside the patch.
    # Greyed out rather than quietly ignored.
    relax.enabled = state.reproject
    relax.prop(state, "relax_iterations")

    row = box.row(align=True)
    row.operator("retop.commit_patch",
                 text="Replace" if state.editing_committed else "Commit",
                 icon='CHECKMARK')
    row.operator("retop.clear_preview", text="Discard", icon='X')


def _draw_match_block(
    box: bpy.types.UILayout, state: "state_mod.RetopPatchState"
) -> None:
    """Matching a committed neighbour's density along a shared side."""
    references = sidematch.active_sides()
    available = [reference for reference in references if reference.available]
    applied = [reference for reference in references if reference.applied]
    # The red ones: retopology across them, and this patch not welding to it.
    # Counted separately from `available` because that is the number worth
    # acting on -- "could be matched" includes every side already matched.
    cracks = [reference for reference in available if not reference.applied]

    row = box.row(align=True)
    row.operator("retop.toggle_match_mode",
                 text=f"Side Highlight ({keymap.describe('match_mode')})",
                 icon='SNAP_EDGE', depress=state.match_mode)
    if not references:
        return

    # What is *being* matched comes first, because it is what the preview is
    # made of. Then the count that is a problem: a side bordering a committed
    # patch and not reproducing it is a crack, and it is the only number here
    # anyone can act on.
    box.label(text=f"{len(applied)} of {len(references)} sides matched",
              icon='CHECKMARK' if applied else 'INFO')
    if cracks:
        alert = box.column(align=True)
        alert.alert = True
        alert.label(text=f"{len(cracks)} side(s) border a committed patch unmatched",
                    icon='ERROR')

    hovered = None
    if 0 <= state.hovered_side < len(references):
        hovered = references[state.hovered_side]
    if hovered is not None:
        # The same words the viewport tooltip uses: one wording for one state,
        # or the panel and the overlay disagree about the side under the cursor.
        title, detail = sidematch.status_of(
            hovered, sidematch.side_override_map(state).get(hovered.index))
        note = box.column(align=True)
        # Red in the panel for the same state the viewport paints red: a side
        # that borders finished retopology and is not welding to it. A side
        # with nothing across it is normal, and alerting on it -- which is what
        # this did -- made the ordinary case look like the broken one.
        note.alert = hovered.available and not hovered.applied
        note.label(text=title,
                   icon='CHECKMARK' if hovered.applied else 'INFO')
        note.label(text=detail)

    if not available and not applied:
        box.label(text="No side borders a committed patch", icon='INFO')
    elif state.match_mode:
        box.label(text="Click a side to match it", icon='EYEDROPPER')

    pinned = _pinned_sides(state)
    if pinned:
        box.label(text=f"{len(pinned)} side(s) matched by hand", icon='CHECKMARK')
    released = _released_sides(state)
    if released:
        box.label(text=f"{len(released)} side(s) released by hand", icon='X')

    if state.match_conflicts and state.generator_name != constants.NGON:
        # A grid has one span per *direction*, so two sides wanting different
        # counts along the same axis cannot both be honoured. Only the winner is
        # substituted; say so, since nothing in the viewport shows which lost.
        note = box.column(align=True)
        note.alert = True
        note.label(text=f"{state.match_conflicts} side(s) outvoted", icon='ERROR')
        note.label(text="A grid has one span per direction.")


def _pinned_sides(state: "state_mod.RetopPatchState") -> list[int]:
    """Flat side indices the user has matched by hand on this patch.

    Not the released ones: an entry saying "leave this side alone" is stored the
    same way, and counting it as a hand-matched side would report the opposite
    of what the user just did.
    """
    return sorted(index for index, kind in sidematch.side_override_map(state).items()
                  if kind != sidematch.PIN_EXCLUDED)


def _released_sides(state: "state_mod.RetopPatchState") -> list[int]:
    return sorted(index for index, kind in sidematch.side_override_map(state).items()
                  if kind == sidematch.PIN_EXCLUDED)


def _draw_matching_settings(
    body: bpy.types.UILayout, state: "state_mod.RetopPatchState"
) -> None:
    body.prop(state, "auto_match_neighbours")
    body.prop(state, "match_margin")


def _draw_tweak_settings(
    body: bpy.types.UILayout, state: "state_mod.RetopPatchState"
) -> None:
    """How the hand-edit round trip sets Blender's tool settings up.

    Read on the way *in*, so changing one mid-edit does nothing until the next
    trip -- said in the panel rather than left to be discovered.
    """
    body.separator()
    body.label(text="Hand-Edit Setup", icon='SNAP_VERTEX')
    body.prop(state, "tweak_auto_merge")
    body.prop(state, "tweak_merge_distance")
    body.prop(state, "tweak_snap_surface")
    # Not part of the tool-settings snapshot: it is this addon's own draw flag
    # on its own object, so it takes effect at once rather than on the next
    # trip -- and it moves nothing, which is the whole point of it.
    body.separator()
    body.prop(state, "tweak_draw_in_front")


def _draw_tab_patch(
    layout: bpy.types.UILayout, state: "state_mod.RetopPatchState"
) -> None:
    body = _section(layout, state, "show_patch_settings",
                    "Patch Settings", icon='MOD_MESHDEFORM')
    if body:
        _draw_patch_settings(body.column(), state)

    body = _section(layout, state, "show_ngon_settings", "N-gon Mode", icon='MESH_PLANE')
    if body:
        _draw_ngon_settings(body.column(), state)


def _draw_patch_settings(
    body: bpy.types.UILayout, state: "state_mod.RetopPatchState"
) -> None:
    body.label(text="Starting Resolution")
    body.row(align=True).prop(state, "resolution", expand=True)
    body.separator()
    body.label(text="Corner Detection")
    # Which *test* finds the corners is not a choice anyone has a signal to
    # make, so it is a developer's knob rather than a setting. Measured across
    # the whole fixture at MID: the method changes the result on two objects
    # out of sixteen in either mode, and on those two the shipped default is
    # the better one -- for grids, Angle keeps Cube Bevel Edges at 0.221%
    # deviation where the others fan two of its faces into N-Sides and take it
    # to 0.838%; for n-gons, Both closes all 91 of Carved Rounded Slot's open
    # edges that Angle leaves. And Both and Topology are *indistinguishable* on
    # every object of the fixture in both modes -- three values, two outcomes,
    # and each mode already ships with the right one.
    if prefs.developer_mode():
        col = body.column(align=True)
        col.label(text="Grid generators:")
        col.row(align=True).prop(state, "corner_method_spans", expand=True)
        col.separator()
        col.label(text="N-gon mode:")
        col.row(align=True).prop(state, "corner_method_ngon", expand=True)
    elif (state.corner_method_spans != 'ANGLE'
            or state.corner_method_ngon != 'BOTH'):
        # Hidden, but the value is stored on the scene: a file already carrying
        # a non-default would otherwise be stuck on it with no control to reach.
        warn = body.column(align=True)
        warn.alert = True
        warn.label(text="Corner detection is not on its defaults", icon='ERROR')
        warn.operator("retop.reset_corner_methods", icon='LOOP_BACK')
    # Always live: TOPOLOGY falls back to the angle test on a boundary with no
    # junction, so the threshold means something whichever method is set.
    body.prop(state, "corner_angle_threshold")
    body.prop(state, "small_side_tolerance")
    body.prop(state, "boundary_weld_distance")


def _draw_ngon_settings(
    body: bpy.types.UILayout, state: "state_mod.RetopPatchState"
) -> None:
    body.prop(state, "ngon_mode", text="N-gon by default")
    body.prop(state, "ngon_angle")
    body.prop(state, "ngon_planar_tolerance")
    body.separator()
    body.prop(state, "ngon_show_verts")
    sub_dots = body.column()
    sub_dots.enabled = state.ngon_show_verts
    sub_dots.prop(state, "ngon_vert_size")
    body.separator()


def _draw_tab_picker(
    layout: bpy.types.UILayout, state: "state_mod.RetopPatchState"
) -> None:
    body = layout.box().column()
    body.label(text="Picker Settings", icon='RESTRICT_SELECT_OFF')
    body.separator()
    body.prop(state, "length_unit")
    body.separator()
    body.label(text="Matching", icon='SNAP_EDGE')
    _draw_matching_settings(body, state)
    body.separator()
    body.prop(state, "pick_depth_tolerance")
    body.prop(state, "pick_max_distance")
    # Filed here rather than under Patch: this is the manual half of matching
    # -- what you reach for when a side could not be matched automatically.
    _draw_tweak_settings(body, state)


def _draw_tab_display(
    layout: bpy.types.UILayout,
    state: "state_mod.RetopPatchState",
    obj: bpy.types.Object | None,
) -> None:
    result_obj = None
    if obj is not None and obj.type == 'MESH':
        result_obj = bpy.data.objects.get(mesh_build.result_object_name_for(obj))

    # Preview and result in one section, under one offset. They are not two
    # settings: the preview's lift *is* the result's, times a fixed margin that
    # keeps the patch being built above its committed neighbours.
    body = _section(layout, state, "show_appearance",
                    "Appearance", icon='SHADING_RENDERED')
    if body:
        body.prop(state, "result_offset", text="General Offset")

        body.label(text="Preview geometry", icon='SHADING_RENDERED')
        body.prop(state, "preview_color", text="Color")
        body.prop(state, "preview_alpha", slider=True, text="Alpha")

        body.separator()
        body.label(text="Result geometry", icon='SHADING_SOLID')
        body.prop(state, "result_color", text="Color")
        body.prop(state, "result_alpha", slider=True, text="Alpha")

        body.separator()
        body.prop(state, "result_see_through")
        body.prop(state, "result_show_wire")
        sub_wire = body.column()
        sub_wire.enabled = state.result_show_wire
        sub_wire.prop(state, "result_wire_opacity", slider=True)
        # Blender has no per-object wireframe opacity: this drives the
        # viewport's own overlay setting, so say so rather than let it look
        # like a per-object one.
        body.separator()
        body.prop(state, "highlight_all_results")
        sub = body.row()
        sub.enabled = state.highlight_all_results
        sub.prop(state, "inactive_result_alpha", slider=True)

    _draw_cad_display(layout, state)

    box = layout.box().column()
    box.label(text="Keybind Overlay", icon='EVENT_A')
    box.prop(state, "overlay_scale")

    box = layout.box().column()
    box.label(text="Isolate  ( / )", icon='ZOOM_SELECTED')
    box.prop(state, "local_view_include_retop")


def _draw_cad_display(
    layout: bpy.types.UILayout, state: "state_mod.RetopPatchState"
) -> None:
    """The Plasticity structure drawn over the source surface.

    Its own block rather than a line inside Preview Appearance: this describes
    the *CAD model*, not the retopology, and it is the one display you turn on
    before picking anything.
    """
    box = _section(layout, state, "show_cad_structure",
                   "Plasticity Structure", icon='MOD_WIREFRAME')
    if box is None:
        return
    box = box.column()
    box.separator()

    box.prop(state, "show_cad_edges", text="CAD Edges (E)", toggle=False)
    sub_edges = box.column(align=True)
    sub_edges.enabled = state.show_cad_edges
    sub_edges.prop(state, "cad_edge_color", text="")
    sub_edges.prop(state, "cad_edge_width")
    sub_edges.prop(state, "show_brep_vertices")

    box.separator()
    box.prop(state, "show_surface_flow", text="Surface Flow (Ctrl+E)")
    sub_flow = box.column(align=True)
    sub_flow.enabled = state.show_surface_flow
    sub_flow.prop(state, "flow_color", text="")
    sub_flow.prop(state, "flow_density")
    box.separator()
    box.prop(state, "cad_display_xray")
    box.separator()
    box.label(text="Show for:")
    box.row(align=True).prop(state, "cad_display_scope", expand=True)

    # Filed under the CAD structure because it is drawn *on* a CAD edge -- the
    # border two patches share -- and not under matching, which is about the
    # patch currently open. This one is about work already committed.
    box.separator()
    box.prop(state, "show_cracks")
    sub_cracks = box.column(align=True)
    sub_cracks.enabled = state.show_cracks
    sub_cracks.prop(state, "crack_color", text="")


def _draw_mirror(
    layout: bpy.types.UILayout,
    context: bpy.types.Context,
    state: "state_mod.RetopPatchState",
) -> None:
    """Symmetry on the committed mesh.

    Which axes are on is read off the Mirror modifier rather than a scene
    property: they belong to one object. So this block describes whatever
    object the session (or the selection) currently resolves to, and says which
    one that is.
    """
    body = layout.box().column()
    body.label(text="Mirror", icon='MOD_MIRROR')
    body.separator()

    source, result = mesh_build.mirror_target(context)
    if result is None:
        body.label(text="Nothing committed to mirror yet", icon='INFO')
        if source is not None:
            body.label(text=f"Would apply to: {source.name}")
        return

    body.label(text=f"On: {result.name}")
    axes = mesh_build.mirror_axes(result)
    row = body.row(align=True)
    for axis, enabled in zip(mesh_build.MIRROR_AXES, axes):
        # depress, not a checkbox: these are operator buttons, and the pressed
        # look is the only way to show state on one.
        row.operator("retop.mirror_axis", text=axis, depress=enabled).axis = axis
    body.label(text=f"{keymap.describe('mirror')}, then X / Y / Z", icon='EVENT_A')

    if not any(axes):
        return

    body.separator()
    body.prop(state, "mirror_clip")
    body.prop(state, "mirror_merge_distance")
    body.operator("retop.apply_mirror", text="Apply Mirror", icon='CHECKMARK')


def _draw_tab_output(
    layout: bpy.types.UILayout,
    context: bpy.types.Context,
    state: "state_mod.RetopPatchState",
) -> None:
    _draw_mirror(layout, context, state)

    body = layout.box().column()
    body.label(text="Shading", icon='SHADING_SOLID')
    body.separator()
    body.prop(state, "result_shade_smooth")
    sub_sharp = body.column()
    sub_sharp.enabled = state.result_shade_smooth
    sub_sharp.prop(state, "sharp_edge_angle")

    body = layout.box().column()
    body.label(text="Collections", icon='OUTLINER_COLLECTION')
    body.separator()
    body.prop(state, "mirror_source_collections")


def _draw_tab_keys(
    layout: bpy.types.UILayout, state: "state_mod.RetopPatchState"
) -> None:
    """Where the keys are, not the keys themselves.

    The keys are real KeyMapItems, so the widget that edits them already exists: Blender's
    own rows, on the addon's preferences page. This tab points at it and then
    lists only what is *not* remappable, which is the part no editor would
    show.
    """
    box = layout.box().column(align=True)
    box.label(text="Keybinds", icon='EVENT_A')
    box.separator()
    box.operator("retop.open_keymap_prefs", text="Edit Keybinds…",
                 icon='PREFERENCES')
    box.label(text="Also: Preferences > Keymap > Add-ons")
    box.separator()
    # Which keys are live when is the first thing anyone asks after a key of
    # theirs stops working, so it is said here rather than only in the manual.
    box.label(text="Keys are live only during a session", icon='INFO')
    box.label(text="Outside one they fall through to Blender")
    box.label(text="and to other addons (Hard Ops' Alt+X)")


def _draw_patch_debug(
    layout: bpy.types.UILayout,
    state: "state_mod.RetopPatchState",
    obj: bpy.types.Object | None,
) -> None:
    """The raw bridge numbers, for when a patch is not the face you expected.

    Behind Developer Mode: this describes the *input* -- `mesh["groups"]` and
    `mesh["face_ids"]`, in the bridge's own terms -- and nothing about it is
    actionable while retopologizing. Someone who installed a release zip has no
    use for a loop index. The integrity warning is the half of this that is
    everyone's, and it is drawn with the other warnings rather than here.
    """
    body = layout.box().column()
    body.label(text="Patch Data", icon='MESH_DATA')
    body.separator()

    if obj is None or obj.type != 'MESH' or not obj.data.get("face_ids"):
        body.label(text="Select a Plasticity mesh", icon='INFO')
        return

    body.prop(state, "debug_patch_ids")
    sub = body.column(align=True)
    sub.enabled = state.debug_patch_ids
    sub.prop(state, "debug_patch_scope")
    sub.prop(state, "debug_patch_detail")
    cull = sub.row()
    # A hovered patch is the one the cursor is on, so it is in front by
    # construction and the overlay ignores this. Greyed out rather than left
    # live: a checkbox that does nothing reads as a broken one.
    cull.enabled = state.debug_patch_scope != 'HOVER'
    cull.prop(state, "debug_patch_cull")

    report = cad_display.integrity(obj.data)
    body.separator()
    body.label(text=obj.name, icon='OUTLINER_OB_MESH')
    body.label(text=f"{len(report.entries)} patches · {report.loop_total} loops")

    # Whether the bridge's Triangulate option was on. Reported, never enforced:
    # nothing here needs triangles -- the group walk is in loop-index space and
    # the BVH fan-triangulates whatever it is handed -- so this is here to
    # answer "what am I actually looking at", not to ask for a re-export.
    sizes = sorted(report.polygon_sizes.items())
    if report.triangulated:
        body.label(text="Triangulated", icon='MESH_DATA')
    else:
        body.label(text="Not triangulated: "
                        + ", ".join(f"{n}-gon x{count}" for n, count in sizes),
                   icon='INFO')

    body.separator()
    if not state.debug_patch_ids:
        body.label(text="Switch it on to read a patch", icon='INFO')
    elif state.debug_patch_scope == 'HOVER':
        _draw_patch_debug_hover(body)
    elif state.debug_patch_scope == 'SELECTED':
        _draw_patch_debug_selected(body, obj, report)
    else:
        body.label(text=f"Labelling all {len(report.entries)} patches")

    body.separator()
    body.operator("retop.print_patch_data", icon='CONSOLE')


def _draw_patch_debug_hover(layout: bpy.types.UILayout) -> None:
    """The patch under the cursor, named here as well as over the surface.

    The panel is a region of the same VIEW_3D area the hover modal tags for
    redraw, so this follows the cursor for free -- and it is worth having beside
    the viewport label rather than instead of it: the label has to stay terse to
    sit on a surface, while the question being asked is often "is this even the
    object I think it is", which wants a name.
    """
    hovered = overlay.debug_hover
    if hovered is None:
        layout.label(text="Point at a patch in the viewport",
                     icon='RESTRICT_SELECT_ON')
        layout.label(text="Object Mode or Edit Mode -- either.")
        return

    name, face_id = hovered
    hovered_obj = bpy.data.objects.get(name)
    if hovered_obj is None or hovered_obj.type != 'MESH':
        return  # gone since the last mouse move; the next one will say so

    table = layout.box().column(align=True)
    table.label(text=hovered_obj.name, icon='OUTLINER_OB_MESH')
    entry = next((e for e in cad_display.integrity(hovered_obj.data).entries
                  if e.face_id == face_id), None)
    if entry is None:
        # The groups name no such face id. `_draw_group_integrity` is already
        # saying why, so this only has to not look like an empty box.
        table.label(text=f"#{face_id}: no group for it", icon='ERROR')
        return
    table.label(text=f"#{entry.face_id}")
    table.label(text=f"loop {entry.loop_start}+{entry.loop_count}")
    table.label(text=f"{entry.poly_count} polygons")


def _draw_patch_debug_selected(
    layout: bpy.types.UILayout,
    obj: bpy.types.Object,
    report: "patch_data.GroupReport",
) -> None:
    wanted = cad_display.selected_face_ids(obj.data)
    selected = [entry for entry in report.entries if entry.face_id in wanted]
    if not selected:
        # Selection flags are only written back to the mesh on leaving Edit
        # Mode, so a patch picked with L reads as nothing at all until then --
        # which is the whole reason Hover is the default. Saying so beats an
        # empty box that looks broken.
        layout.label(text="No patch selected", icon='RESTRICT_SELECT_ON')
        layout.label(text="Select in Object Mode, or use Hover.")
        return
    layout.label(text=f"Selected ({len(selected)}):")
    table = layout.box().column(align=True)
    for entry in selected[:8]:
        table.label(text=f"#{entry.face_id} · loop {entry.loop_start}"
                         f"+{entry.loop_count} · {entry.poly_count} poly")
    if len(selected) > 8:
        table.label(text=f"...and {len(selected) - 8} more")


def _draw_tab_system(
    layout: bpy.types.UILayout,
    state: "state_mod.RetopPatchState",
    obj: bpy.types.Object | None,
) -> None:
    body = layout.box().column()
    body.label(text="System", icon='PREFERENCES')
    body.separator()
    body.label(text=f"Version {version.ADDON_VERSION}")
    body.label(text=f"Build {version.BUILD_ID}")
    body.separator()
    # The reload button is a *developer's* affordance and is hidden unless the
    # preference says so. Installed from a release zip there is nothing to
    # reload against: the way to get new code is to install the new zip. Left
    # on for everyone, it reads as a fix-it button for any misbehaviour, which
    # is exactly what it is not. Blender's own global Reload Scripts is not
    # offered beside it: it can silently half-fail when another installed addon
    # errors during its own reload, which is the one failure the version string
    # cannot report.
    if prefs.developer_mode():
        body.operator("retop.reload_addon", text="Reload Addon Only", icon='FILE_REFRESH')
    else:
        body.label(text="Reload button: Preferences > Add-ons > Developer Mode")

    if prefs.developer_mode():
        _draw_patch_debug(layout, state, obj)


def _draw_tab_bridge(
    layout: bpy.types.UILayout,
    context: bpy.types.Context,
) -> None:
    """The Plasticity bridge's own panel, drawn here.

    Delegated, never rebuilt: `bridge.draw_bridge_panel` calls the bridge's own
    `PlasticityPanel.draw` with this layout, so Connect / Server / Disconnect /
    Refresh / Refacet are the rows that addon draws, and they follow its
    updates without anything here being touched. Rebuilding them would be a
    second, worse copy of a UI whose author is free to change it.

    The version note is the only thing this addon adds, and it is deliberately
    the whole of the "are the bridge's settings right" question for now: the
    facet settings are not checkable against anything, because this addon reads
    a triangulated and an n-gon export alike (`patch_data.group_report` reports
    which, and requires neither). A warning with no criterion behind it would
    send people re-exporting to fix something that was never broken.
    """
    body = layout.box().column()
    body.label(text="Plasticity Bridge", icon='EVENT_P')

    status = bridge.status()
    if not status.installed:
        body.separator()
        col = body.column(align=True)
        for line in _wrapped(
            "The Plasticity bridge addon was not found. It is what imports the "
            "meshes this addon retopologizes -- install and enable it, and its "
            "panel appears here."
        ):
            col.label(text=line)
        col.label(text="github.com/nkallen/plasticity-blender-addon", icon='URL')
        return

    note = bridge.version_note()
    if note:
        warn = body.box().column(align=True)
        # Untested is not broken, so this informs rather than alerts. The one
        # thing it must do is be readable on the day a patch lands on the wrong
        # face, which is what a changed import contract looks like from here.
        warn.label(text="Bridge version", icon='INFO')
        for line in _wrapped(note):
            warn.label(text=line)

    body.separator()
    failed = bridge.draw_bridge_panel(body, context)
    if failed:
        alert = body.box().column(align=True)
        alert.alert = True
        for line in _wrapped(failed):
            alert.label(text=line)
        alert.label(text=f"Retop is tested against bridge "
                         f"{bridge.version_string(bridge.TESTED_VERSION)}.")


class VIEW3D_PT_retop(bpy.types.Panel):
    bl_label = "Retop"
    bl_idname = "VIEW3D_PT_retop"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Retop"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        state = context.scene.plasticity_retop
        obj = context.active_object

        # Version stays visible on every tab: it's the only reliable way to
        # tell whether a deploy/reload actually took.
        layout.label(text=f"v{version.ADDON_VERSION}  ·  build {version.BUILD_ID}",
                     icon='EXPERIMENTAL')

        # ... and this is the other half of that: the line above is what Python
        # holds in memory, which a deploy does not change. Without saying so,
        # running yesterday's code looks exactly like a broken feature -- and
        # the tracebacks it produces point at line numbers that don't match the
        # file you're reading.
        # Only a checkout can *be* stale: an installed zip has one copy of the
        # code and nothing writes over it between reloads.
        stale = version.stale_load() if prefs.developer_mode() else None
        if stale:
            warn = layout.box().column(align=True)
            warn.alert = True
            warn.label(text="Running older code than is deployed", icon='ERROR')
            warn.label(text=f"On disk: v{stale[0]} · build {stale[1]}")
            warn.operator("retop.reload_addon", text="Reload Addon Only",
                          icon='FILE_REFRESH')
            warn.label(text="If this stays, restart Blender.")

        # --- always on: what the session is doing right now ---
        _draw_session(layout, context, state)
        _draw_warnings(layout, context, obj)
        # The active patch never moves into a tab: losing the span controls
        # because you switched tabs mid-adjustment would fight the workflow.
        if state.active_face_id != -1:
            _draw_active_patch(layout, state)

        # --- settings, one tab at a time ---
        layout.separator()
        row = layout.row(align=True)
        row.scale_x = 1.4
        row.scale_y = 1.4
        row.prop(state, "ui_tab", expand=True, icon_only=True)

        tab = state.ui_tab
        if tab == 'PATCH':
            _draw_tab_patch(layout, state)
        elif tab == 'PICKER':
            _draw_tab_picker(layout, state)
        elif tab == 'DISPLAY':
            _draw_tab_display(layout, state, obj)
        elif tab == 'OUTPUT':
            _draw_tab_output(layout, context, state)
        elif tab == 'KEYS':
            _draw_tab_keys(layout, state)
        elif tab == 'SYSTEM':
            _draw_tab_system(layout, state, obj)
        elif tab == 'BRIDGE':
            _draw_tab_bridge(layout, context)


CLASSES = (VIEW3D_PT_retop,)


def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)


# Teardown has to survive a partial registration. `__init__.register` unwinds
# the modules that took when a later one fails, so this can be handed classes
# that never registered -- and an exception here would replace the one saying
# why registration failed with one about the cleanup.
def _drop(cls) -> None:
    try:
        bpy.utils.unregister_class(cls)
    except Exception:
        pass


def unregister() -> None:
    for cls in reversed(CLASSES):
        _drop(cls)
