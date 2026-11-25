"""World-space measurements of planar SPLINE control points carried by tilted inserts."""
import math

import pytest

from test_x03_offline_drawing_author import invoke
from tools_fallback import author_tool


ASKS = [
    ("Measure the tilted spline beam in world space.", (2, 3, .5), 1, "drawing_units"),
    ("List the length of each oblique NURBS beam in centimetres.", (-1, 4, 2), .1, "cm"),
    ("What length does the inclined spline beam have in metres?", (3, 1, 2), .001, "m"),
]


def section(name, records):
    return [(0, "SECTION"), (2, name), *records, (0, "ENDSEC")]


def spline_record(points, knots, weights, normal, handle="D1"):
    tags = [(0, "SPLINE"), (5, handle), (8, "0"), (70, 12), (71, 2),
            (72, len(knots)), (73, len(points)), *zip((210, 220, 230), normal)]
    tags += [(40, v) for v in knots] + [(41, v) for v in weights]
    for pt in points:
        tags += list(zip((10, 20, 30), pt))
    return tags


def placed_dxf(curves, scale):
    # The insert's normal is oblique. Its local X axis is world -X.
    block = [(0, "BLOCK"), (2, "Shape"), (10, 7), (20, -3), *curves,
             (0, "LINE"), (5, "D2"), (8, "0"), (10, 0), (20, 0), (11, 100), (21, 0), (0, "ENDBLK")]
    insert = [(0, "INSERT"), (5, "C1"), (2, "Shape"), (8, "Steel-Beams"), (10, 18),
              (20, 54), (30, -8), (50, 27), *zip((41, 42, 43), scale),
              (210, 0), (220, 3), (230, 4)]
    wire = (section("HEADER", [(9, "$INSUNITS"), (70, 4)]) + section("BLOCKS", block)
            + section("ENTITIES", insert) + [(0, "EOF")])
    return "".join(f"{c}\n{v}\n" for c, v in wire).encode()


def drawing(scale, normal=(0, -4, 3)):
    # Quarter circle of radius 5. Coordinates are already in the block's Cartesian
    # frame, with basis (1,0,0) and (0,3/5,4/5); group 210 does not rotate them.
    return placed_dxf(spline_record([(5, 0, 0), (5, 3, 4), (0, 3, 4)],
                                    [0, 0, 0, 1, 1, 1], [1, math.sqrt(.5), 1], normal), scale)


@pytest.mark.parametrize("ask,scale,factor,unit", ASKS)
def test_oblique_spline_length(ask, scale, factor, unit):
    _, source, _ = author_tool(ask)
    status, payload = invoke(source, drawing(scale))
    assert status == "ok", payload
    result = payload[0]
    # Rotation about the extrusion normal and the orthonormal placement basis
    # preserve speed. Unequal signed axis scales act before those rotations.
    a, b = abs(5*scale[0]), math.hypot(3*scale[1], 4*scale[2])
    h = math.pi / (2*8192)
    expected = h * math.fsum(math.hypot(a*math.sin((i+.5)*h), b*math.cos((i+.5)*h))
                              for i in range(8192))
    # Composite midpoint bound, |speed''| <= max_axis + max_axis**2/min_axis.
    bound = math.pi/2*h*h*(max(a,b) + max(a,b)**2/min(a,b))/24
    assert result["units"] == unit
    tol = result["length_relative_tolerance"]
    assert 0 < tol <= 1e-5
    assert abs(result["lengths_by_layer"]["Steel-Beams"] - factor*expected) <= factor*(bound+tol*expected)


def test_degenerate_spline_normal_is_refused():
    _, source, _ = author_tool("Measure spline beam lengths by layer.")
    status, payload = invoke(source, drawing((1, 2, 3), (0, 0, 0)))
    assert status == "tool_error" and "normal" in str(payload).lower()
