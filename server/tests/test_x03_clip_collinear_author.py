"""Collinear vertices and closure copies preserve the physical footprint and holes."""
from fractions import Fraction

import pytest

from test_x03_clip_exclusion_union_author import intersect, rect
from test_x03_clip_holes_author import geometry
from test_x03_offline_drawing_author import invoke, tools_fallback


ASKS = [
    ("Report the clipped area of each panel with redundant collinear boundary vertices.", 0),
    ("Measure the visible footprint per PV module after the cutouts with repeated closure points.", 1),
    ("Sum retained panel area after the two clipping holes with subdivided straight edges.", -1),
]


def redundant(points):
    expanded = []
    for a,b in zip(points,points[1:]+points[:1]):
        expanded.extend([a,tuple((x+y)/2 for x,y in zip(a,b))])
    return expanded+[points[0]]*3


@pytest.mark.parametrize("ask,shift",ASKS)
def test_equivalent_holes(ask,shift):
    _, source, _ = tools_fallback.author_tool(ask)
    boxes = [(Fraction(1,2),1,3,3),(2,Fraction(3,2),5,4)]
    holes = [rect(tuple(map(float,box))) for box in boxes]
    expected = {}
    for i in range(3):
        panel = (shift+4*i,0,shift+4*i+4,8)
        area = sum(intersect(panel,r) - intersect(panel,r,boxes[0]) - intersect(panel,r,boxes[1])
                   + intersect(panel,r,*boxes) for r in [(0,0,4,8),(4,0,8,4)])
        expected[f"F1[0,0]/C1[0,0]/B1[0,0]/A{i+1}[0,0]"] = area
    variants = [holes,[redundant(h) for h in holes],[redundant(h[::-1]) for h in holes]]
    for shape in variants:
        status,payload = invoke(source,geometry(shift=shift,holes=shape))
        assert status == "ok", payload
        result = payload[0]
        assert result["area_by_layer"] == {"West roof":sum(expected.values())}
        assert result["visible_panels"] == sum(v>0 for v in expected.values())
        if "each panel" in ask or "per PV module" in ask:
            assert result["area_by_placement"] == expected


@pytest.mark.parametrize("bad", [
    [(0,0),(1,0),(2,0),(0,0),(0,0)],
    [(1,1),(3,1),(2,1),(2,3),(1,3),(1,1),(1,1)],
])
def test_collapsed_or_retraced_hole_is_refused(bad):
    _, source, _ = tools_fallback.author_tool(ASKS[0][0])
    status,payload = invoke(source,geometry(holes=(bad,rect((3,2,5,3)))))
    assert status == "tool_error" and "clip" in str(payload).lower(), payload
