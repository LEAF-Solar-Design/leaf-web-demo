"""Rational SPLINE beam lengths through DXF intake, offline author and sandbox."""
import math

import pytest

from test_x03_offline_drawing_author import invoke
from tools_fallback import author_tool


ASKS = [
    ("Measure transformed rational spline beam lengths by layer and report the length tolerance.", "drawing_units", 1),
    ("List each spline beam length in centimetres by layer across nested references.", "cm", .1),
    ("Sum the lengths of NURBS beams per layer in metres including scaled blocks.", "m", .001),
]


def drawing(fault=None):
    # Two quadratic rational quarter circles, with an interior double knot.
    controls = [(10, 0), (10, 10), (0, 10), (-10, 10), (-10, 0)]
    knots = [0, 0, 0, .25, .25, 1, 1, 1]
    weights = [1, math.sqrt(.5), 1, math.sqrt(.5), 1]
    if fault == "knot":
        knots[4] = .1
    if fault == "weight":
        weights[2] = 0
    spline = [(0, "SPLINE"), (5, "D1"), (8, "0"), (70, 4), (71, 2), (72, 8), (73, 5),
              *((40, value) for value in knots), *((41, value) for value in weights)]
    for x, z in controls:
        spline.extend([(10, x), (20, 3), (30, z)])

    def section(name, records):
        return [(0, "SECTION"), (2, name), *records, (0, "ENDSEC")]

    def block(name, records):
        return [(0, "BLOCK"), (2, name), (10, 0), (20, 0), *records, (0, "ENDBLK")]

    def insert(handle, layer, name, scale):
        return [(0, "INSERT"), (5, handle), (2, name), (8, layer), (10, 20), (20, 6),
                *zip((41, 42, 43), scale)]

    records = (section("HEADER", [(9, "$INSUNITS"), (70, 4)])
               + section("BLOCKS", block("Section", spline) +
                         block("Assembly", insert("C1", "0", "Section", (-2, 3, 3))))
               + section("ENTITIES", insert("B1", "Roof-Beams", "Assembly", (2, 1, .5)))
               + [(0, "EOF")])
    return "".join(f"{code}\n{value}\n" for code, value in records).encode()


@pytest.mark.parametrize("ask,unit,factor", ASKS)
def test_transformed_spline_length(ask, unit, factor):
    _, source, _ = author_tool(ask)
    status, payload = invoke(source, drawing())
    assert status == "ok", payload
    result = payload[0]
    assert result["units"] == unit
    # The transformed spline covers half an ellipse with radii 40 and 15.
    # Integrate its analytic speed independently of NURBS parameterization.
    n = 8192
    step = math.pi/n
    expected = step * math.fsum(math.hypot(40*math.sin((i+.5)*step), 15*math.cos((i+.5)*step))
                                for i in range(n))
    # |speed''| <= |v''| + |v'|^2 / min|v| <= 40 + 40^2 / 15.
    oracle_error = math.pi * step**2 * (40 + 40**2/15) / 24
    tolerance = result["length_relative_tolerance"]
    assert 0 < tolerance <= 1e-5
    assert set(result["lengths_by_layer"]) == {"Roof-Beams"}
    length = result["lengths_by_layer"]["Roof-Beams"]
    assert abs(length - expected*factor) <= factor*(expected*tolerance + oracle_error)
    if ask.startswith("List"):
        assert result["lengths_by_handle"] == {"B1/C1/D1": {"layer": "Roof-Beams", unit: length}}


@pytest.mark.parametrize("fault", ["knot", "weight"])
def test_invalid_spline_is_not_counted_as_zero(fault):
    _, source, _ = author_tool(ASKS[0][0])
    status, payload = invoke(source, drawing(fault))
    assert status == "tool_error"
    assert fault in str(payload).lower()
