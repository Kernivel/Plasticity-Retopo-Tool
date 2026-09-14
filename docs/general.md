# General knowledge

Retopologizing with this addon includes jumping back and forth between different
phases: picking a mesh, picking a patch, adjusting the patch, and (optionally)
tweaking the retopology in Edit Mode.

| Phase | You are | Left click | <kbd>Esc</kbd> |
|---|---|---|---|
| **Object** | choosing which mesh to retop | enter that object | end the session |
| **Patch** | choosing a surface | open that patch | leave the object |
| **Adjust** | tuning one patch | take the side under the cursor, or commit | clear typing, then discard |
| **Tweak** | in Blender's Edit Mode | Blender's | Blender's |
r
The viewport draws the keybinds that currently apply to the phase.

**One Plasticity face is one patch**, which is why hovering follows the CAD faces
exactly rather than an angle threshold — the mesh states the answer outright. See
[How it works](how-it-works/index.md) for where that comes from.

## Where the result goes

Committed geometry is written to a second object named **`<Source>_Retop`**,
filed under a `Retop` collection that mirrors the Inbox hierarchy the bridge
built.

!!! warning "The name is the link"

    Everything resolves through `<Source>_Retop`. Rename or re-import the CAD
    object and its retopology becomes unreachable.

## Starting density

**Resolution** Presets are available for the starting density of the retopology
to avoid manually scrolling the spans too much.

| | Very Low | Low | Mid | High | Extreme |
|---|---|---|---|---|---|
| relative to computed | ¼ | ½ | 1 | 2 | 4 |

!!! tip Low poly density

    "For a low poly game ready mesh, I recommend Very Low as a base and then tweak the density from there."


## Using N-gons

I would recommend using N-gons exclusively for flat surfaces.
Regular spanning can create weird grids when working surfaces that have too many sides:
In this cas creating an N-gon that matches the neighbor quad patches is usually easier.

## Complex cases

!!! warning "Complex geometry"

    If you have a complex case, with a single surface containing, rings, holes and curved surfaces,
    the addon might not be able to produce a patch that would be acceptable.
    In this case, the best approach is to go back to Plasticity and use either Isoparam (Plasticity's loopcuts)
    or the Knife tool, to simplify a large patch into smaller ones.

<!-- media: 1min. Exemple of a complex case solved by reworking the topology in Plasticity. -->

## Where to start

This is personal advice, but I would recommend starting with the most dense patches first, as a way to dictate the rest of the topology.
For instance, rings, curved surfaces, and bevels are often the best to start with.

## Work on Neighbors first

After creating a patch, it is advised to work on the patches that are next to it.
If not, the matching could have issues if two neighbors use different spannings for their patches: in this case,
the matching isn't able to produce a spanning topology that conforms to both neighbors resulting in holes in the mesh.

## Editing the retopology

Accessing Blender's Edit Mode is available by pressing <kbd>Tab</kbd>, it enables mix snapping vertices, and faces to let
you correct the retopology with Blender's default tools (Knife, Loop cut, Auto-merge,...)in case of errors.
You can switch back to the Retopologizer by pressing <kbd>Tab</kbd> again.

