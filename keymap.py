"""Which key does what — declared here, owned by Blender.

The session's keys were `event.type == 'X'` comparisons inside `operators._modal`.
That leaves nothing to remap and nothing in Blender's keymap editor to find
either, because a modal operator reads raw events and sits *above* every
keymap. Making them remappable by hand meant a table, a capture modal and a
panel to draw it in — a second, worse keymap editor living next to Blender's.

So they are real `KeyMapItem`s on real operators now, and Blender owns all of
it: the editing UI, the conflict display, the per-item restore, the
persistence in the user's preferences. This module only *declares* what to
register. The addon's preferences page draws them with `rna_keymap_ui.draw_kmi`
— the same rows the keymap editor uses — and the panel's Keybinds tab is a
button that opens it.

**Who dispatches them is a second question, and the answer differs by scope.**
`GLOBAL` actions are dispatched by Blender, like any keymap item, because they
must work with no session running. `SESSION` actions are dispatched by the
modal, which reads the live items and runs the matching operator itself.

That is not belt-and-braces. A key in the 3D View keymap does not reliably beat
one in a *mode* keymap, and the session's keys collide with mode keymaps
constantly -- `X` is `object.delete` in Object Mode, `Tab` is
`object.editmode_toggle` in Object Non-modal. Registering each action in
whichever keymap owns its competitor is unmaintainable and still leaves the
next addon to claim the key; MACHIN3 puts its own `Alt+X` in `Mesh` rather than
`3D View` for exactly this reason. The modal sits above every keymap, so
dispatching from there always wins, and the items stay real -- editable in
Blender's rows, listed in the keymap editor, saved in the user's preferences.

The failure it prevents is not cosmetic: with `X` falling through, deleting a
patch that turned out not to be committed reached `object.delete` and took the
CAD object with it.

Two things stay outside all of this:

- **the digits and Backspace**, numeric entry rather than a shortcut: they must
  stay instantaneous and they only make sense as a block;
- **the mirror's `Alt+X` then `X`/`Y`/`Z`**, a key *sequence*, which Blender's
  keymap has no notion of.

The left click is *half* outside. Taking the side under the cursor is a normal
binding (`pin_neighbour`); what stays in the modal is the
fallback when there is no side under the cursor, which commits — that one
genuinely depends on the hover, and the modal hands the event over whenever it
doesn't apply.
"""
import bpy


def _b(key: str, ctrl: bool = False, shift: bool = False, alt: bool = False) -> dict[str, object]:
    return {"type": key, "ctrl": ctrl, "shift": shift, "alt": alt}


SESSION = 'SESSION'   # dispatched by the modal, which always wins
GLOBAL = 'GLOBAL'     # dispatched by Blender: must work with no session

# (id, label, scope, operator, operator properties, default bindings)
#
# `id` names the action for the overlay hints and the preferences page; it is
# not a Blender concept. Two entries can share an operator and differ by their
# properties, which is how one `nudge_span` covers both wheel directions.
ACTIONS: tuple[tuple[str, str, str, str, dict[str, object], list[dict[str, object]]], ...] = (
    ("span_more", "Span +", SESSION, "retop.nudge_span", {"delta": 1},
     [_b('WHEELUPMOUSE', ctrl=True)]),
    ("span_less", "Span -", SESSION, "retop.nudge_span", {"delta": -1},
     [_b('WHEELDOWNMOUSE', ctrl=True)]),
    ("span_axis", "U / V direction", SESSION, "retop.toggle_span_axis", {}, [_b('TAB')]),
    ("ngon_mode", "N-gon mode", SESSION, "retop.toggle_ngon", {}, [_b('N')]),
    ("match_mode", "Side highlight", SESSION, "retop.toggle_match_mode", {}, [_b('M')]),
    # Taking the side under the cursor is an action like any other, and there
    # was never a reason for it to be fixed in the modal -- only the *fallback*
    # (nothing under the cursor, so commit) genuinely depends on the hover.
    #
    # One click, one meaning. Matching is binary -- the side reproduces the
    # neighbour across it or it does not -- so there is no second gesture and
    # no modifier: clicking a matched side releases it, clicking a released one
    # matches it again.
    # The corner editor takes the same three gestures the patch does, and gets
    # them by declaring them *first*: `session_actions_for` returns every
    # action bound to an event and `_dispatch_bound` runs the first whose poll
    # passes, so a sub-mode wins simply by polling on itself while the actions
    # below poll on it being off. Both halves are written out -- these come
    # first *and* the others exclude it -- because "it works by an order
    # nothing states" is what sent `Tab` to `object.editmode_toggle`.
    # Two items on one operator, differing only by `delta` -- the same shape as
    # the span wheel's pair, and the reason `_registered` keys on the action
    # rather than the idname.
    ("corner_toggle", "Group +", SESSION, "retop.toggle_corner", {"delta": 1},
     [_b('LEFTMOUSE')]),
    ("corner_toggle_back", "Group -", SESSION, "retop.toggle_corner", {"delta": -1},
     [_b('LEFTMOUSE', ctrl=True)]),
    ("corners_accept", "Keep corner set", SESSION, "retop.corners_accept", {},
     [_b('RET'), _b('NUMPAD_ENTER'), _b('RIGHTMOUSE')]),
    ("corners_cancel", "Cancel corner edit", SESSION, "retop.corners_cancel", {},
     [_b('ESC')]),
    # Ctrl+click already meant "take that patch's density", and it still does
    # -- pointing at a *patch*. Pointing at a *side* of the patch being
    # adjusted opens the corner editor instead, which is the same
    # what-is-under-the-cursor split the plain click already makes between
    # `pin_neighbour` and the commit fallback. Neither branch is destructive,
    # so a missed aim by a few pixels costs a re-click either way.
    ("corners_edit", "Edit corners", SESSION, "retop.edit_corners", {},
     [_b('LEFTMOUSE', ctrl=True)]),
    ("pin_neighbour", "Match side", SESSION, "retop.pin_side",
     {}, [_b('LEFTMOUSE')]),
    # Ctrl on the same button, and the modifier comparison is exact (`_matches`),
    # so the two never collide. It is the one gesture that reads as "take that
    # one's settings" without a mode to enter first.
    ("copy_spans", "Copy patch density", SESSION, "retop.copy_patch_spans", {},
     [_b('LEFTMOUSE', ctrl=True)]),
    # Shift on the same button, and in a phase where the plain click means
    # "open this one": gathering several surfaces to open as one patch is the
    # same gesture with the same target, which is what a modifier is for.
    # Exactly like `copy_spans`, the modifier comparison in `_matches` is exact,
    # so the plain pick never fires on it and this never fires on a plain click.
    ("add_surface", "Add surface to patch", SESSION, "retop.toggle_surface", {},
     [_b('LEFTMOUSE', shift=True)]),
    ("delete_patch", "Delete patch", SESSION, "retop.delete_patch", {}, [_b('X')]),
    ("commit", "Commit patch", SESSION, "retop.commit_patch", {},
     [_b('RET'), _b('NUMPAD_ENTER'), _b('RIGHTMOUSE')]),
    ("back", "Discard / back out", SESSION, "retop.back", {}, [_b('ESC')]),
    ("hand_edit", "Hand-edit mesh", SESSION, "retop.tweak_mesh", {}, [_b('TAB')]),
    ("end_tweak", "Back from hand-edit", SESSION, "retop.end_tweak", {}, [_b('TAB')]),
    ("cad_edges", "Plasticity edges", SESSION, "retop.toggle_cad_edges", {}, [_b('E')]),
    ("surface_flow", "Surface flow", SESSION, "retop.toggle_surface_flow", {},
     [_b('E', ctrl=True)]),
    ("local_view", "Isolate", GLOBAL, "retop.local_view", {},
     [_b('SLASH'), _b('NUMPAD_SLASH')]),
    ("mirror", "Mirror", GLOBAL, "retop.mirror", {}, [_b('X', alt=True)]),
    # `V`, not `Shift+X`. The mirror's `Alt+X` is the Hard Ops reflex and stays,
    # but hanging the x-ray off the same letter put it next to two things
    # Blender and this addon both claim `X` for -- and in practice the press
    # came out as `object.delete`'s confirmation popup rather than the toggle.
    # `V` is unbound in Object Mode and is where a user reaches for a
    # visibility toggle anyway. A binding the user has already changed is
    # theirs and is not overwritten: Blender keeps edited items in the
    # preferences, so this default only reaches a fresh install or a Restore.
    ("see_through", "Retopo X-ray", GLOBAL, "retop.toggle_see_through", {},
     [_b('V')]),
)

ACTION_IDS = tuple(entry[0] for entry in ACTIONS)
_BY_ID = {entry[0]: entry for entry in ACTIONS}

# action id -> the KeyMapItems registered for it, filled by
# `operators._register_keymaps`. Held here rather than in `operators` so the
# overlay can read a live binding without importing that module -- a draw
# handler must never pull in the operators, which import it back.
_registered: dict[str, list[bpy.types.KeyMapItem]] = {}

# Event type -> what to call it on screen. Only the ones whose raw name would
# be unhelpful; anything else falls back to the name with its underscores
# turned into spaces, which covers the letters and the F-keys.
KEY_LABELS = {
    # Both Enters read "Enter": which of the two keyboards it is under is not
    # something a hint has any reason to say, and `describe_all` de-duplicates
    # so the commit hint lists "Enter / R-Click" rather than Enter twice.
    'RET': "Enter", 'NUMPAD_ENTER': "Enter", 'ESC': "Esc",
    'BACK_SPACE': "Backspace", 'TAB': "Tab", 'SPACE': "Space",
    'LEFTMOUSE': "Click", 'RIGHTMOUSE': "R-Click", 'MIDDLEMOUSE': "M-Click",
    'WHEELUPMOUSE': "Wheel Up", 'WHEELDOWNMOUSE': "Wheel Down",
    'SLASH': "/", 'NUMPAD_SLASH': "Numpad /", 'BACK_SLASH': "\\",
    'COMMA': ",", 'PERIOD': ".", 'SEMI_COLON': ";", 'QUOTE': "'",
    'MINUS': "-", 'EQUAL': "=", 'GRLESS': "<",
    'LEFT_BRACKET': "[", 'RIGHT_BRACKET': "]",
}


def label_of(action_id: str) -> str:
    entry = _BY_ID.get(action_id)
    return entry[1] if entry else action_id


def scope_of(action_id: str) -> str:
    entry = _BY_ID.get(action_id)
    return entry[2] if entry else SESSION


def operator_of(action_id: str) -> str:
    entry = _BY_ID.get(action_id)
    return entry[3] if entry else ""


def properties_of(action_id: str) -> dict[str, object]:
    entry = _BY_ID.get(action_id)
    return dict(entry[4]) if entry else {}


def default_bindings(action_id: str) -> list[dict[str, object]]:
    entry = _BY_ID.get(action_id)
    # Copied: the caller may be about to edit what it is handed, and the
    # module-level default is shared by everything that asks.
    return [dict(binding) for binding in entry[5]] if entry else []


def _matches(kmi: bpy.types.KeyMapItem, event: object) -> bool:
    """Whether `event` is this item being pressed.

    Modifiers are compared *exactly*, not as a subset: a binding on bare `X`
    must not fire on `Alt+X`, which is the mirror. `kmi.any` is honoured
    because Blender's rows offer it and a user who ticks it means it.
    """
    if kmi.type != getattr(event, "type", None):
        return False
    if getattr(event, "value", None) != 'PRESS':
        return False
    if kmi.any:
        return True
    for name in ("ctrl", "shift", "alt", "oskey"):
        if bool(getattr(kmi, name)) != bool(getattr(event, name, False)):
            return False
    return True


def session_actions_for(event: object) -> list[str]:
    """Every SESSION action `event` asks for, in declaration order.

    More than one, because three actions share `TAB` on purpose -- U/V while
    adjusting, hand-edit while picking, back-from-hand-edit while editing --
    with mutually exclusive polls. Blender resolves that by running the first
    item whose poll passes, and the modal has to do the same rather than take
    the first *match*: taking the first match resolved every Tab to U/V, whose
    poll fails outside ADJUST, and the key then fell through to the keymap and
    was answered by whichever item happened to be registered first. It worked,
    but only by an ordering nothing states, and in the OBJECT phase it reached
    Blender's own `object.editmode_toggle` -- Edit Mode on the CAD object.
    """
    matched = []
    for action_id in ACTION_IDS:
        if scope_of(action_id) != SESSION:
            continue
        for kmi in items_for(action_id):
            if _matches(kmi, event):
                matched.append(action_id)
                break
    if matched or _registered:
        return matched
    # Nothing registered (no addon keyconfig, i.e. --background): fall back to
    # the declaration, so the dispatch is still testable headless.
    for action_id in ACTION_IDS:
        if scope_of(action_id) != SESSION:
            continue
        for binding in default_bindings(action_id):
            if (binding["type"] == getattr(event, "type", None)
                    and getattr(event, "value", None) == 'PRESS'
                    and all(bool(binding.get(m)) == bool(getattr(event, m, False))
                            for m in ("ctrl", "shift", "alt"))):
                matched.append(action_id)
                break
    return matched


def action_is_live(action_id: str) -> bool:
    """Whether this action's operator would run right now.

    Reads the operator rather than the phase: the poll *is* where the phase
    logic lives (see prefs.py), and duplicating it here is how the two would
    come to disagree.
    """
    idname = operator_of(action_id)
    if "." not in idname:
        return False
    operator = getattr(bpy.ops.retop, idname.split(".", 1)[1], None)
    if operator is None:
        return False
    try:
        return bool(operator.poll())
    except Exception:
        return False


def session_action_for(event: object) -> str | None:
    """The SESSION action `event` asks for, or None.

    The one whose poll passes, when several share the key; the first match
    otherwise, so a refusal can still be reported against the action the user
    meant. The modal dispatches these itself rather than letting them fall
    through to the keymap -- see the module docstring for why a keymap item
    cannot be relied on to win against a mode keymap.
    """
    matched = session_actions_for(event)
    for action_id in matched:
        if action_is_live(action_id):
            return action_id
    return matched[0] if matched else None


def remember(action_id: str, kmi: bpy.types.KeyMapItem) -> None:
    """Record a registered item so the overlay can read its live binding."""
    _registered.setdefault(action_id, []).append(kmi)


def forget_all() -> None:
    _registered.clear()


def items_for(action_id: str) -> list[bpy.types.KeyMapItem]:
    """The live KeyMapItems of an action, dropping any Blender has freed.

    A KeyMapItem's Python wrapper outlives the item when a keyconfig is rebuilt
    under it -- reading `.type` off one of those raises, and a draw handler is
    the worst place to find that out.
    """
    alive = []
    for kmi in _registered.get(action_id, []):
        try:
            _ = kmi.type
        except (ReferenceError, AttributeError):
            continue
        alive.append(kmi)
    return alive


def key_label(key: str) -> str:
    return KEY_LABELS.get(key, key.replace("_", " ").title() if len(key) > 1 else key)


def describe_binding(binding: dict[str, object]) -> str:
    """"Ctrl+Shift+X", from a declaration dict."""
    parts = []
    if binding.get("ctrl"):
        parts.append("Ctrl")
    if binding.get("shift"):
        parts.append("Shift")
    if binding.get("alt"):
        parts.append("Alt")
    parts.append(key_label(str(binding.get("type", ""))))
    return "+".join(parts)


def describe_item(kmi: bpy.types.KeyMapItem) -> str:
    """The same, from a live item -- so a remapped key reads as what it is now."""
    parts = []
    if kmi.ctrl:
        parts.append("Ctrl")
    if kmi.shift:
        parts.append("Shift")
    if kmi.alt:
        parts.append("Alt")
    if kmi.oskey:
        parts.append("OS")
    parts.append(key_label(kmi.type))
    return "+".join(parts)


def describe(action_id: str) -> str:
    """The first live binding of an action, falling back to its default.

    First rather than all of them: this feeds the viewport hint line, which is
    one row across the bottom of the screen and cannot afford to list three
    ways of committing. The fallback matters in `--background`, where there is
    no addon keyconfig at all and nothing was ever registered.
    """
    items = items_for(action_id)
    if items:
        return describe_item(items[0])
    defaults = default_bindings(action_id)
    return describe_binding(defaults[0]) if defaults else "unbound"


def describe_all(action_id: str) -> list[str]:
    """Every binding of an action, for the one hint that lists them.

    De-duplicated by what it *reads as*, not by which item it came from: the
    two Enters are one key as far as anyone reading a hint is concerned, and
    "Enter / Enter / R-Click" says nothing the shorter form doesn't.
    """
    items = items_for(action_id)
    described = ([describe_item(kmi) for kmi in items] if items
                 else [describe_binding(b) for b in default_bindings(action_id)])
    return list(dict.fromkeys(described))


def preferences() -> object | None:
    """The addon's preferences entry, or None when there is no addon entry.

    Read straight off the context rather than by importing `prefs`, which
    imports *this* module -- and this one has to stay a leaf the overlay can
    pull in. Returns None in the tests and in `--background`, where the package
    is imported plainly and has no addon entry to hang preferences on.
    """
    try:
        addon = bpy.context.preferences.addons.get(__package__)
    except AttributeError:
        return None
    return getattr(addon, "preferences", None) if addon else None


def global_keys_outside_session() -> bool:
    """Whether the GLOBAL keys mean anything with no session running.

    Off by default, and that default is what the GLOBAL/SESSION split costs
    otherwise: '/' , `Alt+X` and `V` are keys other addons bind too --
    Hard Ops above all, whose own `Alt+X` this one was modelled on -- and an
    addon that claims them from the moment it is installed is one that has to
    be *disabled* to get them back. A session running is the addon being used;
    with none, these operators' polls fail and Blender hands the key on to
    whoever else wants it, which is exactly what a failing poll does.

    A preference rather than a hard rule because the isolate and the mirror are
    genuinely useful between sessions, and the user who wants them back should
    not have to give up the addon to get them.
    """
    return bool(getattr(preferences(), "global_keys_outside_session", False))
