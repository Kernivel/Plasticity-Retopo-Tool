bl_info = {
    "name": "Plasticity Retop",
    "author": "",
    # Must match version.ADDON_VERSION, by hand.
    # scripts/build_zip.py refuses to build when they disagree.
    "version": (0, 81, 0),
    "blender": (4, 2, 0),
    "location": "View3D > N-panel > Retop",
    "description": "Patch-based retopology assistant for meshes imported via the Plasticity bridge",
    "category": "Mesh",
}

from . import version
from . import bridge
from . import constants
from . import patch_data
from . import sides
from . import geometry
from . import generators
from . import cad_display
from . import state
from . import mesh_build
from . import patchprep
from . import sidematch
from . import keymap
from . import tweak
from . import overlay
from . import operators
from . import prefs
from . import ui


# Registration order. `prefs` must come after `operators`: its page draws the
# keymap items the operators register.
_MODULES = (state, operators, prefs, ui)


def register() -> None:
    """Register every module, and undo the ones that took if one of them fails.

    Without the unwind, a failed registration leaves classes registered that
    only a Blender restart clears. See "A registration that fails" in CLAUDE.md.
    The original exception is always re-raised.
    """
    done = []
    try:
        for module in _MODULES:
            module.register()
            done.append(module)
    except Exception:
        for module in reversed(done):
            # A failing teardown must not replace the original exception.
            try:
                module.unregister()
            except Exception:
                pass
        raise


def unregister() -> None:
    for module in reversed(_MODULES):
        module.unregister()
