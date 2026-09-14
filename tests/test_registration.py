"""Run inside Blender: blender --background --python tests/test_registration.py

The addon is fully type-annotated, and a Blender addon is the one place where
that can go wrong silently. A property is declared as an *annotation* --
`active_face_id: bpy.props.IntProperty(...)`, with nothing after an `=` -- so
the thing Blender reads to register it is the thing PEP 563
(`from __future__ import annotations`) stringifies. Where a Blender resolves
those strings back, everything works; where it does not, the PropertyGroup
registers *nothing*, nothing raises, and the panel simply draws empty while
every `state.<anything>` blows up somewhere unrelated.

Blender 5.0 does resolve them, as it happens -- verified by adding the future
import and watching all 73 properties register anyway. That is not a reason to
rely on it: `bl_info` declares 4.2 as the minimum, the behaviour is a property
of the Blender being run rather than of this code, and the failure mode is the
silent kind. So the addon simply never imports that future (3.11 evaluates
`list[int]` and `X | None` unaided) and this pins the choice.

The same reasoning keeps annotations off the *class-body attributes* of a
registered class: that dict is the one place the registration walk looks, and
a `_hover_obj: bpy.types.Object | None = None` sitting among the real
properties is asking it to make sense of something that is not one. Methods
are free -- their annotations live on the function object, which nothing
registers.

So this asserts both halves: the properties really are there, *and* the source
rules that keep them there. The outcome alone would pass on a lenient Blender
right up until someone ran an older one.
"""
import ast
import glob
import importlib
import os
import sys

_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_ADDON_DIR))

import bpy

pr = importlib.import_module(os.path.basename(_ADDON_DIR))

FAILURES = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")
    if not cond:
        FAILURES.append(name)


try:
    pr.unregister()
except Exception:
    pass
pr.register()


# ===========================================================================
# The outcome: the properties really did register.
# ===========================================================================

state = bpy.context.scene.plasticity_retop
props = {p.identifier for p in state.bl_rna.properties}

check("the scene property group is registered", state is not None)
check("with the whole panel's worth of properties", len(props) > 40, len(props))

# A spread across every group the panel drives, so a partial registration --
# which is what a half-broken annotation walk would look like -- is caught.
for name in ("active_face_id", "generator_name", "num_sides", "num_loops",
             "span_u", "span_v", "span", "span_axis",
             "ngon_mode", "ngon_angle", "ngon_available",
             "match_mode", "hovered_side", "side_overrides",
             "auto_match_neighbours", "match_margin",
             "session_active", "session_phase", "session_object_name",
             "editing_committed", "reedit_backup_mesh",
             "corner_method_spans", "corner_method_ngon",
             "resolution", "length_unit", "boundary_weld_distance"):
    check(f"  state.{name} exists", name in props)

# An Operator's own property goes through the same walk. Read it through
# bpy.ops: `bpy.types.RETOP_OT_local_view.bl_rna` is the generic Operator RNA
# and never lists per-operator properties, so checking there proves nothing.
local_view_props = {p.identifier
                    for p in bpy.ops.retop.local_view.get_rna_type().properties}
check("an operator's own property registers too",
      "frame_selected" in local_view_props, sorted(local_view_props))


# ===========================================================================
# The cause: what the source is allowed to contain.
# ===========================================================================

def source_files():
    files = sorted(glob.glob(os.path.join(_ADDON_DIR, "*.py"))
                   + glob.glob(os.path.join(_ADDON_DIR, "generators", "*.py")))
    return [(os.path.relpath(path, _ADDON_DIR), ast.parse(
        open(path, encoding="utf-8").read())) for path in files]


SOURCES = source_files()
check("there are source files to inspect", len(SOURCES) > 10, len(SOURCES))

futures = [name for name, tree in SOURCES
           for node in tree.body
           if isinstance(node, ast.ImportFrom) and node.module == "__future__"
           and any(alias.name == "annotations" for alias in node.names)]
check("no module imports `from __future__ import annotations`", not futures, futures)


# Classes Blender registers: anything deriving from a bpy.types base. Their
# bodies may carry `name: bpy.props.X(...)` assignments and nothing else with
# an annotation.
def is_registered_class(node):
    for base in node.bases:
        # bpy.types.Operator, bpy.types.PropertyGroup, bpy.types.Panel, ...
        if (isinstance(base, ast.Attribute) and isinstance(base.value, ast.Attribute)
                and base.value.attr == "types"
                and isinstance(base.value.value, ast.Name)
                and base.value.value.id == "bpy"):
            return True
    return False


def is_property_deferred(annotation):
    """`bpy.props.SomethingProperty(...)` -- the only annotation Blender
    accepts in a registered class body.

    The *annotation* slot, note, not the value: a Blender property is declared
    `name: bpy.props.IntProperty(...)` with nothing after an `=`. That is
    exactly why PEP 563 destroys it -- the thing it stringifies is the thing
    Blender reads.
    """
    return (isinstance(annotation, ast.Call)
            and isinstance(annotation.func, ast.Attribute)
            and annotation.func.attr.endswith("Property")
            and isinstance(annotation.func.value, ast.Attribute)
            and annotation.func.value.attr == "props")


registered = [(name, node) for name, tree in SOURCES
              for node in ast.walk(tree)
              if isinstance(node, ast.ClassDef) and is_registered_class(node)]
check("the registered classes were found", len(registered) >= 10, len(registered))
check("including the property group",
      any(node.name == "RetopPatchState" for _f, node in registered))
check("and the session operator",
      any(node.name == "RETOP_OT_session" for _f, node in registered))

offenders = []
declared_props = 0
for filename, node in registered:
    for statement in node.body:
        if not isinstance(statement, ast.AnnAssign):
            continue
        if is_property_deferred(statement.annotation):
            declared_props += 1
            continue
        target = getattr(statement.target, "id", "?")
        offenders.append(f"{filename}:{statement.lineno} {node.name}.{target}")

check("no registered class body carries a non-property annotation",
      not offenders, offenders)
check("the bpy.props annotations themselves are still there",
      declared_props > 40, declared_props)


# ===========================================================================
# And the hints are actually present -- a pass that annotated nothing would
# satisfy every check above.
# ===========================================================================

annotated = total = 0
for _filename, tree in SOURCES:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        total += 1
        args = [a for a in node.args.args if a.arg not in ("self", "cls")]
        args += node.args.kwonlyargs
        if node.returns is not None and all(a.annotation for a in args):
            annotated += 1

check("every function carries a return type and argument types",
      annotated == total, f"{annotated}/{total}")

# ===========================================================================
# A registration that fails leaves nothing behind
#
# Blender marks an addon disabled the moment its `register` raises, so nothing
# ever calls `unregister` on the half that did take -- the classes stay
# registered in the running process with no way to reach them, and the next
# attempt to enable it dies on "register_class(...): already registered as a
# subclass 'RetopPatchState'". A different error about a different thing, and
# the only cure is restarting Blender.
#
# That is what one failed registration used to cost. It must cost a failed
# registration and nothing more.
# ===========================================================================
pr.unregister()

_real_register = pr.ui.register


def _explode() -> None:
    raise RuntimeError("deliberate failure, three modules in")


# `ui` is last, so `state`, `operators` and `prefs` have all taken when it goes.
pr.ui.register = _explode
try:
    pr.register()
    raised = ""
except RuntimeError as exc:
    raised = str(exc)
finally:
    pr.ui.register = _real_register

check("the failure is re-raised, not swallowed -- it says why", 
      raised == "deliberate failure, three modules in", raised)
# The tell, and the whole point: the property group is what the repeat attempt
# used to trip over.
check("and the scene property it had already attached is gone again",
      not hasattr(bpy.types.Scene, "plasticity_retop"))

# The one that matters: enabling it again has to work, with no restart.
try:
    pr.register()
    ok, detail = True, ""
except Exception as exc:  # noqa: BLE001
    ok, detail = False, repr(exc)
check("so registering again succeeds rather than reporting a stale class",
      ok, detail)
check("and the addon is whole", hasattr(bpy.types.Scene, "plasticity_retop"))

print()
if FAILURES:
    print(f"=== {len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(1)
print("=== ALL CHECKS PASSED")
