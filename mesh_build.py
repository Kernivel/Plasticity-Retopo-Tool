"""Build/update the preview object shown while tweaking a patch.
Commit it into the source's `<Source>_Retop` result mesh.

Stitching adjacent patches: only a patch's *corner* vertices are guaranteed
to be exact.
Interior boundary points are span-dependent resample points that only coincide
between two patches when their spans happen to match exactly along the
shared edge; blindly proximity-welding them (e.g. bmesh.ops.remove_doubles)
can silently merge unrelated points and drop faces, so we deliberately do
NOT do that here.
To actually get matching spans use the span propagation registry below.

Re-editing a committed patch: each committed face records which Plasticity face it
came from (PATCH_ID_ATTR) and each patch records the spans it was built with
(PATCH_SPANS_PROP).
A patch can be picked again to be re-edited.

The visual "push off the surface" used to see the preview clearly is done
with a non-destructive Displace modifier on the preview object configurable in the UI.

Preview and result are lifted by the *same* measure (result_lift).
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
# The preview is lifted by the result offset times this, so it draws above
# committed patches (see preview_lift).
PREVIEW_LIFT_RATIO = 1.5
# Custom property naming the source object a preview was built from, for its
# lift outside a session.
PREVIEW_SOURCE_PROP = "retop_preview_source"
PREVIEW_MATERIAL_NAME = "RetopPreviewMaterial"
RESULT_MATERIAL_NAME = "RetopResultMaterial"
RESULT_DIM_MATERIAL_NAME = "RetopResultMaterialDim"
COLLECTION_NAME = "Retop"
# The bridge imports under a collection named "Inbox". Mirroring starts below it.
INBOX_COLLECTION_NAME = "Inbox"
SHARP_EDGE_ATTR = "sharp_edge"
# What the addon last wrote into `sharp_edge`, and the user's own choice where
# it differs. See `apply_result_shading`.
SHARP_WRITTEN_ATTR = "retop_sharp_written"
SHARP_USER_ATTR = "retop_sharp_user"
SHARP_USER_NONE = 0
SHARP_USER_ON = 1
SHARP_USER_OFF = 2
SOURCE_VID_ATTR = "retop_source_vid"
BOUNDARY_ATTR = "retop_is_boundary"
# Plasticity face ids are int32, so an INT attribute holds them exactly.
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
    by the unordered pair of corner source-vertex ids. A JSON custom property.
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
    """A side's propagated span, looked up from the source object, or None.
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
    """Record the span along each side of a just-committed patch, for its
    neighbours to propagate. No-op without a result object.
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
# Every committed face carries its patch id (PATCH_ID_ATTR), and each patch's
# settings are stored beside it (PATCH_SPANS_PROP). Picking a patch again
# restores them, and committing replaces its faces.


def get_patch_settings_table(
    result_obj: bpy.types.Object,
) -> dict[str, dict[str, Any]]:
    """{ "<face_id>": {"span_u": .., "span_v": .., "span": .., "generator": ..} }
    for every committed patch. A JSON custom property.
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
    ngon_group_counts: str = "",
) -> None:
    """Record what a just-committed patch was built with, so it reopens the
    same: spans, generator, side grouping and n-gon side counts.
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
        "ngon_group_counts": ngon_group_counts,
    }
    save_patch_settings_table(result_obj, table)


def copy_source_status(
    state: state_mod.RetopPatchState,
    source_obj: bpy.types.Object | None,
    face_id: int,
) -> tuple[str, str]:
    """(title, detail) for a committed patch offered as a density to copy.

    Here, not in `operators`, because the overlay asks it. One wording for the
    tooltip and for what the click reports.
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
        # Shown as the next click would apply them.
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
    """The settings a patch was committed with, or None."""
    result_obj = bpy.data.objects.get(result_object_name_for(source_obj))
    if result_obj is None:
        return None
    return get_patch_settings_table(result_obj).get(str(face_id))


def forget_patch_settings(source_obj: bpy.types.Object, face_id: int) -> bool:
    """Drop the record of what a patch was committed with.

    Called when its geometry is deleted for good. Never touches the span
    registry: its entries describe boundaries a neighbour still shares.
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
    """Patch ids present in `source_obj`'s result mesh. Read from the mesh,
    never the settings table.
    """
    result_obj = bpy.data.objects.get(result_object_name_for(source_obj))
    if result_obj is None:
        return set()
    return {fid for fid in _patch_ids_of_faces(result_obj.data) if fid != NO_PATCH}


def is_patch_committed(source_obj: bpy.types.Object, face_id: int) -> bool:
    return face_id in committed_face_ids(source_obj)


def _source_patch_lookup(
    source_obj: bpy.types.Object,
    to_source_local: mathutils.Matrix,
    surfaces: bool = False,
) -> Callable[[mathutils.Vector], int | None] | None:
    """Return f(co) -> the patch id under a point, by nearest source polygon.

    None if the source has no polygons.
    `co` is mapped into the source's local space by `to_source_local`.
    With `surfaces`, the answer is the raw Plasticity surface, never a composite.
    """
    from . import geometry

    src_mesh = source_obj.data
    if len(src_mesh.polygons) == 0:
        return None

    analysis = (patch_data.analyse_surfaces(src_mesh) if surfaces
                else patch_data.analyse(src_mesh))
    face_id_of_poly = analysis.face_id_of_poly
    bvh, tri_poly = geometry.build_bvh_with_polygon_map(src_mesh)

    def face_id_at(co: mathutils.Vector) -> int | None:
        hit = bvh.find_nearest(to_source_local @ co)
        if hit is None or hit[2] is None:
            return None
        return face_id_of_poly[tri_poly[hit[2]]]

    return face_id_at


def _result_to_source(
    source_obj: bpy.types.Object, result_obj: bpy.types.Object
) -> mathutils.Matrix:
    # Result geometry is in world space. The source's BVH is in its local space.
    return source_obj.matrix_world.inverted() @ result_obj.matrix_world


# --- keeping the tracking true to the part ---
#
# Plasticity can rename faces, so a patch id is only trusted as far as the
# geometry agrees with it. See "A face id is a name" in CLAUDE.md.
#
# The vote samples points inside each face, never the face's own vertices.
# A renamed patch is translated as a whole, by a majority of all its faces.
# A face in no group is decided on its own, then by its neighbours.

# Share of the way from a face corner to its centre where the vote samples.
ADOPTION_PULL = 0.25
ADOPTION_CENTRE_WEIGHT = 2
# A known tag is re-read only when less than this share of its faces sit on it.
KNOWN_TAG_MIN_SHARE = 0.25
# Result object property: `source_signature` as of the last reconciliation.
SOURCE_SIGNATURE_PROP = "retop_source_signature"
# Max distance between a CAD corner and the source vertex it is re-attached to,
# as a share of the model's diagonal.
SOURCE_ID_REMAP_RATIO = 1e-4


def _vote_face(
    face_at: Callable[[mathutils.Vector], int | None],
    mesh: bpy.types.Mesh,
    poly: bpy.types.MeshPolygon,
    votes: dict[int, int],
) -> None:
    centre = poly.center
    fid = face_at(centre)
    if fid is not None:
        votes[fid] = votes.get(fid, 0) + ADOPTION_CENTRE_WEIGHT
    for vi in poly.vertices:
        fid = face_at(mesh.vertices[vi].co.lerp(centre, ADOPTION_PULL))
        if fid is not None:
            votes[fid] = votes.get(fid, 0) + 1


def _strict_winner(votes: dict[int, int]) -> int | None:
    if not votes:
        return None
    best_id, best_votes = max(votes.items(), key=lambda kv: kv[1])
    return best_id if best_votes * 2 > sum(votes.values()) else None


def _adopt_from_neighbours(mesh: bpy.types.Mesh, tags: list[int], pending: list[int]) -> int:
    """Give each face in `pending` the tag most of its edges share with tracked
    faces. Return how many were decided.

    For faces the surface under them cannot decide, e.g. a fill made by hand
    between two patches.
    Runs in passes, so the result does not depend on the order faces are visited.
    """
    if not pending:
        return 0
    edge_faces: dict[tuple[int, int], list[int]] = {}
    for poly in mesh.polygons:
        for key in poly.edge_keys:
            edge_faces.setdefault(key, []).append(poly.index)

    waiting = set(pending)
    decided_total = 0
    while waiting:
        decided: dict[int, int] = {}
        for index in waiting:
            votes: dict[int, int] = {}
            for key in mesh.polygons[index].edge_keys:
                for other in edge_faces[key]:
                    if other != index and other not in waiting and tags[other] != NO_PATCH:
                        votes[tags[other]] = votes.get(tags[other], 0) + 1
            winner = _strict_winner(votes)
            if winner is not None:
                decided[index] = winner
        if not decided:
            break
        for index, winner in decided.items():
            tags[index] = winner
        waiting.difference_update(decided)
        decided_total += len(decided)
    return decided_total


def _translate_patch_settings(
    result_obj: bpy.types.Object, translations: dict[int, int]
) -> None:
    """Move each renamed patch's record to its new id, all in one step.

    Never one after the other: a renumbering can reuse an old id for another face.
    A record already under the new id is kept.
    """
    table = get_patch_settings_table(result_obj)
    moved = {key: value for key, value in table.items()
             if not key.lstrip("-").isdigit() or int(key) not in translations}
    for old, new in translations.items():
        entry = table.get(str(old))
        if entry is not None and str(new) not in moved:
            moved[str(new)] = entry
    if moved != table:
        save_patch_settings_table(result_obj, moved)


def adopt_untracked_faces(source_obj: bpy.types.Object, full: bool = False) -> int:
    """Correct every result face's patch id against the geometry. Return how
    many changed.

    - Untracked faces (NO_PATCH, or 0 when the source has no face 0) are decided
      one by one, then by their neighbours. 0 is what Blender gives a face made
      by hand.
    - Faces whose id the source no longer declares are translated per id, as a
      whole.
    - With `full`, a known id whose faces mostly sit elsewhere is re-read too
      (`KNOWN_TAG_MIN_SHARE`).

    A tag is only written on a strict majority.
    Unclaimed faces are never deleted.
    """
    result_obj = bpy.data.objects.get(result_object_name_for(source_obj))
    if result_obj is None:
        return 0
    mesh = result_obj.data
    count = len(mesh.polygons)
    if count == 0:
        return 0

    tags = _patch_ids_of_faces(mesh) or [NO_PATCH] * count
    known = set(patch_data.analyse(source_obj.data).patches)
    zero_is_untracked = 0 not in known

    def untracked(tag: int) -> bool:
        return tag == NO_PATCH or (tag == 0 and zero_is_untracked)

    present = set(tags)
    stale = {t for t in present if not untracked(t) and t not in known}
    if not full and not stale and not any(untracked(t) for t in present):
        return 0  # every face names a patch the source has: nothing to do

    face_at = _source_patch_lookup(source_obj, _result_to_source(source_obj, result_obj))
    if face_at is None:
        return 0

    groups: dict[int, list[int]] = {}
    loose: list[int] = []
    for index, tag in enumerate(tags):
        if untracked(tag):
            loose.append(index)
        elif full or tag in stale:
            groups.setdefault(tag, []).append(index)

    new_tags = list(tags)
    translations: dict[int, int] = {}
    for tag, faces in groups.items():
        votes: dict[int, int] = {}
        for index in faces:
            _vote_face(face_at, mesh, mesh.polygons[index], votes)
        total = sum(votes.values())
        if tag in known and (total == 0 or votes.get(tag, 0) >= KNOWN_TAG_MIN_SHARE * total):
            continue
        winner = _strict_winner(votes)
        if winner is not None and winner != tag:
            translations[tag] = winner
            for index in faces:
                new_tags[index] = winner
        elif tag not in known:
            # No majority for the group: decide each face on its own.
            loose.extend(faces)

    for index in loose:
        votes = {}
        _vote_face(face_at, mesh, mesh.polygons[index], votes)
        winner = _strict_winner(votes)
        new_tags[index] = winner if winner is not None else NO_PATCH
    _adopt_from_neighbours(mesh, new_tags, [i for i in loose if new_tags[i] == NO_PATCH])

    changed = sum(1 for old, new in zip(tags, new_tags) if old != new)
    if changed == 0:
        return 0

    attr = mesh.attributes.get(PATCH_ID_ATTR)
    if attr is None:
        attr = mesh.attributes.new(PATCH_ID_ATTR, 'INT', 'FACE')
    attr.data.foreach_set("value", new_tags)
    mesh.update()
    result_obj[ADOPTION_PROP] = 1
    if translations:
        _translate_patch_settings(result_obj, translations)
    renamed = f", {len(translations)} renamed patch(es) followed" if translations else ""
    print(f"[Plasticity Retop] Re-read the patch of {changed} face(s) of "
          f"'{result_obj.name}'{renamed}")
    return changed


def source_signature(mesh: bpy.types.Mesh) -> str:
    """The source's `patch_data.geometry_fingerprint`, as a string an object
    property can hold."""
    return ":".join(str(v) for v in patch_data.geometry_fingerprint(mesh))


def _world_diagonal(obj: bpy.types.Object) -> float:
    corners = [obj.matrix_world @ mathutils.Vector(c) for c in obj.bound_box]
    if not corners:
        return 0.0
    lo = mathutils.Vector((min(c[i] for c in corners) for i in range(3)))
    hi = mathutils.Vector((max(c[i] for c in corners) for i in range(3)))
    return (hi - lo).length


def reanchor_composites(source_obj: bpy.types.Object, result_obj: bpy.types.Object) -> int:
    """Find the surfaces of every composite that stopped applying. Return how
    many were restored.

    Reads its anchors first, then the result faces carrying its id.
    The composite keeps its id.
    It is only rewritten when it comes back with as many distinct surfaces as it
    had, none claimed by another composite.
    """
    mesh = source_obj.data
    stored = patch_data.read_composites(mesh)
    if not stored:
        return 0
    applicable, dropped = patch_data.applicable_composites(mesh, mesh.get("face_ids") or ())
    if not dropped:
        return 0

    declared = set(mesh.get("face_ids") or ())
    claimed = {s for surfaces in applicable.values() for s in surfaces}
    anchors = patch_data.read_composite_anchors(mesh)
    at_anchor = _source_patch_lookup(source_obj, mathutils.Matrix.Identity(4), surfaces=True)
    if at_anchor is None:
        return 0
    at_face = _source_patch_lookup(
        source_obj, _result_to_source(source_obj, result_obj), surfaces=True)
    tags = _patch_ids_of_faces(result_obj.data)

    restored = 0
    for composite_id in dropped:
        old = list(dict.fromkeys(stored[composite_id]))
        found: list[int | None] = []
        points = anchors.get(composite_id)
        if points and len(points) == len(stored[composite_id]):
            found = list(dict.fromkeys(at_anchor(mathutils.Vector(p)) for p in points))
        if len(found) != len(old) or None in found:
            found = []
            for index, tag in enumerate(tags):
                if tag != composite_id:
                    continue
                votes: dict[int, int] = {}
                _vote_face(at_face, result_obj.data, result_obj.data.polygons[index], votes)
                winner = _strict_winner(votes)
                if winner is not None and winner not in found:
                    found.append(winner)
        if (len(found) != len(old) or len(found) < 2 or None in found
                or not declared.issuperset(found) or claimed.intersection(found)):
            continue
        stored[composite_id] = [int(s) for s in found]
        claimed.update(found)
        restored += 1

    if restored:
        patch_data.write_composites(mesh, stored)
        print(f"[Plasticity Retop] Found the surfaces of {restored} multi-surface "
              f"patch(es) of '{source_obj.name}' again")
    return restored


def remap_source_ids(
    context: bpy.types.Context,
    source_obj: bpy.types.Object,
    result_obj: bpy.types.Object,
) -> int:
    """Re-attach every CAD corner id to the source vertex at its position.
    Return how many changed.

    A corner keeps its id while that source vertex is still the nearest one.
    Otherwise the nearest source vertex within reach takes over, or the id is
    cleared.
    The span registry is re-keyed with the same mapping. Keys that cannot be
    mapped are dropped.
    """
    mesh = result_obj.data
    attr = mesh.attributes.get(SOURCE_VID_ATTR)
    src_mesh = source_obj.data
    source_count = len(src_mesh.vertices)
    if attr is None or len(mesh.vertices) == 0 or source_count == 0:
        return 0

    vids = [NO_SOURCE] * len(mesh.vertices)
    attr.data.foreach_get("value", vids)

    src_matrix = source_obj.matrix_world
    from mathutils.kdtree import KDTree
    tree = KDTree(source_count)
    for vertex in src_mesh.vertices:
        tree.insert(src_matrix @ vertex.co, vertex.index)
    tree.balance()

    state = context.scene.plasticity_retop
    weld = state_mod.to_blender_units(state, state.boundary_weld_distance)
    reach = max(min(weld, _world_diagonal(source_obj) * SOURCE_ID_REMAP_RATIO),
                _world_diagonal(source_obj) * 1e-7, 1e-9)

    result_matrix = result_obj.matrix_world
    mapping: dict[int, int] = {}
    conflicted: set[int] = set()
    changed = 0
    for index, sid in enumerate(vids):
        if sid == NO_SOURCE:
            continue
        world = result_matrix @ mesh.vertices[index].co
        _co, nearest, distance = tree.find(world)
        if nearest == sid:
            new = sid
        elif nearest is not None and distance <= reach:
            new = nearest
        else:
            new = NO_SOURCE
        if mapping.setdefault(sid, new) != new:
            conflicted.add(sid)
        if new != sid:
            vids[index] = new
            changed += 1

    if changed:
        attr.data.foreach_set("value", vids)
        mesh.update()

    registry = get_span_registry(result_obj)
    if registry:
        rekeyed: dict[str, int] = {}
        for key, span in registry.items():
            try:
                a, b = (int(part) for part in key.rsplit("_", 1))
            except ValueError:
                continue
            if a in conflicted or b in conflicted:
                continue
            na, nb = mapping.get(a, NO_SOURCE), mapping.get(b, NO_SOURCE)
            if na != NO_SOURCE and nb != NO_SOURCE:
                rekeyed[_span_key(na, nb)] = span
        if rekeyed != registry:
            save_span_registry(result_obj, rekeyed)
    return changed


def _forget_unknown_settings(
    source_obj: bpy.types.Object, result_obj: bpy.types.Object
) -> None:
    """Drop patch records whose id neither the source nor any result face
    carries."""
    table = get_patch_settings_table(result_obj)
    if not table:
        return
    alive = set(patch_data.analyse(source_obj.data).patches)
    alive.update(_patch_ids_of_faces(result_obj.data))
    kept = {key: value for key, value in table.items()
            if key.lstrip("-").isdigit() and int(key) in alive and int(key) != NO_PATCH}
    if kept != table:
        save_patch_settings_table(result_obj, kept)


def reconcile_patch_tracking(
    context: bpy.types.Context, source_obj: bpy.types.Object
) -> tuple[int, int, int]:
    """Bring `<Source>_Retop`'s bookkeeping back in line with the source.
    Return (faces re-read, corners re-attached, composites restored).

    When the source signature is unchanged, only untracked or unknown face ids
    are checked.
    Otherwise every face, corner id, registry key and composite is checked once.
    Writes mesh attributes and custom properties only.
    """
    result_obj = bpy.data.objects.get(result_object_name_for(source_obj))
    if result_obj is None:
        return 0, 0, 0
    signature = source_signature(source_obj.data)
    if result_obj.get(SOURCE_SIGNATURE_PROP) == signature:
        faces, corners, composites = adopt_untracked_faces(source_obj), 0, 0
    else:
        composites = reanchor_composites(source_obj, result_obj)
        corners = remap_source_ids(context, source_obj, result_obj)
        if corners:
            clear_stray_source_ids(context, source_obj, result_obj)
        faces = adopt_untracked_faces(source_obj, full=True)
        _forget_unknown_settings(source_obj, result_obj)
        result_obj[SOURCE_SIGNATURE_PROP] = signature

    if faces or corners:
        invalidate_boundary_cache()
        invalidate_crack_cache()
    if faces:
        # Sharp edges depend on patch ids.
        apply_result_shading(context, result_obj)
    return faces, corners, composites


# --- putting the bookkeeping back after a hand edit ---
#
# After a hand edit in Edit Mode (`tweak.py`), two things need repair:
# - faces with no valid patch id: `adopt_untracked_faces`;
# - vertices with a copied `retop_source_vid`: `clear_stray_source_ids`.
# A corner nudged by hand keeps its id.

# A vertex farther than this share of the model's diagonal from the corner it
# names loses the id. Generous, so a hand-nudged corner keeps it.
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

    A vertex loses its id when:
    1. the id is out of range for the source mesh;
    2. it is far from the source vertex it names (STRAY_SOURCE_ID_RATIO);
    3. another vertex names the same source vertex and is closer.

    Clearing is always safe: the vertex then welds by proximity.
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

    Called once per trip out of Edit Mode. Writes mesh attributes only.
    """
    result_obj = bpy.data.objects.get(result_object_name_for(source_obj))
    if result_obj is None:
        return 0, 0

    cleared = clear_stray_source_ids(context, source_obj, result_obj)
    # Faces the knife or a manual fill left untagged.
    adopted = adopt_untracked_faces(source_obj)
    # A hand edit can move a patch border: re-derive the creases.
    apply_result_shading(context, result_obj)
    # The cached boundary vertices have moved.
    invalidate_boundary_cache()
    invalidate_crack_cache()
    return adopted, cleared


# --- taking a patch out for a re-edit, reversibly ---
#
# Picking a committed patch removes its faces at once, after snapshotting the
# result mesh. Every exit but a commit restores the snapshot.
# See "Re-editing removes the old patch on pick" in CLAUDE.md.


def _snapshot_result_mesh(result_obj: bpy.types.Object) -> str:
    backup = result_obj.data.copy()
    backup.name = f"{result_obj.name}{SNAPSHOT_NAME_SUFFIX}"
    # No users while it is a snapshot: the fake user keeps it.
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
    """Throw away a snapshot once the re-edit is committed."""
    backup = bpy.data.meshes.get(backup_mesh_name)
    if backup is None:
        return
    backup.use_fake_user = False
    if backup.users == 0:
        bpy.data.meshes.remove(backup)


def purge_stale_snapshots(keep_name: str = "") -> int:
    """Delete snapshot meshes nothing is using any more, and return how many.

    For snapshots left by an interrupted re-edit. Only called when entering an
    object, inside an undo step.
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

    Faces are picked by patch id, plus untagged faces whose centre sits on this
    patch. A looser rule than adoption: this is visible and reversible.
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
        face_id_at = _source_patch_lookup(
            source_obj, _result_to_source(source_obj, result_obj))
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
    # context='FACES' keeps the vertices a neighbour still uses.
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
# The result mesh is filed under the same collection path as its source below
# "Inbox", rebuilt beneath "Retop".
# Collections are IDs: only from `ensure_result_object`, never a callback.


def _collection_parents() -> dict[str, bpy.types.Collection]:
    """child collection name -> parent collection. The scene's master
    collection is not in it.
    """
    parents = {}
    for coll in bpy.data.collections:
        for child in coll.children:
            parents[child.name] = coll
    return parents


def source_collection_path(source_obj: bpy.types.Object) -> list[str]:
    """The source object collection path *below* Inbox, outermost first.

    Empty when the object is not under an Inbox collection.
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
        seen.add(current.name)  # guard against a cycle
        path.append(current.name)
        current = parents.get(current.name)
    path.reverse()

    # The deepest Inbox wins.
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
    """A direct child of `parent` matching `name`, ignoring a .001 suffix.
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

    With `only_if_unplaced`, only moves a result mesh still at the top of
    Retop: never one the user filed.
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
# Every face is shaded smooth. Only edges between two different patches can be
# sharp, never a patch's own interior edges.
# See "Creases are patch borders" in CLAUDE.md.


def _sharp_edge_flags(
    mesh: bpy.types.Mesh, angle_threshold_deg: float
) -> list[bool]:
    """Which edges are creases: patch borders whose faces meet at more than
    the angle. A tangent border stays smooth.
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

    After every commit and on settings changes. Writes mesh attributes only,
    so it is safe from a property callback.
    """
    state = context.scene.plasticity_retop
    mesh = result_obj.data
    if not mesh.polygons:
        return

    smooth = state.result_shade_smooth
    mesh.polygons.foreach_set("use_smooth", [smooth] * len(mesh.polygons))

    count = len(mesh.edges)
    flags = (_sharp_edge_flags(mesh, state.sharp_edge_angle) if smooth
             else [False] * count)
    user = _sharp_user_choices(mesh)
    if smooth:
        # The user's own marks win over the computed ones, in both directions.
        flags = [True if choice == SHARP_USER_ON
                 else False if choice == SHARP_USER_OFF
                 else flag
                 for flag, choice in zip(flags, user)]

    attr = _edge_attribute(mesh, SHARP_EDGE_ATTR, 'BOOLEAN')
    attr.data.foreach_set("value", flags)
    _edge_attribute(mesh, SHARP_WRITTEN_ATTR, 'BOOLEAN').data.foreach_set("value", flags)
    _edge_attribute(mesh, SHARP_USER_ATTR, 'INT').data.foreach_set("value", user)
    mesh.update()


def _edge_attribute(
    mesh: bpy.types.Mesh, name: str, data_type: str
) -> bpy.types.Attribute:
    attr = mesh.attributes.get(name)
    if attr is not None and (attr.domain != 'EDGE' or attr.data_type != data_type):
        mesh.attributes.remove(attr)
        attr = None
    if attr is None:
        attr = mesh.attributes.new(name, data_type, 'EDGE')
    return attr


def _sharp_user_choices(mesh: bpy.types.Mesh) -> list[int]:
    """Per edge, the sharpness the user set by hand, or SHARP_USER_NONE.

    An edge whose `sharp_edge` differs from what the addon last wrote was
    changed by the user, and the choice is kept.
    """
    count = len(mesh.edges)
    user = [SHARP_USER_NONE] * count
    stored = mesh.attributes.get(SHARP_USER_ATTR)
    if stored is not None and stored.domain == 'EDGE' and stored.data_type == 'INT':
        stored.data.foreach_get("value", user)

    sharp_attr = mesh.attributes.get(SHARP_EDGE_ATTR)
    written_attr = mesh.attributes.get(SHARP_WRITTEN_ATTR)
    if written_attr is None or written_attr.domain != 'EDGE':
        # Shaded before the layer existed: no hand edit to detect yet.
        return user
    written = [False] * count
    written_attr.data.foreach_get("value", written)
    # Blender drops `sharp_edge` altogether when leaving Edit Mode with no
    # sharp edge left, so a missing layer means every edge was cleared.
    sharp = [False] * count
    if sharp_attr is not None and sharp_attr.domain == 'EDGE':
        sharp_attr.data.foreach_get("value", sharp)
    for index, (now, before) in enumerate(zip(sharp, written)):
        if now != before:
            user[index] = SHARP_USER_ON if now else SHARP_USER_OFF
    return user


def reset_sharp_overrides(context: bpy.types.Context) -> int:
    """Forget every hand-set sharp edge and re-shade. Returns how many there were."""
    total = 0
    for result_obj in iter_result_objects(context):
        mesh = result_obj.data
        attr = mesh.attributes.get(SHARP_USER_ATTR)
        if attr is not None:
            values = [SHARP_USER_NONE] * len(mesh.edges)
            attr.data.foreach_get("value", values)
            total += sum(1 for value in values if value != SHARP_USER_NONE)
            mesh.attributes.remove(attr)
        written = mesh.attributes.get(SHARP_WRITTEN_ATTR)
        sharp = mesh.attributes.get(SHARP_EDGE_ATTR)
        if written is not None:
            # What is on the mesh becomes the addon's own, so it is not read
            # back as a fresh hand edit.
            values = [False] * len(mesh.edges)
            if sharp is not None:
                sharp.data.foreach_get("value", values)
            written.data.foreach_set("value", values)
        apply_result_shading(context, result_obj)
    return total


def refresh_result_shading(context: bpy.types.Context) -> None:
    """Re-shade every result mesh -- the shading settings are global."""
    for result_obj in iter_result_objects(context):
        apply_result_shading(context, result_obj)


def apply_wireframe_opacity(context: bpy.types.Context) -> None:
    """Push the wireframe opacity setting into every 3D viewport.

    Blender has no per-object wireframe opacity, so this affects every
    wireframe in those viewports.
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
# Never create or free an ID (material, mesh, object, collection) outside an
# undo step: Ctrl+Z would crash. Only on the session's structural moments,
# never from a property callback, a draw handler or a hover.
# The refresh_*_appearance functions only look materials up.
# See "Creating or freeing a datablock outside an undo step" in CLAUDE.md.


def _create_material(name: str) -> bpy.types.Material:
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
        mat.use_nodes = True
    return mat


def _existing_material(name: str) -> bpy.types.Material | None:
    """Look up a material without creating one. Safe from any context."""
    return bpy.data.materials.get(name)


def ensure_materials() -> None:
    """Create the addon's materials up front, from a context allowed to.
    Result meshes use two: the active one and the dimmed others.
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

    # Transparency, under either Blender version's property name.
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
# A Mirror modifier on the result object, never baked geometry: all the
# bookkeeping reads the base mesh, and baked copies would share patch ids.
# The plane is the source object's origin (`mirror_object`).
# See "Symmetry" in CLAUDE.md.
MIRROR_MODIFIER_NAME = "RetopMirror"
MIRROR_AXES = ('X', 'Y', 'Z')


def mirror_target(
    context: bpy.types.Context, obj: bpy.types.Object | None = None
) -> tuple[bpy.types.Object | None, bpy.types.Object | None]:
    """(source, result) the mirror applies to, or (None, None).

    From the running session first, then from `obj` (default: the active
    object), where a result mesh resolves to its source.
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

    Read off the modifier: the axes belong to one object.
    """
    if result_obj is None:
        return (False, False, False)
    mod = result_obj.modifiers.get(MIRROR_MODIFIER_NAME)
    if mod is None:
        return (False, False, False)
    return tuple(bool(a) for a in mod.use_axis)


def apply_mirror_settings(context: bpy.types.Context) -> None:
    """Push the panel's clip/merge settings onto every existing mirror.

    Called from property callbacks: never creates a mirror.
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

    A modifier is not an ID: safe outside an undo step.
    """
    mod = result_obj.modifiers.get(MIRROR_MODIFIER_NAME)
    if not any(axes):
        if mod is not None:
            result_obj.modifiers.remove(mod)
        return (False, False, False)

    state = context.scene.plasticity_retop
    if mod is None:
        mod = result_obj.modifiers.new(MIRROR_MODIFIER_NAME, 'MIRROR')
        # First in the stack, ahead of the cosmetic offset.
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

    The copies are stamped NO_PATCH: with their originals' ids, re-editing one
    patch would delete both halves. `adopt_untracked_faces` later assigns each
    copy to the face it sits on.
    """
    mod = result_obj.modifiers.get(MIRROR_MODIFIER_NAME)
    if mod is None:
        return 0, "Nothing to apply: this retopology has no mirror"
    if context.mode != 'OBJECT':
        return 0, "Leave Edit Mode first"

    mesh = result_obj.data
    # Face centres before the apply tell the originals from the copies, without
    # relying on Blender's face order.
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
    picking = surface_pick_open(state)
    mat = _existing_material(PREVIEW_MATERIAL_NAME)
    if mat is not None:
        _apply_material_appearance(mat, tuple(state.preview_color), state.preview_alpha)
    obj.color = (*state.preview_color, state.preview_alpha)
    _apply_offset_modifier(obj, preview_lift(context))
    # Follows `result_see_through`, like the result.
    obj.show_in_front = state.result_see_through and not picking
    # Wireframe while surfaces are being picked, so it never hides their tint.
    obj.display_type = 'WIRE' if picking else 'TEXTURED'


def surface_pick_open(state: "state_mod.RetopPatchState") -> bool:
    """Whether Shift+click surfaces are being gathered, or about to be."""
    return (getattr(state, "session_phase", "") == 'PATCH'
            and (bool(getattr(state, "surface_selection", ""))
                 or getattr(state, "surface_hover_face_id", -1) != -1))


def ensure_result_object(
    context: bpy.types.Context, source_obj: bpy.types.Object
) -> bpy.types.Object:
    """The result object for `source_obj`, created empty if missing.
    Called on entering a session and by commit.
    """
    result_name = result_object_name_for(source_obj)
    result_obj = bpy.data.objects.get(result_name)
    if result_obj is not None:
        # File it under the mirrored hierarchy if it still sits at the top.
        place_result_object(context, result_obj, source_obj, only_if_unplaced=True)
        return result_obj

    coll = get_or_create_collection(context)
    ensure_materials()
    mesh = bpy.data.meshes.new(result_name)
    result_obj = bpy.data.objects.new(result_name, mesh)
    coll.objects.link(result_obj)
    result_obj.matrix_world = mathutils.Matrix.Identity(4)
    mesh.materials.append(_create_material(RESULT_MATERIAL_NAME))
    # The emphasized look is only on while a session is open on this object.
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
    """An anti-z-fighting offset proportional to the model's size."""
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

    The preview is lifted by the same measure (`preview_lift`).
    """
    state = context.scene.plasticity_retop
    offset = state_mod.to_blender_units(state, state.result_offset)
    if offset <= 0.0:
        offset = _auto_offset_for(source_obj)
    return offset


def preview_lift(context: bpy.types.Context) -> float:
    """How far the *preview* is pushed off the surface.

    The result offset times PREVIEW_LIFT_RATIO: strictly more, so the preview
    draws in front. There is one offset setting, never two.
    """
    return result_lift(context, preview_source_object()) * PREVIEW_LIFT_RATIO


def preview_source_object() -> bpy.types.Object | None:
    """The CAD object the current preview belongs to, stamped at generation.
    """
    obj = bpy.data.objects.get(PREVIEW_OBJ_NAME)
    if obj is None:
        return None
    return bpy.data.objects.get(obj.get(PREVIEW_SOURCE_PROP, ""))


def _apply_result_offset(
    context: bpy.types.Context, result_obj: bpy.types.Object
) -> None:
    """Lift the result mesh off the CAD surface so the two don't z-fight.
    A Displace modifier with show_render off: the stored geometry never moves.
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
    # Get-only: runs from property callbacks.
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

    A resting result mesh never shows its wireframe.
    """
    return bool(state.result_show_wire and emphasized)


def _resting_result_appearance(
    result_obj: bpy.types.Object, color: tuple[float, float, float]
) -> None:
    """The resting look: same colour, opaque, no emphasis."""
    _apply_result_look(result_obj, color, 1.0, RESULT_MATERIAL_NAME, in_front=False, wire=False)


def iter_result_objects(context: bpy.types.Context) -> list[bpy.types.Object]:
    coll = bpy.data.collections.get(COLLECTION_NAME)
    if coll is None:
        return []
    # all_objects: result meshes are nested (place_result_object).
    return [o for o in coll.all_objects if o.name.endswith(RESULT_NAME_SUFFIX)]


def orphan_result_objects(context: bpy.types.Context) -> list[bpy.types.Object]:
    """Retopology meshes whose source object no longer exists under that name.
    Everything resolves through `<Source>_Retop`, so the panel reports them.
    """
    return [o for o in iter_result_objects(context)
            if source_object_for_result(o) is None and len(o.data.polygons) > 0]


def refresh_result_appearance(context: bpy.types.Context) -> None:
    """Apply the right look to every result mesh:

    - the one being worked on: full alpha, in front, wireframe;
    - the others, in a session with Show All Retopo on: dimmed;
    - everything else: resting.

    Called on every session transition and from property callbacks.
    """
    state = context.scene.plasticity_retop
    color = tuple(state.result_color)
    # A setting, never implied by the session.
    see_through = state.result_see_through
    # A hand-edit draws the mesh being edited in front.
    tweaking = (state.session_phase == 'TWEAK' and state.tweak_draw_in_front)

    active_name = ""
    # A hand-edit started in the OBJECT phase names its own source.
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

    # The preview derives its look from the same settings.
    refresh_preview_appearance(context)
    apply_wireframe_opacity(context)


def set_result_highlight(
    context: bpy.types.Context, source_obj: bpy.types.Object, active: bool
) -> None:
    """Session transitions call this. refresh_result_appearance decides.
    """
    refresh_result_appearance(context)


def ensure_preview_object(context: bpy.types.Context) -> bpy.types.Object:
    """The preview object, created once and then reused for the whole session.

    Never create or delete it on hover: only its geometry is rewritten.
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

    # Smooth shading, like the committed result.
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
        # A negative local index (ring.NO_CORNER) is a corner that moved: the
        # vertex keeps NO_SOURCE and welds by proximity.
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
    # Wireframe on. show_in_front and the lift come from
    # refresh_preview_appearance.
    obj.show_wire = True
    obj.show_all_edges = True

    refresh_preview_appearance(context)
    return obj


def has_preview() -> bool:
    """True when there is preview geometry to commit or discard. The object
    itself stays, empty, between patches.
    """
    obj = bpy.data.objects.get(PREVIEW_OBJ_NAME)
    return obj is not None and len(obj.data.polygons) > 0


def clear_preview_object() -> None:
    """Empty the preview without deleting anything.

    Used everywhere inside a session. Never free the object here:
    remove_preview_object does that, once, at session end.
    """
    obj = bpy.data.objects.get(PREVIEW_OBJ_NAME)
    if obj is None:
        return
    obj.data.clear_geometry()
    obj.data.update()


def remove_preview_object() -> None:
    """Drop the preview object for good. Session teardown only."""
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
    """Bake the preview's base geometry (no offset modifier, world space) into
    `source_obj`'s result mesh. Corners weld by source vertex id, boundary
    points by proximity. Returns (result_obj, error_message_or_None).

    `face_id` is stamped on every new face, and faces already carrying it are
    removed first (context='FACES', so neighbours keep their shared vertices).
    """
    preview_obj = bpy.data.objects.get(PREVIEW_OBJ_NAME)
    if preview_obj is None or len(preview_obj.data.polygons) == 0:
        return None, "No preview to commit"

    result_obj = ensure_result_object(context, source_obj)

    src_mesh = preview_obj.data  # base mesh: the offset modifier is not evaluated
    world_matrix = preview_obj.matrix_world.copy()

    bm = bmesh.new()
    bm.from_mesh(result_obj.data)
    uv_layer = bm.loops.layers.uv.get("UVMap") or bm.loops.layers.uv.new("UVMap")
    result_vid_layer = bm.verts.layers.int.get(SOURCE_VID_ATTR) or bm.verts.layers.int.new(SOURCE_VID_ATTR)
    result_boundary_layer = bm.verts.layers.int.get(BOUNDARY_ATTR) or bm.verts.layers.int.new(BOUNDARY_ATTR)
    patch_id_layer = bm.faces.layers.int.get(PATCH_ID_ATTR)
    if patch_id_layer is None:
        # A new int layer defaults to 0, i.e. "patch 0": stamp NO_PATCH on the
        # existing faces.
        patch_id_layer = bm.faces.layers.int.new(PATCH_ID_ATTR)
        for face in bm.faces:
            face[patch_id_layer] = NO_PATCH

    # Drop a previous version of this patch first.
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

    # Weld coincident boundary vertices only, never interior ones. Never run an
    # unscoped remove_doubles (see the module docstring).
    boundary_verts = [v for v in bm.verts if v[result_boundary_layer] == 1]
    retop_state = context.scene.plasticity_retop
    weld_distance = state_mod.to_blender_units(retop_state, retop_state.boundary_weld_distance)
    if len(boundary_verts) > 1 and weld_distance > 0.0:
        bmesh.ops.remove_doubles(bm, verts=boundary_verts, dist=weld_distance)

    bm.to_mesh(result_obj.data)
    result_obj.data.update()
    bm.free()

    # Re-shade after every commit: a new neighbour changes existing borders.
    apply_result_shading(context, result_obj)

    clear_preview_object()

    if skipped:
        return result_obj, None  # committed; a few faces already existed and were skipped
    return result_obj, None


# --- matching a committed neighbour exactly ----------------------------
#
# A match copies the neighbour's committed boundary vertices, not just their
# count. See sidematch.py.


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


# Committed boundary vertices grouped by owning patch, cached per result mesh
# contents and transform.
_boundary_cache: dict[str, tuple[tuple, CommittedMap]] = {}
_BOUNDARY_CACHE_LIMIT = 4


def invalidate_boundary_cache() -> None:
    _boundary_cache.clear()


def committed_boundary_map(source_obj: bpy.types.Object) -> CommittedMap:
    """{patch face id: [boundary vertex, ...]} in the source object's local
    space, over the whole committed result mesh.

    A welded vertex is listed under both patches. Untracked faces are kept
    under `NO_PATCH`.
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

    # Boundary-flagged vertices only. Without any flag, all are eligible.
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

    # Into the source object's local space, where the sides are.
    result = {face_id: [to_source_local @ mesh.vertices[i].co for i in sorted(indices)]
              for face_id, indices in grouped.items()}

    if len(_boundary_cache) >= _BOUNDARY_CACHE_LIMIT:
        _boundary_cache.pop(next(iter(_boundary_cache)))
    _boundary_cache[key] = (fingerprint, result)
    return result


# --- cracked borders -------------------------------------------------------
#
# A crack: a CAD edge with a committed patch on both sides, left open by the
# result mesh along it. What is drawn is the CAD edge itself.
#
# Detection reads the source's B-rep edges, never proximity between result
# vertices. Each open edge is assigned to the one border it lies along
# (`_border_along`), probed at interior points only, never at its ends.
# See "A crack outlives the session that made it" in CLAUDE.md.

# How far an open edge may sit off a CAD edge and still be that border's, as a
# share of the retopology's cell size.
CRACK_NEAR_RATIO = 0.5
# Floor, as a share of the model extent, for flat borders.
CRACK_FLOOR_RATIO = 1e-4
# How much of a shared edge each side's open row must cover to be a crack.
CRACK_COVERAGE = 0.5
# Max samples along the shared edges.
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

    Only collects them: most are normal (the frontier of the work, or an open
    boundary). The caller decides which are cracks.
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

    Both sides committed and both open along the border. A border with one
    side committed is not a crack. Cached: a draw handler reads it.
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
        return []       # nothing open anywhere

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

    # Assign each open edge to the one border of its own patch it lies along.
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


# Where along an open edge it is probed. Never at the ends: several borders
# meet at a junction.
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

    Every probe must be within `near` of a border for it to qualify. The winner
    is the one whose furthest probe is nearest.
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
            return None      # off every border of the patch
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

    Sampled at one uniform spacing, so the query radius is constant.
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

    Dashed in the geometry: the builtin polyline shader has no stipple.
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

    `exclude_face_id` drops the patch being edited. `only_face_ids` keeps just
    those patches. Given neither, every committed patch.
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

    Deduped: a welded vertex is listed under every patch that owns it.
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

    - `tolerance`: how far off the side a vertex may sit and still be on it.
    - `merge`: how close two vertices must be to be the same one. Always pass
      the strict tolerance: deduping at a wide margin merges consecutive
      neighbour vertices. Defaults to `tolerance`.
    - `rivals`: the patch's other sides. A candidate nearer to one of them
      belongs to that side.
    - `partial`: a neighbour covering part of the side is completed at its own
      spacing, along this side's polyline. Never match a count over a partial
      cover.

    `reason` says which check refused.
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
        # One point is nothing to follow.
        return None, "only one committed vertex along this side"

    found.sort(key=lambda item: item[0])

    # Drop duplicates (a corner owned by two patches). At `merge`, never at
    # `tolerance`.
    ordered = [found[0][1]]
    for _at, point in found[1:]:
        if (point - ordered[-1]).length > merge:
            ordered.append(point)
    if len(ordered) < 2:
        return None, "no committed neighbour"

    # A closed side (a cornerless loop) has one endpoint, not two.
    if (side_points[0] - side_points[-1]).length <= tolerance:
        return _close_matched_ring(ordered, side_points, tolerance, partial)

    # Both endpoints must be covered, unless `partial` fills in the rest.
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

    Cut at the first jump in distance larger than the variation so far, never
    at an absolute distance.
    """
    ordered = sorted(found, key=lambda item: item[1])
    for i in range(1, len(ordered)):
        gap = ordered[i][1] - ordered[i - 1][1]
        spread = ordered[i - 1][1] - ordered[0][1]
        if gap > max(2.0 * merge, 2.0 * spread):
            return ordered[:i]
    return ordered


# A closed side is only partly covered when its largest gap is both a large
# share of the loop and far bigger than the median gap. Gaps are arc lengths,
# never chords.
CLOSED_SIDE_MAX_GAP = 0.25
CLOSED_SIDE_GAP_RATIO = 3.0


# Backstop on the segments a partial match's fill may add.
MAX_MATCHED_SEGMENTS = 512


def _run_spacing(ordered: list[mathutils.Vector]) -> float:
    """The neighbour's own vertex spacing along the stretch it covers.

    The median step, never the mean.
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

    Along the side's own polyline, never a chord.
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

    The side's own endpoints are kept exactly: corners weld by identity.
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

    The neighbour must reach all the way round (checked by the largest gap).
    No vertex is required on the side's start, which is arbitrary: the points
    are rotated to lead with the nearest one, and the caller drops that corner
    id.
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

    # Rotate so that point leads, then repeat it to close the loop.
    rotated = ordered[at_start:] + ordered[:at_start]
    rotated.append(rotated[0].copy())
    return rotated, ""


def _complete_closed_side(
    ordered: list[mathutils.Vector],
    side_points: list[mathutils.Vector],
    total: float,
) -> list[mathutils.Vector] | None:
    """A run covering an arc of a closed side, extended round the rest of it.

    A loop has no endpoints to keep: the remainder is one gap, filled at the
    neighbour's spacing.
    """
    spacing = _run_spacing(ordered)
    if spacing <= 0.0:
        return None

    walked = _cumulative(side_points)
    positions = [_at_along(point, side_points) for point in ordered]
    order = sorted(range(len(ordered)), key=lambda i: positions[i])
    ordered = [ordered[i] for i in order]
    positions = [positions[i] for i in order]

    # The gap wraps through the side's start: fill it as one arc.
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

    Strict (float slack) for automatic matching. With `margin`, widened by
    `match_margin`, for a pinned side.
    Pass the patch's longest side as `reference_length`, never this side's own.
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
# Pull the preview and the isolated sources' `<Source>_Retop` meshes into every
# viewport in Local View. `local_view_set` creates no ID: safe outside an undo
# step.


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

    Only the retopology of a source isolated in that viewport. No-op when the
    setting is off.
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
