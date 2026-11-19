"""Exact rectangular overlap oracle for clipped panel boundary contacts."""
from fractions import Fraction

import pytest

from test_x03_offline_drawing_author import invoke
from tools_fallback import author_tool


CASES = [
    ("Report the clipped area of each panel that meets the boundary along an edge.", 1, 0),
    ("Measure the visible footprint area per PV module at the corners of the clipping box.", 1, 1),
    ("Sum the clipped area per panel, including narrow retained slivers.", 1 - Fraction(1, 2**53), 0),
]


def drawing(xmin, ymin):
    tags = []

    def record(kind, *fields):
        tags.extend([(0, kind), *fields])

    def coords(points):
        return [tag for x, y in points for tag in ((10, float(x)), (20, float(y)))]

    record("SECTION", (2, "BLOCKS"))
    record("BLOCK", (2, "PV panel"), (10, 0), (20, 0))
    record("LWPOLYLINE", (5, "A0"), (90, 4), (70, 1),
           *coords([(0, 0), (1, 0), (1, 1), (0, 1)]))
    record("ENDBLK")
    record("ENDSEC")
    record("SECTION", (2, "ENTITIES"))
    record("INSERT", (5, "A1"), (2, "PV panel"), (8, "Roof"),
           (10, 1000000), (20, 2000000), (41, 2), (42, 3),
           (102, "{ACAD_XDICTIONARY"), (360, "D0"), (102, "}"))
    record("ENDSEC")
    record("SECTION", (2, "OBJECTS"))
    record("DICTIONARY", (5, "D0"), (330, "A1"), (3, "ACAD_FILTER"), (360, "D1"))
    record("DICTIONARY", (5, "D1"), (330, "D0"), (3, "SPATIAL"), (360, "D2"))
    matrix = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0)
    record("SPATIAL_FILTER", (5, "D2"), (330, "D1"), (70, 4),
           *coords([(xmin, ymin), (2, ymin), (2, 2), (xmin, 2)]),
           (71, 1), *((40, v) for v in matrix*2))
    record("ENDSEC")
    record("EOF")
    return "".join(f"{code}\n{value}\n" for code, value in tags).encode()


@pytest.mark.parametrize("ask,xmin,ymin", CASES)
def test_boundary_contact_area(ask, xmin, ymin):
    _, source, _ = author_tool(ask)
    status, payload = invoke(source, drawing(xmin, ymin))
    assert status == "ok", payload
    result = payload[0]
    expected = Fraction(1 - xmin) * (1 - ymin) * 6
    assert Fraction(result["area_by_placement"]["A1[0,0]"]) == expected
    assert result["visible_panels"] == int(expected > 0)
    assert result["area_by_layer"] == ({"Roof": expected} if expected else {})
    assert result["area_units"] == "square drawing units"
    assert "edge" in result["boundary_policy"] and "zero" in result["boundary_policy"]
