"""A single deterministic numerical budget covers all rational spans and members."""
import math

import pytest

from test_x03_oblique_spline_author import placed_dxf, spline_record
from test_x03_offline_drawing_author import invoke, tools_fallback


ASKS = [
    "Measure spline beam lengths over narrow knot spans with at most 24 evaluations.",
    "List each NURBS beam length in centimetres with a budget of 900000 evaluations.",
    "Measure rational spline beam lengths by layer with a limit of 24 evaluations.",
]


def narrow_spans(curved=False, handle="D1"):
    # Two clamped quadratic segments with a shared endpoint. Their first
    # parameter interval is one ulp above 1. Positive geometric weight
    # progressions reparameterize each Bezier without changing its trace.
    points = [(0,0,0), (1,0,0), (2,0,0), (2,1,0), (2,3,0)]
    weights = [1e-80,1,1e80,1,1e-80]
    if curved:
        points = [(2,0,0), (2,2,0), (0,2,0), (-2,2,0), (-2,0,0)]
        weights = [1,math.sqrt(.5)*32,1024,math.sqrt(.5)*32,1]
    edge = math.nextafter(1, 2)
    return spline_record(points, [1]*3 + [edge]*2 + [2]*3, weights, (0,0,1), handle)


@pytest.mark.parametrize("ask,curved,limit", [(ASKS[0], False, 24), (ASKS[1], True, 900000)])
def test_independent_bound_and_evaluation_count(ask, curved, limit):
    _, source, _ = tools_fallback.author_tool(ask)
    status,payload = invoke(source, placed_dxf(narrow_spans(curved), (2,3,1)))
    assert status == "ok", payload
    result = payload[0]
    assert result["spline_evaluation_limit"] == limit
    used = result["spline_evaluations"]
    assert 0 < used <= limit
    if curved:
        # Analytic half-ellipse speed; a composite midpoint bound uses
        # |speed''| <= max_axis + max_axis^2/min_axis.
        h = math.pi/16384
        expected = .1*h*math.fsum(math.hypot(4*math.sin((i+.5)*h),6*math.cos((i+.5)*h))
                                  for i in range(16384))
        oracle_error = .1*math.pi*h*h*(6+36/4)/24
    else:
        # Two straight monotone segments, with 9 blends and 3 projected
        # poles per span. Constant positive weights are unnecessary.
        assert used == 24
        expected, oracle_error = 4+9, 0
    tol = result["length_relative_tolerance"]
    assert 0 < tol <= 1e-5
    assert abs(result["lengths_by_layer"]["Steel-Beams"]-expected) <= tol*expected + oracle_error


@pytest.mark.parametrize("members", [
    narrow_spans(curved=True),
    narrow_spans() + narrow_spans(handle="D3"),
])
def test_exhaustion_discards_partial_result(members):
    _, source, _ = tools_fallback.author_tool(ASKS[2])
    status,payload = invoke(source, placed_dxf(members, (2,3,1)))
    assert status == "tool_error", payload
    assert "did not converge within the evaluation budget" in str(payload)
    assert "lengths_by_layer" not in str(payload)


def test_unrepresentable_weight_ratio_refuses_measurement():
    _, source, _ = tools_fallback.author_tool(ASKS[1])
    curve = spline_record([(0,0,0), (1,1,0), (3,0,0)], [0]*3+[1]*3,
                          [1e-250,1,1e250], (0,0,1))
    status,payload = invoke(source, placed_dxf(curve, (2,3,1)))
    assert status == "tool_error" and "below numeric precision" in str(payload), payload
