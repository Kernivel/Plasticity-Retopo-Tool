# Faces with a hole

A CAD face bounded by **two loops** — a slot cut through a panel, a tube-like
face with two rims, a fillet running all the way round, a cylinder wall — is a
different problem from a face with one boundary.

<!-- media: 10s. A cylinder wall and a washer, each committed as a ring, then
     orbit to show the rungs running straight across. -->

## The Ring generator

Two spans, and <kbd>Tab</kbd> switches which one the wheel drives:

- **around** — points along each loop;
- **across** — rows spanning the gap between them.

The panel names it `Ring (2 loops, N corners)`.

Both loops must end up with the **same** point count, because every quad runs
straight from `outer[i]` to `inner[i]`.

## Two loops is not the same thing as a band

A 200×100 plate with a 5 mm hole is also bounded by two loops, and a band across
it is a disaster: either the hole gets a hundred points or the outline gets
twelve, and every quad is stretched the width of the plate.

The two cases are separated on how **even** the gap between the loops is, and how
far apart the two perimeters are. Both tests are deliberately generous — calling
a band a plate costs more than the reverse.

A non-band that is flat is filled as an [n-gon](ngon.md) instead (outer boundary
plus hole, bridged with two edges), and the panel says why.

!!! note "A committed patch is never rerouted"

    It comes back as whatever it was built as.

## Straight rungs

How the two loops are indexed against each other *is* the shape of the quads.
Aligning them by whole index — all you can do once both are resampled — leaves up
to half a step of rotation, and that residue is not noise but a **constant
shear**, the same angle on every rung: half a step of 16 points is 18.6°.

So a cornerless rim is *phase*-aligned instead: where it is sampled from is
chosen from the geometry, not which sample to start at.

**Only on a cornerless rim, and never on a matched one.** A corner is an
untouched source vertex that welds by identity; moving one while keeping its name
would make a later patch reuse a vertex that is no longer there. A hole with real
corners keeps them and its shear.

## Matching a ring

The two rims are keyed **per loop**, so both can be matched at once as long as
they agree on the count — unlike a grid, where two sides driving the same span
knock each other out.

A rim carrying a match **leads** the band: the other rim is phased onto it, never
the reverse. Before that, a match landing on the "wrong" rim was thrown away and
the two rings came back half a step apart, with a crack all the way round — and
which rim counted as first was decided by extent, which on a tube is a coin flip.

!!! warning "Known rough edge: a multi-side ring"

    Matching one side of a ring sets that **whole loop's** around count from that
    side alone. Correct for the common case, where a rim is one cornerless side;
    wrong when a loop has several corners.

## What is not handled

- **More than one hole in a single face.** A band runs between two loops and
  there is no band between three. A flat face like that is filled as an
  [n-gon](ngon.md) instead, which takes any number of holes; one that is not
  flat gets its outer boundary alone, and the panel says so.
- **Corner matching between the two loops.** They are paired by arc length, so a
  hole shaped very differently from the outer boundary distorts the band.
- **Spans propagating *into* a ring.** "Around" is one number for the whole loop.
  They propagate *out* of one to its neighbours normally.
