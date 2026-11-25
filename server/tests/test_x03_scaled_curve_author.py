"""Measure nested elliptical beam arcs through the offline drawing author and sandbox."""
import math

import pytest

from test_x03_offline_drawing_author import invoke
from tools_fallback import author_tool


ASKS = [
    ("Measure curved beams inside nonuniformly scaled references and show the length tolerance in the report.", "drawing_units", 1),
    ("List each curved beam length by layer in centimetres, including nested inserts.", "cm", .1),
    ("Sum curved beam lengths per layer in metres across nested block references.", "m", .001),
]


def drawing():
    def arc(handle, layer, bulge):
        return [(0, "LWPOLYLINE"), (5, handle), (8, layer), (90, 2), (70, 0),
                (10, 10), (20, 0), (42, bulge), (10, 0), (20, 10)]

    def block(name, children):
        return [(0, "BLOCK"), (2, name), (10, 0), (20, 0), *children, (0, "ENDBLK")]

    def insert(handle, layer, name, sx, sy, angle):
        return [(0, "INSERT"), (5, handle), (8, layer), (2, name), (10, 24), (20, 5),
                (41, sx), (42, sy), (50, angle)]

    def section(name, contents):
        return [(0, "SECTION"), (2, name), *contents, (0, "ENDSEC")]

    small = arc("D1", "0", math.tan(math.pi / 8))
    large = arc("D2", "Beam West", math.tan(-3 * math.pi / 8))
    records = (section("HEADER", [(9, "$INSUNITS"), (70, 4)])
               + section("BLOCKS", block("Arcs", small + large)
                         + block("Frame", insert("C1", "0", "Arcs", -2, 1, 30)))
               + section("ENTITIES", insert("B1", "Beam East", "Frame", 1, 4, 0))
               + [(0, "EOF")])
    return "".join(f"{code}\n{value}\n" for code, value in records).encode()


def ellipse_integral(sweep):
    # Directly differentiate the fixture's radius-10 circle, then apply the
    # two placements to its velocity. Intake stores INSERT angles to 6 decimals.
    angle = round(math.radians(30), 6)
    c, s = math.cos(angle), math.sin(angle)
    n = 32768
    step = sweep / n
    speeds = []
    for i in range(n):
        t = (i + .5) * step
        x, y = 20 * math.sin(t), 10 * math.cos(t)
        speeds.append(math.hypot(c*x - s*y, 4*(s*x + c*y)))
    # |speed''| <= |v''| + |v'|^2 / |v| <= 80 + 80^2/10.
    # The composite midpoint error is at most |sweep| * step^2 * 720 / 24.
    return abs(step) * math.fsum(speeds), abs(sweep) * step**2 * 30


@pytest.mark.parametrize("ask,unit,factor", ASKS)
def test_scaled_curved_beams(ask, unit, factor):
    _, source, _ = author_tool(ask)
    status, payload = invoke(source, drawing())
    assert status == "ok", payload
    result = payload[0]
    assert result["units"] == unit
    tolerance = result["length_relative_tolerance"]
    assert 0 < tolerance <= 1e-5
    totals = result["lengths_by_layer"]
    assert set(totals) == {"Beam East", "Beam West"}
    for layer, handle, sweep in (("Beam East", "D1", math.pi/2), ("Beam West", "D2", -3*math.pi/2)):
        expected, oracle_error = ellipse_integral(sweep)
        assert abs(totals[layer] - factor*expected) <= factor*(expected*tolerance + oracle_error)
        if ask.startswith("List"):
            assert result["lengths_by_handle"]["B1/C1/" + handle] == {unit: totals[layer], "layer": layer}
