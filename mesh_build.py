"""Build/update the preview object shown while tweaking a patch, and bake a
confirmed preview into the persistent per-source-object retopology result
mesh.

Stitching adjacent patches: only a patch's *corner* vertices are guaranteed
to be exact, un-resampled source-mesh vertices, so they are the only points
safe to weld across neighboring patches purely by identity (the same source
vertex index always maps to the same welded result vertex). Interior
boundary points are span-dependent resample points that only coincide
between two patches when their spans happen to match exactly along the
shared edge; blindly proximity-welding them (e.g. bmesh.ops.remove_doubles)
can silently merge unrelated points and drop faces, so we deliberately do
NOT do that here. To actually get matching spans (and therefore a fully
welded, seam-free result), see the span propagation registry below: every
commit records, per pair of corner vertex ids, the span used along that
boundary; generating a new patch looks up its own corner pairs in that
registry and uses any match as its default span for that direction.

Re-editing a committed patch: each baked face records which Plasticity face it
came from (PATCH_ID_ATTR) and each patch records the spans it was built with
(PATCH_SPANS_PROP), so a patch can be picked again, come back with its own
spans, and be committed a second time -- replacing its old faces instead of
doubling up on them.

The visual "push off the surface" used to see the preview clearly is done
with a non-destructive Displace modifier on the preview object, not baked
into its mesh data -- so Commit (which reads the preview's base mesh, not
its modifier-evaluated geometry) always bakes the true, un-offset position.

Preview and result are lifted by the *same* measure (result_lift), the
preview by a little more (PREVIEW_LIFT_RATIO). One control, not two. They are always seen
together -- side by side across a shared boundary, and stacked while a
committed patch is hovered before the click that removes its faces -- so
the preview sitting on the surface while the result floated above it read
as the committed blue patch swallowing the orange one being built.
"""
import bpy
import bmesh
import mathutils
import json
import math

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from . import cad_display
from . import constants
from . import patch_data
from . import state as state_mod

if TYPE_CHECKING:
    from . import generators

# What `committed_boundary_map` hands back: a committed patch's boundary
# vertices, in the *source* object's local space, keyed by the face id owning
# them (NO_PATCH for retopology that predates patch tracking).
CommittedMap = dict[int, list[mathutils.Vector]]

PREVIEW_OBJ_NAME = "RetopPreview"
RESULT_NAME_SUFFIX = "_Retop"
OFFSET_MODIFIER_NAME = "RetopPreviewOffset"
RESULT_OFFSET_MODIFIER_NAME = "RetopResultOffset"
AUTO_OFFSET_RATIO = 0.001  # of the source object's bounding-box diagonal
# The preview is lifted off the CAD surface by the *result* offset times this,
# so the patch being built always draws above the committed patches around it
# instead of fighting them (see preview_lift).
PREVIEW_LIFT_RATIO = 1.5
# Custom property naming the source object a preview was built from, so its
# lift can be derived from the same object the result offset uses even when
# no session is running.
PREVIEW_SOURCE_PROP = "retop_preview_source"
PREVIEW_MATERIAL_NAME = "RetopPreviewMaterial"
RESULT_MATERIAL_NAME = "RetopResultMaterial"
RESULT_DIM_MATERIAL_NAME = "RetopResultMaterialDim"
COLLECTION_NAME = "Retop"
# The Plasticity bridge drops everything it imports under a collection named
# "Inbox"; whatever sits above it is the bridge's own scaffolding (the file /
# connection name), so mirroring starts *below* Inbox.
INBOX_COLLECTION_NAME = "Inbox"
SHARP_EDGE_ATTR = "sharp_edge"
SOURCE_VID_ATTR = "retop_source_vid"
BOUNDARY_ATTR = "retop_is_boundary"
# Plasticity face ids arrive from the bridge as int32 (client.py decodes them
# with dtype=np.int32), so a Blender INT attribute holds them exactly.
PATCH_ID_ATTR = "retop_patch_face_id"
SPAN_REGISTRY_PROP = "retop_side_spans"
PATCH_SPANS_PROP = "retop_patch_spans"
ADOPTION_PROP = "retop_patch_adoption"
SNAPSHOT_NAME_SUFFIX = "_ReeditBackup"
NO_SOURCE = -1
NO_PATCH = -1


def result_object_name_for(source_obj: bpy.types.Object) -> str:
    return f"{source_obj.name}{RESULT_NAME_SUFFIX}"


def _span_key(corner_a: int, corner_b: int) -> str:
    lo, hi = (corner_a, corner_b) if corner_a <= corner_b else (corner_b, corner_a)
    return f"{lo}_{hi}"


def get_span_registry(result_obj: bpy.types.Object) -> dict[str, int]:
    """{ "cornerA_cornerB": span_int } for every committed patch side, keyed
    by the (order-independent) pair of corner source-vertex ids at its ends.
    Persisted as a JSON string custom property so it survives save/reload
    without needing a separate in-memory cache.
    """
    raw = result_obj.get(SPAN_REGISTRY_PROP)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def save_span_registry(
    result_obj: bpy.types.Object, registry: dict[str, int]
) -> None:
    result_obj[SPAN_REGISTRY_PROP] = json.dumps(registry)


def lookup_span(
    registry: dict[str, int], corner_a: int, corner_b: int
) -> int | None:
    return registry.get(_span_key(corner_a, corner_b))


def lookup_propagated_span(
    source_obj: bpy.types.Object, corner_a: int, corner_b: int
) -> int | None:
    """Convenience for operators.py: look up a side's span directly from the
    source object, without the caller needing to know about the result
    object / registry plumbing. Returns None if there's no committed result
    yet, or no neighboring patch has used that edge.
    """
    result_obj = bpy.data.objects.get(result_object_name_for(source_obj))
    if result_obj is None:
        return None
    return lookup_span(get_span_registry(result_obj), corner_a, corner_b)


def register_patch_spans(
    source_obj: bpy.types.Object,
    corner_source_ids: list[int],
    spans_per_side: list[int],
) -> None:
    """Record the span used along each side of a just-committed patch, so
    future neighboring patches can propagate it. No-op if the result object
    doesn't exist yet (shouldn't happen right after a commit, but be safe).
    """
    result_obj = bpy.data.objects.get(result_object_name_for(source_obj))
    if result_obj is None:
        return
    registry = get_span_registry(result_obj)
    n = len(corner_source_ids)
    for i in range(n):
        a = corner_source_ids[i]
        b = corner_source_ids[(i + 1) % n]
        registry[_span_key(a, b)] = spans_per_side[i]
    save_span_registry(result_obj, registry)


# --- committed patches: which ones are in the result mesh, and with what spans ---
#
# Every face baked into the result mesh carries the Plasticity face id of the
# patch it came from (PATCH_ID_ATTR), and the settings used to build that patch
# are stored alongside it (PATCH_SPANS_PROP). Together they make a committed
# patch re-selectable: picking it again restores exactly the spans it was built
# with, and committing again *replaces* its faces instead of piling a second
# copy on top of them (see commit_preview_to_result's `face_id`).


def get_patch_settings_table(
    result_obj: bpy.types.Object,
) -> dict[str, dict[str, Any]]:
    """{ "<face_id>": {"span_u": .., "span_v": .., "span": .., "generator": ..} }
    for every committed patch. JSON custom property, like the span registry.
    """
    raw = result_obj.get(PATCH_SPANS_PROP)
    if not raw:
        return {}
    try:
        table = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return table if isinstance(table, dict) else {}


def save_patch_settings_table(
    result_obj: bpy.types.Object, table: dict[str, dict[str, Any]]
) -> None:
    result_obj[PATCH_SPANS_PROP] = json.dumps(table)


def register_patch_settings(
    source_obj: bpy.types.Object,
    face_id: int,
    span_u: int,
    span_v: int,
    span: int,
    generator_name: str,
    side_groups: str = "",
) -> None:
    """Record what a just-committed patch was built with, so re-selecting it
    later comes back with those exact spans rather than recomputed defaults.

    `side_groups` is the grouping its sides were gathered into, as the JSON
    `state.side_groups` holds. It belongs here with the spans and the generator
    for exactly the same reason: a patch committed as a Quad because two of its
    five sides were merged has to reopen as that Quad. Recomputing it from the
    live scene cannot work -- the grouping is per patch and `set_active_patch`
    clears it on the way in, like every other per-patch choice.
    """
    result_obj = bpy.data.objects.get(result_object_name_for(source_obj))
    if result_obj is None:
        return
    table = get_patch_settings_table(result_obj)
    table[str(face_id)] = {
        "span_u": span_u,
        "span_v": span_v,
        "span": span,
        "generator": generator_name,
        "side_groups": side_groups,
    }
    save_patch_settings_table(result_obj, table)


def copy_source_status(
    state: state_mod.RetopPatchState,
    source_obj: bpy.types.Object | None,
    face_id: int,
) -> tuple[str, str]:
    """(title, detail) for a committed patch offered as a density to copy.

    Here rather than in `operators` because the **overlay** asks it, on every
    redraw while a patch is open, and the overlay may not reach into operators
    -- that import only goes one way (see the module table in CLAUDE.md). It
    needs nothing from a session beyond the state it is handed.

    One wording for the viewport tooltip and for what the click reports
    afterwards: a promise made before a click and a different sentence after it
    is how the side picker's colours went wrong once already.
    """
    stored = lookup_patch_settings(source_obj, face_id) if source_obj else None
    if not stored:
        return "", ""

    source_generator = stored.get("generator") or "?"
    if source_generator != state.generator_name:
        return (f"Patch {face_id}: {source_generator}",
                f"a {source_generator}'s spans mean something else on "
                f"a {state.generator_name or 'this patch'}")

    two_spans = source_generator in constants.TWO_SPAN_GENERATORS
    already = state.copy_source_face_id == face_id
    swapped = already and state.copy_source_swapped

    spans = []
    if two_spans:
        # Shown the way the *next* click would apply them, so the tooltip is a
        # preview of the click rather than a description of the record.
        u, v = stored.get("span_u"), stored.get("span_v")
        if already and not swapped:
            u, v = v, u
        spans = [f"U={u}", f"V={v}"]
    elif stored.get("span"):
        spans = [f"span={stored.get('span')}"]

    if already and two_spans:
        title = (f"Copy from patch {face_id} again"
                 if swapped else f"Swap U/V from patch {face_id}")
        detail = ", ".join(spans)
        return title, detail
    return (f"Copy density from patch {face_id}",
            ", ".join(spans) if spans else source_generator)


def lookup_patch_settings(
    source_obj: bpy.types.Object, face_id: int
) -> dict[str, Any] | None:
    """The settings a patch was committed with, or None if it was never
    committed (or predates this bookkeeping).
    """
    result_obj = bpy.data.objects.get(result_object_name_for(source_obj))
    if result_obj is None:
        return None
    return get_patch_settings_table(result_obj).get(str(face_id))


def forget_patch_settings(source_obj: bpy.types.Object, face_id: int) -> bool:
    """Drop the record of what a patch was committed with.

    Called when its geometry is deleted for good. The *span registry* is
    deliberately left alone: its entries are keyed by corner pair, i.e. they
    describe a shared boundary, and the patch on the other side is still
    committed along it -- dropping them would break its propagation to
    describe a patch that no longer exists.
    """
    result_obj = bpy.data.objects.get(result_object_name_for(source_obj))
    if result_obj is None:
        return False
    table = get_patch_settings_table(result_obj)
    if str(face_id) not in table:
        return False
    del table[str(face_id)]
    save_patch_settings_table(result_obj, table)
    return True


def _patch_ids_of_faces(mesh: bpy.types.Mesh) -> list[int]:
    attr = mesh.attributes.get(PATCH_ID_ATTR)
    if attr is None or len(mesh.polygons) == 0:
        return []
    values = [NO_PATCH] * len(mesh.polygons)
    attr.data.foreach_get("value", values)
    return values


def committed_face_ids(source_obj: bpy.types.Object) -> set[int]:
    """Set of Plasticity face ids currently present in `source_obj`'s result
    mesh. Read from the mesh itself (not from the settings table), so a patch
    the user deleted by hand in Edit Mode stops counting as committed.
    """
    result_obj = bpy.data.objects.get(result_object_name_for(source_obj))
    if result_obj is None:
        return set()
    return {fid for fid in _patch_ids_of_faces(result_obj.data) if fid != NO_PATCH}


def is_patch_committed(source_obj: bpy.types.Object, face_id: int) -> bool:
    return face_id in committed_face_ids(source_obj)


def _source_patch_lookup(
    source_obj: bpy.types.Object, result_obj: bpy.types.Object
) -> Callable[[mathutils.Vector], int | None] | None:
    """Return f(co) -> Plasticity face id for a point of `result_obj`'s mesh,
    by nearest source polygon, or None if the source has no usable geometry.

    This is how retopology that carries no patch id is matched back to the CAD
    face it belongs to: the retopology sits on the surface, so the closest
    source polygon to one of its points names the patch.
    """
    from . import geometry

    src_mesh = source_obj.data
    if len(src_mesh.polygons) == 0:
        return None

    face_id_of_poly = patch_data.analyse(src_mesh).face_id_of_poly
    bvh, tri_poly = geometry.build_bvh_with_polygon_map(src_mesh)
    # The BVH is in the source object's local space; result geometry is stored
    # in world space (i.e. under an identity object matrix, unless the user
    # moved the result object since).
    to_source_local = source_obj.matrix_world.inverted() @ result_obj.matrix_world

    def face_id_at(co: mathutils.Vector) -> int | None:
        hit = bvh.find_nearest(to_source_local @ co)
        if hit is None or hit[2] is None:
            return None
        return face_id_of_poly[tri_poly[hit[2]]]

    return face_id_at


def adopt_untracked_faces(source_obj: bpy.types.Object) -> int:
    """Tag result faces that carry no patch id with the Plasticity face they
    sit on, and return how many were tagged.

    Needed for result meshes committed before patch tracking existed (or by
    hand): without a face id nothing links them to a patch, so re-picking that
    patch can't know it already has geometry to replace.

    Votes are collected from the face centre (weighted, it's the point most
    safely *inside* the patch) plus its vertices, and a face is only *tagged* on
    a strict majority -- a tag is permanent, so it had better be right.
    Removing a patch for a re-edit uses the looser rule in
    remove_patch_from_result: that one is visible on screen and undoable.
    """
    result_obj = bpy.data.objects.get(result_object_name_for(source_obj))
    if result_obj is None:
        return 0
    mesh = result_obj.data
    if len(mesh.polygons) == 0:
        return 0

    existing = _patch_ids_of_faces(mesh)
    if existing and NO_PATCH not in existing:
        return 0  # everything already tagged: nothing to do
    if not existing:
        existing = [NO_PATCH] * len(mesh.polygons)

    face_id_at = _source_patch_lookup(source_obj, result_obj)
    if face_id_at is None:
        return 0

    CENTRE_WEIGHT = 2
    adopted = 0
    for poly in mesh.polygons:
        if existing[poly.index] != NO_PATCH:
            continue

        votes = {}
        centre_id = face_id_at(poly.center)
        if centre_id is not None:
            votes[centre_id] = CENTRE_WEIGHT
        for vi in poly.vertices:
            fid = face_id_at(mesh.vertices[vi].co)
            if fid is not None:
                votes[fid] = votes.get(fid, 0) + 1

        if not votes:
            continue
        best_id, best_votes = max(votes.items(), key=lambda kv: kv[1])
        if best_votes * 2 > sum(votes.values()):  # strict majority only
            existing[poly.index] = best_id
            adopted += 1

    if adopted == 0:
        return 0

    attr = mesh.attributes.get(PATCH_ID_ATTR)
    if attr is None:
        attr = mesh.attributes.new(PATCH_ID_ATTR, 'INT', 'FACE')
    attr.data.foreach_set("value", existing)
    mesh.update()
    result_obj[ADOPTION_PROP] = 1
    print(f"[Plasticity Retop] Adopted {adopted} pre-existing face(s) of "
          f"'{result_obj.name}' into patch tracking")
    return adopted


# --- putting the bookkeeping back after a hand edit ---
#
# `tweak.py` hands the result mesh to Blender's Edit Mode so a failed match can
# be fixed with the knife, a vertex drag and auto-merge. Blender knows nothing
# about this addon's attributes, and the two it gets wrong are the two that are
# read back later:
#
# - a face the knife created carries NO_PATCH, so the patch it belongs to reads
#   as partly "never retopped" and a re-edit builds a second grid over it;
# - a vertex the knife created inherits its neighbours' `retop_source_vid`,
#   i.e. it claims to *be* a CAD corner it is nowhere near. Corner welding is
#   by identity, so the next commit touching that corner would either drag the
#   new vertex onto it or reuse it in the corner's place.
#
# Faces are handled by `adopt_untracked_faces`, which already classifies an
# untagged face onto the patch it sits on and refuses to guess without a strict
# majority. Vertex ids are handled below, and the rule is deliberately not "is
# this vertex where the corner is" alone: a *deliberate* nudge of a corner is
# exactly what this mode exists for, and stripping its identity for having
# moved a hair would undo the fix on the next commit.

# A vertex farther than this share of the model's bounding-box diagonal from
# the CAD corner it names cannot be that corner, however it came by the id.
# Generous on purpose: a hand-nudged corner stays itself, a knife-created
# vertex a cell away does not.
STRAY_SOURCE_ID_RATIO = 0.01


def _bbox_diagonal(obj: bpy.types.Object) -> float:
    corners = [mathutils.Vector(c) for c in obj.bound_box]
    if not corners:
        return 0.0
    lo = mathutils.Vector((min(c[i] for c in corners) for i in range(3)))
    hi = mathutils.Vector((max(c[i] for c in corners) for i in range(3)))
    return (hi - lo).length


def clear_stray_source_ids(
    context: bpy.types.Context,
    source_obj: bpy.types.Object,
    result_obj: bpy.types.Object,
) -> int:
    """Drop `retop_source_vid` from result vertices that cannot own it, and
    return how many were cleared.

    Three ways a vertex loses the claim, in order of how sure we are:

    1. the id is out of range for the source mesh -- an interpolated int
       between two real ids, which is not an index at all;
    2. it is far from the source vertex it names (see STRAY_SOURCE_ID_RATIO);
    3. another vertex names the same source vertex and sits closer. An id is
       one CAD corner and one result vertex; the nearer one keeps it.

    Clearing is always the safe direction: a vertex with no id welds by
    *proximity* like every other boundary point, which is what a hand-placed
    vertex should do anyway.
    """
    mesh = result_obj.data
    attr = mesh.attributes.get(SOURCE_VID_ATTR)
    if attr is None or len(mesh.vertices) == 0:
        return 0

    vids = [NO_SOURCE] * len(mesh.vertices)
    attr.data.foreach_get("value", vids)

    src_mesh = source_obj.data
    source_count = len(src_mesh.vertices)
    src_matrix = source_obj.matrix_world
    result_matrix = result_obj.matrix_world

    state = context.scene.plasticity_retop
    weld = state_mod.to_blender_units(state, state.boundary_weld_distance)
    limit = max(weld, _bbox_diagonal(source_obj) * STRAY_SOURCE_ID_RATIO, 1e-6)

    # (1) and (2), collecting distances for (3) as we go.
    cleared = 0
    claims: dict[int, tuple[int, float]] = {}  # source id -> (vertex, distance)
    for index, sid in enumerate(vids):
        if sid == NO_SOURCE:
            continue
        if not 0 <= sid < source_count:
            vids[index] = NO_SOURCE
            cleared += 1
            continue
        world = result_matrix @ mesh.vertices[index].co
        distance = (src_matrix @ src_mesh.vertices[sid].co - world).length
        if distance > limit:
            vids[index] = NO_SOURCE
            cleared += 1
            continue
        best = claims.get(sid)
        if best is None or distance < best[1]:
            if best is not None:
                vids[best[0]] = NO_SOURCE
                cleared += 1
            claims[sid] = (index, distance)
        else:
            vids[index] = NO_SOURCE
            cleared += 1

    if cleared:
        attr.data.foreach_set("value", vids)
        mesh.update()
    return cleared


def repair_manual_edits(
    context: bpy.types.Context, source_obj: bpy.types.Object
) -> tuple[int, int]:
    """Reconcile `<Source>_Retop` with the addon after it was hand-edited in
    Blender's Edit Mode. Returns (faces adopted, source ids cleared).

    Called on the way out of the tweak round trip -- once per trip, whichever
    way it ended. Everything here writes mesh attributes only, so it is safe
    from the modal; the session pushes one undo step around it.
    """
    result_obj = bpy.data.objects.get(result_object_name_for(source_obj))
    if result_obj is None:
        return 0, 0

    cleared = clear_stray_source_ids(context, source_obj, result_obj)
    # Faces the knife or a manual add left untagged. adopt_untracked_faces
    # already returns early when every face carries an id, so a trip that only
    # moved vertices costs one attribute read.
    adopted = adopt_untracked_faces(source_obj)
    # A crease is a property of the border *between* patches, and a hand edit
    # can move one: re-derive them rather than leave the old ones.
    apply_result_shading(context, result_obj)
    # Every side match reads from this, and the vertices it caches have moved.
    invalidate_boundary_cache()
    invalidate_crack_cache()
    return adopted, cleared


# --- taking a patch out for a re-edit, reversibly ---
#
# Clicking a patch that already has geometry removes that geometry immediately,
# so the viewport shows the patch being rebuilt instead of a new grid stacked on
# the old one. The result mesh is snapshotted into a spare mesh datablock first,
# and discarding the re-edit swaps the snapshot back -- so nothing is lost by
# Esc, by leaving the object, or by ending the session mid-edit.


def _snapshot_result_mesh(result_obj: bpy.types.Object) -> str:
    backup = result_obj.data.copy()
    backup.name = f"{result_obj.name}{SNAPSHOT_NAME_SUFFIX}"
    # It has no users while it's just a snapshot; without this it can be purged.
    backup.use_fake_user = True
    return backup.name


def restore_result_snapshot(result_obj_name: str, backup_mesh_name: str) -> bool:
    """Put a snapshot back as `result_obj_name`'s mesh. Returns True if it did."""
    result_obj = bpy.data.objects.get(result_obj_name)
    backup = bpy.data.meshes.get(backup_mesh_name)
    if result_obj is None or backup is None:
        return False

    current = result_obj.data
    current_name = current.name
    result_obj.data = backup
    backup.use_fake_user = False
    if current.users == 0:
        bpy.data.meshes.remove(current)
        backup.name = current_name
    return True


def drop_result_snapshot(backup_mesh_name: str) -> None:
    """Throw away a snapshot (the re-edit was committed, so it's not needed)."""
    backup = bpy.data.meshes.get(backup_mesh_name)
    if backup is None:
        return
    backup.use_fake_user = False
    if backup.users == 0:
        bpy.data.meshes.remove(backup)


def purge_stale_snapshots(keep_name: str = "") -> int:
    """Delete snapshot meshes nothing is using any more, and return how many.

    A snapshot carries a fake user so it can't be purged while a re-edit is in
    flight, which also means an interrupted one (undo rolled the re-edit back,
    Blender crashed, the addon was reloaded) would sit in the file forever.
    Called when entering an object -- a structural moment that gets its own
    undo step -- never from a hover or a callback.
    """
    stale = [mesh for mesh in bpy.data.meshes
             if mesh.name.endswith(SNAPSHOT_NAME_SUFFIX)
             and mesh.name != keep_name
             and mesh.users <= (1 if mesh.use_fake_user else 0)]
    for mesh in stale:
        mesh.use_fake_user = False
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    return len(stale)


def remove_patch_from_result(
    source_obj: bpy.types.Object, face_id: int
) -> tuple[int, str]:
    """Take patch `face_id`'s existing geometry out of the result mesh, after
    snapshotting it. Returns (removed_face_count, snapshot_mesh_name); the name
    is "" when nothing was removed (and no snapshot was taken).

    Faces are picked by their patch id, plus -- for faces that carry none --
    whichever ones sit on this patch by their centre alone. That looser rule is
    deliberate here: the removal happens on click, so a wrong guess is visible
    straight away and restore_result_snapshot undoes it, whereas a face left
    behind is exactly the "two overlapping surfaces" the tag was meant to
    prevent.
    """
    result_obj = bpy.data.objects.get(result_object_name_for(source_obj))
    if result_obj is None:
        return 0, ""
    mesh = result_obj.data
    if len(mesh.polygons) == 0:
        return 0, ""

    ids = _patch_ids_of_faces(mesh) or [NO_PATCH] * len(mesh.polygons)
    targets = {i for i, fid in enumerate(ids) if fid == face_id}
    untagged = [i for i, fid in enumerate(ids) if fid == NO_PATCH]
    if untagged:
        face_id_at = _source_patch_lookup(source_obj, result_obj)
        if face_id_at is not None:
            for i in untagged:
                if face_id_at(mesh.polygons[i].center) == face_id:
                    targets.add(i)

    if not targets:
        return 0, ""

    backup_name = _snapshot_result_mesh(result_obj)

    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.faces.ensure_lookup_table()
    # context='FACES' drops the patch's own vertices but keeps the ones a
    # neighbouring patch still uses, so the rebuilt grid welds back onto them.
    bmesh.ops.delete(bm, geom=[bm.faces[i] for i in targets], context='FACES')
    bm.to_mesh(mesh)
    mesh.update()
    bm.free()

    return len(targets), backup_name


def get_or_create_collection(context: bpy.types.Context) -> bpy.types.Collection:
    coll = bpy.data.collections.get(COLLECTION_NAME)
    if coll is None:
        coll = bpy.data.collections.new(COLLECTION_NAME)
        context.scene.collection.children.link(coll)
    return coll


# --- mirroring the Plasticity collection hierarchy ---------------------
#
# The bridge recreates Plasticity groups as nested collections under an
# "Inbox" collection. A flat Retop collection loses that structure exactly
# when it matters most -- a model with dozens of parts -- so the result mesh
# is placed under the same relative path, rebuilt beneath "Retop".
#
# Collections are IDs, so these run only from the session structural moments
# (ensure_result_object), never from a property callback or a draw handler.


def _collection_parents() -> dict[str, bpy.types.Collection]:
    """child collection name -> parent collection. The scene master collection
    is not in bpy.data.collections, so a collection linked straight into the
    scene simply has no entry here and ends the walk.
    """
    parents = {}
    for coll in bpy.data.collections:
        for child in coll.children:
            parents[child.name] = coll
    return parents


def source_collection_path(source_obj: bpy.types.Object) -> list[str]:
    """The source object collection path *below* Inbox, outermost first.

    Empty when the object is not under an Inbox collection at all -- it was
    not imported by the bridge, or the hierarchy has been reorganised since,
    and inventing a path out of unrelated collection names would be worse than
    leaving the result at the top of Retop.
    """
    collections = [c for c in source_obj.users_collection
                   if c.name != COLLECTION_NAME]
    if not collections:
        return []

    parents = _collection_parents()
    path = []
    current = collections[0]
    seen = set()
    while current is not None and current.name not in seen:
        seen.add(current.name)  # a corrupt cycle must not hang the session
        path.append(current.name)
        current = parents.get(current.name)
    path.reverse()

    # Deepest Inbox wins: nested imports can produce more than one, and the
    # closest one to the object is what its path is relative to.
    inbox_at = None
    for i, name in enumerate(path):
        if name.split(".")[0].lower() == INBOX_COLLECTION_NAME.lower():
            inbox_at = i
    if inbox_at is None:
        return []
    return path[inbox_at + 1:]


def _child_collection(
    parent: bpy.types.Collection, name: str
) -> bpy.types.Collection | None:
    """A direct child of `parent` matching `name`, ignoring Blender .001
    disambiguation suffixes -- otherwise a name already taken elsewhere in the
    blend would make every session create yet another copy of the same level.
    """
    for child in parent.children:
        if child.name == name or child.name.rsplit(".", 1)[0] == name:
            return child
    return None


def ensure_collection_path(
    context: bpy.types.Context, path: list[str]
) -> bpy.types.Collection:
    """The collection at `path` under Retop, creating the levels it needs."""
    coll = get_or_create_collection(context)
    for name in path:
        child = _child_collection(coll, name)
        if child is None:
            child = bpy.data.collections.new(name)
            coll.children.link(child)
        coll = child
    return coll


def place_result_object(
    context: bpy.types.Context,
    result_obj: bpy.types.Object,
    source_obj: bpy.types.Object,
    only_if_unplaced: bool = False,
) -> bpy.types.Collection | None:
    """Link `result_obj` into the mirror of the source object Inbox path.

    `only_if_unplaced` is for result meshes that already existed: they move
    only if they still sit at the top of Retop, i.e. were never placed.
    Anything the user has filed somewhere themselves stays put.
    """
    if not context.scene.plasticity_retop.mirror_source_collections:
        return None

    path = source_collection_path(source_obj)
    if not path:
        return None

    root = get_or_create_collection(context)
    if only_if_unplaced and result_obj.name not in root.objects:
        return None

    target = ensure_collection_path(context, path)
    if result_obj.name in target.objects:
        return target
    for coll in list(result_obj.users_collection):
        coll.objects.unlink(result_obj)
    target.objects.link(result_obj)
    return target


# --- smooth shading with sharp edges -----------------------------------
#
# Plasticity models read as smooth surfaces meeting at hard creases, and a
# retopology that reads as faceted is hard to judge against them. Every face
# is shaded smooth; the creases come from marking edges sharp -- but *only*
# edges between two different patches. One patch is one CAD surface, so its
# interior is smooth by construction, and a plain angle-based auto-sharpen
# would crease a curved patch own low-span interior edges instead.


def _sharp_edge_flags(
    mesh: bpy.types.Mesh, angle_threshold_deg: float
) -> list[bool]:
    """Which edges of the result mesh are creases: patch borders where the two
    sides genuinely meet at an angle. A tangent border (a fillet running into
    the face it blends) stays smooth, which is the whole point of using the
    angle rather than "every patch border".
    """
    sharp = [False] * len(mesh.edges)
    if not mesh.polygons:
        return sharp

    patch_ids = _patch_ids_of_faces(mesh)
    edge_index = {tuple(sorted(edge.vertices)): edge.index for edge in mesh.edges}

    edge_faces = {}
    for poly in mesh.polygons:
        for key in poly.edge_keys:
            edge_faces.setdefault(key, []).append(poly.index)

    threshold = math.radians(angle_threshold_deg)
    for key, faces in edge_faces.items():
        if len(faces) != 2:
            continue  # open border or non-manifold: leave shading alone
        first, second = faces
        if patch_ids[first] == patch_ids[second]:
            continue
        normal_a = mesh.polygons[first].normal
        normal_b = mesh.polygons[second].normal
        if normal_a.length < 1e-9 or normal_b.length < 1e-9:
            continue
        if normal_a.angle(normal_b, 0.0) > threshold:
            index = edge_index.get(key)
            if index is not None:
                sharp[index] = True
    return sharp


def apply_result_shading(
    context: bpy.types.Context, result_obj: bpy.types.Object
) -> None:
    """Shade the result mesh smooth and re-mark its creases.

    Called after every commit and whenever the shading settings change. It
    only writes mesh attributes -- no datablock is created -- so it is safe
    from a property update callback.
    """
    state = context.scene.plasticity_retop
    mesh = result_obj.data
    if not mesh.polygons:
        return

    smooth = state.result_shade_smooth
    mesh.polygons.foreach_set("use_smooth", [smooth] * len(mesh.polygons))

    flags = (_sharp_edge_flags(mesh, state.sharp_edge_angle) if smooth
             else [False] * len(mesh.edges))
    attr = mesh.attributes.get(SHARP_EDGE_ATTR)
    if attr is None:
        attr = mesh.attributes.new(SHARP_EDGE_ATTR, 'BOOLEAN', 'EDGE')
    attr.data.foreach_set("value", flags)
    mesh.update()


def refresh_result_shading(context: bpy.types.Context) -> None:
    """Re-shade every result mesh -- the shading settings are global."""
    for result_obj in iter_result_objects(context):
        apply_result_shading(context, result_obj)


def apply_wireframe_opacity(context: bpy.types.Context) -> None:
    """Push the wireframe opacity setting into every 3D viewport.

    Blender has no per-object wireframe opacity: `show_wire` is drawn by the
    viewport overlay, and its strength is that overlay own
    `wireframe_opacity`. Driving it is the only way to fade the retopology
    wireframe -- with the caveat, stated in the panel, that it is a viewport
    setting and therefore applies to every object showing a wireframe there.
    """
    opacity = context.scene.plasticity_retop.result_wire_opacity
    for window in context.window_manager.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != 'VIEW_3D':
                continue
            for space in area.spaces:
                if space.type == 'VIEW_3D':
                    space.overlay.wireframe_opacity = opacity


# --- datablock creation and undo ---
#
# Creating or freeing an ID (material, mesh, object, collection) outside of an
# operator that pushes an undo step is what makes Ctrl+Z crash Blender: the
# undo state restored around it doesn't know about the datablock, and the
# depsgraph then walks an object whose data or material array was freed under
# it. So ID creation happens ONLY on the session's structural moments
# (ensure_result_object, the first update_preview_object of a session) -- never
# from a property update callback, a draw handler or a hover.
#
# Everything reached from an update callback (the refresh_*_appearance
# functions) therefore looks materials up and quietly does without if they
# aren't there yet.


def _create_material(name: str) -> bpy.types.Material:
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
        mat.use_nodes = True
    return mat


def _existing_material(name: str) -> bpy.types.Material | None:
    """Look up a material without ever creating one -- safe from any context."""
    return bpy.data.materials.get(name)


def ensure_materials() -> None:
    """Create the addon's materials up front, from a context allowed to create
    datablocks. Result meshes need two of them, not one: the mesh being worked
    on and the other retop meshes shown alongside it carry different alphas,
    and a single shared material can only hold one.
    """
    _create_material(PREVIEW_MATERIAL_NAME)
    _create_material(RESULT_MATERIAL_NAME)
    _create_material(RESULT_DIM_MATERIAL_NAME)


def _apply_material_appearance(
    mat: bpy.types.Material, color: tuple[float, float, float], alpha: float
) -> None:
    bsdf = mat.node_tree.nodes.get("Principled BSDF") if mat.node_tree else None
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (*color, 1.0)
        if "Alpha" in bsdf.inputs:
            bsdf.inputs["Alpha"].default_value = alpha

    # Transparency in Material Preview / Rendered viewport shading, across
    # the property name used by different Blender versions.
    if hasattr(mat, "surface_render_method"):
        mat.surface_render_method = 'BLENDED' if alpha < 1.0 else 'DITHERED'
    elif hasattr(mat, "blend_method"):
        mat.blend_method = 'BLEND' if alpha < 1.0 else 'OPAQUE'

    mat.diffuse_color = (*color, alpha)


def _apply_offset_modifier(obj: bpy.types.Object, offset: float) -> None:
    mod = obj.modifiers.get(OFFSET_MODIFIER_NAME)
    if offset == 0.0:
        if mod is not None:
            obj.modifiers.remove(mod)
        return
    if mod is None:
        mod = obj.modifiers.new(OFFSET_MODIFIER_NAME, 'DISPLACE')
        mod.texture = None
        mod.direction = 'NORMAL'
        mod.mid_level = 0.0
    mod.strength = offset


# --- symmetry -----------------------------------------------------------
#
# Retopping half a symmetric part and mirroring the rest is most of the saving
# on a symmetric model, so this is a Mirror *modifier* on the result object and
# not baked geometry. That is not just non-destructiveness: every piece of this
# addon's bookkeeping reads the result mesh's **base** data -- commit and
# re-edit (PATCH_ID_ATTR), neighbour matching (committed_boundary_map), shading
# (apply_result_shading), adoption -- so a modifier is invisible to all of it by
# construction. Baked mirror faces would carry the same patch ids as the
# originals, and re-editing a patch would delete both halves and rebuild one.
#
# The plane is the **source object's** origin and axes (`mirror_object`), not
# the result object's. Plasticity currently drops every import at the world
# origin so the two coincide, but that is a fact about today's bridge rather
# than something to depend on, and the source object is what the user means by
# "the object".
MIRROR_MODIFIER_NAME = "RetopMirror"
MIRROR_AXES = ('X', 'Y', 'Z')


def mirror_target(
    context: bpy.types.Context, obj: bpy.types.Object | None = None
) -> tuple[bpy.types.Object | None, bpy.types.Object | None]:
    """(source, result) the mirror applies to, or (None, None).

    Resolved from the running session first, then from `obj` (defaulting to the
    active object) through `source_object_for_result`, so pointing at either
    half of the pair -- the CAD object or its retopology -- works. A source with
    no result mesh yet has nothing to mirror and comes back as (source, None).
    """
    state = context.scene.plasticity_retop
    source = None
    if state.session_active and state.session_object_name:
        source = bpy.data.objects.get(state.session_object_name)
    if source is None:
        candidate = obj if obj is not None else context.active_object
        if candidate is not None and candidate.type == 'MESH':
            source = source_object_for_result(candidate) or candidate
    if source is None:
        return None, None
    return source, bpy.data.objects.get(result_object_name_for(source))


def mirror_axes(result_obj: bpy.types.Object | None) -> tuple[bool, bool, bool]:
    """Which axes the result mesh is currently mirrored on.

    Read off the modifier rather than a scene property: the axes belong to one
    object, and two objects being retopped in the same file have no reason to
    agree on them.
    """
    if result_obj is None:
        return (False, False, False)
    mod = result_obj.modifiers.get(MIRROR_MODIFIER_NAME)
    if mod is None:
        return (False, False, False)
    return tuple(bool(a) for a in mod.use_axis)


def apply_mirror_settings(context: bpy.types.Context) -> None:
    """Push the panel's clip/merge settings onto every existing mirror.

    Called from the property callbacks, so it must not create anything: an
    object with no mirror on it stays without one.
    """
    state = context.scene.plasticity_retop
    merge = state_mod.to_blender_units(state, state.mirror_merge_distance)
    for result_obj in iter_result_objects(context):
        mod = result_obj.modifiers.get(MIRROR_MODIFIER_NAME)
        if mod is None:
            continue
        mod.use_clip = state.mirror_clip
        mod.use_mirror_merge = merge > 0.0
        mod.merge_threshold = max(merge, 1e-6)


def set_mirror_axes(
    context: bpy.types.Context,
    source_obj: bpy.types.Object,
    result_obj: bpy.types.Object,
    axes: tuple[bool, bool, bool],
) -> tuple[bool, bool, bool]:
    """Mirror `result_obj` on `axes`, removing the modifier when none are left.

    A modifier is not an ID, so this is safe outside an undo step and from a
    property callback -- the same reason `_apply_offset_modifier` can be.
    """
    mod = result_obj.modifiers.get(MIRROR_MODIFIER_NAME)
    if not any(axes):
        if mod is not None:
            result_obj.modifiers.remove(mod)
        return (False, False, False)

    state = context.scene.plasticity_retop
    if mod is None:
        mod = result_obj.modifiers.new(MIRROR_MODIFIER_NAME, 'MIRROR')
        # Ahead of the cosmetic offset, so the stack reads as "the mesh, then
        # how it is drawn". Visually either order works -- the Displace is
        # along normals and the mirror flips them with the geometry -- but a
        # reader should not have to work that out.
        if result_obj.modifiers.find(MIRROR_MODIFIER_NAME) > 0:
            result_obj.modifiers.move(
                result_obj.modifiers.find(MIRROR_MODIFIER_NAME), 0)

    # The source object's origin and axes are the plane, not the result's.
    mod.mirror_object = source_obj
    mod.use_axis = axes
    merge = state_mod.to_blender_units(state, state.mirror_merge_distance)
    mod.use_clip = state.mirror_clip
    mod.use_mirror_merge = merge > 0.0
    mod.merge_threshold = max(merge, 1e-6)
    return tuple(bool(a) for a in mod.use_axis)


def toggle_mirror_axis(
    context: bpy.types.Context,
    source_obj: bpy.types.Object,
    result_obj: bpy.types.Object,
    axis: str,
) -> tuple[bool, bool, bool]:
    """Flip one axis of the mirror and return the axes that are on afterwards."""
    index = MIRROR_AXES.index(axis)
    axes = list(mirror_axes(result_obj))
    axes[index] = not axes[index]
    return set_mirror_axes(context, source_obj, result_obj, tuple(axes))


def bake_mirror(
    context: bpy.types.Context, result_obj: bpy.types.Object
) -> tuple[int, str | None]:
    """Apply the mirror into real geometry. Returns (faces added, error).

    The mirrored faces are stamped NO_PATCH on the way out, and that is the
    whole reason this is an operator rather than a note telling you to use the
    modifier dropdown. A mirrored face inherits the patch id of the face it
    was copied from, and `remove_patch_from_result` deletes *every* face
    carrying the id being re-edited -- so re-editing one patch after a plain
    apply would take both halves out and rebuild only one, tearing a hole in
    the mirrored side that nothing would ever put back.

    Untracked is the right resting state for them: unclaimed faces are never
    deleted, and `adopt_untracked_faces` will hand each one to the Plasticity
    face it actually sits on the next time the object is entered -- which, on
    the symmetric part this was used for, is the real face on the other side.
    """
    mod = result_obj.modifiers.get(MIRROR_MODIFIER_NAME)
    if mod is None:
        return 0, "Nothing to apply: this retopology has no mirror"
    if context.mode != 'OBJECT':
        return 0, "Leave Edit Mode first"

    mesh = result_obj.data
    # Face centres before the apply, so the copies can be told from the
    # originals afterwards without assuming anything about the order Blender
    # emits them in. The originals come through untouched, so they match
    # exactly; a rounded key is only insurance against float noise.
    def key(centre: mathutils.Vector) -> tuple[int, int, int]:
        return tuple(round(c * 1e5) for c in centre)

    before = {}
    ids = _patch_ids_of_faces(mesh) or [NO_PATCH] * len(mesh.polygons)
    for poly in mesh.polygons:
        before[key(poly.center)] = ids[poly.index]
    faces_before = len(mesh.polygons)

    previous_active = context.view_layer.objects.active
    context.view_layer.objects.active = result_obj
    try:
        bpy.ops.object.modifier_apply(modifier=MIRROR_MODIFIER_NAME)
    except RuntimeError as exc:
        return 0, f"Could not apply the mirror: {exc}"
    finally:
        if previous_active is not None:
            context.view_layer.objects.active = previous_active

    mesh = result_obj.data
    attr = mesh.attributes.get(PATCH_ID_ATTR)
    if attr is None:
        attr = mesh.attributes.new(PATCH_ID_ATTR, 'INT', 'FACE')
    values = [before.get(key(poly.center), NO_PATCH) for poly in mesh.polygons]
    attr.data.foreach_set("value", values)
    mesh.update()

    apply_result_shading(context, result_obj)
    invalidate_boundary_cache()
    invalidate_crack_cache()
    return len(mesh.polygons) - faces_before, None


def refresh_preview_appearance(context: bpy.types.Context) -> None:
    """Re-apply color/alpha/offset settings to the current preview object
    without touching its geometry -- cheap, called from property callbacks.
    """
    obj = bpy.data.objects.get(PREVIEW_OBJ_NAME)
    if obj is None:
        return
    state = context.scene.plasticity_retop
    mat = _existing_material(PREVIEW_MATERIAL_NAME)
    if mat is not None:
        _apply_material_appearance(mat, tuple(state.preview_color), state.preview_alpha)
    obj.color = (*state.preview_color, state.preview_alpha)
    _apply_offset_modifier(obj, preview_lift(context))
    # Same question as the result: draw over the scene, or be occluded like
    # any other object. Alt+X answers it for both, or the preview would keep
    # floating in front while the retopology it belongs to is being checked
    # against the surface.
    obj.show_in_front = state.result_see_through


def ensure_result_object(
    context: bpy.types.Context, source_obj: bpy.types.Object
) -> bpy.types.Object:
    """Return the retop result object for `source_obj`, creating an empty one
    if it doesn't exist yet. Called when entering a retop session (so there's
    something to highlight from the start) and by commit.
    """
    result_name = result_object_name_for(source_obj)
    result_obj = bpy.data.objects.get(result_name)
    if result_obj is not None:
        # Retopology made before the hierarchy was mirrored sits flat at the
        # top of Retop; move it in, but only from there (see place_result_object).
        place_result_object(context, result_obj, source_obj, only_if_unplaced=True)
        return result_obj

    coll = get_or_create_collection(context)
    ensure_materials()
    mesh = bpy.data.meshes.new(result_name)
    result_obj = bpy.data.objects.new(result_name, mesh)
    coll.objects.link(result_obj)
    result_obj.matrix_world = mathutils.Matrix.Identity(4)
    mesh.materials.append(_create_material(RESULT_MATERIAL_NAME))
    # Distinct color from the raw CAD mesh and from the (orange) in-progress
    # preview; the emphasized in-front/wireframe look is only turned on while
    # a retop session is open on this object (see set_result_highlight), so it
    # doesn't visually dominate the viewport once you've moved on.
    _resting_result_appearance(result_obj, tuple(context.scene.plasticity_retop.result_color))
    _apply_result_offset(context, result_obj)
    place_result_object(context, result_obj, source_obj)
    return result_obj


def source_object_for_result(
    result_obj: bpy.types.Object,
) -> bpy.types.Object | None:
    """The Plasticity mesh a result object was built from, or None."""
    if not result_obj.name.endswith(RESULT_NAME_SUFFIX):
        return None
    return bpy.data.objects.get(result_obj.name[:-len(RESULT_NAME_SUFFIX)])


def _auto_offset_for(source_obj: bpy.types.Object | None) -> float:
    """A z-fighting offset proportional to the model, so it works unchanged on
    a 2mm fillet and on a 3m part without anyone typing a magic number.
    """
    if source_obj is None:
        return 0.0
    dims = source_obj.dimensions
    diagonal = mathutils.Vector((dims.x, dims.y, dims.z)).length
    return diagonal * AUTO_OFFSET_RATIO


def result_lift(
    context: bpy.types.Context, source_obj: bpy.types.Object | None
) -> float:
    """How far the committed retopology of `source_obj` is pushed off the CAD
    surface, in Blender units: the explicit Result Offset, or the automatic
    one derived from the object's size.

    One function, because the preview has to be lifted by the same measure:
    the two are shown side by side along a shared boundary, and an offset the
    preview doesn't know about is exactly what makes a committed neighbour
    swallow the patch being built.
    """
    state = context.scene.plasticity_retop
    offset = state_mod.to_blender_units(state, state.result_offset)
    if offset <= 0.0:
        offset = _auto_offset_for(source_obj)
    return offset


def preview_lift(context: bpy.types.Context) -> float:
    """How far the *preview* is pushed off the surface.

    The result offset times PREVIEW_LIFT_RATIO, and nothing else. Strictly
    more than the result, never less: the patch being built (or re-edited,
    where the hover draws it straight over the committed faces before the click
    removes them) must read as the thing in front, and two coplanar surfaces
    z-fight into a stipple that says nothing. The margin is a fraction of an
    offset that is itself 0.1% of the model, so the seam with a committed
    neighbour stays visually flush.

    There is **one** offset, not two. An Extra Offset slider sat beside this
    for a while, in its own panel section, and it read as a second independent
    setting -- which is precisely what it was not, since it was added to a
    number this one already derives from. One distance, one control.
    """
    return result_lift(context, preview_source_object()) * PREVIEW_LIFT_RATIO


def preview_source_object() -> bpy.types.Object | None:
    """The CAD object the current preview belongs to. Stamped on the preview
    at generation time, so the lift is right even outside a session.
    """
    obj = bpy.data.objects.get(PREVIEW_OBJ_NAME)
    if obj is None:
        return None
    return bpy.data.objects.get(obj.get(PREVIEW_SOURCE_PROP, ""))


def _apply_result_offset(
    context: bpy.types.Context, result_obj: bpy.types.Object
) -> None:
    """Push the result mesh off the CAD surface along its normals, purely so
    the two don't z-fight. Non-destructive (a Displace modifier) and flagged
    show_render=False, so the geometry that actually gets rendered/exported is
    the true, un-offset one sitting exactly on the surface.
    """
    offset = result_lift(context, source_object_for_result(result_obj))

    mod = result_obj.modifiers.get(RESULT_OFFSET_MODIFIER_NAME)
    if offset <= 0.0:
        if mod is not None:
            result_obj.modifiers.remove(mod)
        return
    if mod is None:
        mod = result_obj.modifiers.new(RESULT_OFFSET_MODIFIER_NAME, 'DISPLACE')
        mod.texture = None
        mod.direction = 'NORMAL'
        mod.mid_level = 0.0
    mod.strength = offset
    mod.show_render = False


def _apply_result_look(
    result_obj: bpy.types.Object,
    color: tuple[float, float, float],
    alpha: float,
    material_name: str,
    in_front: bool,
    wire: bool,
) -> None:
    # Get-only: this runs from appearance property callbacks, which must not
    # create datablocks (see the note above _create_material).
    mat = _existing_material(material_name)
    if mat is not None:
        _apply_material_appearance(mat, color, alpha)
        if result_obj.data.materials:
            if result_obj.data.materials[0] is not mat:
                result_obj.data.materials[0] = mat
        else:
            result_obj.data.materials.append(mat)
    result_obj.color = (*color, alpha)
    result_obj.show_in_front = in_front
    result_obj.show_wire = wire
    result_obj.show_all_edges = wire


def _wire_wanted(state: state_mod.RetopPatchState, emphasized: bool) -> bool:
    """Whether an emphasized (in-session) result mesh shows its wireframe.

    Deliberately scoped to the session: a resting result mesh has never shown
    its wireframe and still doesn't, so finished retopology stops dominating
    the viewport once you have moved on from it.
    """
    return bool(state.result_show_wire and emphasized)


def _resting_result_appearance(
    result_obj: bpy.types.Object, color: tuple[float, float, float]
) -> None:
    """Neutral, always-on look: same color so it still reads as "retopped",
    but opaque and without the in-front/wireframe emphasis used in-session.
    """
    _apply_result_look(result_obj, color, 1.0, RESULT_MATERIAL_NAME, in_front=False, wire=False)


def iter_result_objects(context: bpy.types.Context) -> list[bpy.types.Object]:
    coll = bpy.data.collections.get(COLLECTION_NAME)
    if coll is None:
        return []
    # all_objects, not objects: result meshes are nested under a mirror of the
    # source object's Plasticity collection path (see place_result_object), so
    # a flat scan would miss every one of them.
    return [o for o in coll.all_objects if o.name.endswith(RESULT_NAME_SUFFIX)]


def orphan_result_objects(context: bpy.types.Context) -> list[bpy.types.Object]:
    """Retopology meshes whose source object no longer exists under the name
    they were built from -- typically because the CAD object was renamed or
    re-imported since. They're invisible to everything here (patch tracking,
    re-editing, span propagation all resolve through `<Source>_Retop`), so a
    session on the renamed object silently starts a *second* result mesh and
    the two overlap in the viewport. Surfaced in the panel for that reason.
    """
    return [o for o in iter_result_objects(context)
            if source_object_for_result(o) is None and len(o.data.polygons) > 0]


def refresh_result_appearance(context: bpy.types.Context) -> None:
    """Apply the right look to every retop result mesh in one pass:

    - the one being worked on: full Result Appearance alpha, drawn in front
      with its wireframe, so the topology under the cursor stays readable;
    - the others, while a session is running and Show All Retopo is on:
      same color at the dimmed alpha, wireframe but not in front, so previously
      retopped parts stay visible for context without stealing attention;
    - everything else: resting (opaque, no emphasis).

    Called on every session transition and from the appearance property
    callbacks.
    """
    state = context.scene.plasticity_retop
    color = tuple(state.result_color)
    # Whether the retopology draws over everything else, or is occluded like
    # any other object. A setting rather than a consequence of the session: it
    # is the only way to check the result sits *on* the surface instead of
    # hovering off it, and that is a thing you want to check mid-session.
    see_through = state.result_see_through
    # A hand-edit overrides it for the mesh being edited: the vertices you are
    # dragging are the point of the trip, and the CAD surface sits over half of
    # them. This is Blender's In Front flag, so nothing moves -- the topology
    # is still drawn exactly where it is, which an extra lift could not claim.
    tweaking = (state.session_phase == 'TWEAK' and state.tweak_draw_in_front)

    active_name = ""
    # In the OBJECT phase the session holds no object, so a hand-edit started
    # there names its own; without that the trip's own mesh is not the "active"
    # one and the override lands on nothing.
    source_name = state.session_object_name or (
        state.tweak_source_object if state.session_phase == 'TWEAK' else "")
    if state.session_active and source_name:
        source_obj = bpy.data.objects.get(source_name)
        if source_obj is not None:
            active_name = result_object_name_for(source_obj)

    for result_obj in iter_result_objects(context):
        _apply_result_offset(context, result_obj)

        if result_obj.name == active_name:
            _apply_result_look(result_obj, color, state.result_alpha,
                               RESULT_MATERIAL_NAME,
                               in_front=see_through or tweaking,
                               wire=_wire_wanted(state, True))
        elif state.session_active and state.highlight_all_results:
            _apply_result_look(result_obj, color, state.inactive_result_alpha,
                               RESULT_DIM_MATERIAL_NAME, in_front=False,
                               wire=_wire_wanted(state, True))
        else:
            _resting_result_appearance(result_obj, color)

    # The preview's lift and see-through are derived from these same settings,
    # so it has to follow them: a Result Offset changed mid-session would
    # otherwise leave the patch being built sunk into its committed neighbours.
    refresh_preview_appearance(context)
    apply_wireframe_opacity(context)


def set_result_highlight(
    context: bpy.types.Context, source_obj: bpy.types.Object, active: bool
) -> None:
    """Kept as the call site used by session transitions; the actual decision
    for every result mesh is made by refresh_result_appearance from session
    state, so all of them stay consistent with each other.
    """
    refresh_result_appearance(context)


def ensure_preview_object(context: bpy.types.Context) -> bpy.types.Object:
    """The preview object, created once and then reused for the whole session.

    Hovering used to create it and delete it again on every mouse move, which
    is the single worst thing an addon can do to Blender's undo: a Ctrl+Z
    landing between two of those steps restores a state where the object or its
    mesh has been freed, and Blender crashes rebuilding the depsgraph. Now the
    object outlives the hover and only its geometry is rewritten.
    """
    obj = bpy.data.objects.get(PREVIEW_OBJ_NAME)
    if obj is not None:
        return obj

    coll = get_or_create_collection(context)
    ensure_materials()
    mesh = bpy.data.meshes.new(PREVIEW_OBJ_NAME)
    obj = bpy.data.objects.new(PREVIEW_OBJ_NAME, mesh)
    coll.objects.link(obj)
    return obj


def update_preview_object(
    context: bpy.types.Context,
    source_obj: bpy.types.Object,
    result: "generators.base.GenerationResult",
    corner_source_ids: list[int] | None = None,
) -> bpy.types.Object:
    obj = ensure_preview_object(context)
    mesh = obj.data
    mesh.clear_geometry()
    verts = [tuple(v) for v in result.verts]
    mesh.from_pydata(verts, [], result.faces)
    mesh.update()

    # One patch is one CAD surface: no creases inside it, so smooth shading
    # alone makes the preview read like the committed result will.
    smooth = context.scene.plasticity_retop.result_shade_smooth
    if mesh.polygons:
        mesh.polygons.foreach_set("use_smooth", [smooth] * len(mesh.polygons))

    uv_layer = mesh.uv_layers.get("UVMap") or mesh.uv_layers.new(name="UVMap")
    for poly in mesh.polygons:
        for li in range(poly.loop_start, poly.loop_start + poly.loop_total):
            vi = mesh.loops[li].vertex_index
            uv_layer.data[li].uv = result.uvs[vi]

    source_vid_attr = mesh.attributes.get(SOURCE_VID_ATTR)
    if source_vid_attr is None:
        source_vid_attr = mesh.attributes.new(SOURCE_VID_ATTR, 'INT', 'POINT')
    values = [NO_SOURCE] * len(mesh.vertices)
    if corner_source_ids:
        # A negative local index is a corner the generator could not place --
        # a ring's phased rim, whose points land nowhere near the source vertex
        # its loop started at. It is emitted rather than dropped so this zip
        # stays aligned (the outer loop's ids come first); the vertex simply
        # keeps NO_SOURCE and welds by proximity, like every other boundary
        # point that moved.
        for local_idx, source_idx in zip(result.corner_local_indices, corner_source_ids):
            if local_idx >= 0:
                values[local_idx] = source_idx
    source_vid_attr.data.foreach_set("value", values)

    boundary_attr = mesh.attributes.get(BOUNDARY_ATTR)
    if boundary_attr is None:
        boundary_attr = mesh.attributes.new(BOUNDARY_ATTR, 'BOOLEAN', 'POINT')
    boundary_values = [False] * len(mesh.vertices)
    for local_idx in result.boundary_local_indices:
        boundary_values[local_idx] = True
    boundary_attr.data.foreach_set("value", boundary_values)

    if len(mesh.materials) == 0:
        preview_mat = _existing_material(PREVIEW_MATERIAL_NAME)
        if preview_mat is not None:
            mesh.materials.append(preview_mat)

    obj.matrix_world = source_obj.matrix_world.copy()
    obj.hide_render = True
    obj[PREVIEW_SOURCE_PROP] = source_obj.name
    # Drawn with its wireframe visible, so the grid being built is easy to
    # read while tweaking spans; show_in_front and the lift off the surface
    # are set by refresh_preview_appearance, from the same settings the
    # committed result follows.
    obj.show_wire = True
    obj.show_all_edges = True

    refresh_preview_appearance(context)
    return obj


def has_preview() -> bool:
    """True when there is preview geometry to commit or discard. The preview
    object itself sticks around empty between patches, so its mere existence
    doesn't mean anything -- its polygons do.
    """
    obj = bpy.data.objects.get(PREVIEW_OBJ_NAME)
    return obj is not None and len(obj.data.polygons) > 0


def clear_preview_object() -> None:
    """Empty the preview without deleting anything.

    Used everywhere inside a session (hover moved off a patch, patch committed,
    preview discarded): freeing the object here would put ID churn back on the
    hover path, which is what made undo crash. remove_preview_object does the
    real teardown, once, when the session ends.
    """
    obj = bpy.data.objects.get(PREVIEW_OBJ_NAME)
    if obj is None:
        return
    obj.data.clear_geometry()
    obj.data.update()


def remove_preview_object() -> None:
    """Drop the preview object for good -- session teardown only."""
    obj = bpy.data.objects.get(PREVIEW_OBJ_NAME)
    if obj is None:
        return
    mesh = obj.data
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def commit_preview_to_result(
    context: bpy.types.Context,
    source_obj: bpy.types.Object,
    face_id: int | None = None,
) -> tuple[bpy.types.Object | None, str | None]:
    """Bake the current preview object's *base* geometry (i.e. without the
    cosmetic offset modifier, in world space) into the persistent retop
    result mesh for `source_obj`, welding only corner vertices that share
    the same source Plasticity vertex id with geometry already present.
    Returns (result_obj, error_message_or_None).

    `face_id` is the Plasticity face id of the patch being committed. It is
    stamped onto every face this call adds, and any faces already carrying it
    are removed first -- that's what makes re-selecting a committed patch and
    changing its spans a *replacement* rather than a second copy layered on
    top of the first. Deleting with context='FACES' is deliberate: it drops the
    patch's own interior/boundary vertices but keeps the ones still used by a
    neighbouring patch's faces, so the neighbours stay welded to the new grid.
    """
    preview_obj = bpy.data.objects.get(PREVIEW_OBJ_NAME)
    if preview_obj is None or len(preview_obj.data.polygons) == 0:
        return None, "No preview to commit"

    result_obj = ensure_result_object(context, source_obj)

    src_mesh = preview_obj.data  # base mesh: offset modifier is NOT evaluated here
    world_matrix = preview_obj.matrix_world.copy()

    bm = bmesh.new()
    bm.from_mesh(result_obj.data)
    uv_layer = bm.loops.layers.uv.get("UVMap") or bm.loops.layers.uv.new("UVMap")
    result_vid_layer = bm.verts.layers.int.get(SOURCE_VID_ATTR) or bm.verts.layers.int.new(SOURCE_VID_ATTR)
    result_boundary_layer = bm.verts.layers.int.get(BOUNDARY_ATTR) or bm.verts.layers.int.new(BOUNDARY_ATTR)
    patch_id_layer = bm.faces.layers.int.get(PATCH_ID_ATTR)
    if patch_id_layer is None:
        # A result mesh committed before patch tracking existed: a fresh int
        # layer would give every one of its faces id 0, which would then read
        # as "patch 0 is committed" and let a re-edit of face 0 delete all of
        # them. Mark them as belonging to no known patch instead.
        patch_id_layer = bm.faces.layers.int.new(PATCH_ID_ATTR)
        for face in bm.faces:
            face[patch_id_layer] = NO_PATCH

    # Drop a previous version of this same patch before anything else, so the
    # vertex bookkeeping below only ever sees geometry that survives.
    if face_id is not None:
        stale = [f for f in bm.faces if f[patch_id_layer] == face_id]
        if stale:
            bmesh.ops.delete(bm, geom=stale, context='FACES')
            bm.verts.ensure_lookup_table()
            bm.faces.ensure_lookup_table()

    # existing source-vertex-id -> bm.vert already in the result mesh
    existing_by_source_id = {}
    for v in bm.verts:
        sid = v[result_vid_layer]
        if sid != NO_SOURCE:
            existing_by_source_id[sid] = v

    src_uv_layer = src_mesh.uv_layers.get("UVMap")
    src_vid_attr = src_mesh.attributes.get(SOURCE_VID_ATTR)
    src_vids = [NO_SOURCE] * len(src_mesh.vertices)
    if src_vid_attr is not None:
        src_vid_attr.data.foreach_get("value", src_vids)

    src_boundary_attr = src_mesh.attributes.get(BOUNDARY_ATTR)
    src_boundary = [False] * len(src_mesh.vertices)
    if src_boundary_attr is not None:
        src_boundary_attr.data.foreach_get("value", src_boundary)

    vert_map = {}
    for v in src_mesh.vertices:
        sid = src_vids[v.index]
        if sid != NO_SOURCE and sid in existing_by_source_id:
            vert_map[v.index] = existing_by_source_id[sid]
            continue
        world_co = world_matrix @ v.co
        new_vert = bm.verts.new(world_co)
        new_vert[result_vid_layer] = sid
        new_vert[result_boundary_layer] = 1 if src_boundary[v.index] else 0
        if sid != NO_SOURCE:
            existing_by_source_id[sid] = new_vert
        vert_map[v.index] = new_vert
    bm.verts.ensure_lookup_table()

    skipped = 0
    for poly in src_mesh.polygons:
        loop_range = range(poly.loop_start, poly.loop_start + poly.loop_total)
        loop_verts = [vert_map[src_mesh.loops[li].vertex_index] for li in loop_range]
        try:
            new_face = bm.faces.new(loop_verts)
        except ValueError:
            skipped += 1
            continue
        new_face[patch_id_layer] = NO_PATCH if face_id is None else face_id
        if src_uv_layer:
            for loop, li in zip(new_face.loops, loop_range):
                loop[uv_layer].uv = src_uv_layer.data[li].uv

    # Weld coincident boundary vertices only (never interior/reprojected
    # ones): with propagation keeping spans equal along a shared edge, the
    # new patch's boundary resample points land (almost) exactly on the
    # neighbor's already-committed boundary points, so this closes the
    # "positions match but topology doesn't" gap propagation alone leaves.
    # Scoping to boundary-flagged verts and a tiny epsilon keeps this safe --
    # see the module docstring for why an unscoped weld is dangerous.
    boundary_verts = [v for v in bm.verts if v[result_boundary_layer] == 1]
    retop_state = context.scene.plasticity_retop
    weld_distance = state_mod.to_blender_units(retop_state, retop_state.boundary_weld_distance)
    if len(boundary_verts) > 1 and weld_distance > 0.0:
        bmesh.ops.remove_doubles(bm, verts=boundary_verts, dist=weld_distance)

    bm.to_mesh(result_obj.data)
    result_obj.data.update()
    bm.free()

    # Creases can only be decided once the new patch is in place: sharpness is
    # a property of the border *between* patches, so committing a neighbour
    # changes the shading of an edge that already existed.
    apply_result_shading(context, result_obj)

    clear_preview_object()

    if skipped:
        return result_obj, None  # committed; a few faces already existed and were skipped
    return result_obj, None


# --- matching a committed neighbour exactly ----------------------------
#
# Copying a neighbour's segment *count* is not enough to weld to it, and the
# difference is invisible until you look at the vertices: the two patches only
# land on the same points if they also divide the same polyline the same way.
# They often don't -- a neighbour committed as an n-gon put its points where the
# boundary curves, not at even spacing, and a grid resampling evenly to the same
# count lands between them every time.
#
# So a match reads the neighbour's *actual* committed boundary vertices. What
# makes that cheap to use is `geometry.resample_polyline_by_arclength`: asked
# for exactly as many points as it was given, it returns them untouched. Feed a
# side those points, tell the generator to put len-1 segments along it, and
# every generator -- Quad, Triangle, N-Side, Wedge, Ring, N-gon, none of them
# modified -- reproduces them exactly.


def _distance_to_polyline(
    point: mathutils.Vector, polyline: list[mathutils.Vector]
) -> tuple[float, float]:
    """(distance, arc length at the closest point) of `point` on `polyline`."""
    best_distance = float("inf")
    best_at = 0.0
    travelled = 0.0
    for start, end in zip(polyline, polyline[1:]):
        segment = end - start
        length = segment.length
        if length < 1e-12:
            continue
        t = max(0.0, min(1.0, (point - start).dot(segment) / (length * length)))
        distance = (point - (start + segment * t)).length
        if distance < best_distance:
            best_distance = distance
            best_at = travelled + t * length
        travelled += length
    return best_distance, best_at


# Committed boundary vertices, grouped by the patch that owns them and cached
# per (result mesh contents, transform). Walking the result mesh is what a
# hover used to spend most of its time on, and the grouping is what lets a
# match aim at the patch actually across a side instead of at everything in
# reach -- see `operators.build_side_references`.
_boundary_cache: dict[str, tuple[tuple, CommittedMap]] = {}
_BOUNDARY_CACHE_LIMIT = 4


def invalidate_boundary_cache() -> None:
    _boundary_cache.clear()


def committed_boundary_map(source_obj: bpy.types.Object) -> CommittedMap:
    """{patch face id: [boundary vertex, ...]} in the source object's local
    space, over the whole committed result mesh.

    A vertex can be on the boundary of two patches at once (that is the point
    of welding), and it is listed under both -- the caller asks "which of these
    patches' vertices lie along my side", and either answer is right.

    Faces committed before patch tracking existed carry `NO_PATCH`; they are
    kept under that key rather than dropped, so old retopology stays matchable.
    """
    result_obj = bpy.data.objects.get(result_object_name_for(source_obj))
    if result_obj is None or not result_obj.data.polygons:
        return {}

    mesh = result_obj.data
    to_source_local = source_obj.matrix_world.inverted() @ result_obj.matrix_world
    key = result_obj.name
    fingerprint = (source_obj.name,
                   patch_data.mesh_fingerprint(mesh),
                   tuple(to_source_local[row][col] for row in range(4) for col in range(4)))
    cached = _boundary_cache.get(key)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]

    patch_ids = _patch_ids_of_faces(mesh)

    # Boundary-flagged verts only -- an interior vertex that happens to pass
    # near a side is not something to weld to. Non-fatal: retopology committed
    # before the flag existed has none, and all of its vertices stay eligible.
    boundary_attr = mesh.attributes.get(BOUNDARY_ATTR)
    flags = None
    if boundary_attr is not None:
        flags = [0] * len(mesh.vertices)
        boundary_attr.data.foreach_get("value", flags)
        if not any(flags):
            flags = None

    grouped = {}
    for poly in mesh.polygons:
        face_id = patch_ids[poly.index] if patch_ids else NO_PATCH
        bucket = grouped.setdefault(face_id, set())
        for index in poly.vertices:
            if flags is None or flags[index]:
                bucket.add(index)

    # Result geometry is stored in the same space the preview was built in
    # (see commit_preview_to_result); sides are in the source object's local one.
    result = {face_id: [to_source_local @ mesh.vertices[i].co for i in sorted(indices)]
              for face_id, indices in grouped.items()}

    if len(_boundary_cache) >= _BOUNDARY_CACHE_LIMIT:
        _boundary_cache.pop(next(iter(_boundary_cache)))
    _boundary_cache[key] = (fingerprint, result)
    return result


# --- cracked borders -------------------------------------------------------
#
# A patch matched to its neighbour welds to it; change one patch's span
# afterwards and it stops welding, leaving a seam along a border that used to
# be closed. Nothing showed it. The side picker says so *while* the patch is
# open -- the side turns red -- but the moment it is committed the warning goes
# with the session, and the patch that did not change is the one left cracked.
#
# What is drawn is the **CAD edge** the two patches share, not either patch's
# own row of vertices. The two rows sag off that curve by different amounts
# (that is what a mismatched span is), so drawing them would draw the symptom
# twice and neither would be the border itself. The edge is the thing both
# patches were supposed to meet on.
#
# Detection reads the source's B-rep edges rather than pairing result vertices
# by proximity, and that is what keeps it honest: proximity cannot tell "the
# patch across this edge" from "a patch that happens to run close by" -- the
# same reason `_match_pool` exists. Two patches are only ever compared along an
# edge the CAD model says they share.
#
# **An open edge belongs to a border only when its *interior* lies along it.**
# The first version asked "is there an open edge within reach of this point on
# the border", and a radius cannot answer that: the reach has to be about the
# size of a retopology cell, and on a bevelled part the next border along is
# that far away too. Measured on `Cube Bevel Edges`, four perfectly welded
# borders came back with three of seven samples "covered" -- by open edges
# belonging to the borders either side of them -- against seven of seven for
# the two that were genuinely open, which is a threshold sitting in the middle
# of the thing it is meant to separate.
#
# Lying *along* the border is what separates them, and it does so by kind
# rather than by degree. An open edge of the row along a border sits a chord's
# sagitta off it -- second order in the cell size, 0.00 to 0.05 of a cell on
# that same part -- for its whole length. An edge leaving a junction is on the
# border at one end and a whole cell away at the other, so it fails outright
# however the tolerance is set.
#
# Sampled at the quarter points and **never at the ends**: a junction is where
# several borders meet, so an endpoint sitting on one of them sits on all of
# them and answers nothing. That is not a corner case -- a coarse patch commits
# a whole border as a single edge, whose two ends are both junctions.
#
# And the border is chosen **for the edge, not for each probe**: the one whose
# *furthest* probe is nearest, which is the border the edge lies along rather
# than the one it happens to touch. Asking each probe separately and requiring
# them to agree fails wherever a border is shorter than the tolerance -- on
# `Cube Chamfer Edges` the whole part is four cells wide, so the first probe of
# a 0.148-long border answered with the border next to it and the disagreement
# dropped a crack covering all of it.

# How far off the CAD edge an open edge's ends may sit and still be that
# border's, as a share of the retopology's own cell size. A cap, not the
# discriminator -- the both-ends rule is that -- so it only has to be loose
# enough for a coarse row's chords to sag off a curved border, which is far
# less than a cell.
CRACK_NEAR_RATIO = 0.5
# ...floored by a share of the model extent, for the flat case where the
# sagitta is zero and the only distance left is float rounding.
CRACK_FLOOR_RATIO = 1e-4
# How much of a shared edge each side's open row has to cover before the border
# is called cracked. Both patches tessellate the whole of an edge they share,
# so a real crack covers essentially all of it from both sides (chords fall a
# little short of the arc, hence not 1.0); anything that merely brushes past is
# an order of magnitude below this.
CRACK_COVERAGE = 0.5
# Samples along the shared edges, so a huge part cannot make the index
# unbounded. Same reasoning as `patch_data.NEIGHBOUR_INDEX_POINTS`.
CRACK_INDEX_POINTS = 200_000

# (patch a, patch b, the shared CAD edge, in the *source* object's local space)
CrackEdge = tuple[int, int, list[mathutils.Vector]]

_crack_cache: dict[str, tuple[tuple, list["CrackEdge"]]] = {}
_CRACK_CACHE_LIMIT = 4


def invalidate_crack_cache() -> None:
    _crack_cache.clear()


def _open_edges(
    mesh: bpy.types.Mesh, to_source_local: mathutils.Matrix
) -> "tuple[list[tuple[mathutils.Vector, mathutils.Vector]], list[int], list[float], float]":
    """Every result edge with one face on it: its two ends, the owning patch,
    its length, and the median of those lengths.

    Both ends, not the midpoint: which border an edge belongs to is decided by
    where *both* of them fall, and a midpoint cannot tell an edge lying along a
    border from one leaving it at a junction.

    An edge with one face is the retopology saying outright that nothing is
    joined to it here. Most of them are perfectly normal -- the frontier of
    what has been retopped so far, or the model's own open boundary -- so this
    only collects them; which border each belongs to, and whether that border
    is cracked, is decided against the CAD edges.
    """
    owners = _patch_ids_of_faces(mesh)
    if not owners:
        return [], [], [], 0.0

    faces_on_edge: dict[tuple[int, int], list[int]] = {}
    for poly in mesh.polygons:
        verts = list(poly.vertices)
        for index, a in enumerate(verts):
            b = verts[(index + 1) % len(verts)]
            faces_on_edge.setdefault((a, b) if a < b else (b, a), []).append(poly.index)

    ends = []
    patch_of = []
    lengths = []
    for (a, b), faces in faces_on_edge.items():
        if len(faces) != 1:
            continue
        pa = to_source_local @ mesh.vertices[a].co
        pb = to_source_local @ mesh.vertices[b].co
        ends.append((pa, pb))
        patch_of.append(owners[faces[0]])
        lengths.append((pb - pa).length)

    ordered = sorted(lengths)
    median = ordered[len(ordered) // 2] if ordered else 0.0
    return ends, patch_of, lengths, median


def crack_edges(source_obj: bpy.types.Object) -> list["CrackEdge"]:
    """Shared CAD edges the committed retopology has failed to close.

    Both patches committed, both leaving an open row along the border they
    share: that is a seam, and it is the one thing about finished retopology
    that is worth drawing in the viewport. A border with only one side
    committed is not a crack -- it is simply the next patch, not done yet.

    Cached on the same fingerprints as everything else derived from a mesh:
    this walks both meshes, and a draw handler runs on every redraw.
    """
    result_obj = bpy.data.objects.get(result_object_name_for(source_obj))
    if result_obj is None or not result_obj.data.polygons:
        return []
    source_mesh = source_obj.data
    if not source_mesh.polygons:
        return []

    to_source_local = source_obj.matrix_world.inverted() @ result_obj.matrix_world
    key = result_obj.name
    fingerprint = (source_obj.name,
                   patch_data.mesh_fingerprint(source_mesh),
                   patch_data.mesh_fingerprint(result_obj.data),
                   tuple(to_source_local[row][col] for row in range(4) for col in range(4)))
    cached = _crack_cache.get(key)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]

    result = _find_crack_edges(source_mesh, result_obj.data, to_source_local)

    if len(_crack_cache) >= _CRACK_CACHE_LIMIT:
        _crack_cache.pop(next(iter(_crack_cache)))
    _crack_cache[key] = (fingerprint, result)
    return result


def _find_crack_edges(
    source_mesh: bpy.types.Mesh,
    result_mesh: bpy.types.Mesh,
    to_source_local: mathutils.Matrix,
) -> list["CrackEdge"]:
    """The scan itself, so `crack_edges` is only the cache around it."""
    from mathutils.kdtree import KDTree

    ends, patch_of, edge_lengths, cell = _open_edges(result_mesh, to_source_local)
    if not ends:
        return []       # nothing open anywhere: nothing can be cracked

    committed = {value for value in _patch_ids_of_faces(result_mesh)
                 if value != NO_PATCH}
    shared = [(owner, other, polyline)
              for owner, other, polyline in cad_display.shared_edges(source_mesh)
              if owner in committed and other in committed and len(polyline) >= 2]
    if not shared:
        return []

    low = [min(vertex.co[axis] for vertex in source_mesh.vertices) for axis in range(3)]
    high = [max(vertex.co[axis] for vertex in source_mesh.vertices) for axis in range(3)]
    extent = sum((high[axis] - low[axis]) ** 2 for axis in range(3)) ** 0.5
    near = max(cell * CRACK_NEAR_RATIO, extent * CRACK_FLOOR_RATIO)
    if near <= 0.0:
        return []

    tree, sample_owner, spans = _shared_edge_index(shared, near, KDTree)
    if tree is None:
        return []

    # An open edge of patch A can only ever be A's side of one of A's own
    # borders, and it only lies **along** one if its whole interior does.
    covered: dict[int, dict[int, float]] = {}
    for index, (start, finish) in enumerate(ends):
        owner = patch_of[index]
        edge = _border_along(tree, sample_owner, shared, start, finish, owner, near)
        if edge is None:
            continue
        covered.setdefault(edge, {})
        covered[edge][owner] = covered[edge].get(owner, 0.0) + edge_lengths[index]

    cracked = []
    for edge, (owner, other, polyline) in enumerate(shared):
        sides = covered.get(edge)
        if not sides:
            continue
        wanted = CRACK_COVERAGE * spans[edge]
        if min(sides.get(owner, 0.0), sides.get(other, 0.0)) >= wanted:
            cracked.append((owner, other, polyline))
    return cracked


# Where along an open edge it is asked which border it lies on. Interior
# points only: at an endpoint every border meeting at that junction is equally
# close, so the answer there is a coin toss between them.
CRACK_PROBES = (0.25, 0.5, 0.75)


def _border_along(
    tree: Any,
    sample_owner: list[int],
    shared: list["CrackEdge"],
    start: mathutils.Vector,
    finish: mathutils.Vector,
    patch: int,
    near: float,
) -> int | None:
    """The border this open edge runs along, or None if it runs along none.

    A border is only in the running if **every** probe is within `near` of it,
    and among those the winner is the one whose worst probe is nearest. An edge
    leaving a junction is close to its border at one probe and far at the next,
    so it never qualifies for that border -- while still qualifying, correctly,
    for the one it does run along. An edge on the frontier of the retopology,
    or on the model's own open boundary, qualifies for none and is not a
    defect.
    """
    per_probe = []
    for fraction in CRACK_PROBES:
        point = start.lerp(finish, fraction)
        nearest: dict[int, float] = {}
        for _co, sample, distance in tree.find_range(point, near):
            edge = sample_owner[sample]
            if patch in shared[edge][:2]:
                if distance < nearest.get(edge, near * 2):
                    nearest[edge] = distance
        if not nearest:
            return None      # this probe is off every border of the patch
        per_probe.append(nearest)

    common = set(per_probe[0])
    for nearest in per_probe[1:]:
        common &= set(nearest)
    if not common:
        return None
    return min(common, key=lambda edge: max(probe[edge] for probe in per_probe))


def _shared_edge_index(
    shared: list["CrackEdge"], near: float, kdtree_class: Any
) -> "tuple[Any, list[int], list[float]]":
    """A KD-tree of points along every candidate border, plus their lengths.

    Sampled at one *uniform* spacing rather than per vertex, so the query
    radius is a constant and each lookup returns a handful of hits -- the same
    reasoning as `patch_data.resolve_neighbours_by_geometry`, where indexing
    per segment and searching a multiple of the segment's own length swept a
    large part of the mesh once per query.
    """
    spans = []
    for _owner, _other, polyline in shared:
        spans.append(sum((b - a).length for a, b in zip(polyline, polyline[1:])))
    total = sum(spans)
    if total <= 0.0:
        return None, [], spans

    spacing = max(near * 0.5, total / CRACK_INDEX_POINTS)
    points = []
    sample_owner = []
    for edge, (_owner, _other, polyline) in enumerate(shared):
        for a, b in zip(polyline, polyline[1:]):
            direction = b - a
            steps = max(1, int(direction.length / spacing) + 1)
            for step in range(steps):
                points.append(a + direction * (step / steps))
                sample_owner.append(edge)
        points.append(polyline[-1])
        sample_owner.append(edge)

    tree = kdtree_class(len(points))
    for index, point in enumerate(points):
        tree.insert(point, index)
    tree.balance()
    return tree, sample_owner, spans


def crack_segments(
    source_obj: bpy.types.Object, dash: float
) -> list[mathutils.Vector]:
    """Cracked borders as dashed point pairs, for one LINES batch.

    Dashed in the geometry rather than by a shader: the builtin polyline shader
    has no stipple, and a dash pattern computed once per mesh change is nothing
    next to one computed per redraw. Dashes make the line read as a *warning*
    rather than as another piece of structure -- the CAD edge overlay draws
    solid lines along the very same curves.
    """
    segments = []
    for _owner, _other, polyline in crack_edges(source_obj):
        for a, b in zip(polyline, polyline[1:]):
            span = (b - a).length
            if span <= 0.0:
                continue
            steps = max(1, int(span / dash))
            for step in range(steps):
                if step % 2:
                    continue
                start = a.lerp(b, step / steps)
                end = a.lerp(b, min(1.0, (step + 1) / steps))
                segments.append(start)
                segments.append(end)
    return segments


def committed_boundary_points(
    source_obj: bpy.types.Object,
    exclude_face_id: int | None = None,
    only_face_ids: Iterable[int] | None = None,
) -> list[mathutils.Vector]:
    """Committed retopology vertices a side could be matched onto, in the
    source object's local space.

    `exclude_face_id` drops the patch being edited, whose own geometry would
    otherwise match itself. `only_face_ids`, when given, keeps just those
    patches -- that is how a side aims at the neighbour actually across it
    rather than at whatever committed geometry happens to pass nearby.

    Given neither, the answer is every committed patch. That is the right
    default for a first look: a side can run against two committed patches in
    sequence (the boundary between them falls mid-side whenever the angle test
    didn't put a corner there), and asking only the majority one back yields
    half a side's worth of points.
    """
    grouped = committed_boundary_map(source_obj)
    wanted = set(grouped) if only_face_ids is None else (set(only_face_ids) & set(grouped))
    return flatten_boundary_points(grouped, wanted, exclude_face_id)


def flatten_boundary_points(
    grouped: CommittedMap,
    face_ids: Iterable[int],
    exclude_face_id: int | None = None,
) -> list[mathutils.Vector]:
    """The vertices of `face_ids` out of a `committed_boundary_map`, deduped.

    Deduped because a welded vertex is listed under every patch that owns it,
    and a match counting it twice reads two neighbour vertices where there is
    one -- which is one segment too many along the side.
    """
    seen = set()
    points = []
    for face_id in face_ids:
        if face_id == exclude_face_id:
            continue
        for point in grouped.get(face_id, ()):  # noqa: B905
            key = (round(point.x, 9), round(point.y, 9), round(point.z, 9))
            if key not in seen:
                seen.add(key)
                points.append(point)
    return points if len(points) >= 2 else []


def match_side_to_points(
    pool: list[mathutils.Vector],
    side_points: list[mathutils.Vector],
    tolerance: float,
    merge: float | None = None,
    rivals: "list[list[mathutils.Vector]] | None" = None,
    partial: bool = False,
) -> tuple[list[mathutils.Vector] | None, str]:
    """Which of `pool` lie along this side, in order, or (None, reason).

    `tolerance` is how far off the side a committed vertex may sit and still
    count as being on it. `merge` is a different question with a different
    answer: how close two of them have to be to be *the same vertex*, owned by
    two patches at once. It defaults to `tolerance`, which is right only while
    the two are the same number.

    They are not, for a pinned side: the picker's margin is a share of the
    patch's longest side, and on a ring that is a whole rim, so the margin ends
    up far wider than the neighbour's own vertex spacing. Deduping at that
    distance then merges *consecutive* neighbour vertices -- 61 points came
    back as 31 -- and the side is handed half the count it should reproduce,
    which is the zigzag band in the report. A pin must reach further off the
    side than an automatic match without ever becoming blinder along it.

    `rivals` are the patch's *other* sides, and a candidate nearer to one of
    them than to this one belongs to that side, not this one -- however well
    inside the tolerance it sits. Without that, a pin on a narrow band reached
    across it and took the neighbour's far row as well as the near one (61
    points came back as 117), which reads as the match spreading over the whole
    surface instead of following the edge under the cursor. A shared corner is
    equally near both sides, so the test is by a clear `merge` margin and keeps
    it.

    `partial` accepts a neighbour that covers only part of the side, and
    **completes** it: the covered stretch keeps the neighbour's own vertices,
    the rest is filled in at the neighbour's own spacing, along this side's own
    polyline. That is a different thing from matching a *count* over a partial
    cover, which is what the endpoint rule below refuses and will go on
    refusing -- a count alone lands between the neighbour's vertices and leaves
    the half-cell offset the whole feature exists to close. Reproducing the
    vertices where the neighbour is, and choosing the rest freely, has no
    offset to leave: the shared stretch is exact and the remainder borders
    nothing.

    `reason` says which check refused -- an opaque "nothing to match" on a side
    that visibly touches a retopologized neighbour is impossible to act on.
    """
    if merge is None:
        merge = tolerance
    if len(side_points) < 2:
        return None, "side has no length"
    if len(pool) < 2:
        return None, "nothing committed yet"

    found = []
    for point in pool:
        distance, at = _distance_to_polyline(point, side_points)
        if distance > tolerance:
            continue
        if rivals and any(_distance_to_polyline(point, other)[0] < distance - merge
                          for other in rivals):
            continue  # it lies along another side of this patch, not this one
        found.append((at, distance, point))
    if not found:
        return None, "no committed neighbour"

    found = _nearest_row(found, merge)
    found = [(at, point) for at, _distance, point in found]
    if len(found) < 2:
        # One point is not something to follow: a side shorter than the
        # neighbour's own vertex spacing has nothing to match along it, and
        # saying so beats "no neighbour" on a side that is visibly against one.
        return None, "only one committed vertex along this side"

    found.sort(key=lambda item: item[0])

    # Drop duplicates at the same place along the side -- two patches meeting
    # here both own the corner vertex. At `merge`, never at `tolerance`: two
    # copies of one welded vertex are coincident, while two *different* ones
    # are a segment apart and must both survive.
    ordered = [found[0][1]]
    for _at, point in found[1:]:
        if (point - ordered[-1]).length > merge:
            ordered.append(point)
    if len(ordered) < 2:
        return None, "no committed neighbour"

    # A *closed* side -- a cornerless loop, which is what a ring's rims and a
    # disc's boundary are -- has one endpoint, not two: `resolve_side_points`
    # returns `loop + [loop[0]]`. Asking for a committed vertex at both ends
    # then asks for two at the same place and always refuses, which is why a
    # bore's rim read as unmatchable while visibly bordering retopology.
    if (side_points[0] - side_points[-1]).length <= tolerance:
        return _close_matched_ring(ordered, side_points, tolerance, partial)

    # Endpoint coverage: without it a neighbour touching part of the side hands
    # back a count that cannot line up along the rest -- the silent half-cell
    # offset this exists to prevent. Unless the rest is *filled in* rather than
    # counted, which is what `partial` does.
    short_start = (ordered[0] - side_points[0]).length > tolerance
    short_end = (ordered[-1] - side_points[-1]).length > tolerance
    if short_start or short_end:
        if not partial:
            return None, ("neighbour stops short of this side's start"
                          if short_start else "neighbour stops short of this side's end")
        return _complete_open_side(ordered, side_points)

    return ordered, ""


def _nearest_row(
    found: "list[tuple[float, float, mathutils.Vector]]", merge: float
) -> "list[tuple[float, float, mathutils.Vector]]":
    """Keep the row of candidates lying *on* the side, drop the next one back.

    A neighbour patch is a grid, so it has a second row of vertices a cell
    behind the one it shares -- and a pinned side's reach is a share of the
    patch's longest side, which on a narrow band is far wider than that cell.
    Both rows then came back (58 points became 116) and the side was asked to
    reproduce a zigzag between them: that is the match "spreading over the
    surface" instead of following the edge under the cursor.

    Told apart by a *gap*, never by an absolute distance. Within one row the
    distances vary smoothly -- that variation is the drift the margin exists to
    reach -- so the row is however far the numbers keep climbing gently, and a
    second row announces itself with a jump bigger than everything the first
    one has varied by. Which makes the rule self-scaling: no constant here says
    how far a neighbour may have drifted, only that a step much larger than the
    drift so far is not drift.
    """
    ordered = sorted(found, key=lambda item: item[1])
    for i in range(1, len(ordered)):
        gap = ordered[i][1] - ordered[i - 1][1]
        spread = ordered[i - 1][1] - ordered[0][1]
        if gap > max(2.0 * merge, 2.0 * spread):
            return ordered[:i]
    return ordered


# What "covers the whole loop" means for a closed side. A gap has to be both a
# large share of the loop *and* far bigger than the others: a neighbour that is
# simply coarse has every gap the same size -- a square inscribed in a circle
# already leaves 22% between points -- while one covering half the rim leaves
# one huge gap among small ones. Testing the share alone refused coarse but
# perfectly matchable neighbours.
#
# **Measured along the side, never as the straight line between two points.**
# The share is of the loop's arc length, so the gaps have to be arc lengths
# too, and on a loop the two diverge without limit: a neighbour covering an
# eighth of a bore's rim leaves a gap of seven eighths, but the *chord* closing
# it is shorter than the rim's own diameter -- 0.44 against a 6.28 perimeter in
# `tests/test_partial_match.py`, i.e. 7% of the loop where the truth is 93%.
# So that neighbour read as covering the whole rim, and the rim came back built
# from the eighth of it the neighbour had touched.
CLOSED_SIDE_MAX_GAP = 0.25
CLOSED_SIDE_GAP_RATIO = 3.0


# A partial match fills what the neighbour does not cover at the neighbour's
# own spacing, and a side cannot be given more segments than this however fine
# the neighbour is against however long the side. Only a backstop: it is
# reached by a neighbour two orders of magnitude finer than the side it borders,
# which is a mesh worth refusing rather than a case worth serving.
MAX_MATCHED_SEGMENTS = 512


def _run_spacing(ordered: list[mathutils.Vector]) -> float:
    """The neighbour's own vertex spacing along the stretch it covers.

    The *median* step, not the mean: a run picked up across a corner has one
    long step in it, and the mean would spread that over the whole fill.
    """
    steps = sorted((b - a).length for a, b in zip(ordered, ordered[1:]))
    steps = [step for step in steps if step > 0.0]
    return steps[len(steps) // 2] if steps else 0.0


def _cumulative(points: list[mathutils.Vector]) -> list[float]:
    walked = [0.0]
    for a, b in zip(points, points[1:]):
        walked.append(walked[-1] + (b - a).length)
    return walked


def _along(
    points: list[mathutils.Vector], walked: list[float], distance: float
) -> mathutils.Vector:
    """The point `distance` along a polyline, by arc length.

    Along the *side's own polyline*, never along a chord between the two
    matched points either side of the gap: the filled points have to sit on the
    boundary the CAD drew, or the patch's edge cuts across the surface.
    """
    distance = min(max(distance, 0.0), walked[-1])
    for index in range(len(walked) - 1):
        span = walked[index + 1] - walked[index]
        if span <= 0.0:
            continue
        if walked[index + 1] >= distance:
            return points[index].lerp(points[index + 1],
                                      (distance - walked[index]) / span)
    return points[-1].copy()


def _fill(
    side_points: list[mathutils.Vector],
    walked: list[float],
    start: float,
    finish: float,
    spacing: float,
) -> list[mathutils.Vector]:
    """Interior points between two arc positions, at about `spacing` apart."""
    gap = finish - start
    if gap <= 0.0 or spacing <= 0.0:
        return []
    steps = max(1, min(MAX_MATCHED_SEGMENTS, round(gap / spacing)))
    return [_along(side_points, walked, start + gap * step / steps)
            for step in range(1, steps)]


def _at_along(
    point: mathutils.Vector, side_points: list[mathutils.Vector]
) -> float:
    """Where along the side this point sits, by arc length."""
    return _distance_to_polyline(point, side_points)[1]


def _complete_open_side(
    ordered: list[mathutils.Vector], side_points: list[mathutils.Vector]
) -> tuple[list[mathutils.Vector] | None, str]:
    """A run covering part of an open side, extended to the whole of it.

    The side's own endpoints are kept exactly: they are the patch's corners,
    welded to their neighbours *by identity*, so moving one would break a weld
    that has nothing to do with this match.
    """
    spacing = _run_spacing(ordered)
    if spacing <= 0.0:
        return None, "neighbour's vertices are coincident"

    walked = _cumulative(side_points)
    head = _at_along(ordered[0], side_points)
    tail = _at_along(ordered[-1], side_points)

    points = [side_points[0].copy()]
    points += _fill(side_points, walked, 0.0, head, spacing)
    points += ordered
    points += _fill(side_points, walked, tail, walked[-1], spacing)
    points.append(side_points[-1].copy())
    return points, ""


def _close_matched_ring(
    ordered: list[mathutils.Vector],
    side_points: list[mathutils.Vector],
    tolerance: float,
    partial: bool = False,
) -> tuple[list[mathutils.Vector] | None, str]:
    """Turn matched points on a closed side into a closed polyline, or refuse.

    One thing has to hold: the neighbour must reach all the way round, checked
    as the largest *gap* between consecutive points, since "both ends covered"
    means nothing on a loop.

    What is deliberately **not** required is a neighbour vertex on the side's
    start point. That start is arbitrary -- a cornerless loop begins wherever
    the half-edge walk happened to, so the "corner" there is not a B-rep vertex
    and nothing else in the model agrees on it. Insisting on one refused every
    real case: a disc committed as a Quad puts its points at arc-length
    resamples from *its own* synthesised corners, which land nowhere near.
    The points are rotated to lead with whichever is nearest instead, and the
    caller drops that side's corner id (see `apply_side_matches`) so nothing
    tries to weld by an identity that has moved.
    """
    total = sum((b - a).length for a, b in zip(side_points, side_points[1:]))
    if total <= 0.0:
        return None, "side has no length"

    positions = [_at_along(point, side_points) for point in ordered]
    gaps = [b - a for a, b in zip(positions, positions[1:])]
    gaps.append(total - positions[-1] + positions[0])   # the wrap, along the side
    gaps.sort()
    largest = gaps[-1]
    median = gaps[len(gaps) // 2]
    if (largest > total * CLOSED_SIDE_MAX_GAP
            and largest > median * CLOSED_SIDE_GAP_RATIO):
        if not partial:
            return None, "neighbour only covers part of this loop"
        ordered = _complete_closed_side(ordered, side_points, total)
        if ordered is None:
            return None, "neighbour's vertices are coincident"

    start = side_points[0]
    at_start = min(range(len(ordered)), key=lambda i: (ordered[i] - start).length)

    # Rotate so that point leads, then repeat it to close the loop -- the same
    # shape `resolve_side_points` hands the generators for a cornerless loop.
    rotated = ordered[at_start:] + ordered[:at_start]
    rotated.append(rotated[0].copy())
    return rotated, ""


def _complete_closed_side(
    ordered: list[mathutils.Vector],
    side_points: list[mathutils.Vector],
    total: float,
) -> list[mathutils.Vector] | None:
    """A run covering an arc of a closed side, extended round the rest of it.

    The reverse of the open case: a loop has no endpoints to keep, so the whole
    remainder is one gap and the fill simply carries the neighbour's spacing
    round it. Which is the case in the report -- a bore's rim bordered by one
    small committed patch, refused outright until now even though the arc they
    share is perfectly matchable.
    """
    spacing = _run_spacing(ordered)
    if spacing <= 0.0:
        return None

    walked = _cumulative(side_points)
    positions = [_at_along(point, side_points) for point in ordered]
    order = sorted(range(len(ordered)), key=lambda i: positions[i])
    ordered = [ordered[i] for i in order]
    positions = [positions[i] for i in order]

    # The gap is what the run does *not* cover, and on a loop it **wraps**: it
    # runs from the last matched point, through the side's own start, to the
    # first. Filled as one arc rather than as the two pieces either side of
    # that start -- the start of a cornerless loop is wherever the half-edge
    # walk began, and splitting the fill there would put one short step at a
    # place nothing in the model knows about.
    gap = total - positions[-1] + positions[0]
    if gap <= 0.0:
        return list(ordered)
    steps = max(1, min(MAX_MATCHED_SEGMENTS, round(gap / spacing)))
    filled = list(ordered)
    for step in range(1, steps):
        filled.append(_along(side_points, walked,
                             (positions[-1] + gap * step / steps) % total))
    return filled


def side_match_tolerance(
    state: state_mod.RetopPatchState,
    side_points: list[mathutils.Vector],
    margin: bool = False,
    reference_length: float | None = None,
) -> float:
    """How far off the boundary a committed vertex may sit and still count as
    being on it.

    Two answers, deliberately. Both patches usually resample the same polyline,
    so the real distance is ~0 and the strict tolerance is float slack. That is
    what the *automatic* matching uses: it fires without being asked, so it must
    never reach for something that only happens to be nearby. `margin=True` is
    the picker's answer, widened by `match_margin`: pointing at a side is saying
    which neighbour you mean, so it can afford to reach one that has drifted --
    a coarse neighbour whose chords sag off a curved boundary, or two CAD edges
    tessellated slightly differently.

    Both are a share of `reference_length`, which callers should set to the
    **patch's** longest side rather than let it default to this side's own.
    A neighbour's drift is an absolute distance; scaling by the side made a
    short side's margin vanish while the long side next to it matched fine, so
    a stub between two retopped faces refused for no reason the user could see.
    Every side of one patch gets the same absolute reach.
    """
    if reference_length is None:
        reference_length = sum((b - a).length
                               for a, b in zip(side_points, side_points[1:]))
    strict = max(state_mod.to_blender_units(state, state.boundary_weld_distance),
                 reference_length * 1e-3, 1e-9)
    if not margin:
        return strict
    return max(strict, reference_length * state.match_margin / 100.0)


# --- Local View ('/') ---------------------------------------------------
#
# Blender's isolate only carries the objects that were selected when it was
# entered, so isolating a CAD surface leaves its `<Source>_Retop` mesh and the
# live preview behind -- exactly the two things you want to keep looking at
# while retopping it. These pull them back in.
#
# `local_view_set` only flips a per-viewport visibility flag on the object: no
# ID is created or freed, so this is safe to call outside an undo step.


def local_view_spaces(context: bpy.types.Context) -> list[bpy.types.SpaceView3D]:
    """Every 3D viewport currently in Local View, across all open windows."""
    spaces = []
    for window in context.window_manager.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != 'VIEW_3D':
                continue
            for space in area.spaces:
                if space.type == 'VIEW_3D' and space.local_view is not None:
                    spaces.append(space)
    return spaces


def sync_local_view(context: bpy.types.Context) -> int:
    """Add the preview and the relevant result meshes to every viewport that is
    in Local View. Returns how many objects were added.

    Only the retopology of a source object that is *itself* isolated in that
    viewport is added -- pulling in every result mesh would drag unrelated
    geometry into an isolated view. No-op when the setting is off.
    """
    if not context.scene.plasticity_retop.local_view_include_retop:
        return 0

    spaces = local_view_spaces(context)
    if not spaces:
        return 0

    # local_view_set() needs the object to be in the view layer.
    view_objects = set(context.view_layer.objects)
    results = iter_result_objects(context)
    preview = bpy.data.objects.get(PREVIEW_OBJ_NAME)

    added = 0
    for space in spaces:
        wanted = []
        if preview is not None:
            wanted.append(preview)
        for result in results:
            source = source_object_for_result(result)
            if (source is not None and source in view_objects
                    and source.local_view_get(space)):
                wanted.append(result)

        for obj in wanted:
            if obj not in view_objects or obj.local_view_get(space):
                continue
            obj.local_view_set(space, True)
            added += 1
    return added
