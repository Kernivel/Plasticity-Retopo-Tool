"""Run inside Blender: blender --background --python tests/test_corner_edit.py

Choosing which group each side of a patch is in.

A Plasticity face with five B-rep vertices goes to the N-Side fan, and the fan
is usually not what the shape wants: a pentagon born of a quad with one corner
cut reads as a *quad* whose bottom side happens to be two sides in a row.
Putting two adjacent sides in one group is what turns the five sides into four,
and four is what `find_generator` should be handed.

The numbering is **free, and checked rather than constrained**. An earlier
version stored the demoted corners and let a click only merge a side into the
one before it -- always valid by construction, and unpredictable to use, since
nothing on screen said whether the next click would open a new group or join an
existing one. So any number is reachable now and a grouping that cannot work is
*reported*. That is what most of this file pins: the rules for what is broken,
and the fact that they only fire on what is genuinely broken.

The generators still see the ungrouped sides; wiring the groups into
`find_generator` is the next step, and the check at the bottom says so out loud
so nobody reads a green suite as "pentagons are handled now".
"""
import os
import sys
import importlib

_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_ADDON_DIR))

import bpy
import mathutils

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


def numbered(references, mapping):
    """The full numbering `mapping` implies, defaults filled in."""
    sidematch.set_side_groups(state, mapping)
    return sidematch.group_numbers(references, state)


five = sides(5)
band = sides(4) + sides(3, loop=1, first=4)

# ---------------------------------------------------------------------------
#  The numbering
# ---------------------------------------------------------------------------
check("by default every side is its own group, numbered from 1",
      numbered(five, {}) == {0: 1, 1: 2, 2: 3, 3: 4, 4: 5}, numbered(five, {}))

# Per loop, so a ring's two rims both start at 1: they are separate boundaries
# and numbering them 1..7 across both would imply an order they have no reason
# to share.
check("each loop numbers its own sides from 1",
      numbered(band, {}) == {0: 1, 1: 2, 2: 3, 3: 4, 4: 1, 5: 2, 6: 3},
      numbered(band, {}))

sidematch.set_side_groups(state, {2: 1})
check("only the sides actually set are stored",
      sidematch.side_groups(state) == {2: 1}, state.side_groups)
sidematch.set_side_groups(state, {})
check("and an empty set stores nothing at all", state.side_groups == "",
      state.side_groups)

# ---------------------------------------------------------------------------
#  Runs and counts
# ---------------------------------------------------------------------------
# The case the whole feature is for: five sides, four groups.
check("two adjacent sides sharing a number are one run",
      sidematch.group_runs(five, numbered(five, {1: 1})) == [[0, 1], [2], [3], [4]],
      sidematch.group_runs(five, numbered(five, {1: 1})))
check("and the patch reads as four-sided",
      sidematch.loop_group_counts(five, numbered(five, {1: 1})) == {0: 4},
      sidematch.loop_group_counts(five, numbered(five, {1: 1})))

# A run may straddle the point the half-edge walk happened to start at -- that
# point is arbitrary, so a grouping that broke there would depend on it.
check("a run wraps the loop's arbitrary start",
      sidematch.group_runs(five, numbered(five, {0: 5})) == [[1], [2], [3], [4, 0]],
      sidematch.group_runs(five, numbered(five, {0: 5})))
check("and still counts as four",
      sidematch.loop_group_counts(five, numbered(five, {0: 5})) == {0: 4},
      sidematch.loop_group_counts(five, numbered(five, {0: 5})))

check("loops are grouped independently",
      sidematch.loop_group_counts(band, numbered(band, {1: 1, 5: 1})) == {0: 3, 1: 2},
      sidematch.loop_group_counts(band, numbered(band, {1: 1, 5: 1})))

# ---------------------------------------------------------------------------
#  What is reported, and what is deliberately not
# ---------------------------------------------------------------------------
at_fault, message = sidematch.group_problems(five, numbered(five, {}))
check("the default numbering has nothing wrong with it", not at_fault and not message,
      (sorted(at_fault), message))

at_fault, message = sidematch.group_problems(five, numbered(five, {1: 1}))
check("nor has a plain merge of two neighbours", not at_fault and not message,
      (sorted(at_fault), message))

# A group becomes one side of a Coons patch, so it has to be a contiguous arc.
# `1,2,1` is not a patch -- and it is exactly what a free numbering makes easy
# to type, which is why it is reported rather than prevented.
at_fault, message = sidematch.group_problems(five, numbered(five, {2: 1}))
check("a number used in two separate places is reported",
      "separate places" in message, message)
# Both runs, not just the second: which of the two to renumber is the user's
# choice, and ringing only one would answer it for them.
check("and every side of both runs is named", at_fault == {0, 2}, sorted(at_fault))

# One closed side, which no generator accepts -- `find_generator` starts at
# Wedge 2.
at_fault, message = sidematch.group_problems(
    five, numbered(five, {0: 1, 1: 1, 2: 1, 3: 1, 4: 1}))
check("a boundary left with one group is reported",
      "at least two groups" in message, message)
check("and the whole loop is named", at_fault == {0, 1, 2, 3, 4}, sorted(at_fault))

# Gaps are not a fault. `1,1,3,4` is three connected runs and a perfectly good
# three-sided patch; complaining would be nagging about nothing.
at_fault, message = sidematch.group_problems(five, numbered(five, {1: 1, 2: 3, 3: 4, 4: 5}))
check("a gap in the numbering is not a fault", not at_fault and not message,
      (sorted(at_fault), message))

# One loop's fault must not implicate the other's sides.
at_fault, message = sidematch.group_problems(band, numbered(band, {2: 1}))
check("a fault on one loop names only that loop's sides",
      at_fault == {0, 2}, sorted(at_fault))
# The same number on *different* loops is two separate boundaries agreeing on a
# label, which means nothing and must not be reported.
at_fault, message = sidematch.group_problems(band, numbered(band, {}))
check("the two loops both using 1, 2, 3 is not a fault",
      not at_fault and not message, (sorted(at_fault), message))

sidematch.set_side_groups(state, {})

# ---------------------------------------------------------------------------
#  Where the bubble sits
# ---------------------------------------------------------------------------
# Shared with the hit test on purpose: two versions of "the middle of this
# side" would drift apart on a curved boundary, and a bubble you cannot click
# where it is drawn is worse than no bubble.
straight = [mathutils.Vector((0.0, 0.0, 0.0)), mathutils.Vector((4.0, 0.0, 0.0))]
check("the anchor is half way along a straight side",
      sidematch.side_midpoint(straight) == mathutils.Vector((2.0, 0.0, 0.0)),
      sidematch.side_midpoint(straight))
# By arc length, not by index: a side tessellated densely at one end would
# otherwise put its bubble in that end.
lopsided = [mathutils.Vector((0.0, 0.0, 0.0)), mathutils.Vector((0.1, 0.0, 0.0)),
            mathutils.Vector((0.2, 0.0, 0.0)), mathutils.Vector((4.0, 0.0, 0.0))]
check("and by arc length rather than by index",
      abs(sidematch.side_midpoint(lopsided).x - 2.0) < 1e-6,
      sidematch.side_midpoint(lopsided))
check("an empty side has no anchor rather than raising",
      sidematch.side_midpoint([]) is None)

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
    "cycle_group": bpy.ops.retop.toggle_corner.poll,
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
      not {"cycle_group", "accept", "cancel"} & before, sorted(before))

state.hovered_side = -1
check("pointing at no side, Ctrl+click is the density copy again",
      "copy" in live() and "edit_corners" not in live(), sorted(live()))

state.corner_edit = True
state.hovered_bubble = 2
during = live()
check("open, it owns Enter, Esc and the click",
      not {"commit", "back", "pin", "copy", "delete"} & during, sorted(during))
check("and the spans with them", not {"span", "ngon"} & during, sorted(during))
check("its own three are live",
      {"cycle_group", "accept", "cancel"} <= during, sorted(during))
# Read *while* choosing groups, which is the whole reason they opt back in.
check("the CAD structure displays stay available",
      {"cad_edges", "surface_flow"} <= during, sorted(during))

state.hovered_bubble = -1
check("with no bubble under the cursor there is nothing to step",
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


for key, ctrl, expected in (('LEFTMOUSE', False, "corner_toggle"),
                            ('LEFTMOUSE', True, "corner_toggle_back"),
                            ('RET', False, "corners_accept"),
                            ('ESC', False, "corners_cancel")):
    state.corner_edit = True
    state.hovered_bubble = 2
    bound = keymap.session_action_for(Event(key, ctrl))
    check(f"{'Ctrl+' if ctrl else ''}{key} is the editor's while it is open",
          bound == expected, bound)

state.corner_edit = False
state.hovered_side = 2
state.hovered_bubble = -1
check("closed, the left click is the side picker's again",
      keymap.session_action_for(Event('LEFTMOUSE')) == "pin_neighbour",
      keymap.session_action_for(Event('LEFTMOUSE')))
check("and Ctrl+click opens the editor rather than stepping a group",
      keymap.session_action_for(Event('LEFTMOUSE', ctrl=True)) == "corners_edit",
      keymap.session_action_for(Event('LEFTMOUSE', ctrl=True)))
check("and Esc discards the patch",
      keymap.session_action_for(Event('ESC')) == "back",
      keymap.session_action_for(Event('ESC')))

# ---------------------------------------------------------------------------
#  Open, step, cancel
# ---------------------------------------------------------------------------
state.side_groups = ""
bpy.ops.retop.edit_corners()
check("the editor opens", state.corner_edit)

state.hovered_bubble = 1
bpy.ops.retop.toggle_corner(delta=1)
check("a click steps the side's number",
      sidematch.group_numbers(five, state)[1] == 3,
      sidematch.group_numbers(five, state))
bpy.ops.retop.toggle_corner(delta=-1)
check("and Ctrl+click steps it back",
      sidematch.group_numbers(five, state)[1] == 2,
      sidematch.group_numbers(five, state))

# Wraps rather than clamping: a click that does nothing at the end of the range
# reads as a broken control. The ceiling is the loop's own side count, since
# more groups than sides is not something a boundary can be cut into.
for _ in range(4):
    bpy.ops.retop.toggle_corner(delta=1)
check("stepping past the last group wraps to the first",
      sidematch.group_numbers(five, state)[1] == 1,
      sidematch.group_numbers(five, state))
check("which is a real merge, so the patch is four-sided now",
      sidematch.loop_group_counts(five, sidematch.group_numbers(five, state)) == {0: 4},
      sidematch.loop_group_counts(five, sidematch.group_numbers(five, state)))

# An unusable grouping is kept, not refused -- and the warning is what says so.
# Sides 0 and 1 are group 1 after the wrap above; putting side 3 there too
# makes group 1 appear in two separate places, with side 2 between them.
state.hovered_bubble = 3
bpy.ops.retop.toggle_corner(delta=2)
check("a grouping that cannot be built is allowed",
      sidematch.group_numbers(five, state)[3] == 1,
      sidematch.group_numbers(five, state))
check("and reported instead", "separate places" in state.group_warning,
      state.group_warning)

bpy.ops.retop.corners_cancel()
check("Esc closes the editor", not state.corner_edit)
check("puts back the grouping it was opened with", state.side_groups == "",
      state.side_groups)
check("and clears the warning with it", state.group_warning == "", state.group_warning)

bpy.ops.retop.edit_corners()
state.hovered_bubble = 3
bpy.ops.retop.toggle_corner(delta=-1)
bpy.ops.retop.corners_accept()
check("Enter closes it keeping the change",
      not state.corner_edit and sidematch.group_numbers(five, state)[3] == 3,
      state.side_groups)

# The complaint has to outlive the editor: an unusable grouping is kept, so the
# panel goes on saying it cannot be built. Refusing to close would be the modal
# nobody can get out of.
bpy.ops.retop.edit_corners()
state.hovered_bubble = 2
bpy.ops.retop.toggle_corner(delta=-2)
standing = state.group_warning
bpy.ops.retop.corners_accept()
check("a warning survives the editor closing on it",
      bool(standing) and state.group_warning == standing, state.group_warning)
bpy.ops.retop.edit_corners()
bpy.ops.retop.corners_cancel()

# ---------------------------------------------------------------------------
#  What is *not* wired up yet
# ---------------------------------------------------------------------------
# Said out loud so a green suite is not read as "five-sided patches are filled
# as quads now". The grouping is chosen, checked and drawn; `_generate_for_face`
# still hands the generator the ungrouped sides.
source = open(os.path.join(_ADDON_DIR, "operators.py"), encoding="utf-8").read()
check("the generators do not read the grouping yet -- next step",
      "group_runs" not in source.split("def _generate_for_face", 1)[-1][:4000])

sidematch._active_sides = []
state.session_active = False

print()
if FAILURES:
    print(f"=== {len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(1)
print("=== ALL CHECKS PASSED")
