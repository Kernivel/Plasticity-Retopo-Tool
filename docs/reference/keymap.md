# Keymap

Every key below is a **normal Blender keymap item** on a normal operator. Change
them in the addon's preferences — the panel's **Keybinds** tab has a button that
opens the page — or find them under
`Preferences > Keymap > Add-ons > 3D View`.

## While adjusting a patch

| Key | Action |
|---|---|
| <kbd>Ctrl</kbd> + wheel | Span up/down (or N-gon detail angle) |
| <kbd>0</kbd>–<kbd>9</kbd>, <kbd>Backspace</kbd> | Type a span directly |
| <kbd>Tab</kbd> | Switch U/V (quad and wedge patches) |
| <kbd>N</kbd> | N-gon mode |
| <kbd>M</kbd> | Side highlight on/off |
| Click a side | Match the committed neighbour across it |
| Click a matched side | Turn that match off |
| <kbd>Ctrl</kbd> + click a done patch | Copy its density (same generator only) |
| <kbd>Ctrl</kbd> + click it again | Take it with U and V exchanged |
| <kbd>X</kbd> | Delete the patch (re-edit only) |
| Right click / <kbd>Enter</kbd> / click on no side | Commit |
| <kbd>Esc</kbd> | Clear typing, then discard |

## While picking a surface

| Key | Action |
|---|---|
| Click | Pick a surface — again on a done one to re-edit it |
| <kbd>Shift</kbd> + click | Add a surface to the next patch, or take it back out |
| Click a picked surface | Open all the picked surfaces as one patch |
| <kbd>Tab</kbd> | Hand-edit the mesh |
| <kbd>Esc</kbd> | Drop the picked surfaces, then leave the object |

## Any phase

| Key | Action |
|---|---|
| <kbd>E</kbd> | Plasticity edges on/off |
| <kbd>Ctrl</kbd> + <kbd>E</kbd> | Surface flow on/off |
| <kbd>Alt</kbd> + <kbd>X</kbd>, then <kbd>X</kbd>/<kbd>Y</kbd>/<kbd>Z</kbd> | Mirror the retopology on that axis |
| <kbd>V</kbd> | Draw the retopology through everything on/off |
| <kbd>/</kbd> | Isolate, retopology included |
| <kbd>Ctrl</kbd> + <kbd>Z</kbd> | Blender's undo — one step per committed patch |

## Three keys, one <kbd>Tab</kbd>

<kbd>Tab</kbd> is bound to three actions with mutually exclusive conditions, and
the phase decides which runs:

| Phase | <kbd>Tab</kbd> does |
|---|---|
| **Adjust** | switch U/V |
| **Patch** | open the hand-edit trip |
| **Object** | open the *selected* object's retopology for hand-editing |
| **Tweak** | come back from it |
| *no session* | Blender's own Edit Mode toggle |


## Not remappable

- **The digits and <kbd>Backspace</kbd>** — numeric entry, not a shortcut. They
  must stay instantaneous and only make sense as a block.
- **<kbd>Alt</kbd>+<kbd>X</kbd> then an axis** — a key *sequence*, which
  Blender's keymap cannot express.

A left click only falls back to committing when there is no side under the
cursor. Taking the side is a normal binding like any other.

## Outside a session

**No key of the addon's is live outside a session.** 

With no session open, those events are handed straight on: Blender's own binding,
or the other addon's, runs unchanged.

