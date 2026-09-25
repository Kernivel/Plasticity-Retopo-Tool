"""Hand-correcting the committed retopology, in Blender's own Edit Mode.

`Tab` in the `PATCH` or `OBJECT` phase selects `<Source>_Retop`, sets up the
tool settings for manual retopology and enters Edit Mode. `Tab` again comes
back. The addon owns the two ends of that trip:

- the setup: vertex snapping onto the same mesh, auto-merge;
- the repair on the way out (`mesh_build.repair_manual_edits`): faces with no
  patch id and vertices with a copied corner id.

Never from `ADJUST`: a re-edit's faces are out of the result mesh, and edits
made while Blender holds it in Edit Mode would lose them.
"""
import json

import bpy

from . import mesh_build
from . import state as state_mod


# Tool settings this mode overwrites, and has to put back. Accessed by name
# through getattr/setattr, since they get renamed across Blender versions.
# A missing name is skipped both ways.
_SNAPSHOT_KEYS = (
    "use_snap",
    "snap_elements",
    "snap_target",
    "use_snap_self",
    "use_snap_align_rotation",
    "use_snap_backface_culling",
    "use_mesh_automerge",
    "double_threshold",
    "mesh_select_mode",
)


def _wanted_settings(
    state: "state_mod.RetopPatchState",
) -> dict[str, object]:
    """The tool settings a manual retopology pass wants.

    - VERTEX snapping with `use_snap_self`, to drag a vertex onto its twin in
      the same mesh.
    - Auto-merge, so a seam closes without a separate Merge by Distance.
    - FACE_NEAREST when `tweak_snap_surface` is on, to stay on the CAD surface.
    """
    elements = {'VERTEX'}
    if state.tweak_snap_surface:
        elements.add('FACE_NEAREST')
    return {
        "use_snap": True,
        "snap_elements": elements,
        "snap_target": 'CLOSEST',
        "use_snap_self": True,
        "use_snap_align_rotation": False,
        "use_mesh_automerge": bool(state.tweak_auto_merge),
        "double_threshold": state_mod.to_blender_units(
            state, state.tweak_merge_distance),
        "mesh_select_mode": (True, False, False),
    }


def _jsonable(value: object) -> object:
    """Sets and bpy's own sequence types don't survive json.dumps."""
    if isinstance(value, (set, frozenset)):
        return {"__set__": sorted(str(item) for item in value)}
    if isinstance(value, (tuple, list)):
        return list(value)
    if hasattr(value, "__len__") and not isinstance(value, (str, bytes)):
        return list(value)  # bpy_prop_array (mesh_select_mode)
    return value


def _from_jsonable(value: object) -> object:
    if isinstance(value, dict) and "__set__" in value:
        return set(value["__set__"])
    return value


def snapshot_tool_settings(context: bpy.types.Context) -> dict[str, object]:
    """The values `_wanted_settings` is about to overwrite."""
    tool_settings = context.scene.tool_settings
    saved: dict[str, object] = {}
    for key in _SNAPSHOT_KEYS:
        if hasattr(tool_settings, key):
            saved[key] = _jsonable(getattr(tool_settings, key))
    return saved


def apply_tool_settings(
    context: bpy.types.Context, values: dict[str, object]
) -> None:
    tool_settings = context.scene.tool_settings
    for key, value in values.items():
        if not hasattr(tool_settings, key):
            continue
        try:
            setattr(tool_settings, key, _from_jsonable(value))
        except (TypeError, ValueError):
            # An enum item this Blender doesn't have: skip it.
            pass


def _session_source(context: bpy.types.Context) -> bpy.types.Object | None:
    """The object whose retopology Tab should open.

    The session's object while one is entered. Otherwise the selection,
    active object first, where `<X>_Retop` means X.
    """
    state = context.scene.plasticity_retop
    entered = bpy.data.objects.get(state.session_object_name)
    if entered is not None:
        return entered

    candidates = [context.view_layer.objects.active]
    candidates += [obj for obj in context.selected_objects]
    for obj in candidates:
        if obj is None:
            continue
        source = mesh_build.source_object_for_result(obj) or obj
        if bpy.data.objects.get(mesh_build.result_object_name_for(source)):
            return source
    return None


def _result_object_for_session(
    context: bpy.types.Context,
) -> tuple[bpy.types.Object | None, bpy.types.Object | None, str | None]:
    """(source, result, error). Both objects or an error, never a mix."""
    source = _session_source(context)
    if source is None:
        return None, None, "Select the object whose retopology you want to edit"
    result = bpy.data.objects.get(mesh_build.result_object_name_for(source))
    if result is None or len(result.data.polygons) == 0:
        return source, None, (f"Nothing to hand-edit yet: '{source.name}' has no "
                              f"committed patch")
    return source, result, None


def can_tweak(context: bpy.types.Context) -> str | None:
    """The reason Tab would refuse right now, or None if it would open.

    Shared by the panel and the modal, so both give the same answer.
    """
    state = context.scene.plasticity_retop
    if not state.session_active:
        return "No retop session is running"
    if state.session_phase not in ('PATCH', 'OBJECT'):
        # Never from ADJUST: a re-edit's faces are out of the result mesh.
        return "Commit or discard the patch first"
    if context.mode != 'OBJECT':
        return "Blender is not in Object Mode"
    _source, _result, error = _result_object_for_session(context)
    return error


def enter_tweak(context: bpy.types.Context) -> str | None:
    """Open Edit Mode on the session's result mesh. Returns an error message
    when it could not, else None (and the phase is left on 'TWEAK').

    Creates no datablock, so it is safe between undo steps.
    """
    state = context.scene.plasticity_retop
    if context.mode != 'OBJECT':
        return "Blender is not in Object Mode"

    source, result, error = _result_object_for_session(context)
    if error is not None or result is None:
        return error

    # A hidden object cannot enter Edit Mode. Per-view-layer flags only.
    result.hide_viewport = False
    try:
        result.hide_set(False)
    except RuntimeError:
        pass  # not in this view layer; the select below reports it properly

    # Empty the preview: only the committed mesh should be on screen.
    mesh_build.clear_preview_object()

    previous_active = context.view_layer.objects.active
    state.tweak_return_object = previous_active.name if previous_active else ""

    for obj in list(context.selected_objects):
        obj.select_set(False)
    try:
        result.select_set(True)
    except RuntimeError:
        return f"'{result.name}' is not in the current view layer"
    context.view_layer.objects.active = result

    # Which object this trip is about, and which phase to return to.
    # `repair_manual_edits` needs the object on the way out.
    state.tweak_source_object = source.name
    state.tweak_return_phase = state.session_phase

    state.tweak_saved_tool_settings = json.dumps(snapshot_tool_settings(context))
    apply_tool_settings(context, _wanted_settings(state))

    # Draw the retopology in front for the trip. Set on the object directly:
    # refresh_result_appearance writes mesh data, which Edit Mode would own.
    if state.tweak_draw_in_front:
        result.show_in_front = True

    try:
        bpy.ops.object.mode_set(mode='EDIT')
    except RuntimeError as exc:
        restore_tool_settings(context)
        return f"Could not enter Edit Mode: {exc}"

    state.session_phase = 'TWEAK'
    return None


def restore_tool_settings(context: bpy.types.Context) -> None:
    """Put back whatever `enter_tweak` overwrote, and forget the snapshot.

    Separate from `exit_tweak`: entering can fail after the settings were
    applied.
    """
    state = context.scene.plasticity_retop
    raw = state.tweak_saved_tool_settings
    state.tweak_saved_tool_settings = ""
    if not raw:
        return
    try:
        saved = json.loads(raw)
    except ValueError:
        return
    apply_tool_settings(context, saved)


def exit_tweak(context: bpy.types.Context) -> tuple[int, int]:
    """Leave Edit Mode, restore the tool settings and repair the bookkeeping
    the hand edits invalidated. Returns (faces adopted, source ids cleared).

    Safe when Blender already left Edit Mode by another route. The repair runs
    once per trip, however it ended.
    """
    state = context.scene.plasticity_retop
    if context.mode != 'OBJECT':
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
        except RuntimeError:
            pass

    restore_tool_settings(context)

    repaired = (0, 0)
    source = (bpy.data.objects.get(state.tweak_source_object)
              or bpy.data.objects.get(state.session_object_name))
    state.tweak_source_object = ""
    if source is not None:
        repaired = mesh_build.repair_manual_edits(context, source)

    # Reselect the object the session is about.
    returning = bpy.data.objects.get(state.tweak_return_object) or source
    state.tweak_return_object = ""
    if returning is not None:
        try:
            returning.select_set(True)
            context.view_layer.objects.active = returning
        except RuntimeError:
            pass

    # Back to the phase the trip started from.
    state.session_phase = state.tweak_return_phase or 'PATCH'
    state.tweak_return_phase = ""
    # Back in Object Mode: `show_in_front` follows `result_see_through` again.
    mesh_build.refresh_result_appearance(context)
    return repaired
