"""Run inside Blender: blender --background --python tests/test_ngon_side_counts.py

N-gon mode: Ctrl+wheel over a side sets that side's vertex count, or its
group's when the side was grouped with others in the corner editor.
  - a count on one side resamples that side and leaves the others alone
  - a count on a group is shared over its sides, and the corner inside stays
  - the counts are kept with the patch and come back on re-edit
  - the registry advertises the counts the mesh got
"""
import os
import sys
import importlib

_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_ADDON_DIR))

import bpy
import mathutils

pr = importlib.import_module(os.path.basename(_ADDON_DIR))

FAILURES = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")
    if not cond:
        FAILURES.append(name)


try:
    pr.unregister()
except Exception:
    pass
pr.register()

state = bpy.context.scene.plasticity_retop
sidematch = pr.sidematch
operators = pr.operators


def make_square(name, face_id, size=4.0, steps=8):
    """A flat square whose sides carry `steps` segments each, fanned from the
    centre. Straight sides, so the n-gon keeps only the four corners."""
    ring = []
    corners = [(0.0, 0.0), (size, 0.0), (size, size), (0.0, size)]
    for (x0, y0), (x1, y1) in zip(corners, corners[1:] + corners[:1]):
        for i in range(steps):
            t = i / steps
            ring.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, 0.0))
    verts = ring + [(size / 2, size / 2, 0.0)]
    centre = len(ring)
    tris = [(i, (i + 1) % len(ring), centre) for i in range(len(ring))]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], tris)
    mesh.update()
    mesh["groups"] = [0, len(tris) * 3]
    mesh["face_ids"] = [face_id]
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj


def preview_vertex_count():
    obj = bpy.data.objects.get(pr.mesh_build.PREVIEW_OBJ_NAME)
    return len(obj.data.vertices) if obj else 0


def preview_has_point(point, eps=1e-6):
    obj = bpy.data.objects.get(pr.mesh_build.PREVIEW_OBJ_NAME)
    return any((v.co - mathutils.Vector(point)).length < eps
               for v in obj.data.vertices) if obj else False


state.ngon_mode = True
square = make_square("NgonSquare", 5)
bpy.context.view_layer.objects.active = square
operators.enter_session_object(bpy.context, square)
operators.set_active_patch(bpy.context, square, 5)
check("the square opens as an n-gon",
      state.generator_name == pr.generators.NGON.name, state.generator_name)
check("straight sides keep only the corners", preview_vertex_count() == 4,
      preview_vertex_count())
references = sidematch.active_sides()
check("four sides are referenced", len(references) == 4, len(references))

# --- one side, no grouping ---
state.session_active = True
state.session_phase = 'ADJUST'
changed, message = operators.nudge_ngon_side(bpy.context, 0, +1)
check("scrolling over a side changes it", changed, message)
check("that side now has two segments", preview_vertex_count() == 5,
      preview_vertex_count())
changed, _ = operators.nudge_ngon_side(bpy.context, 0, +1)
check("and three after a second step", preview_vertex_count() == 6,
      preview_vertex_count())
changed, _ = operators.nudge_ngon_side(bpy.context, 0, -1)
changed, _ = operators.nudge_ngon_side(bpy.context, 0, -1)
changed, message = operators.nudge_ngon_side(bpy.context, 0, -1)
check("a side never drops below one segment", preview_vertex_count() == 4,
      preview_vertex_count())
check("the stored count is floored at one",
      sidematch.ngon_group_counts(state).get("0") == 1,
      sidematch.ngon_group_counts(state))

# --- the operator path, which reads the hovered side ---
state.ngon_group_counts = ""
operators.regenerate_active_preview(bpy.context)
state.hovered_side = 2
bpy.ops.retop.nudge_span(delta=1)
check("the wheel operator adjusts the hovered side, not the angle",
      preview_vertex_count() == 5 and abs(state.ngon_angle - 20.0) < 1e-6,
      f"{preview_vertex_count()} verts, angle {state.ngon_angle}")
state.hovered_side = -1
bpy.ops.retop.nudge_span(delta=1)
check("away from the sides it still drives the angle",
      state.ngon_angle < 20.0, state.ngon_angle)
state.ngon_angle = 20.0
bpy.ops.retop.reset_ngon_counts()
check("reset forgets the counts", state.ngon_group_counts == "")
check("and the side goes back to its corners", preview_vertex_count() == 4,
      preview_vertex_count())

# --- a group of two sides ---
references = sidematch.active_sides()
first, second = references[0], references[1]
sidematch.set_side_groups(state, {1: 1})  # side 1 joins side 0's group
operators.refresh_group_warning(bpy.context)
check("the grouping is valid", state.group_warning == "", state.group_warning)
runs = sidematch.ngon_runs(references, state)
check("sides 0 and 1 form one run", [0, 1] in [sorted(run) for run in runs], runs)

corner = first.points[-1].copy()  # the corner between the two grouped sides
for _ in range(4):
    operators.nudge_ngon_side(bpy.context, 1, +1)
counts = sidematch.ngon_group_counts(state)
check("the count is stored under the group", counts.get("0,1") == 6, counts)
check("the group carries six segments: 4 corners + 4 new vertices",
      preview_vertex_count() == 8, preview_vertex_count())
check("the corner inside the group is still a vertex",
      preview_has_point(corner), tuple(corner))
check("the allocation is shared by length, three each",
      [operators._ngon_allocation.get(i) for i in (0, 1)] == [3, 3],
      operators._ngon_allocation)

# --- commit, then re-open ---
bpy.ops.retop.commit_patch()
result = bpy.data.objects.get(pr.mesh_build.result_object_name_for(square))
check("the n-gon was committed", result is not None and len(result.data.polygons) == 1)
check("with the grouped sides' vertices", len(result.data.vertices) == 8,
      len(result.data.vertices) if result else None)
registry = pr.mesh_build.get_span_registry(result)
check("the registry holds the grouped sides' counts",
      sorted(registry.values()).count(3) == 2, registry)
stored = pr.mesh_build.lookup_patch_settings(square, 5) or {}
check("the patch record keeps the counts",
      "0,1" in (stored.get("ngon_group_counts") or ""), stored)

state.ngon_group_counts = ""
state.side_groups = ""
operators.set_active_patch(bpy.context, square, 5)
check("re-opening restores the grouping", state.side_groups != "", state.side_groups)
check("and the counts", sidematch.ngon_group_counts(state).get("0,1") == 6,
      state.ngon_group_counts)
check("so the preview matches what was committed", preview_vertex_count() == 8,
      preview_vertex_count())
bpy.ops.retop.clear_preview()

# --- per patch: another patch does not inherit them ---
other = make_square("NgonOther", 6)
operators.set_active_patch(bpy.context, other, 6)
check("a fresh patch starts with no counts", state.ngon_group_counts == "",
      state.ngon_group_counts)
check("and its own corners", preview_vertex_count() == 4, preview_vertex_count())
bpy.ops.retop.clear_preview()

operators.end_session(bpy.context)
state.ngon_mode = False

print()
if FAILURES:
    print(f"=== {len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(1)
print("=== ALL CHECKS PASSED")
