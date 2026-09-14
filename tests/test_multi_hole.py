"""Run inside Blender: blender --background --python tests/test_multi_hole.py

A CAD face with more than one hole. Every span generator paves a single
outline, so those still get the outer boundary alone -- what this pins is the
n-gon fill, which bridges each hole into the face around it and comes back with
one more n-gon per hole.

The assertion that carries the weight is that every emitted face is a *simple*
polygon. A bridge is two edges drawn across the region, and with two holes in
it there is nothing stopping one of them being drawn straight through the other
-- which Blender does not refuse, it tessellates into a bowtie. So each face is
checked edge against edge in the patch's own plane, and the crossing bridge is
shown to be rejected rather than merely unused.
"""
import os
import sys
import importlib

_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_ADDON_DIR))

import bpy
import mathutils
from mathutils.geometry import tessellate_polygon

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
ngon = pr.generators.ngon


# ===========================================================================
# A flat plate with two holes, tessellated the way the bridge sends one
# ===========================================================================

def square(cx, cy, half):
    """A square as four corners, counter-clockwise."""
    return [(cx - half, cy - half, 0.0), (cx + half, cy - half, 0.0),
            (cx + half, cy + half, 0.0), (cx - half, cy + half, 0.0)]


OUTER = square(0.0, 0.0, 3.0)
HOLE_A = square(-1.5, 0.0, 0.6)
HOLE_B = square(1.5, 0.0, 0.6)
FACE_ID = 5


def build(name, loops):
    """One mesh carrying one Plasticity face, triangulated across its holes.

    `tessellate_polygon` takes the outer outline first and every later polyline
    as a hole, which is the same order the addon sorts boundary loops into --
    so the fixture is built by the same convention it is then read back with.
    """
    verts = [co for loop in loops for co in loop]
    flat = [[mathutils.Vector(co) for co in loop] for loop in loops]
    tris = [tuple(t) for t in tessellate_polygon(flat)]

    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], tris)
    mesh.update()
    mesh["groups"] = [0, len(tris) * 3]
    mesh["face_ids"] = [FACE_ID]
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj


plate = build("TwoHoles", [OUTER, HOLE_A, HOLE_B])
check("the plate is flat", pr.patchprep.patch_is_planar(plate.data, FACE_ID, 5.0))

spans = pr.patchprep.prepare_patch(plate.data, FACE_ID, 135.0, 0.0, 'BOTH')
check("it really has three boundary loops", spans.num_loops == 3, spans.num_loops)
check("a span generator is still handed the outer boundary alone",
      len(spans.loops_sides) == 1, len(spans.loops_sides))

prepared = pr.patchprep.prepare_patch(plate.data, FACE_ID, 135.0, 0.0, 'BOTH',
                                      keep_holes=True)
check("asking for the holes keeps all three loops",
      len(prepared.loops_sides) == 3, len(prepared.loops_sides))
check("and the patch reports itself as holed rather than as a ring",
      prepared.has_holes and not prepared.is_ring)

result = pr.generators.NGON.generate_holed(prepared.loops_sides, {"ngon_angle": 20.0})
check("two holes come back as three n-gons", len(result.faces) == 3,
      len(result.faces))
check("no vertex is duplicated -- the boundary weld would destroy the face",
      len(result.verts) == len(set(tuple(round(c, 6) for c in v) for v in result.verts)),
      len(result.verts))
for i, face in enumerate(result.faces):
    check(f"face {i} uses each of its vertices once", len(face) == len(set(face)), face)
check("every vertex is used",
      set().union(*(set(f) for f in result.faces)) == set(range(len(result.verts))))


# --- every face is a simple polygon ----------------------------------------
#
# In the plate's own plane, which here is just XY.

def is_simple(face, verts):
    """Whether a face's outline crosses itself. Adjacent edges share a corner
    and are skipped; everything else must miss everything else."""
    count = len(face)
    uv = [(verts[v].x, verts[v].y) for v in face]
    for i in range(count):
        for j in range(i + 1, count):
            if j == i or (j + 1) % count == i or (i + 1) % count == j:
                continue
            if ngon._segments_cross(uv[i], uv[(i + 1) % count],
                                    uv[j], uv[(j + 1) % count]):
                return False
    return True


for i, face in enumerate(result.faces):
    check(f"face {i} does not cross itself", is_simple(face, result.verts))

# A bridge is a doubled edge, so its two ends are the only vertices appearing
# on two faces -- two bridges per hole, four ends each, two holes.
used = [sum(1 for f in result.faces for v in f if v == vertex)
        for vertex in range(len(result.verts))]
check("every vertex is on at least one face", all(u >= 1 for u in used), used)
check("and only a bridge end is on more than one", sum(1 for u in used if u > 1) == 8,
      sum(1 for u in used if u > 1))


# --- the crossing bridge is refused, not merely unused ---------------------
#
# Without this the test above proves nothing: a fill that happened to pick
# honest bridges by luck would pass it.

loops_uv = [[(co[0], co[1]) for co in loop] for loop in (OUTER, HOLE_A, HOLE_B)]
# From the middle of the plate's left edge to the far corner of the *right*
# hole: on its way there it goes straight through the left one.
check("a bridge drawn through another hole is refused",
      not ngon._bridge_is_clear((-3.0, 0.0), (2.1, 0.6), loops_uv, []))
check("and the one that reaches the near hole is not",
      ngon._bridge_is_clear((-3.0, 0.0), (-2.1, 0.6), loops_uv, []))

# And a bridge that leaves the face through a notch in a concave outline.
notched = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (2.2, 4.0),
           (2.2, 1.0), (1.8, 1.0), (1.8, 4.0), (0.0, 4.0)]
check("a bridge across a notch in the outline is refused",
      not ngon._bridge_is_clear((1.0, 3.0), (3.0, 3.0), [notched], []))
check("and one that stays in the same arm of it is not",
      ngon._bridge_is_clear((0.5, 3.0), (1.5, 3.0), [notched], []))


# ===========================================================================
# Through the pipeline: the mode is what decides whether the holes survive
# ===========================================================================

state.ngon_mode = True
bpy.context.view_layer.objects.active = plate
pr.operators.enter_session_object(bpy.context, plate)
pr.operators.set_active_patch(bpy.context, plate, FACE_ID)
check("a flat face with two holes is generated as an n-gon",
      state.generator_name == pr.generators.NGON.name, state.generator_name)
check("and the panel is told there are three loops", state.num_loops == 3,
      state.num_loops)
preview = bpy.data.objects.get(pr.mesh_build.PREVIEW_OBJ_NAME)
check("the preview carries the three faces",
      preview is not None and len(preview.data.polygons) == 3,
      0 if preview is None else len(preview.data.polygons))

bpy.ops.retop.commit_patch()
retop = bpy.data.objects.get("TwoHoles_Retop")
check("committing keeps them", retop is not None and len(retop.data.polygons) == 3,
      0 if retop is None else len(retop.data.polygons))

# Re-opening it has to come back as the n-gon it was committed as, holes and
# all -- same rule as a patch's spans.
pr.operators.set_active_patch(bpy.context, plate, FACE_ID)
check("re-opening it comes back as the same n-gon",
      state.generator_name == pr.generators.NGON.name and state.num_loops == 3,
      f"{state.generator_name} / {state.num_loops}")
bpy.ops.retop.clear_preview()

# With the mode off, the holes are the ones that go: no span generator paves
# more than one outline. The patch is rerouted to the n-gon fill for exactly
# that reason, so what is pinned here is the reroute, not a covered-over face.
state.ngon_mode = False
pr.operators.set_active_patch(bpy.context, plate, FACE_ID)
check("a committed n-gon is not rerouted away from itself on re-edit",
      state.generator_name == pr.generators.NGON.name, state.generator_name)
bpy.ops.retop.clear_preview()
pr.operators.end_session(bpy.context)

fresh = build("TwoHolesFresh", [OUTER, HOLE_A, HOLE_B])
bpy.context.view_layer.objects.active = fresh
pr.operators.enter_session_object(bpy.context, fresh)
pr.operators.set_active_patch(bpy.context, fresh, FACE_ID)
check("an uncommitted flat face with holes is rerouted to the n-gon fill",
      state.generator_name == pr.generators.NGON.name, state.generator_name)
check("and says why", "holes" in state.generator_note, state.generator_note)
bpy.ops.retop.clear_preview()
pr.operators.end_session(bpy.context)

print()
if FAILURES:
    print(f"=== {len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(1)
print("=== ALL CHECKS PASSED")
