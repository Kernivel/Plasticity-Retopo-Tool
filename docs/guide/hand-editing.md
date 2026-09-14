# Hand-editing the result

Blender, Edit Mode is available during the Patch phase through <kbd>Tab</kbd> to fix error manually with
operators like merge by distance, vertex snapping, knife, loop cut, connect-vertex-path, etc...

!!! tip
    
    In adjust mode this switches between the U/V axis for the spanning.

Pressing <kbd>Tab</kbd> again returns to the addon.

<!-- media: 20s. Tab into the trip, knife one cut across two patches, drag a
     vertex onto its twin so auto-merge fires, Tab back, status line shows the
     repair counts. -->


## What is set up for you

- **Vertex snapping, including onto the mesh being edited.**
- **Auto-merge** at *Auto-Merge Distance*, so closing a seam is a drag rather
  than a drag followed by a Merge by Distance.
- **Face Nearest** on top (*Snap to CAD Surface*), so a dragged vertex stays on
  the CAD surface. Turn it off when the surface is in your way.
- **Vertex select mode.**
- **The result drawn in front** (*Draw In Front While Editing*, on by default),

Settings are read on the way **in**.

**Your own snapping and auto-merge settings are saved on the way in and put back
on the way out** — including when the session ends mid-edit, and when a trip is
closed by the mode dropdown or by a script rather than by <kbd>Tab</kbd>.

## The repair, on the way back

Blender knows nothing about this addon's attributes, and the two it gets wrong
are the two read back later.

**Faces the knife created carry no patch tag.** The patch then reads as partly
"never retopped", and a re-edit stacks a second grid on it. They are re-adopted
onto the patch they sit on.

**New vertices inherit a neighbour's "I am CAD corner N" tag**, i.e. they claim
to be a CAD corner they are nowhere near — and the next commit that touched that
corner would weld onto the wrong point. Those identities are dropped.

The status line says how many of each it dealt with.

!!! note "How a stray corner identity is decided"

    Three ways, and "it moved" is the weakest.

    - **Out of range** for the source mesh is certain — an interpolated integer
      between two real ids is not an index.
    - **Two vertices naming the same source vertex** is certain too: one CAD
      corner is one result vertex, and the nearer one keeps it.
    - **Distance alone** only fires past a generous share of the model's bounding
      box. Nudging a corner by hand is what this mode is *for*, and stripping its
      identity for having moved a hair would undo the fix on the next commit that
      touched it.

    Clearing is always the safe direction: a vertex with no id welds by proximity
    like every other boundary point, which is what a hand-placed vertex should do.
