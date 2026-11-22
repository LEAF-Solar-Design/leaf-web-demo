"""Integrate repeated-knot beam spans independently, excluding endpoint jumps."""
import math
import pytest
from test_x03_oblique_spline_author import placed_dxf, spline_record
from test_x03_offline_drawing_author import invoke
from tools_fallback import author_tool

ASKS = [
    ("Measure spline beam length by knot span and report any jumps.", 4, 1),
    ("List each repeated-knot beam length by layer in centimetres.", 0, .1),
    ("Report NURBS beam lengths in metres and identify discontinuities.", -2, .001),
]


def drawing(gap):
    points = [(2, 0, 0), (2, 2, 0), (0, 2, 0), (0, 2+gap, 0), (2, 2+gap, 0), (4, 2+gap, 0)]
    records = spline_record(points, [0,0,0,.375,.375,.375,1,1,1],
                            [1, math.sqrt(.5), 1, 1, 1, 1], (0, 0, 1))
    return placed_dxf(records, (2, 3, 1))


@pytest.mark.parametrize("ask,gap,factor", ASKS)
def test_spans(ask, gap, factor):
    _, source, _ = author_tool(ask)
    status, payload = invoke(source, drawing(gap))
    assert status == "ok", payload
    result = payload[0]
    report = result["spline_spans_by_handle"]["C1/D1"]
    step = math.pi/(2*8192)
    arc = step*math.fsum(math.hypot(4*math.sin((i+.5)*step), 6*math.cos((i+.5)*step))
                         for i in range(8192))
    bound = math.pi/2*step**2*(6 + 36/4)/24
    tol = result["length_relative_tolerance"]
    spans = report["spans"]
    assert [s["interval"] for s in spans] == [[0,.375],[.375,1]]
    assert 0 < tol <= 1e-5
    assert abs(spans[0]["length"]-arc*factor) <= (arc*tol+bound)*factor
    assert spans[1]["length"] == pytest.approx(8*factor)
    assert result["lengths_by_layer"]["Steel-Beams"] == pytest.approx((arc+8)*factor, rel=tol)
    assert [j["knot"] for j in report["jumps"]] == ([.375] if gap else [])
    if gap:
        assert report["jumps"][0]["distance"] == pytest.approx(abs(gap)*3*factor)


@pytest.mark.parametrize("gap,expected", [(4, "tool_error"), (0, "ok")])
def test_chain_requires_physical_continuity(gap, expected):
    _, source, _ = author_tool("Check the joined spline beam chain length and its gaps.")
    status, payload = invoke(source, drawing(gap))
    assert status == expected, payload
    if gap:
        assert "discontinu" in str(payload).lower()
