"""Fallback generator for patches with five or more sides.

The patch is split into one Coons sub-patch per side. Each side is split at its
midpoint, a spoke runs from there to the centre, and the quad between two
consecutive spokes is filled and reprojected like every other generator.
The centre's valence is the number of sides.

A side's segment count is set by the spokes of its two neighbours
(`side_segments`), so several sides can be matched at once.
`spoke_allocation` solves the spokes, and refuses a count it cannot honour.

With nothing matched, every side carries an even count (`even_span`).

See "An N-Side patch is one Coons sub-patch per side" in CLAUDE.md.
"""
import math
from typing import TYPE_CHECKING, Any

import mathutils

from .. import constants
from .. import geometry
from .base import Generator, GenerationResult

if TYPE_CHECKING:
    from mathutils.bvhtree import BVHTree


def plane_basis(
    points: list[mathutils.Vector]
) -> "tuple[mathutils.Vector, mathutils.Vector, mathutils.Vector] | None":
    """(origin, u, v) for the plane a boundary best lies in, or None.

    The normal is the area vector (Newell's method), which tolerates a boundary
    that is not quite planar.
    """
    if len(points) < 3:
        return None
    normal = mathutils.Vector((0.0, 0.0, 0.0))
    for a, b in zip(points, points[1:] + points[:1]):
        normal += a.cross(b)
    if normal.length < 1e-12:
        return None
    normal.normalize()

    # `u` is the edge least parallel to the normal.
    origin = points[0]
    edge = max((b - a for a, b in zip(points, points[1:] + points[:1])),
               key=lambda d: (d - normal * d.dot(normal)).length_squared)
    u = edge - normal * edge.dot(normal)
    if u.length < 1e-12:
        return None
    u.normalize()
    return origin, u, normal.cross(u)


def _flatten(
    points: list[mathutils.Vector],
    basis: "tuple[mathutils.Vector, mathutils.Vector, mathutils.Vector]"
) -> list[tuple[float, float]]:
    origin, u, v = basis
    return [((p - origin).dot(u), (p - origin).dot(v)) for p in points]


def _inside(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    """Plain even-odd ray cast, in the patch's own plane."""
    x, y = point
    hit = False
    j = len(polygon) - 1
    for i, (xi, yi) in enumerate(polygon):
        xj, yj = polygon[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            hit = not hit
        j = i
    return hit


def _distance_to_outline(point: tuple[float, float],
                         polygon: list[tuple[float, float]]) -> float:
    """Shortest distance from `point` to any segment of the closed `polygon`."""
    x, y = point
    best = float("inf")
    for i, (ax, ay) in enumerate(polygon):
        bx, by = polygon[(i + 1) % len(polygon)]
        dx, dy = bx - ax, by - ay
        span = dx * dx + dy * dy
        t = 0.0 if span <= 0.0 else max(
            0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / span))
        best = min(best, math.hypot(x - (ax + t * dx), y - (ay + t * dy)))
    return best


def interior_point(boundary: list[mathutils.Vector]) -> mathutils.Vector:
    """A point inside `boundary`, for the patch's centre.

    Never the mean of the boundary: on a concave patch it lies outside.
    Candidates are the centroids of a triangulation of the boundary, and the
    one furthest from the outline wins.
    Falls back to the mean when the boundary is degenerate.
    See "An N-Side patch" in CLAUDE.md.
    """
    mean = mathutils.Vector((0.0, 0.0, 0.0))
    for point in boundary:
        mean += point
    mean /= float(max(1, len(boundary)))

    basis = plane_basis(boundary)
    if basis is None:
        return mean

    flat = _flatten(boundary, basis)
    try:
        triangles = mathutils.geometry.tessellate_polygon(
            [[mathutils.Vector((x, y, 0.0)) for x, y in flat]])
    except (ValueError, RuntimeError):
        return mean

    best, best_depth = None, -1.0
    for triangle in triangles:
        if len(triangle) != 3:
            continue
        (ax, ay), (bx, by), (cx, cy) = (flat[i] for i in triangle)
        if abs((bx - ax) * (cy - ay) - (cx - ax) * (by - ay)) < 1e-18:
            continue  # a sliver the ear-clip left; its centroid is on an edge
        centroid = ((ax + bx + cx) / 3.0, (ay + by + cy) / 3.0)
        depth = _distance_to_outline(centroid, flat)
        if depth > best_depth:
            best_depth, best = depth, triangle
    if best is None:
        return mean
    return (boundary[best[0]] + boundary[best[1]] + boundary[best[2]]) / 3.0


def even_span(span: int) -> int:
    """The segment count per side an N-Side patch can actually build.

    At least two, and even: the midpoint where the side is split must be one of
    its own vertices.
    """
    span = max(2, int(span))
    return span if span % 2 == 0 else span + 1


def spoke_allocation(
    n: int, default_half: int, wanted: dict[int, int] | None = None,
    order: list[int] | None = None,
) -> tuple[list[int], list[int]]:
    """(spoke counts, side indices whose wanted count could not be honoured).

    `wanted[i]` is the segment count side `i` must end up with.
    Each wanted count fixes a pair of spokes (`side_segments`).
    `order` is the priority to resolve them in. Undecided spokes get
    `default_half`.

    Never approximate: a count nobody asked for is a crack that looks like a
    weld.
    """
    spokes: list[int | None] = [None] * n
    refused = []
    wanted = wanted or {}
    for i in order if order is not None else sorted(wanted):
        total = wanted.get(i)
        if total is None:
            continue
        # This side's count is made of its neighbours' spokes (`side_segments`).
        before, after = (i - 1) % n, (i + 1) % n
        fixed_before, fixed_after = spokes[before], spokes[after]
        if fixed_before is None and fixed_after is None:
            if total < 2:
                refused.append(i)  # a side needs a segment either side of its midpoint
                continue
            spokes[before] = total // 2
            spokes[after] = total - total // 2
        elif fixed_before is None:
            spokes[before] = total - fixed_after
            if spokes[before] < 1:
                spokes[before] = None
                refused.append(i)
        elif fixed_after is None:
            spokes[after] = total - fixed_before
            if spokes[after] < 1:
                spokes[after] = None
                refused.append(i)
        elif fixed_before + fixed_after != total:
            # Both spokes are already fixed and do not add up to what it wants.
            refused.append(i)

    resolved = [default_half if spoke is None else spoke for spoke in spokes]
    return resolved, refused


def side_segments(spokes: list[int]) -> list[int]:
    """How many segments each side ends up with, given the spokes.

    `t[i] = s[i-1] + s[i+1]`: a side is bounded by its neighbours' spokes.
    Its own spoke only says where the side is split.
    """
    n = len(spokes)
    return [spokes[(i - 1) % n] + spokes[(i + 1) % n] for i in range(n)]


class NSideGenerator(Generator):
    name = constants.NSIDE

    def matches(self, num_sides: int) -> bool:
        return num_sides >= 5

    def default_spans(self, sides: list[list[mathutils.Vector]]) -> dict[str, int]:
        seg_lengths = []
        side_lengths = []
        for side in sides:
            total = 0.0
            for a, b in zip(side, side[1:]):
                d = (b - a).length
                total += d
                seg_lengths.append(d)
            side_lengths.append(total)

        target_edge = (sum(seg_lengths) / len(seg_lengths)) if seg_lengths else 1.0
        avg_side = sum(side_lengths) / len(side_lengths)
        return {"span": even_span(round(avg_side / max(target_edge, 1e-6)))}

    def generate(
        self,
        sides: list[list[mathutils.Vector]],
        span_settings: dict[str, Any],
        bvh: "BVHTree | None" = None,
    ) -> GenerationResult:
        if len(sides) < 3:
            raise ValueError("NSideGenerator expects at least 3 sides")

        n = len(sides)
        # One spoke count per side. They decide every side's segment count and
        # where it is split. Uniform when none are given.
        spokes_counts = list(span_settings.get("spokes") or ())
        if len(spokes_counts) != n or any(count < 1 for count in spokes_counts):
            spokes_counts = [even_span(span_settings.get("span", 2)) // 2] * n
        segments_of = side_segments(spokes_counts)

        # Each side resampled to its own count, split after `spokes[i-1]`
        # segments.
        rings = [geometry.resample_polyline_by_arclength(side, count + 1)
                 for side, count in zip(sides, segments_of)]
        splits = [spokes_counts[(i - 1) % n] for i in range(n)]

        # The centre must be inside the boundary (`interior_point`).
        # Read off the original sides, not `rings`, which lost the outline's shape.
        outline = [point for side in sides for point in side[:-1]]
        centre = interior_point(outline)

        def project(point: mathutils.Vector) -> mathutils.Vector:
            if bvh is None:
                return point
            hit = bvh.find_nearest(point)
            if hit and hit[0] is not None:
                return hit[0]
            return point

        # Over a concave notch, the projection can land back on the boundary.
        centre = project(centre)

        # One spoke per side, from its midpoint to the centre, straight then
        # reprojected. Shared by the two sub-patches either side of it.
        spokes = []
        for i, ring in enumerate(rings):
            midpoint = ring[splits[i]]
            length = spokes_counts[i]
            spoke = [midpoint]
            for step in range(1, length):
                spoke.append(project(midpoint.lerp(centre, step / length)))
            spoke.append(centre)
            spokes.append(spoke)

        verts: list[mathutils.Vector] = []
        uvs: list[tuple[float, float]] = []
        index_of: dict[tuple, int] = {}

        def disc_uv(angle: float, radius: float) -> tuple[float, float]:
            return (0.5 + 0.5 * radius * math.cos(angle),
                    0.5 + 0.5 * radius * math.sin(angle))

        def add(key: tuple, point: mathutils.Vector,
                uv: tuple[float, float]) -> int:
            """Vertex index for `key`, creating it once.

            Keyed, never deduplicated by position: the sub-patches share spokes
            and half-sides by construction.
            """
            existing = index_of.get(key)
            if existing is not None:
                return existing
            index_of[key] = len(verts)
            verts.append(point)
            uvs.append(uv)
            return index_of[key]

        def side_angle(side: int, along: float) -> float:
            """Where a point `along` (0..1) side `side` sits round the disc."""
            return 2.0 * math.pi * (side + along) / n

        def boundary_index(side: int, t: int) -> int:
            # A side's last point is the next side's first: one point, one key.
            side, t = ((side + 1) % n, 0) if t == segments_of[side] else (side, t)
            return add(("b", side, t), rings[side][t],
                       disc_uv(side_angle(side, t / segments_of[side]), 1.0))

        def spoke_index(side: int, k: int) -> int:
            # A spoke's ends are a boundary vertex and the centre, keyed as those
            # so both sub-patches share them.
            if k == 0:
                return boundary_index(side, splits[side])
            if k == spokes_counts[side]:
                return add(("centre",), centre, (0.5, 0.5))
            return add(("s", side, k), spokes[side][k],
                       disc_uv(side_angle(side, splits[side] / segments_of[side]),
                               1.0 - k / spokes_counts[side]))

        faces = []
        for i in range(n):
            previous = (i - 1) % n
            # The sub-patch between spoke `previous` (span_u) and spoke `i`
            # (span_v).
            span_u = spokes_counts[previous]
            span_v = spokes_counts[i]
            # Corners: C = this side's first point, M = its midpoint,
            # Z = the centre, P = the previous side's midpoint.
            bottom = rings[i][:span_u + 1]                # C -> M
            right = spokes[i]                             # M -> Z
            top = list(reversed(spokes[previous]))        # Z -> P
            left = rings[previous][splits[previous]:]     # P -> C

            grid = geometry.coons_patch_grid(bottom, right, top, left, span_u, span_v)

            # The sub-patch's four corners on the UV disc, for its interior UVs.
            corner_uvs = (
                disc_uv(side_angle(i, 0.0), 1.0),        # C
                disc_uv(side_angle(i, splits[i] / segments_of[i]), 1.0),  # M
                (0.5, 0.5),                              # centre
                disc_uv(side_angle(previous,
                                   splits[previous] / segments_of[previous]), 1.0),  # P
            )

            local = [[0] * (span_u + 1) for _ in range(span_v + 1)]
            for vi in range(span_v + 1):
                for ui in range(span_u + 1):
                    if vi == 0:
                        local[vi][ui] = boundary_index(i, ui)
                    elif ui == span_u:
                        local[vi][ui] = spoke_index(i, vi)
                    elif vi == span_v:
                        local[vi][ui] = spoke_index(previous, ui)
                    elif ui == 0:
                        local[vi][ui] = boundary_index(
                            previous, segments_of[previous] - vi)
                    else:
                        # Interior point: created here and reprojected.
                        uv_u = ui / span_u
                        uv_v = vi / span_v
                        uv = tuple(
                            corner_uvs[0][axis] * (1 - uv_u) * (1 - uv_v)
                            + corner_uvs[1][axis] * uv_u * (1 - uv_v)
                            + corner_uvs[2][axis] * uv_u * uv_v
                            + corner_uvs[3][axis] * (1 - uv_u) * uv_v
                            for axis in (0, 1))
                        local[vi][ui] = add(("i", i, ui, vi),
                                            project(grid[vi][ui]), uv)

            for vi in range(span_v):
                for ui in range(span_u):
                    faces.append((local[vi][ui], local[vi][ui + 1],
                                  local[vi + 1][ui + 1], local[vi + 1][ui]))

        corner_local_indices = [index_of[("b", i, 0)] for i in range(n)]
        boundary_local_indices = [index_of[("b", i, t)]
                                  for i in range(n) for t in range(segments_of[i])]

        result = GenerationResult(verts, faces, uvs,
                                  corner_local_indices, boundary_local_indices)
        # Per-side counts, for the commit path to register.
        result.side_allocation = segments_of
        return result
