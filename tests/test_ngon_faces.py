"""Run inside Blender: blender --background --python tests/test_ngon_faces.py

What N-gon mode may and may not be applied to:
  - flat faces only (a single face over a bevel is a lid, not a retopology)
  - a face with holes, each bridged into the face around it with two edges
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


def make_object(name, verts, tri_groups):
    """tri_groups: [(face_id, [tri, ...]), ...] laid out in group order."""
    tris = []
    groups = []
    face_ids = []
    for face_id, group_tris in tri_groups:
        groups.extend([len(tris) * 3, len(group_tris) * 3])
        face_ids.append(face_id)
        tris.extend(group_tris)

    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], tris)
    mesh.update()
    mesh["groups"] = groups
    mesh["face_ids"] = face_ids
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj


# ===========================================================================
# Flat faces only
# ===========================================================================
flat = make_object(
    "FlatFace",
    [(0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (2.0, 2.0, 0.0), (0.0, 2.0, 0.0)],
    [(7, [(0, 1, 2), (0, 2, 3)])],
)
check("a plane is flat", pr.patchprep.patch_is_planar(flat.data, 7, 5.0))

# A quarter-cylinder strip: what a bevel or a fillet actually looks like.
QUADS = 6
verts = []
for i in range(QUADS + 1):
    angle = i / QUADS * (math.pi / 2)
    verts.append((math.cos(angle), math.sin(angle), 0.0))
    verts.append((math.cos(angle), math.sin(angle), 1.0))
tris = []
for i in range(QUADS):
    a, b, c, d = i * 2, i * 2 + 1, i * 2 + 3, i * 2 + 2
    tris.append((a, b, c))
    tris.append((a, c, d))
bevel = make_object("BevelFace", verts, [(9, tris)])

check("a bevel is not flat", not pr.patchprep.patch_is_planar(bevel.data, 9, 5.0))
check("and stays not flat at a generous tolerance",
      not pr.patchprep.patch_is_planar(bevel.data, 9, 10.0))
check("a tolerance wide enough swallows it, as documented",
      pr.patchprep.patch_is_planar(bevel.data, 9, 45.0))

check("ngon_blocker names the reason",
      pr.operators.ngon_blocker(state, bevel.data, 9) == "not a flat face",
      pr.operators.ngon_blocker(state, bevel.data, 9))
check("and says nothing about a flat face",
      pr.operators.ngon_blocker(state, flat.data, 7) == "")
check("and several holes are no longer a blocker -- generate_holed bridges them",
      pr.operators.ngon_blocker(state, flat.data, 7, num_loops=3) == "",
      pr.operators.ngon_blocker(state, flat.data, 7, num_loops=3))

# --- with the mode on, a curved patch silently falls back to a grid ---
state.ngon_mode = True
bpy.context.view_layer.objects.active = bevel
pr.operators.enter_session_object(bpy.context, bevel)
pr.operators.set_active_patch(bpy.context, bevel, 9)
check("a bevel is not generated as an n-gon",
      state.generator_name != pr.generators.NGON.name, state.generator_name)
check("the panel is told why", state.ngon_available is False
      and state.ngon_unavailable_reason == "not a flat face",
      f"{state.ngon_available} / {state.ngon_unavailable_reason}")
check("a bevel keeps the four sides a grid needs",
      state.num_sides == 4, state.num_sides)
bpy.ops.retop.clear_preview()
pr.operators.end_session(bpy.context)

bpy.context.view_layer.objects.active = flat
pr.operators.enter_session_object(bpy.context, flat)
pr.operators.set_active_patch(bpy.context, flat, 7)
check("a flat patch is generated as an n-gon",
      state.generator_name == pr.generators.NGON.name, state.generator_name)
check("and the panel reports it as available", state.ngon_available is True)
bpy.ops.retop.clear_preview()
pr.operators.end_session(bpy.context)


# ===========================================================================
# A flat face with one hole
# ===========================================================================
ring_verts = [
    (-2.0, -2.0, 0.0), (2.0, -2.0, 0.0), (2.0, 2.0, 0.0), (-2.0, 2.0, 0.0),  # 0-3 outer
    (-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0),  # 4-7 hole
]
o0, o1, o2, o3, h0, h1, h2, h3 = range(8)
ring_tris = [
    (o0, o1, h1), (o0, h1, h0),
    (o1, o2, h2), (o1, h2, h1),
    (o2, o3, h3), (o2, h3, h2),
    (o3, o0, h0), (o3, h0, h3),
]
holed = make_object("HoledFace", ring_verts, [(5, ring_tris)])

check("the holed face is flat", pr.patchprep.patch_is_planar(holed.data, 5, 5.0))
prepared = pr.patchprep.prepare_patch(holed.data, 5, 135.0, 0.0, 'BOTH')
check("it has two boundary loops", prepared.num_loops == 2, prepared.num_loops)
check("so the pipeline calls it a ring", prepared.is_ring)
check("and one hole is not a blocker",
      pr.operators.ngon_blocker(state, holed.data, 5, num_loops=2) == "")

result = pr.generators.NGON.generate_holed(prepared.loops_sides, {"ngon_angle": 20.0})
check("a hole is filled with two n-gons", len(result.faces) == 2, len(result.faces))
check("no vertex is duplicated -- the boundary weld would destroy the face",
      len(result.verts) == len(set(tuple(round(c, 6) for c in v) for v in result.verts)),
      len(result.verts))
for i, face in enumerate(result.faces):
    check(f"face {i} uses each of its vertices once", len(face) == len(set(face)), face)
check("every vertex is used", set(result.faces[0]) | set(result.faces[1])
      == set(range(len(result.verts))))
check("one corner index per side of both loops",
      len(result.corner_local_indices) == len(prepared.corner_source_ids),
      f"{len(result.corner_local_indices)} vs {len(prepared.corner_source_ids)}")


def edges_of(face):
    return {frozenset((face[i], face[(i + 1) % len(face)])) for i in range(len(face))}


shared = edges_of(result.faces[0]) & edges_of(result.faces[1])
check("the two faces are joined by exactly two bridge edges", len(shared) == 2, len(shared))
# A bridge runs from the outer boundary into the hole; every other shared edge
# would mean the cut doubled back on itself.
outer_count = len(pr.generators.ngon.loop_points(prepared.loops_sides[0], 20.0)[0])
for bridge in shared:
    a, b = tuple(bridge)
    check("a bridge joins the outer boundary to the hole",
          (a < outer_count) != (b < outer_count), f"{a}, {b} (outer < {outer_count})")

# --- end to end ---
state.ngon_mode = True
bpy.context.view_layer.objects.active = holed
pr.operators.enter_session_object(bpy.context, holed)
pr.operators.set_active_patch(bpy.context, holed, 5)
check("the holed face is picked up by N-gon mode",
      state.generator_name == pr.generators.NGON.name, state.generator_name)
preview = bpy.data.objects.get(pr.mesh_build.PREVIEW_OBJ_NAME)
check("the preview holds two faces", len(preview.data.polygons) == 2,
      len(preview.data.polygons))

bpy.ops.retop.commit_patch()
committed = bpy.data.objects.get(pr.mesh_build.result_object_name_for(holed))
check("and both survive the commit and its boundary weld",
      len(committed.data.polygons) == 2, len(committed.data.polygons))
check("the hole is still a hole (no face paved over it)",
      len(committed.data.vertices) == len(result.verts), len(committed.data.vertices))

registry = pr.mesh_build.get_span_registry(committed)
check("both loops registered their segments for propagation",
      len(registry) >= 8, len(registry))
pr.operators.end_session(bpy.context)

print()
if FAILURES:
    print(f"=== {len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(1)
print("=== ALL CHECKS PASSED")
