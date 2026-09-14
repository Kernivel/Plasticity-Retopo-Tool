"""Run inside Blender: blender --background --python tests/test_patch_debug.py

The patch data debug display: `patch_data.group_report`, the labels
`cad_display` places from it, and how the cursor chooses between them.

Three things are pinned here, and the second is the reason the first exists.

**Triangulation is not required.** The bridge offers an untriangulated export
and nothing in this addon cares: `polygon_face_ids` walks the group ranges in
loop-index space, and `geometry.build_bvh_with_polygon_map` fan-triangulates
whatever it is handed. So a quad mesh must come back with an empty problem list
and its patches assigned correctly -- only `triangulated` says which export was
used. Reporting it as a fault would send people re-exporting to fix something
that was never broken.

**Ranges that no longer land on polygon boundaries are.** That is the failure
worth warning about, because `polygon_face_ids` cannot see it: it hands a
polygon to whichever range it falls in and returns a complete, plausible,
wrong answer. The corruption test therefore asserts both that the report names
the problem *and* that the assignment really does go wrong -- without the
second half the warning could be firing on a case that does not matter.

**Which patch gets labelled is a cursor position.** A draw handler is handed no
event, so the hover modal leaves `(object name, face id)` in a module global on
`overlay` and the display resolves the target from there -- which means the two
states nothing else can produce have to be safe: no hover at all (a reload, a
file load, the pointer off the viewport) and a hover naming an object that has
since gone away.
"""
import os
import sys
import importlib

_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_ADDON_DIR))

import bpy

pr = importlib.import_module(os.path.basename(_ADDON_DIR))
patch_data = pr.patch_data
cad_display = pr.cad_display

FAILURES = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")
    if not cond:
        FAILURES.append(name)


def build(name, verts, faces, groups, face_ids):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    mesh["groups"] = groups
    mesh["face_ids"] = face_ids
    return mesh


# ===========================================================================
# A well-formed triangulated import
#
# Two CAD faces, each tessellated into two triangles, each with its own copies
# of the shared border -- which is what the bridge really emits.
# ===========================================================================
TRI_VERTS = [
    (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0),
    (0.0, 1.0, 0.0), (1.0, 1.0, 0.0), (1.0, 2.0, 0.0), (0.0, 2.0, 0.0),
]
TRI_FACES = [(0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7)]
# Two triangles per face = 6 loops per group, one group per face id.
tri_mesh = build("DebugTri", TRI_VERTS, TRI_FACES, [0, 6, 6, 6], [101, 202])

report = patch_data.group_report(tri_mesh)
check("a clean import reports no problems", report.ok, report.problems)
check("and says so through .ok as well as the list",
      report.ok == (not report.problems))
check("one entry per face id", len(report.entries) == 2, len(report.entries))
check("the face ids come back in group order",
      [e.face_id for e in report.entries] == [101, 202])
check("loop ranges are read back verbatim",
      [(e.loop_start, e.loop_count) for e in report.entries] == [(0, 6), (6, 6)])
check("each group is walked back to its polygons",
      [(e.poly_start, e.poly_count) for e in report.entries] == [(0, 2), (2, 2)],
      [(e.poly_start, e.poly_count) for e in report.entries])
check("the loop total is the mesh's", report.loop_total == 12, report.loop_total)
check("all triangles", report.triangulated, report.polygon_sizes)
check("and the histogram says how many", report.polygon_sizes == {3: 4},
      report.polygon_sizes)

# The point of the whole exercise: one face id, several polygons.
check("one face id covers more than one polygon -- the thing groups exist for",
      all(e.poly_count > 1 for e in report.entries))


# ===========================================================================
# An untriangulated import
#
# The same two faces as single quads. Nothing here needs triangles, so this
# must be reported as *information*, never as a problem.
# ===========================================================================
QUAD_VERTS = list(TRI_VERTS)
QUAD_FACES = [(0, 1, 2, 3), (4, 5, 6, 7)]
quad_mesh = build("DebugQuad", QUAD_VERTS, QUAD_FACES, [0, 4, 4, 4], [101, 202])

quad_report = patch_data.group_report(quad_mesh)
check("an untriangulated import is not a problem", quad_report.ok,
      quad_report.problems)
check("but it is reported", not quad_report.triangulated,
      quad_report.polygon_sizes)
check("with the polygon sizes that make it so",
      quad_report.polygon_sizes == {4: 2}, quad_report.polygon_sizes)
check("and its patches are still assigned correctly",
      patch_data.polygon_face_ids(quad_mesh)[0] == [101, 202],
      patch_data.polygon_face_ids(quad_mesh)[0])

mixed_mesh = build("DebugMixed", TRI_VERTS,
                   [(0, 1, 2), (0, 2, 3), (4, 5, 6, 7)], [0, 6, 6, 4],
                   [101, 202])
mixed_report = patch_data.group_report(mixed_mesh)
check("a mixed mesh is fine too", mixed_report.ok, mixed_report.problems)
check("and counts each size", mixed_report.polygon_sizes == {3: 2, 4: 1},
      mixed_report.polygon_sizes)


# ===========================================================================
# Ranges that no longer fit the mesh
#
# The silent corruption: a group boundary that falls mid-polygon. Every check
# here has a second half asserting the damage is real -- a warning about a
# case that does not actually go wrong is a warning worth removing.
# ===========================================================================
truth = list(patch_data.polygon_face_ids(tri_mesh)[0])
check("the clean mesh assigns two polygons to each face",
      truth == [101, 101, 202, 202], truth)

# Shift the second group one loop early: it now starts mid-triangle.
tri_mesh["groups"] = [0, 7, 7, 5]
patch_data.invalidate(tri_mesh)
cad_display.invalidate(tri_mesh)

broken = patch_data.group_report(tri_mesh)
check("a mid-polygon group boundary is reported", not broken.ok,
      broken.problems)
check("and the message names the mesh having moved under the groups",
      any("re-topologized" in p for p in broken.problems), broken.problems)

damaged = list(patch_data.polygon_face_ids(tri_mesh)[0])
check("...and the assignment really is wrong -- or the warning proves nothing",
      damaged != truth, f"{damaged} vs {truth}")

# A gap between two ranges.
tri_mesh["groups"] = [0, 3, 6, 6]
patch_data.invalidate(tri_mesh)
cad_display.invalidate(tri_mesh)
gapped = patch_data.group_report(tri_mesh)
check("a gap between ranges is reported", not gapped.ok, gapped.problems)
check("and named as a gap", any("gap" in p for p in gapped.problems),
      gapped.problems)

# Ranges that stop short of the mesh.
tri_mesh["groups"] = [0, 3, 3, 3]
patch_data.invalidate(tri_mesh)
cad_display.invalidate(tri_mesh)
short = patch_data.group_report(tri_mesh)
check("groups that do not cover the mesh are reported", not short.ok,
      short.problems)
check("and say how far they got",
      any("6 loops" in p and "12" in p for p in short.problems), short.problems)

# One face id in two groups.
tri_mesh["groups"] = [0, 6, 6, 6]
tri_mesh["face_ids"] = [101, 101]
patch_data.invalidate(tri_mesh)
cad_display.invalidate(tri_mesh)
duped = patch_data.group_report(tri_mesh)
check("a repeated face id is reported", not duped.ok, duped.problems)
check("and named", any("repeated" in p for p in duped.problems), duped.problems)

# Counts that disagree.
tri_mesh["groups"] = [0, 6, 6, 6, 12, 3]
tri_mesh["face_ids"] = [101, 202]
patch_data.invalidate(tri_mesh)
cad_display.invalidate(tri_mesh)
lopsided = patch_data.group_report(tri_mesh)
check("len(groups) != 2 * len(face_ids) is reported", not lopsided.ok,
      lopsided.problems)

# A mesh with nothing on it at all.
plain = bpy.data.meshes.new("DebugPlain")
plain.from_pydata(TRI_VERTS, [], TRI_FACES)
plain.update()
bare = patch_data.group_report(plain)
check("a non-Plasticity mesh reports the missing properties", not bare.ok,
      bare.problems)
check("and has no entries to show", bare.entries == [])

# Put it back for the label tests below.
tri_mesh["groups"] = [0, 6, 6, 6]
tri_mesh["face_ids"] = [101, 202]
patch_data.invalidate(tri_mesh)
cad_display.invalidate(tri_mesh)
check("restored to a clean state", patch_data.group_report(tri_mesh).ok)


# ===========================================================================
# The labels themselves
# ===========================================================================
labels = cad_display.patch_labels(tri_mesh)
check("one label per patch", len(labels) == 2, len(labels))
check("carrying the numbers to write",
      sorted((lb.face_id, lb.loop_start, lb.loop_count) for lb in labels)
      == [(101, 0, 6), (202, 6, 6)],
      [(lb.face_id, lb.loop_start, lb.loop_count) for lb in labels])
check("and the polygon count", all(lb.poly_count == 2 for lb in labels),
      [lb.poly_count for lb in labels])

# The anchor must be *on* the patch it names: the mean of a concave outline is
# not, and the mean of an annulus is in its hole.
by_id = {lb.face_id: lb for lb in labels}
check("patch 101's anchor sits inside its own half of the mesh",
      0.0 <= by_id[101].anchor.y <= 1.0, by_id[101].anchor)
check("and patch 202's inside its own",
      1.0 <= by_id[202].anchor.y <= 2.0, by_id[202].anchor)
check("each anchor is a real polygon centre, so it lies on the surface",
      all(abs(lb.anchor.z) < 1e-9 for lb in labels),
      [lb.anchor.z for lb in labels])
check("every label carries a normal to cull by",
      all(abs(lb.normal.length - 1.0) < 1e-5 for lb in labels),
      [lb.normal.length for lb in labels])

# The labels are the whole mesh, always, keyed on the geometry alone. Which of
# them to draw is a filter over a few hundred entries at the point of drawing:
# scoping the cache would put a second, far more volatile key on it -- and the
# expensive half, a scan of every polygon centre, does not depend on the scope
# at all.
first = cad_display.patch_labels(tri_mesh)
check("an unchanged mesh is served from the cache, not rebuilt",
      first is cad_display.patch_labels(tri_mesh))
check("the integrity report is cached too",
      cad_display.integrity(tri_mesh) is cad_display.integrity(tri_mesh))
cad_display.invalidate(tri_mesh)
check("invalidate drops the label cache as well as the rest",
      cad_display.patch_labels(tri_mesh) is not first)

for poly in tri_mesh.polygons:
    poly.select = False
check("a selection change does not rebuild the labels -- they do not depend "
      "on it", cad_display.patch_labels(tri_mesh)
      is cad_display.patch_labels(tri_mesh))


# ===========================================================================
# The Selected scope, and the cache under it
#
# Its own fingerprint, because selection is not geometry: nothing moves when it
# changes, so `mesh_fingerprint` cannot see it.
# ===========================================================================
for poly in tri_mesh.polygons:
    poly.select = False
check("nothing selected means no face ids",
      cad_display.selected_face_ids(tri_mesh) == set(),
      cad_display.selected_face_ids(tri_mesh))

tri_mesh.polygons[2].select = True
check("selecting one polygon names its patch",
      cad_display.selected_face_ids(tri_mesh) == {202},
      cad_display.selected_face_ids(tri_mesh))

tri_mesh.polygons[0].select = True
check("a changed selection is picked up without an explicit invalidate",
      cad_display.selected_face_ids(tri_mesh) == {101, 202},
      cad_display.selected_face_ids(tri_mesh))
found = cad_display.selected_face_ids(tri_mesh)
check("and an unchanged one is served from the cache",
      cad_display.selected_face_ids(tri_mesh) is found)


# ===========================================================================
# The Hover scope
#
# Which patch to label is a cursor position, and a draw handler is given no
# event -- so the hover modal leaves (object name, face id) in a module global
# and the overlay resolves it from there. The two states nothing else can
# produce are the ones worth pinning: no hover at all (a reload, a file load,
# the pointer off the viewport), and a hover naming an object that has gone.
# ===========================================================================
pr.register()
overlay = pr.overlay

hover_obj = bpy.data.objects.new("DebugHoverObj", tri_mesh)
bpy.context.scene.collection.objects.link(hover_obj)

overlay.debug_hover = None
check("no hover names no object",
      overlay._patch_debug_target(bpy.context, 'HOVER') is None)

overlay.debug_hover = (hover_obj.name, 202)
check("a hover names the object it was taken on, whatever is active",
      overlay._patch_debug_target(bpy.context, 'HOVER') is hover_obj,
      overlay._patch_debug_target(bpy.context, 'HOVER'))

overlay.debug_hover = ("NoSuchObject", 202)
check("a hover on an object that has gone resolves to nothing",
      overlay._patch_debug_target(bpy.context, 'HOVER') is None)

# A mesh with no face ids is not a Plasticity import and has nothing to say.
plain_obj = bpy.data.objects.new("DebugPlainObj", plain)
bpy.context.scene.collection.objects.link(plain_obj)
overlay.debug_hover = (plain_obj.name, 0)
check("and neither has a hover on a mesh carrying no face ids",
      overlay._patch_debug_target(bpy.context, 'HOVER') is None)
overlay.debug_hover = None

# The hover is read from the global rather than from the scene: a property
# written on every mouse move would mark the file as modified, and reading a
# mesh must not.
check("the hover lives on the module, not in the scene",
      not hasattr(bpy.context.scene.plasticity_retop, "debug_hover"))


# ===========================================================================
# The panel block
#
# `ui._draw_patch_debug` is drawn by Blender and by nothing else, so -- like the
# draw handlers in `tests/test_overlay.py` -- a name that is not there is found
# by the user unless a test calls it. A stub layout is enough for that: what is
# being checked is that every branch runs, not what it looks like.
# ===========================================================================
class _StubLayout:
    """Records nothing; answers every UILayout call this panel block makes."""

    enabled = True
    alert = False

    def box(self):
        return _StubLayout()

    def column(self, align=False):
        return _StubLayout()

    def row(self, align=False):
        return _StubLayout()

    def label(self, text="", icon='NONE'):
        return None

    def prop(self, data, name, **kwargs):
        getattr(data, name)  # a property that has been renamed must fail here

    def operator(self, idname, **kwargs):
        check(f"the panel's {idname} exists",
              idname.split(".")[1] in dir(bpy.ops.retop))
        return _StubLayout()

    def separator(self):
        return None


ui = pr.ui
panel_state = bpy.context.scene.plasticity_retop
panel_state.debug_patch_ids = True
for scope in ('HOVER', 'SELECTED', 'ALL'):
    panel_state.debug_patch_scope = scope
    for hover in (None, (hover_obj.name, 202), (hover_obj.name, 9999),
                  ("NoSuchObject", 202)):
        overlay.debug_hover = hover
        try:
            ui._draw_patch_debug(_StubLayout(), panel_state, hover_obj)
            ok, detail = True, ""
        except Exception as exc:  # noqa: BLE001 -- reporting it *is* the test
            ok, detail = False, repr(exc)
        check(f"_draw_patch_debug draws with scope={scope} hover={hover}",
              ok, detail)

# And with nothing to describe, which is what an empty scene gives it.
overlay.debug_hover = None
try:
    ui._draw_patch_debug(_StubLayout(), panel_state, None)
    ok, detail = True, ""
except Exception as exc:  # noqa: BLE001
    ok, detail = False, repr(exc)
check("_draw_patch_debug draws with no object at all", ok, detail)
panel_state.debug_patch_ids = False


# ===========================================================================
# Registering under Blender's restricted context
#
# `operators.register` calls `sync_patch_hover`, which asks whether the display
# wants its modal running -- and Blender registers an addon under a
# `_RestrictContext` that has no `scene` on it at all, because no file is
# loaded yet. Reaching straight for `context.scene` there raised
# `'_RestrictContext' object has no attribute 'scene'` and took the whole
# registration down with it: enabling the addon, or deploying and re-enabling
# it, failed outright while the identical code was fine once running.
#
# There is nothing to want at that moment anyway -- the scene the toggle lives
# on arrives with `load_post`, which asks again.
# ===========================================================================
class _RestrictedContext:
    """What Blender hands an addon during registration: no `scene`."""


try:
    wanted = pr.operators._patch_hover_wanted(_RestrictedContext())
    ok, detail = True, repr(wanted)
except Exception as exc:  # noqa: BLE001
    ok, detail = False, repr(exc)
check("the hover check survives a context with no scene", ok, detail)
check("and answers no, since there is no toggle to read yet",
      ok and wanted is False, detail)

# The whole registration, which is what the user actually hits. Not a
# restricted context here -- no test can produce one -- so this only pins that
# a register/unregister round trip stays clean alongside the guard above.
try:
    pr.unregister()
    pr.register()
    ok, detail = True, ""
except Exception as exc:  # noqa: BLE001
    ok, detail = False, repr(exc)
check("register survives a full round trip", ok, detail)


print()
if FAILURES:
    print(f"=== {len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(1)
print("=== ALL CHECKS PASSED")
