"""A finite length cannot describe a rational curve with an interior pole."""
from fractions import Fraction
import math

import pytest

from test_x03_oblique_spline_author import placed_dxf, spline_record
from test_x03_offline_drawing_author import invoke, tools_fallback


F = Fraction
CASES = [
    ("Measure the rational spline beam length by layer.", (1, -1, 1), (F(1, 2),), 1),
    ("How long in metres is each NURBS beam with these weights?", (1, -1, -3), (F(1, 4),), .001),
    ("List each spline beam length in centimetres for this takeoff.", (3, -5, 3), (F(1, 4), F(3, 4)), .1),
]


@pytest.mark.parametrize("ask,weights,roots,factor", CASES)
def test_singular_spline_cannot_report_finite_length(ask, weights, roots, factor):
    points = [(0, 0, 0), (1.5, 2, 0), (6, 8, 0)]
    def drawing(w):
        curve = spline_record(points, [0]*3+[1]*3, w, (0, 0, 1))
        return placed_dxf(curve, (1, 1, 1))

    # Independent power-basis denominator and homogeneous x numerator. A
    # repeated root need not change the denominator sign across the pole.
    a, b, c = weights
    for t in roots:
        assert 0 < t < 1
        assert a + 2*(b-a)*t + (a-2*b+c)*t*t == 0
        assert 3*b*t*(1-t) + 6*c*t*t != 0

    _, source, _ = tools_fallback.author_tool(ask)
    status, payload = invoke(source, drawing(weights))
    assert status == "tool_error", payload
    assert "unsupported" in str(payload).lower() and "weight" in str(payload).lower()
    assert "lengths_by_layer" not in str(payload)

    status, payload = invoke(source, drawing((1, 2, 1)))
    assert status == "ok", payload
    length = payload[0]["lengths_by_layer"]["Steel-Beams"]
    assert math.isfinite(length)
    assert length == pytest.approx(10*factor, rel=1e-5)
