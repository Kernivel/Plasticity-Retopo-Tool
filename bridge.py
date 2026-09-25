"""Finding the Plasticity bridge addon, and drawing *its own* panel inside ours.

The bridge (https://github.com/nkallen/plasticity-blender-addon, MIT,
(c) 2023 Plastic Software, LLC) writes `mesh["groups"]` and `mesh["face_ids"]`.
Its panel is drawn in a Retop tab so Refresh is at hand.

Never vendor or reimplement it: a bundled copy would register the same classes
as the user's own. See "The bridge's panel, inside ours" in CLAUDE.md.

`draw_bridge_panel` calls the bridge's own `PlasticityPanel.draw` with our
layout. Every read goes through `getattr` with a default, and a panel draw
never raises.
"""
import sys
import time
from types import ModuleType
from typing import Any, NamedTuple

import bpy

# What the bridge calls itself in `bl_info`. Never match on the package name,
# which depends on how it was installed.
BRIDGE_ADDON_NAME = "Plasticity"

# The bridge version this addon has been tested with. Reported, never enforced.
TESTED_VERSION = (2, 2, 1)

# How long a failed lookup is remembered. A panel draws on every mouse move.
_SCAN_TTL_SECONDS = 2.0

_cached_name: str | None = None
_last_scan: float = 0.0


class BridgeStatus(NamedTuple):
    """What the bridge is doing, as far as it can be read from outside it.

    Only `installed` is reliable on its own. The rest fall back to defaults
    when the bridge is absent or has renamed them.
    """
    installed: bool
    version: tuple[int, ...] | None
    connected: bool
    subscribed: bool
    filename: str | None
    server: str | None


def forget() -> None:
    """Drop the memoised lookup.

    For the tests, which swap a stand-in bridge faster than the TTL.
    """
    global _cached_name, _last_scan
    _cached_name = None
    _last_scan = 0.0


def _looks_like_bridge(candidate: ModuleType | None) -> bool:
    """`bl_info["name"]` must match, and the module must have
    `plasticity_client`: the name alone also matches other modules.
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

    The cached name is re-validated on every call, never trusted.
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

    Silent on the tested version and older ones within the same major.
    Never blocks anything.
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

    Passing our own panel would put the bridge's rows outside our box.
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

    Exceptions are returned as a sentence, never raised: this runs inside a
    panel draw. The caller prints it under the rows already drawn.
    """
    panel = bridge_panel_class()
    if panel is None:
        return "The Plasticity bridge is installed but its panel could not be read."
    try:
        panel.draw(_PanelProxy(layout), context)
    except Exception as exc:  # a panel draw may not raise
        return f"The bridge's panel failed to draw: {type(exc).__name__}: {exc}"
    return None
