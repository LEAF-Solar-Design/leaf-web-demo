"""Survey-scale DXF coordinates measured against decimal endpoint differences."""
from decimal import Decimal, localcontext

import pytest

from test_x03_oblique_spline_author import section
from test_x03_offline_drawing_author import invoke
from tools_fallback import author_tool


ASKS = [
    ("Measure georeferenced beam lengths by layer with error bounds.", "LINE", (".003", ".004", "0"), 1),
    ("List short survey beam lengths per layer in centimetres.", "LWPOLYLINE", (".005", ".012", "0"), .1),
    ("Report beam lengths by layer at large coordinates in metres.", "LINE", (".001", ".002", ".002"), .001),
]


def drawing(origin, kind, step):
    origin = Decimal(origin)
    xyz = [origin] * 3
    end = [origin+Decimal(v) for v in step]
    tags = [(0, kind), (5, "B1"), (8, "Steel-Beams")]
    if kind == "LINE":
        tags += list(zip((10,20,30), xyz)) + list(zip((11,21,31), end))
    else:
        tags += [(90, 2), (70, 0), (10,xyz[0]), (20,xyz[1]), (10,end[0]), (20,end[1])]
    wire = section("HEADER", [(9,"$INSUNITS"),(70,4)]) + section("ENTITIES", tags) + [(0, "EOF")]
    return "".join(f"{code}\n{value}\n" for code,value in wire).encode()


@pytest.mark.parametrize("ask,kind,step,factor", ASKS)
def test_translation_invariance(ask,kind,step,factor):
    _, source, _ = author_tool(ask)
    with localcontext() as ctx:
        ctx.prec = 50
        expected = sum(Decimal(v)**2 for v in step).sqrt()*Decimal(str(factor))
    for origin in ("0", "1000000000000000"):
        status, payload = invoke(source, drawing(origin, kind, step))
        assert status == "ok", payload
        value = payload[0]["lengths_by_layer"]["Steel-Beams"]
        bound = payload[0]["absolute_error_by_layer"]["Steel-Beams"]
        assert 0 < bound <= value*1e-5
        assert abs(expected - Decimal.from_float(value)) <= Decimal.from_float(bound)


def test_collapsed_segment_refused():
    _, source, _ = author_tool(ASKS[0][0])
    status, payload = invoke(source, drawing("1000000000000000", "LINE", (0,0,0)))
    assert status == "tool_error" and "collapse" in str(payload).lower()
