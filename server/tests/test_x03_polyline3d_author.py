"""Measure classic spatial POLYLINE edges without flattening Z or counting closure twice."""
import math

import pytest

from test_x03_ocs_arc_author import drawing, placed
from test_x03_offline_drawing_author import invoke
from tools_fallback import author_tool


CASES = [
    ("Measure the open 3D beam lengths by layer.", [(0, 0, 0), (0, 0, 5), (3, 4, 5)], False, 1),
    ("List closed spatial beam lengths in centimetres.", [(1, 2, 3), (4, 6, 3), (4, 6, 15)], True, .1),
    ("Sum beam lengths in metres even when adjacent vertices repeat.",
     [(-2, 0, 1), (1, 4, 1), (1, 4, 1), (1, 4, 8)], False, .001),
]


def render(points, nested, flags, vertex_flags):
    layer = "0" if nested else "Roof-Beams"
    tags = [(0, "POLYLINE"), (5, "A1"), (8, layer), (70, flags), (66, 1)]
    for point in points:
        tags.extend([(0, "VERTEX"), (8, layer), (70, vertex_flags), *zip((10, 20, 30), point)])
    tags.append((0, "SEQEND"))
    if nested:
        return drawing(tags)
    records = [(0, "SECTION"), (2, "HEADER"), (9, "$INSUNITS"), (70, 4), (0, "ENDSEC"),
               (0, "SECTION"), (2, "ENTITIES"), *tags, (0, "ENDSEC"), (0, "EOF")]
    return "".join(f"{code}\n{value}\n" for code, value in records).encode()


@pytest.mark.parametrize("ask,points,closed,factor", CASES)
@pytest.mark.parametrize("nested", [False, True])
def test_spatial_edges(ask, points, closed, factor, nested):
    _, source, _ = author_tool(ask)
    flags = 8 | int(closed)
    status, payload = invoke(source, render(points, nested, flags, 32))
    assert status == "ok", payload
    vertices = [placed(v) if nested else v for v in points]
    ends = vertices[1:] + (vertices[:1] if closed else [])
    expected = math.fsum(math.dist(a, b) for a, b in zip(vertices, ends)) * factor
    assert payload[0]["lengths_by_layer"] == {"Roof-Beams": pytest.approx(expected, rel=1e-6)}
    for bad_flags, vertex_flags in [(flags | 16, 32), (flags | 4, 32), (flags, 32 | 128)]:
        status, error = invoke(source, render(points, nested, bad_flags, vertex_flags))
        assert status == "tool_error" and "flag" in str(error).lower(), error
