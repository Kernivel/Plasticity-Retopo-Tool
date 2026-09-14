# Your first patch

!!! Note "Prerequisites"

    Since this plugin relies on the Plasticity Bridge, you first need to import a mesh through it.
    Please visit https://doc.plasticity.xyz/blender/how-to-use to learn how to do that.

## 1. Start the session

Open the 3D view's N-panel, **Retop** tab, and press **Start Retop Session**.

!!! warning "Object Mode only"

    The plugin work in Object Mode, starting it from Edit Mode will lead to an error.

<video autoplay loop muted playsinline poster="../assets/img/start-session.jpg">
  <source src="../assets/video/start-session.webm" type="video/webm">
</video>

## 2. Pick an object

Click a Plasticity-imported mesh. 

- a result mesh called **`<Object>_Retop`** is created if it does not exist;

!!! Note "Two ways select"

    Selecting `<Object>_Retop` itself and starting a session works too — it resolves
    back to the source object.

## 3. Pick a surface

Hover the model. The patch under the cursor previews in orange, click to start working on it.

<video autoplay loop muted playsinline poster="../assets/img/hover-patches.jpg">
  <source src="../assets/video/hover-patches.webm" type="video/webm">
</video>

!!! tip "Turn the CAD edges on"

    Press <kbd>E</kbd> turns Plasticity's borders on - telling you where "patches" are.

## 4. Adjust

You are now in the **Adjust** phase. The patch is a live preview.
You can edit the spans in the U and V directions, select a neighbor to force a match,
or switch to N-Gon mode when working with flat faces.

| | |
|---|---|
| <kbd>Ctrl</kbd> + wheel | Density up/down |
| <kbd>0</kbd>–<kbd>9</kbd> | Type a span directly |
| <kbd>Tab</kbd> | Switch U/V (quad and wedge patches) |
| <kbd>N</kbd> | N-gon mode (flat faces) |
| <kbd>Esc</kbd> | Discard |


## 5. Commit

Right-click, or <kbd>Enter</kbd>, or left-click where no side is under the
cursor. The grid is welded into `<Object>_Retop` and you can pick up the next surface.

<!-- media: 20s. Commit four adjacent patches in a row without leaving the
     Adjust/Patch loop, so the "commit drops you back to picking" rhythm is
     visible. -->

## 6. Do the neighbor

Pick a neighboring face and start again. Matching should apply when possible,
so the two patches share their boundary points exactly.

See [side matching](guide/matching.md) (on by default).

## Editing a previous patch

Click on an already committed patch to edit it.

- **Commit** keeps the new version.
- **Discard** (or <kbd>Esc</kbd>, or leaving the object, or ending the session)
  puts the old patch back.
- <kbd>X</kbd> deletes it.

!!! tip "Leaving the session"

    <kbd>Esc</kbd> goes back one level each press: **Adjust** → **Patch** →
    **Object** → session ends.
