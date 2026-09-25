"""Shared contract for retop generators.

A generator takes a patch's boundary sides (already split by sides.py) and
produces a preview mesh: a flat list of local-space Vector verts and a list
of faces (tuples of indices into that vert list, each face a tri or quad).
"""
from typing import TYPE_CHECKING, Any

import mathutils

if TYPE_CHECKING:
    from mathutils.bvhtree import BVHTree


class GenerationResult:
    __slots__ = ("verts", "faces", "uvs", "corner_local_indices", "boundary_local_indices",
                 "side_allocation")

    def __init__(
        self,
        verts: list[mathutils.Vector],
        faces: list[tuple[int, ...]],
        uvs: list[tuple[float, float]] | None = None,
        corner_local_indices: list[int] | None = None,
        boundary_local_indices: list[int] | None = None,
    ) -> None:
        # Segments per side, for generators the commit path cannot recompute
        # from a span. A Ring gives one list per loop, an N-gon one flat list
        # (all loops concatenated).
        self.side_allocation: tuple[list[int], list[int]] | list[int] | None = None
        self.verts = verts  # list[Vector], local/object space
        self.faces = faces  # list[tuple[int, ...]]
        self.uvs = uvs if uvs is not None else [(0.0, 0.0)] * len(verts)  # list[(u, v)], one per vert
        # Local vert index of each patch corner, in boundary-walk order.
        # Corners are exact source vertices: the only points welded by identity.
        self.corner_local_indices = corner_local_indices or []
        # Local vert index of every boundary point, corners included.
        # These are welded to neighbours by proximity. Interior points never are.
        self.boundary_local_indices = boundary_local_indices or []


class Generator:
    """A generator is identified by `name` (see constants) and selected by
    `matches`. Ring and N-gon are reached directly.
    """

    name: str = "base"

    def matches(self, num_sides: int) -> bool:
        raise NotImplementedError

    def default_spans(self, sides: list[list[mathutils.Vector]]) -> dict[str, int]:
        """sides: list of point-lists (already resolved to Vectors, i.e. the
        output of resolve_side_points), walking the patch boundary in order.
        """
        raise NotImplementedError

    def generate(
        self,
        sides: list[list[mathutils.Vector]],
        span_settings: dict[str, Any],
        bvh: "BVHTree | None" = None,
    ) -> GenerationResult:
        """sides: list of vertex-index lists (one per patch side, walking the
        boundary in order, consecutive sides sharing their corner vertex).
        positions: dict vertex_index -> Vector (object space) is expected to
        already be baked into `sides` by the caller via `resolve_side_points`.
        """
        raise NotImplementedError


def resolve_side_points(
    sides: list[list[int]], positions: dict[int, mathutils.Vector]
) -> list[list[mathutils.Vector]]:
    """Convert vertex-index sides into Vector-point sides.

    Copies every point: `positions` is the shared, read-only table cached by
    `patch_data.analyse`, and a generator may pass its input straight into a
    preview mesh.
    """
    return [[positions[vi].copy() for vi in side] for side in sides]
