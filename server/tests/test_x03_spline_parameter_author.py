"""Positive affine changes of a NURBS knot domain preserve world-space length."""
import math

import pytest

from test_x03_oblique_spline_author import placed_dxf, spline_record
from test_x03_offline_drawing_author import invoke, tools_fallback


ASKS = [
    ("Measure spline beam lengths by layer after shifting the parameter origin.", 1024, 1056, 2, 3, 1),
    ("List each NURBS beam length in centimetres using its rescaled knot interval.", -9, -8.75, 3, 1, .1),
    ("How long in metres is each spline beam? Measure across its full parameter range.", -1e308, 1e308, 1, 4, .001),
]


@pytest.mark.parametrize("ask,left,right,sx,sy,factor", ASKS)
def test_affine_parameter_change_preserves_rational_curve_length(ask, left, right, sx, sy, factor):
    _, source, _ = tools_fallback.author_tool(ask)
    n = 8192
    theta = math.pi / 2
    h = theta / n
    a, b = 5 * sx, 5 * sy
    expected = factor * h * math.fsum(math.hypot(a*math.sin((i+.5)*h), b*math.cos((i+.5)*h))
                                       for i in range(n))
    # Composite midpoint error for ellipse speed, bounded via the first two derivatives.
    bound = factor * theta * h**2 * (max(a,b) + max(a,b)**2/min(a,b)) / 24
    lengths = []
    for start, end in ((0, 1), (left, right)):
        tags = spline_record([(5, 0, 0), (5, 5, 0), (0, 5, 0)], [start]*3 + [end]*3,
                             [1, math.sqrt(.5), 1], (0, 0, 1))
        status, payload = invoke(source, placed_dxf(tags, (sx, sy, 1)))
        assert status == "ok", payload
        result = payload[0]
        measured = result["lengths_by_layer"]["Steel-Beams"]
        assert abs(measured - expected) <= 1e-5*expected + bound
        lengths.append(measured)
    assert lengths[0] == pytest.approx(lengths[1], rel=1e-12)


def test_wide_domain_interior_knot_retains_blending_fraction():
    _, source, _ = tools_fallback.author_tool("Measure spline beam lengths by layer.")
    for lo, mid, hi in ((0, .5, 1), (-1e308, 0, 1e308)):
        tags = spline_record([(0, 0, 0), (2, 0, 0), (2, 2, 0), (4, 2, 0)],
                             [lo]*3 + [mid] + [hi]*3, [1]*4, (0, 0, 1))
        status, payload = invoke(source, placed_dxf(tags, (1, 1, 1)))
        assert status == "ok", payload
        # Two mirror-image quadratic pieces meet at (2,1), halfway between
        # the two interior controls. The first has derivative (4-4u, 2u).
        n = 8192
        length = 2*math.fsum(math.hypot(4*(1-u), 2*u)
                             for u in ((i+.5)/n for i in range(n))) / n
        # |speed''| <= |acceleration|^2 / min(speed); min(speed)=4/sqrt(5).
        bound = 2*20*math.sqrt(5)/(4*24*n**2)
        assert abs(payload[0]["lengths_by_layer"]["Steel-Beams"] - length) <= 1e-5*length + bound


@pytest.mark.parametrize("ask,straight_length", [
    ("Total beam lengths by layer across LINE, POLYLINE and SPLINE entities.", 107),
    ("List each beam length, including straight lines, polylines and NURBS.", 107),
    ("Measure the combined length of spline, line and polyline beams per layer.", 107),
    ("Measure only spline beam lengths per layer.", 0),
])
def test_mixed_beam_takeoff(ask, straight_length):
    # placed_dxf also places a 100-unit line in this block.
    curve = spline_record([(5, 0, 0), (5, 5, 0), (0, 5, 0)], [0]*3 + [1]*3,
                          [1, math.sqrt(.5), 1], (0, 0, 1))
    polyline = [(0, "LWPOLYLINE"), (5, "E3"), (8, "0"), (90, 3), (70, 0),
                (10, 0), (20, 0), (10, 3), (20, 0), (10, 3), (20, 4)]
    _, source, _ = tools_fallback.author_tool(ask)
    status, payload = invoke(source, placed_dxf(curve + polyline, (1, 1, 1)))
    assert status == "ok", payload
    expected = straight_length + 5*math.pi/2
    assert payload[0]["lengths_by_layer"] == pytest.approx({"Steel-Beams": expected}, rel=1e-5)


@pytest.mark.parametrize("ask", [
    "Measure all beams, including splines, and total their lengths by layer.",
    "List all beam lengths by layer, counting bulged arcs and NURBS too.",
])
def test_inclusive_takeoff_measures_polyline_arcs(ask):
    spline = spline_record([(0, 0, 0), (1, 0, 0), (2, 0, 0)], [0]*3 + [1]*3,
                           [1]*3, (0, 0, 1))
    arc = [(0, "LWPOLYLINE"), (5, "E4"), (8, "0"), (90, 2), (70, 0),
           (10, 0), (20, 0), (42, 1), (10, 6), (20, 0)]
    _, source, _ = tools_fallback.author_tool(ask)
    status, payload = invoke(source, placed_dxf(spline + arc, (1, 1, 1)))
    assert status == "ok", payload
    # Two-unit spline, radius-three semicircle, and the fixture's 100-unit line.
    expected = 102 + 3*math.pi
    assert payload[0]["lengths_by_layer"] == pytest.approx({"Steel-Beams": expected}, rel=1e-5)
