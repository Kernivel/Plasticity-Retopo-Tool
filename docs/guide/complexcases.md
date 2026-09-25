# Working with complex cases

When using the addon, you might encounter surfaces with geometry that containes, holes, rings and curved surfaces in a 
single surface.

This addon creates patches by finding the closest primitive to the selected surface.
If the surface it too complex, the closest primitive might not be good enough.

In this case, you will need to reduce the complexity of the surface directly in Plasticity before bridging it back to Blender.


## 1. Open Plasticity
Go to the corresponding file and surface that failed creating good topology.

## 2. Knife & Isoparam cuts
Find the complex surface and use Plasticity's knife tool (<kbd>Shift+A</kbd> and then <kdb>K</kbd> by default)
to split the surface into smaller ones.

In some case the Isoparam cuts (<kbd>Ctrl+R</kbd> by default) can work just like loop cuts or control loops in Blender and produce better results.

## 3. Save & Refresh

Save your changes in the Plasticity file then in Blender use the Refresh button from the Plasticity Bridge addon to refresh the mesh.

A Refresh can give the part's faces new ids, even when you changed nothing. The addon checks its committed patches against the surface they sit on whenever you start a session on the object, so they are still recognised and re-editing replaces them instead of stacking a new grid on top.

A patch built from several surfaces is found again the same way. If the surfaces under it were really split or merged, the panel says so and you pick them again. Mirroring might also need to be set up again.

## 4. Repeat

Repeat #2 and #3 until the geometry produced by the addon is fixed.