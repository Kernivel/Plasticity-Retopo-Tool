# Plasticity Retop

A Blender addon for patch-based retopology of CAD meshes imported through the
Plasticity ↔ Blender bridge addon [plasticity-blender-addon](https://github.com/nkallen/plasticity-blender-addon).

**📖 [Documentation](https://kernivel.github.io/Plasticity-Retopo-Tool/)** — built from
`docs/` by `.github/workflows/docs.yml`. Nothing in `docs/` ships with the addon.

## Overview

A session from picking an object to committed patches.

[overview.webm](https://github.com/user-attachments/assets/00b3cc60-ad3b-4466-b679-9fbfd8d9d142)

Retopologizing a pistol grip end to end.

[grip-overview.webm](https://github.com/user-attachments/assets/637f5781-3d9d-4ee0-8ab5-7074a99d497f)

## Installing

Blender 4.2 or newer (developed against 5.1). Download the `.zip` from the
[latest release](https://github.com/Kernivel/Plasticity-Retopo-Tool/releases),
drag it into Blender — or `Edit > Preferences > Add-ons > Install from Disk` —
and enable *Plasticity Retop*. Updating means installing the newer zip over it.

## Main features

### Intuitive

Intuitive view of the retopology with CAD edges, Isolate and overlaying options.

### Matching a neighbour

Weld neighboor patches together.

### Plasticity like controls

Intuitive handling using Plasticity workflow when retopologizing.

### Easy access to edit mode

Easy access to Blender bases edit tools to correct the topology mid-session.

### Multi Select

Group surfaces together to create a single patch

### N gon Flat faces

For patching faces that ate complex with holes but don't require spanning.

### Edge flow channel

Set up the grouping of edges used when creating the patch to control the edge flow.


