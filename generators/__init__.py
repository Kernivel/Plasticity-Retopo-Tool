from . import base
from . import quad
from . import triangle
from . import wedge
from . import nside
from . import ring
from . import ngon

# Order matters: the first generator whose matches() accepts the side count
# wins, so the specialised ones come before the general N-Side fallback.
GENERATORS: list[base.Generator] = [
    wedge.WedgeGenerator(),
    triangle.TriangleGenerator(),
    quad.QuadGenerator(),
    nside.NSideGenerator(),
]

# Not in GENERATORS: a ring is chosen by its patch having two boundary loops,
# not by a side count.
RING: ring.RingGenerator = ring.RingGenerator()

# Not in GENERATORS either: N-gon is a mode the user toggles.
NGON: ngon.NgonGenerator = ngon.NgonGenerator()


def find_generator(num_sides: int) -> base.Generator | None:
    for gen in GENERATORS:
        if gen.matches(num_sides):
            return gen
    return None
