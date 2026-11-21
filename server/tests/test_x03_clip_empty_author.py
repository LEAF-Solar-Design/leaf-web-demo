"""Nested rectangular clips can have disjoint interiors despite each cutting a panel."""
import pytest

from test_x03_oblique_spline_author import section
from test_x03_offline_drawing_author import invoke, tools_fallback


CASES = [
    ("Report the clipped area of each panel across the two nested boundaries.", (2, 0, 3, 3)),
    ("Measure retained footprint per PV module after both rack clipping windows.", (0, 2, 3, 3)),
    ("List visible area per panel where the two XCLIP windows meet.", (1, 1, 3, 3)),
]


def drawing(inner, reverse=False, missing=False):
    def block(name, contents):
        return [(0, "BLOCK"), (2, name), (10, 0), (20, 0), *contents, (0, "ENDBLK")]

    def insert(handle, name, layer, y, dictionary=None):
        tags = [(0, "INSERT"), (5, handle), (2, name), (8, layer), (10, 0), (20, y)]
        if dictionary:
            tags += [(102, "{ACAD_XDICTIONARY"), (360, dictionary), (102, "}")]
        return tags

    def clip(prefix, owner, box):
        x0, y0, x1, y1 = box
        identity = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0)
        return [(0, "DICTIONARY"), (5, prefix+"0"), (330, owner), (3, "ACAD_FILTER"), (360, prefix+"1"),
                (0, "DICTIONARY"), (5, prefix+"1"), (330, prefix+"0"), (3, "SPATIAL"), (360, prefix+"2"),
                (0, "SPATIAL_FILTER"), (5, prefix+"2"), (330, prefix+"1"), (70, 2),
                (10, x0), (20, y0), (10, x1), (20, y1), (71, 1), *((40, n) for n in identity*2)]

    outline = [(0, "LWPOLYLINE"), (5, "A0"), (70, 1), (90, 4)]
    for x, y in ((0, 0), (3, 0), (3, 3), (0, 3)):
        outline.extend([(10, x), (20, y)])
    blocks = block("PV panel", outline)
    blocks += block("Row", insert("B1", "PV panel", "0", 0))
    blocks += block("Rack", insert("C1", "Row", "0", 0, "D0"))
    roots = [insert("F1", "Rack", "East", 0, "90"), insert("F2", "Rack", "West", 10, "80")]
    objects = [clip("9", "F1", (0, 0, 1, 1)), clip("8", "F2", (0, 0, 3, 3))]
    if not missing:
        objects.append(clip("D", "C1", inner))
    if reverse:
        roots.reverse()
        objects.reverse()
    tags = section("BLOCKS", blocks) + section("ENTITIES", sum(roots, []))
    tags += section("OBJECTS", sum(objects, [])) + [(0, "EOF")]
    return "".join(f"{c}\n{v}\n" for c, v in tags).encode()


@pytest.mark.parametrize("ask,inner", CASES)
def test_empty_intersection_and_visible_control(ask, inner):
    _, source, _ = tools_fallback.author_tool(ask)
    x0, y0, x1, y1 = inner
    east = max(0, min(1, x1)-x0) * max(0, min(1, y1)-y0)
    west = (x1-x0) * (y1-y0)
    assert east == 0 and west > 0
    for reverse in (False, True):
        status, payload = invoke(source, drawing(inner, reverse))
        assert status == "ok", payload
        result = payload[0]
        assert result["area_by_placement"] == {
            "F1[0,0]/C1[0,0]/B1[0,0]": east, "F2[0,0]/C1[0,0]/B1[0,0]": west}
        assert result["area_by_layer"] == {"West": west}
        assert result["visible_panels"] == 1
        status, payload = invoke(source, drawing(inner, reverse, missing=True))
        assert status == "tool_error" and "clip" in str(payload).lower(), payload
