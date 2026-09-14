# 2. Choosing a generator

Step 1 handed over a boundary. This step turns it into **sides**, and the sides
are what choose the generator — so everything about how a patch is filled is
decided by where its corners land.

## Corners, then sides

Two corner tests run, and they miss opposite things.

**The angle test** flags a boundary vertex that turns sharper than *Corner Angle
Threshold* (135° of deviation by default). It is purely geometric, so it swallows
anything gentle: a 30° chamfer reads as a smooth stretch, lands mid-side, and
every generator paves straight across it.

**The topological test** flags the vertex where the neighbouring Plasticity face
changes — a real B-rep vertex, at any angle. But a face whose whole boundary runs
against one single neighbour has no junction at all, however square it looks.

Which one runs is set **per mode**, and that split is not cosmetic:

| Mode | Method | Why |
|---|---|---|
| Grid generators | *Angle* | A grid's side count **chooses the generator**, so every extra corner is an extra side. With topology on, a bevel — whose long side borders face after face — goes from a Quad with a clean grid to an N-Side with a pole in the middle of it. |
| [N-gon](../guide/ngon.md) | *Both* | An n-gon only *follows* its boundary, so extra corners cost it nothing, and they are the one thing that keeps a shallow chamfer the angle test cannot see. |

*Topology* falls back to the angle test on a boundary that yields no junction: a
patch with no corners is one single side, which every span generator would read
as unusable.

!!! info "You do not choose this — the rows are behind Developer Mode"

    Measured rather than asserted. Across the whole fixture at Mid resolution the
    method changes the result on **two objects out of sixteen** in either mode,
    and on those two the shipped default is the better one:

    | | Angle | Both / Topology |
    |---|--:|--:|
    | Cube Bevel Edges, *grid* | **0.221%**, 9 Quad + 2 Triangle + 2 Wedge | 0.838%, two faces fanned into N-Sides |
    | Carved Rounded Slot, *n-gon* | 96v/36f, **91 open edges** | **48v/28f, 0 open edges** |

    Every other object is identical under all three, and *Both* and *Topology*
    give the same result on **every** object in **both** modes — three values, two
    outcomes, and each mode already ships with the right one. If a `.blend` saved
    before this was hidden still carries a non-default, the settings tab says so
    and offers a **Reset Corner Detection** button.

### Too many corners

A tessellated curve can flag corner after corner, and a five-sided patch is
filled very differently from a four-sided one. So candidates are **ranked** by how
much the boundary bends there, and cut where consecutive scores fall off a cliff.

It is a *ratio*, never an absolute angle — that is the only thing separating the
two cases that both produce "more than four sides". A real hexagon bends the same
at every corner and has no cliff anywhere, so nothing is cut; a curve the angle
test over-sampled sits far below the real corners, and the drop is unmistakable.

Four is tried first, so a quad wins any tie, and cutting all the way down to a
triangle takes a much clearer cliff — a rectangle with one chamfered corner is
three 90° turns and two 45° ones, and a plain 2:1 rule called it a triangle.

**Topological corners are exempt** and take no part in the ranking. A junction is
a fact the mesh states outright, and a gentle chamfer's junction barely bends, so
ranking it would drop the very thing the topological test exists to catch.

Sides shorter than *Small Side Tolerance* are then merged into their neighbour,
which is what keeps a two-triangle sliver from counting as a side of its own.

### Too few corners

A single closed curve — a disc, a bore rim — has no corners by either test, so it
is one side, and no generator accepts one. Four corners are synthesised, but
**where** they go is read off the shape rather than spread evenly: turn is
measured over a *window* of the perimeter, because a tessellated rounded end is
dozens of individually insignificant turns and one real feature.

However many features it finds decides the generator: **two ends make a Wedge**,
three a Triangle, four a Quad. Only a boundary whose turn is genuinely uniform —
a circle — falls back to four points spread by arc length.

That matters most on a long strip curving back on itself (a rounded slot, a bore
wall, a ribbon around a feature). Its perimeter is dominated by its two long
sides, so evenly spaced quarter points land in the *middle* of them, and the
"quad" handed to the fill is half a long side plus half an end — which comes out
as a fan.

One corner is worse than none, so a boundary that yields exactly one is topped up
to four, *anchored on the real corner* — the one feature the face has lands on a
side boundary rather than mid-side.

Rings never get synthesised corners: a cornerless rim gives them no trouble, and
inventing four on each of two loops would pair their points across a shear
instead of straight across the band.

## The side count picks the generator

<figure class="diagram" markdown="0">
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 560 280" role="img" aria-label="The six generators and the shapes they produce">
  <g fill="none" stroke="#2980b9" stroke-width="1.5">
    <g transform="translate(20,10)">
      <rect x="6" y="30" width="88" height="30" fill="#eef5fb"/>
      <path d="M28 30V60M50 30V60M72 30V60M6 45H94"/>
      <g fill="#e6473d" stroke="none"><circle cx="6" cy="45" r="4"/><circle cx="94" cy="45" r="4"/></g>
    </g>
    <g transform="translate(160,10)">
      <path d="M10 78L50 12L90 78Z" fill="#eef5fb"/>
      <path d="M30 45L50 52M70 45L50 52M50 78L50 52"/>
      <g fill="#e6473d" stroke="none"><circle cx="10" cy="78" r="4"/><circle cx="50" cy="12" r="4"/><circle cx="90" cy="78" r="4"/></g>
    </g>
    <g transform="translate(300,10)">
      <rect x="12" y="14" width="76" height="64" fill="#eef5fb"/>
      <path d="M31 14V78M50 14V78M69 14V78M12 35H88M12 56H88"/>
      <g fill="#e6473d" stroke="none"><circle cx="12" cy="14" r="4"/><circle cx="88" cy="14" r="4"/><circle cx="88" cy="78" r="4"/><circle cx="12" cy="78" r="4"/></g>
    </g>
  </g>

  <g font-family="sans-serif" font-size="12" fill="#14405c" text-anchor="middle">
    <text x="70" y="104">Wedge — 2 sides</text>
    <text x="210" y="104">Triangle — 3</text>
    <text x="350" y="104">Quad — 4</text>
  </g>

  <g fill="none" stroke="#2980b9" stroke-width="1.5">
    <g transform="translate(20,130)">
      <path d="M50 7L86 33L72 76L28 76L14 33Z" fill="#eef5fb"/>
      <path d="M68 20L50 45M79 55L50 45M50 76L50 45M21 55L50 45M32 20L50 45"/>
      <circle cx="50" cy="45" r="3" fill="#8e44ad" stroke="none"/>
      <g fill="#e6473d" stroke="none"><circle cx="50" cy="7" r="4"/><circle cx="86" cy="33" r="4"/><circle cx="72" cy="76" r="4"/><circle cx="28" cy="76" r="4"/><circle cx="14" cy="33" r="4"/></g>
    </g>
    <g transform="translate(160,130)">
      <circle cx="50" cy="45" r="38" fill="#eef5fb"/>
      <circle cx="50" cy="45" r="18" fill="#ffffff"/>
      <path d="M88 45H68M76.9 71.9L62.7 57.7M50 83V63M23.1 71.9L37.3 57.7M12 45H32M23.1 18.1L37.3 32.3M50 7V27M76.9 18.1L62.7 32.3"/>
    </g>
    <g transform="translate(300,130)">
      <path d="M25 14H75L90 45L75 76H25L10 45Z" fill="#eef5fb"/>
      <g fill="#e6473d" stroke="none"><circle cx="25" cy="14" r="3"/><circle cx="75" cy="14" r="3"/><circle cx="90" cy="45" r="3"/><circle cx="75" cy="76" r="3"/><circle cx="25" cy="76" r="3"/><circle cx="10" cy="45" r="3"/></g>
    </g>
  </g>

  <g font-family="sans-serif" font-size="12" fill="#14405c" text-anchor="middle">
    <text x="70" y="224">N-Side — 5+</text>
    <text x="210" y="224">Ring — 2 loops</text>
    <text x="350" y="224">N-gon — a mode</text>
  </g>

  <g font-family="sans-serif" font-size="11" fill="#666">
    <text x="20" y="252">Red dots are corners. The N-Side pole's valence is the side count, not the</text>
    <text x="20" y="268">span; the Ring's rungs run straight across the band, one per point of each rim.</text>
  </g>
</svg>
</figure>

| Sides | Generator | What it makes |
|---|---|---|
| 2 | **Wedge** | a grid running along a strip: two ends, two long sides |
| 3 | **Triangle** | a three-way Coons fill |
| 4 | **Quad** | a Coons grid, `Span U` × `Span V` |
| 5+ | **N-Side** | one Coons sub-patch per side around a central pole |
| — | **Ring** | *two boundary loops*: a band of quads across the gap |
| — | **N-gon** | one face following the boundary, for flat patches |

The first two rows of that table are the ones that make the corner method matter:
the count is looked up in order, and specialised generators are tried before the
N-Side fallback.

The last two are **not** chosen by a side count, and that is where the order of
the decisions shows:

1. **Is it committed already?** A patch in the result mesh comes back as whatever
   it was built as — same rule as its spans. Hovering finished work must show
   what is there, not what the current mode would build.
2. **Does the mode ask for an n-gon?** <kbd>N</kbd>, or a patch committed as one.
3. **Can it take one?** One hard blocker: a patch that is not flat, which would
   get a flat lid over it. The panel names it rather than the key doing nothing.
   Holes are not a blocker — each one is bridged into the face around it, so a
   face with four holes comes back as five n-gons.
4. **Does it have two boundary loops?** Then it is a Ring candidate — but two
   loops is not the same thing as a band. A 200×100 plate with a 5 mm hole is an
   annulus too, and a Ring has to give both loops the same point count. So the
   gap between the loops is checked for evenness; a non-band that is flat is
   filled as an n-gon instead, and the panel says why. See
   [Faces with a hole](../guide/rings.md).
5. **Otherwise the side count decides.**

More than two loops is not handled: only the outer loop is used, and the panel
warns rather than silently paving over the holes.

## What a generator actually does

Every generator except the n-gon builds a grid by **Coons interpolation** between
its sides — the surface implied by the four (or three, or two) boundary curves —
and then **reprojects the interior onto the original CAD surface** through a BVH
built from that patch's own triangles.

<figure class="diagram" markdown="0">
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 560 190" role="img" aria-label="A Coons grid between the sides, and its interior reprojected onto the surface">
  <g font-family="sans-serif" font-size="11" fill="#555">
    <text x="10" y="18">the grid the sides imply</text>
    <text x="300" y="18">seen edge-on</text>
  </g>

  <g transform="translate(20,36)">
    <path d="M0 12C40 0 90 0 130 12" fill="none" stroke="#27ae60" stroke-width="2.5"/>
    <path d="M130 12C142 40 142 76 130 104" fill="none" stroke="#27ae60" stroke-width="2.5"/>
    <path d="M130 104C90 116 40 116 0 104" fill="none" stroke="#27ae60" stroke-width="2.5"/>
    <path d="M0 104C-12 76 -12 40 0 12" fill="none" stroke="#27ae60" stroke-width="2.5"/>
    <g fill="none" stroke="#7fa8c4" stroke-width="1">
      <path d="M32.5 5C36 40 36 76 32.5 111"/>
      <path d="M65 2C67 40 67 76 65 114"/>
      <path d="M97.5 5C95 40 95 76 97.5 111"/>
      <path d="M-4 35C40 27 90 27 134 35"/>
      <path d="M-6 58C40 52 90 52 136 58"/>
      <path d="M-4 81C40 89 90 89 134 81"/>
    </g>
    <g fill="#e6473d"><circle cx="0" cy="12" r="4"/><circle cx="130" cy="12" r="4"/><circle cx="130" cy="104" r="4"/><circle cx="0" cy="104" r="4"/></g>
  </g>

  <g transform="translate(310,50)">
    <path d="M0 60Q100 0 200 60" fill="none" stroke="#2c3e50" stroke-width="2"/>
    <path d="M0 60H200" fill="none" stroke="#b9c4cc" stroke-width="1" stroke-dasharray="4 3"/>
    <g stroke="#8e44ad" stroke-width="1.2">
      <path d="M40 58V43"/><path d="M80 58V33"/><path d="M120 58V33"/><path d="M160 58V43"/>
    </g>
    <g fill="#8e44ad">
      <circle cx="40" cy="40.8" r="3.5"/><circle cx="80" cy="31.2" r="3.5"/>
      <circle cx="120" cy="31.2" r="3.5"/><circle cx="160" cy="40.8" r="3.5"/>
    </g>
    <g fill="#e6473d"><circle cx="0" cy="60" r="4"/><circle cx="200" cy="60" r="4"/></g>
    <text x="0" y="92" font-family="sans-serif" font-size="10" fill="#666">interpolation puts interior points on the chord;</text>
    <text x="0" y="106" font-family="sans-serif" font-size="10" fill="#666">reprojection puts them back on the surface</text>
  </g>
</svg>
</figure>

Two consequences worth holding on to:

- **Boundary rows are left exactly where the loops put them.** They are samples
  of the real CAD boundary, and a neighbour welds to them — moving them would
  break the weld. (The one exception is a ring rim that had to be phase-aligned:
  it lands nowhere near a source vertex by construction, so it is reprojected
  like the interior.)
- **Deviation is an interior question.** Interior vertices are on the surface by
  construction, so measuring a patch's accuracy at its vertices reads ~0 on every
  shape and proves nothing. The benchmark measures across face *interiors*.

## The Coons patch itself

The fill above is a **Coons patch**: the surface that a quadrilateral's four
boundary curves imply, and nothing more. The idea in one line — *blend the
boundary twice, then subtract what the two blends counted twice.*

Half the difficulty is the notation, so:

| | |
|---|---|
| `u`, `v` | two numbers from 0 to 1 — the same coordinates as a UV map. A unit square is the domain; the patch is its image. |
| `C0(u)`, `C1(u)` | the bottom and top boundary curves, walked by `u`. |
| `D0(v)`, `D1(v)` | the left and right boundary curves, walked by `v`. |
| `P00 P10 P01 P11` | the four corners. **Each belongs to two curves at once**, and that sharing is the whole problem below. |
| `S(u, v)` | the point of the surface. |

Three terms are built from those, and combined as `S = Sv + Su − B`:

- `Sv = (1-v)·C0(u) + v·C1(u)` — ruled between **bottom and top**. It honours
  those two curves exactly and knows nothing about the sides.
- `Su = (1-u)·D0(v) + u·D1(v)` — ruled between **left and right**. It honours the
  sides and knows nothing about the bottom and top.
- `B(u, v) = (1-u)(1-v)·P00 + u(1-v)·P10 + (1-u)v·P01 + uv·P11` — the **bilinear**
  blend of the four corners alone: the warped quad you would get by ignoring the
  curvature of every edge.

<figure class="diagram" markdown="0">
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 560 175" role="img" aria-label="The two ruled surfaces, minus the bilinear patch of the corners, equals the Coons patch">
  <defs>
    <path id="cnBot" d="M10 80Q55 94 100 78"/>
    <path id="cnTop" d="M14 22Q56 8 98 18"/>
    <path id="cnLeft" d="M10 80Q8 50 14 22"/>
    <path id="cnRight" d="M100 78Q105 48 98 18"/>
  </defs>

  <g transform="translate(15,20)">
    <use href="#cnBot" fill="none" stroke="#27ae60" stroke-width="2.5"/>
    <use href="#cnTop" fill="none" stroke="#27ae60" stroke-width="2.5"/>
    <use href="#cnLeft" fill="none" stroke="#c9d2d8" stroke-width="1.5"/>
    <use href="#cnRight" fill="none" stroke="#c9d2d8" stroke-width="1.5"/>
    <g fill="none" stroke="#27ae60" stroke-width="1">
      <path d="M32.5 85.1V16.5M55 86.5L56 14M77.5 84.1L77 14.5"/>
    </g>
  </g>

  <g transform="translate(155,20)">
    <use href="#cnBot" fill="none" stroke="#c9d2d8" stroke-width="1.5"/>
    <use href="#cnTop" fill="none" stroke="#c9d2d8" stroke-width="1.5"/>
    <use href="#cnLeft" fill="none" stroke="#d35400" stroke-width="2.5"/>
    <use href="#cnRight" fill="none" stroke="#d35400" stroke-width="2.5"/>
    <g fill="none" stroke="#d35400" stroke-width="1">
      <path d="M9.5 65.1H101.75M10 50.5H102M11.5 36.1H100.75"/>
    </g>
  </g>

  <g transform="translate(295,20)">
    <path d="M10 80L100 78L98 18L14 22Z" fill="none" stroke="#7f8c8d" stroke-width="2"/>
    <g fill="none" stroke="#b9c4cc" stroke-width="1" stroke-dasharray="4 3">
      <path d="M55 79L56 20M12 51L99 48"/>
    </g>
    <g fill="#7f8c8d">
      <circle cx="10" cy="80" r="3.5"/><circle cx="100" cy="78" r="3.5"/>
      <circle cx="98" cy="18" r="3.5"/><circle cx="14" cy="22" r="3.5"/>
    </g>
  </g>

  <g transform="translate(435,20)">
    <use href="#cnBot" fill="none" stroke="#8e44ad" stroke-width="2.5"/>
    <use href="#cnTop" fill="none" stroke="#8e44ad" stroke-width="2.5"/>
    <use href="#cnLeft" fill="none" stroke="#8e44ad" stroke-width="2.5"/>
    <use href="#cnRight" fill="none" stroke="#8e44ad" stroke-width="2.5"/>
    <g fill="none" stroke="#a98ac4" stroke-width="1">
      <path d="M32.5 85.1V16.5M55 86.5L56 14M77.5 84.1L77 14.5"/>
      <path d="M9.5 65.1H101.75M10 50.5H102M11.5 36.1H100.75"/>
    </g>
  </g>

  <g font-family="sans-serif" font-size="18" fill="#555" text-anchor="middle">
    <text x="137" y="75">+</text>
    <text x="277" y="75">−</text>
    <text x="417" y="75">=</text>
  </g>

  <g font-family="sans-serif" font-size="11" text-anchor="middle">
    <text x="70" y="135" fill="#14512f">Sv — ruled in v</text>
    <text x="70" y="150" fill="#777">bottom to top</text>
    <text x="210" y="135" fill="#6b3d0c">Su — ruled in u</text>
    <text x="210" y="150" fill="#777">left to right</text>
    <text x="350" y="135" fill="#555">B — bilinear</text>
    <text x="350" y="150" fill="#777">the 4 corners, counted twice</text>
    <text x="490" y="135" fill="#4a2060">the Coons patch</text>
    <text x="490" y="150" fill="#777">through all four curves</text>
  </g>
</svg>
</figure>

### Why the bilinear term has to come off

Look at what each term is worth along **one** edge — the bottom one, `v = 0`:

- `Sv(u, 0) = 1·C0(u) + 0·C1(u) = C0(u)` — the real boundary curve.
- `Su(u, 0) = (1-u)·D0(0) + u·D1(0) = (1-u)·P00 + u·P10` — the straight chord
  between the two corners.
- `B(u, 0) = (1-u)·P00 + u·P10` — **the same chord, term for term.**

<figure class="diagram" markdown="0">
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 470 130" role="img" aria-label="Along the bottom edge, the ruled-in-u term and the bilinear term are the same chord">
  <g transform="translate(20,20)">
    <path d="M0 20L110 6" fill="none" stroke="#b9c4cc" stroke-width="1" stroke-dasharray="4 3"/>
    <path d="M0 20Q55 62 110 6" fill="none" stroke="#27ae60" stroke-width="2.5"/>
    <g fill="#7f8c8d"><circle cx="0" cy="20" r="3.5"/><circle cx="110" cy="6" r="3.5"/></g>
    <text x="55" y="70" font-family="sans-serif" font-size="11" fill="#14512f" text-anchor="middle">Sv at v = 0</text>
    <text x="55" y="85" font-family="sans-serif" font-size="10" fill="#777" text-anchor="middle">the real edge curve</text>
  </g>
  <g transform="translate(170,20)">
    <path d="M0 20Q55 62 110 6" fill="none" stroke="#b9c4cc" stroke-width="1" stroke-dasharray="4 3"/>
    <path d="M0 20L110 6" fill="none" stroke="#d35400" stroke-width="2.5"/>
    <g fill="#7f8c8d"><circle cx="0" cy="20" r="3.5"/><circle cx="110" cy="6" r="3.5"/></g>
    <text x="55" y="70" font-family="sans-serif" font-size="11" fill="#6b3d0c" text-anchor="middle">Su at v = 0</text>
    <text x="55" y="85" font-family="sans-serif" font-size="10" fill="#777" text-anchor="middle">the straight chord</text>
  </g>
  <g transform="translate(320,20)">
    <path d="M0 20Q55 62 110 6" fill="none" stroke="#b9c4cc" stroke-width="1" stroke-dasharray="4 3"/>
    <path d="M0 20L110 6" fill="none" stroke="#7f8c8d" stroke-width="2.5"/>
    <g fill="#7f8c8d"><circle cx="0" cy="20" r="3.5"/><circle cx="110" cy="6" r="3.5"/></g>
    <text x="55" y="70" font-family="sans-serif" font-size="11" fill="#555" text-anchor="middle">B at v = 0</text>
    <text x="55" y="85" font-family="sans-serif" font-size="10" fill="#777" text-anchor="middle">exactly the same chord</text>
  </g>
</svg>
<figcaption>Along that edge <code>Su - B</code> is zero, and what is left is <code>C0(u)</code>: the patch
sits on its own boundary.</figcaption>
</figure>

So `Sv + Su − B = C0(u)`. The parasitic chord cancels itself out and the true
curve is what remains — and the same argument runs on the other three edges. Put
shortly: each ruled surface already carries the corners inside it, so adding the
two counts the corners twice, and `B` is exactly that double contribution.

The quickest way to see it is at a corner, `(0, 0)`: `Sv = P00` and `Su = P00`,
so the sum is `2·P00`. Without the correction the patch would miss its own
corners by a factor of two.

!!! warning "It is not a refinement"

    Drop the bilinear term and you do not get a slightly wrong surface — you get
    one that no longer touches **any** of its boundary curves. For the same
    reason the four curves must genuinely meet at the corners: sides that do not
    close make the patch incoherent, which is why the boundary walk and the weld
    of [step 1](patches.md) come first.

### One point, step by step

For a given `(u, v)`:

1. Evaluate the four boundary curves: `C0(u)`, `C1(u)`, `D0(v)`, `D1(v)` — four
   points on the border.
2. `Sv`: blend bottom and top. The point slides along the green segment.
3. `Su`: blend left and right. A point on the orange segment.
4. `B`: blend the four corners — the position "without any curvature".
5. `S = Sv + Su − B`.

Because `S − Sv = Su − B`, those four points form a **parallelogram**: you start
from `Sv` — where the bottom and top curves put you — and apply the offset the
left and right curves have with respect to their own corners.

<figure class="diagram" markdown="0">
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 500 235" role="img" aria-label="Evaluating one point: Sv, Su, B and S form a parallelogram">
  <defs>
    <marker id="cnArrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">
      <path d="M0 0L10 5L0 10z" fill="#8e44ad"/>
    </marker>
  </defs>

  <g transform="translate(15,20)">
    <path d="M25 150Q135 195 245 150" fill="none" stroke="#27ae60" stroke-width="2.5"/>
    <path d="M25 30H245" fill="none" stroke="#27ae60" stroke-width="2.5"/>
    <path d="M25 150Q-15 90 25 30" fill="none" stroke="#d35400" stroke-width="2.5"/>
    <path d="M245 150V30" fill="none" stroke="#d35400" stroke-width="2.5"/>

    <path d="M135 172.5V30" fill="none" stroke="#27ae60" stroke-width="1" stroke-dasharray="5 4"/>
    <path d="M5 90H245" fill="none" stroke="#d35400" stroke-width="1" stroke-dasharray="5 4"/>

    <g fill="#27ae60"><circle cx="135" cy="172.5" r="4"/><circle cx="135" cy="30" r="4"/></g>
    <g fill="#d35400"><circle cx="5" cy="90" r="4"/><circle cx="245" cy="90" r="4"/></g>

    <g font-family="sans-serif" font-size="10" fill="#555">
      <text x="143" y="188">C0(u)</text>
      <text x="143" y="24">C1(u)</text>
      <text x="16" y="82">D0(v)</text>
      <text x="212" y="82">D1(v)</text>
    </g>

    <circle cx="130" cy="96" r="26" fill="none" stroke="#b9c4cc" stroke-width="1"/>
    <path d="M156 96H205" fill="none" stroke="#b9c4cc" stroke-width="1" stroke-dasharray="3 3"/>
  </g>

  <g>
    <path d="M420 97.6H384M420 142.6H384" fill="none" stroke="#8e44ad" stroke-width="1.5" marker-end="url(#cnArrow)"/>
    <path d="M420 97.6V142.6M380 97.6V142.6" fill="none" stroke="#c9d2d8" stroke-width="1" stroke-dasharray="4 3"/>
    <circle cx="420" cy="97.6" r="5" fill="#7f8c8d"/>
    <circle cx="380" cy="97.6" r="5" fill="#d35400"/>
    <circle cx="420" cy="142.6" r="5" fill="#27ae60"/>
    <circle cx="380" cy="142.6" r="6" fill="#8e44ad"/>
    <g font-family="sans-serif" font-size="11" fill="#555">
      <text x="430" y="94">B</text>
      <text x="366" y="88" fill="#6b3d0c">Su</text>
      <text x="430" y="147" fill="#14512f">Sv</text>
      <text x="340" y="163" fill="#4a2060">S(u,v)</text>
      <text x="332" y="196" font-size="10" fill="#777">the same offset, applied</text>
      <text x="332" y="209" font-size="10" fill="#777">from Sv instead of from B</text>
    </g>
  </g>
</svg>
<figcaption>Enlarged: on a real patch those four points sit within a fraction of a cell of
one another.</figcaption>
</figure>

A worked example, with three straight edges and only the bottom one curved.
Corners `P00=(0,0)`, `P10=(10,0)`, `P01=(0,10)`, `P11=(10,10)`, and
`C0(u) = (10u, -4u(1-u))`. At `u = 0.5`, `v = 0.25`:

```
C0(0.5)  = (5, -1)     C1(0.5)  = (5, 10)    Sv = 0.75*(5,-1) + 0.25*(5,10) = (5, 1.75)
D0(0.25) = (0, 2.5)    D1(0.25) = (10, 2.5)  Su = (5, 2.5)
B = (5, 2.5)
S = (5, 1.75) + (5, 2.5) - (5, 2.5) = (5, 1.75)
```

`Su` and `B` are identical here because the left and right edges are straight and
never leave their own chords: their difference is zero, so only the bottom edge's
curvature travels into the surface. A dip of 1 unit at the middle of that edge is
still 0.25 deep a quarter of the way up, fading linearly to nothing at the top.

### What that means here

- **The addon uses the discrete version**, which is the same formula evaluated at
  grid indices — `u = i / span_u`, `v = j / span_v`. The boundary rows are placed
  first (that is what the sides, the spans and the matching are all about) and
  every interior vertex is then one evaluation of `S(u, v)`, in
  `geometry.coons_patch_grid`. Nothing is solved or iterated.
- **The parameterisation is uniform in index, not in space.** What makes the
  cells even is that the sides were resampled by **arc length** beforehand.
- **Bilinear blending gives only C0 continuity between neighbouring patches** —
  position, not tangent. Tangent continuity would take cubic Hermite blending
  functions and the cross derivatives at the corners (twist vectors), which
  nothing in the import can supply. Here the shape is carried by **reprojecting
  the interior onto the CAD surface** instead, and the seam between two patches
  is closed by welding shared vertices rather than by matching tangents.

## Where the spans come from

The generator is chosen; how many segments it puts along each direction is
settled separately, and **the order is fixed** because each step exists to
survive the one before it:

1. **The generator's own default**, computed from the patch's edge lengths.
2. **The resolution preset** scales that (Very Low ¼ … Extreme ×4).
3. **Propagation** from an already-committed neighbour, keyed by the pair of
   corner ids the shared side runs between. Scaling *this* by the preset would
   break the weld it exists to make.
4. **The spans the patch was committed with**, if it is being re-edited. These
   beat both of the above, or re-opening a patch would silently re-shape work you
   had already tuned.
5. **Anything you typed or scrolled** for this patch.
6. **The winning matches** — see the next step. A pin always decides the span; an
   automatic match only seeds it the first time, so scrolling a span on a side
   that happens to border a committed neighbour is not silently undone.

Never below 1 segment. An N-Side is a special case here: its sides do not share
one span, so the allocation is solved across the whole ring of sides at once, and
whatever cannot be fitted is refused rather than approximated.

[Next: matching a neighbour →](matching.md)
