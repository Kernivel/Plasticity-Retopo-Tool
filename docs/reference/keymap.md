# Keymap

Every key below is a **normal Blender keymap item** on a normal operator. Change
them in the addon's preferences — the panel's **Keybinds** tab has a button that
opens the page — or find them under
`Preferences > Keymap > Add-ons > 3D View`.

They are ordinary rows: click the key field, press a new key, and the restore
arrow puts one back.

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

Wheel without <kbd>Ctrl</kbd> still zooms.

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

In **Adjust** on a single-span generator it is still swallowed — a patch is open
and letting Blender toggle Edit Mode would take the session out from under it —
but it **reports** that rather than doing nothing. A key that does nothing and
says nothing reads as a captured key.

## Not remappable

- **The digits and <kbd>Backspace</kbd>** — numeric entry, not a shortcut. They
  must stay instantaneous and only make sense as a block.
- **<kbd>Alt</kbd>+<kbd>X</kbd> then an axis** — a key *sequence*, which
  Blender's keymap cannot express.

A left click only falls back to committing when there is no side under the
cursor. Taking the side is a normal binding like any other.

## Outside a session

**No key of the addon's is live outside a session.** The three global ones
(<kbd>/</kbd>, <kbd>Alt</kbd>+<kbd>X</kbd>, <kbd>V</kbd>) are
all keys something else wants — Hard Ops binds `Alt+X`, and <kbd>/</kbd> is
Blender's own isolate — and an addon that has to be *disabled* to give a key back
is not self-contained.

With no session open, those events are handed straight on: Blender's own binding,
or the other addon's, runs unchanged.

**Keep Global Keys Outside a Session** is an addon *preference* (per user, not
per file) for anyone who wants the isolate and the mirror between sessions.

Panel buttons are unaffected — the mirror's UI is bound to nothing and works
whenever there is a result mesh.

## Why the modal dispatches them

Session keys are resolved by the modal operator itself rather than being left to
fall through to the keymap. A keymap item in *3D View* does not reliably beat one
in a *mode* keymap, and the session's keys collide with those constantly:
<kbd>X</kbd> is `object.delete` in Object Mode, <kbd>Tab</kbd> is
`object.editmode_toggle` in Object Non-modal.

That was not a cosmetic failure. With <kbd>X</kbd> falling through, pressing it on
a patch that turned out not to be committed reached `object.delete` and **took the
CAD object with it**.

The modal sits above every keymap, so dispatching there always wins — and the
items stay real, so Blender's rows edit them and your preferences save them. The
two keys Blender claims (<kbd>X</kbd>, <kbd>Tab</kbd>) are consumed even when the
action refuses, and it says why; everything else falls through on purpose, so
<kbd>N</kbd> outside Adjust still opens the sidebar.

The viewport hints at the bottom read the **live** items, so they follow a
remapping rather than saying what the default was.
