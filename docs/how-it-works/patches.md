# 1. Identifying a patch

A Plasticity model arrives in Blender as a triangle soup, but it is not one:
every triangle still records which CAD face it came from. That single fact is
what the whole addon is built on — a patch is not found by an angle threshold or
a flood fill, it is *read*.

## What the bridge actually sends

The bridge writes two custom properties on the imported mesh:

- `mesh["groups"]` — a flat list of `[loop_start, loop_count]` pairs, in polygon
  order;
- `mesh["face_ids"]` — one Plasticity face id per group, in the same order.

One plasticity face can be selected in Edit Mode by pressing the <kbd>L</kbd> key (by default) from Blender.
In case you want to see groups and ids, the plugin offers a debug mode that displays them in the viewport.


<figure class="diagram" markdown="0">
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 470 230" role="img" aria-label="The same triangles, before and after reading the face ids">
  <defs>
    <g id="hwTri" fill="none" stroke="#b9c4cc" stroke-width="1">
      <path d="M0 0H180M0 45H180M0 90H180M0 135H180"/>
      <path d="M0 0V135M45 0V135M90 0V135M135 0V135M180 0V135"/>
      <path d="M0 0L45 45M45 0L90 45M90 0L135 45M135 0L180 45M0 45L45 90M45 45L90 90M90 45L135 90M135 45L180 90M0 90L45 135M45 90L90 135M90 90L135 135M135 90L180 135"/>
    </g>
  </defs>

  <g font-family="sans-serif" font-size="11" fill="#555">
    <text x="20" y="24">What you see in Blender</text>
    <text x="250" y="24">What the face ids say it is</text>
  </g>

  <g transform="translate(20,40)">
    <rect x="0" y="0" width="180" height="135" fill="#f0f0f0"/>
    <use href="#hwTri"/>
  </g>

  <g transform="translate(250,40)">
    <rect x="0" y="0" width="90" height="135" fill="#dce9f5"/>
    <rect x="90" y="0" width="90" height="135" fill="#e2f1d8"/>
    <use href="#hwTri"/>
    <path d="M90 0V135" stroke="#2c3e50" stroke-width="3" fill="none"/>
    <path d="M0 0H180V135H0Z" stroke="#2c3e50" stroke-width="2" fill="none"/>
    <circle cx="90" cy="0" r="4" fill="#e6473d"/>
    <circle cx="90" cy="135" r="4" fill="#e6473d"/>
  </g>

  <g font-family="sans-serif" font-size="10" fill="#666">
    <text x="250" y="192">patch A</text>
    <text x="340" y="192">patch B</text>
    <text x="250" y="210" fill="#2c3e50">— shared CAD edge</text>
    <text x="366" y="210" fill="#e6473d">B-rep vertex</text>
  </g>
</svg>
<figcaption> The structure on the right is read back from the two custom properties.</figcaption>
</figure>

## From ids to a patch

Four derivations, in order:

1. **The patch** is the set of triangles sharing one face id.
2. **Its boundary** is the set of edges with a triangle of that patch on one side
   only. Walking those edges head-to-tail gives the **boundary loops** — one for
   a plain face, two for a face with a hole or a tube-like face, more for several
   holes.
3. **Its neighbours**: each boundary edge is a half-edge `(a, b)`; the reversed
   half-edge `(b, a)` belongs to the patch across it, and *that* patch's face id
   is the neighbour of this segment.
4. **Its corners** are the vertices where that neighbour *changes*. That is a
   genuine B-rep vertex — the junction between two CAD edges — and the mesh
   states it outright, at any angle.

## The border, and the corners on it

The border decides the quad/ngon patch.

A boundary segment is a **half-edge** `(a, b)`: an edge carrying a triangle of
this patch on one side and nothing of it on the other. Walking those segments
head to tail closes a loop.

Each half-edge is also a question asked of the rest of the mesh: who owns the
reversed one, `(b, a)`? There are two answers.

- **Another face id** — the segment runs along a CAD edge, and it names the
  patch on the other side of it.
- **Nothing** — the segment is on a free boundary of an open sheet. The model
  simply stops there.

A **CAD edge** is the maximal run of consecutive segments whose answer does not
change. The vertex where it *does* change is a **B-rep vertex**: the junction
between two CAD edges, and a corner of this patch.

<figure class="diagram" markdown="0">
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 470 215" role="img" aria-label="A patch boundary coloured by the neighbouring face, with a dot where that neighbour changes">
  <g transform="translate(30,40)">
    <rect x="0" y="0" width="200" height="110" fill="#f4f6f7"/>
    <text x="100" y="60" font-family="sans-serif" font-size="11" fill="#888" text-anchor="middle">this patch</text>
    <path d="M0 0H200V110" fill="none" stroke="#2980b9" stroke-width="3"/>
    <path d="M200 110H0" fill="none" stroke="#27ae60" stroke-width="3"/>
    <path d="M0 110V0" fill="none" stroke="#b9c4cc" stroke-width="3" stroke-dasharray="5 4"/>
    <circle cx="200" cy="0" r="4" fill="#fff" stroke="#7f8c8d" stroke-width="2"/>
    <g fill="#e6473d">
      <circle cx="200" cy="110" r="5"/><circle cx="0" cy="110" r="5"/><circle cx="0" cy="0" r="5"/>
    </g>
  </g>

  <g font-family="sans-serif" font-size="11">
    <text x="256" y="52" fill="#2980b9">neighbour = face B</text>
    <text x="256" y="72" fill="#27ae60">neighbour = face C</text>
    <text x="256" y="92" fill="#8a949c">no neighbour — free boundary</text>
    <text x="256" y="120" fill="#e6473d">the neighbour changes here:</text>
    <text x="256" y="134" fill="#e6473d">a B-rep vertex, i.e. a corner</text>
    <text x="256" y="162" fill="#7f8c8d">a 90° turn in the middle of one</text>
    <text x="256" y="176" fill="#7f8c8d">run — the face ids say nothing</text>
    <text x="256" y="190" fill="#7f8c8d">about it</text>
  </g>
</svg>
<figcaption>Three corners out of four, on a shape whose four corners are equally obvious to
the eye.</figcaption>
</figure>

The mesh states the junction outright, and it states it **at any angle**, so two faces meeting almost
tangentially can still be separated as easily as two 90° edges. 

But read the diagram the other way round and the limit of it is just as plain:

- **A corner the ids do not know about.** The top right of that patch turns 90°
  and stays with the same neighbour throughout, so no junction is recorded there.
- **A face bordered by one single neighbour has no corner at all**, however
  square it looks — a cylinder's seam, a disc sitting inside a ring, a fillet
  running the length of a part. The answer never changes anywhere.
- **A free boundary is one single run.** Nothing distinguishes its segments from
  one another, so the outer edge of an open sheet yields nothing either.

That is why a second, purely geometric corner test runs beside this one, and why
which of the two is trusted depends on what the patch is about to be filled with.
[Step 2](generators.md) is where the two are reconciled, the extra candidates a
tessellated curve throws up are ranked away, and the boundary is finally cut into
**sides**.

One property of these corners is worth carrying forward, because the commit
depends on it: a corner found this way is an **exact source vertex** — a vertex
of the CAD tessellation, not a resample of it. Two patches meeting there can
therefore be welded by *identity* rather than by proximity, which is what makes
the corners the one part of a boundary that never moves.

## Two things the import does that get in the way

Both are handled before any of the above runs, and both are worth knowing about,
because when they go wrong the symptom looks like a bug in the generators rather
than in the input.

<figure class="diagram" markdown="0">
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 205" role="img" aria-label="Duplicated border vertices, and a T-junction along a shared edge">
  <g font-family="sans-serif" font-size="11" fill="#555">
    <text x="10" y="18">A. every border vertex exists twice</text>
    <text x="360" y="18">B. the two sides do not always agree</text>
  </g>

  <g transform="translate(10,34)">
    <rect x="0" y="20" width="110" height="110" fill="#dce9f5"/>
    <rect x="126" y="20" width="110" height="110" fill="#e2f1d8"/>
    <path d="M0 20V130M126 20V130" stroke="#2c3e50" stroke-width="2" fill="none"/>
    <text x="34" y="80" font-family="sans-serif" font-size="11" fill="#666">patch A</text>
    <text x="160" y="80" font-family="sans-serif" font-size="11" fill="#666">patch B</text>
    <g fill="#e6473d">
      <circle cx="0" cy="20" r="4"/><circle cx="0" cy="75" r="4"/><circle cx="0" cy="130" r="4"/>
      <circle cx="126" cy="20" r="4"/><circle cx="126" cy="75" r="4"/><circle cx="126" cy="130" r="4"/>
    </g>
    <text x="0" y="158" font-family="sans-serif" font-size="10" fill="#666">the same three points, under six indices —</text>
    <text x="0" y="172" font-family="sans-serif" font-size="10" fill="#666">merged by position before anything else runs</text>
  </g>

  <g transform="translate(360,34)">
    <rect x="0" y="20" width="110" height="110" fill="#dce9f5"/>
    <rect x="126" y="20" width="110" height="110" fill="#e2f1d8"/>
    <path d="M0 20V130M126 20V130" stroke="#2c3e50" stroke-width="2" fill="none"/>
    <text x="30" y="80" font-family="sans-serif" font-size="11" fill="#666">coarse</text>
    <text x="164" y="80" font-family="sans-serif" font-size="11" fill="#666">fine</text>
    <g fill="#2c3e50">
      <circle cx="0" cy="20" r="4"/><circle cx="0" cy="130" r="4"/>
      <circle cx="126" cy="20" r="4"/><circle cx="126" cy="130" r="4"/>
    </g>
    <g fill="#e6a23d">
      <circle cx="126" cy="57" r="4"/><circle cx="126" cy="93" r="4"/>
    </g>
    <text x="0" y="158" font-family="sans-serif" font-size="10" fill="#666">the amber vertices split segments the other</text>
    <text x="0" y="172" font-family="sans-serif" font-size="10" fill="#666">side does not have — so those never pair</text>
  </g>
</svg>
</figure>

### The border is duplicated

The bridge tessellates each CAD face **separately**, so the two faces meeting
along an edge each carry their own copy of every vertex on it, at the same
position under different indices. Until those copies are merged, no boundary
half-edge ever finds its opposite: no patch can name a neighbour, no corner is
found topologically, and the CAD structure overlay has nothing to draw.

They are merged by position, and two details of that merge matter:

- **Only border vertices are merged.** A vertex strictly inside a patch already
  has polygons on both sides of every edge it touches, so there is no second copy
  of it to find, and scoping the merge that way makes it safe whichever way the
  bridge behaves.
- **The tolerance is capped by the mesh's own shortest edge.** An edge is the
  mesh saying outright that its two ends are distinct points of the surface;
  welding across one destroys the triangle carrying it, the patch's boundary walk
  then runs into a dead end, and what comes back is an *open chain handed out as
  a loop* — which draws a chord straight across a face the model never divided.
  On one real part that collapsed 205 genuine edges and left 101 of 245 loops
  open.

!!! note "If a patch far from the origin dices strangely"

    The merge tolerance is `1e-5` in the mesh's local units. On a part whose
    coordinates run to a few hundred units the float32 ulp is already that order,
    so two faces' independently rounded copies of a shared vertex can miss each
    other. That shows up as a phantom patch border — and therefore wrong corners,
    a wrong side count and the wrong generator. Suspect it before anything else.

### The two sides do not tessellate an edge the same way

The finer side of a CAD edge drops vertices in the *middle* of the coarser side's
segments, so the reversed half-edge simply is not there and those segments report
no neighbour at all. This is not a rare case: on one exported object **2327 of
4857** boundary segments came back unpaired, and 1312 of 2287 faces had at least
one.

Left alone it is not a quiet degradation either. Every unpaired segment reads as
a change of neighbour, so it becomes a phantom B-rep vertex; the side count then
picks the wrong generator; and the CAD overlay draws one shared border as a
string of unrelated edges. That is exactly the symptom *"Blender's patch does not
match Plasticity's face"*.

So whatever the exact pairing misses is resolved **geometrically** instead: the
neighbour is the patch whose own boundary segment this one lies *along*. It runs
only on the segments the exact pass missed, so a cleanly tessellated mesh pays
one scan that finds nothing.

The tolerance there is a share of the **model extent**, not of the segment's own
length. Two faces sharing a CAD edge put their boundaries on the same curve, so
the only thing between them is float rounding — which scales with coordinate
magnitude, not with feature size. Measured across a real part, a genuine shared
border sits within 1e-7 of the extent, i.e. coincident. Scaling by the segment
instead lets a long one reach a long way *sideways*, and take a separate sheet
floating above it for its neighbour.

## What comes out of step 1

For the patch under the cursor: its polygons, its boundary loops ordered **outer
loop first**, the neighbouring face id of every boundary segment, and the source
vertex index of every boundary point. That is everything the next step needs.

!!! tip "Seeing it for yourself"

    <kbd>E</kbd> draws the recovered Plasticity edges and, with
    **Show B-rep Vertices** on, a dot at every junction. If those disagree with
    what Plasticity draws on the same part, the problem is in this step and not
    in the fill — see [Seeing the CAD structure](../guide/cad-structure.md).

[Next: choosing a generator →](generators.md)
