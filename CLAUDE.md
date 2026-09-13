# Plasticity Retop — working notes

Blender addon: patch-based retopology for CAD meshes imported through the
[plasticity-blender-addon](https://github.com/nkallen/plasticity-blender-addon)
bridge. Inspired by the Maya/Blender "Retop Tool" (Patreon: MayaMatters);
this is an independent implementation.

## Commands

```bash
python scripts/run_tests.py          # headless test suite (needs Blender only)
python scripts/deploy.py             # copy into Blender's addons folder
python scripts/deploy.py --list      # show detected Blender config dirs

# retopologize every patch of a real bridge export and measure the result:
# deviation from the CAD surface, topology, cell quality
blender tests/fixtures/TestCases.blend --background --factory-startup --python scripts/benchmark.py
blender tests/fixtures/TestCases.blend --background --factory-startup --python scripts/benchmark.py -- --resolution HIGH --object Cylinder

# why a recovered boundary disagrees with the CAD face Plasticity draws:
# missed welds, tessellations that don't pair, pinched loops
blender YourFile.blend --background --factory-startup --python scripts/diagnose_edges.py
blender YourFile.blend --background --factory-startup --python scripts/diagnose_edges.py -- --object Body --face 1234

# regenerate the two documents derived from that fixture
blender tests/fixtures/TestCases.blend --background --factory-startup --python scripts/gen_results.py       # RESULTS.md
blender tests/fixtures/TestCases.blend --background --factory-startup --python scripts/gen_expectations.py  # the EXPECTED table

python scripts/build_zip.py          # dist/<name>-<version>.zip, what a release ships
python scripts/build_zip.py --check  # only verify the two version literals agree
```

**`--factory-startup` is not optional on those five**, however harmless a
read-only script looks: run without it and every installed addon loads too, and
one of them *saved the fixture over itself* on quit (it left a `.blend1` beside
it, which is the tell). The file is frozen — `git status` after any run against
it, and `git checkout` it back if it moved.

`RESULTS.md` is that benchmark written up as tables, and `gen_expectations.py`
emits the golden table `tests/test_fixtures.py` asserts on. **Both are
generated — never hand-edit either.** A re-export of the fixture renumbers
every Plasticity face id even when no vertex moves, so a stale table makes
"the fixture changed" indistinguishable from "the code regressed"; regenerate
and read the diff instead, where generator and side count per face are the
entries that should hold steady.

`tests/fixtures/TestCases.blend` is real Plasticity output imported through the
bridge — the one place the input contract is tested rather than assumed, since
every other test builds its mesh from the same mental model as the code.
`tests/test_fixtures.py` pins what comes out of it, and
`tests/fixtures/README.md` says what each shape is for, what it deliberately
does *not* cover, and why the file must be treated as frozen. **Deviation is
measured across face interiors, never at vertices** — the generators reproject
interior vertices onto the surface by construction, so a vertex-only figure
reads ~0 on every shape and proves nothing.

There may be no system Python (Windows' `python` can be the Store stub, and
`py` may not exist). Both scripts are stdlib-only, so run them with Blender's
bundled interpreter — `<Blender>/<ver>/python/bin/python.exe` — or invoke a
test file straight through Blender:
`blender --background --factory-startup --python tests/test_operators.py`.
**Always confirm the panel's version string changed after deploying**: a deploy
that never ran looks exactly like a feature that doesn't work.

Blender is found via `--blender`, `$BLENDER`, `PATH`, then usual install
paths. Plasticity itself is **not** needed to develop or test: the tests build
synthetic meshes carrying the same custom properties the bridge writes.

After deploying, use the panel's **Reload Addon Only** button — Blender's own
global "Reload Scripts" (which the panel deliberately does not offer beside it)
can silently half-fail when another installed addon errors during its own
reload. A reload that leaves a module stale is the one failure
the version string cannot report; see the reload invariant below.

**A deploy switches Developer Mode on by itself.** `scripts/deploy.py` writes
`.deployed` into the copy it makes, carrying a timestamp, and
`prefs.seed_developer_mode` (called from `prefs.register`) turns the preference
on when that stamp differs from the one it last acted on (`dev_seed_stamp`).
Deploying *is* the statement that this is a working copy, and a deploy that
lands with the reload button still hidden looks exactly like a deploy that did
not land -- which is the one failure the version string exists to report.
A stamp rather than a flag so it comes back on after *every* deploy while
still being switchable off in between; `developer_mode()` keeps reading the
preference, never the marker, or turning it off could not work at all. The name
`.deployed` is spelled in **two files and cannot be shared** -- `scripts/` is
excluded from the deploy, so the installed addon has no `deploy.py` to import
it from -- which is the bl_info/version drift shape all over again, hence
`tests/test_deploy_marker.py` comparing the two literals. The marker is in
`SKIP_DIRS`, so a release zip can never carry one; seeding is silent when
`keymap.preferences()` is None, so the tests and `--background` are untouched.
Nothing can turn a preference on in a Blender that is already running, so the
first deploy needs a restart or a re-enable before the button appears.

**That button is behind `prefs.developer_mode`, off by default**, and so is
the red stale-load warning. Neither means anything to someone who installed the
addon from a release zip: there is one copy of the code, nothing writes over it
between reloads, and the way to get new code is to install the new zip. Left on
for everyone, a reload button reads as a fix-it button for any
misbehaviour, which is exactly what it is not. `prefs.developer_mode()` reads
through `keymap.preferences()`, which returns None outside an installed addon —
the tests and `--background` — so it is off there too and nothing headless can
come to depend on a user setting.

**A release is a zip, and `scripts/build_zip.py` builds it.** One top-level
folder named after the package, with the addon inside: Blender installs a zip
by extracting it into the addons folder, so files at the root scatter loose
among every other addon and `from . import keymap` has no package to be
relative to. It excludes exactly what `deploy.py` excludes, *importing* that
list rather than restating it — two copies would disagree the first time a
directory was added, and a release quietly carrying the test suite, the fixture
`.blend` and the built docs site is not something anyone would notice.
`.github/workflows/release.yml` runs it on a `v*` tag and attaches the result.

**`bl_info["version"]` and `version.ADDON_VERSION` are two literals in two
files and they drift.** bl_info sat at `0.1.0` while the panel said `0.56.0`;
Blender parses bl_info as *source*, before any of this code runs, so nothing
can derive one from the other. `build_zip.py --check` refuses to build when
they disagree, and the release workflow also checks the tag names the same
number — a mislabelled asset is worse than a failed build.

Bump `version.py` (`ADDON_VERSION` / `BUILD_ID`) on every change: the panel
shows it, and it's the only reliable way to confirm a reload actually took.
The panel also compares the constants Python holds in memory against what
`version.py` reads *on disk* (`version.stale_load()`) and says so in red when
they differ — that is the "I deployed and nothing changed" state, and the
tracebacks it produces cite line numbers that don't match the file you're
reading. Diagnose that mismatch before anything else. It only works if the
version was bumped, which is the discipline above.

## Writing the docs (`docs/`)

The site is MkDocs on the `readthedocs` theme. `.github/workflows/docs.yml`
publishes it to GitHub Pages on a push to `main` that touches `docs/`,
`mkdocs.yml` or `requirements-docs.txt`. A new page must be added to the `nav`
in `mkdocs.yml` or it ships unreachable.

Check a change before pushing. There is no system Python, so use Blender's:

```bash
<Blender>/<ver>/python/bin/python.exe -m pip install -r requirements-docs.txt
<Blender>/<ver>/python/bin/python.exe -m mkdocs build --strict
```

`--strict` fails on a dead internal link, which is the only thing that keeps the
site honest as pages are renamed.

**Write short paragraphs.** Three or four lines, one idea each. A ten-line
paragraph is two or three paragraphs that were never separated.

**Lead with the point.** The first sentence of a paragraph says what is true.
What follows supports it.

**Cut the clause after the dash.** A sentence that ends with ` — ` and a
qualification usually ends twice. Most of those asides carry no information the
reader needs; delete them. If one does matter, it is a sentence of its own.

**A bullet list is for parallel items**, not for one sentence chopped into
pieces. Three bullets that only make sense read in order should be a paragraph.

**Do not write the docs in this file's voice.** CLAUDE.md is dense on purpose:
it records why a decision was made, for whoever changes the code next. The docs
answer what the addon does, for someone using it. Measurements, fixture numbers
and rejected alternatives belong here, not there.

**Explain a thing once.** The `how-it-works` pages are a numbered pipeline;
link to the step that owns a subject instead of restating it.

**Diagrams are hand-written inline SVG**, inside
`<figure class="diagram" markdown="0">`. Match the existing palette: blue and
purple for the addon's own geometry, green and orange for the two directions of
a fill, red for a corner or a B-rep vertex, grey for what is inert. Compute the
coordinates rather than eyeballing them, and open the page before pushing. An
SVG with a typo renders as nothing at all, and the build will not say so.

**The theme is plain MkDocs plus admonitions.** Tables, fenced code,
`attr_list`, `def_list` and `md_in_html` are available. Math, Mermaid, content
tabs and collapsible blocks are not. An unsupported extension builds cleanly and
reads as broken, so write formulas as inline code.

## Input data contract

The bridge writes two custom properties on each imported mesh:

- `mesh["groups"]` — flat list of `[loop_start, loop_count]` pairs, polygon order
- `mesh["face_ids"]` — one Plasticity face id per group, same order

One Plasticity face = one **patch**. The bridge is only needed at import time;
nothing here talks to it at runtime.

**That is the whole of it — there is no edge data.** The wire protocol carries
`vertices, faces, normals, groups, face_ids` and nothing else (verified in the
bridge's `client.decode_object_data` / `handler.py`), so no edge ids and no
"this segment is a real CAD edge" flag. What the bridge *does* also apply is
Plasticity's own per-loop normals, as custom split normals with
`use_smooth = True` everywhere — the true surface normals, not facet ones.

The equivalent of edge identity is derivable anyway, from `face_ids` alone:
along a patch's boundary, each half-edge's opposite polygon names the
*neighbouring* patch, and the vertex where that neighbour changes is a genuine
B-rep vertex — the junction between two CAD edges. That is what
`patch_data.build_directed_owners` / `boundary_neighbours_for_loop` recover,
and what the topological corner detector runs on.

**But the two faces meeting along a CAD edge do not always tessellate it the
same way**, and that pairing needs them to. On a real part the finer side drops
vertices in the *middle* of the coarser side's boundary segments -- a T-junction
along the border -- so the reversed half-edge simply is not there and the
segment reports no neighbour at all. It is not a rare case: on one exported
object, 2327 of 4857 boundary segments came back unmatched this way, and on the
whole file 1312 of 2287 faces had at least one. Downstream that is *not* a quiet
degradation. `detect_topological_corners` fires wherever the neighbour changes,
so every such segment becomes a phantom B-rep vertex; the side count picks the
generator, so the patch is filled by the wrong one; and `cad_display` draws one
shared border as a string of unrelated edges. That is the reported symptom
"Blender's patch doesn't match Plasticity's face".
`patch_data.resolve_neighbours_by_geometry` is the fallback, and its shape is
the point: it runs **only** on segments the exact pairing missed, so a cleanly
tessellated mesh pays one scan that finds nothing. See the invariant below for
why its tolerance is a share of the *model extent*.

**The bridge tessellates each face separately, so patch borders are
duplicated.** The two faces meeting along a B-rep edge each carry their own
copy of every vertex on it, at the same position under different indices. Any
topology work must therefore go through `patch_data.build_weld_map()`
(position-based KDTree merge) before it can tell a patch border from a hole:
without it no boundary half-edge ever finds its opposite, so no patch can name
its neighbour and the topological corner test and `cad_display` have nothing
to run on.

*Inside* one face is a separate question, and the code no longer assumes an
answer. `build_weld_map` welds only `patch_data.weld_candidates` — the
vertices lying on an edge Blender's own connectivity leaves unshared (fewer
than two polygons on it). A vertex strictly inside a patch has a polygon on
both sides of every edge it touches, which means its neighbours already share
its index and there is no second copy of that point to merge it with. That
makes the scoping safe whichever way the bridge behaves: where a face's
triangles are natively shared the KD-tree only sees the borders, and where
they are not, every edge carries one polygon, `weld_candidates` returns None,
and the whole mesh goes in exactly as before (`tests/test_unwelded.py` pins
that end, `tests/test_weld_scope.py` the other).

The one behaviour genuinely given up: an interior vertex coincident with
something is no longer merged. That is a T-junction, which a B-rep kernel does
not emit, and merging it never affected the loops anyway.

**The weld may never reach across a real edge, so the epsilon is capped by the
mesh's own shortest one** (`shortest_edge`, half of it). An edge is the mesh
saying outright that its two ends are distinct points of the surface; welding
across one destroys the triangle carrying it, whose two directed corners are
then dropped as `a == b`. A patch's directed boundary is *balanced* by
construction -- one outgoing and one incoming per polygon corner, and
cancelling a pair removes one of each at both ends -- so it decomposes into
closed cycles; drop one corner and it no longer does, the walk in
`compute_boundary_loops` runs into a dead end, and what comes back is an **open
chain handed out as a loop**. Every reader closes a loop with `% n`, so that
chain draws a chord from its last vertex straight back to its first, across a
face the model never divided, and counts as an extra boundary loop besides --
which is what "there is an edge here that Plasticity doesn't have" looks like,
and what made single faces report five and seven loops. On one real part 205
real edges were collapsed in a single object and 101 of its 245 loops came back
open. Capping rather than testing adjacency pair by pair: when two genuinely
distinct positions both fall inside the epsilon the cluster has to be *split*
by position, and which copy is edge-joined to which is an accident of how the
bridge emitted the triangles. The copies this exists to merge are unaffected --
they are the same double rounded the same way twice, so they sit at distance
zero, not at the epsilon.

On a real part the cap bites *hard*: one object's shortest edge is 2e-8, so the
epsilon drops to 1e-8 and the weld becomes, in effect, exact-position merging.
That is safe here for two reasons worth stating, because neither is obvious.
`weld_candidates` already scopes the weld to border vertices, so a tighter
epsilon cannot leave a face's own interior unwelded and turn its triangulation
edges into boundaries. And a border that no longer pairs vertex for vertex is
picked up by `resolve_neighbours_by_geometry` anyway -- which is why that
fallback has to exist *before* this cap is safe to apply. Measured on that
part, capping raises the unpaired segment count (2327 to 2570 on one object)
and the fallback takes every one of them back to zero.

And the walk itself now **returns only loops that closed**, over a multimap so
a vertex with two outgoing boundary half-edges keeps both. A fragment that
cannot close loses part of one patch's border; keeping it invents geometry
across the whole part. `tests/test_boundary_walk.py`.

The epsilon is `1e-5` **absolute, in the mesh's local units, and no caller
overrides it**. On a part whose coordinates run to a few hundred units the
float32 ulp is already the same order, so two faces' independently rounded
copies of a shared vertex can miss each other — which shows up as a phantom
patch border, hence wrong corners, wrong side count, wrong generator. Suspect
this before anything else when a patch dices strangely far from the origin.

## Architecture

| Module | Role |
|---|---|
| `constants.py` | generator names and the sets built from them; imports nothing, so `overlay` can share them with `operators` |
| `patch_data.py` | mesh → patches, weld map (scoped to border candidates), boundary loops, **and the per-mesh cache all of that lives in** (`analyse`) |
| `sides.py` | corner detection (angle and/or topology), corner *ranking*, split loop into sides, merge small sides |
| `geometry.py` | Coons/transfinite grids, arc-length resampling, BVH, reprojection, the interior relaxation and the cell-quality measure it steers by |
| `generators/` | one generator per patch type; `find_generator(n_sides)` picks the first match |
| `cad_display.py` | B-rep edges/vertices recovered from `face_ids`, the derived surface flow, and the raw group data behind the patch debug display — all cached, for the overlay |
| `patchprep.py` | one face → `PreparedPatch`: corners resolved, boundary split into sides, planarity |
| `sidematch.py` | side references, what each may match, pin kinds, span-collision resolution, substitution |
| `mesh_build.py` | preview object, committing into `<Source>_Retop`, span registry, committed-boundary cache, appearance, shading, collection mirroring |
| `keymap.py` | the keybind declaration + live-item lookup; leaf, so `overlay` can read it |
| `prefs.py` | the addon preferences page: Blender's own keymap rows |
| `tweak.py` | the Edit Mode round trip: tool-setting setup and restore |
| `operators.py` | the session modal + commit/discard/reload operators |
| `overlay.py` | keybind hints (POST_PIXEL) + every kind of dot; side highlight and CAD structure lines (POST_VIEW) |
| `state.py` | all scene properties; span props have live-update callbacks |
| `ui.py` | N-panel: persistent session/patch block + icon tabs (`state.ui_tab`) |

`patchprep` and `sidematch` are **leaves**: neither imports `operators`, which
is what keeps `overlay` able to read side references directly instead of
through a late import. Anything needing a session or a preview object stays in
`operators`.

Generator order matters: specialised ones (Wedge 2, Triangle 3, Quad 4) come
before the N-Side fallback (5+). `generators/ring.py` is **not** in that list:
a band is recognised by its patch having two boundary loops, not by a side
count, so `_generate_for_face` reaches for `generators.RING` directly.
`generators/ngon.py` is out of the list for a different reason: it's a *mode*
(`state.ngon_mode`, `N` during a session), not something a side count selects.

Two boundary loops does **not** by itself mean Ring: see the band invariant.

## Hard-won invariants — don't regress these

- **Nothing derived from a mesh is recomputed per hover.** Hovering a patch
  re-prepares it, and preparing it used to re-walk every polygon, rebuild a
  KD-tree over every vertex and rebuild the directed-half-edge table — on every
  mouse move. On a 16k-triangle part that is ~85 ms a frame, i.e. a ceiling of
  about 11 fps from patch preparation alone; cached it is ~0.5 ms.
  `patch_data.analyse` is the single entry point and returns everything one
  parse produces (patches, boundary loops, polygon→face-id map, weld map,
  directed owners, vertex positions) as one consistent `MeshPatches`.
  Invalidation is by **content**, not by event: `mesh_fingerprint` CRCs the
  vertex coordinates via `foreach_get` plus the element counts, which is one C
  loop and orders of magnitude cheaper than what it guards, and which cannot go
  stale the way a "someone changed the mesh" hook can. `mesh_build`'s
  committed-boundary map and `cad_display` use the same fingerprint.
  **The cached tables are shared and must be treated as read-only** —
  `generators.base.resolve_side_points` copies every point it takes out of
  `positions`, because a generator may hand its input straight through into a
  preview mesh (`resample_polyline_by_arclength` returns the very objects it was
  given when the count already matches).
- **A neighbour missed by half-edge pairing is found by geometry, and the
  tolerance is a share of the model extent.** `resolve_neighbours_by_geometry`
  fills in a segment's neighbour with the patch whose own boundary segment this
  one *lies along*. Of the **extent**, not of the segment's own length, and
  that is the whole of it: two faces sharing a CAD edge put their boundaries on
  the same curve, so the only thing between their chords is float rounding,
  which scales with coordinate magnitude and not with feature size -- measured
  across four objects of a real part, a genuine shared border sits within 1e-7
  of the extent at the 95th percentile, i.e. coincident. Scaling by the segment
  instead lets a long one reach a long way *sideways*: the separate sheet 0.02
  above a 4-unit edge in `tests/test_match_specificity.py` is 0.5% of that edge
  and was taken as its neighbour, while at a share of the extent it is 4e-3,
  four hundred times outside the limit. The index samples every segment at one
  **uniform** spacing (the median segment length) rather than a few points
  each, so the query radius is a constant and each lookup returns a handful of
  hits; indexing three points per segment and searching a multiple of the
  segment's own length swept a large part of the mesh once per orphan, which
  was 7.8 s on a 35k-polygon object against 0.12 s now -- and *less* accurate,
  since a long neighbour kept no sample near a short segment.
  `tests/test_tjunction.py` builds a border only half of which pairs, and
  asserts both that the exact pass still fails on it -- or the test proves
  nothing -- and that the two genuine B-rep vertices are all that survive.
- **Welding across patches.** Only *corner* vertices are exact source-mesh
  vertices, so they're welded by identity (`retop_source_vid` attribute).
  Interior boundary points are span-dependent resamples: they're welded only
  by proximity, only among boundary-flagged verts, at
  `boundary_weld_distance`. An unscoped `remove_doubles` silently merges
  unrelated points and drops faces.
- **Span propagation** (`mesh_build` span registry, JSON on the result object)
  keyed by corner-id pairs is what makes matching spans — and therefore
  seam-free welds — happen automatically between neighbours.
- **Re-editing removes the old patch on pick, and can always put it back.**
  Every baked face carries `retop_patch_face_id` and every patch its own spans
  (`retop_patch_spans`). Picking a patch that already has geometry reopens it
  with those spans *and immediately deletes its faces*
  (`remove_patch_from_result`) after snapshotting the whole result mesh into a
  spare datablock. Removing on pick, not on commit, is deliberate: the patch
  visibly disappears, so a failure to identify it shows up at once instead of
  turning into two overlapping surfaces after the commit. Every exit that isn't
  a commit — Discard, Esc, leaving the object, `end_session`, a reload — must go
  through `restore_reedit_removal`, and commit through `keep_reedit_removal`.
  The delete uses `context='FACES'` so vertices still used by a neighbour
  survive and the new grid welds back onto them. Creating the face-id layer on a
  result mesh that predates it must stamp `NO_PATCH` on the existing faces — a
  fresh int layer defaults them to 0, i.e. "patch 0".
- **Retopology committed before face tracking existed is claimed on entry.**
  `mesh_build.adopt_untracked_faces` (called from `enter_session_object` and
  `set_active_patch`) classifies each untagged result face onto the source
  patch it sits on, by nearest source polygon, voting with the face centre
  (weighted) plus its verts. Without it those patches read as "never retopped"
  and re-editing silently doubled them up. On anything short of a strict
  majority the face keeps `NO_PATCH`: unclaimed faces are never deleted, so a
  misread degrades to a duplicate, never to a hole in a neighbour. One pass per
  result mesh, marked by `retop_patch_adoption`. Removal for a re-edit uses a
  looser rule (face centre only) since it's visible and reversible.
- **Corners come from two tests that miss opposite things.** The angle test
  (`detect_corners`) is geometric and swallows anything gentle: a 30° chamfer
  reads as a smooth stretch, lands mid-side, and every generator paves across
  it. The topological test (`detect_topological_corners`) fires where the
  *neighbouring* face id changes — a real B-rep vertex, at any angle — but a
  face whose whole boundary runs against one single neighbour has no junction
  at all, however square it looks. `resolve_corners` is the arbiter.
  **The switch is per mode** (`corner_method_spans`, default `ANGLE`;
  `corner_method_ngon`, default `BOTH`) and that split is not cosmetic: a
  grid's *side count chooses the generator*, so the extra corners topology
  finds turn a bevel — whose long side borders face after face — from a Quad
  into an N-Side, i.e. from a clean grid into a midpoint fan. An n-gon only
  follows its boundary, so extra corners cost it nothing and are the one thing
  that keeps a shallow chamfer. `TOPOLOGY` falls back to the angle test when a
  boundary yields no junction — a patch with no corners is one single side,
  which every span generator would then read as unusable.
  **`boundary_neighbours` is per loop and parallel to `boundary_loops`**, so
  anything reordering the loops (`sort_loops_outer_first`) must carry the
  neighbours with them or a hole's neighbours get handed to the outer boundary.
- **Too many corners is reduced only where the ranking shows a cliff.**
  `sides.dominant_corners` runs on five or more candidates, sorts them by how
  much the boundary bends, and cuts where consecutive scores fall off by
  `CORNER_CLIFF`. A *ratio*, never an absolute angle, because that is the only
  thing separating the two cases that both produce "more than four sides": a
  real hexagon bends the same at every corner and has no cliff anywhere, while
  a curve the angle test sampled into corners sits far below the real ones and
  the drop is unmistakable. Four is tried first so a quad wins any tie, and
  cutting all the way to a *triangle* takes a much clearer cliff
  (`CORNER_CLIFF_TO_TRIANGLE`) — a rectangle with one chamfered corner is three
  90s and two 45s, and a plain 2:1 rule called it a triangle.
  **Topological corners are exempt** and take no part in the ranking. A
  junction is a fact the mesh states outright; a gentle chamfer's junction
  bends barely at all, so ranking it would drop the very thing the topology
  test exists to catch (`tests/test_corners.py` pins that).
  What *cannot* be decided is a boundary where every vertex bends the same and
  every one is flagged — a coarsely tessellated circle and a real octagon are
  the same polyline. `sides.corners_are_uniform` only **reports** it, into
  `state.corner_warning`, and the panel says to raise the threshold. Guessing
  would round a real octagon off half the time.
  **Why the method is not a user-facing setting is measured, not assumed.**
  Across the whole fixture at MID it changes the result on *two objects out of
  sixteen* in either mode -- and on those two the shipped default is the better
  one. For grids: `ANGLE` holds Cube Bevel Edges at 0.221% deviation with 9
  Quad, 2 Triangle and 2 Wedge, where `BOTH`/`TOPOLOGY` fan two of those faces
  into N-Sides and take it to 0.838%, and Cube Chamfer Edges gains a second
  N-Side. For n-gons: `BOTH` closes all 91 of Carved Rounded Slot's open
  boundary edges that `ANGLE` leaves (48v/28f against 96v/36f, same deviation)
  and takes Cube Bevel Edges from 0.221%/62 open to 0.147%/52. Every other
  object is identical under all three. And `BOTH` and `TOPOLOGY` are
  **indistinguishable on every object of the fixture in both modes** -- three
  values, two outcomes, and each mode already ships with the right one. There
  is no situation the panel could describe in which changing it is the answer,
  so the rows live behind `prefs.developer_mode`, with
  `RETOP_OT_reset_corner_methods` as the way back for a file that stored a
  non-default before they were hidden.
- **One corner is worse than none.** `resolve_corners` used to hand back a
  single corner as-is: `split_into_sides` then returns one side, no generator
  accepts one, and the face is silently unpickable with no message anywhere.
  `sides.complete_corners` tops it up to four, *anchored on the real corner* so
  the one feature the face has lands on a side boundary rather than mid-side.
  `tests/test_corner_ranking.py` pins it on a cardioid cusp.
- **Synthesised corners are for single-loop patches only.** A **ring** never
  goes through `find_generator` — it is chosen by having two loops — so a
  cornerless rim gives it no trouble, and inventing four corners on each of its
  two loops actively breaks it: `ring.ring_from_sides` allocates points *per
  side*, so two loops whose invented corners don't face each other get their
  points paired across a shear instead of straight across the band. That is
  what a chamfer running round a circle showed. Hence
  `resolve_corners(allow_synthesis=...)`, passed `num_loops == 1` by
  `_prepare_patch`; `tests/test_ring_chamfer.py` pins it.
- **A boundary with no corner still has a shape, and it is asked first.**
  Neither corner test finds anything on a single closed curve, and without
  `sides.synthesise_corners` such a face has *one* side, `find_generator` has
  nothing that accepts one (Wedge 2, Triangle 3, Quad 4, N-Side 5+),
  `_generate_for_face` returns None, and the face reads as "not selectable"
  with no message anywhere.
  But four corners spread by arc length is only right for a *circle*. A long
  strip that curves back on itself — a rounded slot, a bore wall, a ribbon
  round a feature — has its perimeter dominated by its two long sides, so the
  quarter points land in the middle of them: the "quad" fed to the Coons patch
  is half a long side plus half an end, and it comes out as a fan.
  `shape_corners` reads the boundary instead. Turn measured **over a window**
  (`SHAPE_WINDOW`, a share of the perimeter) rather than at a vertex, because a
  tessellated rounded end is dozens of individually insignificant turns and one
  real feature. Peaks must be *local maxima* at that scale — without it the
  tangency where a straight side meets a cap gets picked, half as sharp as the
  cap but well above the flat run — and separated by `SHAPE_SEPARATION`.
  However many it finds is what decides the generator: two ends make a Wedge (a
  grid running along the strip), three a Triangle, four a Quad. A boundary
  whose turn is uniform (`SHAPE_CONTRAST`) has no shape at all — a circle — and
  only that falls back to four by arc length, spread by *length* rather than
  index because a tessellated circle is not sampled uniformly.
  `tests/test_strip.py` pins the stadium, the circle and the rounded rectangle.
- **The interior is relaxed after the grid is built, and the boundary never
  moves.** Every generator here interpolates between opposite sides, which is
  the right answer for a four-sided region whose sides face each other and a
  poor one as soon as they do not: against a **concave** boundary -- the rim of
  a hole, a slot's flank, a face left by a boolean -- the cells bunch against
  the concavity and stretch away from it, and around an **acute** corner they
  collapse. Neither is decided by the boundary, so no choice of corners, spans
  or generator can fix it; only moving the interior points can, and
  `geometry.relax_interior_points` is that, run from `_generate_for_face` once
  for every generator (`state.relax_iterations`, 8 by default).
  What is pinned is the generator's own `boundary_local_indices`, never
  recomputed from the preview's topology: a vertex is untouchable because
  something *outside* the patch may weld to it -- by identity for a corner, by
  proximity for the rest -- which is a fact about the patch, not about whether
  an edge happens to carry one face. So matching, propagation and the welds are
  untouched by construction, and `tests/test_relax.py` asserts the boundary is
  where it was to the last float rather than within a tolerance.
  **Every move must improve the worst cell it touches**, and that guard is what
  makes it a default rather than a per-part setting. A plain Laplacian pass is
  not an improvement everywhere -- on an already-regular grid it pulls the
  interior towards equal *edge lengths*, which is not where transfinite
  interpolation put them -- and measured across the fixture the unguarded
  version improved four shapes' cell quality and made three others' worse. With
  the guard, a patch it has nothing to offer comes back exactly as the generator
  built it.
  The measure is `geometry.cell_quality`: the sine of the smallest corner angle
  **times** the ratio of the shortest edge to the longest. Neither half alone
  will do -- a 1x100 rectangle has four perfect right angles, and a rhombus with
  a 5 degree corner has four equal edges -- and a stretched cell is the whole
  complaint this answers. It also takes a reference normal, so a step that turns
  a cell over while keeping it square scores 0; no measure of the angles can see
  that on its own.
  **Every pass reprojects**, because a Laplacian step moves a point towards the
  chord between its neighbours, i.e. *inside* a curved surface, and iterating
  without putting it back is exactly the deviation the whole addon is measured
  by. So it is a surface-constrained relaxation -- step, project, repeat -- and
  it does nothing at all without a BVH: with `reproject` off it would be the
  shrink and nothing else, which is why the panel greys the row out rather than
  ignoring it. A step whose *projection* is longer than the step itself is
  refused: the correction after a tangential move is second order and far
  shorter than the move, so a longer one means the point left the patch and
  `find_nearest` is answering with the nearest point on the **boundary** --
  which drags the vertex onto an edge and folds the cells either side of it,
  the same fold `nside.interior_point` exists to avoid, met from the other
  direction.
  **It stops at `RELAX_QUALITY_TARGET` (0.5) and only tries the vertices
  touching a cell below it.** A cell twice as long as it is wide scores 0.5 and
  is an ordinary retopology cell, not a defect. That stop is also what makes
  this affordable on **every hover**: a clean patch costs one sweep of its faces
  and returns (0.4 ms at 10x10, 6 ms at 40x40), and the cost of the rest scales
  with the size of the problem rather than the size of the patch. The count in
  the panel is a cap, not a dose.
  Measured on the fixture at MID, against the same run with it off: `Cone`'s max
  deviation 13.86% to 10.48% and its worst aspect ratio 9.2x to 2.9x,
  `Carved Rounded Slot`'s aspect p95 10.33x to 10.00x and skew p95 58.8 to 55.0
  degrees, `Cube Two Booleans`' aspect p95 7.79x to 7.42x -- and **not one
  count moved**: same vertices, same faces, same open boundary edges, same
  welds, the same five known failures at the same values.

- **An N-Side patch is one Coons sub-patch per side, not one quad per boundary
  vertex.** A quad mesh of an odd-sided region has to put an irregular vertex
  somewhere and the middle is where every tool puts it, so the centre is not
  the problem — its *valence* was. The old fill emitted a quad per boundary
  vertex, so the pole had `sides × span` spokes (24 on a six-sided patch at
  span 4), and the only points inside the patch were that centre and one
  midpoint per boundary segment: on a curved face the fill cut the curvature
  off as a chord, which is what "it makes a fan" looks like on screen.
  Now each side is split at its midpoint, a spoke runs from there to the
  centre, and the quad between two consecutive spokes goes through the same
  `coons_patch_grid` and BVH reprojection as every other generator. The pole's
  valence becomes the *side* count, the interior becomes a real grid, and every
  interior point sits on the surface.
  **And the sides stop sharing one span, which is what lets several of them be
  matched at once.** A side is bounded by the spokes of its two *neighbours* —
  `t[i] = s[i-1] + s[i+1]`, its own spoke only saying where it is split — so
  choosing the spokes chooses every side's count. Two sides bordering two
  finished patches used to be a collision by construction: one was matched and
  the other was told "another side drives the same span" and left on the CAD
  tessellation, i.e. a crack along an edge visibly against retopology.
  `nside.spoke_allocation` is that solve, and deliberately not a general one:
  `s[i-1] + s[i+1] = t[i]` over a cycle is only always solvable for odd `n`, so
  the wanted counts are applied in the order `_winning_matches` ranks them (a
  pin, then the denser match) and whatever cannot be fitted is **refused**
  rather than approximated — a match returning a count nobody asked for is a
  crack that looks like a weld. `operators.nside_allocation` runs it for
  generation *and* for the commit's span registry, off the same winners, so the
  registry cannot advertise a count the mesh does not have. `span_key_for`
  therefore keys an N-Side per side, like an n-gon; what decides a conflict is
  the allocation, not `_winning_matches`.
  With nothing matched every spoke is equal, so **each side carries an even
  number of segments** — it is split at one of its own vertices, and
  `even_span` rounds the uniform case up to something buildable.
  **Those midpoints are no longer emitted on the boundary**: the old
  fill subdivided every boundary segment without telling the neighbours, i.e.
  it put a T-junction on every shared edge, which is why Cube Chamfer Edges
  went from 32 open boundary edges to 6 with its deviation unchanged.
  **And the centre has to be *inside* the patch, which the mean of the boundary
  points is not.** On a convex outline it is, which is why this held for a year;
  a face left by a boolean cut is concave, the mean lands outside the region,
  and `project` then answers with the nearest point *on the surface* — which is
  the boundary itself. The two sub-patches meeting at that spoke are quads
  `C → M → Z → P` with `Z` sitting exactly on `M`: two corners in one place, a
  zero-area cell whose normal is float noise. Every count looks healthy while it
  happens — vertex count, face count, deviation — and what it measures as is
  faces facing into the surface and an aspect ratio in the millions.
  `nside.interior_point` triangulates the outline (`tessellate_polygon`, which
  handles concave) and takes a triangle centroid, so the candidate is inside by
  construction. **Which** candidate is not a detail: the largest triangle's
  centroid fixed the patch that was folding and made a clean neighbour fold, so
  it is scored by distance to the nearest boundary segment instead — the deepest
  point of the shape. Being inside is necessary and not sufficient, and the
  sufficient condition is the polygon's *kernel*, the points the whole boundary
  can see: a spoke that leaves the region on its way to a midpoint turns over the
  two quads either side of it, and a shape whose kernel is empty has no valid
  centre at all. Measured on `Cube Two Booleans`: unwelded coincident vertices
  100 → 9, worst aspect ratio 7.5M → 2.8M, **max deviation 6.08% → 11.11%** and
  faces facing inward 34 → 35. The p95 deviation does not move (0.048% either
  way), so both numbers are one localised artefact — the fold this does not
  reach, which is still open.
  Per-side spans *in the panel* and hand-placed corners are still what the
  reference tool's full N-Side mode adds; the machinery under them is here.
  `tests/test_nside_match.py`.
- **A patch can have more than one boundary loop, and their order is random.**
  `compute_boundary_loops` walks a *set* of half-edges, so "loop 0" of a face
  with a hole is as likely to be the hole as the face. Anything reducing a
  patch to one loop goes through `patch_data.sort_loops_outer_first` (largest
  extent first). Two loops = a band, filled by the Ring generator; more than
  two = only the outer loop is used, and `state.num_loops` makes the panel say
  so rather than silently paving over the holes.
- **Two boundary loops is not the same thing as a band.** The Ring generator
  gives both loops the same point count, because every quad it makes runs
  straight from one to the other. On a washer, a tube wall or a fillet round a
  boss that is right. On a 200x100 plate with a 5mm hole it is a disaster:
  either the hole gets a hundred points or the outline gets twelve, and every
  quad is stretched the width of the plate. `ring.is_band` separates them on
  how *even* the gap between the loops is (`BAND_GAP_SPREAD`, sampled around
  the outer loop) and how far apart the two perimeters are
  (`BAND_PERIMETER_RATIO`) — both deliberately generous, since calling a band a
  plate costs more than the reverse. A non-band that is flat is filled as an
  n-gon instead (`generate_holed`, which is what pressing N would give it
  anyway) and `state.generator_note` says why. A committed patch is never
  rerouted: it comes back as whatever it was built as. `tests/test_band.py`.
- **The two loops of a ring must resample to the same point count.** Both go
  through `ring.around_count`, which floors the "around" span at each loop's
  side count (a side can't get zero segments). The commit path re-derives the
  per-side allocation with that same helper to register spans per loop —
  pairing corners cyclically across both loops' ids would invent a side running
  from the outer boundary to the hole.
- **A band's rungs must run straight across it, and whole-index alignment
  cannot get them there.** Every quad runs from `outer[i]` to `inner[i]`, so
  how the loops are indexed against each other *is* the shape of the quads.
  `align_rings` searches whole offsets, which is all it can do once both loops
  are resampled — leaving up to half a step of rotation. On an annulus that
  residue is not noise but a **constant shear**, the same angle on every rung:
  half a step of 64 points is 2.8°, and of 16 points 18.6°. `ring.phase_align`
  removes it by choosing where the inner loop is *sampled from* rather than
  which sample to start at — the phase is read off the geometry (each outer
  point's nearest arc-length on the inner loop implies an offset; their
  **circular** mean is the answer, circular because 0 and L are the same
  offset and a plain average across the seam lands halfway round).
  **Only on a cornerless rim, and never on a matched one.** A corner is an
  untouched source vertex welded by identity, so moving one while keeping its
  name would make a later patch reuse a vertex that is no longer there; a hole
  with real corners keeps them and its shear. A phased rim's corner id is
  emitted as `ring.NO_CORNER` instead — in place, not by shortening the list,
  because the caller zips it against `corner_source_ids` positionally and the
  outer loop's ids come first, so a short list stamps an outer id onto the
  hole's vertex the moment it is the *outer* rim that got phased. A point with
  no id welds by proximity like every other boundary point.
  **And a phased rim has to be reprojected.** Boundary rows are normally left
  exactly where the loops put them, because they are samples of the real
  boundary that a neighbour welds to. A phased one lands nowhere near a source
  vertex by construction, so on a curved rim every point sits mid-chord, a
  sagitta inside the surface — which is what took three fixture shapes from
  ~0.002% vertex deviation to 0.05%. It is not shared with anything (its
  corner id is gone), so it gets the interior's reprojection.
  `tests/test_ring_straightness.py` measures the angle between each rung and
  the radial it should lie along, and pins the *spread* as well as the worst
  case — a fix that merely averaged the error out would otherwise pass.
- **A matched rim leads the band; it is never resampled onto the other one.**
  Matching hands a side a committed neighbour's own vertices, and every other
  generator reproduces them for free — `resample_polyline_by_arclength` returns
  the points it was given when the count already matches. A ring did not. It
  always led with `loops[0]` and phase-resampled the other rim onto it, so a
  match landing on that rim was thrown away and the two rings came back half a
  step apart — with a crack all the way round, which is what the fixture's 464
  open boundary edges on Plate And Cylinder were. Which rim is `loops[0]` is
  decided by *extent* (`sort_loops_outer_first`), and on a tube the two are
  equal: the same match worked or didn't for no reason visible on screen, which
  is the "matching is inconsistent" half of the report.
  `span_settings["locked_loops"]` (filled from `sidematch.applied_loops()`)
  names the loops carrying a match; the ring **leads with a locked one**, takes
  `around` from its point count, and phases the *free* loop onto it instead.
  A locked rim is not phased for the same reason a cornered one isn't, but not
  for the same cause: its points are not a sample of a boundary at all.
  **`align_rings` re-indexes the free loop, so its corners must be looked up
  through the map it returns** — the lead loop maps to itself. Getting that
  wrong stamps a loop's corner ids onto whatever vertices sit at those
  positions, and a corner welds by identity: the neighbours then weld to points
  on the far side of the band, which measured as a 4.9% deviation and a quad
  spanning the whole part. `tests/test_ring_match.py`.
- **Which way the band faces follows the outer loop, not whichever rim leads.**
  The two loops of a patch wind *opposite* ways — an outer boundary one way, a
  hole the other — so pairing them straight across means one of them is
  reversed, and which one decides which way every quad faces. It used to be
  the hole, always, because the outer loop always led. Once a matched hole
  leads, it is the outer row that comes back reversed and **every normal of the
  band flips**: the retopology turned inside out the moment a side was matched,
  which is what Blender's face-orientation overlay showed as a red band. The
  quads are emitted the other way round when `ring.loop_area_vector` says the
  outer row ended up running against its own walk order. No count above catches
  this — the fixture had 1610 of 2139 faces turned over with its vertex count,
  face count, deviation and open-edge count all unchanged — so
  `benchmark.flipped_faces` measures it against the *nearest source polygon*
  (a CAD import is a shell whose faces already carry the right orientation, and
  there is no other definition of right), `tests/test_fixtures.py` asserts it
  is zero on every shape, and `tests/test_ring_match.py` builds the two rims
  wound opposite ways — wound the same way, the bug is invisible.
- **A ring's two rims are keyed per loop, not against each other.** Every other
  generator collides sides that share a span, because a grid has one count per
  direction. A ring's rungs run `outer[i]` → `inner[i]`, so both rims *can* be
  reproduced at once as long as they agree on the count — and whether they
  agree is exactly what `_honours` checks once the span is resolved. Hence
  `span_u@0` / `span_u@1` and `sidematch.span_base`, which is what every reader
  of a span key (the winner-to-span loop in `_prepare_patch`, `_honours`) has
  to go through. A shared key dropped the second rim's match even when it
  wanted the very same number.
- **Everything resolves through `<Source>_Retop`.** Rename or re-import the CAD
  object and its retopology becomes unreachable — a session on the new name
  starts a second result mesh that overlaps the first. `orphan_result_objects`
  exists to say so in the panel rather than let it look like a broken re-edit.
- **Creating or freeing a datablock outside an undo step crashes Ctrl+Z.**
  Blender's undo restores a memfile snapshot; an ID created between two steps
  isn't in it, and the depsgraph then walks an object whose data or material
  array was freed under it (`DepsgraphNodeBuilder::build_materials`, null read
  — that's the crash reported on 2026-08-17). So: **no ID creation from
  property update callbacks, draw handlers or hovers.** The preview object is
  created once by `enter_session_object` and only ever has its *geometry*
  rewritten afterwards (`clear_preview_object` empties it,
  `remove_preview_object` is session teardown only); materials are made by
  `ensure_materials` at the same moment, and every `refresh_*_appearance` path
  is get-only. Every moment that creates or frees an ID, or writes to the
  result mesh, calls `operators.push_undo`: entering an object, opening a
  re-edit, each **commit**, each **delete**, a discard that *restored* one, and
  ending the session. `undo_post`/`redo_post` run `_on_undo_redo`, which drops
  the active patch and any re-edit snapshot (undo has just replaced the mesh
  state they described) and touches **scene properties only**.
- **One undo step per committed patch, pushed by hand.** Ctrl+Z used to roll
  the *whole session* back: the only steps below a mid-session state were
  "Retop: enter <obj>" and the re-edit ones, so one press restored the file
  state from before the session and every committed patch went at once. Commit,
  delete and discard therefore push their own step — and carry `REGISTER`
  **without** `UNDO`, because Blender pushes one of its own for an
  `OPTYPE_UNDO` operator run from the UI and two identical states on the stack
  read as a Ctrl+Z that does nothing. (The modal calls them through `bpy.ops`,
  which is the path where the automatic push was missing in the first place.)
  A discard with nothing to restore pushes nothing: it changed no datablock.
  These are also the steps that own the snapshot datablock `keep_reedit_removal`
  frees, so pushing them closes a latent case of the invariant above.
  `tests/test_undo.py` pins the count per action.
- **The undo handler defers everything it may not do to the modal.**
  `_on_undo_redo` can only write scene properties, but the step also invalidated
  the preview geometry, the hover and the cached side references — so it sets
  `operators._undo_needs_reconcile`, and `_reconcile_after_undo` runs on the
  modal's next event (the 0.1 s timer, so within a frame or two). Undoing *past*
  the session's own start restores `session_active = False`, and the modal must
  then finish rather than sit there swallowing viewport events for a session the
  panel no longer shows — with `end_session(push=False)`, since pushing a step
  straight after an undo throws away the redo the user just made available.
- **Type hints everywhere, but never `from __future__ import annotations`.**
  A Blender property is declared *as an annotation* —
  `span_u: bpy.props.IntProperty(...)`, nothing after an `=` — so the thing
  Blender reads to register it is exactly the thing PEP 563 turns into a
  string. Blender 5.0 resolves those strings back (checked: all 73 properties
  still register with the future imported), but `bl_info` says 4.2, that
  behaviour belongs to the Blender being run rather than to this code, and the
  failure mode is the silent kind — the group registers *nothing*, nothing
  raises, and the panel just draws empty. Not worth relying on: Python 3.11
  evaluates `list[int]` and `X | None` unaided, and a forward reference is
  quoted individually. For the same reason **only methods are annotated inside
  a registered class** — never its class-body attributes, which is the one
  dict the registration walk reads.
  A module needing a *new* runtime import purely for a hint puts it under
  `if TYPE_CHECKING:` and quotes the annotation instead: `sides`, `patch_data`
  and `cad_display` stay free of Blender imports, and `overlay` must not widen
  what a draw handler pulls in. `tests/test_registration.py` pins all of it,
  including that every function still carries its types.
- **Never unregister an operator class from inside its own `execute()`** —
  that crashes Blender natively. `RETOP_OT_reload_addon` defers the real work
  to a `bpy.app.timers` callback.
- **The reload must never hand-list submodules.** Reloading a package re-runs
  its imports, but those resolve straight out of `sys.modules`, so a module
  missing from `_perform_reload` keeps running its *old* code next to modules
  that are new. The crash then lands somewhere unrelated — a reloaded caller
  hitting an `AttributeError` for a function the stale module doesn't have yet
  (that's how `generators/ngon.py` broke commit after being added to a
  hand-written list). Submodules are collected from `sys.modules` under the
  package name; `tests/test_reload.py` asserts nothing is missed. The version
  string can't catch this: `version.py` reloads fine either way.
- **Local View ('/') is overridden.** `RETOP_OT_local_view` is bound to
  `SLASH`/`NUMPAD_SLASH` in the addon keyconfig; it delegates the toggle to
  `view3d.localview` and then calls `mesh_build.sync_local_view`, which pulls
  the preview and each isolated source's `<Source>_Retop` into every viewport
  that is in local view. The binding cannot follow a *scene* property, so
  `local_view_include_retop` off instead makes `sync_local_view` a no-op and
  '/' behaves exactly like stock Blender. What the binding does follow is the
  session: like every `GLOBAL` key it polls false with none open, so outside a
  session '/' is not the addon's at all (see the keys section). `local_view_set` only
  flips a per-viewport flag — no ID is created, so it is safe outside an undo
  step and can be called from `enter_session_object` (a session started while
  already isolated would otherwise build its preview invisibly).
- **Creases are patch borders, never a plain angle threshold.**
  `mesh_build.apply_result_shading` shades every face smooth and marks an edge
  sharp only when its two faces carry *different* `PATCH_ID_ATTR` values and
  their normals differ by more than `sharp_edge_angle`. One patch is one CAD
  surface, so an angle-based auto-sharpen would crease a curved patch's own
  low-span interior instead. It's re-run after **every** commit, not just the
  first: sharpness is a property of the border *between* patches, so a new
  neighbour changes the shading of an edge that already existed. It only writes
  mesh attributes, so it's safe from a property callback.
- **There is no per-object wireframe opacity in Blender.** `show_wire` is drawn
  by the viewport overlay and its strength is that overlay's
  `wireframe_opacity`, so `result_wire_opacity` writes to every 3D viewport
  (`apply_wireframe_opacity`) and the panel says outright that it affects all
  wireframes there. `result_show_wire` is scoped to *emphasized* (in-session)
  result meshes: a resting one has never shown a wireframe and still doesn't.
- **Result meshes are filed under a mirror of the Inbox hierarchy.**
  `source_collection_path` walks up from the source object's collection and
  returns the path *below* the deepest collection named `Inbox` (what the
  bridge builds above it is its own scaffolding). `place_result_object`
  rebuilds that path under `Retop`. Two consequences: `iter_result_objects`
  must use `all_objects`, not `objects`, or it finds nothing; and collections
  are IDs, so this runs only from `ensure_result_object` — never a callback.
  Mirror levels usually come out as `Name.001` because the source collection
  already owns the name; `_child_collection` matches by base name so a session
  doesn't create a new level every time. An existing result mesh is re-homed
  only if it still sits at the top of `Retop`, so a manual filing is never
  stomped.
- **Raycasting looks *through* every obstacle, it never gives up at one.** The
  addon's own preview/result meshes (or the hover flickers: ray hits the
  preview → "no patch" → preview deleted → rebuilt), viewport-hidden objects
  (Local View `/`), and non-Plasticity meshes — a stand-in or a block-out in
  front used to abort the cast and make everything behind it unpickable. The
  step past a hit scales with the distance travelled: a fixed epsilon is either
  too small to clear the surface at range (the same hit repeats until the
  attempt cap) or large enough to skip past a thin recess floor on a small
  part.
- **Hover hysteresis** keeps the current patch unless a new one is clearly in
  front; coincident CAD surfaces otherwise flip-flop every mouse move.
- **`context.region` / `region_data` are unreliable inside a modal** (may be
  the N-panel's region). Use `operators.viewport_region()` and window-absolute
  mouse coords.
- **The modal swallows nothing outside the 3D view's WINDOW region.** One
  check, `point_in_region`, at the top of `_modal`, passing *every* event
  through — not a list of them. Piecemeal per-handler checks kept leaking: the
  clicks were guarded but MOUSEMOVE was not (Blender highlights buttons from
  mouse moves, so the panel went dead while the clicks were being let through),
  and the keys were not either (a digit typed into a panel field went to
  span entry, Enter committed the patch instead of confirming the field).
  `PANEL_EVENTS` records the rule so `tests/test_raycast.py` can assert it.
  `_in_viewport` does not cover the panel — it is a *region* inside the same
  VIEW_3D area. **And the WINDOW region alone does not either**: with Region
  Overlap (Blender's default) the WINDOW region spans the whole area and the
  N-panel, toolbar and headers float *on top* of it, so a point under the panel
  is genuinely inside WINDOW. `point_in_viewport` subtracts every region in
  `OVERLAY_REGION_TYPES`; testing WINDOW on its own is what kept the panel dead
  through three separate attempts at this bug. The cost, accepted: session
  keybinds need the pointer over the viewport, like Blender's own region
  keymaps.
- **A modal cursor is set on the whole window**, so it has to be dropped the
  moment the pointer leaves the 3D view (`_update_cursor`, called before the
  area check so it also fires when the pointer moves to another editor).
  Leaving the eyedropper over the N-panel reads as "the UI is not for you".
  `_apply_phase_ui` therefore only sets the cursor when the pointer isn't known
  to be outside.
- **`gpu.state.point_size_set` cannot be trusted.** The backend ignores it
  whenever program point size is enabled — the shader has to write
  `gl_PointSize` then, and the builtin `UNIFORM_COLOR` one does not — which
  came out as 1px dots that ignored their size setting entirely. Every dot is
  screen-space geometry instead (`_discs_around`, a triangle fan each), drawn
  from the POST_PIXEL handler where the region and `region_data` are at hand to
  project with. Round rather than the two triangles it started as: a square dot
  reads as a handle you can grab, and none of these are — they mark where a
  vertex is. `DOT_SEGMENTS` stays a multiple of four so a disc still measures
  exactly the size asked for; `tests/test_overlay.py` asserts that, and that
  nothing calls `point_size_set(` again.
- **The draw handlers are the one code Blender alone invokes**, so nothing
  else notices when they break. `overlay.enable()` once shipped raising
  `NameError` on a function an over-eager edit had deleted, with the whole
  suite green: no test installed the handlers or called a callback.
  `tests/test_overlay.py` now does both, across every phase and mode
  combination — a draw callback that references a name that isn't there fails
  at the call, whatever the early exits do afterwards.
- **Session state lives in the scene but the modal doesn't.** A reload or a
  crashed modal leaves `session_active` set with nothing listening;
  `operators.session_is_running()` detects it and the panel offers a reset.
- **`show_in_front` is a setting, not a consequence of the session.**
  `result_see_through` (V, `RETOP_OT_toggle_see_through`) decides whether
  the retopology draws over the rest of the scene or is occluded like any other
  object — and turning it *off* is the only way to check the result sits on the
  surface rather than floating off it, which is something you want mid-session.
  An earlier version drove this from the viewport's own X-ray on Alt+Z; taking
  over Blender's binding cost more than it gave, and the viewport's X-ray is a
  different question anyway.
- **Cosmetic offsets are Displace modifiers, never baked.** Commit reads the
  preview's *base* mesh; the result offset sets `show_render = False`.
  **And there is only one offset, and now only one control for it.**
  `mesh_build.result_lift` is the measure the committed result is pushed off
  the CAD surface by, and the preview takes that same one times
  `PREVIEW_LIFT_RATIO` (`preview_lift`). An Extra Offset slider sat beside it
  for a while, in a *separate* panel section, and both halves of that were
  wrong: it read as an independent setting when it was an addend to a number
  the preview already derives from, and it did not go through
  `to_blender_units`, so in millimetres the two sliders sat on scales a
  thousand apart and one whole unit of it was a metre — the smallest usable
  drag threw the preview off the model. Preview and result are now one
  Appearance section with one Offset in it (`state.show_appearance`).
  The preview used to sit at 0
  by default, i.e. on the surface and *under* every committed neighbour: a
  patch hovered before the click that removes its faces came back orange
  buried in blue, and a shared boundary showed a step that exists in neither
  mesh. Strictly more, never equal — two coplanar surfaces z-fight into a
  stipple — and only by a fraction of an offset that is itself 0.1% of the
  model, so the seam still reads as flush. `refresh_result_appearance` ends
  by refreshing the preview for the same reason, and `show_in_front` on the
  preview follows `result_see_through` (V) rather than being pinned on:
  checking the retopology against the surface has to include the patch being
  built. The one thing that *does* override it is a hand-edit: for the length
  of the `TWEAK` trip the result draws in front (`tweak_draw_in_front`, on by
  default), because the vertices being dragged are the point of the trip and
  the CAD surface hides half of them. It is Blender's In Front flag and not a
  lift, deliberately — nothing moves, so what is on screen is still exactly
  where the topology is, which no offset could claim. `enter_tweak` sets the
  flag on the object directly rather than through `refresh_result_appearance`,
  which also assigns materials and so writes to mesh data Blender owns in Edit
  Mode; `refresh_result_appearance` knows about the phase too, so a redraw
  triggered mid-edit agrees with the trip instead of undoing it, and
  `exit_tweak` calls it to put the flag back under `result_see_through`. The preview is stamped with its source object
  (`PREVIEW_SOURCE_PROP`) so the automatic offset resolves outside a session
  too.
- **Order matters in `end_session` / `exit_session_object`:** clear the state
  first, *then* call `refresh_result_appearance` — it derives every mesh's look
  from session state, so refreshing first re-applies what you're clearing.

## Session model (`RETOP_OT_session`)

**Picking an object selects it.** `enter_session_object` ends with
`select_only`, because everything Blender does "to the object" — isolate,
frame, the header, the properties editor — reads the *selection*, not this
addon's state. Without it '/' isolated whatever happened to be selected before
the session started, or nothing at all, on the one object the session is
demonstrably about. It selects the **resolved** source, so entering by way of
`<X>_Retop` does not leave the retopology selected; selection is not an ID, so
it is safe outside an undo step, and it falls inside the session's own step
anyway.

**Context follows what the user is doing.** `resolve_session_object` maps a
`<Something>_Retop` object to `Something`, so selecting the retopology and
starting a session carries on with the source rather than failing on a mesh
that has no patch data and never will; `enter_session_object` and the operator's
`invoke` both go through it, and the panel offers it as a button instead of the
useless "no Plasticity face data" warning. And **Blender leaving Object Mode
hands the viewport back**: the modal passes everything through and
`_leave_for_other_mode` drops to the `OBJECT` phase, because entering Edit Mode
on the retopology is the normal way to hand-tweak it. One exception, and it is
not optional: if the mesh being edited *is* the result mesh a re-edit took
faces from, the session stays put — `restore_reedit_removal` writes to that
mesh, and anything written while Blender owns it in edit mode is discarded on
exit, so the patch would be gone for good.

`OBJECT` → click a Plasticity object → `PATCH` → click a surface → `ADJUST`
→ commit → back to `PATCH`. Esc: `ADJUST`→`PATCH`, `PATCH`→`OBJECT`,
`OBJECT`→ end. Commit clearing `active_face_id` is the signal the modal
watches to return to picking. Clicking a patch that's already committed enters
`ADJUST` in re-edit mode (`state.editing_committed`): same keybinds, but commit
replaces the existing patch.

**Starting resolution.** `state.resolution` (Very Low … Extreme, powers of two)
scales what `generator.default_spans` computed, via
`state.scale_default_spans`. Every factor carries a `RESOLUTION_TRIM` of 0.75
on top of its power of two: what the generators compute from edge lengths reads
about a quarter too dense in practice, and each preset was inheriting that.
The ratios between presets are unchanged — High is still exactly twice Mid. Order matters and is asserted: the preset is
applied **first**, then propagation from a committed neighbour, then the spans
a patch was committed with — scaling either of those would break the welds they
exist to make, and would re-shape finished work on re-edit. Never below 1 span.
The n-gon has no span to scale; its resolution is `ngon_angle`, which Ctrl+wheel
drives directly (inverted, and multiplicatively — a 2° step is nothing at 90°
and everything at 4°).

Keybinds in `ADJUST`: Ctrl+wheel = span (or N-gon detail), `0-9`/Backspace = type a span,
Tab = U/V (quad/wedge), `N` = n-gon mode, `M` = match a neighbour,
`X` = delete the patch (re-edit only), right-click or Enter = commit,
Esc = clear typing then discard. Plain wheel stays zoom.

Ctrl+Z is deliberately *not* one of them: the modal passes it through to
Blender, and the session's own undo steps (one per committed patch) are what
make that mean "take the last patch back" instead of "roll the session back".

**Tab is per phase, and every phase of a session answers it.**
In `PATCH` it opens the hand-edit round trip below; in `OBJECT` it opens the
*selected* object's retopology (see `tweak._session_source`) — it used to fall
through to Blender there, which put the CAD **source** into Edit Mode, the one
mesh nothing here ever wants edited by hand. In `ADJUST` it is U/V — and it
is still swallowed on a single-span generator, because a patch is open there
and letting Blender toggle Edit Mode would take the session out from under it,
but it now *reports* that instead of doing nothing. A key that does nothing and
says nothing reads as a captured key, which is exactly how the unconditional
version of that line got reported as "the addon eats Tab".

**The session refuses to start outside Object Mode** (`RETOP_OT_session.poll`).
It used to run the whole entry path from Edit Mode — create the result mesh,
create the preview object, push an undo step — and only *then* have the modal
hand the viewport straight back, since it does nothing in another mode. Two
datablocks created from inside an edit session, for a session that never
opened: that is the shape of the Ctrl+Z crash the undo invariant exists to
prevent. The panel says which mode to leave rather than offering a dead button.

**Deleting a patch** (`X`, `RETOP_OT_delete_patch`) falls straight out of the
re-edit model: picking a committed patch already took its faces out and
snapshotted the result mesh, so deleting is `keep_reedit_removal` — dropping
that snapshot instead of restoring it — with nothing committed in its place.
Hence the poll on `editing_committed`: there is nothing to delete on a patch
that was never committed. The removal used `context='FACES'`, so the shared
boundary vertices a neighbour still uses survive and its welds hold.
`forget_patch_settings` clears the patch's own entry, but the **span registry
is deliberately left alone** — its entries are keyed by corner pair, i.e. they
describe a shared boundary whose other side is still committed, and dropping
them would break that neighbour's propagation to describe a patch that no
longer exists. Shading is re-applied because a crease belongs to the border
*between* patches, so losing one changes the edges of the ones around it.

**Matching a neighbour.** The patch's sides are drawn in the viewport
throughout `ADJUST` — green where a committed neighbour can be matched, grey
where there is nothing to match, orange under the cursor. `match_mode` is on by
default and `M` toggles the highlight; it is a *preference*, so it survives
between patches and sessions (`_clear_match_state` resets the pins and the
hover, never the mode). A left click has no other job while adjusting a patch,
so it takes the side under the cursor, and **commits when it is pointing at
none** — same as right-click and Enter. The picker deliberately does not take
`Esc`: it is always on, and swallowing `Esc` would leave no way to discard.

Hit-testing is in *screen* space (`nearest_side_to_cursor`): the sides are
polylines lying exactly on the surface, so a raycast hits the surface beside
them as often as the line.

**A match copies vertices, not a count.** Copying the neighbour's segment count
is not enough to weld to it and the difference is invisible until you look at
the vertices: the two patches only coincide if they also divide the *same*
polyline the same way, and they often don't — a neighbour committed as an n-gon
put its points where the boundary curves, not at even spacing, so a grid
resampling evenly to the same count lands between them every time.
`mesh_build.committed_boundary_points` therefore gathers the neighbours' own
committed vertices — once per patch, not once per side, since it walks the
whole result mesh and a patch with eight sides was walking it eight times per
hover — and `match_side_to_points` picks out the ones lying along a side. It
returns `None` unless they cover both endpoints of the side (a neighbour
touching half of it has nothing to align the rest against — matching a count
there *is* the half-cell offset).

**How far it reaches is two answers, not one.** `side_match_tolerance` is float
slack by default: both patches usually resample the same polyline, so the real
distance is ~0, and that strict answer is what the *automatic* matching uses —
it fires without being asked, so it must never reach for something that merely
happens to be nearby. `margin=True` widens it by `match_margin`, and that
is the picker's answer: pointing at a side says which neighbour you mean, so it
can reach one that has drifted — a coarse neighbour whose chords sag off a
curved boundary, two CAD edges tessellated slightly differently.
`SideReference` carries both, and `apply_side_matches` takes the strict one for
automatic matching and the generous one only for sides you pinned.

Both are a share of `reference_length`, which `build_side_references` sets to
the **patch's longest side**, not the side being matched. A neighbour's drift
is an absolute distance; scaling by the side made a short side's reach vanish,
so a stub between two retopped faces refused while the long side beside it
matched without trouble. And "found one point" is reported as its own reason
rather than "no neighbour": a side shorter than the neighbour's vertex spacing
genuinely has nothing to follow, and saying which of the two it is matters.

**The generator a patch was committed with is recorded, and that is what
makes "copy that one's density" answerable.** `register_patch_settings` stores
`{span_u, span_v, span, generator}` per face id; the re-edit path already read
the spans back, and `copy_spans_from` reads the generator too.
`Ctrl`+click in `ADJUST` raycasts the patch under the cursor and takes its
spans -- **only** when the two patches were built by the same generator, which
is the whole of the rule: a Ring's two counts mean around and across, a Quad's
mean its own U and V, an N-Side's single one means segments per side, so the
same number carried across is a number rather than a density. The refusal names
both. This is the half that propagation cannot do: propagation is about a
*shared boundary*, where two patches must agree or crack, and two patches that
never touch have no such constraint for the mesh to derive.
**A click carrying a modifier is a binding, and the modal has to dispatch it.**
Clicks are held out of `_dispatch_bound` at the top of `_modal` on purpose --
a plain one means "the thing under the cursor", which only the picker can
resolve -- but that exclusion swallowed *modified* clicks too, so `Ctrl`+click
fell through to the commit fallback and left `ADJUST` with nothing copied. The
dispatch is attempted again just before that fallback, which is safe for the
plain click as well: the picker has already had it, `pin_neighbour` polls on a
side being under the cursor, and with none there nothing runs and the fallback
still commits.

**And the hover says what the click would take.** A colour that only appears
after the click is a colour that answers a question nobody can ask any more, so
the modal writes `state.copy_hover_face_id` on every mouse move in `ADJUST`
(one raycast, the same one the `PATCH` phase already pays for) and the overlay
outlines that patch with `cad_display.edge_segments`, which is cached per
patch -- a hover may not walk a mesh. Amber, the colour the sides gave up when
matching became binary: this is not a state of the patch being adjusted, so it
must not land anywhere on the matched/unmatched scale. It is drawn **dimmed
rather than not at all** when the generators disagree, because an outline that
appears only over a valid target makes a refusal indistinguishable from a
target that was never there; the tooltip then says which of the two it is.
`mesh_build.copy_source_status` is that wording, and it lives there rather than
in `operators` because the **overlay** asks it and the overlay may not reach
into operators.

The click reads `overlay.cursor_window` rather than an event -- a SESSION
action is dispatched by the modal, which does not hand the operator its event,
the same arrangement the side tooltip has -- so `ray_from_event` is now a thin
wrapper on `ray_from_window`. **Between two Quads the U/V axes are each patch's own, and clicking the same
patch again is what resolves them.** Which direction is U comes from where the
boundary walk started, so a copy lands rotated about half the time and nothing
in either patch says which half in advance -- there is no rule to get right
here, only a way to turn it over. `state.copy_source_face_id` /
`copy_source_swapped` carry that, cleared by `set_active_patch` with the rest
of the per-patch state (kept, the first click on the *next* patch would come
back swapped). A ring never needs it and a single-span generator has nothing
to exchange, which is reported rather than ignored. The tooltip shows the
spans **as the next click would apply them**, so it previews the click instead
of describing the record.

**Which corners are side boundaries is a choice, and `Ctrl`+click on a side
opens it.** A Plasticity face with five B-rep vertices goes to the N-Side fan,
and the fan is usually not what the shape wants: a pentagon born of a quad with
one corner cut, chamfered or booleaned reads as a *quad* whose bottom side
happens to be two sides in a row. Turning that junction off — keeping the
vertex, dropping its status as a side boundary — merges the two sides meeting
there, and four groups is what `find_generator` should then be handed.

**A corner, not a side, and that is the whole of the gesture.** "Turn side 2
off" has to say whether 2 joins 1 or 3; "turn this corner off" merges the two
sides that meet at it and there is nothing left to choose. A corner is named by
the side it *starts*, so one flat index serves the overlay, the hit test and
the stored override. `sidematch.corner_groups` / `loop_group_counts` are the
grouping, in `sidematch` because the **overlay** reads them on every redraw and
a draw handler may never import `operators`; only `nearest_corner_to_cursor`
stays there, since it needs a region.

**A sub-state of `ADJUST`, never a phase of its own.** `session_phase ==
'ADJUST'` is tested at some twenty places — eight in `overlay` alone — and the
patch is still open, still has spans, still has a preview, so every one of them
must stay true. An enum value would have turned them all off in silence.
`state.corner_edit` is the flag and `operators._in_phase` is where the
exclusion lives once, so every action polling through it is inert for the
length of the edit without restating it; the two CAD displays opt back in with
`during_corner_edit=True`, because the structure is read *while* choosing
corners. `commit`, `back`, `pin_neighbour`, `copy_spans` and `delete_patch`
spell their own polls and exclude it by hand.

**One key, two meanings, resolved by poll — and both halves written out.** The
editor takes the plain click, `Enter`/right-click and `Esc`; it wins by being
declared **first** in `ACTIONS` *and* by the patch's own actions polling on it
being closed. Either alone would work today, and "it works by an order nothing
states" is exactly what sent `Tab` to `object.editmode_toggle`. `Ctrl`+click is
split the way the plain click already is — by what is under the cursor: a
**side** of this patch opens the editor, anything else copies a density. So
`copy_spans` now polls `hovered_side == -1`, and `_modal_match` stops writing
`copy_hover_face_id` while a side is hovered — an amber outline promising a
copy and delivering a mode is the failure the hover exists to prevent. Neither
branch destroys anything, which is what makes a few pixels of ambiguity
affordable; `X` falling through to `object.delete` is what it looks like when
that is not true.

**A loop is never taken below two sides.** One closed side is what
`sides.synthesise_corners` exists to undo, and `find_generator` starts at
Wedge 2 — so demoting the last corner of a loop would leave the patch silently
unpickable. Refused at the click, while the editor is still open to undo it.
Esc restores the set the editor was opened with (`corner_edit_backup`): the
edit is several clicks long, so it owes a way out that neither commits the
patch nor keeps a half-made corner set.

The overlay paints the sides **by group** while it is open, in colours that are
neither green nor red — those two mean "welds" and "cracks" everywhere else
here, and a group is neither — with the side picker's own colours and tooltip
suppressed for the duration. A demoted corner is drawn small and dark rather
than not at all: it is still a vertex of the mesh, and one that vanished would
read as geometry lost. `tests/test_corner_edit.py`.

**Not wired into the generators yet.** The grouping is chosen, stored and
drawn; `_generate_for_face` still hands the ungrouped sides over, so a pentagon
is still filled by the fan. The test says so out loud so a green suite is not
read as "five-sided patches are handled". What the next step needs, and why it
is the only real work here: the demoted corner must stay a **mesh vertex**,
because neighbours weld to it, and `resample_polyline_by_arclength` over a
merged side would slide off it — so the group's span has to be allocated per
sub-side as integers, the way `nside.spoke_allocation` allocates spokes.
Matching stays per *real* side throughout (`side_neighbours` and `_match_pool`
must keep seeing five), and what is given up is that corner's
`retop_source_vid`: it welds by proximity instead of identity, and span
propagation out of it stops — the same trade `sidematch._recut_arbitrary_loop`
already accepts.

**A neighbour covering part of a side is completed, not refused — and only
when you point at it.** A bore's rim is one long cornerless side and the patch
beside it may touch an eighth of it; matching used to refuse outright. What it
was right to refuse is matching a *count* over a partial cover, which lands
between the neighbour's vertices everywhere except by luck — that is the
half-cell offset the coverage rules exist to prevent. Reproducing the
neighbour's vertices where it reaches and **filling the rest at its own
spacing**, along the side's own polyline, has no offset to leave: the shared
stretch is exact and the remainder borders nothing.
`match_side_to_points(partial=True)` is that, `_complete_open_side` and
`_complete_closed_side` do it, and `build_side_references` passes it on the
**margin** call only — the picker's answer. Automatic matching keeps the strict
one, for the same reason it does not take the margin: how to divide the rest of
a side is a decision, not something to do to every side of every patch a hover
passes over. An open side keeps its own endpoints exactly, because those are
corners welded by *identity*; a closed one has none to keep, so the whole
remainder is one gap and it is filled as one arc **through the loop's start**,
which is wherever the half-edge walk began and which nothing else in the model
agrees on. `MAX_MATCHED_SEGMENTS` is a backstop, not a rule.

**A closed side's gaps are arc lengths, not chords.** `_close_matched_ring`
compared straight-line distances between consecutive matched points against a
share of the loop's *arc* length, and on a loop those two diverge without
limit: a neighbour covering an eighth of a rim leaves a gap of seven eighths,
but the chord closing it is shorter than the rim's diameter — 0.44 against a
6.28 perimeter, i.e. 7% of the loop where the truth is 93%. So that neighbour
read as covering the whole rim and the rim came back built from the eighth of
it the neighbour had touched. Measured along the side, the same case refuses
(or is completed, above). No fixture number moves: every object's open-edge
count is unchanged, which is what says this only ever fired where it was wrong.

**A closed side has one endpoint, not two.** A cornerless loop — a ring's rims,
a disc's boundary — comes back from `resolve_side_points` as `loop + [loop[0]]`,
so requiring a committed vertex "at both ends" asks for two in the same place
and always refuses: a bore's rim read as unmatchable while visibly bordering
finished retopology. `_close_matched_ring` handles that case instead.

Coverage becomes the largest *gap* between consecutive matched points, and it
has to be both a large share of the loop **and** far bigger than the others
(`CLOSED_SIDE_GAP_RATIO`): a merely coarse neighbour has every gap the same
size — a square inscribed in a circle already leaves 22% between points — while
one covering half the rim leaves one huge gap among small ones. Testing the
share alone refused coarse but perfectly matchable neighbours.

A neighbour vertex on the side's *start* is deliberately not required. That
start is arbitrary — a cornerless loop begins wherever the half-edge walk
happened to — so it is not a B-rep vertex and nothing else in the model agrees
on it; a disc committed as a Quad puts its points at arc-length resamples from
its own synthesised corners, which land nowhere near. The match is rotated to
lead with whichever point is nearest, and `apply_side_matches` then **drops
that side's corner id**: a corner welds by *identity*, so leaving the name on a
point that has moved would make a later patch reuse a vertex somewhere else, or
drag this one onto it. It welds by proximity instead, like every other boundary
point.

**A corner nothing agrees on is cut where the neighbour already put one.**
Nothing on a disc's boundary is a corner, so `sides.synthesise_corners` cuts it
into four at the quarter points of its arc length -- and those four are
arbitrary in the strongest sense: the loop had to be split somewhere to have
sides, and no other part of the model has an opinion on where. The neighbour
has its own vertices along that same circle at its own phase, so the quarter
points fall *between* them, and the endpoint-coverage rule then asks each side
for a committed vertex at a corner the neighbour has no reason to have one at.
Whether it finds one is a coin toss per corner: on the fixture's truncated cone
two of the four sides landed within 0.0026 of a rim vertex and matched, while
the other two had theirs claimed by the side next door -- it lies *on* that one,
so `rivals` drops it, correctly -- and refused with "neighbour stops short of
this side's start". That is the reported "a cap can never be matched to the ring
around it", and no tolerance fixes it: widening one far enough to swallow a
whole vertex spacing is exactly the half-cell offset the rule exists to prevent.
`sidematch._recut_arbitrary_loop` re-cuts instead. The boundary is matched
**once**, as the closed side it really is -- the `_close_matched_ring` path,
rotation and all -- and the sides are carved out of the neighbour's own ring of
points, so every corner lands on one at distance zero and the *strict* tolerance
passes, i.e. automatic matching fires where a hand pin used to refuse half the
sides. Three things make it safe. Only a loop `sides.resolve_corners_detail`
flags **arbitrary** comes here -- a shape corner (a strip's end, a slot's cap)
is a fact about the boundary and is never moved, which is why the flag is set
by `synthesise_corners_detail`'s arc-length fallback alone. The side **count**
is kept, so `find_generator` still sees a quad and the commit path, which
re-prepares the patch and replays the references by index, still lines up. And
it runs only with `auto_match_neighbours` on: moving a patch's corners is not
something to do unasked. What it gives up is the corner ids (blanked to
`NO_SOURCE`, so span propagation *out* of the patch stops) -- its corners were
never B-rep vertices, so the pairs it would have registered named nothing a
neighbour could look up. `tests/test_disc_match.py` asserts both ends: that the
quarter cut really is unmatchable, or the test proves nothing, and that every
side is applied afterwards on grid and n-gon alike. On the fixture the two cones
went from 140 and 200 open boundary edges to zero.

**Two sides driving one span is only a conflict when they want different
counts.** `_winning_matches` outvoted every side but one per span key, which is
right when a grid is asked for 12 along a direction by one side and 13 by the
other -- and wrong when both ask for the same number, which a re-cut loop makes
the normal case (`_opposed_segment_counts` spreads the remainder over *opposite*
pairs precisely so they agree). Outvoting one of them anyway left half a disc
welded and half of it on the CAD tessellation: a crack down a boundary that had
just been arranged to close. A tie is recorded on the loser as
`SideReference.tied_points` -- with `tied_key`, so a side of *another* span with
a coincidentally equal count is never swept in with it -- and `_with_ties` hands
both sides their own neighbour's vertices. Each keeps its own points: they agree
on the count, not on where they are.

**How far a match reaches off a side is not how close two vertices have to be
to be the same one.** Both were `tolerance`, and on a real part the two numbers
cross: the picker's margin is a share of the *patch's longest side*, which on a
ring is a whole rim, so deduping at that distance merged the neighbour's own
consecutive vertices — 61 points came back as 31, and the side was asked to
reproduce half the count it should have. `match_side_to_points` takes `merge`
separately, and every caller passes the strict tolerance for it: two copies of
one welded vertex are coincident, two different ones are a segment apart.

**And a neighbour's second row is not the edge under the cursor.** A committed
patch is a grid, so there is another row of its vertices one cell behind the
one it shares; on a narrow band that row is well inside a pinned side's reach,
and taking both rows is what "the match spreads over the surface instead of
following the edge I pointed at" looks like. Two rules keep it out.
`rivals` — the patch's other sides — drops a candidate that is nearer to one of
them than to this side, which is what settles a band's two rims. And
`_nearest_row` cuts the rest by a **gap**: within one row the distances vary
smoothly (that variation *is* the drift the margin exists to reach), so a step
larger than everything the row has varied by so far is a different row. No
constant says how far a neighbour may drift — only that a jump is not drift.

**A side may only match the faces it actually borders.** This used to be pure
proximity — one pool of every committed vertex in the result mesh, keep
whatever falls within the tolerance — and proximity cannot tell "the patch
across this edge" from "a patch that happens to run close by". A face stacked a
fraction above another, a thin wall, two sheets meeting at a shallow angle: all
of them put committed vertices well inside a side's reach without touching it,
and the side came back with a run of vertices tracing a loop through its
neighbourhood instead of the edge it shares. The mesh already says which face
is across each boundary segment, so `patchprep.side_neighbours` returns the
whole list per side (`[0]` is the majority, which is only what the picker
*names*), `mesh_build.committed_boundary_map` groups the result mesh by owning
patch, and `sidematch._match_pool` intersects the two.

A side is still matched against **every** face it borders, not just the
majority one: a side runs against two committed patches whenever the boundary
between them falls mid-side, which is the normal case when the angle test
didn't put a corner there. Untracked retopology (`NO_PATCH`, predating patch
ids) belongs to no named face and so stays in every pool. When a side's
neighbours are named but none is committed, the pool is **empty** rather than
falling back to the rest of the mesh, and the reason names the patch it is
waiting for. `tests/test_match_specificity.py` builds the stacked-sheet case
and asserts both that it is refused and that plain proximity would have taken
it — otherwise the test proves nothing.

**Exclude the patch being generated, not the "active" one.** Picking a
committed patch generates it *before* recording it as active, so reading
`state.active_face_id` in `build_side_references` left the patch's own
committed geometry in its own pool and it matched itself: a re-edit came back
with whatever spans reproduced what was already there instead of the ones it
was committed with. `build_side_references` takes the face id explicitly.

**Green is never a side merely under the cursor.** Hovering an unmatched side
used to turn it the same green as a matched one, which put the two states one
mouse move apart and undid most of what the colours are for. A hover brightens
a side's *own* colour instead — never to green.

- **A crack outlives the session that made it, so the report has to too.**
  The side picker is the warning *while* a patch is open; commit it and the
  warning leaves with the preview. But the patch that ends up cracked is the
  one the user did **not** touch: match A to B, commit both, re-open A and type
  a different span, and it is B that is left with a seam. `mesh_build.crack_edges`
  is the standing answer — a CAD edge with a **committed patch on both sides**
  that the result mesh leaves open along it. Both halves of that matter: an
  open border with only one side committed is the frontier of the work, which
  every part has and none of which is a defect, and painting it would make the
  report meaningless for the same reason painting every unmatched side red
  would.
  **Detection reads the source's B-rep edges, never proximity between result
  vertices.** Proximity cannot tell "the patch across this edge" from "a patch
  that runs close by" — the same reason `_match_pool` exists — so two patches
  are only ever compared along an edge the CAD model says they share
  (`cad_display.shared_edges`, the pairs `edge_polylines` throws away).
  **And every open edge is assigned to exactly one border: the one it lies
  *along*.** Asking instead whether some open edge is *within reach* of a point
  on the border cannot work, and the reason is worth keeping: the reach has to
  be about the size of a retopology cell, and on a bevelled part the next
  border along is that far away too. Measured on `Cube Bevel Edges`, four
  perfectly welded borders came back "covered" by three samples of seven — by
  the open rows of the borders either side of them — against seven of seven for
  the two that were genuinely open, which is a threshold sitting inside the
  thing it is meant to separate. Assignment separates them by *kind*: a row
  along a border stays a chord's sagitta off it for its whole length (0.00 to
  0.05 of a cell, measured), while an edge leaving a junction is on the border
  at one end and a cell away at the other. `_border_along` probes the quarter
  points — **never the ends**, where a junction puts every border meeting there
  at the same distance and a coarse patch's whole-border-in-one-edge has
  nothing but ends — and takes the border whose *furthest* probe is nearest,
  requiring every probe to be within reach of it. One decision per edge, not
  one per probe: a border shorter than the tolerance (`Cube Chamfer Edges` is
  four cells wide) otherwise has its first probe answered by its neighbour and
  the disagreement drops the crack entirely. Then a border is cracked when each
  patch's assigned row covers `CRACK_COVERAGE` of it. Across the whole fixture
  that reports 25 borders, every one of them independently confirmed, and
  nothing between 0.00 and 0.42.
  And what gets **drawn is that CAD edge**, not either patch's row — the two
  rows sag off it by different amounts, which is the very thing being reported,
  so drawing them would draw the symptom twice and neither would be the border.
  Dashed, because the CAD edge overlay draws a solid line along the same curve
  and a second solid one reads as more structure rather than as a warning; the
  dashes are built into the geometry, since the builtin polyline shader has no
  stipple and a pattern computed per redraw is what the cache exists to
  prevent. Cached on both meshes' fingerprints, like everything else a draw
  handler reads. `tests/test_cracks.py` pins that a welded border reports
  nothing — or the test proves nothing — and that re-matching closes it.

**A grid cannot honour two counts in one direction.** Two sides driving the
same span used to both get substituted, with the second silently winning the
count — leaving the loser's points resampled to a number that was not theirs,
i.e. the exact crack matching exists to close. `sidematch.span_key_for` names
the span a side drives (an n-gon gets a key per side and a ring one per *loop*,
so neither collides with itself), `_winning_matches` keeps one per key — a pin
beats an automatic match, then the denser one wins — and only the winner is
substituted. The rest keep the boundary the CAD drew, and
`state.match_conflicts` tells the panel how many were outvoted. Every reader of
a key goes through `sidematch.span_base`, since a ring's is qualified by its
loop.

**An automatic match seeds a span; a pin decides it.** Spans are resolved
*before* any side is rewritten, and `sidematch._honours` then drops any match
the resolved span can no longer reproduce. Without that split, scrolling the
span on a side bordering a committed neighbour did nothing at all — the match
put its own count straight back every regeneration and the control looked
broken. Changing the count away from the neighbour's is how you say "don't weld
here"; a pin is immune, because it was asked for.

**A click on a matched side turns the match off.** It used to release the
*pin*, which with automatic matching on — the default — is invisible: the
automatic match put itself straight back on the next regeneration and the side
stayed green, so the click read as broken. `PIN_EXCLUDED` records "leave this
side alone" and `_match_candidates` honours it over the automatic pass, making
the gesture a plain two-state toggle. Clicking an excluded side matches it
again.

**Two kinds of pin, one gesture, because matching is binary.**
`PIN_NEIGHBOUR` follows the committed patch across the side; `PIN_EXCLUDED` is
"leave this side alone", above. A side with nothing committed across it cannot
be pinned at all — `adopt_side_reference` refuses it.

There was a third, `PIN_SOURCE`: the side's **own CAD tessellation**, thinned
by curvature with the same rule n-gon mode uses, needing no neighbour. It was
not redundant in effect — an unmatched side of a *grid* is resampled evenly by
`resample_polyline_by_arclength`, which cuts a chord across the very chamfer
`ngon.side_points` would have kept — but it was redundant as an *answer*.
Nothing is welded to anything, so it is not matching, and giving it a colour on
the matched/unmatched scale turned a binary question into a three-way one: the
overlay's own vocabulary said "this side is doing something with its
neighbour" about a side that has no neighbour. It also had a `Ctrl`+click of
its own for a while, dropped earlier for the same kind of reason. Keeping a
chamfer that the even resampling cuts across is a *density* question, and if it
comes back it belongs with the spans, not with the matching.

`state.side_overrides` stores the *kind*, not the count: the count is
recomputed from live geometry every regeneration, so a stored copy could only
disagree.

Every refusal carries a `reason`, surfaced in the click warning and the panel,
and the viewport **brightens a side's own colour on hover** rather than
replacing it — a single hover colour hid the one thing worth knowing before
clicking, which is whether the side can be matched at all.

**Green means a side is being matched, and red means one that should be is
not.** Three states for one binary question, and the middle one is the point:
`applied` is green, `available and not applied` is **red**, and everything else
is grey. Red is a side with finished retopology across it that the patch is not
welding to — outvoted on a span collision, span typed over since, or released
by hand — which is exactly where a crack ends up. It used to draw the same grey
as a side with nothing across it, so the one state worth acting on was hidden
among the ones that are simply normal, and only the tooltip told them apart.
Painting *every* unmatched side red is the obvious alternative and it is wrong:
on the first patch of a model nothing is committed anywhere, so the whole patch
would come up red with nothing wrong at all — red has to mean a problem or it
means nothing. `SideReference.applied` (set by `apply_side_matches`, which is
the only place that knows) and `.available` carry it;
`overlay._side_appearance` maps it, and `ui._draw_match_block` counts the red
ones separately and alerts on that count alone. The **tooltip by the cursor**
uses the same three-way answer: `sidematch.status_of` returns the one wording
the overlay and the panel both use ("Matched" / "Not matched — this edge will
crack" / "Nothing to match along this edge", plus why), so the two can't
disagree about the side under the pointer. The
tooltip needs the mouse and a draw handler has no event to read it from, so the
modal leaves it in `overlay.cursor_window` — the same arrangement as
`hover_committed`, cleared the moment the pointer leaves the viewport.

What makes those points cheap to use is `resample_polyline_by_arclength`:
asked for exactly as many points as it was given, it returns them untouched. So
`apply_side_matches` substitutes them for the side's polyline in `prepared`,
and every generator — Quad, Triangle, Wedge, N-Side, Ring, N-gon, **none of
them modified** — reproduces them exactly, as long as it puts `len - 1`
segments along that side. That count is what the pin stores
(`state.side_overrides`, JSON, cleared by `set_active_patch` because a pin
names a side *by index* on the patch it was picked on).

`auto_match_neighbours` (default on) applies the same substitution
automatically to **every** generator, not just n-gons: a grid that copies only
the segment count still lands between the neighbour's vertices whenever the
neighbour didn't space them evenly. Automatic matching takes only the strict
answer; the margin is for sides you pointed at. The commit re-runs the
substitution before registering, or the registry would advertise a curvature
count on a side that was actually matched.

The overlay draws **which vertices a match would take** — green dots on the
hovered side's candidates and on every pinned side's. Knowing a side *can* be matched is only half of it; a match
going to the wrong neighbour or stopping short is invisible from a coloured
line lying on the boundary. `SideReference` carries world-space copies for
exactly that, computed at generation time — a draw handler has no business
transforming points on every redraw.

`sidematch._active_sides` is a module global holding Vectors describing a
preview: rebuilt on every generation, dropped on every session exit, and empty
after a reload — the overlay must cope with that.

**N-gon mode** replaces the span grid with one face following the boundary,
for flat faces where a grid is only wasted geometry. A patch committed as an
n-gon reopens as one whatever the current mode — same rule as its spans.
Toggling the mode goes through the property's update callback, and
`regenerate_active_preview` writes `generator_name`/`num_sides`/`num_loops`
back so the panel and overlay follow.

`operators.ngon_blocker` decides whether a patch may take one at all, and
writes `ngon_available`/`ngon_unavailable_reason` for the panel and the `N`
key to explain themselves instead of doing nothing. Two blockers:

- **Not flat.** `patch_is_planar` compares every polygon of the patch against
  their average normal (`ngon_planar_tolerance`, 5° default). One face across
  a bevel or a fillet is a flat lid over it — the shape is simply gone. This
  runs on every hover, so it uses polygon normals only: no boundary walk, no
  KD-tree.
- **More than one hole.** One hole is fine: `generate_holed` bridges it to the
  outer boundary with two edges and emits **two** n-gons. That is forced —
  a Blender n-gon carries a single loop, and the one-face "keyhole" alternative
  needs the bridge vertices duplicated, which the boundary weld then merges
  back and destroys the face. Two faces need no duplicates and stay manifold.
  The hole loop is wound opposite to the outer one (both are half-edges of the
  same patch), which is why both arcs are walked *forward*. Corner indices are
  emitted outer-loop-first to match `PreparedPatch.corner_source_ids`, and the
  commit registers spans per loop like a ring does.

The mode gate and the corner method interact: the method depends on which
generator will run, so `_generate_for_face` decides the mode *first* and
re-prepares the patch if a blocker only shows up once the loop count is known.

**Its boundary is *selected*, never resampled** (`ngon.side_points`): it walks
the source boundary accumulating turn and keeps a vertex every `ngon_angle`
degrees. This is not a cosmetic choice. `sides.py` only calls a vertex a corner
past `corner_angle_threshold` (45° of deviation), so a chamfer — typically
20-40° — is *not* a corner and sits in the middle of a side; arc-length
resampling, which is what this did first, put points wherever the even spacing
fell and cut a straight chord across it. Accumulating turn keeps the chamfer's
own vertex, and every kept point is a genuine CAD boundary vertex.

The cost: a curvature-selected n-gon side does not line up point-for-point with
a *grid* neighbour along a shared edge, so only their shared corners weld. Side
matching buys that back exactly — a matched side is handed the neighbour's own
vertices — and `ngon_match_neighbours` does it without being asked.

## Keys (`keymap.py`, `prefs.py`)

The session's keys were `event.type == 'X'` comparisons inside `_modal`. That
leaves nothing to remap and nothing in Blender's keymap editor to find either,
because a modal operator reads raw events and never goes near a keymap. The
first attempt at fixing that was a table plus a capture modal plus a panel to
edit it in — a second, worse keymap editor next to the real one.

**They are real `KeyMapItem`s on real operators now, and Blender owns all of
it**: the editing UI, the conflict display, the per-item restore, the
persistence in the user's preferences. `keymap.py` only *declares* what to
register; `prefs.py` draws the rows with `rna_keymap_ui.draw_kmi` on the
addon's preferences page, and the panel's Keybinds tab is a button that opens
it (`RETOP_OT_open_keymap_prefs`) plus a read-only list of what isn't
remappable. The same items show up under Preferences > Keymap > Add-ons.

**Who dispatches them differs by scope, and that split is not belt-and-braces.**
`GLOBAL` actions (isolate, mirror, x-ray) are dispatched by Blender like any
keymap item, because they must work with no session. `SESSION` actions are
dispatched by the **modal**, which resolves the event against the live items
(`keymap.session_action_for`) and runs the operator itself.

Letting them fall through to the keymap does not work: an item in the `3D View`
keymap does not reliably beat one in a *mode* keymap, and the session's keys
collide with those constantly — `X` is `object.delete` in Object Mode, `Tab` is
`object.editmode_toggle` in Object Non-modal. Registering each action in
whichever keymap owns its competitor is unmaintainable and still loses to the
next addon that claims the key; MACHIN3 puts its own `Alt+X` in `Mesh` rather
than `3D View` for exactly this reason. The modal sits above every keymap, so
dispatching there always wins, and the items stay real — Blender's rows edit
them, the keymap editor lists them, the preferences save them.

The failure that forced this was not cosmetic: with `X` falling through,
pressing it on a patch that turned out not to be committed reached
`object.delete` and took the CAD object with it. Hence `_MUST_CONSUME`: a
session action whose poll *fails* is still consumed for the two keys Blender
claims (`delete_patch`, `hand_edit`) and says why; everything else falls
through on purpose, so `N` outside `ADJUST` still opens the sidebar.

**No key is spelled out in `_modal`.** `tests/test_keymap.py` greps its source
for the event types it must not test — a hardcoded `event.type == 'X'` is what
made them unremappable in the first place, and it comes back one line at a
time.

**The `poll` is where the phase logic lives.** Every action is offered whether
a session is running or not, so each operator polls `session_active` and its
phases. Three actions share `TAB` — U/V in `ADJUST`, hand-edit in `PATCH` and
`OBJECT`, back-from-hand-edit in `TWEAK` — with mutually exclusive polls, so
the first whose poll passes is the one that runs. That is the "one key, two
meanings" design expressed as data instead of spelled out. With no session, all
three fail and `Tab` belongs to Blender again.

**And the modal resolves a shared key the way Blender does: first poll that
passes, not first match.** `keymap.session_actions_for` returns *every* action
bound to the event and `_dispatch_bound` walks them. Returning only the first
match resolved every `Tab` to U/V, whose poll fails outside `ADJUST`; the key
then fell through to the keymap and was answered by whichever item happened to
be registered first. It worked — but by an ordering nothing states, it made
`_modal_tweak`'s own `end_tweak` branch dead code, and in the `OBJECT` phase it
reached Blender's `object.editmode_toggle`. When none of the candidates polls,
a key Blender claims is still consumed (`_MUST_CONSUME`) and the refusal
reported, so `Tab` can never leak mid-session.

**No key of the addon's is live outside a session.** The session's keys never
were — every one of those operators polls `session_active` — but the three
`GLOBAL` ones (`/`, `Alt+X`, `V`) were claimed from the moment the addon
was installed, and all three are keys something else wants: Hard Ops binds
`Alt+X`, and `/` is Blender's own isolate. An addon that has to be *disabled*
to give a key back is not self-contained, so `operators._global_keys_live`
gates those polls on a session being open, and **a failing poll is what hands
the event on** — Blender skips the item and the next handler down (the other
addon, or Blender's own binding) runs it, unchanged.
`keymap.global_keys_outside_session`, an addon *preference* (per user, not per
file), is the way back for anyone who wants the isolate and the mirror between
sessions.
Panel buttons are unaffected: the mirror's UI goes through
`retop.mirror_axis` / `retop.apply_mirror`, which are bound to nothing and
still poll on having a result mesh. `tests/test_keymap.py` pins that every
`GLOBAL` action is gated — by comparing against the scope in `ACTIONS`, so a
new one added there fails the test until it is considered.

**Two things stay outside the keymap**: the **digits and Backspace** (numeric
entry, not a shortcut — they must stay instantaneous and only make sense as a
block), and the mirror's **`Alt+X` then `X`/`Y`/`Z`**, a key *sequence*, which
Blender's keymap cannot express.

**The left click is split, not fixed.** Taking the side under the cursor is
`retop.pin_side`, two normal bindings differing by a `source` property (plain
click follows the committed neighbour, `Ctrl` the CAD edge). What stays in
`_modal_match` is only the *fallback* — nothing under the cursor, so commit —
which genuinely depends on the hover; the picker returns `PASS_THROUGH` the
moment a side is under the cursor and the binding takes it from there. Ctrl+
click was fixed for a while purely because it had been lumped in with that
fallback, which it never shared.

**`typed_span` moved to the scene.** The keys that clear it — U/V, N-gon, the
span wheel — are operators now, and an operator has no way to reach the running
modal's attributes. The overlay echoes the same property.

**`RETOP_OT_back` asks for the session to end; it does not end it.** The timer,
the modal cursor and the draw handlers belong to the modal instance, so the
operator clears `session_active` and the modal acts on it at the top of
`_modal`. Same shape as the undo reconciliation.

**The modal catches up with phases it did not cause.** `retop.back`,
`retop.tweak_mesh` and the panel's buttons all move the phase without telling
the instance, leaving a stale hover and a cursor describing the phase before.
One `session_phase != self._last_phase` check covers every route in — and it
clears the hover only when the new phase is `PATCH` or `OBJECT`, because
entering `ADJUST` is the modal's own click handler and the hover it just built
*is* the preview.

**The overlay reads the live items** (`keymap.describe`), never the
declaration: a hint that says `E` when the key is now `Ctrl+E` is worse than no
hint, since it is the one place a user checks before deciding the feature is
broken. It falls back to the declared default when nothing is registered, which
is the `--background` case. `items_for` drops wrappers Blender has freed under
it — a draw handler is the worst place to find that out. `commit` is the one
action whose hint lists *all* its bindings; the right-click is the
Plasticity-style affordance people arrive expecting.

**The registry keys on the action, not the operator.** Two items share
`retop.nudge_span` and differ only by a `delta` property, so matching them back
by idname would pair them up wrong.

**A reload does not lose settings, and nothing here saves them.** Blender keeps
a PropertyGroup's values as ID properties on the scene, keyed by name, so
`del bpy.types.Scene.plasticity_retop` and re-declaring it re-attach to the
same stored data. `tests/test_reload.py` asserts it rather than trusting it: it
is a fact about Blender's storage, not about this code. The same test pins that
a reload neither stacks the app handlers (removed *by name*, since a reload
leaves a new function object) nor orphans keymap items (the unregister happens
*before* the modules reload, or the session's keys would fire twice per press).

## Symmetry (`mesh_build`, `RETOP_OT_mirror`)

`Alt+X` then `X`/`Y`/`Z` mirrors the retopology — the Hard Ops reflex, which is
why the retopo x-ray moved off `X` altogether, to `V`. **Not `Alt+Z`**: that is Blender's own
viewport X-ray, and the note under `result_see_through` about not taking it
over still stands. `Shift+X` was the x-ray's key until it turned out that in
practice the press came out as `object.delete`'s confirmation popup — with two
of the addon's own meanings already hanging off `X` (the mirror's `Alt+X`, the
patch delete's bare `X`), a third was one too many.

**The axis prompt draws a gizmo under the cursor**, not only a status-bar line.
The status bar is at the bottom of the *window*: with a hand on the keyboard
and eyes on the part, it is the one place nobody is looking. Three squares
labelled X / Y / Z, lit for the axes already mirrored — which the key alone
cannot say, since that state lives on the modifier rather than in the scene.
It has **its own draw handler** (`overlay.enable_mirror_gizmo`) rather than
riding the session's, because the mirror is a `GLOBAL` key: with
`global_keys_outside_session` on it is armed with no session running and no
session overlay installed. Every exit path of the modal goes through
`RETOP_OT_mirror._finish`, and `overlay.disable()` drops it too — a draw
handler nobody owns is the one kind of leak the version string cannot report,
and `tests/test_overlay.py` pins both ends.

**It is a Mirror modifier on the result object, never baked geometry**, and
that is not merely non-destructiveness. Every piece of bookkeeping here reads
the result mesh's *base* data — commit and re-edit through `PATCH_ID_ATTR`,
matching through `committed_boundary_map`, `apply_result_shading`,
`adopt_untracked_faces` — so a modifier is invisible to all of it by
construction. Baked mirror faces would carry the same patch ids as the
originals, and `remove_patch_from_result` deletes *every* face carrying the id
being re-edited: re-editing one patch would take both halves out and rebuild
one, tearing a hole in the mirrored side that nothing would put back.

**The plane is the source object's origin** (`mod.mirror_object = source_obj`),
not the result object's. Plasticity drops every import at the world origin so
the two coincide today, but that is a fact about the current bridge, and the
source object is what "the object" means to the user.

**Which axes are on lives on the modifier, not in scene state.** They belong to
one object, and two objects retopped in the same file have no reason to agree.
`mirror_axes` reads them back for the panel; only *how* the mirror behaves
(`mirror_clip`, `mirror_merge_distance`) is a scene preference. A modifier is
not an ID, so all of this is safe from a property callback and outside an undo
step — the same reason `_apply_offset_modifier` is.

**Applying it has to stamp `NO_PATCH` on the copies**, which is the whole
reason `bake_mirror` exists instead of a note pointing at the modifier
dropdown. Untracked is the right resting state: unclaimed faces are never
deleted, and `adopt_untracked_faces` hands each copy to the Plasticity face it
sits on the next time the object is entered — which, on the symmetric part this
was used for, is the real face on the other side. The copies are told from the
originals by **face centre**, not by index: the originals come through the
apply untouched so their centres match exactly, while assuming Blender appends
the mirrored half is an ordering detail that could change under a silent
corruption.

That hand-off is **only correct because the part is symmetric**, and the
degenerate case is worth naming: mirror a patch out over empty space, apply it,
and adoption has only the *original* patch to offer the copies — they join it,
and re-editing it then takes them with it. That is mirroring something that
isn't symmetric, which is a user error the adoption rule degrades on rather
than a case to defend against; `tests/test_mirror.py` builds the symmetric part
deliberately and says why.

**A modal on top of the session modal is how the axis prompt avoids a
collision.** `X` in `ADJUST` deletes a patch, and a modal handler sits *above*
the keymap `Alt+X` is bound in — so the session modal now requires a bare `X`
(no alt/shift/ctrl), and once `RETOP_OT_mirror` is armed it sees the axis keys
first anyway. The prompt cancels on anything that isn't an axis: an armed
prompt nobody can get out of is worse than one that gives up easily. Modifier
*releases* are ignored, or letting go of Alt would cancel it before it started.

## Hand-editing the result (`tweak.py`)

Sometimes the generators get a boundary wrong — a side whose neighbour could
not be matched, two boundaries that ended up one vertex apart, a merge that did
not take — and the fix is a few vertex moves and one extra edge. Every tool for
that already exists in Blender, and **all of them are Edit Mode operators**:
merge by distance, vertex snapping, knife, loop cut, connect-vertex-path. There
is no version of this that stays in Object Mode; an object-mode
reimplementation would be a worse knife and a worse snap, written twice.

So `Tab` from the `PATCH` phase hands the viewport over. `enter_tweak` selects
`<Source>_Retop`, makes it active, snapshots the tool settings and replaces
them with what manual retopology wants, and enters Edit Mode; the phase becomes
`TWEAK` and `_modal_tweak` passes **everything** through except the `Tab` that
ends the trip. Blender's own keys are the feature: `K` knife, `Ctrl+R` loop
cut, `J` connect, `G` move, `Ctrl+Tab` select mode. The addon owns only the two
ends of the trip, and each is there for a reason:

- **The setup.** Vertex snapping with `use_snap_self` **on** — the whole point
  is dragging a vertex onto its twin in the *same* mesh, which Blender's
  default (other objects only) makes impossible — plus auto-merge at
  `tweak_merge_distance`, so closing a seam is a drag rather than a drag
  followed by a Merge by Distance that gets forgotten once and leaves a crack
  nobody sees until export. `tweak_snap_surface` adds `FACE_NEAREST` so a
  dragged vertex stays on the CAD surface. Settings are read on the way **in**,
  so changing one mid-edit does nothing until the next trip; the panel says so.
- **The repair** (`mesh_build.repair_manual_edits`), because Blender knows
  nothing about this addon's attributes and the two it gets wrong are the two
  read back later. A knife cut leaves faces carrying `NO_PATCH` — the patch
  then reads as partly "never retopped" and a re-edit stacks a second grid on
  it — and vertices that *inherited* a neighbour's `retop_source_vid`, i.e.
  claim to be a CAD corner they are nowhere near, which the next commit would
  weld onto that corner by identity. Faces go back through
  `adopt_untracked_faces`; ids go through `clear_stray_source_ids`.

**A stray source id is decided three ways, and "it moved" is the weakest.**
Out of range for the source mesh is certain (an interpolated int between two
real ids is not an index). Two vertices naming the same source vertex is
certain too — one CAD corner is one result vertex — and the nearer one keeps
it. Distance alone only fires past `STRAY_SOURCE_ID_RATIO` of the model's
bounding box, deliberately generous: **nudging a corner by hand is what this
mode is for**, and stripping its identity for having moved a hair would undo
the fix on the next commit that touched it. Clearing is always the safe
direction — a vertex with no id welds by proximity like every other boundary
point, which is what a hand-placed vertex should do.

**The tool settings are the user's, and every exit path restores them**:
`restore_tool_settings` is called by a failed `enter_tweak`, by `exit_tweak`,
and by `end_session`. The snapshot lives on a scene property rather than a
module global so an addon reload mid-edit doesn't lose it. It is JSON, so sets
(`snap_elements`) and `bpy_prop_array`s (`mesh_select_mode`) round trip through
`_jsonable`; every key is read and written through `getattr`/`setattr`, since
`snap_elements` and friends have been renamed and split more than once across
Blender versions and a missing name must be skipped symmetrically in both
directions.

**From `PATCH` and from `OBJECT`, never from `ADJUST`, and that last one is
not a convenience.** A re-edit has the patch's faces *out* of the result mesh
with only a snapshot datablock to put them back, and anything written to a mesh
Blender holds in Edit Mode is discarded on exit — the patch would be gone for
good. That is the same rule `_leave_for_other_mode` already enforces for the
reverse direction, and `can_tweak` is where the key and the panel button both
read it, so they refuse with the same reason instead of one of them silently
doing nothing.

**In `OBJECT` the session holds no object, so the selection names one.**
`_session_source` resolves it the way `operators.resolve_session_object` does
(pointing at `<X>_Retop` means X), active object first. Two things then have to
survive the trip, because neither is derivable on the way back: which source it
was about — `repair_manual_edits`, the whole reason `Tab` is ours rather than
Blender's, needs it — and which phase it started from, since landing in `PATCH`
would claim the session had entered an object it never did
(`tweak_source_object` / `tweak_return_phase`).

**The trip has to be closed even when Tab didn't close it.** Leaving Edit Mode
by the mode dropdown, by a script or by an undo fires no event of its own, so
the `TWEAK` dispatch sits *before* the `TIMER` early-out in `_modal` and
`_modal_tweak` checks `context.mode` first: the repair runs once per trip
whichever way the trip ended. `Ctrl+Tab` is deliberately left to Blender (the
select-mode pie, used constantly while retopping), and the modal cursor is
*restored* rather than set in this phase — a cursor pinned on the window would
sit on top of the knife's own.

## Seeing the CAD structure (`cad_display.py`)

The bridge sends a triangle soup, so a Plasticity import reads as one
undifferentiated field of triangles even though the mesh records which triangle
belongs to which CAD face. Two overlays put that structure back; they make very
different kinds of claim and the distinction matters.

**Plasticity edges are exact.** A boundary half-edge `(a, b)` of one patch is
matched by `(b, a)` of the patch across it, so a B-rep edge is the maximal run
of boundary segments whose neighbouring face id does not change, and a B-rep
vertex is where it does — the same signal the topological corner test runs on.
`_edge_runs` splits a loop at those junctions. Each shared edge is walked by
*both* its faces, and the lower face id emits it: settled by comparison rather
than by remembering what has been seen, since a set of welded vertex indices is
a much larger thing to carry around than one `<`. An outer boundary (no face
across it) always emits. On an **open sheet** a whole free boundary is one run,
because nothing distinguishes its segments — that is a real limitation, and it
costs nothing visually since everything is drawn as segments anyway.

**Surface flow is derived, and the panel says so.** Plasticity's isoparametric
curves come from each face's NURBS parameterisation and *none of it crosses the
bridge* — the protocol carries no surface parameters at all. What is drawn
instead is the grid each face would be retopologized into: the same corner
split, the same generators, at a low span, reprojected through one shared BVH.
On a fillet or a swept face that lands very close to the true isoparms, because
both answer the same question about the same boundary. It is also the more
useful of the two here, being the topology the retopology would actually get.
A non-band annulus draws its outer loop only, for the same reason
`_generate_for_face` refuses to ring it.

Everything is cached on the same fingerprint as `patch_data.analyse`, per
`(product, face id)`. A draw handler runs on every redraw and may not walk a
mesh. Both displays are drawn as **one LINES batch each**, since a CAD part has
hundreds of edges and a draw call apiece is what turns an overlay into a
stutter.

**Whether they draw through the model is `cad_display_xray`, off by default.**
Through-the-mesh reads well on a flat layout and turns a curved or enclosed
part into a thicket, since the far side shows through the near one, so the
readable default is the honest one. `show_brep_vertices` is off by default for
the same kind of reason: on a real part every junction is a dot and a few
hundred of them bury the edges they punctuate.
Through is what makes a whole part's layout readable at a glance; off is what
makes a *curved or enclosed* one readable, because the far side stops showing
through the near side. The reason it was `depth_test_set('NONE')`
unconditionally still holds, though: the lines lie exactly *on* the surface
they describe, so depth-testing them against it is a coin flip per pixel and
they come out as a stipple. `_towards_viewer` is the answer — every point is
nudged along the view axis by `DEPTH_NUDGE` of its distance to the viewpoint,
proportional for the same reason the raycast's step past a hit is (a fixed
epsilon is either too small to clear the surface at range or big enough to lift
a line off a small part visibly). One view direction for the whole batch, not a
per-point eye vector: at that magnitude the difference at the edge of frame is
far below a pixel, and a draw handler has no business normalising a vector per
point. It reads the region's own matrix, so the handler still imports nothing
it didn't already.

The **B-rep vertex dots stay on top regardless**: they are screen-space quads
(POST_PIXEL), so there is no depth to test them against. Said in the property
description rather than left to be noticed.

`E` toggles the edges, `Ctrl+E` the flow, in every session phase: the structure
is read while *choosing* a surface as much as while adjusting one. Both are
remappable — see `keymap.py`.

## Reading the raw bridge data (the patch debug display)

Both displays above describe what the CAD model *means*. When a patch comes out
belonging to a face it visibly isn't, the question is one level below that:
what do `mesh["groups"]` and `mesh["face_ids"]` actually say?
`state.debug_patch_ids` writes each patch's face id over its own surface, with
the `[loop_start, loop_count]` range beside it, and
`operators.RETOP_OT_print_patch_data` dumps the whole table to the console —
a part has hundreds of faces, which is a console's job and not a sidebar's.

**It has its own draw handler, installed for the life of the addon.** The
session's overlay is not an option: the question is asked of a *freshly
imported* object, before any session exists. And the handler is not armed and
disarmed by the property's update callback either — a handler installed that
way is missing after a file load that restored the toggle as on, which looks
exactly like a display that does not work. It is installed once from
`operators.register` and its callback early-outs on the toggle, so the cost
when off is one `getattr` per redraw. `overlay.disable()` deliberately does
**not** take it with it (`tests/test_overlay.py` pins that): a debug display
that vanishes when the session ends is one that cannot be used to find out why
the session went wrong.

**Labelling every face at once is a wall of text**, so one patch is labelled at
a time and the one chosen is the one **under the cursor**
(`debug_patch_scope`, default `HOVER`). Selecting it with `L` was the first
answer and it is the worse one: the bridge never joins two faces'
tessellations, so select-linked does stop exactly on a patch border — but it
asks for a selection to be made before the question can be asked and unmade
afterwards, it destroys whatever selection was there, and in **Edit Mode**,
which is where a mesh is examined, it reads as nothing at all: Blender only
writes selection flags back to the mesh datablock on *leaving* edit mode, so the
one place the picker is natural is the one place its answer never arrives.
Hovering has none of that and works in either mode. `SELECTED` is kept for the
one case hovering cannot serve — several patches at once — and `ALL` for a small
part; the panel says why a selection reads as empty rather than looking broken.

**Hover needs a mouse position, and a draw handler is given no event.** So
`operators.RETOP_OT_patch_hover` is a modal that does nothing but raycast on
mouse moves and leave `(object name, face id)` in `overlay.debug_hover` — the
same arrangement the session's tooltip has with `overlay.cursor_window`, except
this one must run with **no session**, so it is its own modal rather than a line
in `_modal`. It returns `PASS_THROUGH` on every event, so it sits under the
knife, a loop cut, a transform or Blender's own selection without taking
anything from them, and it checks the toggle on every event rather than trusting
whoever turned it off. A module global and not a scene property: a property
written on every mouse move marks the file as modified, and looking at a mesh
must not. `operators.sync_patch_hover` starts it — from the property update, and
from `load_post`, since a modal does not survive a file load but the property
asking for one does, and a display frozen on the last thing the cursor touched
before the load looks exactly like one that has stopped working. Starting is
deferred through a timer: an update callback has a restricted context and
`INVOKE_DEFAULT` needs a window and a VIEW_3D area to register a handler on.
The hover is also **not culled by facing** — the cursor is on it, so it is in
front by construction, and the panel greys `debug_patch_cull` out under `HOVER`
rather than leave a checkbox that does nothing.

The Edit Mode half rests on two facts about Blender rather than on anything
here. `scene.ray_cast` **does** hit the object being edited, through its
evaluated cage, so the pick needs no special case. And `mesh.polygons` is
whatever it was on entering edit mode, so the labels describe the mesh as it was
at that moment — which is the right answer for a CAD import nobody edits, and
the only one available, since the datablock is what carries `groups`/`face_ids`
in the first place.

`patch_labels` is the whole mesh, always, keyed on the geometry alone: which of
them to draw is a filter over a few hundred entries and belongs at the point of
drawing, since scoping the cache would put a second, far more volatile key on it
while its expensive half — a scan of every polygon centre — does not depend on
the scope at all. `selected_face_ids` does need that second key, because
selection is not geometry and so does not move `mesh_fingerprint`:
`cad_display._selection_fingerprint` is a second CRC, `foreach_get` plus
`zlib.crc32`, the same budget the geometry one already spends.

**A label's anchor is a polygon centre, never the mean of the patch.** The mean
of a concave face is outside it and the mean of an annulus is in its hole,
which puts a label on a face it does not describe. The polygon nearest that
mean is on the surface by construction. Back-facing patches are culled by that
polygon's normal (`debug_patch_cull`) — the labels are screen-space text with
no depth to be tested against, which is what keeps them readable over the
surface and what would otherwise write the far side of a closed part over the
near side.

**`patch_data.group_report` is the check under all of it, and one of its
answers is reported to everyone.** `polygon_face_ids` walks the group ranges
positionally — it advances when a polygon's `loop_start` passes the current
range's end — which is correct exactly while those ranges still tile the loop
array *on polygon boundaries*. They do as imported, and stop doing so the
moment anything re-topologizes the mesh underneath them: a triangulate, a
decimate, a join, a modifier applied. Nothing raises when they don't. Polygons
are handed to whichever face the range they fall in happens to name, and the
answer that comes back is complete, plausible and wrong — every patch boundary
in the file, with nothing anywhere saying why. So the integrity half is drawn
by `ui._draw_group_integrity` with the other warnings, visible without
Developer Mode and only when something is actually broken — the same judgement
`show_cracks` makes. The inspector half stays behind Developer Mode: a loop
index is of no use to anyone who installed a release zip.

**Triangulation is reported, never required, and that is measured rather than
assumed.** The bridge offers an untriangulated export and nothing here cares:
`polygon_face_ids` works in loop-index space, and
`geometry.build_bvh_with_polygon_map` fan-triangulates whatever it is handed
(its own comment says the triangulated import makes that "normally a no-op").
So `GroupReport.triangulated` and the polygon-size histogram answer "what am I
looking at", and a quad export must come back with an **empty** problem list —
reporting it as a fault would send people re-exporting to fix something that
was never broken. `tests/test_patch_debug.py` pins that, and pins the
corruption case at both ends: the report names the mid-polygon boundary *and*
`polygon_face_ids` really does mis-assign on it, since a warning about a case
that does not actually go wrong is a warning worth removing.

A modulo-3 check on `loop_start` would catch some of this — in an all-triangle
mesh every range boundary is a multiple of three — but it is a proxy for the
real question and answers it only for one export setting. Checking the
boundaries against the polygons themselves is the same cost and holds either
way.

## Status

Implemented: Quad, Triangle, Wedge (2 sides), N-Side (5+, one Coons
sub-patch per side around a centre) -- all of them followed by the interior
relaxation that evens out their cells without touching their boundary -- Ring (two boundary loops: a face with a hole, or a tube-like
face — this is the Cylinder case), N-gon mode for flat faces, topological
corner detection, span propagation,
per-patch UVs, boundary welding, smooth shading with sharp patch borders,
Inbox collection mirroring, viewport session with overlay, re-selecting a
committed patch to change its spans (replaces it in place).

Also implemented: matching a committed neighbour along a shared side, by
pointing at it (`M`) or automatically, for every generator and confined to the
faces the side actually borders, with the side colours saying in three states
whether each one is matched, unmatched against a committed patch (red, the only
state that leaves a crack) or simply has nothing across it;
corner ranking, which keeps a quad a quad when the angle test
also flags a tessellated curve; the Plasticity edge / B-rep vertex / surface
flow overlay (`E`, `Ctrl`+`E`); the hand-edit round trip into Blender's Edit
Mode (`Tab` from `PATCH`), set up for retopology and repaired on the way back;
symmetry as a Mirror modifier planed on the source origin (`Alt+X` then an
axis, with an Apply that keeps re-editing safe); every key as a real
`KeyMapItem`, edited in Blender's own rows on the addon preferences page; and a
per-mesh cache under all of it, without which none of the above is affordable
on every hover.

Not implemented yet: **N-gon on a face with several holes** (the pipeline
truncates past two loops, so only one bridge pair is ever possible),
**Ring with corner matching** (the two loops are paired by
arc length, so a hole shaped very differently from the outer boundary distorts
the band, and spans don't propagate *into* a ring), faces with **more than one
hole** (outer loop only, panel warns), **Quad Fill** with configurable loop
cuts, **N-Side** with per-side spans and manual corner placement, quad-family
(solving a chain of connected quads in one click).

Also implemented: the **patch debug display** — each patch's face id and
`[loop_start, loop_count]` written over its own surface, following the cursor
in Object *and* Edit Mode, behind Developer Mode; and the **group
integrity check** under it, which is shown to everyone because group ranges
that have stopped lining up with the polygons make `polygon_face_ids` wrong
everywhere without raising anything.

Also implemented: **several matches on one N-Side patch**, since its sides no
longer share a span (`nside.spoke_allocation`); and **cracked borders**, the
standing report of a shared CAD edge two committed patches failed to close.

Also implemented: **matching one side of a multi-side ring**. A rim of one
cornerless side made "this loop carries a neighbour's vertices" and "this
*side* does" the same statement, since the loop's total and the match's count
were the same number — so `locked_loops` was all the band needed, and
`loop_point_count` read the whole rim. Cut that rim into several, by an isoparm
or by a corner the angle test found, and only some of its sides hold the
neighbour's points: the allocation then shared the loop's total out by *length*
and handed the matched side a count nobody asked for, resampling its vertices
off the ones they had to land on. `sidematch.applied_side_counts` carries
`{loop: {side: segments}}` beside `applied_loops`, and
`ring.allocate_segments` takes it as `pinned` — matched sides keep their exact
count, only the remainder is shared out. A pin set that cannot fit is dropped
**whole**, because half of it is the half-welded crack the allocation exists to
prevent and a band's two rims must come out with the same point count either
way. `tests/test_ring_multiside_match.py` pins it, and asserts the old
behaviour fails on the same geometry — otherwise the test proves nothing.

Known rough edge: a ring's spans still do not propagate *into* it, and its two
loops are paired by arc length, so a hole shaped very differently from the
outer boundary distorts the band.
