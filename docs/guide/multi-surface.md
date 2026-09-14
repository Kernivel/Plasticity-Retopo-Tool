# One patch, several surfaces

<kbd>Shift</kbd>+click, while picking a surface.

One Plasticity face is one patch. 
In case the Retopology should produce a lower poly mesh, it can be useful to combine patches together.

## Picking the surfaces

<kbd>Shift</kbd>+click a surface to add it to a group.
<kbd>Shift</kbd>+click it again to take it back out.

There is no limit on how many surfaces can be grouped for retopo. The hint line carries the count once anything is picked.

The preview updates to show the patch that is going to be created.

Click any picked surface to open them all as one patch. What opens is a normal
patch: it chooses a generator from its own outline, takes spans, matches its
neighbours, and commits like any other.

Clicking a surface that is **not** picked opens that one instead and drops the
selection. <kbd>Esc</kbd> cancels the selection without leaving the object.

## The surfaces have to touch

Every surface added must border the ones already picked.

## Splitting it back

A patch covering several surfaces carries a **Split Into Surfaces** button in
the panel while it is open. Its surfaces become patches again.

Deleting it (<kbd>X</kbd> while re-editing it) takes it apart too. Its
retopology goes and its surfaces become patches of their own again, which is
what deleting a patch means everywhere else.

A patch that has been committed refuses to split, and says so. The retopology
names that patch, and those faces would be left pointing at something that no
longer exists. Delete the patch first
(<kbd>X</kbd> while re-editing it), then split.

## The CAD edges stay

The Plasticity model stays untouched in case the grouping was a mistake,
 you can always go back to the original surfaces.


## What this does not do

The retopology still follows the surface underneath it. Picking several
surfaces dissolves the *borders* between them, not the shapes of the faces
themselves: a grid across a boss still drapes over the boss.

## After a re-export

Re-exporting the mesh breaks the grouping so you will need to rebuild it.
