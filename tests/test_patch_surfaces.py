"""Run inside Blender: blender --background --python tests/test_patch_surfaces.py

One patch built from several Plasticity surfaces.

One CAD face is one patch, and that is the input contract -- but a model is cut
into faces by the modelling history, not by what wants a single grid across it.
A boss with two fillet rings around it is five surfaces one sheet of retopology
should cross.

What this pins is that the assembly happens at the *parse*: `build_patches` is
handed a remapped polygon->face-id map, the borders between the surfaces cancel
in `compute_boundary_loops` by the same rule a face's own triangulation edges
do, and everything downstream keeps seeing one patch with one id. So the checks
below are mostly about the boundary that comes back, and about the two ways the
gesture is refused -- surfaces that do not touch, and a composite naming
surfaces the mesh no longer has.

Each of them asserts the single-surface behaviour too. A test that only said
"the combined rectangle has four sides" would pass just as well on a build
where gathering surfaces did nothing at all.
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
# A flat strip of three square surfaces side by side, plus one detached square.
#
# Flat and square on purpose: a rectangle's corner set is not in question, so
# anything the side count says afterwards is about the assembly and not about
# the corner detection.
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

mesh = bpy.data.meshes.new("SurfacesTestMesh")
mesh.from_pydata(verts, [], tris)
mesh.update()
mesh["groups"] = groups
mesh["face_ids"] = face_ids

obj = bpy.data.objects.new("SurfacesTestObj", mesh)
bpy.context.collection.objects.link(obj)


def loop_of(face_id):
    patch = patch_data.analyse(mesh).patches.get(face_id)
    return patch.boundary_loops[0] if patch and patch.boundary_loops else []


def side_count(face_id):
    prepared = patchprep.prepare_patch(mesh, face_id, 45.0, 0.0, 'ANGLE')
    return len(prepared.sides) if prepared else 0


# ---------------------------------------------------------------------------
# 1. One patch per surface, and the border between two of them is real
# ---------------------------------------------------------------------------
analysis = patch_data.analyse(mesh)
check("four patches to start with", sorted(analysis.patches) == [10, 11, 12, 20],
      f"got {sorted(analysis.patches)}")
check("none of them covers more than one surface", analysis.composites == {})
check("each surface is its own square", len(loop_of(10)) == 4 and len(loop_of(11)) == 4,
      f"got {len(loop_of(10))} / {len(loop_of(11))}")
check("and they know they border each other",
      patch_data.patch_neighbour_ids(analysis.patches[10]) == {11},
      f"got {patch_data.patch_neighbour_ids(analysis.patches[10])}")


# ---------------------------------------------------------------------------
# 2. Two surfaces, one patch: the border between them is gone
#
# Six boundary vertices, not eight: the two the surfaces shared are still on
# the outline (a rectangle twice as long has a vertex mid-side), but the
# *segment* between them is not a boundary any more -- it has a polygon on both
# sides, both of them this patch's, so it cancels.
# ---------------------------------------------------------------------------
composite_id = patch_data.next_composite_id({})
patch_data.write_composites(mesh, {composite_id: [10, 11]})

analysis = patch_data.analyse(mesh)
check("the cache noticed without being told",
      sorted(analysis.patches) == sorted([composite_id, 12, 20]),
      f"got {sorted(analysis.patches)}")
check("the analysis says which surfaces it covers",
      analysis.composites == {composite_id: [10, 11]})
check("one boundary loop, six vertices round the pair", len(loop_of(composite_id)) == 6,
      f"got {len(loop_of(composite_id))}")
check("it borders the third surface, not the two it is made of",
      patch_data.patch_neighbour_ids(analysis.patches[composite_id]) == {12},
      f"got {patch_data.patch_neighbour_ids(analysis.patches[composite_id])}")
check("both surfaces' polygons went into it",
      len(analysis.patches[composite_id].poly_indices) == 4,
      f"got {len(analysis.patches[composite_id].poly_indices)}")
check("and it is still a quad, so it still gets a grid",
      side_count(composite_id) == 4, f"got {side_count(composite_id)}")


# ---------------------------------------------------------------------------
# 3. One that cannot be applied is reported, never silently dropped
#
# A re-export renumbers every face id, so this is the state a patch recorded
# before one ends up in -- and one that quietly stops applying looks exactly
# like an addon that forgot it.
# ---------------------------------------------------------------------------
patch_data.write_composites(mesh, {composite_id: [10, 11], -2000: [12, 999]})
analysis = patch_data.analyse(mesh)
check("a patch naming a surface the mesh does not have is dropped",
      analysis.dropped_composites == [-2000], f"got {analysis.dropped_composites}")
check("and the one beside it still applies", composite_id in analysis.composites)
check("the named surface is a patch of its own again", 12 in analysis.patches)


# ---------------------------------------------------------------------------
# 4. Splitting puts it back exactly
# ---------------------------------------------------------------------------
patch_data.write_composites(mesh, {})
analysis = patch_data.analyse(mesh)
check("split back into the surfaces it was built from",
      sorted(analysis.patches) == [10, 11, 12, 20], f"got {sorted(analysis.patches)}")
check("and the border between them is a boundary again", len(loop_of(10)) == 4)
check("the property is gone rather than left empty",
      mesh.get(patch_data.COMPOSITE_PROP) is None)


# ---------------------------------------------------------------------------
# 5. Picking surfaces: contiguity, and the patch that grows as you pick
#
# A patch whose surfaces only meet at a point has a pinched boundary; one whose
# surfaces do not meet at all comes back with two outer loops, which the loop
# count reads as a band and the Ring generator then stretches across the gap.
#
# From the second pick on the composite is **live on the mesh**, which is what
# lets the preview show the patch rather than one of its surfaces. That is also
# what makes the two halves below worth asserting together: the selection stays
# in the mesh's own surface ids while `analyse` no longer has them, so anything
# reading the merged analysis to answer a question about the *selection* is
# asking the wrong table.
#
# There is no ceiling on how many: the check below takes all three of the
# strip, which is every surface it has.
# ---------------------------------------------------------------------------
operators = pr.operators
state = bpy.context.scene.plasticity_retop
state.session_object_name = obj.name
operators.discard_pending_composite(bpy.context)

ok, message = operators.toggle_patch_surface(bpy.context, obj, 10)
check("the first surface is always takeable", ok, message)
check("and it is in the selection", operators.surface_selection(state) == [10])
check("one surface builds nothing -- it is already a patch",
      state.pending_composite_id == -1)

ok, message = operators.toggle_patch_surface(bpy.context, obj, 20)
check("a detached surface is refused", not ok, message)
ok, message = operators.toggle_patch_surface(bpy.context, obj, 12)
check("so is one that only touches through a surface not picked", not ok, message)

ok, _message = operators.toggle_patch_surface(bpy.context, obj, 11)
check("a neighbour is taken", ok)
pending = state.pending_composite_id
check("and the two of them are a patch on the mesh already", pending != -1, pending)
check("which is what the preview can be built from",
      pending in patch_data.analyse(mesh).patches)
check("the surfaces themselves are gone from that analysis",
      10 not in patch_data.analyse(mesh).patches)
check("but not from the one describing the model",
      10 in patch_data.analyse_surfaces(mesh).patches)

ok, _message = operators.toggle_patch_surface(bpy.context, obj, 12)
check("and now the third one is contiguous too -- three is not a special case", ok)
check("all three picked", operators.surface_selection(state) == [10, 11, 12])
check("the patch was rebuilt over all three",
      len(patch_data.analyse(mesh).patches[state.pending_composite_id].poly_indices) == 6,
      patch_data.read_composites(mesh))

ok, _message = operators.toggle_patch_surface(bpy.context, obj, 12)
check("clicking a picked surface drops it again",
      ok and operators.surface_selection(state) == [10, 11])
check("and the patch shrinks back with it",
      len(patch_data.analyse(mesh).patches[state.pending_composite_id].poly_indices) == 4)

# Every way out that is not "open it" has to put the mesh back: a composite
# left behind comes back as a patch nobody built.
operators.discard_pending_composite(bpy.context)
check("abandoning the pick leaves nothing on the mesh",
      patch_data.read_composites(mesh) == {} and state.pending_composite_id == -1,
      patch_data.read_composites(mesh))
check("and the surfaces are patches again",
      sorted(patch_data.analyse(mesh).patches) == [10, 11, 12, 20])


# ---------------------------------------------------------------------------
# 5b. The CAD structure still describes the model
#
# The B-rep edges between the picked surfaces do not stop existing because a
# patch was laid across them -- and an overlay that says they did is reporting
# the addon's decision as if it were the model's. This is the half that reads
# `analyse_surfaces`.
# ---------------------------------------------------------------------------
edges_before = len(pr.cad_display.edge_segments(mesh))
verts_before = len(pr.cad_display.brep_vertices(mesh))
check("the model has edges to draw", edges_before > 0, edges_before)

operators.toggle_patch_surface(bpy.context, obj, 10)
operators.toggle_patch_surface(bpy.context, obj, 11)
check("a patch over two surfaces keeps every CAD edge",
      len(pr.cad_display.edge_segments(mesh)) == edges_before,
      f"{len(pr.cad_display.edge_segments(mesh))} vs {edges_before}")
check("and every B-rep vertex",
      len(pr.cad_display.brep_vertices(mesh)) == verts_before,
      f"{len(pr.cad_display.brep_vertices(mesh))} vs {verts_before}")
# Asked about one patch it describes that patch, which is the opposite answer
# and the right one: the border between the two surfaces is not its boundary.
check("while the patch's own outline is the outline of the pair",
      len(pr.cad_display.edge_segments(mesh, state.pending_composite_id))
      < edges_before)


# ---------------------------------------------------------------------------
# 5c. The overlay draws the patch being picked
#
# The draw handlers are the one code Blender alone invokes, so nothing else
# notices when they break -- and a highlight is only ever seen by running it.
# The assertion is that the callback gets all the way down to the GPU call,
# which headless Blender has none of: a name that is not there raises long
# before that.
# ---------------------------------------------------------------------------
overlay = pr.overlay
triangles = pr.cad_display.patch_triangles(mesh, state.pending_composite_id)
check("the fill is every polygon of the patch, fanned", len(triangles) == 12,
      f"got {len(triangles)}")
check("and it is cached on the mesh like everything a draw handler reads",
      pr.cad_display.patch_triangles(mesh, state.pending_composite_id) is triangles)

state.session_active = True
state.session_phase = 'PATCH'

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
      any("2 surfaces" in action for action in hints.values()), hints)
operators.discard_pending_composite(bpy.context)
hints = dict(overlay.keybinds_for(state))
check("and names the gesture before anything is picked -- it is behind a "
      "modifier, which nobody finds on their own",
      any(action == "Add surface to patch" for action in hints.values()), hints)
state.session_active = False
state.session_object_name = ""


# ---------------------------------------------------------------------------
# 6. Writing it, and flattening
#
# Adding a surface to a patch that already covers several rebuilds it flat
# rather than nesting, so a surface list is always raw Plasticity face ids.
# ---------------------------------------------------------------------------
new_id, message = operators.build_composite(bpy.context, obj, [10, 11])
check("it is written", new_id is not None and new_id <= patch_data.COMPOSITE_ID_BASE,
      f"got {new_id}")
check("with both surfaces", patch_data.read_composites(mesh)[new_id] == [10, 11])

second_id, _message = operators.build_composite(bpy.context, obj, [new_id, 12])
composites = patch_data.read_composites(mesh)
check("adding a third surface flattens rather than nesting",
      composites.get(second_id) == [10, 11, 12], f"got {composites}")
check("and the one it stood on is gone", new_id not in composites)
check("the new id is not one that was just freed", second_id != new_id)

done, message = operators.split_composite(bpy.context, obj, second_id)
check("and it splits back", done and patch_data.read_composites(mesh) == {}, message)

operators.discard_pending_composite(bpy.context)


# ---------------------------------------------------------------------------
# 7. A multi-surface patch commits, and reopens as the same patch
#
# The riskiest part of assembling at the parse: everything downstream keeps
# treating an id as an id, and this one is *negative*. It lands in
# `PATCH_ID_ATTR` on every committed face, which is the int layer re-editing
# and adoption read back -- so what this checks is that a synthetic id survives
# that round trip like a Plasticity one does.
# ---------------------------------------------------------------------------
bpy.context.view_layer.objects.active = obj
pr.operators.enter_session_object(bpy.context, obj)
state.ngon_mode = False

composite_id, _message = operators.build_composite(bpy.context, obj, [10, 11])
generator, num_sides, _propagated = operators.set_active_patch(
    bpy.context, obj, composite_id)
check("it generates", generator is not None, f"got {generator}")
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
check("every committed face names the one patch", stamped == {composite_id},
      f"got {stamped}")
check("which the result mesh agrees is committed",
      pr.mesh_build.is_patch_committed(obj, composite_id))

operators.set_active_patch(bpy.context, obj, composite_id)
check("re-picking it reopens the same patch", state.editing_committed)
check("and takes its faces back out", state.reedit_removed_faces > 0,
      state.reedit_removed_faces)

# Splitting is refused while it is committed: the faces carry an id that would
# name a patch nothing could reach, so nothing would ever delete them again.
done, message = operators.split_composite(bpy.context, obj, composite_id)
check("a committed patch refuses to split", not done, message)


# ---------------------------------------------------------------------------
# 8. Deleting it takes the patch apart as well as emptying it
#
# Nothing carries the id after the faces go, and leaving the composite behind
# leaves an area that is still one patch with nothing in it -- which cannot be
# taken apart either, since Split Into Surfaces polls on that patch being
# *open* and deleting it is what closed it. That is a patch you can neither
# rebuild as its surfaces nor get rid of.
# ---------------------------------------------------------------------------
bpy.ops.retop.delete_patch()
check("the patch's faces are gone",
      len(result.data.polygons) == 0, len(result.data.polygons))
check("and so is the patch itself", patch_data.read_composites(mesh) == {},
      patch_data.read_composites(mesh))
check("its surfaces are patches of their own again",
      sorted(patch_data.analyse(mesh).patches) == [10, 11, 12, 20],
      sorted(patch_data.analyse(mesh).patches))
check("which is what makes them selectable one by one",
      operators.set_active_patch(bpy.context, obj, 10)[0] is not None)
bpy.ops.retop.clear_preview()
state.active_face_id = -1


# ---------------------------------------------------------------------------
# 9. Esc during a pick backs out of it without taking the session with it
#
# `retop.back` used to clear the preview unguarded, and an operator whose poll
# fails *raises* -- out of the modal, which stops the session on the one key
# that exists to back out of things. The preview is empty in exactly the cases
# a pick leaves it empty: one surface picked, or a set nothing can be built
# over.
# ---------------------------------------------------------------------------
state.session_phase = 'PATCH'
state.session_active = True
operators.toggle_patch_surface(bpy.context, obj, 10)
check("a pick with one surface leaves no preview",
      not pr.mesh_build.has_preview())
check("and Esc still backs out of it", bpy.ops.retop.back() == {'FINISHED'})
check("with the pick dropped", operators.surface_selection(state) == [])

operators.toggle_patch_surface(bpy.context, obj, 10)
operators.toggle_patch_surface(bpy.context, obj, 11)
check("two surfaces do build a preview", pr.mesh_build.has_preview())
check("and Esc backs out of that too", bpy.ops.retop.back() == {'FINISHED'})
check("taking the patch off the mesh with it",
      patch_data.read_composites(mesh) == {}
      and state.pending_composite_id == -1)
check("and leaving the session running", state.session_active)

pr.operators.end_session(bpy.context)


print()
if FAILURES:
    print(f"=== {len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(1)
print("=== ALL CHECKS PASSED")
