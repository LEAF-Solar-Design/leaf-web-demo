"""Joined spline placement and gap reports through the real offline author."""
import math

import pytest

from test_x03_offline_drawing_author import invoke
from tools_fallback import author_tool


ASKS = [
    "Measure a beam made from joined spline segments and identify gaps between their endpoints.",
    "Report the lengths of connected NURBS beam segments in centimetres, including any breaks.",
    "Check whether the spline beam chain is continuous and give its length in metres.",
]


def drawing(gap=0, fork=False):
    def block(name, contents):
        return [(0, "BLOCK"), (2, name), (10, 0), (20, 0), *contents, (0, "ENDBLK")]

    def insert(handle, name, layer, x, y, sx=1, sy=1, rotation=0):
        return [(0, "INSERT"), (5, handle), (2, name), (8, layer), (10, x),
                (20, y), (41, sx), (42, sy), (50, rotation)]

    def spline(handle, control, weights):
        degree = len(control) - 1
        records = [(0, "SPLINE"), (5, handle), (8, "0"), (70, 4), (71, degree),
                   (72, 2*len(control)), (73, len(control))]
        for knot in [0]*len(control) + [1]*len(control):
            records.append((40, knot))
        for weight in weights:
            records.append((41, weight))
        for x, y in control:
            records.extend([(10, x), (20, y), (30, 0)])
        return records

    arc = spline("A1", [(10, 0), (10, 10), (0, 10)], [1, math.sqrt(.5), 1])
    # The stem's second endpoint meets the arc, so record order is insufficient.
    stem = spline("B1", [(0, 5), (0, 0)], [1, 1])
    assembly = [*insert("C1", "Stem", "0", 0, 10+gap), *insert("C2", "Arc", "0", 0, 0)]
    if fork:
        assembly.extend(insert("C3", "Stem", "0", 0, 10, rotation=90))
    records = [(0, "SECTION"), (2, "HEADER"), (9, "$INSUNITS"), (70, 4), (0, "ENDSEC"),
               (0, "SECTION"), (2, "BLOCKS"), *block("Arc", arc), *block("Stem", stem),
               *block("Assembly", assembly), (0, "ENDSEC"), (0, "SECTION"), (2, "ENTITIES"),
               *insert("D1", "Assembly", "Beam-East", 80, -50, 2, 3, 90), (0, "ENDSEC"), (0, "EOF")]
    return "".join(f"{code}\n{value}\n" for code, value in records).encode()


@pytest.mark.parametrize("ask,gap,factor", [(ASKS[0], 0, 1), (ASKS[1], 2, .1), (ASKS[2], 1, .001)])
def test_joined_segment_report(ask, gap, factor):
    _, source, _ = author_tool(ask)
    status, payload = invoke(source, drawing(gap))
    assert status == "ok", payload
    row = payload[0]["chains_by_layer"]["Beam-East"]
    n = 8192
    step = math.pi/(2*n)
    # Quarter ellipse plus a straight 15 mm stem; midpoint quadrature on analytic speed.
    expected = 15 + step * math.fsum(
        math.hypot(20*math.sin((i+.5)*step), 30*math.cos((i+.5)*step)) for i in range(n))
    oracle_error = (math.pi/2) * step**2 * (30 + 30**2/20) / 24
    assert abs(row["segment_length"] - factor*expected) <= factor*(expected*1e-5 + oracle_error)
    assert set(row["segments"]) == {"D1/C2/A1", "D1/C1/B1"}
    assert row["continuous"] == (gap == 0)
    assert len(row["gaps"]) == bool(gap)
    if gap:
        match = row["gaps"][0]
        assert match["distance"] == pytest.approx(gap*3*factor)
        assert match["between"] == ["D1/C1/B1", "D1/C2/A1"]


def test_three_endpoints_at_one_joint_are_ambiguous():
    _, source, _ = author_tool(ASKS[0])
    status, payload = invoke(source, drawing(fork=True))
    assert status == "tool_error" and "ambiguous" in str(payload).lower(), payload
