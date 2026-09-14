"""Run inside Blender: blender --background --python tests/test_fixtures.py

Retopologize real bridge output and pin what comes out.

Every other test in this suite builds its mesh from the same mental model as
the code, which means none of them can catch "the bridge emits X and we
assumed Y". These fixtures were made in Plasticity and imported through the
bridge, so they are the only place that assumption is actually tested. See
tests/fixtures/README.md for what each shape is for.

Three kinds of assertion, deliberately separated:

- **invariants** -- true of any correct retopology, whatever the tuning
  constants say. A planar face must be reproduced exactly; nothing may come
  out non-manifold. These are the ones worth being strict about.
- **golden values** -- what the pipeline happens to do today. They exist to
  make a change *visible*, not because the numbers are sacred: a tweak to
  CORNER_CLIFF that silently re-routes every bevel is the failure this
  catches. Regenerate with scripts/gen_expectations.py and read the diff;
  never hand-edit. Note that the vertex and face counts are **order
  sensitive** -- this test commits in the fixture's own face order, and span
  propagation seeds everything downstream from whichever patch commits first,
  so the same shapes in a different order legitimately produce a different
  count. Generator and side count per face are the entries that should hold
  steady.
- **known gaps** -- things that are wrong today and recorded as wrong. These
  fail when they start *working*, so that fixing one forces the expectations
  here to be updated rather than leaving a test that quietly asserts a bug.

Deviation figures are a percentage of each object's bounding-box diagonal,
measured across face interiors -- never at vertices, which the generators put
on the surface by construction. The measurement code is imported from
scripts/benchmark.py so the suite and the by-hand tool cannot drift apart.
"""
import os
import sys

_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_ADDON_DIR))
sys.path.insert(0, os.path.join(_ADDON_DIR, "scripts"))

import bpy

FIXTURE = os.path.join(_ADDON_DIR, "tests", "fixtures", "TestCases.blend")

FAILURES = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")
    if not cond:
        FAILURES.append(name)


def known_gap(name, still_broken, extra=""):
    """Record something that is wrong today, and fail when it stops being."""
    if still_broken:
        print(f"[GAP ] {name} -- still open, as recorded {extra}")
        return
    print(f"[FAIL] {name} -- THIS NOW WORKS. Update the expectations in "
          f"tests/test_fixtures.py (and tests/fixtures/README.md).")
    FAILURES.append(f"{name} (fixed -- expectations are stale)")


if not os.path.isfile(FIXTURE):
    print(f"[FAIL] fixture missing: {FIXTURE}")
    sys.exit(1)

# Load the fixture ourselves rather than have run_tests.py pass it on the
# command line: the runner invokes every test the same way, and a test that
# needed special treatment would be a test nobody remembers to run.
bpy.ops.wm.open_mainfile(filepath=FIXTURE)

import importlib

pr = importlib.import_module(os.path.basename(_ADDON_DIR))
import benchmark  # scripts/benchmark.py -- measurement helpers only

try:
    pr.unregister()
except Exception:
    pass
pr.register()


# ===========================================================================
# Golden values, at MID resolution.
#
# Regenerate with scripts/gen_expectations.py, never by hand. The face ids are
# Plasticity's own, and a re-export renumbers every one of them even when the
# geometry is untouched -- which is exactly what happened when the placement
# variants were added, and is why a stale table makes "the fixture changed"
# indistinguishable from "the code regressed".
# ===========================================================================

RESOLUTION = 'MID'

EXPECTED = {
    "Carved Rounded Slot": dict(
        source_tris=156, patches=11,
        result_verts=172, result_faces=131,
        faces=[
            (1737, "Quad", 4, 1),
            (1738, "Quad", 4, 1),
            (1739, "Quad", 4, 1),
            (1742, "Ring", 5, 2),
            (1743, "Quad", 4, 1),
            (1748, "Quad", 4, 1),
            (1750, "Quad", 4, 1),
            (1752, "Quad", 4, 1),
            (1754, "Quad", 4, 1),
            (1756, "Quad", 4, 1),
            (1777, "Quad", 4, 1),
        ],
        max_deviation_pct=0.09,  # measured 0.0690%
        open_edges=90,
    ),
    "Cone": dict(
        source_tris=188, patches=2,
        result_verts=614, result_faces=612,
        faces=[
            (1402, "Quad", 4, 1),
            (1407, "Quad", 4, 1),
        ],
        max_deviation_pct=14.0,  # measured 10.4833%
        open_edges=0,
    ),
    "Cube Bevel Edges": dict(
        source_tris=1556, patches=13,
        result_verts=1035, result_faces=982,
        faces=[
            (1997, "Quad", 4, 1),
            (1998, "Quad", 4, 1),
            (1999, "Quad", 4, 1),
            (2002, "Quad", 4, 1),
            (2003, "Quad", 4, 1),
            (2008, "Wedge", 2, 1),
            (2010, "Quad", 4, 1),
            (2012, "Quad", 4, 1),
            (2014, "Wedge", 2, 1),
            (2017, "Quad", 4, 1),
            (2019, "Triangle", 3, 1),
            (2021, "Quad", 4, 1),
            (2022, "Triangle", 3, 1),
        ],
        max_deviation_pct=0.57,  # measured 0.4331%
        open_edges=118,
    ),
    "Cube Chamfer Edges": dict(
        source_tris=20, patches=9,
        result_verts=34, result_faces=31,
        faces=[
            (199, "Quad", 4, 1),
            (200, "Quad", 4, 1),
            (201, "Quad", 4, 1),
            (203, "Quad", 4, 1),
            (204, "Quad", 4, 1),
            (210, "Quad", 4, 1),
            (216, "Quad", 4, 1),
            (218, "Quad", 4, 1),
            (221, "N-Side", 5, 1),
        ],
        max_deviation_pct=9.6e-05,  # measured 0.0001%
        open_edges=6,
    ),
    "Cube Two Booleans": dict(
        source_tris=493, patches=15,
        result_verts=2045, result_faces=1947,
        faces=[
            (2167, "Quad", 4, 1),
            (2256, "Quad", 4, 1),
            (2257, "Quad", 4, 1),
            (2258, "Quad", 4, 1),
            (2261, "Quad", 4, 1),
            (2262, "Ring", 2, 2),
            (2263, "Quad", 4, 1),
            (2268, "Ring", 5, 2),
            (2269, "N-Side", 8, 1),
            (2273, "N-Side", 8, 1),
            (2274, "Quad", 4, 1),
            (2275, "Quad", 4, 1),
            (2276, "Ring", 5, 2),
            (2279, "N-Side", 6, 1),
            (2281, "N-Side", 6, 1),
        ],
        max_deviation_pct=15.0,  # measured 11.1111%
        open_edges=244,
    ),
    "Cylinder": dict(
        source_tris=320, patches=3,
        result_verts=1592, result_faces=1590,
        faces=[
            (260, "Quad", 4, 1),
            (261, "Quad", 4, 1),
            (264, "Ring", 2, 2),
        ],
        max_deviation_pct=0.079,  # measured 0.0602%
        open_edges=0,
    ),
    "Flat Loop": dict(
        source_tris=622, patches=4,
        result_verts=232, result_faces=232,
        faces=[
            (1820, "Ring", 2, 2),
            (1821, "Ring", 2, 2),
            (1825, "Ring", 2, 2),
            (1826, "Ring", 2, 2),
        ],
        max_deviation_pct=0.11,  # measured 0.0808%
        open_edges=0,
    ),
    "Intersecting Curves": dict(
        source_tris=210, patches=15,
        result_verts=634, result_faces=608,
        faces=[
            (120266, "Quad", 4, 1),
            (120340, "Quad", 4, 1),
            (120902, "Triangle", 3, 1),
            (121351, "Triangle", 3, 1),
            (161700, "Quad", 4, 1),
            (161716, "Quad", 4, 1),
            (161720, "Quad", 4, 1),
            (161724, "Quad", 4, 1),
            (161728, "Quad", 4, 1),
            (171107, "Quad", 4, 1),
            (174822, "Quad", 4, 1),
            (175283, "Quad", 4, 1),
            (176574, "Triangle", 3, 1),
            (177080, "Quad", 4, 1),
            (177679, "Triangle", 3, 1),
        ],
        max_deviation_pct=3.5,  # measured 2.6315%
        open_edges=98,
    ),
    "Loopsided Chamfers Cube": dict(
        source_tris=28, patches=14,
        result_verts=34, result_faces=32,
        faces=[
            (1347, "Quad", 4, 1),
            (1348, "Quad", 4, 1),
            (1354, "Quad", 4, 1),
            (1357, "Quad", 4, 1),
            (1360, "Quad", 4, 1),
            (1363, "Quad", 4, 1),
            (1366, "Quad", 4, 1),
            (1369, "Quad", 4, 1),
            (1372, "Quad", 4, 1),
            (1373, "Quad", 4, 1),
            (1376, "Quad", 4, 1),
            (1377, "Quad", 4, 1),
            (1381, "Quad", 4, 1),
            (1383, "Quad", 4, 1),
        ],
        max_deviation_pct=0.00018,  # measured 0.0001%
        open_edges=0,
    ),
    "Non Cordal Bevels": dict(
        source_tris=336, patches=18,
        result_verts=42, result_faces=32,
        faces=[
            (1255780, "Quad", 4, 1),
            (1255796, "Quad", 4, 1),
            (1255800, "Quad", 4, 1),
            (1255804, "Quad", 4, 1),
            (1255808, "Quad", 4, 1),
            (1397230, "Quad", 4, 1),
            (1397293, "Quad", 4, 1),
            (1397297, "Quad", 4, 1),
            (1397301, "Quad", 4, 1),
            (1397305, "Quad", 4, 1),
            (1447976, "Quad", 4, 1),
            (1448009, "Quad", 4, 1),
            (1448043, "Quad", 4, 1),
            (1448076, "Quad", 4, 1),
            (1448110, "Quad", 4, 1),
            (1448143, "Quad", 4, 1),
            (1448177, "Quad", 4, 1),
            (1448210, "Quad", 4, 1),
        ],
        max_deviation_pct=2.5,  # measured 1.8776%
        open_edges=16,
    ),
    "Plate": dict(
        source_tris=622, patches=5,
        result_verts=775, result_faces=657,
        faces=[
            (291, "Quad", 4, 1),
            (292, "Ring", 2, 2),
            (295, "Ring", 2, 2),
            (296, "Quad", 4, 1),
            (300, "Ring", 2, 2),
        ],
        max_deviation_pct=0.11,  # measured 0.0770%
        open_edges=232,
    ),
    "Plate And Cylinder": dict(
        source_tris=2803, patches=14,
        result_verts=2229, result_faces=2139,
        faces=[
            (1502, "Ring", 2, 2),
            (1503, "Quad", 4, 1),
            (1506, "Quad", 4, 1),
            (1507, "Quad", 4, 1),
            (1512, "Ring", 5, 2),
            (1513, "Quad", 4, 1),
            (1514, "Quad", 4, 1),
            (1544, "Ring", 8, 2),
            (1545, "Quad", 4, 1),
            (1546, "Quad", 4, 1),
            (1552, "Quad", 4, 1),
            (1554, "Quad", 4, 1),
            (1558, "Quad", 4, 1),
            (1605, "Ring", 2, 2),
        ],
        max_deviation_pct=0.19,  # measured 0.1399%
        open_edges=184,
    ),
    "Shape with holes": dict(
        source_tris=616, patches=17,
        result_verts=243, result_faces=200,
        faces=[
            (546, "N-gon", 15, 5),
            (547, "N-gon", 15, 5),
            (550, "Quad", 4, 1),
            (551, "Quad", 4, 1),
            (555, "Quad", 4, 1),
            (557, "Quad", 4, 1),
            (559, "Quad", 4, 1),
            (561, "Quad", 4, 1),
            (563, "Ring", 2, 2),
            (565, "Ring", 2, 2),
            (567, "Quad", 4, 1),
            (569, "Quad", 4, 1),
            (571, "Quad", 4, 1),
            (572, "Quad", 4, 1),
            (573, "Quad", 4, 1),
            (577, "Quad", 4, 1),
            (579, "Quad", 4, 1),
        ],
        max_deviation_pct=12.0,  # measured 8.9746%
        open_edges=0,
    ),
    "Sphere": dict(
        source_tris=12320, patches=1,
        result_verts=0, result_faces=0,
        faces=[
            (322, None, None, 0),
        ],
        max_deviation_pct=None,
        open_edges=None,
    ),
    "Square Plate Small Hole": dict(
        source_tris=28, patches=11,
        result_verts=16, result_faces=14,
        faces=[
            (763, "Quad", 4, 1),
            (764, "Quad", 4, 1),
            (765, "Quad", 4, 1),
            (768, "Quad", 4, 1),
            (769, "Quad", 4, 1),
            (774, "Quad", 4, 1),
            (776, "Quad", 4, 1),
            (778, "Quad", 4, 1),
            (780, "Quad", 4, 1),
            (782, "Quad", 4, 1),
            (784, "Ring", 8, 2),
        ],
        max_deviation_pct=9.4e-05,  # measured 0.0001%
        open_edges=0,
    ),
    "Square Plate Small Hole Far Away": dict(
        source_tris=28, patches=11,
        result_verts=16, result_faces=14,
        faces=[
            (1117, "Quad", 4, 1),
            (1118, "Quad", 4, 1),
            (1119, "Quad", 4, 1),
            (1122, "Quad", 4, 1),
            (1123, "Quad", 4, 1),
            (1128, "Quad", 4, 1),
            (1130, "Quad", 4, 1),
            (1132, "Quad", 4, 1),
            (1134, "Quad", 4, 1),
            (1136, "Quad", 4, 1),
            (1138, "Ring", 8, 2),
        ],
        max_deviation_pct=0.00088,  # measured 0.0007%
        open_edges=0,
    ),
    "Square Plate Small Hole Scaled Down": dict(
        source_tris=28, patches=11,
        result_verts=12, result_faces=10,
        faces=[
            (940, "Quad", 4, 1),
            (941, "Quad", 4, 1),
            (942, "Quad", 4, 1),
            (945, "Quad", 4, 1),
            (946, "Quad", 4, 1),
            (951, "Quad", 4, 1),
            (953, "Quad", 4, 1),
            (955, "Quad", 4, 1),
            (957, "Quad", 4, 1),
            (959, "Quad", 4, 1),
            (961, "Ring", 8, 2),
        ],
        max_deviation_pct=0.086,  # measured 0.0661%
        open_edges=0,
    ),
    "Stairs Beveled": dict(
        source_tris=1392, patches=31,
        result_verts=202, result_faces=158,
        faces=[
            (294878, "Quad", 4, 1),
            (294925, "Wedge", 2, 1),
            (294941, "Quad", 4, 1),
            (294945, "N-Side", 5, 1),
            (294949, "Wedge", 2, 1),
            (387975, "Quad", 4, 1),
            (388043, "Quad", 4, 1),
            (433291, "Quad", 4, 1),
            (433361, "Quad", 4, 1),
            (439260, "Quad", 4, 1),
            (439290, "Quad", 4, 1),
            (439320, "Quad", 4, 1),
            (507154, "Quad", 4, 1),
            (507177, "Quad", 4, 1),
            (507197, "Quad", 4, 1),
            (507215, "Quad", 4, 1),
            (507233, "Quad", 4, 1),
            (507251, "Quad", 4, 1),
            (507269, "Quad", 4, 1),
            (507287, "Quad", 4, 1),
            (507305, "Quad", 4, 1),
            (508970, "N-Side", 5, 1),
            (508983, "Quad", 4, 1),
            (508985, "Quad", 4, 1),
            (508987, "Quad", 4, 1),
            (508989, "Quad", 4, 1),
            (508991, "Quad", 4, 1),
            (508993, "Quad", 4, 1),
            (508995, "Quad", 4, 1),
            (508997, "Quad", 4, 1),
            (508998, "Quad", 4, 1),
        ],
        max_deviation_pct=1.2,  # measured 0.8616%
        open_edges=112,
    ),
    "Stairs Curves and Bevel": dict(
        source_tris=5971, patches=13,
        result_verts=8957, result_faces=8747,
        faces=[
            (1615999, "Triangle", 3, 1),
            (1616875, "Quad", 4, 1),
            (1616877, "Quad", 4, 1),
            (1616914, "Triangle", 3, 1),
            (1617186, "Quad", 4, 1),
            (1627638, "Quad", 4, 1),
            (1627662, "Quad", 4, 1),
            (1627683, "Triangle", 3, 1),
            (1627698, "Triangle", 3, 1),
            (1627711, "Quad", 4, 1),
            (1627728, "Quad", 4, 1),
            (1627752, "Quad", 4, 1),
            (1627776, "Quad", 4, 1),
        ],
        max_deviation_pct=2.2,  # measured 1.6727%
        open_edges=546,
    ),
    "Torus": dict(
        source_tris=27126, patches=1,
        result_verts=0, result_faces=0,
        faces=[
            (334, None, None, 0),
        ],
        max_deviation_pct=None,
        open_edges=None,
    ),
    "Truncated Cone": dict(
        source_tris=374, patches=3,
        result_verts=1664, result_faces=1662,
        faces=[
            (2040, "Quad", 4, 1),
            (2041, "Quad", 4, 1),
            (2042, "Ring", 2, 2),
        ],
        max_deviation_pct=0.054,  # measured 0.0413%
        open_edges=0,
    ),
}

# Faces whose every polygon is coplanar, so the retopology has to reproduce
# them to floating-point exactness at any span. The sharpest assertion here.
#
# "Square Plate Small Hole Far Away" is deliberately *not* in this set even
# though it is the same flat shape as the one at the origin: at coordinates
# around 100 its float32 noise measures ~7x what the same part shows at the
# origin (0.0007% against 0.0001%), which is the entire point of having it. It
# gets a recorded ceiling instead, and the placement gap below carries the
# rest of the story.
PLANAR_OBJECTS = {
    "Cube Chamfer Edges",
    "Loopsided Chamfers Cube",
    "Square Plate Small Hole",
}

# What "exactly" can mean against float32 vertex storage. See the check below.
PLANAR_EXACT_PCT = 1e-3

# One CAD shape, three placements: at the origin, out at (50, 100, 20), and
# scaled to a hundredth. See the known gap at the bottom.
PLACEMENT_VARIANTS = (
    "Square Plate Small Hole",
    "Square Plate Small Hole Far Away",
    "Square Plate Small Hole Scaled Down",
)


# ===========================================================================
# Run
# ===========================================================================

context = bpy.context
present = {obj.name for obj in bpy.data.objects
           if obj.type == 'MESH' and obj.data.get("face_ids")}
check("the fixture holds every expected object",
      set(EXPECTED) <= present, sorted(set(EXPECTED) - present))
# The other direction matters just as much: a shape added to the .blend
# that nobody wrote an expectation for is a shape nothing is testing.
check("and the test knows about every object in the fixture",
      present <= set(EXPECTED), sorted(present - set(EXPECTED)))

results = {}
for name in sorted(EXPECTED):
    obj = bpy.data.objects.get(name)
    if obj is None:
        continue
    records = benchmark.retopologize(context, obj, RESOLUTION)
    results[name] = records

for name in sorted(EXPECTED):
    expected = EXPECTED[name]
    obj = bpy.data.objects.get(name)
    records = results.get(name)
    if obj is None or records is None:
        continue
    print(f"\n--- {name}")

    check(f"{name}: source triangle count is unchanged",
          len(obj.data.polygons) == expected["source_tris"],
          f"{len(obj.data.polygons)} vs {expected['source_tris']}")
    check(f"{name}: patch count", len(records) == expected["patches"],
          f"{len(records)} vs {expected['patches']}")

    got = sorted((r["face_id"], r["generator"], r["sides"], r["loops"])
                 for r in records)
    want = sorted(expected["faces"])
    if got != want:
        differing = [(g, w) for g, w in zip(got, want) if g != w]
        check(f"{name}: every face resolved as recorded", False,
              f"first differences {differing[:4]}")
    else:
        check(f"{name}: every face resolved as recorded", True,
              f"{len(want)} face(s)")

    result_obj = bpy.data.objects.get(pr.mesh_build.result_object_name_for(obj))
    faces = len(result_obj.data.polygons) if result_obj else 0
    verts = len(result_obj.data.vertices) if result_obj else 0
    check(f"{name}: result mesh size",
          (verts, faces) == (expected["result_verts"], expected["result_faces"]),
          f"{verts}v/{faces}f vs {expected['result_verts']}v/"
          f"{expected['result_faces']}f")

    if not faces:
        continue

    # --- invariants ---------------------------------------------------
    state = context.scene.plasticity_retop
    weld = pr.state.to_blender_units(state, state.boundary_weld_distance)
    topology = benchmark.topology_report(result_obj, weld)
    check(f"{name}: nothing is non-manifold",
          topology["non_manifold"] == 0, topology["non_manifold"])
    check(f"{name}: no coincident vertex pair failed to weld",
          topology["unwelded"] == 0, topology["unwelded"])
    check(f"{name}: no wire edges", topology["wire"] == 0, topology["wire"])
    # Orientation, which no count in the table would catch: a generator can
    # turn a whole patch over without changing a single number above. A ring
    # did exactly that once a matched rim started leading the band.
    check(f"{name}: nothing faces into the surface",
          benchmark.flipped_faces(obj, result_obj) == 0,
          benchmark.flipped_faces(obj, result_obj))

    interior, corners = benchmark.deviation_samples(obj, result_obj)
    scale = benchmark.bbox_diagonal(obj)
    worst = 100.0 * max(interior, default=0.0) / scale
    worst_vertex = 100.0 * max(corners, default=0.0) / scale

    # Loose enough to cover the far-from-origin variant, whose float32
    # noise at coordinate ~100 is several times what the same shape shows
    # at the origin. Still four orders below any real chordal error.
    check(f"{name}: every vertex sits on the CAD surface",
          worst_vertex < 1e-2, f"max {worst_vertex:.6f}%")

    if name in PLANAR_OBJECTS:
        # A flat face carries no chordal error at any span, so anything above
        # float noise means the fill is not on the surface it claims.
        #
        # Not literally zero: Blender stores vertices as float32, so a point
        # interpolated across a unit-sized face and then measured against a
        # BVH lands ~1e-6 units off whatever it should be. The measured value
        # is 4.6e-5%; the smallest *real* error in this fixture set is the
        # cylinder's 0.15%, so this threshold sits two orders above the noise
        # and three below anything meaningful.
        check(f"{name}: a planar object is reproduced exactly (to float32)",
              worst < PLANAR_EXACT_PCT, f"max {worst:.9f}% "
              f"vs {PLANAR_EXACT_PCT}%")
    else:
        check(f"{name}: deviation within the recorded ceiling",
              worst <= expected["max_deviation_pct"],
              f"max {worst:.4f}% vs ceiling {expected['max_deviation_pct']}%")

    # --- golden ---------------------------------------------------------
    #
    # Order-dependent and a worst case: this test commits in face_id order,
    # and auto-matching can only match a neighbour that is *already*
    # committed, so a patch baked early can never weld to one baked later.
    # Pinned anyway -- the iteration order is deterministic, so a change here
    # is a real change in behaviour.
    check(f"{name}: open boundary edge count",
          topology["open_edges"] == expected["open_edges"],
          f"{topology['open_edges']} vs {expected['open_edges']}")


# ===========================================================================
# Known gaps. Each fails when it starts working.
# ===========================================================================

print("\n--- known gaps")

for name in ("Sphere", "Torus"):
    records = results.get(name, [])
    unusable = [r for r in records if r["generator"] is None]
    known_gap(
        f"{name}: a closed periodic face cannot be retopologized",
        len(unusable) == len(records) and bool(records),
        "-- one CAD face, no boundary loop at all, so prepare_patch "
        "returns None and the patch is silently unpickable")

holed = [r for r in results.get("Shape with holes", []) if r["loops"] > 2]
known_gap(
    "Shape with holes: faces past two boundary loops are paved over",
    len(holed) == 2,
    f"-- faces {[r['face_id'] for r in holed]} have "
    f"{[r['loops'] for r in holed]} loops; only the outer one is filled, "
    "which is what the 8.98% deviation max measures")

# The pentagons of the chamfered cube, found by *shape* rather than by id: a
# re-export renumbers every face id but does not move a vertex, so anything
# keyed to an id here would silently stop testing what it names.
chamfer_obj = bpy.data.objects.get("Cube Chamfer Edges")
if chamfer_obj is not None:
    analysis = pr.patch_data.analyse(chamfer_obj.data)
    by_id = {r["face_id"]: r for r in results.get("Cube Chamfer Edges", [])}
    pentagons = sorted(
        face_id for face_id, patch in analysis.patches.items()
        if len(patch.boundary_loops) == 1 and len(patch.boundary_loops[0]) == 5)
    generators = [by_id[f]["generator"] for f in pentagons if f in by_id]
    known_gap(
        "Cube Chamfer: symmetric pentagons resolve to different generators",
        len(generators) >= 2 and len(set(generators)) > 1,
        f"-- faces {pentagons} -> {generators}; each is a single 5-vertex "
        "boundary loop, so dominant_corners is tipping either side of "
        "CORNER_CLIFF on float noise")

# The headline finding from the three placement variants.
sizes = {name: (EXPECTED[name]["result_verts"], EXPECTED[name]["result_faces"])
         for name in PLACEMENT_VARIANTS if name in EXPECTED}
known_gap(
    "One shape at three placements gives three different retopologies",
    len(set(sizes.values())) > 1,
    f"-- {sizes}. Same CAD shape, and the generators and side counts do agree "
    "across all three; it is the density that moves. Neither absolute "
    "tolerance is scaled to the part: boundary_weld_distance defaults to 1e-4 "
    "in metres, and patch_data's weld epsilon is a hard-coded 1e-5, so a part "
    "at coordinate ~100 and a part 0.01 across are measured with the same "
    "yardstick as one of unit size. Scaling the weld distance to the part "
    "recovers some of it but not all, so there is a second cause still open")

# A plate whose hole is small is not a band, but its faces come back as Rings
# anyway -- the case CLAUDE.md calls the disaster: every quad stretched from
# the outline to the hole.
rings = [r for r in results.get("Square Plate Small Hole", [])
         if r["generator"] == "Ring"]
known_gap(
    "Square Plate Small Hole: a small hole is still filled as a band",
    bool(rings),
    f"-- face(s) {[r['face_id'] for r in rings]} came back Ring, which the "
    "benchmark reports as an aspect ratio around 400x")

# A cone's lateral face runs to a point. The apex is a singularity no quad
# grid represents, and the deviation says so.
cone = EXPECTED.get("Cone")
known_gap(
    "Cone: the apex is approximated badly",
    bool(cone) and cone["max_deviation_pct"] > 5.0,
    f"-- deviation ceiling {cone['max_deviation_pct'] if cone else '?'}%, "
    "against 0.2% for the cylinder beside it; the lateral face is a "
    "cornerless loop closing on a point, filled as a Quad")

pr.operators.end_session(context)

print()
if FAILURES:
    print(f"=== {len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(1)
print("=== ALL CHECKS PASSED")
