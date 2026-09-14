# Seeing the CAD structure

The bridge sends a triangle soup, so a Plasticity import reads as one
undifferentiated field of triangles — even though the mesh records which triangle
belongs to which CAD face.

Two overlays put that structure back. They make very different kinds of claim,
and the distinction matters.

| | Key | |
|---|---|---|
| **Plasticity edges** | <kbd>E</kbd> | exact, recovered from the face ids |
| **Surface flow** | <kbd>Ctrl</kbd>+<kbd>E</kbd> | **derived**, not imported |


<video autoplay loop muted playsinline poster="../../assets/img/cad-edges.jpg">
  <source src="../../assets/video/cad-edges.webm" type="video/webm">
</video>

## Plasticity edges are exact

A boundary half-edge `(a, b)` of one patch is matched by `(b, a)` of the patch
across it. So a **B-rep edge** is the maximal run of boundary segments whose
neighbouring face id does not change, and a **B-rep vertex** is where it does —
the same signal the topological corner test runs on.

Those vertices are also the only points patches weld to each other *by identity*,
which is why they are worth seeing.

**Show CAD Vertices** is off by default: on a real part every junction is a dot,
and a few hundred of them bury the edges they punctuate.


## Surface flow is derived

Plasticity's isoparametric curves come from each face's NURBS parameterisation,
and **none of it crosses the bridge**. The protocol carries no surface parameters
at all.

What is drawn instead is **the grid each face would be retopologized into**: the
same corner split, the same generators, at a low span, reprojected onto the
surface. On a fillet or a swept face that lands very close to the true isoparms,
because both answer the same question about the same boundary.


## Settings

| Setting | Default |
|---|---|
| **Show CAD Edges** | off |
| **Show CAD Vertices** | off |
| **Show Surface Flow** | off |
| **Flow Density** | 3 |
| **Show For** | object / patch under the cursor |
| **Draw Through the Mesh** | off |
| **CAD Edge Color / Width** | cyan, 2 px |
| **Surface Flow Color** | violet |
