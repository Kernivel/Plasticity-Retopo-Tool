"""Which key does what — declared here, owned by Blender.

Every key is a real `KeyMapItem`, and Blender owns editing and saving it.
This module only declares what to register.

`GLOBAL` actions are dispatched by Blender. `SESSION` actions are dispatched by
the modal, which reads the live items: a 3D View keymap item does not reliably
beat a mode keymap (`X` is `object.delete` in Object Mode).
See "Keys" in CLAUDE.md.

Outside the keymap: the digits and Backspace (numeric entry), and the mirror's
`Alt+X` then `X`/`Y`/`Z` (a key sequence). The click fallback that commits when
nothing is under the cursor stays in the modal.
"""
import bpy


def _b(key: str, ctrl: bool = False, shift: bool = False, alt: bool = False) -> dict[str, object]:
    return {"type": key, "ctrl": ctrl, "shift": shift, "alt": alt}


SESSION = 'SESSION'   # dispatched by the modal, which always wins
GLOBAL = 'GLOBAL'     # dispatched by Blender: must work with no session

# (id, label, scope, operator, operator properties, default bindings)
#
# `id` names the action for the overlay hints and the preferences page.
# Two entries can share an operator and differ by their properties.
ACTIONS: tuple[tuple[str, str, str, str, dict[str, object], list[dict[str, object]]], ...] = (
    ("span_more", "Span +", SESSION, "retop.nudge_span", {"delta": 1},
     [_b('WHEELUPMOUSE', ctrl=True)]),
    ("span_less", "Span -", SESSION, "retop.nudge_span", {"delta": -1},
     [_b('WHEELDOWNMOUSE', ctrl=True)]),
    ("span_axis", "U / V direction", SESSION, "retop.toggle_span_axis", {}, [_b('TAB')]),
    ("ngon_mode", "N-gon mode", SESSION, "retop.toggle_ngon", {}, [_b('N')]),
    ("match_mode", "Side highlight", SESSION, "retop.toggle_match_mode", {}, [_b('M')]),
    # The corner editor's actions come first: the first action whose poll
    # passes wins. They poll on the editor being open, and the actions below
    # poll on it being closed. Both are needed.
    ("corner_toggle", "Group +", SESSION, "retop.toggle_corner", {"delta": 1},
     [_b('LEFTMOUSE')]),
    ("corner_toggle_back", "Group -", SESSION, "retop.toggle_corner", {"delta": -1},
     [_b('LEFTMOUSE', ctrl=True)]),
    ("corners_accept", "Keep corner set", SESSION, "retop.corners_accept", {},
     [_b('RET'), _b('NUMPAD_ENTER'), _b('RIGHTMOUSE')]),
    ("corners_cancel", "Cancel corner edit", SESSION, "retop.corners_cancel", {},
     [_b('ESC')]),
    # Ctrl+click on a side opens the corner editor; on a patch it copies its
    # density (`copy_spans`).
    ("corners_edit", "Edit corners", SESSION, "retop.edit_corners", {},
     [_b('LEFTMOUSE', ctrl=True)]),
    # Clicking a matched side releases it; clicking a released one matches it.
    ("pin_neighbour", "Match side", SESSION, "retop.pin_side",
     {}, [_b('LEFTMOUSE')]),
    # Modifiers are compared exactly (`_matches`), so this never fires on a
    # plain click.
    ("copy_spans", "Copy patch density", SESSION, "retop.copy_patch_spans", {},
     [_b('LEFTMOUSE', ctrl=True)]),
    # Shift+click gathers several surfaces into one patch.
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
    # `V`, never another `X` combination: `X` is already taken twice.
    ("see_through", "Retopo X-ray", GLOBAL, "retop.toggle_see_through", {},
     [_b('V')]),
)

ACTION_IDS = tuple(entry[0] for entry in ACTIONS)
_BY_ID = {entry[0]: entry for entry in ACTIONS}

# action id -> the KeyMapItems registered for it, filled by
# `operators._register_keymaps`. Here so the overlay never imports `operators`.
_registered: dict[str, list[bpy.types.KeyMapItem]] = {}

# Event type -> on-screen name, where the raw name is unhelpful.
KEY_LABELS = {
    # Both Enters read "Enter"; `describe_all` de-duplicates them.
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
    # Copied: the module-level default is shared.
    return [dict(binding) for binding in entry[5]] if entry else []


def _matches(kmi: bpy.types.KeyMapItem, event: object) -> bool:
    """Whether `event` is this item being pressed.

    Modifiers are compared exactly: bare `X` must not fire on `Alt+X`.
    `kmi.any` is honoured.
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

    Several actions can share a key (three share `TAB`), with mutually
    exclusive polls. The caller runs the first whose poll passes, never merely
    the first match.
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
    # Nothing registered (--background): fall back to the declaration.
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

    Asks the operator's poll, which is where the phase logic lives.
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

    The one whose poll passes, else the first match, so a refusal can name the
    action the user meant.
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

    A wrapper can outlive its item when a keyconfig is rebuilt; reading it
    would raise inside a draw handler.
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

    Only the first: it feeds the one-line viewport hints. Falls back to the
    default in `--background`.
    """
    items = items_for(action_id)
    if items:
        return describe_item(items[0])
    defaults = default_bindings(action_id)
    return describe_binding(defaults[0]) if defaults else "unbound"


def describe_all(action_id: str) -> list[str]:
    """Every binding of an action, for the one hint that lists them.

    De-duplicated by how each one reads, so the two Enters appear once.
    """
    items = items_for(action_id)
    described = ([describe_item(kmi) for kmi in items] if items
                 else [describe_binding(b) for b in default_bindings(action_id)])
    return list(dict.fromkeys(described))


def preferences() -> object | None:
    """The addon's preferences entry, or None when there is no addon entry.

    Read off the context, never by importing `prefs`: this module must stay a
    leaf. None in the tests and in `--background`.
    """
    try:
        addon = bpy.context.preferences.addons.get(__package__)
    except AttributeError:
        return None
    return getattr(addon, "preferences", None) if addon else None


def global_keys_outside_session() -> bool:
    """Whether the GLOBAL keys mean anything with no session running.

    Off by default: with no session, these operators' polls fail and the key
    goes to Blender or another addon. An addon preference.
    """
    return bool(getattr(preferences(), "global_keys_outside_session", False))
