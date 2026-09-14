# Benchmark results

Every patch of every fixture shape, retopologized and measured at **MID** resolution.

Generated — do not hand-edit:

```bash
blender tests/fixtures/TestCases.blend --background --python scripts/gen_results.py
```

| | |
|---|---|
| Generated | 2026-09-14 |
| Blender | 5.1.1 |
| Addon | 0.78.0 (2026-09-14-m) |
| Resolution | MID |
| Fixture | `tests/fixtures/TestCases.blend`, 21 objects |

**Deviation is a percentage of each object's bounding-box diagonal, sampled across face interiors** — never at vertices, which the generators put on the surface by construction and which therefore read ~0 on every shape. The `vertex` column is shown only to make that point: it is the number a naive measurement would report.

## Overview

| Object | Source tris | CAD faces | Generators | Result | Deviation max | Open edges |
|---|--:|--:|---|--:|--:|--:|
| `Carved Rounded Slot` | 156 | 11 | 10&nbsp;Quad, 1&nbsp;Ring | 172v / 131f | 0.0690% | 90 |
| `Cone` | 188 | 2 | 2&nbsp;Quad | 614v / 612f | 10.4833% | 0 |
| `Cube Bevel Edges` | 1556 | 13 | 9&nbsp;Quad, 2&nbsp;Triangle, 2&nbsp;Wedge | 1035v / 982f | 0.4331% | 118 |
| `Cube Chamfer Edges` | 20 | 9 | 8&nbsp;Quad, 1&nbsp;N-Side | 34v / 31f | 0.0001% | 6 |
| `Cube Two Booleans` | 493 | 15 | 8&nbsp;Quad, 4&nbsp;N-Side, 3&nbsp;Ring | 2045v / 1947f | 11.1111% | 244 |
| `Cylinder` | 320 | 3 | 2&nbsp;Quad, 1&nbsp;Ring | 1592v / 1590f | 0.0602% | 0 |
| `Flat Loop` | 622 | 4 | 4&nbsp;Ring | 232v / 232f | 0.0808% | 0 |
| `Intersecting Curves` | 210 | 15 | 11&nbsp;Quad, 4&nbsp;Triangle | 634v / 608f | 2.6315% | 98 |
| `Loopsided Chamfers Cube` | 28 | 14 | 14&nbsp;Quad | 34v / 32f | 0.0001% | 0 |
| `Non Cordal Bevels` | 336 | 18 | 18&nbsp;Quad | 42v / 32f | 1.8776% | 16 |
| `Plate` | 622 | 5 | 3&nbsp;Ring, 2&nbsp;Quad | 775v / 657f | 0.0770% | 232 |
| `Plate And Cylinder` | 2803 | 14 | 10&nbsp;Quad, 4&nbsp;Ring | 2229v / 2139f | 0.1399% | 184 |
| `Shape with holes` | 616 | 17 | 13&nbsp;Quad, 2&nbsp;N-gon, 2&nbsp;Ring | 243v / 200f | 8.9746% | 0 |
| `Sphere` | 12320 | 1 | 1&nbsp;unusable | — | — | — |
| `Square Plate Small Hole` | 28 | 11 | 10&nbsp;Quad, 1&nbsp;Ring | 16v / 14f | 0.0001% | 0 |
| `Square Plate Small Hole Far Away` | 28 | 11 | 10&nbsp;Quad, 1&nbsp;Ring | 16v / 14f | 0.0007% | 0 |
| `Square Plate Small Hole Scaled Down` | 28 | 11 | 10&nbsp;Quad, 1&nbsp;Ring | 12v / 10f | 0.0661% | 0 |
| `Stairs Beveled` | 1392 | 31 | 27&nbsp;Quad, 2&nbsp;N-Side, 2&nbsp;Wedge | 202v / 158f | 0.8616% | 112 |
| `Stairs Curves and Bevel` | 5971 | 13 | 9&nbsp;Quad, 4&nbsp;Triangle | 8957v / 8747f | 1.6727% | 546 |
| `Torus` | 27126 | 1 | 1&nbsp;unusable | — | — | — |
| `Truncated Cone` | 374 | 3 | 2&nbsp;Quad, 1&nbsp;Ring | 1664v / 1662f | 0.0413% | 0 |

## Deviation from the CAD surface

| Object | mean | rms | p95 | max | vertex max |
|---|--:|--:|--:|--:|--:|
| `Carved Rounded Slot` | 0.0059% | 0.0161% | 0.0431% | **0.0690%** | 0.0002% |
| `Cone` | 0.0540% | 0.3095% | 0.1715% | **10.4833%** | 0.0000% |
| `Cube Bevel Edges` | 0.0397% | 0.0864% | 0.2083% | **0.4331%** | 0.0013% |
| `Cube Chamfer Edges` | 0.0000% | 0.0000% | 0.0000% | **0.0001%** | 0.0000% |
| `Cube Two Booleans` | 0.0796% | 0.3972% | 0.2428% | **11.1111%** | 0.0005% |
| `Cylinder` | 0.0171% | 0.0247% | 0.0464% | **0.0602%** | 0.0006% |
| `Flat Loop` | 0.0153% | 0.0263% | 0.0562% | **0.0808%** | 0.0000% |
| `Intersecting Curves` | 0.0033% | 0.0389% | 0.0181% | **2.6315%** | 0.0002% |
| `Loopsided Chamfers Cube` | 0.0000% | 0.0000% | 0.0001% | **0.0001%** | 0.0000% |
| `Non Cordal Bevels` | 0.3398% | 0.6903% | 1.6046% | **1.8776%** | 0.0000% |
| `Plate` | 0.0072% | 0.0179% | 0.0478% | **0.0770%** | 0.0001% |
| `Plate And Cylinder` | 0.0145% | 0.0261% | 0.0599% | **0.1399%** | 0.0081% |
| `Shape with holes` | 0.2015% | 0.6966% | 0.5635% | **8.9746%** | 0.0000% |
| `Square Plate Small Hole` | 0.0000% | 0.0000% | 0.0000% | **0.0001%** | 0.0000% |
| `Square Plate Small Hole Far Away` | 0.0000% | 0.0001% | 0.0000% | **0.0007%** | 0.0000% |
| `Square Plate Small Hole Scaled Down` | 0.0059% | 0.0172% | 0.0441% | **0.0661%** | 0.0001% |
| `Stairs Beveled` | 0.0726% | 0.1804% | 0.5608% | **0.8616%** | 0.0000% |
| `Stairs Curves and Bevel` | 0.0095% | 0.0713% | 0.0234% | **1.6727%** | 0.0118% |
| `Truncated Cone` | 0.0076% | 0.0121% | 0.0251% | **0.0413%** | 0.0009% |

A large `max` against a modest `p95` means a few samples are far out rather than the whole surface being off — which is the signature of a hole being paved over, or an apex no grid represents.

## Topology

| Object | Verts | Faces | Face sizes | Open edges | Non-manifold | Unwelded | Inward | Interior poles |
|---|--:|--:|---|--:|--:|--:|--:|--:|
| `Carved Rounded Slot` | 172 | 131 | 131&times;4-gon | 90 | 0 | 0 | 0 | 4 |
| `Cone` | 614 | 612 | 612&times;4-gon | 0 | 0 | 0 | 0 | 8 |
| `Cube Bevel Edges` | 1035 | 982 | 12&times;3-gon, 970&times;4-gon | 118 | 0 | 0 | 0 | 2 |
| `Cube Chamfer Edges` | 34 | 31 | 31&times;4-gon | 6 | 0 | 0 | 0 | 7 |
| `Cube Two Booleans` | 2045 | 1947 | 1947&times;4-gon | 244 | 0 | 8 | 89 | 8 |
| `Cylinder` | 1592 | 1590 | 1590&times;4-gon | 0 | 0 | 0 | 0 | 8 |
| `Flat Loop` | 232 | 232 | 232&times;4-gon | 0 | 0 | 0 | 0 | 0 |
| `Intersecting Curves` | 634 | 608 | 40&times;3-gon, 568&times;4-gon | 98 | 0 | 0 | 0 | 5 |
| `Loopsided Chamfers Cube` | 34 | 32 | 32&times;4-gon | 0 | 0 | 0 | 0 | 8 |
| `Non Cordal Bevels` | 42 | 32 | 32&times;4-gon | 16 | 0 | 0 | 0 | 8 |
| `Plate` | 775 | 657 | 657&times;4-gon | 232 | 0 | 0 | 0 | 4 |
| `Plate And Cylinder` | 2229 | 2139 | 2139&times;4-gon | 184 | 0 | 0 | 0 | 4 |
| `Shape with holes` | 243 | 200 | 190&times;4-gon, 1&times;8-gon, 1&times;9-gon, 2&times;10-gon, 1&times;12-gon, 1&times;13-gon, 1&times;15-gon, 1&times;19-gon, 2&times;21-gon | 0 | 0 | 0 | 0 | 78 |
| `Square Plate Small Hole` | 16 | 14 | 14&times;4-gon | 0 | 0 | 0 | 0 | 8 |
| `Square Plate Small Hole Far Away` | 16 | 14 | 14&times;4-gon | 0 | 0 | 0 | 0 | 8 |
| `Square Plate Small Hole Scaled Down` | 12 | 10 | 10&times;4-gon | 0 | 0 | 0 | 0 | 8 |
| `Stairs Beveled` | 202 | 158 | 4&times;3-gon, 154&times;4-gon | 112 | 0 | 0 | 6 | 2 |
| `Stairs Curves and Bevel` | 8957 | 8747 | 106&times;3-gon, 8641&times;4-gon | 546 | 0 | 0 | 2 | 0 |
| `Truncated Cone` | 1664 | 1662 | 1662&times;4-gon | 0 | 0 | 0 | 0 | 8 |

**Non-manifold, unwelded and inward-facing are zero everywhere**, which is the set worth being strict about. "Inward" counts faces pointing the opposite way from the CAD surface under them -- a whole patch can turn over without changing any other number in this table, which is exactly what a matched rim leading a band once did. Open-edge counts are a worst case rather than a quality score: this run commits in the fixture's own face order, and auto-matching can only match an *already committed* neighbour, so a patch baked early can never weld to one baked later.

## Cell quality

| Object | Aspect p50 | p95 | max | Skew p50 | p95 | max | Edge cv |
|---|--:|--:|--:|--:|--:|--:|--:|
| `Carved Rounded Slot` | 1.70 | 10.00 | **10.0** | 10.9° | 55.0° | 69.9° | 1.36 |
| `Cone` | 1.11 | 2.33 | **2.9** | 28.8° | 67.3° | 85.3° | 0.48 |
| `Cube Bevel Edges` | 1.50 | 1.92 | **11.2** | 0.0° | 1.0° | 70.6° | 1.09 |
| `Cube Chamfer Edges` | 1.12 | 4.83 | **6.0** | 0.0° | 35.3° | 45.0° | 0.29 |
| `Cube Two Booleans` | 1.93 | 7.42 | **3301639.9** | 9.3° | 85.9° | 90.0° | 1.21 |
| `Cylinder` | 1.01 | 1.08 | **1.1** | 1.5° | 37.9° | 83.3° | 0.09 |
| `Flat Loop` | 1.10 | 1.11 | **1.1** | 3.5° | 3.9° | 3.9° | 0.04 |
| `Intersecting Curves` | 7.11 | 32.81 | **34.1** | 9.9° | 64.3° | 85.7° | 1.29 |
| `Loopsided Chamfers Cube` | 2.21 | 7.69 | **7.7** | 0.0° | 44.0° | 44.0° | 0.50 |
| `Non Cordal Bevels` | 2.37 | 5.80 | **5.8** | 0.0° | 45.0° | 45.0° | 0.54 |
| `Plate` | 1.05 | 1.11 | **1.1** | 6.2° | 52.9° | 83.3° | 0.12 |
| `Plate And Cylinder` | 1.12 | 1.81 | **10.0** | 0.0° | 34.5° | 79.9° | 1.63 |
| `Shape with holes` | 1.57 | 9.25 | **163.9** | 0.0° | 54.8° | 134.8° | 1.81 |
| `Square Plate Small Hole` | 5.00 | 800.00 | **800.0** | 0.0° | 45.0° | 45.0° | 0.81 |
| `Square Plate Small Hole Far Away` | 5.00 | 798.92 | **798.9** | 0.0° | 45.0° | 45.0° | 0.81 |
| `Square Plate Small Hole Scaled Down` | 3.54 | 7.07 | **7.1** | 0.0° | 45.0° | 45.0° | 0.68 |
| `Stairs Beveled` | 2.29 | 9.78 | **14.3** | 19.6° | 87.9° | 90.0° | 1.05 |
| `Stairs Curves and Bevel` | 1.76 | 9.80 | **47.6** | 10.9° | 39.8° | 90.0° | 0.69 |
| `Truncated Cone` | 1.13 | 1.60 | **1.7** | 2.2° | 43.0° | 84.5° | 0.31 |

Percentiles and worst, never means: the failure mode is a handful of degenerate cells from a fan or a stretched band, and a mean hides exactly that. `Edge cv` is the coefficient of variation of edge length — how uneven the mesh is overall.

## One shape, three placements

The same CAD shape, modelled three times in Plasticity: at the origin, out at (50, 100, 20), and scaled to a hundredth. Because the geometry is identical, **any difference here is attributable to placement alone**.

| Placement | Result | Generators | Deviation max | Open edges | Aspect max |
|---|--:|---|--:|--:|--:|
| `Square Plate Small Hole` | 16v / 14f | 10&nbsp;Quad, 1&nbsp;Ring | 0.0001% | 0 | 800 |
| `Square Plate Small Hole Far Away` | 16v / 14f | 10&nbsp;Quad, 1&nbsp;Ring | 0.0007% | 0 | 799 |
| `Square Plate Small Hole Scaled Down` | 12v / 10f | 10&nbsp;Quad, 1&nbsp;Ring | 0.0661% | 0 | 7 |

The generators and side counts agree across all three — corner detection and generator selection are scale-stable. It is the density that moves, because neither absolute tolerance is scaled to the part: `boundary_weld_distance` defaults to 1e-4 *in metres*, and `patch_data`'s weld epsilon is a hard-coded 1e-5. Scaling the weld distance to the part recovers some of the difference on the small one and none of it on the far one, so a second cause is still open.

## Commit order changes the result

Same geometry, same generator chosen for every face — only the order patches are committed in differs. Span propagation seeds everything downstream from whichever patch commits first.

| Object | Fixture order | Face id ascending | Face id descending |
|---|--:|--:|--:|
| `Cube Chamfer Edges` | 34v / 31f | 32v / 27f | 26v / 23f |
| `Cube Bevel Edges` | 1035v / 982f | 922v / 865f | 1196v / 1148f |

This is why the golden table in `tests/test_fixtures.py` treats generator and side count as the entries that should hold steady, and vertex and face counts as regression detectors rather than quality scores.

## Known gaps

Each is asserted in `tests/test_fixtures.py` as *still broken*, so it fails when it starts working and forces the expectations to be updated. See `tests/fixtures/README.md` for the full write-up.

| # | Gap | Where it shows |
|--:|---|---|
| 1 | One shape at three placements gives three different retopologies | `Square Plate Small Hole` &times;3 |
| 2 | A small hole is still filled as a band, stretching quads from the outline to the hole | `Square Plate Small Hole` |
| 3 | A closed periodic face has no boundary loop, so it is silently unpickable | `Sphere`, `Torus` |
| 4 | A cone's apex is a singularity no quad grid represents | `Cone` |
| 5 | Faces past two boundary loops have every hole but the outer boundary paved over | `Shape with holes` |
| 6 | Symmetric pentagons resolve to different generators | `Cube Chamfer Edges` |
