"""Run inside Blender: blender --background --python tests/test_corner_edit.py

Choosing which of a patch's corners are side boundaries.

A Plasticity face with five B-rep vertices goes to the N-Side fan, and the fan
is usually not what the shape wants: a pentagon born of a quad with one corner
cut reads as a *quad* whose bottom side happens to be two sides in a row.
Turning that junction off -- keeping the vertex, dropping its status as a side
boundary -- is what turns the five sides into four groups.

This pins the half that is implemented: the grouping itself, the gestures that
drive it, and the fact that the editor takes the click, the commit keys and Esc
away from the patch for as long as it is open. The generators still see the
ungrouped sides; wiring the groups into `find_generator` is the next step, and
the check at the bottom says so out loud so nobody reads a green suite as
"pentagons are handled now".
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

state = bpy.context.scene.plasticity_retop
sidematch = pr.sidematch


def sides(count, loop=0, first=0):
    """`count` bare SideReferences, enough for the grouping to run on."""
    return [sidematch.SideReference(first + n, loop, n, [], None, None)
            for n in range(count)]


# ---------------------------------------------------------------------------
#  Grouping
# ---------------------------------------------------------------------------
five = sides(5)

check("with every corner on, each side is its own group",
      sidematch.corner_groups(five, set()) == [[0], [1], [2], [3], [4]],
      sidematch.corner_groups(five, set()))

# The case the whole feature is for: five sides, four groups, and the two that
# meet at the demoted corner are the ones that merged.
check("turning one corner off merges the two sides meeting at it",
      sidematch.corner_groups(five, {1}) == [[0, 1], [2], [3], [4]],
      sidematch.corner_groups(five, {1}))
check("and the patch reads as four-sided",
      sidematch.loop_group_counts(five, {1}) == {0: 4},
      sidematch.loop_group_counts(five, {1}))

# A group may straddle the point the half-edge walk happened to start at --
# that point is arbitrary, so a grouping that broke there would depend on it.
check("a group wraps the loop's arbitrary start",
      sidematch.corner_groups(five, {0}) == [[1], [2], [3], [4, 0]],
      sidematch.corner_groups(five, {0}))
check("and still counts as four",
      sidematch.loop_group_counts(five, {0}) == {0: 4},
      sidematch.loop_group_counts(five, {0}))

check("two off gives a triangle", sidematch.loop_group_counts(five, {1, 2}) == {0: 3},
      sidematch.loop_group_counts(five, {1, 2}))

# A ring's two rims are separate boundaries: a corner turned off on one must
# not merge anything on the other, and the counts are reported per loop.
band = sides(4) + sides(3, loop=1, first=4)
check("loops are grouped independently",
      sidematch.loop_group_counts(band, {1, 5}) == {0: 3, 1: 2},
      sidematch.loop_group_counts(band, {1, 5}))

# Every corner of a loop off leaves one closed side, which no generator accepts
# -- `find_generator` starts at Wedge 2. Reported as one group so the caller
# can refuse rather than silently making the patch unpickable.
check("a loop with nothing left reports one group",
      sidematch.loop_group_counts(five, {0, 1, 2, 3, 4}) == {0: 1},
      sidematch.loop_group_counts(five, {0, 1, 2, 3, 4}))

sidematch.set_demoted_corners(state, {3, 1})
check("the stored form round trips", sidematch.demoted_corners(state) == {1, 3},
      state.corner_overrides)
sidematch.set_demoted_corners(state, set())
check("and empty stores nothing at all", state.corner_overrides == "",
      state.corner_overrides)

# ---------------------------------------------------------------------------
#  The editor takes the patch's gestures for as long as it is open
# ---------------------------------------------------------------------------
sidematch._active_sides = five
state.session_active = True
state.session_phase = 'ADJUST'
state.active_face_id = 1
state.match_mode = True
state.hovered_side = 2
state.editing_committed = True

POLLS = {
    "commit": bpy.ops.retop.commit_patch.poll,
    "back": bpy.ops.retop.back.poll,
    "pin": bpy.ops.retop.pin_side.poll,
    "copy": bpy.ops.retop.copy_patch_spans.poll,
    "delete": bpy.ops.retop.delete_patch.poll,
    "span": bpy.ops.retop.nudge_span.poll,
    "ngon": bpy.ops.retop.toggle_ngon.poll,
    "cad_edges": bpy.ops.retop.toggle_cad_edges.poll,
    "surface_flow": bpy.ops.retop.toggle_surface_flow.poll,
    "edit_corners": bpy.ops.retop.edit_corners.poll,
    "corner_toggle": bpy.ops.retop.toggle_corner.poll,
    "accept": bpy.ops.retop.corners_accept.poll,
    "cancel": bpy.ops.retop.corners_cancel.poll,
}


def live():
    return {name for name, poll in POLLS.items() if poll()}


state.corner_edit = False
before = live()
check("with a side under the cursor, Ctrl+click opens the editor rather than copying",
      "edit_corners" in before and "copy" not in before, sorted(before))
check("and the editor's own keys are dead until it is open",
      not {"corner_toggle", "accept", "cancel"} & before, sorted(before))

state.hovered_side = -1
check("pointing at no side, Ctrl+click is the density copy again",
      "copy" in live() and "edit_corners" not in live(), sorted(live()))

state.corner_edit = True
state.hovered_corner = 2
during = live()
check("open, it owns Enter, Esc and the click",
      not {"commit", "back", "pin", "copy", "delete"} & during, sorted(during))
check("and the spans with them", not {"span", "ngon"} & during, sorted(during))
check("its own three are live",
      {"corner_toggle", "accept", "cancel"} <= during, sorted(during))
# Read *while* choosing corners, which is the whole reason they opt back in.
check("the CAD structure displays stay available",
      {"cad_edges", "surface_flow"} <= during, sorted(during))

state.hovered_corner = -1
check("with no corner under the cursor there is nothing to toggle",
      not bpy.ops.retop.toggle_corner.poll())

# ---------------------------------------------------------------------------
#  One key, two meanings, resolved by poll and not by declaration order alone
# ---------------------------------------------------------------------------
keymap = pr.keymap


class Event:
    def __init__(self, type, ctrl=False):
        self.type = type
        self.value = 'PRESS'
        self.ctrl = ctrl
        self.shift = False
        self.alt = False
        self.oskey = False


for key, expected in (('LEFTMOUSE', "corner_toggle"), ('RET', "corners_accept"),
                      ('ESC', "corners_cancel")):
    state.corner_edit = True
    state.hovered_corner = 2
    bound = keymap.session_action_for(Event(key))
    check(f"{key} is the editor's while it is open", bound == expected, bound)

state.corner_edit = False
state.hovered_side = 2
state.hovered_corner = -1
check("closed, the left click is the side picker's again",
      keymap.session_action_for(Event('LEFTMOUSE')) == "pin_neighbour",
      keymap.session_action_for(Event('LEFTMOUSE')))
check("and Esc discards the patch",
      keymap.session_action_for(Event('ESC')) == "back",
      keymap.session_action_for(Event('ESC')))

# ---------------------------------------------------------------------------
#  Open, toggle, cancel
# ---------------------------------------------------------------------------
state.corner_overrides = ""
bpy.ops.retop.edit_corners()
check("the editor opens", state.corner_edit)

state.hovered_corner = 1
bpy.ops.retop.toggle_corner()
check("a click turns the corner off", sidematch.demoted_corners(state) == {1},
      state.corner_overrides)
bpy.ops.retop.toggle_corner()
check("and clicking it again turns it back on",
      sidematch.demoted_corners(state) == set(), state.corner_overrides)

bpy.ops.retop.toggle_corner()
bpy.ops.retop.corners_cancel()
check("Esc closes the editor", not state.corner_edit)
check("and puts back the corner set it was opened with",
      sidematch.demoted_corners(state) == set(), state.corner_overrides)

bpy.ops.retop.edit_corners()
state.hovered_corner = 3
bpy.ops.retop.toggle_corner()
bpy.ops.retop.corners_accept()
check("Enter closes it keeping the change",
      not state.corner_edit and sidematch.demoted_corners(state) == {3},
      state.corner_overrides)

# Below two sides a loop is one closed curve, which no generator accepts -- so
# it is refused at the click, while the editor is still open to undo it.
bpy.ops.retop.edit_corners()
for corner in (0, 1, 2, 4):
    state.hovered_corner = corner
    bpy.ops.retop.toggle_corner()
check("a loop is never taken below two sides",
      sidematch.loop_group_counts(five, sidematch.demoted_corners(state))[0] >= 2,
      sorted(sidematch.demoted_corners(state)))
bpy.ops.retop.corners_cancel()

# ---------------------------------------------------------------------------
#  What is *not* wired up yet
# ---------------------------------------------------------------------------
# Said out loud so a green suite is not read as "five-sided patches are filled
# as quads now". The grouping is chosen and drawn; `_generate_for_face` still
# hands the generator the ungrouped sides.
source = open(os.path.join(_ADDON_DIR, "operators.py"), encoding="utf-8").read()
check("the generators do not read the corner set yet -- next step",
      "corner_groups" not in source.split("def _generate_for_face", 1)[-1][:4000])

sidematch._active_sides = []
state.session_active = False

print()
if FAILURES:
    print(f"=== {len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(1)
print("=== ALL CHECKS PASSED")
