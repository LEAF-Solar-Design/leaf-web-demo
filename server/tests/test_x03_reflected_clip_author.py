"""Nested reflected placements with independent world-space polygon intersections."""
from fractions import Fraction

import pytest

from test_x03_oblique_spline_author import section
from test_x03_offline_drawing_author import invoke, tools_fallback


CASES = [
    ("Report the clipped area of each panel on the mirrored rack.", 2),
    ("Measure the visible footprint per PV module after both XCLIP filters.", 3),
    ("Sum the exposed area of panels under nested reflected clipping boundaries.", 4),
]
FOOTPRINT = [(0, 0), (4, 0), (0, 4)]


def rectangle(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def drawing(inner_clip, outer_clip, reverse, inner_x=-2, outer_y=3):
    def coords(points):
        return [tag for x,y in (points[::-1] if reverse else points) for tag in ((10,x), (20,y))]

    def insert(handle, name, layer, origin, scale, dictionary):
        return [(0,"INSERT"), (5,handle), (2,name), (8,layer), (10,origin[0]), (20,origin[1]),
                (41,scale[0]), (42,scale[1]), (50,90), (102,"{ACAD_XDICTIONARY"),
                (360,dictionary), (102,"}")]

    def spatial(owner, ext, table, boundary, points):
        identity = (1,0,0,0,0,1,0,0,0,0,1,0)
        return [(0,"DICTIONARY"), (5,ext), (330,owner), (3,"ACAD_FILTER"), (360,table),
                (0,"DICTIONARY"), (5,table), (330,ext), (3,"SPATIAL"), (360,boundary),
                (0,"SPATIAL_FILTER"), (5,boundary), (330,table), (70,4),
                *coords(points), (71,1), *((40,v) for v in identity*2)]

    panel = [(0,"BLOCK"), (2,"PV Panel"), (10,0), (20,0), (0,"LWPOLYLINE"), (5,"E1"),
             (90,3), (70,1), *coords(FOOTPRINT), (0,"ENDBLK")]
    rack = [(0,"BLOCK"), (2,"Rack"), (10,0), (20,0),
            *insert("C1", "PV Panel", "0", (4,6), (inner_x,1.5), "D0"), (0,"ENDBLK")]
    tags = (section("BLOCKS", panel + rack)
            + section("ENTITIES", insert("B1", "Rack", "Roof", (30,20), (-4,outer_y), "A0"))
            + section("OBJECTS", spatial("B1","A0","A1","A2",outer_clip)
                      + spatial("C1","D0","D1","D2",inner_clip)) + [(0,"EOF")])
    return "".join(f"{c}\n{v}\n" for c,v in tags).encode()


def world_area(inner_clip, outer_clip):
    # Quarter turns keep transformed clip rectangles axis-aligned. Clip the
    # transformed triangle by their half planes in exact rational arithmetic.
    def rack(x, y):
        return 30 - 3*y, 20 - 4*x

    def panel(x, y):
        return rack(4 - Fraction(3,2)*y, 6 - 2*x)

    polygon = [panel(*p) for p in FOOTPRINT]
    for points, transform in ((inner_clip, panel), (outer_clip, rack)):
        corners = [transform(*p) for p in points]
        for dim in range(2):
            values = [p[dim] for p in corners]
            for limit, sign in ((min(values), 1), (max(values), -1)):
                kept = []
                for a, b in zip(polygon, polygon[1:]+polygon[:1]):
                    da, db = sign*(a[dim]-limit), sign*(b[dim]-limit)
                    if (da >= 0) != (db >= 0):
                        t = Fraction(da, da-db)
                        kept.append(tuple(x+t*(y-x) for x,y in zip(a,b)))
                    if db >= 0:
                        kept.append(b)
                polygon = kept
    cross = sum(a[0]*b[1]-a[1]*b[0] for a,b in zip(polygon, polygon[1:]+polygon[:1]))
    return abs(cross)/2


@pytest.mark.parametrize("ask,right", CASES)
def test_reflected_clip_area(ask, right):
    _, source, _ = tools_fallback.author_tool(ask)
    inner = rectangle(1, 0, right, 3)
    outer = rectangle(1, -1, 4, 5)
    expected = world_area(inner, outer)
    assert 0 < expected < 4*4/2*36
    for reverse in (False, True):
        status, payload = invoke(source, drawing(inner, outer, reverse))
        assert status == "ok", payload
        result = payload[0]
        assert result["visible_panels"] == 1
        assert result["area_by_layer"] == pytest.approx({"Roof": float(expected)})
        if "each panel" in ask or "per PV module" in ask:
            assert result["area_by_placement"] == pytest.approx({"B1[0,0]/C1[0,0]": float(expected)})


@pytest.mark.parametrize("inner_x,outer_y", [(0, 3), (-2, 0)])
def test_singular_reflection_returns_no_area(inner_x, outer_y):
    _, source, _ = tools_fallback.author_tool(CASES[0][0])
    box = rectangle(0, 0, 4, 4)
    status, payload = invoke(source, drawing(box, box, False, inner_x, outer_y))
    assert status == "tool_error" and "transform" in str(payload).lower(), payload
