"""Run inside Blender: blender --background --python tests/test_reconcile.py

The retopology keeps recognising its patches after Plasticity renames faces.

A face id is only a name, and Plasticity renames faces: on a real part a whole
block of them shifted by a constant, with no vertex moving, and the bridge's
plain Refresh delivered the new names. Every committed face then carried an id
the source no longer declared, the patch read as never retopologized, and
picking it built a second grid over the first.

What is simulated here is that Refresh, done the way the bridge does it --
`clear_geometry` on the same datablock and refill -- with the three things a
real one does at once: faces renamed to ids nobody had, two faces *swapping*
names (an old id reissued to another face, which no "is this id known" test can
see), and every vertex re-indexed, which is what a corner id and the span
registry are keyed by.

Each fix is checked against the broken state first -- that the tags really are
wrong before reconciliation -- or the test proves nothing.
"""
import importlib
import os
import sys

_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_ADDON_DIR))

import bmesh
import bpy

pr = importlib.import_module(os.path.basename(_ADDON_DIR))
mesh_build = pr.mesh_build
operators = pr.operators
patch_data = pr.patch_data

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
# A 6x3 grid cut down the middle into two surfaces, A and B, and a triangle C
# standing on its top edge. A and B share a border, which is where a vote read
# at the vertices themselves would be a coin toss.
# ---------------------------------------------------------------------------
W, H = 6, 3
VERTS = [(float(i), float(j), 0.0) for j in range(H + 1) for i in range(W + 1)]
APEX = len(VERTS)
VERTS.append((W / 2, H + 2.0, 0.0))


def gv(i, j):
    return j * (W + 1) + i


def cells(i0, i1):
    tris = []
    for j in range(H):
        for i in range(i0, i1):
            tris.append((gv(i, j), gv(i + 1, j), gv(i + 1, j + 1)))
            tris.append((gv(i, j), gv(i + 1, j + 1), gv(i, j + 1)))
    return tris


PATCH_TRIS = [cells(0, W // 2), cells(W // 2, W),
              [(gv(i + 1, H), gv(i, H), APEX) for i in range(W)]]


def fill(mesh, ids, perm=None):
    """(Re)fill `mesh` the way the bridge's Refresh does: same datablock,
    geometry cleared and rewritten. `perm` re-indexes the vertices."""
    perm = perm or list(range(len(VERTS)))
    verts = [None] * len(VERTS)
    for old, new in enumerate(perm):
        verts[new] = VERTS[old]
    tris, groups = [], []
    for patch in PATCH_TRIS:
        groups.extend([len(tris) * 3, len(patch) * 3])
        tris.extend(tuple(perm[v] for v in tri) for tri in patch)
    mesh.clear_geometry()
    mesh.from_pydata(verts, [], tris)
    mesh.update()
    mesh["groups"] = groups
    mesh["face_ids"] = list(ids)


mesh = bpy.data.meshes.new("ReconcileMesh")
fill(mesh, (101, 102, 202))
obj = bpy.data.objects.new("ReconcileObj", mesh)
bpy.context.collection.objects.link(obj)
bpy.context.view_layer.objects.active = obj

operators.enter_session_object(bpy.context, obj)
state.ngon_mode = False
for face_id in (101, 102, 202):
    generator, _sides, _prop = operators.set_active_patch(bpy.context, obj, face_id)
    check(f"patch {face_id} generates", generator is not None, str(generator))
    # A and B get different records, so a move that overwrote one with the
    # other would show.
    if face_id == 101:
        state.span_u, state.span_v = 3, 2
    elif face_id == 102:
        state.span_u, state.span_v = 2, 3
    bpy.ops.retop.commit_patch()

result = bpy.data.objects[mesh_build.result_object_name_for(obj)]


def per_patch():
    counts = {}
    for tag in mesh_build._patch_ids_of_faces(result.data):
        counts[tag] = counts.get(tag, 0) + 1
    return counts


def corner_positions():
    """Every corner id on the result, as the position of the source vertex it
    names -- which is what has to survive a re-index, not the number."""
    vids = [a.value for a in result.data.attributes[mesh_build.SOURCE_VID_ATTR].data]
    return sorted(tuple(round(c, 5) for c in result.data.vertices[i].co)
                  for i, sid in enumerate(vids) if sid != mesh_build.NO_SOURCE)


def corners_on_their_vertex():
    vids = [a.value for a in result.data.attributes[mesh_build.SOURCE_VID_ATTR].data]
    bad = 0
    for i, sid in enumerate(vids):
        if sid == mesh_build.NO_SOURCE:
            continue
        if not 0 <= sid < len(mesh.vertices) or \
                (mesh.vertices[sid].co - result.data.vertices[i].co).length > 1e-5:
            bad += 1
    return bad


def registry_by_position():
    out = set()
    for key, span in mesh_build.get_span_registry(result).items():
        a, b = (int(p) for p in key.split("_"))
        pa = tuple(round(c, 5) for c in mesh.vertices[a].co)
        pb = tuple(round(c, 5) for c in mesh.vertices[b].co)
        out.add((min(pa, pb), max(pa, pb), span))
    return out


before = per_patch()
table_before = mesh_build.get_patch_settings_table(result)
corners_before = corner_positions()
registry_before = registry_by_position()
check("three patches committed", set(before) == {101, 102, 202}, str(before))
check("with a corner id on the result", len(corners_before) > 0)
check("and a span registry to carry", len(registry_before) > 0)
check("reconciling an unchanged source changes nothing",
      mesh_build.reconcile_patch_tracking(bpy.context, obj) == (0, 0, 0))


# ---------------------------------------------------------------------------
# The Refresh: A and B swap names, C gets one nobody had, vertices re-indexed.
# ---------------------------------------------------------------------------
perm = list(reversed(range(len(VERTS))))
fill(mesh, (102, 101, 9202), perm)

check("the broken state is real: C reads as never committed",
      not mesh_build.is_patch_committed(obj, 9202))
check("and A's faces claim B's new name",
      per_patch()[101] == before[101] and mesh_build.is_patch_committed(obj, 102))
check("and the corner ids name the wrong vertices", corners_on_their_vertex() > 0)

faces, corners, _composites = mesh_build.reconcile_patch_tracking(bpy.context, obj)
after = per_patch()
check("every face was re-read", faces == sum(before.values()), f"{faces}")
check("A's faces follow A to its new name", after.get(102) == before[101], str(after))
check("B's faces follow B", after.get(101) == before[102], str(after))
check("C's faces follow C to a name nobody had",
      after.get(9202) == before[202], str(after))

table_after = mesh_build.get_patch_settings_table(result)
check("A's record moved with it", table_after.get("102") == table_before["101"],
      f"{table_after.get('102')} vs {table_before['101']}")
check("B's record too, not overwritten by A's",
      table_after.get("101") == table_before["102"]
      and table_before["101"] != table_before["102"])
check("and C's", table_after.get("9202") == table_before["202"]
      and "202" not in table_after, str(sorted(table_after)))

check("corners were re-attached", corners > 0, str(corners))
check("every corner names the source vertex it sits on", corners_on_their_vertex() == 0,
      str(corners_on_their_vertex()))
check("and none was lost", corner_positions() == corners_before)
check("the span registry describes the same boundaries",
      registry_by_position() == registry_before,
      f"{len(registry_by_position())} vs {len(registry_before)}")
check("a second reconciliation is a no-op",
      mesh_build.reconcile_patch_tracking(bpy.context, obj) == (0, 0, 0))

# Picking A now is a re-edit of A, not a second grid over it.
operators.set_active_patch(bpy.context, obj, 102)
check("picking A reopens it as a re-edit", state.editing_committed)
check("and takes out exactly A's faces", state.reedit_removed_faces == before[101],
      f"{state.reedit_removed_faces} vs {before[101]}")
operators.restore_reedit_removal(bpy.context)
check("discarding puts them back", per_patch() == after, str(per_patch()))


# ---------------------------------------------------------------------------
# A face made by hand in Edit Mode gets 0 from Blender, not NO_PATCH -- and 0
# names nothing on this source, so it is untracked and adopted like one.
# ---------------------------------------------------------------------------
attr = result.data.attributes[mesh_build.PATCH_ID_ATTR]
tags = [a.value for a in attr.data]
hand_made = [i for i, t in enumerate(tags) if t == 102][:2]
for i in hand_made:
    tags[i] = 0
attr.data.foreach_set("value", tags)
check("faces tagged 0 read as nothing committed",
      per_patch().get(0) == len(hand_made))
mesh_build.reconcile_patch_tracking(bpy.context, obj)
check("and are handed to the patch they sit on", per_patch() == after, str(per_patch()))


# ---------------------------------------------------------------------------
# A face the surface cannot decide goes to the patch most of its edges touch.
# ---------------------------------------------------------------------------
strip = bpy.data.meshes.new("NeighbourStrip")
strip.from_pydata([(x, y, 0.0) for y in (0, 1) for x in range(5)], [],
                  [(0, 1, 6, 5), (1, 2, 7, 6), (2, 3, 8, 7), (3, 4, 9, 8)])
row = [5, mesh_build.NO_PATCH, mesh_build.NO_PATCH, 5]
check("two untracked faces between two of one patch both join it",
      mesh_build._adopt_from_neighbours(strip, row, [1, 2]) == 2 and row == [5, 5, 5, 5],
      str(row))
row = [5, mesh_build.NO_PATCH, 7, 7]
check("a face with one edge on each of two patches is left alone",
      mesh_build._adopt_from_neighbours(strip, row, [1]) == 0
      and row[1] == mesh_build.NO_PATCH, str(row))


# ---------------------------------------------------------------------------
# A multi-surface patch: renamed surfaces are found again by position, first
# from the anchors stored with it, then -- anchors gone -- from its own faces.
# ---------------------------------------------------------------------------
operators.end_session(bpy.context)
comp_mesh = bpy.data.meshes.new("ReconcileCompositeMesh")
fill(comp_mesh, (301, 302, 303))
comp_obj = bpy.data.objects.new("ReconcileCompositeObj", comp_mesh)
bpy.context.collection.objects.link(comp_obj)
bpy.context.view_layer.objects.active = comp_obj
operators.enter_session_object(bpy.context, comp_obj)
state.ngon_mode = False

composite_id, _message = operators.build_composite(bpy.context, comp_obj, [301, 302])
check("the composite is anchored as it is built",
      len(patch_data.read_composite_anchors(comp_mesh).get(composite_id, [])) == 2)
operators.set_active_patch(bpy.context, comp_obj, composite_id)
state.span_u, state.span_v = 4, 2
bpy.ops.retop.commit_patch()
check("and committed", mesh_build.is_patch_committed(comp_obj, composite_id))

for ids, how in (((401, 402, 403), "its anchors"), ((502, 501, 503), "its own faces")):
    if how == "its own faces":
        del comp_mesh[patch_data.COMPOSITE_ANCHORS_PROP]
    fill(comp_mesh, ids, perm)
    check(f"[{how}] renamed, it no longer applies",
          patch_data.analyse(comp_mesh).dropped_composites == [composite_id])
    _faces, _corners, restored = mesh_build.reconcile_patch_tracking(bpy.context, comp_obj)
    check(f"[{how}] it is found again", restored == 1, str(restored))
    check(f"[{how}] over the same two surfaces, under their new names",
          sorted(patch_data.read_composites(comp_mesh).get(composite_id, []))
          == sorted(ids[:2]), str(patch_data.read_composites(comp_mesh)))
    check(f"[{how}] and its faces are still its own",
          mesh_build.is_patch_committed(comp_obj, composite_id)
          and patch_data.analyse(comp_mesh).dropped_composites == [])

operators.end_session(bpy.context)


print()
if FAILURES:
    print(f"=== {len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(1)
print("=== ALL CHECKS PASSED")
