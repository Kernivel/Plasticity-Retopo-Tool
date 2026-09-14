# Symmetry

<kbd>Alt</kbd>+<kbd>X</kbd>, then <kbd>X</kbd>, <kbd>Y</kbd> or <kbd>Z</kbd>.

The retopology is mirrored across that axis of the **source object's** origin.
Press the same axis again to turn it off; the three are cumulative. The
**Output** tab shows which are on and lets you click them directly.

## Added as a modifier

The mirror lives in the modifier stack by default,
it can be commited right away howerver using the <kbd></kbd> toggle (Need to be implemented).

## Applying it

When the retopology is done, use the panel's **Apply Mirror** button rather than
the modifier dropdown.

The plugins needs to assign them id's again before they can be used by it.
A standart "Apply" from the modifier panel would break this, as new surfaces would copy their old ids.
