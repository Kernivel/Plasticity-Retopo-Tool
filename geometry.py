"""Grid generation (Coons interpolation), surface reprojection and interior
relaxation. Independent of any operator or UI code.
"""
from typing import TYPE_CHECKING

import mathutils

if TYPE_CHECKING:
    # Annotations only: the BVH helpers import bvhtree lazily.
    import bpy
    from mathutils.bvhtree import BVHTree


def resample_polyline_by_arclength(
    points: list[mathutils.Vector], count: int
) -> list[mathutils.Vector]:
    """Resample an ordered polyline to exactly `count` points (count >= 2),
    evenly spaced by arc length, keeping the first and last points fixed.
    """
    if count < 2:
        raise ValueError("count must be >= 2")
    if len(points) == count:
        return list(points)

    seg_lengths = []
    total = 0.0
    for a, b in zip(points, points[1:]):
        d = (b - a).length
        seg_lengths.append(d)
        total += d

    if total < 1e-12:
        return [points[0].copy() for _ in range(count)]

    result = [points[0].copy()]
    target_step = total / (count - 1)
    seg_i = 0
    seg_acc = 0.0

    for k in range(1, count - 1):
        target = target_step * k
        while seg_i < len(seg_lengths) and seg_acc + seg_lengths[seg_i] < target:
            seg_acc += seg_lengths[seg_i]
            seg_i += 1
        if seg_i >= len(seg_lengths):
            result.append(points[-1].copy())
            continue
        remaining = target - seg_acc
        seg_len = seg_lengths[seg_i]
        t = 0.0 if seg_len < 1e-12 else remaining / seg_len
        a = points[seg_i]
        b = points[seg_i + 1]
        result.append(a.lerp(b, t))

    result.append(points[-1].copy())
    return result


def coons_patch_grid(
    side_bottom: list[mathutils.Vector],
    side_right: list[mathutils.Vector],
    side_top: list[mathutils.Vector],
    side_left: list[mathutils.Vector],
    span_u: int,
    span_v: int,
) -> list[list[mathutils.Vector]]:
    """Build a (span_u+1) x (span_v+1) grid of 3D points spanning a 4-sided
    patch, via bilinearly-blended Coons interpolation.

    Sides must be given walking around the patch boundary in order, already
    resampled:
      side_bottom : span_u+1 points, u=0..1 at v=0   (P00 -> P10)
      side_right  : span_v+1 points, v=0..1 at u=1   (P10 -> P11)
      side_top    : span_u+1 points, u=1..0 at v=1   (P11 -> P01), i.e. reversed u order
      side_left   : span_v+1 points, v=1..0 at u=0   (P01 -> P00), i.e. reversed v order

    Returns a list of rows (v index) of lists of points (u index):
    grid[v][u], both 0-indexed up to span_u / span_v.
    """
    nu = span_u + 1
    nv = span_v + 1

    c0 = side_bottom                          # C(u, 0)
    c1 = list(reversed(side_top))             # C(u, 1)
    d0 = list(reversed(side_left))            # C(0, v)
    d1 = side_right                           # C(1, v)

    p00 = c0[0]
    p10 = c0[-1]
    p01 = c1[0]
    p11 = c1[-1]

    grid = [[None] * nu for _ in range(nv)]

    for vi in range(nv):
        v = vi / span_v
        for ui in range(nu):
            u = ui / span_u

            ruled_u = c0[ui].lerp(c1[ui], v)
            ruled_v = d0[vi].lerp(d1[vi], u)
            bilinear_corners = (
                p00 * (1 - u) * (1 - v)
                + p10 * u * (1 - v)
                + p01 * (1 - u) * v
                + p11 * u * v
            )

            point = ruled_u + ruled_v - bilinear_corners
            grid[vi][ui] = point

    return grid


def fan_collapsed_grid(
    side_ab: list[mathutils.Vector],
    side_bc: list[mathutils.Vector],
    side_ca: list[mathutils.Vector],
) -> list[list[mathutils.Vector]]:
    """Grid for a 3-sided patch A-B-C: a Coons quad with one corner repeated,
    (P00, P10, P11, P01) = (A, B, C, C). The last row collapses to apex C.

    side_ab : span_u+1 points, A -> B (the Coons bottom side)
    side_bc : span_v+1 points, B -> C (the Coons right side)
    side_ca : span_v+1 points, C -> A. Already in the order coons_patch_grid
              expects for its left side: do NOT reverse it.

    Returns grid[v][u] like coons_patch_grid.
    """
    if len(side_bc) != len(side_ca):
        raise ValueError("side_bc and side_ca must have the same point count (shared span)")

    span_u = len(side_ab) - 1
    span_v = len(side_bc) - 1
    apex = side_bc[-1]

    side_top = [apex] * (span_u + 1)  # P11 -> P01, both C: degenerate
    side_left = side_ca               # P01 (C) -> P00 (A), already in this order

    return coons_patch_grid(side_ab, side_bc, side_top, side_left, span_u, span_v)


def build_bvh_with_polygon_map(mesh: "bpy.types.Mesh") -> tuple["BVHTree", list[int]]:
    """BVH over every polygon of `mesh` (local object space), plus a list
    mapping each BVH triangle index back to the polygon it came from.

    A hit reports a triangle index; the map turns it back into a polygon.
    """
    from mathutils.bvhtree import BVHTree

    verts = [v.co.copy() for v in mesh.vertices]
    tris = []
    tri_poly = []

    for poly in mesh.polygons:
        loop_verts = list(poly.vertices)
        for i in range(1, len(loop_verts) - 1):
            tris.append((loop_verts[0], loop_verts[i], loop_verts[i + 1]))
            tri_poly.append(poly.index)

    return BVHTree.FromPolygons(verts, tris), tri_poly


def build_bvh_for_polygons(
    mesh: "bpy.types.Mesh", poly_indices: "list[int]"
) -> "BVHTree":
    """Build a BVHTree restricted to the given polygons of `mesh`, in the
    mesh's local object space.
    """
    from mathutils.bvhtree import BVHTree

    vert_remap = {}
    verts = []
    tris = []

    for poly_idx in poly_indices:
        poly = mesh.polygons[poly_idx]
        loop_verts = list(poly.vertices)
        local_ids = []
        for vi in loop_verts:
            if vi not in vert_remap:
                vert_remap[vi] = len(verts)
                verts.append(mesh.vertices[vi].co.copy())
            local_ids.append(vert_remap[vi])
        # fan-triangulate (import is already triangulated, so this is normally a no-op)
        for i in range(1, len(local_ids) - 1):
            tris.append((local_ids[0], local_ids[i], local_ids[i + 1]))

    return BVHTree.FromPolygons(verts, tris)


# How far one relaxation pass moves a vertex towards the average of its
# neighbours. Under-relaxed on purpose: a full step oscillates.
RELAX_STRENGTH = 0.5


def cell_quality(
    points: "list[mathutils.Vector]",
    reference_normal: "mathutils.Vector | None" = None,
) -> float:
    """How square a face is, in [0, 1]: 1 for a square or an equilateral
    triangle, 0 for a degenerate or turned-over one.

    The sine of the smallest corner angle times the shortest edge over the
    longest. Neither term alone will do: a 1x100 rectangle has perfect angles,
    a thin rhombus has equal edges.

    With `reference_normal`, a face turned more than 90 degrees from it scores 0.
    """
    count = len(points)
    if count < 3:
        return 0.0

    if reference_normal is not None:
        normal = mathutils.geometry.normal(points)
        if normal.length_squared == 0.0 or normal.dot(reference_normal) <= 0.0:
            return 0.0

    worst_angle = 1.0
    shortest = None
    longest = 0.0
    for i in range(count):
        before = points[i - 1] - points[i]
        after = points[(i + 1) % count] - points[i]
        if before.length_squared == 0.0 or after.length_squared == 0.0:
            return 0.0
        # Sine of the corner angle: 0 at both 0 and 180 degrees.
        worst_angle = min(
            worst_angle, before.normalized().cross(after.normalized()).length)
        length = after.length
        shortest = length if shortest is None else min(shortest, length)
        longest = max(longest, length)

    if not longest:
        return 0.0
    return worst_angle * (shortest / longest)


# The cell quality relaxation works up to, and stops at. A 2:1 cell scores 0.5.
# Only vertices touching a cell below it are tried, which keeps the pass cheap
# enough for every hover.
RELAX_QUALITY_TARGET = 0.5


def relax_interior_points(
    verts: "list[mathutils.Vector]",
    faces: "list[tuple[int, ...]]",
    pinned: "set[int] | list[int]",
    bvh: "BVHTree",
    iterations: int,
    strength: float = RELAX_STRENGTH,
    target: float = RELAX_QUALITY_TARGET,
) -> int:
    """Laplacian relaxation of a generated patch's interior, in place.

    Returns how many vertices ended up somewhere other than where they started.

    Returns how many vertices ended up somewhere other than where they started.

    `pinned` is the generator's own `boundary_local_indices`: the boundary never
    moves, so every weld to a neighbour holds.

    - A move is kept only if it improves the worst cell it touches.
    - Uniform weights, never cotangent: those go negative and can turn a cell
      over.
    - Jacobi: every vertex reads the previous pass, so the result does not
      depend on vertex order.
    - Reprojected every pass. Does nothing without a BVH.
    - A step whose projection is longer than the step itself is refused: the
      point left the patch.

    See "The interior is relaxed after the grid is built" in CLAUDE.md.
    """
    if iterations <= 0 or bvh is None or not verts or not faces:
        return 0

    fixed = set(pinned)
    neighbours: list[set[int]] = [set() for _ in verts]
    incident: list[list[int]] = [[] for _ in verts]
    for index, face in enumerate(faces):
        count = len(face)
        for i in range(count):
            a = face[i]
            b = face[(i + 1) % count]
            if a != b:  # a fan's apex names itself twice
                neighbours[a].add(b)
                neighbours[b].add(a)
            if index not in incident[a]:
                incident[a].append(index)

    free = [i for i in range(len(verts)) if i not in fixed and neighbours[i]]
    if not free:
        return 0

    start = [verts[i].copy() for i in free]

    for _ in range(iterations):
        # One score per face per pass, read by every candidate below.
        quality = [cell_quality([verts[vi] for vi in face]) for face in faces]
        active = [i for i in free
                  if any(quality[f] < target for f in incident[i])]
        if not active:
            break  # nothing below the target: this patch is not the problem

        updated = {}
        for i in active:
            ring = neighbours[i]
            target_point = mathutils.Vector((0.0, 0.0, 0.0))
            for j in ring:
                target_point += verts[j]
            target_point /= len(ring)

            stepped = verts[i].lerp(target_point, strength)
            step = (stepped - verts[i]).length
            if step <= 1e-12:
                continue  # already at the average of its neighbours
            hit = bvh.find_nearest(stepped)
            if hit is None or hit[0] is None:
                continue  # nothing to project onto: leave it where it is
            candidate = hit[0]
            if (candidate - stepped).length > step:
                continue  # the step left the patch

            if _improves(verts, faces, incident[i], i, candidate,
                         min(quality[f] for f in incident[i])):
                updated[i] = candidate

        if not updated:
            break  # converged: no step left that would improve anything
        for index, point in updated.items():
            verts[index] = point

    return sum(1 for i, was in zip(free, start)
               if (verts[i] - was).length > 1e-9)


def _improves(
    verts: "list[mathutils.Vector]",
    faces: "list[tuple[int, ...]]",
    incident: "list[int]",
    moved: int,
    to: "mathutils.Vector",
    before: "float | None" = None,
) -> bool:
    """Whether putting vertex `moved` at `to` raises the quality of the worst
    face it belongs to. `before` is that worst quality, if already known.

    Each face is scored against its own normal, so turning it over never counts
    as an improvement.
    """
    if before is None:
        before = min(cell_quality([verts[vi] for vi in faces[f]])
                     for f in incident)
    for face_index in incident:
        face = faces[face_index]
        reference = mathutils.geometry.normal([verts[vi] for vi in face])
        after = cell_quality(
            [to if vi == moved else verts[vi] for vi in face], reference)
        if after <= before:
            return False
    return True
