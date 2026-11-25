"""Count panel insertion points using real DXF spatial filters on nested INSERTs."""
from collections import Counter

import pytest

from test_x03_offline_drawing_author import invoke
from tools_fallback import author_tool


ASKS = [
    "Count panels inside clipped block references, counting insertion points on clip edges.",
    "How many PV modules survive the standard and inverted XCLIP boundaries in nested inserts?",
    "Tally panel insertion points by layer after applying nested block clipping boundaries.",
]


def drawing(broken=False):
    def section(name, data):
        return [(0, "SECTION"), (2, name), *data, (0, "ENDSEC")]

    def block(name, data):
        return [(0, "BLOCK"), (2, name), (10, 0), (20, 0), *data, (0, "ENDBLK")]

    def extension(handle):
        return [(102, "{ACAD_XDICTIONARY"), (360, handle), (102, "}")]

    def insert(handle, name, layer, x, y, ext=None, cols=1, rows=1, scale=1, angle=0):
        return [(0, "INSERT"), (5, handle), *(extension(ext) if ext else []), (2, name),
                (8, layer), (10, x), (20, y), (41, scale), (50, angle),
                (70, cols), (71, rows), (44, 1), (45, 1)]

    def dictionary(handle, owner, key, ref):
        return [(0, "DICTIONARY"), (5, handle), (330, owner), (100, "AcDbDictionary"),
                (3, key), (360, ref)]

    def vertices(points):
        return [pair for x, y in points for pair in ((10, x), (20, y), (30, 0))]

    def spatial(handle, owner, points, inverted=None):
        matrix = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0)
        return [(0, "SPATIAL_FILTER"), (5, handle), *(extension(inverted) if inverted else []),
                (330, owner), (100, "AcDbFilter"), (100, "AcDbSpatialFilter"),
                (70, len(points)), *[v for v in vertices(points) if v[0] != 30],
                (210, 0), (220, 0), (230, 1), (11, 0), (21, 0), (31, 0),
                (71, 1), (72, 0), (73, 0), *((40, x) for x in matrix*2)]

    hole = [(1, 0), (3, 0), (3, 2), (1, 2)]
    # The compare path connects the inner boundary to a surrounding extent.
    compare = [*hole, hole[0], (-1, -1), (-1, 4), (6, 4), (6, -1), (-1, -1), hole[0]]
    objects = (dictionary("91", "B1", "ACAD_FILTER", "92")
               + dictionary("92", "91", "SPATIAL", "99" if broken else "93")
               + spatial("93", "92", [(2, 0), (8, 2)])
               + dictionary("81", "C1", "ACAD_FILTER", "82")
               + dictionary("82", "81", "SPATIAL", "83")
               + spatial("83", "82", compare, "84")
               + dictionary("84", "83", "ACAD_XREC_ROUNDTRIP", "85")
               + [(0, "XRECORD"), (5, "85"), (330, "84"), (100, "AcDbXrecord"), (280, 1),
                  (102, "ACAD_INVERTEDCLIP_ROUNDTRIP"), *vertices(hole),
                  (102, "ACAD_INVERTEDCLIP_ROUNDTRIP_COMPARE"), *vertices(compare)])
    blocks = (block("PV Module", [(0, "POINT"), (10, 0), (20, 0)])
              + block("Rack", insert("D1", "PV Module", "0", 0, 0, cols=5, rows=3))
              + block("Array", insert("C1", "Rack", "0", 2, 0, "81", scale=2)))
    records = (section("BLOCKS", blocks) + section("ENTITIES", insert("B1", "Array", "Roof", 37, 19, "91", angle=90))
               + section("OBJECTS", objects) + [(0, "EOF")])
    return "".join(f"{code}\n{value}\n" for code, value in records).encode()


@pytest.mark.parametrize("ask", ASKS)
def test_clipped_panel_insertions(ask):
    _, source, _ = author_tool(ask)
    status, payload = invoke(source, drawing())
    assert status == "ok", payload
    # Each module origin starts in Rack coordinates. First omit strict hole
    # interiors, then transform into Array coordinates for its inclusive clip.
    expected = Counter()
    for y in range(3):
        for x in range(5):
            if not (1 < x < 3 and 0 < y < 2) and 2 <= 2 + 2*x <= 8 and 0 <= y <= 2:
                expected["Roof"] += 1
    assert payload[0]["total"] == sum(expected.values()) == 11
    assert payload[0]["counts_by_layer"] == dict(expected)
    assert payload[0]["boundary_policy"] == "include panel insertion points on clip edges"


def test_unresolved_spatial_filter_refuses_count():
    _, source, _ = author_tool(ASKS[0])
    status, payload = invoke(source, drawing(broken=True))
    assert status == "tool_error" and "clip" in str(payload).lower(), payload
