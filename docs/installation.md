# Installation

## Requirements

| |                                                                                                        |
|---|--------------------------------------------------------------------------------------------------------|
| **Blender** | 4.2 or newer (developed against 5.1)                                                                   |
| **Plasticity** | Only to *import* a model                                                                               |
| **Bridge** | [plasticity-blender-addon](https://github.com/nkallen/plasticity-blender-addon), also import-time only |

!!! warning "A mesh not imported through the bridge has no patches"

    Modelled-in-Blender geometry, an STL, an OBJ — none of them carry the
    Plasticity face ids, so there is nothing to divide into patches. The session
    says so rather than silently offering you a single patch.

## Installing the addon

Download the `.zip` from the
[latest release](https://github.com/Kernivel/Plasticity-Retopo-Tool/releases)
and **drag it into Blender**, or use `Edit > Preferences > Add-ons >
Install from Disk`. Then enable **Plasticity Retop** in the add-ons list.

To update, install the newer zip over it.