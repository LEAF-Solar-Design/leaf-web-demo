"""Fit points and endpoint tangents do not constitute an explicit control net."""
import pytest

from test_x03_oblique_spline_author import placed_dxf
from test_x03_offline_drawing_author import invoke, tools_fallback


CASES = [
    ("Measure the spline beam length on each layer.", (1, 0, 0), (0, 1, 0)),
    ("How long in metres is each fitted NURBS beam?", (0, 1, 0), (1, 0, 0)),
    ("List spline beam lengths in centimetres for the takeoff.", (1, 1, 1), (1, -1, 0)),
]


@pytest.mark.parametrize("ask,start,end", CASES)
def test_fit_only_spline_has_explicit_unsupported_result(ask, start, end):
    _, source, _ = tools_fallback.author_tool(ask)
    curve = [(0, "SPLINE"), (5, "A1"), (8, "0"), (70, 0), (71, 3),
             (72, 0), (73, 0), (74, 3)]
    for point in ((0, 0, 0), (3, 4, 0), (6, 0, 0)):
        curve.extend(zip((11, 21, 31), point))
    curve.extend(zip((12, 22, 32), start))
    curve.extend(zip((13, 23, 33), end))

    status, payload = invoke(source, placed_dxf(curve, (1, 1, 1)))
    assert status == "tool_error", payload
    assert "fit-point-only" in str(payload).lower()
    assert "unsupported" in str(payload).lower()
    assert "lengths_by_layer" not in str(payload)


def test_control_net_with_fit_data_remains_measurable():
    _, source, _ = tools_fallback.author_tool(CASES[0][0])
    line = [(0, "SPLINE"), (5, "A1"), (8, "0"), (70, 0), (71, 1),
            (72, 4), (73, 2), (74, 2)]
    line.extend((40, k) for k in (0, 0, 1, 1))
    for codes in ((10, 20, 30), (11, 21, 31)):
        for point in ((0, 0, 0), (3, 4, 0)):
            line.extend(zip(codes, point))
    status, payload = invoke(source, placed_dxf(line, (1, 1, 1)))
    assert status == "ok", payload
    assert payload[0]["lengths_by_layer"] == pytest.approx({"Steel-Beams": 5})
