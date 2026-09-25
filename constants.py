"""Names and small facts every layer of the addon has to agree on.

Generator names are compared as strings across modules, so a typo fails
silently. Always use these constants.

Imports nothing from the package, so `overlay` and `operators` can both use it.
"""

# --- generator names -------------------------------------------------------
#
# The value of `Generator.name` for each generator, and what
# `state.generator_name` holds while that generator is driving the preview.

QUAD = "Quad"
TRIANGLE = "Triangle"
WEDGE = "Wedge"
NSIDE = "N-Side"
RING = "Ring"
NGON = "N-gon"

# Generators driven by two spans: quad U/V, wedge along/across, ring
# around/across. Tab switches which one is adjusted.
# Every other generator has a single span.
TWO_SPAN_GENERATORS = frozenset({QUAD, WEDGE, RING})

# Panel labels for the two spans, per generator. Defaults to along/across.
SPAN_LABELS: dict[str, tuple[str, str]] = {
    QUAD: ("Span U", "Span V"),
    RING: ("Span (around)", "Span (across)"),
}
DEFAULT_SPAN_LABELS = ("Span (along)", "Span (across)")


def span_labels(generator_name: str) -> tuple[str, str]:
    return SPAN_LABELS.get(generator_name, DEFAULT_SPAN_LABELS)
