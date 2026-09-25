# The N-panel

`View3D > N-panel > Retop`.

The top block is always there — session state, the active patch, the generator
that ran, warnings — and below it an icon row of six tabs.

<!-- media: screenshot of the panel with a patch open, all six tab icons
     visible. 2x scale, cropped to the panel. -->

## Patch

Corner detection, density, and boundary welding.

| Setting | Default | |
|---|---|---|
| **Resolution** | Mid | Scales the span count a new patch starts at: Very Low ¼ · Low ½ · Mid 1 · High 2 · Extreme 4. Only the starting point — a neighbour's span still wins |
| **Span U / V / Span** | 4 | The live density of the open patch |
| **Corners (Grid)** | Angle | *Developer Mode only.* How a boundary is split into sides for the span generators. See [How it works](../how-it-works/generators.md#corners-then-sides) |
| **Corners (N-gon)** | Both | *Developer Mode only.* The same question for [n-gon mode](../guide/ngon.md) |
| **Corner Angle Threshold** | 135° | Boundary turns sharper than this are corners |
| **Small Side Tolerance** | 0 | Merge a side shorter than this into the next one, so a tessellation sliver does not cost a whole extra side. 0 = never |
| **Reproject** | on | Snap interior grid vertices onto the CAD surface |
| **Boundary Weld Distance** | 0.0001 | How close two boundary points must be to weld. Only boundary-flagged vertices are considered |
| **N-gon Detail Angle** | 20° | Turn accumulated between kept boundary vertices |
| **N-gon Flatness Tolerance** | 5° | How far from flat a patch may be and still take an n-gon |

## Picker

How surfaces and sides are picked in the viewport.

| Setting | Default | |
|---|---|---|
| **Match Committed Neighbours** | on | Apply [side matching](../guide/matching.md) automatically |
| **Match Margin** | 2% | Extra reach for a side you *pointed at*. Automatic matching never uses it |
| **Pick Depth Tolerance** | 0 | Hover hysteresis — how much closer a new patch must be to take over |
| **Pick Max Distance** | 0 | 0 = unlimited |
| **Auto Merge** | on | Weld on drop while hand-editing |
| **Auto-Merge Distance** | 0.001 | |
| **Snap to CAD Surface** | on | Face Nearest snapping during the hand-edit trip |
| **Length Unit** | | The unit the distance fields above are read in |

## Display

Preview and result appearance, the CAD overlays, isolate behaviour.

| Setting | Default | |
|---|---|---|
| **Preview Color / Alpha** | orange, 0.6 | |
| **Result Color / Alpha** | blue, 1.0 | |
| **Offset** | 0 | How far the committed result is pushed off the CAD surface. The preview takes the same measure times a small margin, so a hovered patch never sits *under* a committed neighbour |
| **See Retopo Through Meshes** | on | <kbd>V</kbd>. Turning it **off** is the only way to check the retopology sits on the surface rather than floating off it |
| **Show Wireframe** | on | Scoped to in-session result meshes |
| **Wireframe Opacity** | 0.5 | Blender has no per-object wireframe opacity, so this writes to every 3D viewport — the panel says so |
| **Show All Retopo** | on | Draw other objects' retopology too |
| **Other Retopo Alpha** | 0.25 | |
| **Show N-gon Vertices** | on | |
| **Vertex Dot Size** | 11 | |
| **Keep Retopo in Isolate** | on | <kbd>/</kbd> pulls the preview and the result in. Off makes <kbd>/</kbd> behave exactly like stock Blender |
| **Keybind Overlay Size** | 1.0 | The hints are drawn in pixels, so they shrink on a 4K screen and crowd a small one |

Plus the [CAD structure](../guide/cad-structure.md) block: **Show CAD Edges**,
**Show CAD Vertices**, **Show Surface Flow**, **Flow Density**, **Show For**,
**Draw Through the Mesh**, and the two colours.

## Output

The committed mesh: shading, symmetry, collections.

| Setting | Default | |
|---|---|---|
| **Shade Smooth** | on | |
| **Sharp Edge Angle** | 30° | An edge is creased only when its two faces belong to **different patches** *and* their normals differ by more than this. One patch is one CAD surface, so an angle-only rule would crease a curved patch's own interior |
| **Mirror axes** | — | Which of X/Y/Z the [Mirror modifier](../guide/symmetry.md) uses. Stored on the modifier, not the scene |
| **Clip at the Plane** | on | |
| **Mirror Merge Distance** | 0.001 | |
| **Apply Mirror** | — | Bakes it, keeping re-editing safe |
| **Mirror Inbox Collections** | on | File `<Object>_Retop` under a `Retop` tree mirroring the bridge's Inbox hierarchy |

Shading is re-applied after **every** commit and every delete, not just the
first: sharpness is a property of the border *between* patches, so a new
neighbour changes the shading of an edge that already existed.

Edges you mark or clear sharp yourself are kept. Re-shading only changes the
edges you have not touched. **Reset Hand-Set Sharp Edges** forgets your
changes, so every crease follows the angle again.

## Keybinds

A button that opens the addon preferences page, where Blender's own keymap rows
edit every binding, plus a read-only list of what is
[not remappable](keymap.md#not-remappable).

## System

The version and build string — and, with **Developer Mode** on, the on-disk
comparison and **Reload Addon Only**.

Developer Mode lives in the addon's own preferences
(`Preferences > Add-ons > Plasticity Retop`) and is off by default. Installed
from a release zip there is nothing to reload against: one copy of the code,
replaced when you install the next zip. The rest of this section is the
development loop.

!!! danger "Read the red line first"

    The panel compares the constants Python holds **in memory** against what
    `version.py` reads **on disk**, and says so in red when they differ. That is
    the "I deployed and nothing changed" state, and its tracebacks cite line
    numbers that do not match the file you are reading. Diagnose it before
    anything else.

Use **Reload Addon Only** rather than Blender's global *Reload Scripts*: the
latter can quietly half-fail when another installed addon errors during its own
reload.

## Warnings you may see

| | |
|---|---|
| *Corners look uniform* | Every boundary vertex bends the same and every one is flagged — a coarsely tessellated circle and a real octagon are the same polyline. Raise the corner threshold |
| *N-gon unavailable: not flat* | The patch is a bevel or a fillet; one face would be a flat lid over it |
| *N boundary loops* | More than two. A flat face is filled as an [n-gon](../guide/ngon.md) so its holes survive; one that is not flat gets its outer boundary alone, and the warning names the reason |
| *N matches outvoted* | Two sides wanted different counts along the same span. See [matching](../guide/matching.md#a-grid-cannot-honour-two-counts-in-one-direction) |
| *Orphan result object* | A `<X>_Retop` whose source no longer exists — usually a rename or a re-import |
| *Session active with no modal* | A reload or a crash left the state set. The panel offers a reset |
