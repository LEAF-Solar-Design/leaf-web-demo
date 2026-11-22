"""The same rational beam at three INSUNITS scales has one physical length."""
import math

import pytest

from test_x03_oblique_spline_author import placed_dxf, spline_record
from test_x03_offline_drawing_author import invoke, tools_fallback


ASKS = [
    ("Measure the spline beam length in metres.", "m", 1),
    ("List NURBS beam lengths by layer in centimetres.", "cm", .01),
    ("How long in feet is each spline beam?", "ft", .3048),
]


def drawing(unit, metres, extra_unit=None):
    radius = 5/metres
    curve = spline_record([(radius,0,0),(radius,radius,0),(0,radius,0)],
                          [0,0,0,1,1,1], [1,math.sqrt(.5),1], (0,0,1))
    raw = placed_dxf(curve,(2,3,1))
    header = b"9\n$INSUNITS\n70\n4\n"
    declared = f"9\n$INSUNITS\n70\n{unit}\n".encode()
    if extra_unit is not None:
        declared += f"9\n$INSUNITS\n70\n{extra_unit}\n".encode()
    return raw.replace(header,declared,1)


@pytest.mark.parametrize("ask,label,metres_per_output", ASKS)
def test_physical_length_is_unit_invariant(ask,label,metres_per_output):
    _, source, _ = tools_fallback.author_tool(ask)
    h = (math.pi/2)/8192
    expected_m = h*math.fsum(math.hypot(10*math.sin((i+.5)*h),15*math.cos((i+.5)*h)) for i in range(8192))
    # Composite midpoint bound, |speed''| <= largest axis + largest^2/smallest.
    bound_m = (math.pi/2)*h*h*(15+225/10)/24
    physical = []
    for code,metres in ((6,1),(4,.001),(2,.3048)):
        status,payload = invoke(source,drawing(code,metres))
        assert status == "ok", payload
        result = payload[0]
        assert result["units"] == label
        tolerance = result["length_relative_tolerance"]
        assert 0 < tolerance <= 1e-5
        measured_m = metres_per_output*result["lengths_by_layer"]["Steel-Beams"]
        assert abs(measured_m - expected_m) <= bound_m + tolerance*expected_m
        physical.append(measured_m)
    assert max(physical)-min(physical) < 1e-10


def test_duplicate_units_agree_or_refuse_conflict():
    _, source, _ = tools_fallback.author_tool(ASKS[0][0])
    assert invoke(source,drawing(6,1,6))[0] == "ok"
    for first,second in ((4,6),(6,4)):
        status,payload = invoke(source,drawing(first,1,second))
        assert status == "tool_error" and "conflicting drawing insertion units" in str(payload).lower()
