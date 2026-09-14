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
The Plasticity Retopo plugin should be able to handle small changes like these, but patches groups or mirroring might need to be rebuilt.

## 4. Repeat

Repeat #2 and #3 until the geometry produced by the addon is fixed.