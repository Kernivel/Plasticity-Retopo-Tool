"""Run inside Blender: blender --background --python tests/test_operators.py

End-to-end test of the addon through its actual bpy.ops operators (register,
select a face, generate preview, commit, repeat for a second patch, verify
the accumulated RetopResult mesh).
"""
import os
import sys
import importlib

# Make the test runnable from any checkout: the addon package is the parent
# directory of tests/, so put its parent on sys.path and import it by folder
# name (the hyphen means it can only be imported via import_module).
_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_ADDON_DIR))

import math


import bpy
import bmesh

pr = importlib.import_module(os.path.basename(_ADDON_DIR))

FAILURES = []


def check(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {extra}")
    if not cond:
        FAILURES.append(name)


try:
    pr.unregister()  # in case it's already enabled via saved user preferences
except Exception:
    pass
pr.register()

# ---------------------------------------------------------------------------
# Build the same synthetic 2-patch mesh as bg_test_retop.py
# ---------------------------------------------------------------------------
N = 4
verts = []
groups = []
face_ids = []
grid_index = {}
for j in range(N + 1):
    for i in range(N + 1):
        z = 0.4 * math.sin(i / N * math.pi) * math.sin(j / N * math.pi)
        grid_index[(i, j)] = len(verts)
        verts.append((i, j, z))

patch_a_tris = []
for j in range(N):
    for i in range(N):
        v00 = grid_index[(i, j)]
        v10 = grid_index[(i + 1, j)]
        v11 = grid_index[(i + 1, j + 1)]
        v01 = grid_index[(i, j + 1)]
        patch_a_tris.append((v00, v10, v11))
        patch_a_tris.append((v00, v11, v01))

groups.extend([0, len(patch_a_tris) * 3])
face_ids.append(101)

apex_index = len(verts)
verts.append((N / 2, N + 2.0, 1.9))
patch_b_tris = []
for i in range(N):
    v_a = grid_index[(i, N)]
    v_b = grid_index[(i + 1, N)]
    patch_b_tris.append((v_b, v_a, apex_index))

groups.extend([len(patch_a_tris) * 3, len(patch_b_tris) * 3])
face_ids.append(202)

mesh = bpy.data.meshes.new("PlasticityTestMesh")
mesh.from_pydata(verts, [], patch_a_tris + patch_b_tris)
mesh.update()
mesh["groups"] = groups
mesh["face_ids"] = face_ids

obj = bpy.data.objects.new("PlasticityTestObj", mesh)
bpy.context.collection.objects.link(obj)
bpy.context.view_layer.objects.active = obj
obj.select_set(True)

check("panel registered", hasattr(bpy.types, "VIEW3D_PT_retop"))
check("operator registered", hasattr(bpy.ops.retop, "session"))

# --- dedicated addon-only reload. The operator itself just schedules a
# bpy.app.timers callback (safe: never unregisters its own class while its
# execute() is still on the call stack -- doing that inline is what crashed
# Blender natively the first time this was tried), so it can't be exercised
# end-to-end in --background mode (no running event loop to fire the timer).
# Call the underlying reload function directly to verify the actual
# unregister/reload/register cycle itself is correct. ---
res = bpy.ops.retop.reload_addon()
check("reload_addon operator FINISHED (schedules the real work)", res == {'FINISHED'}, str(res))

pr.operators._perform_reload()
check("panel still registered after _perform_reload", hasattr(bpy.types, "VIEW3D_PT_retop"))
check("operators still registered after _perform_reload", hasattr(bpy.ops.retop, "session"))
check("scene state survives _perform_reload", hasattr(bpy.context.scene, "plasticity_retop"))


def activate_patch(obj, poly_index):
    """Pick the patch owning `poly_index` via the same code path the viewport
    picker uses on click (set_active_patch), since the Edit Mode fallback
    operator was removed."""
    face_id_of_poly, _ = pr.patch_data.polygon_face_ids(obj.data)
    face_id = face_id_of_poly[poly_index]
    return pr.operators.set_active_patch(bpy.context, obj, face_id)


# ---------------------------------------------------------------------------
# Patch A (quad, poly 0)
# ---------------------------------------------------------------------------
gen_name, n_sides, _prop = activate_patch(obj, 0)
check("set_active_patch (A) resolved a generator", gen_name is not None, str(gen_name))

state = bpy.context.scene.plasticity_retop
check("state.generator_name == Quad", state.generator_name == "Quad", state.generator_name)
check("state.active_face_id == 101", state.active_face_id == 101, state.active_face_id)

preview_obj = bpy.data.objects.get(pr.mesh_build.PREVIEW_OBJ_NAME)
check("preview object exists after generate (A)", preview_obj is not None)
expected_faces_a = state.span_u * state.span_v
check("preview face count matches span (A)",
      preview_obj is not None and len(preview_obj.data.polygons) == expected_faces_a,
      f"expected {expected_faces_a}, got {len(preview_obj.data.polygons) if preview_obj else 'N/A'}")

# --- live-update test: changing span_u/span_v via the property (as the N-panel
# sliders do) must regenerate the preview automatically, with no explicit
# Update Preview button click. ---
state.span_u = 6
state.span_v = 3
preview_obj = bpy.data.objects.get(pr.mesh_build.PREVIEW_OBJ_NAME)
expected_faces_live = 6 * 3
check("live span_u/span_v update regenerates preview automatically",
      preview_obj is not None and len(preview_obj.data.polygons) == expected_faces_live,
      f"expected {expected_faces_live}, got {len(preview_obj.data.polygons) if preview_obj else 'N/A'}")
# restore defaults for the rest of the test
state.span_u = 4
state.span_v = 4
expected_faces_a = state.span_u * state.span_v

res = bpy.ops.retop.commit_patch()
check("commit_patch (A) FINISHED", res == {'FINISHED'}, str(res))

result_obj = bpy.data.objects.get(pr.mesh_build.result_object_name_for(obj))
check("result object exists after commit (A)", result_obj is not None)
# The preview object deliberately outlives a commit, emptied rather than freed:
# creating and freeing datablocks between undo steps is what made Ctrl+Z crash
# Blender. Only ending the session frees it.
check("preview is emptied after commit (A), not freed", not pr.mesh_build.has_preview(),
      str(pr.mesh_build.has_preview()))
check("the preview object itself survives the commit",
      bpy.data.objects.get(pr.mesh_build.PREVIEW_OBJ_NAME) is not None)
faces_after_a = len(result_obj.data.polygons) if result_obj else 0
check("result face count == patch A faces", faces_after_a == expected_faces_a,
      f"expected {expected_faces_a}, got {faces_after_a}")

uv_layer = result_obj.data.uv_layers.get("UVMap") if result_obj else None
check("result mesh has a UVMap", uv_layer is not None)

# ---------------------------------------------------------------------------
# Patch B (triangle, poly index = len(patch_a_tris))
# ---------------------------------------------------------------------------
gen_name_b, n_sides_b, _prop_b = activate_patch(obj, len(patch_a_tris))
check("set_active_patch (B) resolved a generator", gen_name_b is not None, str(gen_name_b))
check("state.generator_name == Triangle", state.generator_name == "Triangle", state.generator_name)
check("state.active_face_id == 202", state.active_face_id == 202, state.active_face_id)

preview_obj = bpy.data.objects.get(pr.mesh_build.PREVIEW_OBJ_NAME)
expected_faces_b = state.span * state.span
check("preview face count matches span (B)",
      preview_obj is not None and len(preview_obj.data.polygons) == expected_faces_b,
      f"expected {expected_faces_b}, got {len(preview_obj.data.polygons) if preview_obj else 'N/A'}")

# --- propagation: patch B shares its base edge with patch A's committed top
# edge (length N=4, patch A used span 4 there since it's a square). Its
# default span should now be propagated to 4 instead of independently
# computed (~2), so both patches are fully welded along that whole edge, not
# just at the two corners. ---
check("patch B span propagated from committed patch A", state.span == 4, f"got {state.span}")

# --- result highlight is owned by the retop *session*, not by individual
# patch edits: picking a patch outside a session (as this part of the test
# does) must NOT switch it on. The session lifecycle is covered further down. ---
result_obj_mid_edit = bpy.data.objects.get(pr.mesh_build.result_object_name_for(obj))
check("editing a patch outside a session leaves the result resting",
      result_obj_mid_edit is not None and not result_obj_mid_edit.show_in_front,
      f"show_in_front={result_obj_mid_edit.show_in_front if result_obj_mid_edit else 'N/A'}")

# --- cosmetic offset must be a non-destructive modifier, never baked into
# the committed geometry -- and the preview must sit *above* the committed
# result, not on the surface underneath it. ---
preview_obj = bpy.data.objects.get(pr.mesh_build.PREVIEW_OBJ_NAME)
base_lift = (pr.mesh_build.result_lift(bpy.context, obj)
             * pr.mesh_build.PREVIEW_LIFT_RATIO)
check("the preview is lifted further than the committed result",
      base_lift > pr.mesh_build.result_lift(bpy.context, obj) > 0.0,
      f"preview={base_lift}, result={pr.mesh_build.result_lift(bpy.context, obj)}")
check("the preview knows which source object it belongs to",
      pr.mesh_build.preview_source_object() is obj,
      str(pr.mesh_build.preview_source_object()))

# One offset drives both, so moving it moves the preview with the result --
# there is no second slider that could disagree with it.
mod = preview_obj.modifiers.get(pr.mesh_build.OFFSET_MODIFIER_NAME) if preview_obj else None
check("the preview's lift is the result's, times the margin",
      mod is not None and abs(mod.strength - base_lift) < 1e-6,
      f"mod={mod}, strength={mod.strength if mod else 'N/A'}, base={base_lift}")

# An explicit Result Offset, since the default 0 means "derive it from the
# object's size" and doubling zero moves nothing.
before_offset = state.result_offset
state.result_offset = 1.0
raised = (pr.mesh_build.result_lift(bpy.context, obj)
          * pr.mesh_build.PREVIEW_LIFT_RATIO)
mod_after = preview_obj.modifiers.get(pr.mesh_build.OFFSET_MODIFIER_NAME)
check("and follows it when it changes",
      mod_after is not None and abs(mod_after.strength - raised) < 1e-6
      and abs(raised - base_lift) > 1e-9,
      f"strength={mod_after.strength if mod_after else 'N/A'}, "
      f"want={raised}, was={base_lift}")
state.result_offset = before_offset

apex_local_before = preview_obj.data.vertices[-1].co.copy()  # base mesh, unaffected by the modifier

# --- Alt+X is one answer for both meshes: a preview drawn in front while the
# result is being checked against the surface is the same confusion again. ---
state.result_see_through = False
pr.mesh_build.refresh_result_appearance(bpy.context)
check("the preview follows See Retopo Through Meshes",
      preview_obj.show_in_front is False, f"show_in_front={preview_obj.show_in_front}")
state.result_see_through = True
pr.mesh_build.refresh_result_appearance(bpy.context)
check("the preview is drawn in front again when see-through is back on",
      preview_obj.show_in_front is True, f"show_in_front={preview_obj.show_in_front}")

res = bpy.ops.retop.commit_patch()
check("commit_patch (B) FINISHED", res == {'FINISHED'}, str(res))

result_obj = bpy.data.objects.get(pr.mesh_build.result_object_name_for(obj))
total_faces = len(result_obj.data.polygons) if result_obj else 0
check("result face count == patch A + patch B faces",
      total_faces == expected_faces_a + expected_faces_b,
      f"expected {expected_faces_a + expected_faces_b}, got {total_faces}")

# ---------------------------------------------------------------------------
# Re-selecting an already-committed patch: it must come back with the spans it
# was committed with, and committing again must REPLACE its faces instead of
# stacking a second grid on top of them.
# ---------------------------------------------------------------------------
check("committed patches are tracked on the result mesh",
      pr.mesh_build.committed_face_ids(obj) == {101, 202},
      str(pr.mesh_build.committed_face_ids(obj)))
check("an uncommitted face id isn't reported as committed",
      not pr.mesh_build.is_patch_committed(obj, 999))

faces_before_reedit = len(result_obj.data.polygons)

# Patch A was committed at 4x4; pick it again with the panel's spans left on
# something else entirely, and it must restore its own 4/4.
state.span_u = 9
state.span_v = 7
gen_name_re, _n_re, _prop_re = activate_patch(obj, 0)
check("re-selecting a committed patch resolves its generator", gen_name_re == "Quad", str(gen_name_re))
check("re-selecting flags the patch as a re-edit", state.editing_committed, str(state.editing_committed))
check("re-selecting restores the spans it was committed with",
      state.span_u == 4 and state.span_v == 4, f"span_u={state.span_u}, span_v={state.span_v}")

preview_obj = bpy.data.objects.get(pr.mesh_build.PREVIEW_OBJ_NAME)
check("re-selecting rebuilds the preview at the stored spans",
      preview_obj is not None and len(preview_obj.data.polygons) == 16,
      f"got {len(preview_obj.data.polygons) if preview_obj else 'N/A'}")

# Picking it takes the old geometry out straight away, so the viewport shows the
# patch being rebuilt instead of a second grid stacked on the first.
check("re-selecting removes the old patch's faces on the spot",
      state.reedit_removed_faces == expected_faces_a
      and len(result_obj.data.polygons) == faces_before_reedit - expected_faces_a,
      f"removed={state.reedit_removed_faces}, faces={len(result_obj.data.polygons)} "
      f"(expected {faces_before_reedit - expected_faces_a})")
check("a re-edit keeps a snapshot to restore from", state.reedit_backup_mesh != "",
      state.reedit_backup_mesh)

# Discarding must put it back exactly as it was.
_backup_name = state.reedit_backup_mesh
bpy.ops.retop.clear_preview()
check("discarding a re-edit restores the committed patch",
      len(result_obj.data.polygons) == faces_before_reedit,
      f"expected {faces_before_reedit}, got {len(result_obj.data.polygons)}")
check("discarding a re-edit clears the re-edit flag", not state.editing_committed)
check("the restored patch is still tracked", pr.mesh_build.committed_face_ids(obj) == {101, 202},
      str(pr.mesh_build.committed_face_ids(obj)))
check("the snapshot mesh is not left behind", bpy.data.meshes.get(_backup_name) is None
      or bpy.data.meshes.get(_backup_name) is result_obj.data,
      str(_backup_name))
# result_obj.data is swapped by the restore, so re-resolve it before going on.
result_obj = bpy.data.objects.get(pr.mesh_build.result_object_name_for(obj))

# Now really change patch A's span and commit: A's 16 faces are replaced by
# 2x4=8, and patch B's faces are untouched.
activate_patch(obj, 0)
state.span_u = 2
state.span_v = 4
res = bpy.ops.retop.commit_patch()
check("re-committing a patch FINISHED", res == {'FINISHED'}, str(res))
check("committing a re-edit drops the snapshot", state.reedit_backup_mesh == "",
      state.reedit_backup_mesh)
expected_after_replace = faces_before_reedit - expected_faces_a + (2 * 4)
check("re-committing replaces the patch instead of duplicating it",
      len(result_obj.data.polygons) == expected_after_replace,
      f"expected {expected_after_replace}, got {len(result_obj.data.polygons)}")
check("both patches are still tracked after a replace",
      pr.mesh_build.committed_face_ids(obj) == {101, 202},
      str(pr.mesh_build.committed_face_ids(obj)))
check("committing clears the re-edit flag", not state.editing_committed)

stored = pr.mesh_build.lookup_patch_settings(obj, 101)
check("the new spans are stored for the next re-edit",
      stored is not None and stored["span_u"] == 2 and stored["span_v"] == 4, str(stored))

check("no face is left without a patch id",
      pr.mesh_build.NO_PATCH not in pr.mesh_build._patch_ids_of_faces(result_obj.data))

# Restore patch A to 4x4 so the checks further down see the original layout.
activate_patch(obj, 0)
state.span_u = 4
state.span_v = 4
bpy.ops.retop.commit_patch()
check("patch A restored to its original span",
      len(result_obj.data.polygons) == faces_before_reedit,
      f"expected {faces_before_reedit}, got {len(result_obj.data.polygons)}")

# Removing a patch's faces must take its own vertices with them (and only
# those): a wrong delete context leaves the old grid's interior points behind
# as loose vertices.
_used_verts = set()
for _poly in result_obj.data.polygons:
    _used_verts.update(_poly.vertices)
_loose = [v.index for v in result_obj.data.vertices if v.index not in _used_verts]
check("the result mesh has no loose vertices after a replace", not _loose, str(_loose[:8]))


# ---------------------------------------------------------------------------
# Legacy result meshes: retopology committed before per-face patch tracking has
# no patch ids, so its patches would read as "never retopped" and a re-edit
# would stack a second grid on top. Entering the object claims them by
# geometry (adopt_untracked_faces) so re-editing replaces them like any other.
# ---------------------------------------------------------------------------
def _faces_per_patch(result_obj):
    counts = {}
    for fid in pr.mesh_build._patch_ids_of_faces(result_obj.data):
        counts[fid] = counts.get(fid, 0) + 1
    return counts


tracked_before = _faces_per_patch(result_obj)

# Strip the tracking to reproduce a mesh committed by an older version.
result_obj.data.attributes.remove(result_obj.data.attributes[pr.mesh_build.PATCH_ID_ATTR])
del result_obj[pr.mesh_build.PATCH_SPANS_PROP]
check("a legacy result mesh reports nothing as committed",
      pr.mesh_build.committed_face_ids(obj) == set(),
      str(pr.mesh_build.committed_face_ids(obj)))

adopted = pr.mesh_build.adopt_untracked_faces(obj)
check("adoption claims every pre-existing face",
      adopted == len(result_obj.data.polygons), f"{adopted} of {len(result_obj.data.polygons)}")
check("adoption puts each face back on the patch it came from",
      _faces_per_patch(result_obj) == tracked_before,
      f"{_faces_per_patch(result_obj)} vs {tracked_before}")
check("adopted patches are re-editable", pr.mesh_build.committed_face_ids(obj) == {101, 202},
      str(pr.mesh_build.committed_face_ids(obj)))
check("re-running adoption is a no-op", pr.mesh_build.adopt_untracked_faces(obj) == 0)

# An adopted patch has no stored spans (the old version never wrote any), so it
# reopens on propagated/computed defaults -- but it must still be a re-edit, and
# committing must still replace it rather than duplicate it.
faces_before_legacy_reedit = len(result_obj.data.polygons)
activate_patch(obj, 0)
check("an adopted patch is flagged as a re-edit", state.editing_committed, str(state.editing_committed))
state.span_u = 3
state.span_v = 3
bpy.ops.retop.commit_patch()
expected_legacy = faces_before_legacy_reedit - tracked_before[101] + 9
check("re-committing an adopted patch replaces it",
      len(result_obj.data.polygons) == expected_legacy,
      f"expected {expected_legacy}, got {len(result_obj.data.polygons)}")

# Put patch A back to 4x4 for the checks that follow.
activate_patch(obj, 0)
state.span_u = 4
state.span_v = 4
bpy.ops.retop.commit_patch()
check("patch A back to its original span after the legacy round-trip",
      len(result_obj.data.polygons) == faces_before_legacy_reedit,
      f"expected {faces_before_legacy_reedit}, got {len(result_obj.data.polygons)}")

# Ending the session (or the addon reloading) in the middle of a re-edit must
# roll it back too, not leave the patch missing from the result mesh.
activate_patch(obj, 0)
check("a re-edit in flight has taken the patch out",
      len(result_obj.data.polygons) < faces_before_legacy_reedit,
      f"faces={len(result_obj.data.polygons)}")
pr.operators.end_session(bpy.context)
result_obj = bpy.data.objects.get(pr.mesh_build.result_object_name_for(obj))
check("ending the session mid-re-edit restores the patch",
      len(result_obj.data.polygons) == faces_before_legacy_reedit,
      f"expected {faces_before_legacy_reedit}, got {len(result_obj.data.polygons)}")
check("no re-edit snapshot survives the session",
      state.reedit_backup_mesh == "" and not state.editing_committed,
      f"backup='{state.reedit_backup_mesh}', flag={state.editing_committed}")

check("result rests when no session owns it (opaque, no in-front/wire)",
      result_obj is not None and not result_obj.show_in_front and not result_obj.show_wire
      and abs(result_obj.color[3] - 1.0) < 1e-6,
      f"show_in_front={result_obj.show_in_front}, alpha={result_obj.color[3]}")

# ---------------------------------------------------------------------------
# Session lifecycle: enter object -> highlight + PATCH phase; commit returns
# to PATCH (so the next surface can be picked immediately); leaving the object
# drops the highlight but keeps the session; ending it clears everything.
# ---------------------------------------------------------------------------
pr.operators.enter_session_object(bpy.context, obj)
check("entering an object marks the session active", state.session_active, str(state.session_active))
check("entering an object moves to the PATCH phase", state.session_phase == 'PATCH', state.session_phase)
check("entering an object records it", state.session_object_name == obj.name, state.session_object_name)
check("entering an object caches how many patches are done for the panel",
      state.committed_patch_count == 2, str(state.committed_patch_count))

result_obj = bpy.data.objects.get(pr.mesh_build.result_object_name_for(obj))
check("entering an object highlights its result mesh",
      result_obj is not None and result_obj.show_in_front and result_obj.show_wire,
      f"show_in_front={result_obj.show_in_front if result_obj else 'N/A'}")

# Retop one more patch and commit: active_face_id must clear (that's the
# signal a running session watches to go back to picking a surface), while
# the highlight stays on because the session still owns the object.
activate_patch(obj, 0)
check("patch active again inside the session", state.active_face_id == 101, state.active_face_id)
bpy.ops.retop.commit_patch()
check("commit clears active_face_id (session returns to picking)", state.active_face_id == -1,
      state.active_face_id)
result_obj = bpy.data.objects.get(pr.mesh_build.result_object_name_for(obj))
check("commit keeps the session highlight on", result_obj.show_in_front, result_obj.show_in_front)

pr.operators.exit_session_object(bpy.context)
check("leaving the object returns to the OBJECT phase", state.session_phase == 'OBJECT', state.session_phase)
check("leaving the object clears the highlight", not result_obj.show_in_front, result_obj.show_in_front)

pr.operators.end_session(bpy.context)
check("ending the session clears session state",
      not state.session_active and state.session_object_name == "" and state.active_face_id == -1,
      f"active={state.session_active}, obj='{state.session_object_name}'")
check("ending the session is what frees the preview object",
      bpy.data.objects.get(pr.mesh_build.PREVIEW_OBJ_NAME) is None)

# --- undo safety: the handler that runs after Ctrl+Z drops the active patch
# and any re-edit snapshot, since undo has just replaced the mesh state both
# were describing. It must touch scene state only. ---
pr.operators.enter_session_object(bpy.context, obj)
activate_patch(obj, 0)
meshes_before_undo = len(bpy.data.meshes)
pr.operators._on_undo_redo(bpy.context.scene)
check("undo drops the active patch", state.active_face_id == -1, str(state.active_face_id))
check("undo drops the re-edit snapshot",
      not state.editing_committed and state.reedit_backup_mesh == "",
      f"flag={state.editing_committed}, backup='{state.reedit_backup_mesh}'")
check("undo returns an adjusting session to picking", state.session_phase == 'PATCH',
      state.session_phase)
check("the undo handler creates or frees nothing", len(bpy.data.meshes) == meshes_before_undo,
      f"{meshes_before_undo} -> {len(bpy.data.meshes)}")
check("the undo handler is registered",
      any(getattr(h, "__name__", "") == "_on_undo_redo" for h in bpy.app.handlers.undo_post))
pr.operators.end_session(bpy.context)

# A re-edit snapshot orphaned by that undo carries a fake user, so nothing but
# this collects it -- and entering an object is where it happens.
_orphan = result_obj.data.copy()
_orphan.name = f"Whatever{pr.mesh_build.SNAPSHOT_NAME_SUFFIX}"
_orphan.use_fake_user = True
pr.operators.enter_session_object(bpy.context, obj)
check("entering an object purges orphaned re-edit snapshots",
      not any(m.name.endswith(pr.mesh_build.SNAPSHOT_NAME_SUFFIX) for m in bpy.data.meshes),
      str([m.name for m in bpy.data.meshes]))
pr.operators.end_session(bpy.context)

# ---------------------------------------------------------------------------
# Keybind plumbing: which span the wheel drives, Tab switching U/V, and the
# overlay's hint list per phase.
# ---------------------------------------------------------------------------
state.generator_name = "Quad"
state.span_axis = 'U'
check("wheel drives span_u on a quad in U mode",
      pr.operators.active_span_prop(state) == "span_u", pr.operators.active_span_prop(state))
state.span_axis = 'V'
check("wheel drives span_v on a quad in V mode",
      pr.operators.active_span_prop(state) == "span_v", pr.operators.active_span_prop(state))
state.generator_name = "Triangle"
check("wheel drives the single span on a triangle",
      pr.operators.active_span_prop(state) == "span", pr.operators.active_span_prop(state))

def _poll_without_session():
    """Whether the span key would fire with no session running.

    It must not: every session key is a KeyMapItem in the 3D View keymap, so it
    is offered whether a session exists or not and the poll is the only thing
    standing between a stray Ctrl+wheel and a span nobody is adjusting.
    """
    was_active = state.session_active
    state.session_active = False
    try:
        return bpy.ops.retop.nudge_span.poll()
    finally:
        state.session_active = was_active


# Nudging must go through the property (so the live-update callback fires) and
# never fall below 1.
state.generator_name = "Quad"
state.span_axis = 'U'
state.span_u = 3
# Through the operator: Ctrl+wheel is a real KeyMapItem on retop.nudge_span
# now, and its poll wants a live session in the ADJUST phase.
state.session_active = True
state.session_phase = 'ADJUST'
bpy.ops.retop.nudge_span(delta=2)
check("scroll up raises the active span", state.span_u == 5, state.span_u)
for _ in range(10):
    bpy.ops.retop.nudge_span(delta=-1)
check("scroll down clamps the span at 1", state.span_u == 1, state.span_u)
check("and the key is refused outside a session",
      not _poll_without_session(), "nudge_span should need a session")

adjust_keys = [k for k, _a in pr.overlay.keybinds_for(state)]
check("adjust overlay lists the Plasticity-style binds",
      "Ctrl+Scroll" in adjust_keys and "Tab" in adjust_keys and "R-Click" in adjust_keys,
      str(adjust_keys))
state.generator_name = "Triangle"
check("Tab hint is hidden for non-quad patches (single span)",
      "Tab" not in [k for k, _a in pr.overlay.keybinds_for(state)],
      str([k for k, _a in pr.overlay.keybinds_for(state)]))
state.session_phase = 'PATCH'
# Alt+X and E ride along in every phase. Alt+X answers "is the retopology where
# I think it is"; E draws the Plasticity edges, which is read while *choosing* a
# surface as much as while adjusting one. Ctrl+Z is Blender's own, advertised
# only here: one undo step is one committed patch, so this is the phase where
# it means "take the last patch back" -- but it is deliberately *not* listed:
# it is Blender's own key, reaching for it is automatic, and the hint line is
# short enough that every entry has to earn its width. Tab is here and only
# here: it opens the hand-edit round trip into Blender's Edit Mode, which a
# patch open for adjustment must not do (its faces are out of the result mesh
# with only a snapshot to put them back, and Edit Mode discards writes to it on
# exit). Shift+Click gathers several faces into one patch, and is named here
# whether or not anything is gathered yet: it is behind a modifier on a click,
# which is exactly the kind of gesture nobody finds on their own.
check("patch phase overlay shows its own binds",
      [k for k, _a in pr.overlay.keybinds_for(state)]
      == ["Click", "Shift+Click", "Tab", "Alt+X", "E", "V", "Esc"],
      str(pr.overlay.keybinds_for(state)))

# ---------------------------------------------------------------------------
# Typed span entry: digits set the active span directly, Backspace edits it.
# ---------------------------------------------------------------------------
_session_cls = pr.operators.RETOP_OT_session


class _TypingSelf:
    # The buffer lives on the scene now (state.typed_span), because the keys
    # that clear it are real operators and an operator cannot reach the running
    # modal. Only the digits themselves are still the modal's.
    _set_typed = _session_cls._set_typed
    _flush_typed_span = _session_cls._flush_typed_span
    _handle_typed_digit = _session_cls._handle_typed_digit


class _KeyEvent:
    def __init__(self, key):
        self.type = key


typer = _TypingSelf()
state.generator_name = "Quad"
state.span_axis = 'U'
state.span_u = 4

check("digit key is consumed", typer._handle_typed_digit(bpy.context, _KeyEvent('ONE')) is True)
typer._handle_typed_digit(bpy.context, _KeyEvent('TWO'))
check("typing '12' sets the span to 12", state.span_u == 12, state.span_u)
typer._handle_typed_digit(bpy.context, _KeyEvent('BACK_SPACE'))
check("backspace trims the typed span back to 1", state.span_u == 1, state.span_u)
check("numpad digits work too", typer._handle_typed_digit(bpy.context, _KeyEvent('NUMPAD_5')) is True)
check("typing '15' after backspace sets 15", state.span_u == 15, state.span_u)
check("non-digit keys are not consumed",
      typer._handle_typed_digit(bpy.context, _KeyEvent('A')) is False)

typer._set_typed("")
typer._handle_typed_digit(bpy.context, _KeyEvent('ZERO'))
check("a leading zero doesn't drop the span below 1", state.span_u >= 1, state.span_u)

# ---------------------------------------------------------------------------
# Show All Retopo: every result mesh is visible while a session runs, with the
# worked-on one at full alpha and the others dimmed.
# ---------------------------------------------------------------------------
second_mesh = bpy.data.meshes.new("SecondSource")
second_mesh.from_pydata([(9, 0, 0), (10, 0, 0), (10, 1, 0)], [], [(0, 1, 2)])
second_mesh.update()
second_mesh["groups"] = [0, 3]
second_mesh["face_ids"] = [909]
second_obj = bpy.data.objects.new("SecondSource", second_mesh)
bpy.context.collection.objects.link(second_obj)
pr.mesh_build.ensure_result_object(bpy.context, second_obj)

state.session_active = True
state.session_object_name = obj.name
state.highlight_all_results = True
state.result_alpha = 1.0
state.inactive_result_alpha = 0.25
pr.mesh_build.refresh_result_appearance(bpy.context)

active_result = bpy.data.objects.get(pr.mesh_build.result_object_name_for(obj))
other_result = bpy.data.objects.get(pr.mesh_build.result_object_name_for(second_obj))
check("the worked-on result is drawn in front at full alpha",
      active_result.show_in_front and abs(active_result.color[3] - 1.0) < 1e-6,
      f"in_front={active_result.show_in_front}, alpha={active_result.color[3]}")
check("other retopo meshes are shown dimmed, not in front",
      other_result.show_wire and not other_result.show_in_front
      and abs(other_result.color[3] - 0.25) < 1e-6,
      f"wire={other_result.show_wire}, in_front={other_result.show_in_front}, alpha={other_result.color[3]}")
check("the two use different materials (one alpha each)",
      active_result.data.materials[0] is not other_result.data.materials[0],
      f"{active_result.data.materials[0].name} vs {other_result.data.materials[0].name}")

state.highlight_all_results = False
pr.mesh_build.refresh_result_appearance(bpy.context)
check("disabling Show All Retopo rests the other meshes",
      not other_result.show_wire and abs(other_result.color[3] - 1.0) < 1e-6,
      f"wire={other_result.show_wire}, alpha={other_result.color[3]}")
check("the worked-on result stays highlighted regardless",
      active_result.show_in_front, active_result.show_in_front)

state.highlight_all_results = True
state.session_active = False
pr.mesh_build.refresh_result_appearance(bpy.context)
check("with no session everything rests",
      not active_result.show_in_front and not other_result.show_wire,
      f"active_in_front={active_result.show_in_front}, other_wire={other_result.show_wire}")

# ---------------------------------------------------------------------------
# Result offset: keeps the retopo off the CAD surface so they don't z-fight,
# without ever changing the geometry that renders/exports.
# ---------------------------------------------------------------------------
state.result_offset = 0.0  # automatic
pr.mesh_build.refresh_result_appearance(bpy.context)
off_mod = active_result.modifiers.get(pr.mesh_build.RESULT_OFFSET_MODIFIER_NAME)
check("auto result offset adds a Displace modifier", off_mod is not None)
check("auto offset scales with the source object, not a magic constant",
      off_mod is not None and off_mod.strength > 0.0
      and abs(off_mod.strength - obj.dimensions.length * pr.mesh_build.AUTO_OFFSET_RATIO) < 1e-9,
      f"strength={off_mod.strength if off_mod else 'N/A'}, dims={tuple(obj.dimensions)}")
check("result offset is viewport-only (never rendered/exported)",
      off_mod is not None and off_mod.show_render is False,
      f"show_render={off_mod.show_render if off_mod else 'N/A'}")
check("result offset displaces along normals from zero",
      off_mod is not None and off_mod.direction == 'NORMAL' and off_mod.mid_level == 0.0,
      f"direction={off_mod.direction if off_mod else 'N/A'}")

state.length_unit = 'MM'
state.result_offset = 2.0  # 2 mm, explicit
pr.mesh_build.refresh_result_appearance(bpy.context)
off_mod = active_result.modifiers.get(pr.mesh_build.RESULT_OFFSET_MODIFIER_NAME)
check("an explicit result offset is converted from the chosen unit",
      off_mod is not None and abs(off_mod.strength - 0.002) < 1e-9,
      f"strength={off_mod.strength if off_mod else 'N/A'}")
state.length_unit = 'M'
state.result_offset = 0.0

# The committed geometry itself must be untouched by the cosmetic offset.
vert_before = active_result.data.vertices[0].co.copy()
pr.mesh_build.refresh_result_appearance(bpy.context)
check("the offset never moves the stored result geometry",
      (active_result.data.vertices[0].co - vert_before).length < 1e-12,
      str((active_result.data.vertices[0].co - vert_before).length))

state.session_phase = 'ADJUST'
check("overlay advertises Ctrl+Scroll for spans (plain wheel stays zoom)",
      any(k == "Ctrl+Scroll" for k, _a in pr.overlay.keybinds_for(state)),
      str([k for k, _a in pr.overlay.keybinds_for(state)]))
check("overlay no longer claims the plain wheel",
      not any(k == "Scroll" for k, _a in pr.overlay.keybinds_for(state)),
      str([k for k, _a in pr.overlay.keybinds_for(state)]))

# ---------------------------------------------------------------------------
# Stale-session recovery: if the modal dies without reaching _finish (addon
# reloaded mid-session, or an exception), the scene still says "in session"
# while nothing listens to clicks/Esc. The panel must be able to tell, and
# end_session must clear it.
# ---------------------------------------------------------------------------
state.session_active = True
state.session_object_name = obj.name
state.session_phase = 'PATCH'
check("a session with no running modal is reported as not running",
      not pr.operators.session_is_running(), str(pr.operators.session_is_running()))

res = bpy.ops.retop.end_session()
check("end_session operator FINISHED", res == {'FINISHED'}, str(res))
check("end_session clears the stale state",
      not state.session_active and state.session_object_name == "",
      f"active={state.session_active}, obj='{state.session_object_name}'")

# Reloading mid-session must not leave a phantom session behind.
state.session_active = True
state.session_object_name = obj.name
state.session_phase = 'PATCH'
pr.operators._perform_reload()
pr2 = importlib.import_module(os.path.basename(_ADDON_DIR))
state = bpy.context.scene.plasticity_retop
check("reloading mid-session tears the session down",
      not state.session_active and not pr2.operators.session_is_running(),
      f"active={state.session_active}, running={pr2.operators.session_is_running()}")
pr = pr2

# --- metric unit conversion ---
state.length_unit = 'MM'
check("mm converts to Blender units", abs(pr.state.to_blender_units(state, 1.0) - 0.001) < 1e-12,
      str(pr.state.to_blender_units(state, 1.0)))
state.length_unit = 'IN'
check("inches convert to Blender units", abs(pr.state.to_blender_units(state, 1.0) - 0.0254) < 1e-12,
      str(pr.state.to_blender_units(state, 1.0)))
state.length_unit = 'M'
check("meters are 1:1 with Blender units", abs(pr.state.to_blender_units(state, 1.0) - 1.0) < 1e-12,
      str(pr.state.to_blender_units(state, 1.0)))

# Boundary vertices shared between A and B should be welded (both patches
# were built from the same source boundary loop and neither reprojects its
# own boundary), so vertex count should be noticeably less than the naive
# sum of both patches' independent grids.
naive_sum_verts = (state.span_u + 1) * (state.span_v + 1)  # not reused directly, just documents intent
print(f"result verts={len(result_obj.data.vertices)} faces={total_faces}")

# ---------------------------------------------------------------------------
pr.unregister()

print()
if FAILURES:
    print(f"=== {len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(1)
else:
    print("=== ALL CHECKS PASSED")
    sys.exit(0)
