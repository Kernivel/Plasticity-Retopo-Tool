"""Finding the Plasticity bridge addon, and drawing *its own* panel inside ours.

The bridge (https://github.com/nkallen/plasticity-blender-addon, MIT,
(c) 2023 Plastic Software, LLC) is what puts `mesh["groups"]` and
`mesh["face_ids"]` on an imported mesh -- the whole input contract this addon
reads. Nothing here talks to it at runtime, but the first thing a user does
after changing the part in Plasticity is a *Refresh* in its panel, which means
leaving the Retop tab for another one and coming back.

**Nothing is vendored and nothing is reimplemented.** A bundled copy would
register the same classes the user's own installation does -- the bl_idnames
are fixed (`wm.connect_button`, `wm.list`, `OBJECT_PT_plasticity_panel`) and so
are a dozen `bpy.types.Scene.prop_plasticity_*` -- so whichever of the two
unregisters first would strip the other's scene properties out from under it.
That is the "already registered as a subclass" restart-Blender failure
`__init__.register`'s unwind exists to make survivable, walked into on purpose.
Bundling would not even buy what it looks like it buys: the protocol is
negotiated with *Plasticity itself* at the handshake (the client fills
`supported_messages` from what the server advertises), so a frozen client is
the one that stops speaking when Plasticity updates, not the user's.

So the bridge is **detected and delegated to**. `draw_bridge_panel` calls the
bridge's own `PlasticityPanel.draw` with a layout of our choosing, which is a
1:1 copy in the strict sense -- the same code drawing the same rows, Connect /
Server / Disconnect / Refresh included, following their updates for free. It
is safe to do because that `draw` touches `self` only through `self.layout`:
everything else it reads comes from `context.scene` and its own module-level
client.

What this module will *not* do is assume anything about the bridge beyond
`bl_info`. Every read goes through `getattr` with a default, because a
property has to be able to disappear in a later version without taking a panel
draw down with it -- a `draw` that raises leaves a blank panel and no message,
which reads exactly like a feature that does not work.
"""
import sys
import time
from types import ModuleType
from typing import Any, NamedTuple

import bpy

# What the bridge calls itself in `bl_info`. The *package* name cannot be
# matched on: it is whatever folder the user installed it under --
# `plasticity-blender-addon`, `plasticity-blender-addon-main` from a zip of the
# default branch, or `bl_ext.<repo>.<name>` as a 4.2+ extension.
BRIDGE_ADDON_NAME = "Plasticity"

# The bridge this addon has been used against. Reported, never enforced: the
# input contract is `groups` / `face_ids`, and a bridge that keeps it works
# whatever its version number says. A version check that *blocked* would turn
# every bridge update into a broken addon, which is the failure mode the check
# is supposed to warn about.
TESTED_VERSION = (2, 2, 1)

# Scanning is a dict walk over `sys.modules`, and a panel draws on every mouse
# move across it. Cheap, but not free, and the answer changes only when an
# addon is enabled or disabled -- so a miss is remembered for this long, and
# the bridge becoming available mid-session heals itself within it.
_SCAN_TTL_SECONDS = 2.0

_cached_name: str | None = None
_last_scan: float = 0.0


class BridgeStatus(NamedTuple):
    """What the bridge is doing, as far as it can be read from outside it.

    `installed` is the only field that can be trusted on its own: the rest come
    back at their defaults both when the bridge is absent and when a future
    version has renamed them, which are deliberately the same answer here --
    neither is something a panel can act on.
    """
    installed: bool
    version: tuple[int, ...] | None
    connected: bool
    subscribed: bool
    filename: str | None
    server: str | None


def forget() -> None:
    """Drop the memoised lookup.

    Needed by the tests, which install and remove a stand-in bridge faster than
    the TTL. Not needed in Blender: enabling or disabling an addon is picked up
    by the TTL on its own.
    """
    global _cached_name, _last_scan
    _cached_name = None
    _last_scan = 0.0


def _looks_like_bridge(candidate: ModuleType | None) -> bool:
    """Two facts, because neither is sufficient.

    `bl_info["name"]` is the bridge's own claim to be the bridge, and it is
    what an addon is identified by everywhere else in Blender. But this
    addon's own package name also contains "plasticity", as does every one of
    the bridge's submodules, so the name filter alone matches things that are
    not it. `plasticity_client` is the module attribute everything else here
    reads through, so requiring it means a match is also usable.
    """
    info = getattr(candidate, "bl_info", None)
    if not isinstance(info, dict) or info.get("name") != BRIDGE_ADDON_NAME:
        return False
    return hasattr(candidate, "plasticity_client")


def _scan() -> str | None:
    own_root = (__package__ or "").split(".")[0]
    for name, candidate in list(sys.modules.items()):
        if "plasticity" not in name.lower():
            continue
        if own_root and (name == own_root or name.startswith(own_root + ".")):
            continue
        if _looks_like_bridge(candidate):
            return name
    return None


def module() -> ModuleType | None:
    """The bridge's top-level module, or None when it isn't enabled.

    The cached name is re-validated rather than trusted: disabling the bridge
    leaves its entry in `sys.modules` in some Blender versions and removes it
    in others, and reloading it replaces the object. Checking that what we hand
    back still looks like the bridge costs one `getattr` and makes both
    behaviours the same.
    """
    global _cached_name, _last_scan

    if _cached_name is not None:
        found = sys.modules.get(_cached_name)
        if _looks_like_bridge(found):
            return found
        _cached_name = None

    now = time.monotonic()
    if (now - _last_scan) < _SCAN_TTL_SECONDS:
        return None
    _last_scan = now

    _cached_name = _scan()
    return sys.modules.get(_cached_name) if _cached_name else None


def _version_of(found: ModuleType) -> tuple[int, ...] | None:
    info = getattr(found, "bl_info", None)
    raw = info.get("version") if isinstance(info, dict) else None
    if not isinstance(raw, (tuple, list)):
        return None
    parts = tuple(int(part) for part in raw if isinstance(part, int))
    return parts or None


def status() -> BridgeStatus:
    found = module()
    if found is None:
        return BridgeStatus(False, None, False, False, None, None)

    client: Any = getattr(found, "plasticity_client", None)
    return BridgeStatus(
        installed=True,
        version=_version_of(found),
        connected=bool(getattr(client, "connected", False)),
        subscribed=bool(getattr(client, "subscribed", False)),
        filename=getattr(client, "filename", None) or None,
        server=getattr(client, "server", None) or None,
    )


def version_string(version: tuple[int, ...] | None) -> str:
    return ".".join(str(part) for part in version) if version else "unknown"


def version_note() -> str | None:
    """A sentence about the installed bridge's version, or None when there is
    nothing worth saying.

    Silent on the tested version: a note that draws on every redraw to say only
    "this is fine" is a note nobody reads on the day it stops saying that. A
    *newer* bridge is not a fault either -- it is untested, and saying which of
    the two it is, is the honest distinction. Nothing here blocks: the contract
    is `groups` / `face_ids`, and a bridge that keeps it works whatever its
    version says.
    """
    state = status()
    if not state.installed:
        return None
    tested = version_string(TESTED_VERSION)
    if state.version is None:
        return (f"This bridge does not report a version. Retop reads the data written "
                f"by bridge {tested}.")
    found = version_string(state.version)
    if state.version[:1] > TESTED_VERSION[:1]:
        return (f"Bridge {found} is a major version ahead of the tested {tested}. If "
                f"patches come out belonging to the wrong face, the import contract "
                f"is what changed.")
    if state.version > TESTED_VERSION:
        return f"Bridge {found} is newer than the tested {tested}. Untested, not known to be wrong."
    if state.version[:1] < TESTED_VERSION[:1]:
        return f"Bridge {found} is older than the tested {tested}."
    return None


class _PanelProxy:
    """Stands in for a `bpy.types.Panel` instance so the bridge's own `draw`
    can be given a layout of our choosing.

    Passing our panel instance straight through would work and be wrong:
    `self.layout` is then the whole N-panel, so the bridge's rows would land
    outside the box they belong in and any nesting we do around them would be
    ignored.
    """

    def __init__(self, layout: bpy.types.UILayout) -> None:
        self.layout = layout


def bridge_panel_class() -> Any:
    found = module()
    if found is None:
        return None
    ui = getattr(found, "ui", None)
    if ui is None:
        ui = sys.modules.get(f"{found.__name__}.ui")
    return getattr(ui, "PlasticityPanel", None)


def draw_bridge_panel(layout: bpy.types.UILayout, context: bpy.types.Context) -> str | None:
    """Draw the bridge's own panel body into `layout`. Returns why it couldn't,
    or None when it did.

    The exception is caught and *returned* rather than raised, because this is
    called from a panel draw: one that raises aborts the rest of the panel and
    prints to a console nobody has open, so the tab would simply be missing its
    lower half with nothing saying why. The one thing this cannot undo is the
    rows the bridge had already emitted before it raised -- so the caller
    reports the failure underneath them, which is where the gap is.
    """
    panel = bridge_panel_class()
    if panel is None:
        return "The Plasticity bridge is installed but its panel could not be read."
    try:
        panel.draw(_PanelProxy(layout), context)
    except Exception as exc:  # a panel draw may not raise; see above
        return f"The bridge's panel failed to draw: {type(exc).__name__}: {exc}"
    return None
