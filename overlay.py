"""Viewport overlays drawn while a retop session is running: the bottom-right
keybind hints (mirroring how Plasticity shows its own modal keybinds), the
N-gon vertex dots, the side highlight, and the CAD structure of the source
surface (see cad_display).

Two draw handlers: the hints and every dot in 2D (POST_PIXEL), the lines in
the scene (POST_VIEW).

Both are read-only. A draw handler must never create a datablock (it would
crash Ctrl+Z) or walk a mesh: everything is cached elsewhere.
"""
import math
from typing import TYPE_CHECKING

import blf
import bpy
import gpu
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader

# Never import `operators` from here: it imports this module back.
from . import cad_display
from . import constants
from . import keymap
from . import mesh_build
from . import patch_data
from . import sidematch

if TYPE_CHECKING:
    # Annotations only: never widen what a draw handler imports.
    import mathutils

    from . import state as state_mod

_handle: object | None = None
_points_handle: object | None = None

# --- N-gon vertex dots ---
#
# An n-gon is one face, so its boundary vertices are shown as dots.
VERT_COLOR = (1.0, 0.85, 0.2, 1.0)
VERT_OUTLINE_COLOR = (0.05, 0.05, 0.05, 0.9)
VERT_SIZE = 11.0          # fallback when the scene property isn't there yet
VERT_OUTLINE_RATIO = 1.45  # dark disc behind the bright one

# Every dot is screen-space geometry, never a GL point: `gpu.state.point_size_set`
# is ignored with program point size on.
# Keep this a multiple of four, so a disc measures exactly 2*half across.
DOT_SEGMENTS = 12

# --- side reference picker (M in ADJUST) ---
#
# Green: matched. Red: borders a committed patch and is not matched (a crack).
# Grey: nothing to match. Never paint every unmatched side red.
# Hover brightens a side's own colour, never turns it green.
SIDE_MATCHED_COLOR = (0.25, 0.95, 0.45, 0.95)
SIDE_MATCHED_HOVER_COLOR = (0.60, 1.0, 0.75, 1.0)
SIDE_UNMATCHED_COLOR = (0.90, 0.28, 0.24, 0.95)      # borders a committed patch, unmatched
SIDE_UNMATCHED_HOVER_COLOR = (1.0, 0.55, 0.50, 1.0)
SIDE_BLOCKED_COLOR = (0.42, 0.42, 0.45, 0.45)        # nothing across it: normal
SIDE_BLOCKED_HOVER_COLOR = (0.92, 0.94, 0.96, 1.0)
SIDE_WIDTH = 3.0
SIDE_MATCHED_WIDTH = 4.5
SIDE_HOVER_WIDTH = 6.0

# --- the patch a Ctrl+click would copy a density from -----------------------
#
# Amber, off the matched/unmatched scale. An outline, never a fill.
COPY_SOURCE_COLOR = (1.0, 0.72, 0.25, 0.95)
COPY_SOURCE_REFUSED_COLOR = (0.75, 0.45, 0.30, 0.7)
COPY_SOURCE_WIDTH = 3.0
TOOLTIP_COPY = (1.0, 0.82, 0.5, 1.0)


# --- the surfaces gathered for the next patch ------------------------------
#
# Cyan, which nothing else here uses.
SURFACE_SELECTED_COLOR = (0.30, 0.90, 1.0, 0.95)
SURFACE_SELECTED_WIDTH = 3.0
# The surface itself, faintly tinted.
SURFACE_FILL_COLOR = (0.30, 0.90, 1.0, 0.22)
# The surface under the cursor while Shift is held: the same blue, lighter,
# no outline.
SURFACE_CANDIDATE_COLOR = (0.30, 0.90, 1.0, 0.10)


# --- cracked borders -------------------------------------------------------
#
# The same red as an unmatched side. Dashed, since the CAD edge overlay draws a
# solid line along the same curve.
CRACK_ALPHA = 0.85
CRACK_WIDTH = 2.5
CRACK_HOVER_WIDTH = 4.5
# Dash length as a share of the model extent.
CRACK_DASH_RATIO = 0.004
# How near the cursor has to be, in pixels, to name a cracked border.
CRACK_HOVER_PIXELS = 12.0

# --- the tooltip on the hovered side ---
#
# Two lines by the cursor: what the side is doing, and why.
TOOLTIP_BG = (0.10, 0.10, 0.11, 0.90)
TOOLTIP_TEXT = (0.95, 0.95, 0.95, 1.0)
TOOLTIP_DETAIL = (0.72, 0.74, 0.76, 1.0)
TOOLTIP_MATCHED = (0.45, 1.0, 0.60, 1.0)
TOOLTIP_PAD = 8
TOOLTIP_OFFSET = 18   # from the cursor, so the pointer never covers the text

# --- the corner editor's groups ---
#
# Never green or red: those mean welds and cracks everywhere else.
CORNER_GROUP_COLORS = (
    (0.40, 0.66, 1.00, 0.95),   # blue
    (0.80, 0.55, 1.00, 0.95),   # violet
    (0.35, 0.85, 0.90, 0.95),   # teal
    (1.00, 0.78, 0.35, 0.95),   # gold
    (0.95, 0.55, 0.85, 0.95),   # magenta
    (0.70, 0.75, 0.82, 0.95),   # slate
)
CORNER_GROUP_WIDTH = 5.0
# One bubble per side, carrying its group number. Clicking it changes the group.
GROUP_BUBBLE_SIZE = 26.0
GROUP_BUBBLE_OUTLINE = (0.05, 0.05, 0.05, 0.95)
GROUP_BUBBLE_OUTLINE_RATIO = 1.16
GROUP_BUBBLE_TEXT = (0.06, 0.06, 0.08, 1.0)
GROUP_BUBBLE_FONT = 15.0
# The hovered bubble is enlarged, never recoloured: its colour is its group.
GROUP_BUBBLE_HOVER_RATIO = 1.18
GROUP_BUBBLE_HOVER_RING = (1.00, 1.00, 1.00, 1.0)
GROUP_BUBBLE_HOVER_RING_RATIO = 1.34
# A bubble the grouping cannot use (`sidematch.group_problems`).
GROUP_BUBBLE_FAULT_RING = (0.95, 0.25, 0.22, 1.0)
GROUP_BUBBLE_FAULT_RATIO = 1.40
# More segments than a dot, since a bubble is larger. See `_feathered_disc`.
GROUP_BUBBLE_SEGMENTS = 48
GROUP_WARNING_TEXT = (1.0, 0.72, 0.68, 1.0)
GROUP_WARNING_BACKDROP = (0.10, 0.04, 0.04, 0.88)

# --- the vertices a match would take ---
#
# Drawn for the side under the cursor and for every pinned side.
MATCH_DOT_COLOR = (0.35, 1.0, 0.55, 1.0)         # from a committed neighbour
MATCH_DOT_OUTLINE = (0.05, 0.05, 0.05, 0.9)
MATCH_DOT_SIZE = 9.0
MATCH_DOT_OUTLINE_RATIO = 1.5

# --- the CAD structure under the triangles ---
#
# See cad_display.
BREP_DOT_COLOR = (1.0, 1.0, 1.0, 1.0)
BREP_DOT_OUTLINE = (0.05, 0.05, 0.05, 0.9)
BREP_DOT_SIZE = 7.0
FLOW_WIDTH = 1.0
FLOW_ALPHA = 0.55

MARGIN = 18
LINE_HEIGHT = 22
KEY_GAP = 10       # between a key's box and its action text
ITEM_GAP = 26      # between one key/action pair and the next along a row
FONT_SIZE = 13

TEXT_COLOR = (0.92, 0.92, 0.92, 1.0)
KEY_TEXT_COLOR = (1.0, 1.0, 1.0, 1.0)
KEY_BG_COLOR = (0.28, 0.28, 0.30, 0.85)
TYPED_COLOR = (1.0, 0.72, 0.25, 1.0)

# From `constants`: never import `operators` here.
TWO_SPAN_GENERATOR_NAMES = constants.TWO_SPAN_GENERATORS

# The pointer in window coordinates, or None off the viewport. Written by the
# modal: a draw handler has no event.
cursor_window: "tuple[float, float] | None" = None

# Set by the modal when the hovered patch is committed, so the hint reads
# "Re-edit patch".
hover_committed: bool = False


def keybinds_for(
    state: "state_mod.RetopPatchState",
) -> list[list[tuple[str, str]]]:
    """[(key, action), ...] for the session's current phase, bottom line last.

    Keys are read from the live keymap, never written out, so a remap shows.
    """
    phase = state.session_phase

    def key(action_id: str) -> str:
        return keymap.describe(action_id)

    # The x-ray, mirror and CAD edges are offered in every phase.
    see_through = (key("see_through"), "Retopo X-Ray: "
                   + ("on" if getattr(state, "result_see_through", True) else "off"))
    # The mirrored axes live on the modifier: the panel shows them, not this.
    mirror = (key("mirror"), "Mirror X/Y/Z")
    cad_edges = (key("cad_edges"), "Plasticity edges: "
                 + ("on" if getattr(state, "show_cad_edges", False) else "off"))

    if phase == 'OBJECT':
        return [
            ("Click", "Enter object"),
            # Opens the selected object's retopology in Edit Mode.
            (key("hand_edit"), "Hand-edit mesh"),
            cad_edges,
            see_through,
            (key("back"), "End session"),
        ]
    if phase == 'TWEAK':
        # Blender's own keys, listed as a reminder.
        return [
            ("K", "Knife"),
            ("Ctrl+R", "Loop cut"),
            ("J", "Connect vertices"),
            ("G", "Move (snapped, auto-merge)"),
            ("M", "Merge by distance"),
            # Blender's own Tab: the way out of Edit Mode.
            ("Tab", "Back to Retop"),
        ]

    if phase == 'PATCH':
        # Always advertised: a modified click is hard to discover. Carries the
        # count once something is gathered.
        gathered = len(patch_data.parse_surface_selection(
            getattr(state, "surface_selection", "")))
        surfaces = (key("add_surface"),
                    f"{gathered} surfaces — click one for one patch" if gathered
                    else "Add surface to patch")
        return [
            ("Click", "Re-edit patch" if hover_committed else "Pick surface"),
            surfaces,
            (key("hand_edit"), "Hand-edit mesh"),
            # Ctrl+Z is not listed: it is Blender's own key.
            mirror,
            cad_edges,
            see_through,
            (key("back"), "Leave object"),
        ]

    # ADJUST
    # getattr: a draw handler can fire mid-reload.
    commit_label = "Replace patch" if getattr(state, "editing_committed", False) else "Commit"

    # The corner editor owns the click, Enter and Esc while open.
    if getattr(state, "corner_edit", False):
        return [
            (key("corner_toggle"), "Next group number"),
            (key("corner_toggle_back"), "Previous group number"),
            cad_edges,
            (keymap.describe_all("corners_accept")[0], "Keep corner set"),
            (key("corners_cancel"), "Cancel"),
        ]

    # The two span keys are one hint (`_pair_label`).
    span_key = _pair_label("span_more", "span_less")
    if getattr(state, "ngon_mode", False):
        binds = [
            (span_key, "Detail +/- (on a side: its vertices)"),
            (key("ngon_mode"), "Back to grid"),
        ]
    else:
        binds = [
            (span_key, "Span +/-"),
            ("0-9", "Type span"),
        ]
        if state.generator_name in TWO_SPAN_GENERATOR_NAMES:
            binds.append((key("span_axis"), f"U/V direction (now {state.span_axis})"))
        binds.append((key("ngon_mode"), "N-gon (flat faces)"))
    if getattr(state, "editing_committed", False):
        binds.append((key("delete_patch"), "Delete patch"))
    binds.append(cad_edges)
    binds.append(see_through)
    binds.append((key("match_mode"), "Side highlight: "
                  + ("on" if getattr(state, "match_mode", True) else "off")))
    binds.append((key("pin_neighbour"), "Match a side, else " + commit_label.lower()))
    # Not in n-gon mode: there are no spans to copy.
    if not getattr(state, "ngon_mode", False):
        binds.append((key("copy_spans"), "Copy a patch's density"))
    # Same key as the copy: a side opens the editor, a patch is copied.
    binds.append((key("corners_edit"), "Edit corners (on a side)"))
    # Every commit binding, not just the first.
    for label in keymap.describe_all("commit"):
        binds.append((label, commit_label))
    binds.append((key("back"), "Discard"))
    return binds


def _pair_label(up_action: str, down_action: str) -> str:
    """One label for two opposite actions, e.g. "Ctrl+Scroll" for a wheel pair.

    Collapsed only for the two directions of one wheel with the same modifiers;
    otherwise both are spelled out.
    """
    up = keymap.describe(up_action)
    down = keymap.describe(down_action)
    up_key, _, up_rest = up.rpartition("+")
    down_key, _, down_rest = down.rpartition("+")
    if up_key == down_key and {up_rest, down_rest} == {"Wheel Up", "Wheel Down"}:
        return f"{up_key}+Scroll" if up_key else "Scroll"
    return f"{up} / {down}"


def _set_font_size(font_id: int, size: float) -> None:
    # blf.size() dropped its dpi argument in Blender 4.0.
    try:
        blf.size(font_id, size)
    except TypeError:
        blf.size(font_id, size, 72)


def _draw_filled_rect(
    x: float, y: float, width: float, height: float,
    color: tuple[float, float, float, float],
) -> None:
    vertices = (
        (x, y), (x + width, y),
        (x + width, y + height), (x, y + height),
    )
    indices = ((0, 1, 2), (0, 2, 3))
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    batch = batch_for_shader(shader, 'TRIS', {"pos": vertices}, indices=indices)

    gpu.state.blend_set('ALPHA')
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)
    gpu.state.blend_set('NONE')


def _draw_key_background(x: float, y: float, width: float, height: float) -> None:
    _draw_filled_rect(x, y, width, height, KEY_BG_COLOR)


def _draw() -> None:
    context = bpy.context
    state = getattr(context.scene, "plasticity_retop", None)
    if state is None or not state.session_active:
        return

    region = context.region
    if region is None:
        return

    _draw_vertex_dots(context, state, region)
    _draw_group_bubbles(context, state, region)
    _draw_group_warning(context, state, region)
    _draw_match_points(context, state, region)
    _draw_brep_vertices(context, state, region)

    scale = max(0.5, getattr(state, "overlay_scale", 1.0))
    _draw_side_tooltip(state, region, scale)
    # One tooltip at a time: the side picker's first.
    if not _side_tooltip_shown(state):
        # Then the copy source, then the crack report.
        if not _draw_copy_tooltip(context, state, region, scale):
            _draw_crack_tooltip(context, state, region, scale)

    font_id = 0
    _set_font_size(font_id, FONT_SIZE * scale)

    margin = MARGIN * scale
    line_height = LINE_HEIGHT * scale
    key_gap = KEY_GAP * scale
    item_gap = ITEM_GAP * scale
    key_pad = 12 * scale

    binds = keybinds_for(state)
    if not binds:
        return

    # Rows centred at the bottom, wrapped as needed.
    entries = []
    for key, action in binds:
        key_w, _ = blf.dimensions(font_id, key)
        action_w, action_h = blf.dimensions(font_id, action)
        entries.append((key, action, key_w + key_pad, action_w, action_h))

    available = region.width - 2 * margin
    rows = [[]]
    row_width = 0.0
    for entry in entries:
        width = entry[2] + KEY_GAP + entry[3]
        # Never leave a row empty.
        if rows[-1] and row_width + item_gap + width > available:
            rows.append([])
            row_width = 0.0
        row_width += (item_gap if rows[-1] else 0.0) + width
        rows[-1].append(entry)

    # Bottom-up, so the rows read top-down in the order the binds were given.
    for row_index, row in enumerate(reversed(rows)):
        y = margin + row_index * line_height
        total = sum(entry[2] + key_gap + entry[3] for entry in row)
        total += item_gap * (len(row) - 1)
        x = (region.width - total) * 0.5

        for key, action, key_box_w, action_w, action_h in row:
            _draw_key_background(x, y - 4 * scale, key_box_w, action_h + 9 * scale)

            blf.color(font_id, *KEY_TEXT_COLOR)
            blf.position(font_id, x + key_pad * 0.5, y, 0)
            blf.draw(font_id, key)

            blf.color(font_id, *TEXT_COLOR)
            blf.position(font_id, x + key_box_w + key_gap, y, 0)
            blf.draw(font_id, action)

            x += key_box_w + key_gap + action_w + item_gap

    typed = getattr(state, "typed_span", "")
    if typed and state.session_phase == 'ADJUST':
        label = f"Span: {typed}_"
        label_w, _label_h = blf.dimensions(font_id, label)
        blf.color(font_id, *TYPED_COLOR)
        blf.position(font_id, (region.width - label_w) * 0.5,
                     margin + len(rows) * line_height + 6 * scale, 0)
        blf.draw(font_id, label)


def _preview_vertex_coords() -> "list[mathutils.Vector] | None":
    """World-space vertices of the preview object, or None.
    From the base mesh, like commit: the offset is a modifier.
    """
    obj = bpy.data.objects.get(mesh_build.PREVIEW_OBJ_NAME)
    if obj is None or obj.type != 'MESH' or not obj.data.vertices:
        return None
    matrix = obj.matrix_world
    return [matrix @ vertex.co for vertex in obj.data.vertices]


def _draw_side_references(state: "state_mod.RetopPatchState") -> None:
    """The active patch's sides while the side picker is on."""
    references = sidematch.active_sides()
    if not references:
        return

    hovered = getattr(state, "hovered_side", -1)
    shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    viewport = gpu.state.viewport_get()
    shader.bind()
    shader.uniform_float("viewportSize", (viewport[2], viewport[3]))

    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')  # they lie on the surface

    # Hovered side last, so it draws over its neighbours rather than under them.
    for reference in sorted(references, key=lambda ref: ref.index == hovered):
        if len(reference.points) < 2:
            continue
        is_hovered = reference.index == hovered
        color, width = _side_appearance(reference, is_hovered)
        shader.uniform_float("lineWidth", width)
        shader.uniform_float("color", color)
        batch_for_shader(shader, 'LINE_STRIP', {"pos": reference.points}).draw(shader)

    gpu.state.blend_set('NONE')


def _draw_corner_groups(state: "state_mod.RetopPatchState") -> None:
    """The active patch's sides, coloured by the group each belongs to.

    Five sides in four colours is a quad.
    """
    references = sidematch.active_sides()
    if not references:
        return

    numbers = sidematch.group_numbers(references, state)
    by_index = {reference.index: reference for reference in references}

    shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    viewport = gpu.state.viewport_get()
    shader.bind()
    shader.uniform_float("viewportSize", (viewport[2], viewport[3]))
    shader.uniform_float("lineWidth", CORNER_GROUP_WIDTH)

    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')  # they lie on the surface

    # Coloured by group number, so a number used twice shows as one colour twice.
    for index, reference in by_index.items():
        if len(reference.points) < 2:
            continue
        number = numbers.get(index, 1)
        shader.uniform_float(
            "color", CORNER_GROUP_COLORS[(number - 1) % len(CORNER_GROUP_COLORS)])
        batch_for_shader(
            shader, 'LINE_STRIP', {"pos": reference.points}).draw(shader)

    gpu.state.blend_set('NONE')


def _feathered_disc(
    centre: "mathutils.Vector", radius: float,
    colour: tuple[float, float, float, float], feather: float = 1.25,
    segments: int = 48,
) -> "tuple[list[tuple[float, float]], list[tuple[float, float, float, float]], list[tuple[int, int, int]]]":
    """A disc whose last pixel fades to nothing, as (vertices, colours, indices).

    These draws have no multisampling. A second ring at zero alpha, drawn
    through `SMOOTH_COLOR`, antialiases the edge.
    The opaque part still measures exactly `radius`.
    """
    x, y = centre[0], centre[1]
    clear = (colour[0], colour[1], colour[2], 0.0)
    vertices = [(x, y)]
    colours = [colour]
    inner = radius - feather * 0.5
    outer = radius + feather * 0.5
    for step in range(segments):
        angle = 2.0 * math.pi * step / segments
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        vertices.append((x + inner * cos_a, y + inner * sin_a))
        colours.append(colour)
    for step in range(segments):
        angle = 2.0 * math.pi * step / segments
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        vertices.append((x + outer * cos_a, y + outer * sin_a))
        colours.append(clear)

    indices = []
    for step in range(segments):
        nxt = (step + 1) % segments
        indices.append((0, 1 + step, 1 + nxt))
        # The fringe, as a quad per segment split into two triangles.
        indices.append((1 + step, 1 + segments + step, 1 + segments + nxt))
        indices.append((1 + step, 1 + segments + nxt, 1 + nxt))
    return vertices, colours, indices


def _draw_group_bubbles(
    context: bpy.types.Context,
    state: "state_mod.RetopPatchState",
    region: bpy.types.Region,
) -> None:
    """POST_PIXEL: one numbered bubble per side, saying which group it is in.

    Anchored through `sidematch.side_midpoint`, shared with the hit test.
    """
    if state.session_phase != 'ADJUST' or not getattr(state, "corner_edit", False):
        return

    references = sidematch.active_sides()
    if not references:
        return

    rv3d = context.region_data
    if rv3d is None:
        return

    numbers = sidematch.group_numbers(references, state)
    at_fault, _message = sidematch.group_problems(references, numbers)
    hovered = getattr(state, "hovered_bubble", -1)

    scale = max(0.5, getattr(state, "overlay_scale", 1.0))
    half = GROUP_BUBBLE_SIZE * scale / 2.0

    # Gathered first, so the hovered one draws last.
    bubbles = []
    for reference in references:
        anchor = sidematch.side_midpoint(reference.points)
        if anchor is None:
            continue
        screen = view3d_utils.location_3d_to_region_2d(region, rv3d, anchor)
        if screen is None:
            continue
        number = numbers.get(reference.index, 1)
        colour = CORNER_GROUP_COLORS[(number - 1) % len(CORNER_GROUP_COLORS)]
        bubbles.append((screen, number, colour, reference.index == hovered,
                        reference.index in at_fault))
    if not bubbles:
        return
    bubbles.sort(key=lambda entry: entry[3])

    # SMOOTH_COLOR: the fringe carries its own alpha (`_feathered_disc`).
    shader = gpu.shader.from_builtin('SMOOTH_COLOR')
    gpu.state.blend_set('ALPHA')
    shader.bind()

    for screen, _number, colour, is_hovered, is_at_fault in bubbles:
        radius = half * (GROUP_BUBBLE_HOVER_RATIO if is_hovered else 1.0)
        rings = [(GROUP_BUBBLE_OUTLINE, radius * GROUP_BUBBLE_OUTLINE_RATIO),
                 (colour, radius)]
        if is_at_fault:
            rings.insert(0, (GROUP_BUBBLE_FAULT_RING,
                             radius * GROUP_BUBBLE_FAULT_RATIO))
        if is_hovered:
            rings.insert(0, (GROUP_BUBBLE_HOVER_RING,
                             radius * GROUP_BUBBLE_HOVER_RING_RATIO))
        # Largest first: each smaller disc covers the middle of the one under it,
        # leaving a ring.
        for ring_colour, ring_radius in sorted(rings, key=lambda ring: -ring[1]):
            vertices, colours, indices = _feathered_disc(
                screen, ring_radius, ring_colour, segments=GROUP_BUBBLE_SEGMENTS)
            batch_for_shader(shader, 'TRIS', {"pos": vertices, "color": colours},
                             indices=indices).draw(shader)

    gpu.state.blend_set('NONE')

    font_id = 0
    _set_font_size(font_id, GROUP_BUBBLE_FONT * scale)
    blf.color(font_id, *GROUP_BUBBLE_TEXT)
    for screen, number, _colour, _is_hovered, _is_at_fault in bubbles:
        label = str(number)
        width, height = blf.dimensions(font_id, label)
        blf.position(font_id, screen.x - width * 0.5, screen.y - height * 0.5, 0)
        blf.draw(font_id, label)


def _draw_group_warning(
    context: bpy.types.Context,
    state: "state_mod.RetopPatchState",
    region: bpy.types.Region,
) -> None:
    """POST_PIXEL: what is wrong with the grouping, while the editor is open.

    Reads `state.group_warning`, the string the panel shows too.
    """
    if region is None:
        return
    if state.session_phase != 'ADJUST' or not getattr(state, "corner_edit", False):
        return
    message = getattr(state, "group_warning", "")
    if not message:
        return

    scale = max(0.5, getattr(state, "overlay_scale", 1.0))
    font_id = 0
    _set_font_size(font_id, FONT_SIZE * scale)
    width, height = blf.dimensions(font_id, message)

    pad = 10.0 * scale
    x = max(pad, (region.width - width) * 0.5)
    y = region.height - height - pad * 4.0

    _fill_rect(x - pad, y - pad * 0.6, min(width, region.width) + pad * 2,
               height + pad * 1.2, GROUP_WARNING_BACKDROP)
    blf.color(font_id, *GROUP_WARNING_TEXT)
    blf.position(font_id, x, y, 0)
    blf.draw(font_id, message)


def _fill_rect(
    x: float, y: float, width: float, height: float,
    colour: tuple[float, float, float, float],
) -> None:
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    shader.bind()
    shader.uniform_float("color", colour)
    corners = [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]
    batch_for_shader(shader, 'TRIS', {"pos": corners},
                     indices=[(0, 1, 2), (0, 2, 3)]).draw(shader)
    gpu.state.blend_set('NONE')


def _side_appearance(
    reference: "sidematch.SideReference", hovered: bool
) -> "tuple[tuple[float, float, float, float], float]":
    """(colour, line width) for one side of the picker.

    Green: applied. Red: available but not applied. Grey: nothing to match.
    """
    if reference.applied:
        return ((SIDE_MATCHED_HOVER_COLOR if hovered else SIDE_MATCHED_COLOR),
                SIDE_HOVER_WIDTH if hovered else SIDE_MATCHED_WIDTH)
    if reference.available:
        return ((SIDE_UNMATCHED_HOVER_COLOR if hovered else SIDE_UNMATCHED_COLOR),
                SIDE_HOVER_WIDTH if hovered else SIDE_MATCHED_WIDTH)
    return ((SIDE_BLOCKED_HOVER_COLOR if hovered else SIDE_BLOCKED_COLOR),
            SIDE_HOVER_WIDTH if hovered else SIDE_WIDTH)


# Red, like an unmatched side.
TOOLTIP_CRACK = (1.0, 0.55, 0.5, 1.0)


def _draw_side_tooltip(
    state: "state_mod.RetopPatchState", region: bpy.types.Region, scale: float
) -> None:
    """What the side under the cursor is doing, said by the cursor.

    Two lines next to the pointer, from `sidematch.status_of`.
    """
    if state.session_phase != 'ADJUST' or not getattr(state, "match_mode", False):
        return
    if getattr(state, "corner_edit", False):
        return
    if cursor_window is None:
        return

    references = sidematch.active_sides()
    index = getattr(state, "hovered_side", -1)
    if not (0 <= index < len(references)):
        return
    reference = references[index]

    pins = sidematch.side_override_map(state)
    title, detail = sidematch.status_of(reference, pins.get(index))
    _draw_tooltip_box(region, scale, title, detail,
                      TOOLTIP_MATCHED if reference.applied else TOOLTIP_TEXT)


def _draw_tooltip_box(
    region: bpy.types.Region,
    scale: float,
    title: str,
    detail: str,
    title_color: tuple[float, float, float, float],
) -> None:
    """Two lines in a box by the cursor.

    Shared by every tooltip.
    """
    if cursor_window is None:
        return

    font_id = 0
    _set_font_size(font_id, FONT_SIZE * scale)
    title_w, title_h = blf.dimensions(font_id, title)
    detail_w, detail_h = blf.dimensions(font_id, detail)

    pad = TOOLTIP_PAD * scale
    line = max(title_h, detail_h) + 6 * scale
    width = max(title_w, detail_w) + 2 * pad
    height = 2 * line + 2 * pad - 6 * scale

    x = cursor_window[0] - region.x + TOOLTIP_OFFSET * scale
    y = cursor_window[1] - region.y + TOOLTIP_OFFSET * scale
    # Kept inside the region.
    x = min(max(0.0, x), max(0.0, region.width - width))
    y = min(max(0.0, y), max(0.0, region.height - height))

    _draw_filled_rect(x, y, width, height, TOOLTIP_BG)

    blf.color(font_id, *title_color)
    blf.position(font_id, x + pad, y + pad + line - 6 * scale, 0)
    blf.draw(font_id, title)

    blf.color(font_id, *TOOLTIP_DETAIL)
    blf.position(font_id, x + pad, y + pad - 6 * scale, 0)
    blf.draw(font_id, detail)


def _crack_under_cursor(
    context: bpy.types.Context,
    state: "state_mod.RetopPatchState",
    region: bpy.types.Region,
) -> "tuple[int, int] | None":
    """The two patches of the cracked border under the pointer, if any.

    In screen space: a raycast would hit the surface beside the line.
    """
    obj = _crack_source(state)
    if obj is None or cursor_window is None:
        return None
    cracks = mesh_build.crack_edges(obj)
    if not cracks:
        return None

    rv3d = context.region_data
    if rv3d is None:
        return None
    matrix = obj.matrix_world
    mouse = (cursor_window[0] - region.x, cursor_window[1] - region.y)
    limit = CRACK_HOVER_PIXELS * max(0.5, getattr(state, "overlay_scale", 1.0))

    best = None
    best_distance = limit
    for owner, other, polyline in cracks:
        for point in polyline:
            screen = view3d_utils.location_3d_to_region_2d(region, rv3d, matrix @ point)
            if screen is None:
                continue
            distance = math.hypot(screen[0] - mouse[0], screen[1] - mouse[1])
            if distance < best_distance:
                best_distance = distance
                best = (owner, other)
    return best


def _draw_crack_tooltip(
    context: bpy.types.Context,
    state: "state_mod.RetopPatchState",
    region: bpy.types.Region,
    scale: float,
) -> None:
    """Name the two patches of the crack under the cursor, and what to do."""
    if not getattr(state, "show_cracks", True):
        return
    pair = _crack_under_cursor(context, state, region)
    if pair is None:
        return
    owner, other = pair
    _draw_tooltip_box(
        region, scale, "Cracked border",
        f"patches {owner} and {other} are both retopologized but not welded — "
        "re-open either and match this side",
        TOOLTIP_CRACK)


def _draw_copy_tooltip(
    context: bpy.types.Context,
    state: "state_mod.RetopPatchState",
    region: bpy.types.Region,
    scale: float,
) -> bool:
    """What Ctrl+click on the patch under the cursor would take. Returns
    whether anything was drawn.

    """
    face_id = getattr(state, "copy_hover_face_id", -1)
    if state.session_phase != 'ADJUST' or face_id == -1:
        return False

    obj = bpy.data.objects.get(getattr(state, "session_object_name", ""))
    title, detail = mesh_build.copy_source_status(state, obj, face_id)
    if not title:
        return False
    _draw_tooltip_box(region, scale, f"{keymap.describe('copy_spans')}: {title}"
                      if title.startswith("Copy") else title,
                      detail, TOOLTIP_COPY)
    return True


def _side_tooltip_shown(state: "state_mod.RetopPatchState") -> bool:
    """Whether `_draw_side_tooltip` just drew something."""
    if state.session_phase != 'ADJUST' or not getattr(state, "match_mode", False):
        return False
    return 0 <= getattr(state, "hovered_side", -1) < len(sidematch.active_sides())


def _draw_points() -> None:
    """POST_VIEW: every line drawn in the scene. The dots are in `_draw`.
    """
    context = bpy.context
    state = getattr(context.scene, "plasticity_retop", None)
    # getattr throughout: a draw handler can fire mid-reload.
    if state is None or not state.session_active:
        return

    # In every phase, unlike the side highlight.
    _draw_cad_structure(context, state)
    _draw_cracked_borders(context, state)
    _draw_copy_source(context, state)
    _draw_surface_selection(context, state)

    if state.session_phase != 'ADJUST':
        return
    # The corner editor or the side picker, never both.
    if getattr(state, "corner_edit", False):
        _draw_corner_groups(state)
    elif getattr(state, "match_mode", False):
        _draw_side_references(state)


def _match_dot_sets(
    state: "state_mod.RetopPatchState",
) -> "list[tuple[list[mathutils.Vector], tuple[float, float, float, float]]]":
    """[(world points, colour)] the match overlay should draw right now.

    The hovered side shows what a click would take; a pinned side shows what it
    took.
    """
    references = sidematch.active_sides()
    if not references:
        return []

    pins = sidematch.side_override_map(state)
    hovered = getattr(state, "hovered_side", -1)

    sets = []
    for reference in references:
        kind = pins.get(reference.index)
        if kind == sidematch.PIN_NEIGHBOUR and reference.match_world:
            sets.append((reference.match_world, MATCH_DOT_COLOR))
        elif (reference.index == hovered and reference.match_world
              and kind in (None, sidematch.PIN_EXCLUDED)):
            # Preview what a click would take, released sides included.
            sets.append((reference.match_world, MATCH_DOT_COLOR))
    return sets


def _draw_match_points(
    context: bpy.types.Context,
    state: "state_mod.RetopPatchState",
    region: bpy.types.Region,
) -> None:
    """Dots on the vertices the current matches land on."""
    if state.session_phase != 'ADJUST':
        return
    if not getattr(state, "match_mode", False):
        return

    sets = _match_dot_sets(state)
    if not sets:
        return

    rv3d = context.region_data
    if rv3d is None:
        return

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    shader.bind()

    for points, color in sets:
        projected = []
        for point in points:
            screen = view3d_utils.location_3d_to_region_2d(region, rv3d, point)
            if screen is not None:  # None for anything behind the camera
                projected.append(screen)
        if not projected:
            continue
        for half, dot_color in (
                (MATCH_DOT_SIZE * MATCH_DOT_OUTLINE_RATIO * 0.5, MATCH_DOT_OUTLINE),
                (MATCH_DOT_SIZE * 0.5, color)):
            vertices, indices = _discs_around(projected, half)
            shader.uniform_float("color", dot_color)
            batch_for_shader(shader, 'TRIS', {"pos": vertices},
                             indices=indices).draw(shader)

    gpu.state.blend_set('NONE')


def _draw_copy_source(
    context: bpy.types.Context, state: "state_mod.RetopPatchState"
) -> None:
    """POST_VIEW: outline the committed patch a Ctrl+click would copy from.

    Its cached B-rep edges (`cad_display.edge_segments`).
    """
    face_id = getattr(state, "copy_hover_face_id", -1)
    if state.session_phase != 'ADJUST' or face_id == -1:
        return
    obj = bpy.data.objects.get(getattr(state, "session_object_name", ""))
    if obj is None or obj.type != 'MESH':
        return

    segments = cad_display.edge_segments(obj.data, face_id)
    if not segments:
        return

    # Dimmed, never hidden, when the copy would be refused.
    title, _detail = mesh_build.copy_source_status(state, obj, face_id)
    matches = title.startswith("Copy")

    matrix = obj.matrix_world
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    _draw_line_batch([matrix @ point for point in segments],
                     COPY_SOURCE_COLOR if matches else COPY_SOURCE_REFUSED_COLOR,
                     COPY_SOURCE_WIDTH)
    gpu.state.blend_set('NONE')


def _draw_surface_selection(
    context: bpy.types.Context, state: "state_mod.RetopPatchState"
) -> None:
    """POST_VIEW: tint and outline the surfaces Shift+click has gathered.

    From `cad_display`'s cached edges and triangles.
    """
    if state.session_phase != 'PATCH':
        return
    raw = getattr(state, "surface_selection", "")
    candidate = getattr(state, "surface_hover_face_id", -1)
    if not raw and candidate == -1:
        return
    obj = bpy.data.objects.get(getattr(state, "session_object_name", ""))
    if obj is None or obj.type != 'MESH':
        return

    # From the second surface on, the pick is a pending composite: draw that.
    pending = getattr(state, "pending_composite_id", -1)
    face_ids = ([pending] if pending != -1
                else patch_data.parse_surface_selection(raw))

    outline = []
    fill = []
    for face_id in face_ids:
        outline.extend(cad_display.edge_segments(obj.data, face_id))
        fill.extend(cad_display.patch_triangles(obj.data, face_id))

    # The candidate is a raw surface, which a composite may have absorbed.
    candidate_fill = (cad_display.patch_triangles(obj.data, candidate, surfaces=True)
                      if candidate != -1 else [])
    if not outline and not fill and not candidate_fill:
        return

    matrix = obj.matrix_world
    rv3d = context.region_data
    gpu.state.blend_set('ALPHA')
    # Depth-tested, and nudged towards the viewer (`_towards_viewer`).
    gpu.state.depth_test_set('LESS_EQUAL')

    def place(points: "list[mathutils.Vector]") -> "list[mathutils.Vector]":
        return _towards_viewer([matrix @ point for point in points], rv3d)

    # Fills first, outline over them.
    _draw_tri_batch(place(candidate_fill), SURFACE_CANDIDATE_COLOR)
    _draw_tri_batch(place(fill), SURFACE_FILL_COLOR)
    _draw_line_batch(place(outline), SURFACE_SELECTED_COLOR, SURFACE_SELECTED_WIDTH)
    gpu.state.blend_set('NONE')


def _crack_source(
    state: "state_mod.RetopPatchState"
) -> "bpy.types.Object | None":
    """The session's source object, when it has retopology to be cracked."""
    obj = bpy.data.objects.get(getattr(state, "session_object_name", ""))
    if obj is None or obj.type != 'MESH' or not obj.data.get("face_ids"):
        return None
    return obj


def _crack_extent(obj: "bpy.types.Object") -> float:
    """The source object's diagonal, in its own local units."""
    low = [min(corner[axis] for corner in obj.bound_box) for axis in range(3)]
    high = [max(corner[axis] for corner in obj.bound_box) for axis in range(3)]
    return sum((high[axis] - low[axis]) ** 2 for axis in range(3)) ** 0.5


def _draw_cracked_borders(
    context: bpy.types.Context, state: "state_mod.RetopPatchState"
) -> None:
    """POST_VIEW: the borders two committed patches failed to close.

    In every phase, always through the model.
    """
    if not getattr(state, "show_cracks", True):
        return
    obj = _crack_source(state)
    if obj is None:
        return

    dash = _crack_extent(obj) * CRACK_DASH_RATIO
    if dash <= 0.0:
        return
    segments = mesh_build.crack_segments(obj, dash)
    if not segments:
        return

    matrix = obj.matrix_world
    colour = tuple(getattr(state, "crack_color", (1.0, 0.25, 0.2)))
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    _draw_line_batch([matrix @ point for point in segments],
                     colour + (CRACK_ALPHA,), CRACK_WIDTH)
    gpu.state.blend_set('NONE')


def _cad_display_target(
    context: bpy.types.Context, state: "state_mod.RetopPatchState"
) -> "tuple[bpy.types.Object | None, bpy.types.Mesh | None, int | None]":
    """(object, mesh, face id or None) the CAD overlay should describe.

    None for the face id means the whole object, also for ACTIVE with nothing
    picked.
    """
    obj = bpy.data.objects.get(getattr(state, "session_object_name", ""))
    if obj is None or obj.type != 'MESH' or not obj.data.get("face_ids"):
        return None, None, None

    face_id = None
    if getattr(state, "cad_display_scope", 'OBJECT') == 'ACTIVE':
        active = getattr(state, "active_face_id", -1)
        if active != -1:
            face_id = active
    return obj, obj.data, face_id


def _draw_line_batch(
    points: "list[mathutils.Vector]",
    color: tuple[float, float, float, float],
    width: float,
) -> None:
    """One LINES batch for a whole list of point pairs.

    Never one draw call per edge.
    """
    if len(points) < 2:
        return
    shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    viewport = gpu.state.viewport_get()
    shader.bind()
    shader.uniform_float("viewportSize", (viewport[2], viewport[3]))
    shader.uniform_float("lineWidth", width)
    shader.uniform_float("color", color)
    batch_for_shader(shader, 'LINES', {"pos": points}).draw(shader)


def _draw_tri_batch(
    points: "list[mathutils.Vector]",
    color: tuple[float, float, float, float],
) -> None:
    """One TRIS batch for a whole list of triangle corners."""
    if len(points) < 3:
        return
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    shader.bind()
    shader.uniform_float("color", color)
    batch_for_shader(shader, 'TRIS', {"pos": points}).draw(shader)


# How far a depth-tested line is nudged towards the viewer, as a share of its
# distance to the viewpoint. Proportional, never absolute.
DEPTH_NUDGE = 0.002


def _towards_viewer(
    points: list["mathutils.Vector"], rv3d: bpy.types.RegionView3D | None
) -> list["mathutils.Vector"]:
    """Lift world-space points off the surface, towards the viewpoint.

    Without it, depth-tested lines on the surface come out as a stipple.
    One view direction for the whole batch.
    """
    if rv3d is None or not points:
        return points
    inverse = rv3d.view_matrix.inverted()
    towards = inverse.col[2].to_3d().normalized()  # camera +Z: back at the viewer
    origin = inverse.translation
    return [point + towards * ((point - origin).length * DEPTH_NUDGE)
            for point in points]


def _draw_cad_structure(
    context: bpy.types.Context, state: "state_mod.RetopPatchState"
) -> None:
    """POST_VIEW: the Plasticity edges, and the flow of each CAD face."""
    want_edges = getattr(state, "show_cad_edges", False)
    want_flow = getattr(state, "show_surface_flow", False)
    if not (want_edges or want_flow):
        return

    obj, mesh, face_id = _cad_display_target(context, state)
    if mesh is None:
        return

    matrix = obj.matrix_world
    gpu.state.blend_set('ALPHA')

    # Through the model when `cad_display_xray` is on; otherwise depth-tested
    # and nudged towards the viewer.
    xray = getattr(state, "cad_display_xray", True)
    rv3d = context.region_data
    if xray:
        gpu.state.depth_test_set('NONE')
    else:
        gpu.state.depth_test_set('LESS_EQUAL')

    def place(points: list["mathutils.Vector"]) -> list["mathutils.Vector"]:
        world = [matrix @ point for point in points]
        return world if xray else _towards_viewer(world, rv3d)

    # Flow first, edges over it.
    if want_flow:
        colour = tuple(getattr(state, "flow_color", (0.65, 0.45, 1.0)))
        _draw_line_batch(
            place(cad_display.flow_segments(
                mesh, getattr(state, "flow_density", 3),
                getattr(state, "corner_angle_threshold", 135.0), face_id)),
            colour + (FLOW_ALPHA,), FLOW_WIDTH)

    if want_edges:
        colour = tuple(getattr(state, "cad_edge_color", (0.1, 0.9, 1.0)))
        _draw_line_batch(
            place(cad_display.edge_segments(mesh, face_id)),
            colour + (1.0,), getattr(state, "cad_edge_width", 2.0))

    gpu.state.blend_set('NONE')
    gpu.state.depth_test_set('NONE')


def _draw_brep_vertices(
    context: bpy.types.Context,
    state: "state_mod.RetopPatchState",
    region: bpy.types.Region,
) -> None:
    """The junctions between CAD edges, as screen-space dots.

    Only with the edge display on.
    """
    if not getattr(state, "show_cad_edges", False):
        return
    if not getattr(state, "show_brep_vertices", False):
        return

    obj, mesh, face_id = _cad_display_target(context, state)
    if mesh is None:
        return

    rv3d = context.region_data
    if rv3d is None:
        return

    matrix = obj.matrix_world
    projected = []
    for point in cad_display.brep_vertices(mesh, face_id):
        screen = view3d_utils.location_3d_to_region_2d(region, rv3d, matrix @ point)
        if screen is not None:  # None for anything behind the camera
            projected.append(screen)
    if not projected:
        return

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    shader.bind()
    for half, color in ((BREP_DOT_SIZE * 0.75, BREP_DOT_OUTLINE),
                        (BREP_DOT_SIZE * 0.5, BREP_DOT_COLOR)):
        vertices, indices = _discs_around(projected, half)
        shader.uniform_float("color", color)
        batch_for_shader(shader, 'TRIS', {"pos": vertices},
                         indices=indices).draw(shader)
    gpu.state.blend_set('NONE')


def _discs_around(
    centres: "list[mathutils.Vector]", half: float, segments: int = DOT_SEGMENTS
) -> tuple[list[tuple[float, float]], list[tuple[int, int, int]]]:
    """A triangle fan per centre, as (vertices, indices) for a TRIS batch.

    Round, never square. `segments` stays a multiple of four so the disc
    measures exactly 2*half across.
    """
    vertices = []
    indices = []
    for point in centres:
        base = len(vertices)
        x, y = point
        vertices.append((x, y))
        for step in range(segments):
            angle = 2.0 * math.pi * step / segments
            vertices.append((x + half * math.cos(angle), y + half * math.sin(angle)))
        for step in range(segments):
            indices.append((base, base + 1 + step,
                            base + 1 + (step + 1) % segments))
    return vertices, indices


def _draw_vertex_dots(
    context: bpy.types.Context,
    state: "state_mod.RetopPatchState",
    region: bpy.types.Region,
) -> None:
    """A dot on every boundary vertex of the n-gon being adjusted."""
    if state.session_phase != 'ADJUST':
        return
    if not getattr(state, "ngon_mode", False):
        return
    if not getattr(state, "ngon_show_verts", False):
        return

    coords = _preview_vertex_coords()
    if not coords:
        return

    rv3d = context.region_data
    if rv3d is None:
        return

    projected = []
    for point in coords:
        screen = view3d_utils.location_3d_to_region_2d(region, rv3d, point)
        if screen is not None:  # None for anything behind the camera
            projected.append(screen)
    if not projected:
        return

    size = getattr(state, "ngon_vert_size", VERT_SIZE)
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    shader.bind()

    # Dark disc behind the bright one, for contrast.
    for half, color in ((size * VERT_OUTLINE_RATIO * 0.5, VERT_OUTLINE_COLOR),
                        (size * 0.5, VERT_COLOR)):
        vertices, indices = _discs_around(projected, half)
        shader.uniform_float("color", color)
        batch_for_shader(shader, 'TRIS', {"pos": vertices},
                         indices=indices).draw(shader)

    gpu.state.blend_set('NONE')


# ---------------------------------------------------------------------------
#  The patch data debug display
# ---------------------------------------------------------------------------
# Each patch's face id and `[loop_start, loop_count]`, on its surface.
#
# Its own handler, installed once for the life of the addon: it works with no
# session. The callback early-outs on the toggle.
DEBUG_LABEL_COLOR = (1.0, 0.95, 0.55, 1.0)
DEBUG_DETAIL_COLOR = (0.72, 0.80, 0.95, 1.0)
DEBUG_BG = (0.08, 0.08, 0.10, 0.78)
DEBUG_FONT_SIZE = 12
DEBUG_PAD = 5
DEBUG_LINE_GAP = 2

# A backstop for the All scope.
MAX_DEBUG_LABELS = 250

_debug_handle: object | None = None

# (object name, face id) under the cursor, or None. Written by
# `operators.RETOP_OT_patch_hover`. A module global, never a scene property:
# writing one on every mouse move would mark the file modified.
debug_hover: tuple[str, int] | None = None


def _has_face_ids(obj: "bpy.types.Object | None") -> bool:
    return (obj is not None and obj.type == 'MESH'
            and bool(obj.data.get("face_ids")))


def _patch_debug_target(
    context: bpy.types.Context, scope: str
) -> "bpy.types.Object | None":
    """The object whose patch data to write out.

    Under Hover, whatever the cursor found. Otherwise the active object when
    it has face ids, else the session's object.
    """
    if scope == 'HOVER':
        if debug_hover is None:
            return None
        hovered = bpy.data.objects.get(debug_hover[0])
        return hovered if _has_face_ids(hovered) else None

    obj = context.active_object
    if _has_face_ids(obj):
        return obj

    state = getattr(context.scene, "plasticity_retop", None)
    if state is None:
        return None
    session = bpy.data.objects.get(getattr(state, "session_object_name", ""))
    return session if _has_face_ids(session) else None


def _facing_away(
    normal: "mathutils.Vector",
    anchor: "mathutils.Vector",
    rv3d: bpy.types.RegionView3D,
) -> bool:
    """Whether a patch's own normal points away from the viewpoint.

    Labels have no depth test, so back faces are culled by normal.
    """
    inverse = rv3d.view_matrix.inverted()
    if rv3d.is_perspective:
        eye = anchor - inverse.translation
    else:
        # Orthographic: one view axis for every point.
        eye = -inverse.col[2].to_3d()
    return normal.dot(eye) > 0.0


def _draw_patch_debug() -> None:
    context = bpy.context
    state = getattr(context.scene, "plasticity_retop", None)
    if state is None or not getattr(state, "debug_patch_ids", False):
        return

    region = context.region
    rv3d = context.region_data
    if region is None or rv3d is None:
        return

    scope = getattr(state, "debug_patch_scope", 'HOVER')
    obj = _patch_debug_target(context, scope)
    if obj is None:
        return

    labels = cad_display.patch_labels(obj.data)
    if scope == 'HOVER':
        wanted = {debug_hover[1]} if debug_hover is not None else set()
    elif scope == 'SELECTED':
        wanted = cad_display.selected_face_ids(obj.data)
    else:
        wanted = None
    if wanted is not None:
        labels = [label for label in labels if label.face_id in wanted]
    if not labels:
        return

    matrix = obj.matrix_world
    rotation = matrix.to_3x3()
    # Never cull the hovered patch: the cursor is on it.
    cull = getattr(state, "debug_patch_cull", True) and scope != 'HOVER'
    detail = getattr(state, "debug_patch_detail", True)
    scale = max(0.5, getattr(state, "overlay_scale", 1.0))

    font_id = 0
    _set_font_size(font_id, DEBUG_FONT_SIZE * scale)
    pad = DEBUG_PAD * scale
    gap = DEBUG_LINE_GAP * scale

    drawn = 0
    for label in labels:
        if drawn >= MAX_DEBUG_LABELS:
            break
        world = matrix @ label.anchor
        if cull and _facing_away(rotation @ label.normal, world, rv3d):
            continue
        screen = view3d_utils.location_3d_to_region_2d(region, rv3d, world)
        if screen is None:  # behind the camera
            continue

        lines = [(f"#{label.face_id}", DEBUG_LABEL_COLOR)]
        if detail:
            lines.append((f"{label.loop_start}+{label.loop_count}",
                          DEBUG_DETAIL_COLOR))
            lines.append((f"{label.poly_count} poly", DEBUG_DETAIL_COLOR))

        measured = [(text, colour) + blf.dimensions(font_id, text)
                    for text, colour in lines]
        width = max(item[2] for item in measured)
        height = sum(item[3] for item in measured) + gap * (len(measured) - 1)

        x = screen[0] - width * 0.5
        y = screen[1] - height * 0.5
        _draw_filled_rect(x - pad, y - pad, width + 2 * pad, height + 2 * pad,
                          DEBUG_BG)

        # Bottom-up, so the id is on top.
        cursor = y
        for text, colour, text_w, text_h in reversed(measured):
            blf.color(font_id, *colour)
            blf.position(font_id, x + (width - text_w) * 0.5, cursor, 0)
            blf.draw(font_id, text)
            cursor += text_h + gap
        drawn += 1


def enable_patch_debug() -> None:
    """Install the debug handler. Idempotent; left installed for the life of
    the addon."""
    global _debug_handle
    if _debug_handle is None:
        _debug_handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_patch_debug, (), 'WINDOW', 'POST_PIXEL')


def disable_patch_debug() -> None:
    global _debug_handle
    if _debug_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_debug_handle, 'WINDOW')
        _debug_handle = None


# ---------------------------------------------------------------------------
#  The mirror's axis picker
# ---------------------------------------------------------------------------
# Alt+X arms `RETOP_OT_mirror`, which waits for X, Y or Z. This draws the three
# axes under the cursor, lit when mirrored.
# Its own handler: the mirror can be armed with no session running.
MIRROR_AXES = ("X", "Y", "Z")
MIRROR_BOX = 34          # side of one axis square, before UI scale
MIRROR_GAP = 6
MIRROR_PAD = 10
MIRROR_BG = (0.10, 0.10, 0.11, 0.92)
MIRROR_OFF = (0.22, 0.23, 0.25, 1.0)
MIRROR_ON = (0.16, 0.55, 0.95, 1.0)
MIRROR_LABEL_OFF = (0.78, 0.79, 0.81, 1.0)
MIRROR_LABEL_ON = (1.0, 1.0, 1.0, 1.0)
MIRROR_TITLE = (0.88, 0.89, 0.91, 1.0)

_mirror_handle = None
# (window x, window y) of the pointer, and which axes are on. Written by the
# mirror operator.
mirror_cursor: "tuple[int, int] | None" = None
mirror_state: tuple[bool, bool, bool] = (False, False, False)


def _draw_mirror_gizmo() -> None:
    """Three labelled squares under the cursor, one per axis, lit when on."""
    if mirror_cursor is None:
        return
    region = bpy.context.region
    if region is None or region.type != 'WINDOW':
        return

    # getattr: this can run with no session.
    state = getattr(bpy.context.scene, "plasticity_retop", None)
    scale = max(0.5, getattr(state, "overlay_scale", 1.0))
    box = MIRROR_BOX * scale
    gap = MIRROR_GAP * scale
    pad = MIRROR_PAD * scale

    font_id = 0
    _set_font_size(font_id, FONT_SIZE * scale)
    title = "Mirror"
    title_w, title_h = blf.dimensions(font_id, title)

    width = 3 * box + 2 * gap + 2 * pad
    height = box + title_h + 3 * pad

    x = mirror_cursor[0] - region.x - width * 0.5
    y = mirror_cursor[1] - region.y + TOOLTIP_OFFSET * scale
    x = min(max(0.0, x), max(0.0, region.width - width))
    y = min(max(0.0, y), max(0.0, region.height - height))

    _draw_filled_rect(x, y, width, height, MIRROR_BG)

    blf.color(font_id, *MIRROR_TITLE)
    blf.position(font_id, x + pad, y + height - pad - title_h, 0)
    blf.draw(font_id, title)

    for i, axis in enumerate(MIRROR_AXES):
        on = bool(mirror_state[i]) if i < len(mirror_state) else False
        bx = x + pad + i * (box + gap)
        by = y + pad
        _draw_filled_rect(bx, by, box, box, MIRROR_ON if on else MIRROR_OFF)
        label_w, label_h = blf.dimensions(font_id, axis)
        blf.color(font_id, *(MIRROR_LABEL_ON if on else MIRROR_LABEL_OFF))
        blf.position(font_id, bx + (box - label_w) * 0.5,
                     by + (box - label_h) * 0.5, 0)
        blf.draw(font_id, axis)


def enable_mirror_gizmo() -> None:
    global _mirror_handle
    if _mirror_handle is None:
        _mirror_handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_mirror_gizmo, (), 'WINDOW', 'POST_PIXEL')


def disable_mirror_gizmo() -> None:
    """Drop the handler *and* the state it draws from.

    Every exit path of the mirror modal must call this.
    """
    global _mirror_handle, mirror_cursor
    mirror_cursor = None
    if _mirror_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_mirror_handle, 'WINDOW')
        _mirror_handle = None


def enable() -> None:
    global _handle, _points_handle
    if _handle is None:
        _handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw, (), 'WINDOW', 'POST_PIXEL')
    if _points_handle is None:
        _points_handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_points, (), 'WINDOW', 'POST_VIEW')


def disable() -> None:
    global _handle, _points_handle
    disable_mirror_gizmo()
    if _handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handle, 'WINDOW')
        _handle = None
    if _points_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_points_handle, 'WINDOW')
        _points_handle = None
