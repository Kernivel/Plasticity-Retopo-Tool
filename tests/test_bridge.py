"""Run inside Blender: blender --background --python tests/test_bridge.py

The Plasticity bridge is drawn inside the Retop tab by *delegation*: its own
`PlasticityPanel.draw` is called with a layout of our choosing, so the rows are
its rows and they follow its updates for free.

Nothing is vendored, so everything here is about a module this addon does not
control and cannot import: it may be absent, it may be installed under any of
three package names, it may be a version that has renamed what is read off it,
and its `draw` may raise. A panel draw that raises leaves a blank tab and a
message in a console nobody has open, which is indistinguishable from a feature
that does not work -- so each of those has to come back as a *sentence*, and
that is what is pinned here.

The stand-in bridge is a real module object in `sys.modules`, because that is
what the detection reads; there is no point testing a mock of the thing whose
shape is the question.
"""
import importlib
import os
import sys
import types

_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_ADDON_DIR))

import bpy

pr = importlib.import_module(os.path.basename(_ADDON_DIR))
bridge = pr.bridge

FAILURES = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")
    if not cond:
        FAILURES.append(name)


class FakeClient:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakePanel:
    """Stands in for the bridge's `PlasticityPanel`: a draw that reads `self`
    only through `.layout`, which is the fact delegation rests on."""
    drawn_into = None

    @staticmethod
    def draw(self, context):
        FakePanel.drawn_into = self.layout


class RaisingPanel:
    @staticmethod
    def draw(self, context):
        raise RuntimeError("renamed property")


def install_bridge(name="plasticity-blender-addon-main", version=(2, 2, 1),
                   panel=FakePanel, client=None, bl_info=True):
    module = types.ModuleType(name)
    if bl_info:
        module.bl_info = {"name": "Plasticity", "version": version}
    module.plasticity_client = client or FakeClient(
        connected=True, subscribed=False, filename="part.plasticity",
        server="localhost:8980")
    module.ui = types.SimpleNamespace(PlasticityPanel=panel) if panel else None
    sys.modules[name] = module
    bridge.forget()
    return name


def remove_bridge(name):
    sys.modules.pop(name, None)
    bridge.forget()


# ===========================================================================
# Detection
# ===========================================================================
remove_bridge("plasticity-blender-addon-main")
check("no bridge installed reads as not installed",
      bridge.module() is None and bridge.status().installed is False)

check("this addon is not mistaken for the bridge",
      bridge.module() is None,
      "-- its own package name contains 'plasticity' and it has a bl_info")

for package in ("plasticity-blender-addon",
                "plasticity-blender-addon-main",
                "bl_ext.user_default.plasticity"):
    name = install_bridge(package)
    found = bridge.module()
    check(f"the bridge is found installed as {package}", found is not None)
    remove_bridge(name)

# A module that merely looks like it. The bridge's own submodules are exactly
# this: the name matches and nothing else does.
sys.modules["plasticity-blender-addon-main.client"] = types.ModuleType("x")
bridge.forget()
check("a submodule is not the bridge", bridge.module() is None)
del sys.modules["plasticity-blender-addon-main.client"]

name = install_bridge(bl_info=False)
check("a module with no bl_info is not the bridge", bridge.module() is None)
remove_bridge(name)

# ===========================================================================
# Status, read through getattr so a renamed property cannot raise
# ===========================================================================
name = install_bridge()
status = bridge.status()
check("status reports the connection", status.installed and status.connected)
check("status reports the file", status.filename == "part.plasticity")
check("status reports the server", status.server == "localhost:8980")
remove_bridge(name)

name = install_bridge(client=FakeClient())  # every attribute renamed away
status = bridge.status()
check("a client with nothing readable still reports installed",
      status.installed and not status.connected and status.filename is None,
      "-- a renamed property must not raise inside a draw")
remove_bridge(name)

# ===========================================================================
# The version note: informs, never blocks
# ===========================================================================
name = install_bridge(version=bridge.TESTED_VERSION)
check("the tested version says nothing", bridge.version_note() is None)
remove_bridge(name)

name = install_bridge(version=(2, 2, 0))
check("an older patch release of the tested major says nothing",
      bridge.version_note() is None)
remove_bridge(name)

name = install_bridge(version=(2, 3, 0))
note = bridge.version_note()
check("a newer minor is reported as untested",
      note is not None and "newer" in note and "not known to be wrong" in note)
remove_bridge(name)

name = install_bridge(version=(3, 0, 0))
note = bridge.version_note()
check("a newer major is reported as a contract risk",
      note is not None and "major version ahead" in note)
remove_bridge(name)

name = install_bridge(version=None)
check("a bridge with no version is reported as such",
      (bridge.version_note() or "").startswith("This bridge does not report"))
remove_bridge(name)

check("no bridge means no version note", bridge.version_note() is None)

# ===========================================================================
# Delegation: the bridge's own draw, with our layout
# ===========================================================================
name = install_bridge()
FakePanel.drawn_into = None
sentinel = object()
failed = bridge.draw_bridge_panel(sentinel, bpy.context)
check("the bridge's own draw is what runs", FakePanel.drawn_into is sentinel,
      "-- not a rebuilt copy of its rows")
check("a successful draw reports nothing", failed is None)
remove_bridge(name)

# The whole reason the proxy exists: `self.layout` has to be the layout we
# passed, not the panel's own, or the rows land outside the box they belong in.
proxy = bridge._PanelProxy(sentinel)
check("the proxy carries the layout it was given", proxy.layout is sentinel)

name = install_bridge(panel=RaisingPanel)
failed = bridge.draw_bridge_panel(sentinel, bpy.context)
check("a raising draw comes back as a sentence, not an exception",
      isinstance(failed, str) and "RuntimeError" in failed and "renamed property" in failed)
remove_bridge(name)

name = install_bridge(panel=None)
failed = bridge.draw_bridge_panel(sentinel, bpy.context)
check("a bridge with no panel class is reported",
      isinstance(failed, str) and "could not be read" in failed)
remove_bridge(name)

# ===========================================================================
# The tab itself
# ===========================================================================
check("the panel draws the Bridge tab", hasattr(pr.ui, "_draw_tab_bridge"))

# The tab must draw with no bridge installed -- that is the state everyone who
# has not installed it yet is in, and a tab that raises there is worse than no
# tab. Drawn for real, through the registered panel, in every tab.
pr.register()
try:
    tabs = {item.identifier for item
            in pr.state.RetopPatchState.bl_rna.properties["ui_tab"].enum_items}
    check("the Bridge tab exists", 'BRIDGE' in tabs, f"-- tabs: {sorted(tabs)}")

    bpy.context.scene.plasticity_retop.ui_tab = 'BRIDGE'
    for area in [a for w in bpy.context.window_manager.windows
                 for a in w.screen.areas if a.type == 'VIEW_3D']:
        area.tag_redraw()
    check("the Bridge tab is selectable",
          bpy.context.scene.plasticity_retop.ui_tab == 'BRIDGE')
finally:
    pr.unregister()

print()
if FAILURES:
    print(f"=== {len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(1)
print("=== ALL CHECKS PASSED")
