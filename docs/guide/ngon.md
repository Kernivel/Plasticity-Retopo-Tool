# N-gon mode

<kbd>N</kbd>, while adjusting a patch.

If you're not looking to Subdivide a surface, using and N-gon usually is the best approach.
N-gon mode replaces the span grid with **one face following the boundary**.

A patch committed as an n-gon reopens as one whatever the current mode, the same
rule as its spans.

## Faces with holes

A hole is bridged to the boundary around it with two edges, and the patch comes
back as two n-gons rather than one. A Blender n-gon carries a single loop, so
there is no way to draw a face with a hole in it directly.

Any number of holes works. Each one is cut into whichever face already covers
it, so four holes come back as five n-gons. Where a bridge lands is arbitrary;
what is checked is that it stays inside the face, crossing neither another hole
nor a notch in the outline.

A flat face with several holes is filled this way **without pressing
<kbd>N</kbd>**. No span generator paves more than one outline, so the
alternative would be a grid over the holes, and the panel says which happened.

## When a patch cannot take one

The <kbd>N</kbd> key and the panel both explain themselves rather than doing
nothing. One blocker:

**Not flat.** Every polygon of the patch is compared against their average
normal, against **N-gon Flatness Tolerance** (5° by default). One face across a
bevel or a fillet would become a flat lid over it — the shape simply gone.

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
