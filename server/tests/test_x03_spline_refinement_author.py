"""Equivalent rational Bezier representations must produce the same beam length."""
import math

import pytest

from test_x03_oblique_spline_author import placed_dxf, spline_record
from test_x03_offline_drawing_author import invoke, tools_fallback


CASES = [
    ("Measure spline beam lengths by layer after knot insertion.", "insert", (2, 3, 1), 1),
    ("List the length of each degree-elevated NURBS beam in centimetres.", "elevate", (4, 1, 1), .1),
    ("How long in metres is each spline beam after refining the curve?", "both", (-1, 5, 1), .001),
]


def blend(a, b, t):
    return tuple((1-t)*x + t*y for x, y in zip(a, b))


def curve(variant):
    # Homogeneous control triples (wx, wy, w) for a radius-five quarter circle.
    w = math.sqrt(.5)
    controls = [(5, 0, 1), (5*w, 5*w, w), (0, 5, 1)]
    if variant in ("elevate", "both"):
        p, q, r = controls
        controls = [p, blend(p, q, 2/3), blend(q, r, 1/3), r]
    degree = len(controls) - 1
    knots = [0] * (degree + 1) + [1] * (degree + 1)
    if variant in ("insert", "both"):
        t = .375
        controls = [controls[0], *(blend(a, b, t) for a, b in zip(controls, controls[1:])), controls[-1]]
        knots.insert(degree + 1, t)
    if variant == "both":
        knots = [k + 32 for k in knots]
    tags = spline_record([(x/v, y/v, 0) for x, y, v in controls], knots,
                         [v for _, _, v in controls], (0, 0, 1))
    return [(code, degree if code == 71 else value) for code, value in tags]


@pytest.mark.parametrize("ask,variant,scale,factor", CASES)
def test_refinement_preserves_length(ask, variant, scale, factor):
    _, source, _ = tools_fallback.author_tool(ask)
    a, b = abs(5*scale[0]), abs(5*scale[1])
    n = 8192
    h = math.pi/(2*n)
    expected = factor*h*math.fsum(math.hypot(a*math.sin((i+.5)*h), b*math.cos((i+.5)*h))
                                  for i in range(n))
    # For v=|r'|, |v''| <= |r'''| + |r''|^2/min(v). Composite midpoint remainder.
    error = factor*(math.pi/2)*h*h*(max(a,b) + max(a,b)**2/min(a,b))/24
    lengths = []
    for representation in ("original", variant):
        status, payload = invoke(source, placed_dxf(curve(representation), scale))
        assert status == "ok", payload
        result = payload[0]
        assert 0 < result["length_relative_tolerance"] <= 1e-5
        value = result["lengths_by_layer"]["Steel-Beams"]
        if "each" in ask:
            unit = "cm" if factor == .1 else "m"
            assert result["lengths_by_handle"] == {"C1/D1": {"layer": "Steel-Beams", unit: value}}
        assert abs(value - expected) <= error + result["length_relative_tolerance"]*expected
        lengths.append(value)
    assert lengths[1] == pytest.approx(lengths[0], rel=1e-5)
