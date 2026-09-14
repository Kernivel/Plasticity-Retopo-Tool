# Plasticity Retop

A Blender addon for patch-based retopology of CAD meshes imported through the
Plasticity ↔ Blender bridge addon [plasticity-blender-addon](https://github.com/nkallen/plasticity-blender-addon).

**📖 [Documentation](https://kernivel.github.io/Plasticty-Retopo-Tool/)** — built from
`docs/` by `.github/workflows/docs.yml`. Nothing in `docs/` ships with the addon.

## Overview

A session from picking an object to committed patches.

<video src="https://github.com/Kernivel/Plasticty-Retopo-Tool/raw/main/docs/assets/video/overview.webm" poster="https://github.com/Kernivel/Plasticty-Retopo-Tool/raw/main/docs/assets/img/overview.jpg" controls muted loop playsinline width="100%">
  <a href="https://github.com/Kernivel/Plasticty-Retopo-Tool/raw/main/docs/assets/video/overview.webm">Watch the overview clip</a>
</video>

Retopologizing a pistol grip end to end.

<video src="https://github.com/Kernivel/Plasticty-Retopo-Tool/raw/main/docs/assets/video/grip-overview.webm" poster="https://github.com/Kernivel/Plasticty-Retopo-Tool/raw/main/docs/assets/img/grip-overview.jpg" controls muted loop playsinline width="100%">
  <a href="https://github.com/Kernivel/Plasticty-Retopo-Tool/raw/main/docs/assets/video/grip-overview.webm">Watch the grip clip</a>
</video>

## Installing

Blender 4.2 or newer (developed against 5.1). Download the `.zip` from the
[latest release](https://github.com/Kernivel/Plasticty-Retopo-Tool/releases),
drag it into Blender — or `Edit > Preferences > Add-ons > Install from Disk` —
and enable *Plasticity Retop*. Updating means installing the newer zip over it.

## Main features

### Matching a neighbour

Weld neighboor patches together.

## Hand tweaks

Intuitive handling using Plasticity workflow when retopologizing and easy access to Blender bases edit tools to correct the topology mid-session.