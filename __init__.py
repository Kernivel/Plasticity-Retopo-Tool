bl_info = {
    "name": "Plasticity Retop",
    "author": "",
    # Kept in step with version.ADDON_VERSION by hand -- two literals in two
    # files, which is why scripts/build_zip.py refuses to build when they
    # disagree. Blender parses this as source before any of this code runs, so
    # nothing can derive it.
    "version": (0, 73, 0),
    "blender": (4, 2, 0),
    "location": "View3D > N-panel > Retop",
    "description": "Patch-based retopology assistant for meshes imported via the Plasticity bridge",
    "category": "Mesh",
}

from . import version
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


# Registration order. `prefs` after `operators`, because the preferences page
# draws the keymap items the operators registered and an AddonPreferences whose
# draw finds nothing is a blank page with no explanation.
_MODULES = (state, operators, prefs, ui)


def register() -> None:
    """Register every module, and undo the ones that took if one of them fails.

    The unwind is not tidiness. Blender marks an addon *disabled* the moment
    its `register` raises, so nothing will ever call `unregister` on the half
    that did take -- and those classes stay registered in the running process
    with no way to reach them. The next attempt to enable it then dies on
    `register_class(...): already registered as a subclass 'RetopPatchState'`,
    which is a different error about a different thing, and the only cure is
    restarting Blender. That is what a single failed registration used to cost
    (see `operators._patch_hover_wanted` for the one that did it), and there is
    no reason for the first failure to be unrecoverable as well as a failure.

    The original exception is re-raised, never swallowed: it is what says why
    registration failed, and the unwind exists so that it is the *only* thing
    the user has to read. `tests/test_registration.py` fails the same
    "already registered" way without it.
    """
    done = []
    try:
        for module in _MODULES:
            module.register()
            done.append(module)
    except Exception:
        for module in reversed(done):
            # Teardown, on a path that is already failing: one module refusing
            # to come back out must not hide the exception being re-raised, or
            # replace it with one about the cleanup.
            try:
                module.unregister()
            except Exception:
                pass
        raise


def unregister() -> None:
    for module in reversed(_MODULES):
        module.unregister()
