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


## Related settings

| Setting | Default | |
|---|---|---|
| **N-gon Detail Angle** | 20° | how much turn between kept boundary vertices |
| **N-gon Flatness Tolerance** | 5° | how far from flat a patch may be and still qualify |
| **Show N-gon Vertices** | on | draw a dot on each kept boundary vertex |
| **Match Neighbour** | on | apply side matching to n-gons without being asked |
