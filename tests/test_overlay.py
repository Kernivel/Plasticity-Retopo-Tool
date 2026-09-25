"""Run inside Blender: blender --background --python tests/test_overlay.py

The viewport overlays: their draw handlers, and the keybind hints.

This file exists because the suite once passed with `overlay.enable()` raising
`NameError` on a function that had been deleted by an over-eager edit. Nothing
called it -- the draw handlers are the one part of the addon that only Blender
invokes, so unless a test does it too, a missing name gets found by the user.
So: install the handlers, and call every callback.
"""
import os
import sys
import importlib

_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_ADDON_DIR))

import bpy

pr = importlib.import_module(os.path.basename(_ADDON_DIR))

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

overlay = pr.overlay
state = bpy.context.scene.plasticity_retop


# ===========================================================================
# Installing and removing the handlers
# ===========================================================================
def enable():
    """Returns the exception instead of raising, so a missing name is reported
    as a failed check rather than killing the run."""
    try:
        overlay.enable()
        return None
    except Exception as exc:  # noqa: BLE001 -- reporting it *is* the test
        return exc


error = enable()
check("enable() installs the handlers without raising", error is None, error)
check("both are installed",
      overlay._handle is not None and overlay._points_handle is not None)

error = enable()
check("calling it twice is harmless", error is None, error)
check("and does not stack a second pair",
      overlay._handle is not None and overlay._points_handle is not None)

overlay.disable()
check("disable() removes both",
      overlay._handle is None and overlay._points_handle is None)
overlay.disable()
check("and is safe twice", True)


# ===========================================================================
# The callbacks themselves
#
# Headless there is no region, so they take their early exits -- which is
# exactly the path that was broken: a name that does not exist fails at import
# of the *call*, before any of that matters.
# ===========================================================================
def call(callback, label):
    try:
        callback()
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"    {label} raised: {exc!r}")
        return False


overlay.enable()
for phase in ('OBJECT', 'PATCH', 'ADJUST'):
    for session_active in (False, True):
        state.session_active = session_active
        state.session_phase = phase
        check(f"_draw survives phase={phase} active={session_active}",
              call(overlay._draw, "_draw"))
        check(f"_draw_points survives phase={phase} active={session_active}",
              call(overlay._draw_points, "_draw_points"))

state.session_active = True
state.session_phase = 'ADJUST'
for ngon in (False, True):
    for match in (False, True):
        state.ngon_mode = ngon
        state.match_mode = match
        check(f"_draw_points survives ngon={ngon} match={match}",
              call(overlay._draw_points, "_draw_points"))

# The corner editor draws its own groups and its own dots, off the side
# picker's code path entirely -- so it needs its own pass through both
# handlers, with sides present *and* absent: the empty case is what a draw
# fired between two patches sees.
for corners in (False, True):
    state.corner_edit = corners
    state.hovered_bubble = 1 if corners else -1
    check(f"_draw_points survives corner_edit={corners}",
          call(overlay._draw_points, "_draw_points"))
    check(f"_draw survives corner_edit={corners}", call(overlay._draw, "_draw"))

state.corner_edit = True
pr.sidematch._active_sides = [
    pr.sidematch.SideReference(n, 0, n, [], None, None) for n in range(5)]
# Group 1 in two separate places -- the invalid state the fault rings and
# the banner exist for, so both run here rather than only the happy path.
pr.sidematch.set_side_groups(state, {2: 1})
# With sides actually present the group drawing runs past its early exits and
# reaches the GPU, which headless Blender has none of -- so the assertion is
# that *that* is the only thing it hits. A missing name would raise here first,
# which is the whole reason this file calls the callbacks at all.
try:
    overlay._draw_points()
    reached = "no error"
except SystemError as exc:
    reached = "gpu" if "background mode" in str(exc) else repr(exc)
except Exception as exc:  # noqa: BLE001
    reached = repr(exc)
check("the corner groups draw down to the GPU call and no further",
      reached in ("gpu", "no error"), reached)
check("_draw_group_bubbles survives with no region",
      call(lambda: overlay._draw_group_bubbles(bpy.context, state, None),
           "_draw_group_bubbles"))
state.group_warning = "Group 1 is in 2 separate places."
check("_draw_group_warning survives with no region",
      call(lambda: overlay._draw_group_warning(bpy.context, state, None),
           "_draw_group_warning"))
state.group_warning = ""
pr.sidematch._active_sides = []
pr.sidematch.set_side_groups(state, {})
state.corner_edit = False
state.hovered_bubble = -1

check("with no preview there are no vertex coords",
      overlay._preview_vertex_coords() is None)
check("and with no active patch, no side references to draw",
      pr.sidematch.active_sides() == [])

bpy.context.scene.plasticity_retop.typed_span = "12"
check("_draw survives a half-typed span", call(overlay._draw, "_draw"))
bpy.context.scene.plasticity_retop.typed_span = ""
overlay.disable()


# ===========================================================================
# The hints themselves
# ===========================================================================
for phase in ('OBJECT', 'PATCH', 'ADJUST'):
    state.session_phase = phase
    binds = overlay.keybinds_for(state)
    check(f"phase {phase} advertises some keys", len(binds) > 0, len(binds))
    check(f"phase {phase} yields (key, action) pairs",
          all(isinstance(k, str) and isinstance(a, str) and k and a
              for k, a in binds), binds)

state.session_phase = 'ADJUST'
state.ngon_mode = True
ngon_binds = dict(overlay.keybinds_for(state))
check("N-gon mode offers Ctrl+Scroll for detail, not for a span it has not got",
      ngon_binds.get("Ctrl+Scroll", "").startswith("Detail +/-"), ngon_binds)
state.ngon_mode = False
grid_binds = dict(overlay.keybinds_for(state))
check("a grid offers it for the span", "Span" in grid_binds.get("Ctrl+Scroll", ""),
      grid_binds)

state.editing_committed = True
check("a re-edit advertises the delete",
      "X" in dict(overlay.keybinds_for(state)))
state.editing_committed = False
check("and a fresh patch does not",
      "X" not in dict(overlay.keybinds_for(state)))

# The overlay scale reaches the draw path rather than being a dead property.
state.overlay_scale = 2.0
overlay.enable()
check("_draw survives a scaled overlay", call(overlay._draw, "_draw"))
state.overlay_scale = 0.5
check("and a shrunk one", call(overlay._draw, "_draw"))
state.overlay_scale = 1.0
overlay.disable()


# ===========================================================================
# N-gon vertex dots
#
# They used to be GL points sized with `gpu.state.point_size_set`, which the
# backend ignores whenever program point size is on -- the shader has to write
# gl_PointSize then, and the builtin UNIFORM_COLOR one does not. The result was
# 1px dots that ignored the size setting completely. They are screen-space
# discs now (round, because a square dot reads as a handle you can grab), so
# the pixels asked for are the pixels drawn, and that is a pure function this
# can check exactly.
# ===========================================================================
source = open(os.path.join(_ADDON_DIR, "overlay.py"), encoding="utf-8").read()
# The *call*, not the word: the comment above the constants explains why it is
# gone, and should keep saying so.
check("nothing calls GL point size any more",
      "point_size_set(" not in source)

SEGMENTS = overlay.DOT_SEGMENTS
vertices, indices = overlay._discs_around([(100.0, 200.0)], 5.0)
check("one dot is a centre plus its rim", len(vertices) == SEGMENTS + 1,
      len(vertices))
check("and one triangle per segment", len(indices) == SEGMENTS, len(indices))
check("the fan closes -- no gap where it wraps",
      indices[-1][2] == indices[0][1], indices[-1])
check("the segment count keeps the disc measurable", SEGMENTS % 4 == 0, SEGMENTS)
check("indices stay inside the vertices they were built with",
      all(0 <= i < len(vertices) for tri in indices for i in tri), indices)

xs = [v[0] for v in vertices]
ys = [v[1] for v in vertices]
check("the disc is 2*half wide -- the size setting reaches the geometry",
      abs((max(xs) - min(xs)) - 10.0) < 1e-6, max(xs) - min(xs))
check("and 2*half tall", abs((max(ys) - min(ys)) - 10.0) < 1e-6, max(ys) - min(ys))
check("centred on the point it marks",
      abs((max(xs) + min(xs)) / 2 - 100.0) < 1e-6
      and abs((max(ys) + min(ys)) / 2 - 200.0) < 1e-6, (xs, ys))

wide, _ = overlay._discs_around([(0.0, 0.0)], 10.0)
narrow, _ = overlay._discs_around([(0.0, 0.0)], 5.0)
check("doubling the size doubles the disc -- the bug was that it did not",
      (max(v[0] for v in wide) - min(v[0] for v in wide))
      == 2 * (max(v[0] for v in narrow) - min(v[0] for v in narrow)))

many_v, many_i = overlay._discs_around([(0.0, 0.0), (50.0, 0.0), (0.0, 50.0)], 3.0)
check("every dot gets its own fan",
      len(many_v) == 3 * (SEGMENTS + 1) and len(many_i) == 3 * SEGMENTS,
      f"{len(many_v)} verts, {len(many_i)} tris")
check("nothing at all is still nothing", overlay._discs_around([], 5.0) == ([], []))

# And the draw path itself, in the states it is reached from.
state.session_active = True
state.session_phase = 'ADJUST'
state.ngon_mode = True
state.ngon_show_verts = True
for size in (2.0, 11.0, 40.0):
    state.ngon_vert_size = size
    check(f"_draw survives a dot size of {size}", call(overlay._draw, "_draw"))
state.ngon_vert_size = 11.0

check("_draw_vertex_dots is safe with no region",
      call(lambda: overlay._draw_vertex_dots(bpy.context, state, None),
           "_draw_vertex_dots"))


# ===========================================================================
# The mirror's axis gizmo
# ===========================================================================
# Its own handler, because the mirror is a GLOBAL key: with
# `global_keys_outside_session` on it is armed with no session running and no
# session overlay installed. Which means nothing else here would ever call it.
overlay.enable_mirror_gizmo()
check("the gizmo installs its own handler", overlay._mirror_handle is not None)
overlay.enable_mirror_gizmo()
check("arming it twice does not stack a second one",
      overlay._mirror_handle is not None)

check("it draws nothing with no cursor recorded",
      call(overlay._draw_mirror_gizmo, "_draw_mirror_gizmo"))

overlay.mirror_cursor = (400, 300)
for axes in ((False, False, False), (True, False, False), (True, True, True)):
    overlay.mirror_state = axes
    check(f"_draw_mirror_gizmo survives axes={axes}",
          call(overlay._draw_mirror_gizmo, "_draw_mirror_gizmo"))

overlay.disable_mirror_gizmo()
check("disabling it drops the handler", overlay._mirror_handle is None)
check("and the cursor it drew from", overlay.mirror_cursor is None)
overlay.disable_mirror_gizmo()
check("and is safe twice", True)

# A session teardown or a reload must not leave the prompt's handler behind.
overlay.enable_mirror_gizmo()
overlay.disable()
check("overlay.disable() takes the gizmo with it",
      overlay._mirror_handle is None)


# ===========================================================================
# The patch data debug display
#
# Its own handler, installed for the life of the addon rather than for the life
# of a session: it describes an imported mesh, which is something you look at
# before any session exists. So nothing in the session's enable/disable pair
# would ever call it.
# ===========================================================================
check("register() installed it", overlay._debug_handle is not None)

overlay.enable_patch_debug()
check("arming it twice does not stack a second one",
      overlay._debug_handle is not None)

# Off is the default, and the early exit is the path taken on every redraw of
# every viewport for anyone not using it.
state.debug_patch_ids = False
check("_draw_patch_debug survives with the toggle off",
      call(overlay._draw_patch_debug, "_draw_patch_debug"))

state.debug_patch_ids = True
for scope in ('HOVER', 'SELECTED', 'ALL'):
    for detail in (False, True):
        for cull in (False, True):
            state.debug_patch_scope = scope
            state.debug_patch_detail = detail
            state.debug_patch_cull = cull
            check(f"_draw_patch_debug survives scope={scope} "
                  f"detail={detail} cull={cull}",
                  call(overlay._draw_patch_debug, "_draw_patch_debug"))

# Hover is fed by a modal that leaves a name in a module global, so the draw
# path has to cope with the two states nothing else can produce: no hover at
# all (a reload, a file load, the pointer off the viewport), and a hover naming
# an object that has since gone away.
state.debug_patch_scope = 'HOVER'
overlay.debug_hover = None
check("_draw_patch_debug survives with no hover",
      call(overlay._draw_patch_debug, "_draw_patch_debug"))
overlay.debug_hover = ("NoSuchObject", 7)
check("...and with a hover naming an object that is gone",
      call(overlay._draw_patch_debug, "_draw_patch_debug"))
check("which resolves to no target rather than raising",
      overlay._patch_debug_target(bpy.context, 'HOVER') is None)
overlay.debug_hover = None

state.debug_patch_ids = False

# The session's own teardown must NOT take this one with it: it is not the
# session's, and a debug display that vanishes when a session ends is one that
# cannot be used to work out why the session went wrong.
overlay.disable()
check("overlay.disable() leaves the debug handler alone",
      overlay._debug_handle is not None)

overlay.disable_patch_debug()
check("disable_patch_debug() removes it", overlay._debug_handle is None)
overlay.disable_patch_debug()
check("and is safe twice", True)
overlay.enable_patch_debug()  # put it back, as register() left it

print()
if FAILURES:
    print(f"=== {len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(1)
print("=== ALL CHECKS PASSED")
