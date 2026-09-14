"""Run inside Blender: blender --background --python tests/test_copy_density.py

Copying one committed patch's density onto another.

Span propagation already handles two patches that *touch*: they share a
boundary, so they have to agree or they crack. This is the other half -- two
patches that never meet and want the same density anyway, which nothing in the
mesh can work out on its own.

What makes it answerable is that the generator each patch was built by is
recorded at commit time, alongside its spans. A count copied between two
different generators would be a number with a different meaning: a Ring's two
are around and across, a Quad's are its own U and V. So the copy is refused
across generators, and this pins that as much as the copy itself.
"""
import os
import sys
import importlib

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

# ---------------------------------------------------------------------------
#  Two square patches that do not touch, plus a strip that borders neither.
#
#   A (id 1)          B (id 2)
#   x 0..2            x 4..6
# ---------------------------------------------------------------------------
verts = []
tris = []
groups = []
for index, x0 in enumerate((0.0, 4.0)):
    base = len(verts)
    verts += [(x0, 0.0, 0.0), (x0 + 2.0, 0.0, 0.0),
              (x0 + 2.0, 2.0, 0.0), (x0, 2.0, 0.0)]
    start = len(tris) * 3
    tris += [(base, base + 1, base + 2), (base, base + 2, base + 3)]
    groups += [start, 6]

mesh = bpy.data.meshes.new("CopyMesh")
mesh.from_pydata(verts, [], tris)
mesh.update()
mesh["groups"] = groups
mesh["face_ids"] = [1, 2]

obj = bpy.data.objects.new("CopyObj", mesh)
bpy.context.collection.objects.link(obj)
bpy.context.view_layer.objects.active = obj

pr.operators.enter_session_object(bpy.context, obj)
state.ngon_mode = False

# --- commit A with a density nothing would have computed -------------------
pr.operators.set_active_patch(bpy.context, obj, 1)
check("A is a quad", state.generator_name == "Quad", state.generator_name)
state.span_u = 7
state.span_v = 3
bpy.ops.retop.commit_patch()
check("A is committed", state.committed_patch_count == 1, state.committed_patch_count)

stored = pr.mesh_build.lookup_patch_settings(obj, 1)
check("its generator is recorded, which is what makes the copy answerable",
      stored and stored.get("generator") == "Quad", stored)
check("and its spans with it", (stored.get("span_u"), stored.get("span_v")) == (7, 3),
      (stored.get("span_u"), stored.get("span_v")))

# --- open B and take A's density ------------------------------------------
pr.operators.set_active_patch(bpy.context, obj, 2)
check("B opens with its own computed spans, not A's",
      (state.span_u, state.span_v) != (7, 3), (state.span_u, state.span_v))

done, message = pr.operators.copy_spans_from(bpy.context, 1)
check("copying from A is accepted", done, message)
check("and B now has A's density", (state.span_u, state.span_v) == (7, 3),
      (state.span_u, state.span_v))
check("the report says where it came from", "patch 1" in message, message)

# The preview has to have been rebuilt: assigning a span fires the update
# callback, which is the same path the wheel takes.
preview = bpy.data.objects.get(pr.mesh_build.PREVIEW_OBJ_NAME)
check("and the preview was regenerated at that density",
      preview is not None and len(preview.data.polygons) == 7 * 3,
      preview and len(preview.data.polygons))

# --- the refusals ----------------------------------------------------------
done, message = pr.operators.copy_spans_from(bpy.context, 2)
check("a patch cannot copy from itself", not done, message)
check("and says so", "this patch" in message, message)

done, message = pr.operators.copy_spans_from(bpy.context, 999)
check("nor from a face that was never committed", not done, message)
check("naming it, rather than failing silently", "999" in message, message)

# --- clicking the same patch again turns U and V over ----------------------
# Which of a quad's directions is U comes from where its boundary walk started,
# so a copy lands rotated about half the time and nothing in either patch can
# say in advance which half. A second click is the answer -- one gesture, two
# states, the same shape as clicking a matched side to release it.
done, message = pr.operators.copy_spans_from(bpy.context, 1)
check("clicking the same patch again swaps the two spans", done, message)
check("so U and V come back exchanged", (state.span_u, state.span_v) == (3, 7),
      (state.span_u, state.span_v))
check("and the report says it was swapped", "swap" in message.lower(), message)

done, message = pr.operators.copy_spans_from(bpy.context, 1)
check("and a third click turns them back", (state.span_u, state.span_v) == (7, 3),
      (state.span_u, state.span_v))

# The tooltip has to preview the *next* click, not describe the record: a
# tooltip that reads U=7 on a click that is about to apply V=7 is worse than
# none.
title, detail = pr.mesh_build.copy_source_status(state, obj, 1)
check("the tooltip offers the swap once a copy has been taken",
      "Swap" in title, title)
check("and shows the spans the way that click would apply them",
      "U=3" in detail and "V=7" in detail, detail)

# Starting a different patch forgets it, or the first click there would come
# back swapped for a reason belonging to the patch before.
state.copy_source_face_id = -1
state.copy_source_swapped = False

# --- across generators -----------------------------------------------------
# Rewrite A's record as if it had been committed as a Ring. Cheaper than
# building a ring here, and it is the *record* the rule reads -- which is the
# thing being tested.
pr.mesh_build.register_patch_settings(obj, 1, 4, 12, 4, "Ring")
before = (state.span_u, state.span_v)
done, message = pr.operators.copy_spans_from(bpy.context, 1)
check("a Ring's density is not copied onto a Quad", not done, message)
check("and the refusal names both generators",
      "Ring" in message and "Quad" in message, message)
check("B's spans are untouched by the refusal",
      (state.span_u, state.span_v) == before, (state.span_u, state.span_v))

# ===========================================================================
# The click has to reach the operator at all
#
# It did not: clicks are held out of the modal's key dispatch on purpose -- a
# plain one means "the thing under the cursor", which only the side picker can
# resolve -- and that exclusion swallowed *modified* clicks too. Ctrl+click
# committed the patch and left ADJUST with nothing copied.
# ===========================================================================
class FakeEvent:
    def __init__(self, type, value='PRESS', ctrl=False, shift=False, alt=False):
        self.type = type
        self.value = value
        self.ctrl = ctrl
        self.shift = shift
        self.alt = alt
        self.oskey = False


plain = pr.keymap.session_actions_for(FakeEvent('LEFTMOUSE'))
ctrl = pr.keymap.session_actions_for(FakeEvent('LEFTMOUSE', ctrl=True))
# Each button now carries two actions, separated by their polls rather than by
# their modifiers: the corner editor takes both while it is open, and declares
# them first so it wins. What must not change is which one answers with the
# editor closed, which is every case this file is about.
check("a plain left click offers the side picker, under the corner editor",
      plain == ["corner_toggle", "pin_neighbour"], plain)
check("and Ctrl+click offers the density copy, under the group editor's two",
      ctrl == ["corner_toggle_back", "corners_edit", "copy_spans"], ctrl)
check("with the editor closed the corner actions poll false, so the click is the copy's",
      not state.corner_edit and not bpy.ops.retop.toggle_corner.poll()
      and not bpy.ops.retop.corners_accept.poll())
check("the two never resolve to each other -- modifiers compare exactly",
      not set(plain) & set(ctrl), (plain, ctrl))

# The modal must dispatch it rather than fall through to its commit fallback.
# Read from the source: the fallback is the last thing a LEFTMOUSE meets, so
# what matters is that a dispatch attempt comes before it.
import inspect                                                      # noqa: E402
source = inspect.getsource(pr.operators.RETOP_OT_session._modal)
click_at = source.index("if event.type == 'LEFTMOUSE' and event.value == 'PRESS':")
tail = source[click_at:]
check("the modal dispatches a bound click before falling back to commit",
      tail.index("_dispatch_bound") < tail.index("self._commit("),
      tail[:200])

# ===========================================================================
# The hover that says what a click would take
# ===========================================================================
state.session_phase = 'ADJUST'
state.active_face_id = 2
state.generator_name = "Quad"
pr.mesh_build.register_patch_settings(obj, 1, 7, 3, 4, "Quad")

title, detail = pr.mesh_build.copy_source_status(state, obj, 1)
check("a matching patch offers its density", title.startswith("Copy"), title)
check("and the tooltip reads the spans out, so the click is not a guess",
      "U=7" in detail and "V=3" in detail, detail)

pr.mesh_build.register_patch_settings(obj, 1, 4, 12, 4, "Ring")
title, detail = pr.mesh_build.copy_source_status(state, obj, 1)
check("a patch of another generator still names itself",
      title == "Patch 1: Ring", title)
check("and says why the click will refuse, rather than going quiet",
      "Ring" in detail and "Quad" in detail, detail)
check("a face that was never committed offers nothing",
      pr.mesh_build.copy_source_status(state, obj, 999) == ("", ""),
      pr.mesh_build.copy_source_status(state, obj, 999))

pr.operators.end_session(bpy.context)

print()
if FAILURES:
    print(f"=== {len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(1)
print("=== ALL CHECKS PASSED")
