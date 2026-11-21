"""Lengths of transformed semicubical parabolas across their stationary cusps."""
import math

import pytest

from test_x03_oblique_spline_author import placed_dxf, spline_record
from test_x03_offline_drawing_author import invoke, tools_fallback


CASES = [
    ("Report the spline beam length and its spans by layer.", .375, .75, (1, 2, 1), 1),
    ("List each NURBS beam length in centimetres with its knot spans.", .75, .25, (-3, 1, 1), .1),
    ("Measure the spline beam in metres and include its spans.", .5, .5, (2, 3, 1), .001),
]


def cubic_span(lo, hi):
    # Endpoint values and derivatives of (u²,u³) determine each cubic Bezier net.
    h = (hi-lo)/3
    return [(lo**2, lo**3, 0), (lo**2+h*2*lo, lo**3+h*3*lo**2, 0),
            (hi**2-h*2*hi, hi**3-h*3*hi**2, 0), (hi**2, hi**3, 0)]


def arc_primitive(u, sx, sy):
    # Integral from zero of |u| sqrt(4 sx² + 9 sy² u²).
    length = (math.hypot(2*sx, 3*sy*u)**3 - abs(2*sx)**3)/(27*sy**2)
    return math.copysign(length, u)


def quadrature(lo, hi, sx, sy):
    # Integrate the two smooth one-sided speeds separately at the cusp.
    bounds = [lo, 0, hi] if lo < 0 < hi else [lo, hi]
    total = 0
    for a, b in zip(bounds, bounds[1:]):
        h = (b-a)/512
        def speed(index):
            u = a+index*h
            return math.hypot(2*sx*u, 3*sy*u*u)
        total += h/3 * (speed(0) + speed(512)
                        + 4*math.fsum(speed(i) for i in range(1, 512, 2))
                        + 2*math.fsum(speed(i) for i in range(2, 512, 2)))
    return total


@pytest.mark.parametrize("ask,cusp,knot,scale,factor", CASES)
def test_cusp_preserves_both_arc_lengths(ask, cusp, knot, scale, factor):
    edges = [-cusp, knot-cusp, 1-cusp]
    controls = cubic_span(*edges[:2]) + cubic_span(*edges[1:])[1:]
    knots = [0]*4 + [knot]*3 + [1]*4
    tags = spline_record(controls, knots, [], (0, 0, 1))
    curve = [(code, 3 if code == 71 else value) for code, value in tags]
    _, source, _ = tools_fallback.author_tool(ask)
    status, payload = invoke(source, placed_dxf(curve, scale))
    assert status == "ok", payload
    result = payload[0]
    tol = result["length_relative_tolerance"]
    assert 0 < tol <= 1e-5
    measured = result["spline_spans_by_handle"]["C1/D1"]
    assert not measured["jumps"]
    assert [s["interval"] for s in measured["spans"]] == [[0, knot], [knot, 1]]
    sx, sy, _ = scale
    exact = []
    for lo, hi in zip(edges, edges[1:]):
        length = arc_primitive(hi, sx, sy) - arc_primitive(lo, sx, sy)
        assert quadrature(lo, hi, sx, sy) == pytest.approx(length, rel=1e-10)
        exact.append(factor*length)
    assert [s["length"] for s in measured["spans"]] == pytest.approx(exact, rel=tol, abs=0)
    assert result["lengths_by_layer"]["Steel-Beams"] == pytest.approx(sum(exact), rel=tol)
