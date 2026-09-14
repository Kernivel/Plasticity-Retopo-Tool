# Merging several faces

<kbd>Shift</kbd>+click, while picking a surface.

One Plasticity face is one patch. That is how the addon reads a CAD import, and
most of the time it is the right unit of work.

It is not always. A model is cut into faces by the modelling history rather
than by what wants a single grid across it. A boss with two fillet rings around
it is five faces that one sheet of retopology should cross, and a chamfer
running along an edge is a strip you rarely want a patch of its own for.

Merging says so. Several faces become one patch, the borders between them
disappear, and what is left is the outline of the whole area.

## Gathering the faces

<kbd>Shift</kbd>+click a surface to add it to the merge. Selected patches are
outlined in cyan. <kbd>Shift</kbd>+click one again to take it back out.

Click any selected patch to open them all as one. The patch that opens is a
normal patch: it picks a generator from its own outline, takes spans, matches
its neighbours, and commits like any other.

Clicking a patch that is **not** in the selection opens that one instead and
drops the selection. <kbd>Esc</kbd> cancels the selection without leaving the
object.

## The faces have to touch

Every face added must border the ones already selected. A face that touches the
selection only at a corner, or not at all, is refused with a reason.

This is not a nicety. Parts that meet at a point give the merged patch a
pinched boundary, and parts that do not meet at all give it two separate outer
boundaries — which the pipeline reads as a face with a hole and fills as a
band, stretched across the gap between them.

## Splitting it back

A merged patch carries a **Split Back Apart** button in the panel while it is
open. Its faces become patches again.

A merged patch that has been committed refuses to split, and says so. Its
retopology names the merged patch, and those faces would be left pointing at
something that no longer exists — nothing would ever clean them up. Delete the
patch first (<kbd>X</kbd> while re-editing it), then split.

## What merging does not do

The retopology still follows the surface underneath it. Merging dissolves the
*borders* between the faces, not the shapes of the faces themselves: a grid
merged across a boss still drapes over the boss.

So merging is the answer to "these faces should share one grid", and not yet to
"ignore this small feature".

## After a re-export

A merge is stored on the mesh, against the face ids it was made from. Exporting
the model again from Plasticity renumbers every face id, even when nothing
moved, so a merge written before the re-export no longer names anything.

The panel says so when that happens rather than letting the patches quietly
come back one per face. Merge them again.
