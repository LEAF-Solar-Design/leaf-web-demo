"""Opposing concave clips share only their separated rectangular teeth."""
from fractions import Fraction

import pytest

from test_x03_oblique_spline_author import section
from test_x03_offline_drawing_author import invoke, tools_fallback


CASES = [
    ("Report the clipped area of each panel within the split rack windows.", [(0, 1), (4, 7)]),
    ("Measure retained footprint per PV module across disconnected clipping regions.", [(0, .5), (2, 3), (5, 7)]),
    ("List visible area per panel after intersecting both concave XCLIP boundaries.", [(0, 2), (3, 4), (6, 6.25), (7, 9)]),
]


def drawing(teeth, swap, reverse):
    height = 5
    # The spine joins all fingers in one clip. Its reflection joins them in
    # the other clip. Between fingers their two spines have disjoint y ranges.
    lower = [(0, 0), (teeth[-1][1], 0)]
    for i in reversed(range(len(teeth))):
        lo, hi = teeth[i]
        lower.extend([(hi, height), (lo, height)])
        if i:
            lower.extend([(lo, 1), (teeth[i-1][1], 1)])
    upper = [(x, height-y) for x, y in lower]

    def coords(vertices):
        return [tag for x, y in vertices for tag in ((10, x), (20, y))]

    def block(name, members):
        return [(0, "BLOCK"), (2, name), (10, 0), (20, 0), *members, (0, "ENDBLK")]

    def insert(handle, name, layer, clip=None):
        tags = [(0, "INSERT"), (5, handle), (2, name), (8, layer), (10, 0), (20, 0)]
        if clip:
            tags.extend([(102, "{ACAD_XDICTIONARY"), (360, clip), (102, "}")])
        return tags

    def spatial(prefix, owner, poly):
        poly = poly[::-1] if reverse else poly
        identity = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0)
        return [(0, "DICTIONARY"), (5, prefix+"0"), (330, owner), (3, "ACAD_FILTER"), (360, prefix+"1"),
                (0, "DICTIONARY"), (5, prefix+"1"), (330, prefix+"0"), (3, "SPATIAL"), (360, prefix+"2"),
                (0, "SPATIAL_FILTER"), (5, prefix+"2"), (330, prefix+"1"), (70, len(poly)),
                *coords(poly), (71, 1), *((40, x) for x in identity*2)]

    def panel(name, handle, left, right):
        return block(name, [(0, "LWPOLYLINE"), (5, handle), (70, 1), (90, 4),
                            *coords([(left, 0), (right, 0), (right, height), (left, height)])])

    blocks = panel("PV panel", "A1", 0, teeth[-1][1])
    blocks += panel("PV module", "A2", teeth[0][1], teeth[1][0])
    panels = [insert("B1", "PV panel", "0"), insert("B2", "PV module", "0")]
    if reverse:
        panels.reverse()
    blocks += block("Row", sum(panels, [])) + block("Rack", insert("C1", "Row", "0", "D0"))
    root = insert("F1", "Rack", "Roof", "E0")
    first, second = (upper, lower) if swap else (lower, upper)
    objects = spatial("D", "C1", first) + spatial("E", "F1", second)
    tags = section("BLOCKS", blocks) + section("ENTITIES", root) + section("OBJECTS", objects) + [(0, "EOF")]
    return "".join(f"{code}\n{value}\n" for code, value in tags).encode()


@pytest.mark.parametrize("ask,teeth", CASES)
def test_disconnected_components(ask, teeth):
    _, source, _ = tools_fallback.author_tool(ask)
    exact = sum(5*(Fraction(right)-Fraction(left)) for left, right in teeth)
    for swap in (False, True):
        for reverse in (False, True):
            status, payload = invoke(source, drawing(teeth, swap, reverse))
            assert status == "ok", payload
            result = payload[0]
            assert result["area_by_placement"] == {
                "F1[0,0]/C1[0,0]/B1[0,0]": exact,
                "F1[0,0]/C1[0,0]/B2[0,0]": 0,
            }
            assert result["area_by_layer"] == {"Roof": exact}
            assert result["visible_panels"] == 1
