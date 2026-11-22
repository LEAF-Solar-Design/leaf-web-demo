"""Measure a full period against the analytic derivative of a uniform quadratic spline."""
import math

import pytest

from test_x03_oblique_spline_author import placed_dxf, spline_record
from test_x03_offline_drawing_author import invoke
from tools_fallback import author_tool


ASKS = [
    ("Measure the periodic spline beam including its seam once.", (1, 1, 1), 1),
    ("List closed periodic beam spline lengths in centimetres.", (-2, 3, 1), .1),
    ("Report the periodic NURBS beam perimeter length in metres.", (3, 1, 1), .001),
]
CORNERS = [(0, 0, 0), (5, 0, 0), (5, 3, 0), (0, 3, 0)]


def drawing(scale, fault=None):
    points = CORNERS + CORNERS[:2]
    knots = list(range(9))
    weights = [1] * 6
    if fault == "control":
        points[-1] = (4, 0, 0)
    elif fault == "knots":
        knots[-1] += 1
    elif fault == "weights":
        weights[-2] = 2
    tags = spline_record(points, knots, weights, (0, 0, 1))
    tags = [(c, 3 if c == 70 else v) for c, v in tags]
    # Include an unrelated open spline on the same inherited beam layer.
    line = spline_record([(0, 0, 0), (1, 0, 0), (2, 0, 0)], [0, 0, 0, 1, 1, 1],
                         [1, 1, 1], (0, 0, 1), handle="D3")
    return placed_dxf(tags + line, scale)


@pytest.mark.parametrize("ask,scale,factor", ASKS)
def test_complete_period(ask, scale, factor):
    _, source, _ = author_tool(ask)
    status, payload = invoke(source, drawing(scale))
    assert status == "ok", payload
    result = payload[0]
    # Each span velocity interpolates consecutive scaled polygon edges.
    edges = [tuple((b-a)*k for a, b, k in zip(p, q, scale))
             for p, q in zip(CORNERS, CORNERS[1:] + CORNERS[:1])]
    n = 4096
    expected = math.fsum(math.hypot(*(a+(b-a)*(i+.5)/n for a,b in zip(prev,nxt)))
                         for prev,nxt in zip(edges, edges[1:]+edges[:1]) for i in range(n))/n
    # Consecutive rectangle edges are perpendicular.
    # Composite midpoint error per unit span <= max |speed''| / (24*n*n).
    oracle_error = 0.
    for prev, nxt in zip(edges, edges[1:] + edges[:1]):
        change = math.dist(prev, nxt)
        min_speed = math.hypot(*prev)*math.hypot(*nxt)/change
        oracle_error += change**2/(24*n*n*min_speed)
    measured = result["lengths_by_layer"]["Steel-Beams"]
    assert abs(measured - expected*factor) <= (expected*1e-5 + oracle_error)*factor


@pytest.mark.parametrize("fault", ["control", "knots", "weights"])
def test_periodic_evidence_must_wrap(fault):
    _, source, _ = author_tool(ASKS[0][0])
    status, payload = invoke(source, drawing((1, 1, 1), fault))
    assert status == "tool_error" and "periodic" in str(payload).lower()
