"""The addon's preferences page, which is where the keybinds live.

Drawn with `rna_keymap_ui.draw_kmi`, Blender's own keymap rows: the keys are
real `KeyMapItem`s (see keymap.py). Never build a second keymap editor.

The N-panel's Keybinds tab opens this page.
"""
import os

import bpy

from . import keymap

# Written into a deployed copy by scripts/deploy.py, and nothing else.
# Must match the literal in scripts/deploy.py (tests/test_deploy_marker.py).
DEV_MARKER_NAME = ".deployed"


def _addon_keymap_items() -> list[tuple[bpy.types.KeyMap, bpy.types.KeyMapItem]]:
    """The addon's registered items, newest registration first in ACTIONS order.

    Read from the registry `operators._register_keymaps` fills, never by idname:
    two items can share an operator.
    """
    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig is None:
        return []
    km = keyconfig.keymaps.get('3D View')
    if km is None:
        return []
    pairs = []
    for action_id in keymap.ACTION_IDS:
        for kmi in keymap.items_for(action_id):
            pairs.append((km, kmi))
    return pairs


def developer_mode() -> bool:
    """Whether the addon's own development affordances are shown.

    Off by default, and off in the tests and `--background` (no preferences).
    Always reads the setting, never the deploy marker, so it can be turned off.
    """
    prefs = keymap.preferences()
    return bool(getattr(prefs, "developer_mode", False))


def deploy_stamp() -> str:
    """What the last deploy into this copy wrote, or "" if it was not deployed.

    A stamp, not a flag: each deploy writes a new one.
    """
    marker = os.path.join(os.path.dirname(os.path.abspath(__file__)), DEV_MARKER_NAME)
    try:
        with open(marker, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        # No marker (a release zip), or unreadable.
        return ""


def seed_developer_mode() -> None:
    """Turn Developer Mode on when the code was just deployed from a checkout.

    Once per deploy stamp, so it can still be turned off until the next deploy.
    Silent when `keymap.preferences()` is None.
    """
    stamp = deploy_stamp()
    if not stamp:
        return
    prefs = keymap.preferences()
    if prefs is None or getattr(prefs, "dev_seed_stamp", None) == stamp:
        return
    try:
        prefs.dev_seed_stamp = stamp
        prefs.developer_mode = True
    except AttributeError:
        # Preferences from an older registration, mid-reload. Never fail
        # register() over this.
        pass


def draw_keymap(layout: bpy.types.UILayout) -> None:
    """The keybind rows. Shared by the preferences page and nothing else yet."""
    import rna_keymap_ui  # Blender ships it; imported lazily, it is UI-only

    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig is None:
        layout.label(text="No addon keyconfig in this Blender", icon='ERROR')
        return

    km = keyconfig.keymaps.get('3D View')
    if km is None or not keymap.items_for(keymap.ACTION_IDS[0]):
        layout.label(text="Keybinds are not registered", icon='ERROR')
        layout.label(text="Re-enable the addon, or use Reload Addon Only.")
        return

    column = layout.column()
    for action_id in keymap.ACTION_IDS:
        items = keymap.items_for(action_id)
        if not items:
            continue
        row = column.row()
        # A fixed-width split for the label, or the columns stretch.
        split = row.split(factor=0.25)
        split.label(text=keymap.label_of(action_id))
        body = split.column()
        for kmi in items:
            rna_keymap_ui.draw_kmi(
                ["ADDON", "USER", "DEFAULT"], keyconfig, km, kmi, body, 0)


class RETOP_AddonPreferences(bpy.types.AddonPreferences):
    # Must be the package name for Blender to attach this to the addon entry.
    bl_idname = __package__

    # Annotated, never assigned: that is how Blender registers a property.
    global_keys_outside_session: bpy.props.BoolProperty(
        name="Global Keys Outside a Session",
        description=("Keep Isolate ('/'), Mirror (Alt+X) and Retopo X-ray (V) live when no "
                     "retopology session is running. Off by default so the addon claims no key "
                     "unless it is being used -- with it off, those keys fall straight through to "
                     "Blender and to other addons (Hard Ops binds Alt+X too). The session's own "
                     "keys are never affected: they only ever exist while a session is open"),
        default=False,
    )

    # Not drawn. The stamp of the deploy that last switched Developer Mode on
    # (`seed_developer_mode`).
    dev_seed_stamp: bpy.props.StringProperty(default="")

    developer_mode: bpy.props.BoolProperty(
        name="Developer Mode",
        description=("Show the System tab's reload button and the stale-code warning. Reloading "
                     "is for working on the addon from a checkout, where the panel's version "
                     "string is the only way to tell a deploy actually took. An addon installed "
                     "from a release zip is reloaded by re-installing it, so the button is "
                     "hidden by default rather than offering a developer's workflow to everyone"),
        default=False,
    )

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        layout.label(text="Keybinds", icon='EVENT_A')
        box = layout.box()
        box.prop(self, "global_keys_outside_session")
        draw_keymap(layout)
        layout.separator()
        layout.label(text="Development", icon='CONSOLE')
        layout.box().prop(self, "developer_mode")


CLASSES = (RETOP_AddonPreferences,)


def register() -> None:
    for cls in CLASSES:
        try:
            bpy.utils.register_class(cls)
        except Exception:
            # A plain import (the tests) has no addon entry to attach to.
            pass
    # After the classes: it writes to the preferences it just registered.
    seed_developer_mode()


def unregister() -> None:
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
