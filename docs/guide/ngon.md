# N-gon mode

<kbd>N</kbd>, while adjusting a patch.

If you're not looking to Subdivide a surface, using and N-gon usually is the best approach.
N-gon mode replaces the span grid with **one face following the boundary**.

## Faces with holes

A hole is bridged to the boundary around it with two edges, and the patch comes
back as two n-gons rather than one.


A flat face with several holes is filled by default with an N-Gon.
It should probably be cut properly in Plasticity if you need to have curved geometry there. 

## When a patch cannot take one

The <kbd>N</kbd> key and the panel both explain themselves rather than doing
nothing. One blocker:

**Not flat.** Every polygon of the patch is compared against their average
normal, against **N-gon Flatness Tolerance** (5° by default). 
## NGon samples


<kbd>Ctrl</kbd>+wheel drives the detail angle directly.

When not using matching, this can be helpful to drive the details of the n-gon.

## Vertices on one side

Point at a side and <kbd>Ctrl</kbd>+wheel sets how many vertices that side
gets. The side is then divided evenly, and the other sides keep following the
detail angle. The side highlight (<kbd>M</kbd>) has to be on for the side to
be picked up.

Group sides first to set them together. <kbd>Ctrl</kbd>+click a side opens the
group editor, as on a grid. Over a grouped side the wheel sets the count for the
whole group, shared out by length. The corners inside the group stay vertices,
so neighbours still weld to them.

A side matched to a neighbour keeps the neighbour's vertices. Click it to
release the match before changing its count.

The counts are saved with the patch and come back when you re-edit it.
**Reset Side Counts** in the panel drops them.


## Related settings

| Setting | Default | |
|---|---|---|
| **N-gon Detail Angle** | 20° | how much turn between kept boundary vertices |
| **N-gon Flatness Tolerance** | 5° | how far from flat a patch may be and still qualify |
| **Show N-gon Vertices** | on | draw a dot on each kept boundary vertex |
| **Match Neighbour** | on | apply side matching to n-gons without being asked |
