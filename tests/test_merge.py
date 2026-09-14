"""Run inside Blender: blender --background --python tests/test_merge.py

Merging several Plasticity faces into one patch.

One CAD face is one patch, and that is the input contract -- but a model is cut
into faces by the modelling history, not by what wants a single grid across it.
A boss with two fillet rings around it is five faces one sheet of retopology
should cross.

What this pins is that the merge happens at the *parse*: `build_patches` is
handed a remapped polygon->face-id map, the borders between the members cancel
in `compute_boundary_loops` by the same rule a face's own triangulation edges
do, and everything downstream keeps seeing one patch with one id. So the checks
below are mostly about the boundary that comes back, and about the two ways a
merge is refused -- parts that do not touch, and a group naming faces the mesh
no longer has.

Each of them asserts the *un*merged behaviour too. A test that only said "the
merged rectangle has four sides" would pass just as well on a build where
merging did nothing at all.
"""
import importlib
import os
import sys

_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_ADDON_DIR))

import bpy

pr = importlib.import_module(os.path.basename(_ADDON_DIR))
patch_data = pr.patch_data
patchprep = pr.patchprep

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


# ---------------------------------------------------------------------------
# A flat strip of three square faces side by side, plus one detached square.
#
# Flat and square on purpose: a rectangle's corner set is not in question, so
# anything the side count says afterwards is about the merge and not about the
# corner detection.
# ---------------------------------------------------------------------------
verts = [(x, y, 0.0) for y in (0.0, 1.0) for x in (0.0, 1.0, 2.0, 3.0)]
verts += [(x, y, 0.0) for y in (0.0, 1.0) for x in (10.0, 11.0)]


def v(i, j):
    return j * 4 + i


tris = []
groups = []
face_ids = []
for k, face_id in enumerate((10, 11, 12)):
    groups.extend([len(tris) * 3, 6])
    face_ids.append(face_id)
    tris.append((v(k, 0), v(k + 1, 0), v(k + 1, 1)))
    tris.append((v(k, 0), v(k + 1, 1), v(k, 1)))

groups.extend([len(tris) * 3, 6])
face_ids.append(20)
tris.append((8, 9, 11))
tris.append((8, 11, 10))

mesh = bpy.data.meshes.new("MergeTestMesh")
mesh.from_pydata(verts, [], tris)
mesh.update()
mesh["groups"] = groups
mesh["face_ids"] = face_ids

obj = bpy.data.objects.new("MergeTestObj", mesh)
bpy.context.collection.objects.link(obj)


def loop_of(face_id):
    patch = patch_data.analyse(mesh).patches.get(face_id)
    return patch.boundary_loops[0] if patch and patch.boundary_loops else []


def side_count(face_id):
    prepared = patchprep.prepare_patch(mesh, face_id, 45.0, 0.0, 'ANGLE')
    return len(prepared.sides) if prepared else 0


# ---------------------------------------------------------------------------
# 1. Unmerged: four patches, and the border between two of them is real
# ---------------------------------------------------------------------------
analysis = patch_data.analyse(mesh)
check("four patches before any merge", sorted(analysis.patches) == [10, 11, 12, 20],
      f"got {sorted(analysis.patches)}")
check("nothing is merged yet", analysis.merges == {})
check("each face is its own square", len(loop_of(10)) == 4 and len(loop_of(11)) == 4,
      f"got {len(loop_of(10))} / {len(loop_of(11))}")
check("and they know they border each other",
      patch_data.patch_neighbour_ids(analysis.patches[10]) == {11},
      f"got {patch_data.patch_neighbour_ids(analysis.patches[10])}")


# ---------------------------------------------------------------------------
# 2. Merged: the border between the members is gone
#
# Six boundary vertices, not eight: the two the members shared are still on the
# outline (a rectangle twice as long has a vertex mid-side), but the *segment*
# between them is not a boundary any more -- it has a polygon on both sides,
# both of them this patch's, so it cancels.
# ---------------------------------------------------------------------------
merged_id = patch_data.next_merged_id({})
patch_data.write_merges(mesh, {merged_id: [10, 11]})

analysis = patch_data.analyse(mesh)
check("the cache noticed without being told",
      sorted(analysis.patches) == sorted([merged_id, 12, 20]),
      f"got {sorted(analysis.patches)}")
check("the merge is reported on the analysis", analysis.merges == {merged_id: [10, 11]})
check("one boundary loop, six vertices round the union", len(loop_of(merged_id)) == 6,
      f"got {len(loop_of(merged_id))}")
check("the union borders the third face, not its own members",
      patch_data.patch_neighbour_ids(analysis.patches[merged_id]) == {12},
      f"got {patch_data.patch_neighbour_ids(analysis.patches[merged_id])}")
check("the members' polygons all went into it",
      len(analysis.patches[merged_id].poly_indices) == 4,
      f"got {len(analysis.patches[merged_id].poly_indices)}")
check("and the union is still a quad, so it still gets a grid",
      side_count(merged_id) == 4, f"got {side_count(merged_id)}")


# ---------------------------------------------------------------------------
# 3. A group that cannot be applied is reported, never silently dropped
#
# A re-export renumbers every face id, so this is the state a merge written
# before one ends up in -- and a merge that quietly stops being a merge looks
# exactly like an addon that forgot it.
# ---------------------------------------------------------------------------
patch_data.write_merges(mesh, {merged_id: [10, 11], -2000: [12, 999]})
analysis = patch_data.analyse(mesh)
check("a group naming a face the mesh does not have is dropped",
      analysis.dropped_merges == [-2000], f"got {analysis.dropped_merges}")
check("and the one beside it still applies", merged_id in analysis.merges)
check("the named face is a patch of its own again", 12 in analysis.patches)


# ---------------------------------------------------------------------------
# 4. Splitting puts it back exactly
# ---------------------------------------------------------------------------
patch_data.write_merges(mesh, {})
analysis = patch_data.analyse(mesh)
check("split back into the faces it was made of",
      sorted(analysis.patches) == [10, 11, 12, 20], f"got {sorted(analysis.patches)}")
check("and the border between them is a boundary again", len(loop_of(10)) == 4)
check("the property is gone rather than left empty",
      mesh.get(patch_data.MERGE_PROP) is None)


# ---------------------------------------------------------------------------
# 5. Contiguity, checked as the selection is built
#
# A union whose parts only meet at a point has a pinched boundary; one whose
# parts do not meet at all comes back with two outer loops, which the loop
# count reads as a band and the Ring generator then stretches across the gap.
# ---------------------------------------------------------------------------
operators = pr.operators
state = bpy.context.scene.plasticity_retop
state.merge_selection = ""

ok, message = operators.toggle_merge_face(bpy.context, obj, 10)
check("the first patch is always takeable", ok, message)
check("and it is in the selection", operators.merge_selection(state) == [10])

ok, message = operators.toggle_merge_face(bpy.context, obj, 20)
check("a detached face is refused", not ok, message)
ok, message = operators.toggle_merge_face(bpy.context, obj, 12)
check("so is one that only touches through a face not selected", not ok, message)
ok, _message = operators.toggle_merge_face(bpy.context, obj, 11)
check("a neighbour is taken", ok)
ok, _message = operators.toggle_merge_face(bpy.context, obj, 12)
check("and now the third one is contiguous too", ok)
check("all three selected", operators.merge_selection(state) == [10, 11, 12])

ok, _message = operators.toggle_merge_face(bpy.context, obj, 12)
check("clicking a selected patch drops it again",
      ok and operators.merge_selection(state) == [10, 11])


# ---------------------------------------------------------------------------
# 5b. The overlay draws the selection, and says how to build one
#
# The draw handlers are the one code Blender alone invokes, so nothing else
# notices when they break -- and a highlight is only ever seen by running it.
# The assertion is that the callback gets all the way down to the GPU call,
# which headless Blender has none of: a name that is not there raises long
# before that.
# ---------------------------------------------------------------------------
overlay = pr.overlay
triangles = pr.cad_display.patch_triangles(mesh, 10)
check("a patch's fill is its own polygons, fanned", len(triangles) == 6,
      f"got {len(triangles)}")
check("and it is cached on the mesh like everything a draw handler reads",
      pr.cad_display.patch_triangles(mesh, 10) is triangles)

state.session_active = True
state.session_phase = 'PATCH'
state.session_object_name = obj.name
operators.set_merge_selection(state, [10, 11])

overlay.enable()
try:
    overlay._draw_points()
    reached = "no error"
except SystemError as exc:
    reached = "gpu" if "background mode" in str(exc) else repr(exc)
except Exception as exc:  # noqa: BLE001
    reached = repr(exc)
check("the highlight draws down to the GPU call and no further",
      reached in ("gpu", "no error"), reached)
overlay.disable()

hints = dict(overlay.keybinds_for(state))
check("the viewport says how many are picked",
      any("2 picked" in action for action in hints.values()), hints)
operators.set_merge_selection(state, [])
hints = dict(overlay.keybinds_for(state))
check("and names the gesture before anything is picked -- it is behind a "
      "modifier, which nobody finds on their own",
      any(action == "Add to merge" for action in hints.values()), hints)
state.session_active = False
state.session_object_name = ""


# ---------------------------------------------------------------------------
# 6. Applying it, and flattening
#
# Merging something already merged replaces the group it stood on rather than
# nesting inside it, so a member list is always raw Plasticity face ids.
# ---------------------------------------------------------------------------
new_id, message = operators.apply_merge(bpy.context, obj, [10, 11])
check("the merge is written", new_id is not None and new_id <= patch_data.MERGED_ID_BASE,
      f"got {new_id}")
check("with both members", patch_data.read_merges(mesh)[new_id] == [10, 11])

second_id, _message = operators.apply_merge(bpy.context, obj, [new_id, 12])
merges = patch_data.read_merges(mesh)
check("merging a merged patch flattens", merges.get(second_id) == [10, 11, 12],
      f"got {merges}")
check("and the group it stood on is gone", new_id not in merges)
check("the new id is not one that was just freed", second_id != new_id)

done, message = operators.split_merge(bpy.context, obj, second_id)
check("and it splits back", done and patch_data.read_merges(mesh) == {}, message)

state.merge_selection = ""


# ---------------------------------------------------------------------------
# 7. A merged patch commits, and reopens as the same patch
#
# The riskiest part of doing the merge at the parse: everything downstream
# keeps treating an id as an id, and a merged one is *negative*. It lands in
# `PATCH_ID_ATTR` on every committed face, which is the int layer re-editing
# and adoption read back -- so what this checks is that a synthetic id survives
# that round trip like a Plasticity one does.
# ---------------------------------------------------------------------------
bpy.context.view_layer.objects.active = obj
pr.operators.enter_session_object(bpy.context, obj)
state.ngon_mode = False

merged_id, _message = operators.apply_merge(bpy.context, obj, [10, 11])
generator, num_sides, _propagated = operators.set_active_patch(bpy.context, obj, merged_id)
check("the merged patch generates", generator is not None, f"got {generator}")
check("as a quad", num_sides == 4, f"got {num_sides}")
check("and it is not a re-edit -- nothing was ever committed here",
      not state.editing_committed)

state.span_u = 2
state.span_v = 2
bpy.ops.retop.commit_patch()

result = bpy.data.objects.get(pr.mesh_build.result_object_name_for(obj))
stamped = {poly_id for poly_id in
           (result.data.attributes[pr.mesh_build.PATCH_ID_ATTR].data[i].value
            for i in range(len(result.data.polygons)))}
check("every committed face names the merged patch", stamped == {merged_id},
      f"got {stamped}")
check("which the result mesh agrees is committed",
      pr.mesh_build.is_patch_committed(obj, merged_id))

operators.set_active_patch(bpy.context, obj, merged_id)
check("re-picking it reopens the same patch", state.editing_committed)
check("and takes its faces back out", state.reedit_removed_faces > 0,
      state.reedit_removed_faces)

# Splitting is refused while it is committed: the faces carry an id that would
# name a patch nothing could reach, so nothing would ever delete them again.
done, message = operators.split_merge(bpy.context, obj, merged_id)
check("a committed merge refuses to split", not done, message)

pr.operators.end_session(bpy.context)


print()
if FAILURES:
    print(f"=== {len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(1)
print("=== ALL CHECKS PASSED")
