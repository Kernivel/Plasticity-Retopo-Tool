"""Viewport overlays drawn while a retop session is running: the bottom-right
keybind hints (mirroring how Plasticity shows its own modal keybinds), the
N-gon vertex dots, the side highlight, and the CAD structure of the source
surface (see cad_display).

Two separate draw handlers, because they draw in different spaces: the hints
and every kind of dot are 2D (POST_PIXEL), the lines are in the scene
(POST_VIEW).

Both are read-only by construction. A draw handler that created a datablock
would crash the next Ctrl+Z -- see the note in mesh_build. Everything expensive
they show is computed and cached elsewhere for the same reason: a redraw is not
a place to walk a mesh.
"""
import math
from typing import TYPE_CHECKING

import blf
import bpy
import gpu
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader

# Safe to import: all three are loaded before this module (see __init__.py) and
# none imports it back. operators is the one that can't be reached from here.
from . import cad_display
from . import constants
from . import keymap
from . import mesh_build
from . import sidematch

if TYPE_CHECKING:
    # Annotations only: the overlay runs inside a draw handler and must never
    # widen what it pulls in at import time.
    import mathutils

    from . import state as state_mod

_handle: object | None = None
_points_handle: object | None = None

# --- N-gon vertex dots ---
#
# A span grid shows its own topology: the preview's quads *are* the spans. An
# n-gon is one face, so its boundary vertices are invisible -- and they are the
# only thing there is to judge in that mode (is the chamfer picked up? is the
# curve dense enough?). Hence dots, and only in n-gon mode.
VERT_COLOR = (1.0, 0.85, 0.2, 1.0)
VERT_OUTLINE_COLOR = (0.05, 0.05, 0.05, 0.9)
VERT_SIZE = 11.0          # fallback when the scene property isn't there yet
VERT_OUTLINE_RATIO = 1.45  # dark disc behind the bright one

# Drawn as screen-space geometry rather than GL points. `gpu.state.point_size_set`
# is a no-op whenever the backend runs with program point size enabled -- the
# shader has to write gl_PointSize then, and the builtin UNIFORM_COLOR one does
# not -- which came out as 1px dots that ignored the size setting entirely. A
# fan of triangles per dot always honours the pixels asked for.
#
# A fan rather than the two triangles it started as: a square dot reads as a
# handle you can grab, which none of these are -- they mark where a vertex is.
# Twelve segments is where a dot this size stops looking like a polygon, and it
# is divisible by four so the disc still measures exactly 2*half across, which
# is what keeps the size setting checkable.
DOT_SEGMENTS = 12

# --- side reference picker (M in ADJUST) ---
#
# Matching is binary -- a side reproduces the neighbour across it or it does
# not -- so the colours answer that one question, and the states are what it
# can return.
#
# Green is a side being matched. **Red is the failure**, and it is deliberately
# narrow: a side bordering a committed neighbour that is *not* reproducing it,
# because it lost a span collision, because the span was typed over since, or
# because it was released by hand. That is where a crack will actually be, and
# it used to draw the same grey as a side with nothing across it -- the one
# state worth acting on, hidden among the ones that are simply normal.
# Painting *every* unmatched side red was the alternative, and it says the
# wrong thing: on the first patch of a model nothing is committed anywhere, so
# the whole patch would come up red with nothing wrong at all.
#
# Grey is that normal: no committed neighbour along this side, nothing to
# match, nothing to fix.
#
# Hover brightens a side's *own* colour instead of replacing it: a single hover
# colour hid the one thing worth knowing before clicking -- whether this side
# can be matched at all -- and turned a refusal into a surprise. It is never
# green, or hovering an unmatched side would look like matching it.
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
# Amber, which the side colours gave up when matching became binary: it is not
# a state of *this* patch at all, so it must not land anywhere on the
# matched/unmatched scale. Its own outline rather than a fill -- the patch is
# finished geometry, and tinting it would compete with the preview being
# adjusted, which is the thing actually being worked on.
COPY_SOURCE_COLOR = (1.0, 0.72, 0.25, 0.95)
COPY_SOURCE_REFUSED_COLOR = (0.75, 0.45, 0.30, 0.7)
COPY_SOURCE_WIDTH = 3.0
TOOLTIP_COPY = (1.0, 0.82, 0.5, 1.0)


# --- cracked borders -------------------------------------------------------
#
# Same red as an unmatched side, because it is the same failure seen later: the
# side picker says it while the patch is open, this says it once the patch has
# been committed and the session has moved on. Dashed, and that is the whole
# reason it is legible -- the CAD edge overlay draws a solid line along the
# very same curve, so a solid red one would read as another piece of structure
# rather than as a warning. Dimmer than a side, too: it is a standing report on
# work already done, not the thing under the cursor.
CRACK_ALPHA = 0.85
CRACK_WIDTH = 2.5
CRACK_HOVER_WIDTH = 4.5
# Dash length in *world* units, as a share of the model extent: a dash pattern
# in pixels would need the view matrix on every redraw, and one in absolute
# units is either invisible on a small part or a solid line on a large one.
CRACK_DASH_RATIO = 0.004
# How near the cursor has to be, in pixels, to name a cracked border.
CRACK_HOVER_PIXELS = 12.0

# --- the tooltip on the hovered side ---
#
# Colour alone cannot say *why* a side is grey, and the panel is the wrong
# place to read it: the pointer is already on the side, in the viewport, about
# to click. Two lines by the cursor -- what the side is doing, and why.
TOOLTIP_BG = (0.10, 0.10, 0.11, 0.90)
TOOLTIP_TEXT = (0.95, 0.95, 0.95, 1.0)
TOOLTIP_DETAIL = (0.72, 0.74, 0.76, 1.0)
TOOLTIP_MATCHED = (0.45, 1.0, 0.60, 1.0)
TOOLTIP_PAD = 8
TOOLTIP_OFFSET = 18   # from the cursor, so the pointer never covers the text

# --- the vertices a match would actually take ---
#
# Knowing a side *can* be matched is only half of it. Which vertices it would
# land on is the half that shows a match going to the wrong neighbour, or
# stopping short, or picking up a run that wanders off the shared edge -- and
# none of that is visible from a coloured line lying on the boundary. Drawn for
# the side under the cursor and for every side already pinned.
# The corner editor's own vocabulary, deliberately off the matched/unmatched
# scale: while it is open the side colours and the copy outline are suppressed,
# so these mean groups and nothing else. No green and no red among them -- the
# eye reads those two as "welds" and "cracks" everywhere else in this overlay,
# and a group is neither.
CORNER_GROUP_COLORS = (
    (0.40, 0.66, 1.00, 0.95),   # blue
    (0.80, 0.55, 1.00, 0.95),   # violet
    (0.35, 0.85, 0.90, 0.95),   # teal
    (1.00, 0.78, 0.35, 0.95),   # gold
    (0.95, 0.55, 0.85, 0.95),   # magenta
    (0.70, 0.75, 0.82, 0.95),   # slate
)
CORNER_GROUP_WIDTH = 5.0
# A corner still acting as a side boundary, one that has been turned off, and
# whichever is under the cursor. The off one is drawn small and dark rather
# than not at all: it is still a vertex of the mesh, and a corner that vanished
# would read as geometry lost rather than as a boundary given up.
# One bubble per side, carrying its group number -- what you click to change
# which group the side is in. Screen-space geometry like every other dot here:
# `gpu.state.point_size_set` is ignored whenever program point size is on,
# which is what made these 1px once before.
GROUP_BUBBLE_SIZE = 26.0
GROUP_BUBBLE_OUTLINE = (0.05, 0.05, 0.05, 0.95)
GROUP_BUBBLE_OUTLINE_RATIO = 1.16
GROUP_BUBBLE_TEXT = (0.06, 0.06, 0.08, 1.0)
GROUP_BUBBLE_FONT = 15.0
# The hovered bubble is lifted rather than recoloured: its colour *is* its
# group, and replacing it would hide the one thing the bubble is there to say.
GROUP_BUBBLE_HOVER_RATIO = 1.18
GROUP_BUBBLE_HOVER_RING = (1.00, 1.00, 1.00, 1.0)
GROUP_BUBBLE_HOVER_RING_RATIO = 1.34

MATCH_DOT_COLOR = (0.35, 1.0, 0.55, 1.0)         # from a committed neighbour
MATCH_DOT_OUTLINE = (0.05, 0.05, 0.05, 0.9)
MATCH_DOT_SIZE = 9.0
MATCH_DOT_OUTLINE_RATIO = 1.5

# --- the CAD structure under the triangles ---
#
# See cad_display: the Plasticity edges are recovered exactly, the surface flow
# is derived from each face's boundary. Both are drawn without depth testing,
# for the same reason the side highlight is -- they lie *on* the surface, so
# testing them against it is a coin flip per pixel.
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

# From `constants`, not from `operators`: the draw handler must never pull in
# the operators module, which imports this one back.
TWO_SPAN_GENERATOR_NAMES = constants.TWO_SPAN_GENERATORS

# The digits being typed are echoed from `state.typed_span`, not a module
# global: the keys that clear it (U/V, N-gon, the span wheel) are real
# operators now, and an operator cannot reach the running modal's attributes.

# Where the pointer was when the modal last looked, in window coordinates, or
# None when it is not over the viewport. The tooltip needs it and a draw
# handler has no event to read it from -- same arrangement as
# `hover_committed` below.
cursor_window: "tuple[float, float] | None" = None

# Set by the session modal when the patch under the cursor has already been
# committed, so the hint reads "Re-edit patch" -- clicking it reopens it with
# the spans it was built with instead of starting a fresh one.
hover_committed: bool = False


def keybinds_for(
    state: "state_mod.RetopPatchState",
) -> list[list[tuple[str, str]]]:
    """[(key, action), ...] for the session's current phase, bottom line last.

    Every key here is looked up in the keymap table rather than written out, so
    a remapped binding changes the hint with it. A hint line that says `E` when
    the key is now `Ctrl+E` is worse than no hint line at all -- it is the one
    place a user checks before deciding the feature is broken.
    """
    phase = state.session_phase

    def key(action_id: str) -> str:
        # Read off the live KeyMapItem, so a remapped key changes the hint with
        # it. A hint that says `E` when the key is now `Ctrl+E` is worse than
        # no hint at all -- it is the one place a user checks before deciding
        # the feature is broken.
        return keymap.describe(action_id)

    # The retopo x-ray is offered in every phase: it is the binding that
    # answers "is the retopology where I think it is", and that comes up at any
    # point. It sits on `V`, off the `X` the mirror and the patch delete share:
    # Shift+X came out as `object.delete`'s confirmation popup in practice.
    see_through = (key("see_through"), "Retopo X-Ray: "
                   + ("on" if getattr(state, "result_see_through", True) else "off"))
    # Which axes are currently mirrored lives on the modifier, not in state,
    # and a draw handler has no business reaching for an object to find out --
    # so the hint names the key and the panel names the state.
    mirror = (key("mirror"), "Mirror X/Y/Z")
    # Same reasoning as the x-ray: the CAD structure is read in every phase,
    # and most of all while deciding which surface to pick next.
    cad_edges = (key("cad_edges"), "Plasticity edges: "
                 + ("on" if getattr(state, "show_cad_edges", False) else "off"))

    if phase == 'OBJECT':
        return [
            ("Click", "Enter object"),
            # Tab is the session's here too: it opens the *selected* object's
            # retopology in Edit Mode. Advertised even though it can refuse
            # (nothing selected has any retopology yet), because the modal
            # consumes it either way -- letting it through would put the CAD
            # object into Edit Mode, which is the one thing nobody wants.
            (key("hand_edit"), "Hand-edit mesh"),
            cad_edges,
            see_through,
            (key("back"), "End session"),
        ]
    if phase == 'TWEAK':
        # Blender's keys, not ours: the session is only holding the door open.
        # Listed anyway because the whole point of the round trip is that you
        # do not have to remember which mode you are in to fix a seam.
        return [
            ("K", "Knife"),
            ("Ctrl+R", "Loop cut"),
            ("J", "Connect vertices"),
            ("G", "Move (snapped, auto-merge)"),
            ("M", "Merge by distance"),
            # Blender's own Tab, not the table's: the modal consumes it in this
            # phase whatever hand_edit is bound to, because getting *out* of a
            # mode Blender put you in has to be the key Blender uses.
            ("Tab", "Back to Retop"),
        ]

    if phase == 'PATCH':
        return [
            ("Click", "Re-edit patch" if hover_committed else "Pick surface"),
            (key("hand_edit"), "Hand-edit mesh"),
            # Ctrl+Z is deliberately *not* listed. It is Blender's own key and
            # reaching for it is automatic; what the session does is make one
            # step mean one committed patch, which is a property of the undo
            # stack rather than a binding to advertise. The line is short and
            # every entry on it has to earn its width.
            mirror,
            cad_edges,
            see_through,
            (key("back"), "Leave object"),
        ]

    # ADJUST
    # getattr: this runs inside a draw handler, which can fire in the middle of
    # a reload when the scene still carries the previous property group.
    commit_label = "Replace patch" if getattr(state, "editing_committed", False) else "Commit"

    # The corner editor has taken the click, Enter and Esc, so the hints have
    # to say so: leaving the patch's line up while its keys mean something else
    # is the one thing a hint must never do.
    if getattr(state, "corner_edit", False):
        return [
            ("Click", "Merge side into the group before it"),
            cad_edges,
            (keymap.describe_all("corners_accept")[0], "Keep corner set"),
            (key("corners_cancel"), "Cancel"),
        ]

    # N-gon mode has no spans at all, so advertising span keys there would be
    # advertising keys that do nothing.
    # The two span keys are one hint, since they are a pair and the line has no
    # room for both -- "Ctrl+Wheel Up/Down" collapses to "Ctrl+Scroll" whenever
    # they really are the two directions of one wheel, and spells both out when
    # somebody has bound them to something else.
    span_key = _pair_label("span_more", "span_less")
    if getattr(state, "ngon_mode", False):
        binds = [
            (span_key, "Detail +/-"),
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
    # Only on a generator that has spans to copy: an n-gon's density is its
    # angle, and there is nothing in the record for the click to take.
    if not getattr(state, "ngon_mode", False):
        binds.append((key("copy_spans"), "Copy a patch's density"))
    # Same key as the copy, separated by what is under the cursor -- so the
    # hint names the side, which is the half a user has to aim at.
    binds.append((key("corners_edit"), "Edit corners (on a side)"))
    # Every way of committing, not just the first: right-click and Enter are
    # both worth knowing (the right-click is the Plasticity-style affordance
    # people arrive expecting), and this is the one action where the second
    # binding is as much a habit as the first.
    for label in keymap.describe_all("commit"):
        binds.append((label, commit_label))
    binds.append((key("back"), "Discard"))
    return binds


def _pair_label(up_action: str, down_action: str) -> str:
    """One label for two opposite actions, e.g. "Ctrl+Scroll" for a wheel pair.

    They are always used together and the hint line is one row across the
    bottom of the screen, so spending two entries on "more" and "less" costs
    more than it says. Collapsed only when they really are the two directions
    of one wheel with the same modifiers; anything else is spelled out, because
    a user who moved one of them needs to see what they moved it to.
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
    _draw_match_points(context, state, region)
    _draw_brep_vertices(context, state, region)

    scale = max(0.5, getattr(state, "overlay_scale", 1.0))
    _draw_side_tooltip(state, region, scale)
    # Only when the side picker has not already spoken: a side under the cursor
    # is the patch being worked on, and two boxes at one pointer is neither.
    if not _side_tooltip_shown(state):
        # The copy source wins over the crack report: the cursor is on a patch
        # the user has deliberately moved onto, and what a click there does now
        # is the more immediate question.
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

    # Laid out in a row, wrapped onto as many rows as it takes, and centred at
    # the bottom. A column down one side is what a docked panel covers -- and
    # every corner of a 3D view has something docked in it.
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
        # Never leave a row empty: one entry wider than the viewport still has
        # to go somewhere.
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
    """World-space vertices of the preview object, or None when there is
    nothing to draw. Read from the *base* mesh, like commit does: the Preview
    Offset is a Displace modifier, so evaluating it would put the dots where
    the geometry isn't going to be committed.
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
    gpu.state.depth_test_set('NONE')  # same reason as the dots: they lie on the surface

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

    A group is what a generator will be handed as one side, so this is the
    whole answer to "what have I made this patch into": five sides in four
    colours is a quad, and the count is what `find_generator` reads.
    """
    references = sidematch.active_sides()
    if not references:
        return

    demoted = sidematch.demoted_corners(state)
    groups = sidematch.corner_groups(references, demoted)
    by_index = {reference.index: reference for reference in references}

    shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    viewport = gpu.state.viewport_get()
    shader.bind()
    shader.uniform_float("viewportSize", (viewport[2], viewport[3]))
    shader.uniform_float("lineWidth", CORNER_GROUP_WIDTH)

    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')  # they lie on the surface; same as the dots

    for position, group in enumerate(groups):
        shader.uniform_float(
            "color", CORNER_GROUP_COLORS[position % len(CORNER_GROUP_COLORS)])
        for index in group:
            reference = by_index.get(index)
            if reference is None or len(reference.points) < 2:
                continue
            batch_for_shader(
                shader, 'LINE_STRIP', {"pos": reference.points}).draw(shader)

    gpu.state.blend_set('NONE')


def _draw_group_bubbles(
    context: bpy.types.Context,
    state: "state_mod.RetopPatchState",
    region: bpy.types.Region,
) -> None:
    """POST_PIXEL: one numbered bubble per side, saying which group it is in.

    The number is the whole interface: colour alone says "these two sides are
    together" only if you can tell two hues apart at a glance across a part,
    and it cannot say *which* group without a legend. A bubble carrying `2`
    says it outright, and it is the thing you click.

    Anchored through `sidematch.side_midpoint`, the same helper the hit test
    uses -- two implementations of "the middle of this side" would drift apart
    on a curved boundary, and a bubble you cannot click where you see it is
    worse than no bubble.
    """
    if state.session_phase != 'ADJUST' or not getattr(state, "corner_edit", False):
        return

    references = sidematch.active_sides()
    if not references:
        return

    rv3d = context.region_data
    if rv3d is None:
        return

    demoted = sidematch.demoted_corners(state)
    numbers = sidematch.group_numbers(references, demoted)
    hovered = getattr(state, "hovered_bubble", -1)

    scale = max(0.5, getattr(state, "overlay_scale", 1.0))
    half = GROUP_BUBBLE_SIZE * scale / 2.0

    # Gathered first so the discs go out as a few batches rather than one per
    # bubble, and so the hovered one draws last -- over its neighbours, which
    # is what it means for it to be under the pointer.
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
        bubbles.append((screen, number, colour, reference.index == hovered))
    if not bubbles:
        return
    bubbles.sort(key=lambda entry: entry[3])

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    shader.bind()

    for screen, _number, colour, is_hovered in bubbles:
        radius = half * (GROUP_BUBBLE_HOVER_RATIO if is_hovered else 1.0)
        rings = [(GROUP_BUBBLE_OUTLINE, radius * GROUP_BUBBLE_OUTLINE_RATIO),
                 (colour, radius)]
        if is_hovered:
            rings.insert(0, (GROUP_BUBBLE_HOVER_RING,
                             radius * GROUP_BUBBLE_HOVER_RING_RATIO))
        for ring_colour, ring_radius in rings:
            shader.uniform_float("color", ring_colour)
            vertices, indices = _discs_around([screen], ring_radius)
            batch_for_shader(shader, 'TRIS', {"pos": vertices},
                             indices=indices).draw(shader)

    gpu.state.blend_set('NONE')

    font_id = 0
    _set_font_size(font_id, GROUP_BUBBLE_FONT * scale)
    blf.color(font_id, *GROUP_BUBBLE_TEXT)
    for screen, number, _colour, _is_hovered in bubbles:
        label = str(number)
        width, height = blf.dimensions(font_id, label)
        blf.position(font_id, screen.x - width * 0.5, screen.y - height * 0.5, 0)
        blf.draw(font_id, label)


def _side_appearance(
    reference: "sidematch.SideReference", hovered: bool
) -> "tuple[tuple[float, float, float, float], float]":
    """(colour, line width) for one side of the picker.

    Green is a side whose vertices the preview is *actually* reproducing, red
    one that borders a committed neighbour and is not, grey one with nothing
    across it to match. Only the red is a problem, which is the whole reason it
    is red: "could be matched but is not" and "has nothing to match" used to
    draw the same grey, and only the tooltip told them apart.
    """
    if reference.applied:
        return ((SIDE_MATCHED_HOVER_COLOR if hovered else SIDE_MATCHED_COLOR),
                SIDE_HOVER_WIDTH if hovered else SIDE_MATCHED_WIDTH)
    if reference.available:
        return ((SIDE_UNMATCHED_HOVER_COLOR if hovered else SIDE_UNMATCHED_COLOR),
                SIDE_HOVER_WIDTH if hovered else SIDE_MATCHED_WIDTH)
    return ((SIDE_BLOCKED_HOVER_COLOR if hovered else SIDE_BLOCKED_COLOR),
            SIDE_HOVER_WIDTH if hovered else SIDE_WIDTH)


# Red, like the border it names and like an unmatched side: three places, one
# meaning.
TOOLTIP_CRACK = (1.0, 0.55, 0.5, 1.0)


def _draw_side_tooltip(
    state: "state_mod.RetopPatchState", region: bpy.types.Region, scale: float
) -> None:
    """What the side under the cursor is doing, said by the cursor.

    Two lines: whether it is selected for surface matching, and why. Drawn from
    the POST_PIXEL handler, next to the pointer rather than in a corner -- the
    question is asked with the mouse already on the side.
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

    Shared, not copied: the side picker and the cracked-border report both
    speak from the pointer, and two boxes drawn by two functions end up
    disagreeing about padding the first time either is touched.
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
    # Kept inside the region: a tooltip half off the edge of the viewport is
    # exactly the half you needed to read.
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

    Screen space, like the side picker and for the same reason: the line lies
    exactly on the surface, so a raycast hits the surface beside it as often as
    the line itself.

    Walked only when there *are* cracks, which is the exceptional case -- on
    retopology that welded, this returns after one empty list.
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
    """Name the two patches of the crack under the cursor, and what to do.

    The dashes say *where*; only this says which two patches disagree, and a
    seam you cannot attribute is one you cannot fix -- the border looks the
    same from both sides.
    """
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

    The outline says *which* patch; only this says what would come across, and
    a density you cannot read before taking it is a click you have to undo to
    find out about.
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
    """POST_VIEW: the side highlight, which is genuinely 3D geometry.

    The vertex dots are *not* here -- they are drawn in screen space by
    `_draw_vertex_dots`, from the POST_PIXEL handler.
    """
    context = bpy.context
    state = getattr(context.scene, "plasticity_retop", None)
    # getattr throughout: a draw handler can fire mid-reload, when the scene
    # still carries the previous property group.
    if state is None or not state.session_active:
        return

    # Drawn in every phase, unlike the side highlight: the CAD structure is what
    # you read *while choosing* a surface, not only while adjusting one.
    _draw_cad_structure(context, state)
    _draw_cracked_borders(context, state)
    _draw_copy_source(context, state)

    if state.session_phase != 'ADJUST':
        return
    # One or the other, never both: the corner editor has taken over the same
    # lines the side picker paints, and two colour schemes on one polyline is
    # neither of them.
    if getattr(state, "corner_edit", False):
        _draw_corner_groups(state)
    elif getattr(state, "match_mode", False):
        _draw_side_references(state)


def _match_dot_sets(
    state: "state_mod.RetopPatchState",
) -> "list[tuple[list[mathutils.Vector], tuple[float, float, float, float]]]":
    """[(world points, colour)] the match overlay should draw right now.

    The hovered side shows what clicking it would take; a pinned side shows
    what it did take, so a pin that landed on the wrong neighbour stays visible
    after the click rather than only during the hover.
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
            # Nothing being matched here: preview what a click would take.
            # Including a side released by hand -- the dots are what clicking
            # it again would bring back.
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

    Its B-rep edges, which `cad_display` already has cached per patch -- a
    hover may not walk a mesh, and this one fires on every mouse move while a
    patch is open.
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

    # Dimmed when the two generators disagree: the outline still says "there is
    # something here", and the tooltip says why the click will refuse. Drawn
    # either way, because an outline that appears only on a valid target makes
    # a refusal look like a target that was never there.
    title, _detail = mesh_build.copy_source_status(state, obj, face_id)
    matches = title.startswith("Copy")

    matrix = obj.matrix_world
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    _draw_line_batch([matrix @ point for point in segments],
                     COPY_SOURCE_COLOR if matches else COPY_SOURCE_REFUSED_COLOR,
                     COPY_SOURCE_WIDTH)
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

    Drawn in every phase and through the model unconditionally. Both are
    deliberate: a crack is a defect in finished work, so it has to be visible
    while picking the next patch -- which is when it can still be acted on --
    and one hidden behind the part it is in is a warning that only reaches
    somebody already looking at it.
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

    None for the face id means the whole object. A scope of ACTIVE with nothing
    picked yet falls back to the whole object rather than drawing nothing --
    "pick a surface" is exactly the moment the model's layout is worth seeing.
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

    One batch, not one per edge: a CAD part has hundreds of edges, and a draw
    call each is what turns an overlay into a stutter.
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


# How far a depth-tested CAD line is nudged towards the viewer, as a share of
# its distance to the viewpoint. Proportional rather than absolute for the same
# reason the raycast's step past a hit is: a fixed epsilon is either too small
# to clear the surface at range or large enough to lift a line off a small part
# visibly. Small enough that the line still reads as lying *on* the surface.
DEPTH_NUDGE = 0.002


def _towards_viewer(
    points: list["mathutils.Vector"], rv3d: bpy.types.RegionView3D | None
) -> list["mathutils.Vector"]:
    """Lift world-space points off the surface, towards the viewpoint.

    Only needed when the CAD display is depth-tested: the lines lie exactly on
    the surface they describe, so testing them against it without this is a
    coin flip per pixel and they come out as a stipple. One view direction for
    the whole batch rather than a per-point eye vector -- at a nudge this small
    the difference at the edge of the frame is far below a pixel, and a draw
    handler should not be normalising a vector per point.
    """
    if rv3d is None or not points:
        return points
    # Everything here comes out of the region's own matrix, so the draw handler
    # still imports nothing it didn't already.
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

    # Drawn through the model by default -- that is what makes the structure of
    # a whole part readable at a glance. Turned off, they are occluded like
    # real geometry, so the far side of a curved or enclosed shape stops
    # showing through the near side; the nudge is what keeps them from
    # z-fighting with the very surface they lie on.
    xray = getattr(state, "cad_display_xray", True)
    rv3d = context.region_data
    if xray:
        gpu.state.depth_test_set('NONE')
    else:
        gpu.state.depth_test_set('LESS_EQUAL')

    def place(points: list["mathutils.Vector"]) -> list["mathutils.Vector"]:
        world = [matrix @ point for point in points]
        return world if xray else _towards_viewer(world, rv3d)

    # Flow first, edges over it: the edges are the exact thing and should never
    # be hidden by the derived one.
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

    Tied to the edge display: a B-rep vertex is where two edges meet, and dots
    with no edges to sit on say nothing.
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

    Round rather than square: these mark where a vertex *is*, and a square dot
    reads as a handle to grab. `segments` stays a multiple of four so the disc
    measures exactly 2*half across and 2*half tall -- the size setting has to
    stay something a test can measure.
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

    # Dark disc behind the bright one, so a dot stays readable over both a
    # pale CAD surface and the dark background.
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
# What the bridge actually wrote, on the surface it wrote it about: the face id
# of each patch, and the `[loop_start, loop_count]` range that names it.
#
# Its own handler rather than the session's, and deliberately so: the question
# it answers -- "which CAD face is this, and does the mesh still agree with the
# groups?" -- is asked of a freshly imported object, before any session exists.
# The callback early-outs on the toggle, so it is installed once and left there;
# a handler armed and disarmed by a property update is a handler that is missing
# after a file load, which looks exactly like an overlay that does not work.
DEBUG_LABEL_COLOR = (1.0, 0.95, 0.55, 1.0)
DEBUG_DETAIL_COLOR = (0.72, 0.80, 0.95, 1.0)
DEBUG_BG = (0.08, 0.08, 0.10, 0.78)
DEBUG_FONT_SIZE = 12
DEBUG_PAD = 5
DEBUG_LINE_GAP = 2

# A backstop, not a policy. Under Hover -- the default -- one label is drawn and
# this is never reached; under All, on a part of a few hundred faces, the text
# stopped being readable long before it stopped being affordable.
MAX_DEBUG_LABELS = 250

_debug_handle: object | None = None

# (object name, face id) under the cursor, or None. Written by
# `operators.RETOP_OT_patch_hover`, which is the only thing that sees an event;
# a module global rather than a scene property because a property written on
# every mouse move marks the file as modified, and reading a mesh should not.
# Empty after a reload and after a file load, which this must cope with.
debug_hover: tuple[str, int] | None = None


def _has_face_ids(obj: "bpy.types.Object | None") -> bool:
    return (obj is not None and obj.type == 'MESH'
            and bool(obj.data.get("face_ids")))


def _patch_debug_target(
    context: bpy.types.Context, scope: str
) -> "bpy.types.Object | None":
    """The object whose patch data to write out.

    Under Hover that is whatever the cursor found, which may be neither the
    active object nor the session's -- pointing at a patch is the whole of the
    question being asked, so nothing else gets to override it. Otherwise the
    active object when it carries face ids, so this works with no session at
    all, and the session's object failing that, since during one the active
    object may well be the preview or the result mesh.
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

    The labels are screen-space text and so have no depth to be tested against
    -- which is what keeps them legible over the surface they describe, and also
    what would otherwise write the back of a closed part over its front.
    """
    inverse = rv3d.view_matrix.inverted()
    if rv3d.is_perspective:
        eye = anchor - inverse.translation
    else:
        # Ortho: every point is seen along the same axis, and the camera's
        # translation is not on it.
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
    # A hovered patch is the one the cursor is on, so it is in front by
    # construction; culling it can only ever hide the answer (a back face seen
    # through an open shell is still the face being pointed at).
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

        # Bottom-up, so the id reads first from the top.
        cursor = y
        for text, colour, text_w, text_h in reversed(measured):
            blf.color(font_id, *colour)
            blf.position(font_id, x + (width - text_w) * 0.5, cursor, 0)
            blf.draw(font_id, text)
            cursor += text_h + gap
        drawn += 1


def enable_patch_debug() -> None:
    """Install the debug handler. Idempotent, and left installed for the life of
    the addon -- see the note above."""
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
# Alt+X arms `RETOP_OT_mirror` and it then waits for X, Y or Z. That prompt
# lived in the status bar alone, which is at the bottom of the *window* -- the
# one place nobody is looking while their hand is on the keyboard and their eyes
# are on the part. Hard Ops puts its axis choice under the cursor, and this is
# the same idea: three keys, each showing whether that axis is mirrored right
# now, drawn where the question was asked.
#
# It has its own handler rather than riding the session's, because the mirror is
# a GLOBAL key: with `global_keys_outside_session` on it is armed with no
# session running and no session overlay installed.
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
# mirror operator; a draw handler has no event to read a cursor from and no
# business reaching for a modifier on an object.
mirror_cursor: "tuple[int, int] | None" = None
mirror_state: tuple[bool, bool, bool] = (False, False, False)


def _draw_mirror_gizmo() -> None:
    """Three labelled squares under the cursor, one per axis, lit when on."""
    if mirror_cursor is None:
        return
    region = bpy.context.region
    if region is None or region.type != 'WINDOW':
        return

    # The panel's own overlay scale, read defensively: this handler can be
    # installed with no session and the scene property is the only thing that
    # says how big the user wants the addon's overlays.
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

    Every exit path of the mirror modal calls this -- an axis pressed, Esc, a
    click, a key that isn't an axis -- because a draw handler nobody owns is
    the one kind of leak the version string cannot report.
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
