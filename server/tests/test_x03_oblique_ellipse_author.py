"""Measure partial native ellipses after tilted and reflected placements."""
import math

import pytest

from test_x03_ocs_arc_author import drawing, placed
from test_x03_offline_drawing_author import invoke
from tools_fallback import author_tool


CASES = [
    ("Measure the curved beam lengths by layer for the tilted elliptical members.",
     0.2, 2.7, .5, "drawing_units", 1),
    ("List each beam length in centimetres for the ellipse crossing parameter zero.",
     5.4, 1.2, .25, "cm", .1),
    ("Sum the beam lengths per layer in metres across reflected elliptical spans.",
     1.3, 5.9, .8, "m", .001),
]


@pytest.mark.parametrize("ask,start,end,ratio,unit,factor", CASES)
def test_oblique_ellipse(ask, start, end, ratio, unit, factor):
    check_ellipse(ask, start, end, ratio, unit, factor, (end-start) % math.tau)


@pytest.mark.parametrize("start,end,ratio,sweep", [
    (0, 6.28318530717959, .5, math.tau),
    (0, 1e-13, .5, 1e-13),
    (.7, 1.2, .001, .5),
])
def test_ellipse_span_controls(start, end, ratio, sweep):
    check_ellipse(CASES[0][0], start, end, ratio, "drawing_units", 1, sweep)


def check_ellipse(ask, start, end, ratio, unit, factor, sweep):
    # Perpendicular WCS directions of length five. Their cross product points
    # along the recorded plane normal (0, 3, 4).
    major = (3, 3.2, -2.4)
    minor = tuple(ratio*x for x in (-4, 2.4, -1.8))
    records = [(0, "ELLIPSE"), (5, "D7"), (8, "0"), (10, 120), (20, -30), (30, 7),
               *zip((11,21,31), major), (210, 0), (220, 3), (230, 4),
               (40, ratio), (41, start), (42, end)]
    _, source, _ = author_tool(ask)
    status, payload = invoke(source, drawing(records))
    assert status == "ok", payload
    report = payload[0]
    assert report["units"] == unit
    assert set(report["lengths_by_layer"]) == {"Roof-Beams"}
    step = sweep / 16384
    def speed(t):
        tangent = tuple(b*math.cos(t) - a*math.sin(t) for a, b in zip(major, minor))
        return math.hypot(*placed(tangent))

    expected = step * math.fsum(speed(start + (i+.5)*step) for i in range(16384))
    # Singular values of the two placements lie in [1, 12] in composition.
    # Thus speed >= 5*ratio and both derivatives of velocity have norm <= 60.
    bound = sweep * step**2 * (60 + 60**2 / (5*ratio)) / 24
    tol = report["length_relative_tolerance"]
    assert 0 < tol <= 1e-5
    assert abs(report["lengths_by_layer"]["Roof-Beams"] - factor*expected) <= factor*(bound + tol*expected)
