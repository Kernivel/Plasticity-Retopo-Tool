"""Run inside Blender: blender --background --python tests/test_sharp_overrides.py

Sharp edges set by hand are the user's, and re-shading keeps them:
  - clearing a computed crease, or marking an edge the addon left smooth,
    survives the Edit Mode round trip and every later commit
  - Shade Smooth off still flattens everything, and on brings the hand edits back
  - Reset Hand-Set Sharp Edges goes back to the computed creases
"""
import os
import sys
import importlib
import math

_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_ADDON_DIR))

import bpy

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
mesh_build = pr.mesh_build


def build_object(name):
    """Three 1x1 patches in a row: 11 flat, 22 folded up 90 degrees along
    y=1, 33 flat beside 11 along x=1 (a tangent border)."""
    verts = [
        (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0),
        (1.0, 1.0, 1.0), (0.0, 1.0, 1.0),
        (2.0, 0.0, 0.0), (2.0, 1.0, 0.0),
    ]
    tris_a = [(0, 1, 2), (0, 2, 3)]
    tris_b = [(3, 2, 4), (3, 4, 5)]
    tris_c = [(1, 6, 7), (1, 7, 2)]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], tris_a + tris_b + tris_c)
    mesh.update()
    mesh["groups"] = [0, 6, 6, 6, 12, 6]
    mesh["face_ids"] = [11, 22, 33]
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj


def retop_patch(obj, face_id):
    pr.operators.set_active_patch(bpy.context, obj, face_id)
    return bpy.ops.retop.commit_patch()


def edge_key(mesh, edge):
    a, b = (tuple(round(c, 4) for c in mesh.vertices[i].co) for i in edge.vertices)
    return tuple(sorted((a, b)))


def sharp_by_key(result):
    mesh = result.data
    attr = mesh.attributes.get(mesh_build.SHARP_EDGE_ATTR)
    values = [False] * len(mesh.edges)
    attr.data.foreach_get("value", values)
    return {edge_key(mesh, edge): values[edge.index] for edge in mesh.edges}


def set_sharp(result, key, value):
    mesh = result.data
    attr = mesh.attributes.get(mesh_build.SHARP_EDGE_ATTR)
    for edge in mesh.edges:
        if edge_key(mesh, edge) == key:
            attr.data[edge.index].value = value
            return True
    return False


obj = build_object("SharpObj")
bpy.context.view_layer.objects.active = obj
pr.operators.enter_session_object(bpy.context, obj)
retop_patch(obj, 11)
retop_patch(obj, 22)
result = bpy.data.objects.get(mesh_build.result_object_name_for(obj))

computed = sharp_by_key(result)
creases = [key for key, value in computed.items() if value]
smooth = [key for key, value in computed.items() if not value]
check("the fold is creased", len(creases) > 0, len(creases))

cleared = creases[0]
marked = smooth[0]
set_sharp(result, cleared, False)
set_sharp(result, marked, True)

# What leaving the hand-edit round trip runs.
mesh_build.repair_manual_edits(bpy.context, obj)
after = sharp_by_key(result)
check("a crease cleared by hand stays cleared", after[cleared] is False)
check("an edge marked by hand stays sharp", after[marked] is True)
check("the other creases are untouched",
      all(after[key] for key in creases if key != cleared))

# A later commit rebuilds the mesh and renumbers its edges.
retop_patch(obj, 33)
after = sharp_by_key(result)
check("the hand edits survive another commit",
      after.get(cleared) is False and after.get(marked) is True,
      (after.get(cleared), after.get(marked)))
check("the tangent border to the new patch stays smooth",
      sum(after.values()) == len(creases) - 1 + 1, sum(after.values()))

state.result_shade_smooth = False
check("Shade Smooth off still clears every crease", not any(sharp_by_key(result).values()))
state.result_shade_smooth = True
after = sharp_by_key(result)
check("turning it on brings the hand edits back",
      after.get(cleared) is False and after.get(marked) is True)

state.sharp_edge_angle = 45.0
after = sharp_by_key(result)
check("changing the angle keeps them too",
      after.get(cleared) is False and after.get(marked) is True)
state.sharp_edge_angle = 30.0

# Undoing a hand edit by hand is also a hand edit.
set_sharp(result, marked, False)
mesh_build.refresh_result_shading(bpy.context)
check("clearing a hand-marked edge again sticks",
      sharp_by_key(result).get(marked) is False)

count = mesh_build.reset_sharp_overrides(bpy.context)
check("reset reports what it forgot", count == 2, count)
after = sharp_by_key(result)
check("reset goes back to the computed creases",
      after.get(cleared) is True and after.get(marked) is False,
      (after.get(cleared), after.get(marked)))
set_sharp(result, cleared, True)  # no-op: already what the addon wrote
mesh_build.refresh_result_shading(bpy.context)
check("and nothing is read back as a hand edit afterwards",
      sharp_by_key(result).get(cleared) is True)

# The real round trip: Tab into Edit Mode, Clear Sharp, Tab back.
bpy.context.view_layer.objects.active = obj
obj.select_set(True)
state.session_phase = 'PATCH'
error = pr.tweak.enter_tweak(bpy.context)
if error is not None and "Edit Mode" in error:
    print(f"[SKIP] no Edit Mode in this build: {error}")
else:
    check("the hand-edit trip opens", error is None, error)
    bpy.ops.mesh.select_mode(type='EDGE')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.mark_sharp(clear=True)
    pr.tweak.exit_tweak(bpy.context)
    check("the trip ends in Object Mode", bpy.context.mode == 'OBJECT', bpy.context.mode)
    check("Clear Sharp in Edit Mode is not undone on the way back",
          not any(sharp_by_key(result).values()), sum(sharp_by_key(result).values()))
    mesh_build.refresh_result_shading(bpy.context)
    check("nor by the next re-shade", not any(sharp_by_key(result).values()))

pr.operators.end_session(bpy.context)

print()
if FAILURES:
    print(f"=== {len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(1)
print("=== ALL CHECKS PASSED")
