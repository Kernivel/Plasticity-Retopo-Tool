# One patch, several surfaces

<kbd>Shift</kbd>+click, while picking a surface.

One Plasticity face is one patch. That is how the addon reads a CAD import, and
most of the time it is the right unit of work.

It is not always. A model is cut into faces by the modelling history rather
than by what wants a single grid across it. A boss with two fillet rings around
it is five surfaces that one sheet of retopology should cross, and a chamfer
running along an edge is a strip you rarely want a patch of its own for.

Picking several surfaces says so. They become one patch, the borders between
them disappear, and what is left is the outline of the whole area.

## Picking the surfaces

<kbd>Shift</kbd>+click a surface to add it. Picked surfaces are tinted cyan and
outlined, so a small one gathered up on a busy part is visible as a surface
rather than as one more border among hundreds. <kbd>Shift</kbd>+click it again
to take it back out.

There is no limit on how many. The hint line along the bottom of the viewport
carries the key, and the count once anything is picked.

From the second surface on, the preview shows the patch they make — the grid
across the whole area, rebuilt on every pick. That is the question the gesture
asks, so it is what the viewport answers; hovering other surfaces while picking
no longer replaces it.

Click any picked surface to open them all as one patch. What opens is a normal
patch: it chooses a generator from its own outline, takes spans, matches its
neighbours, and commits like any other.

Clicking a surface that is **not** picked opens that one instead and drops the
selection. <kbd>Esc</kbd> cancels the selection without leaving the object.

## The surfaces have to touch

Every surface added must border the ones already picked. One that touches the
selection only at a corner, or not at all, is refused with a reason.

This is not a nicety. Parts that meet at a point give the patch a pinched
boundary, and parts that do not meet at all give it two separate outer
boundaries — which the pipeline reads as a face with a hole and fills as a
band, stretched across the gap between them.

## Splitting it back

A patch covering several surfaces carries a **Split Into Surfaces** button in
the panel while it is open. Its surfaces become patches again.

A patch that has been committed refuses to split, and says so. The retopology
names that patch, and those faces would be left pointing at something that no
longer exists — nothing would ever clean them up. Delete the patch first
(<kbd>X</kbd> while re-editing it), then split.

## The CAD edges stay

The borders between the picked surfaces stop being patch boundaries, but they
are still edges of the model. The Plasticity edge overlay (<kbd>E</kbd>) and
the B-rep vertices keep showing them, and splitting the patch brings them back
as patch boundaries too.

What changes is what the *retopology* crosses, not what the model is.

## What this does not do

The retopology still follows the surface underneath it. Picking several
surfaces dissolves the *borders* between them, not the shapes of the faces
themselves: a grid across a boss still drapes over the boss.

So this is the answer to "these surfaces should share one grid", and not yet to
"ignore this small feature".

## After a re-export

The choice is stored on the mesh, against the face ids it was made from.
Exporting the model again from Plasticity renumbers every face id, even when
nothing moved, so a patch recorded before the re-export no longer names
anything.

The panel says so when that happens rather than letting the patches quietly
come back one per surface. Pick them again.
