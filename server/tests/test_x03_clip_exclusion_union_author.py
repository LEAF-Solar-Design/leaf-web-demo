"""Disjoint area accounting for intersecting nested inverted spatial filters."""
import itertools

import pytest

from test_x03_clip_holes_author import geometry
from test_x03_offline_drawing_author import invoke, tools_fallback


ASKS = [
    ("Break down how much area each overlapping cutout removes without counting their overlap twice.",
     (1, 0, 5, 4), (3, 1, 7, 4)),
    ("Report the clipped area of each panel after applying both overlapping exclusion loops.",
     (0, 0, 3, 8), (2, 0, 4, 8)),
    ("Sum panel area by roof after subtracting the union of the nested cutouts.",
     (1, 1, 7, 4), (3, 2, 5, 3)),
]


def rect(box):
    x,y,u,v = box
    return [(x,y), (u,y), (u,v), (x,v)]


def intersect(*boxes):
    left = max(b[0] for b in boxes)
    bottom = max(b[1] for b in boxes)
    right = min(b[2] for b in boxes)
    top = min(b[3] for b in boxes)
    return max(0, right-left)*max(0, top-bottom)


@pytest.mark.parametrize("ask,a,b", ASKS)
def test_overlapping_cutouts(ask,a,b):
    _, source, _ = tools_fallback.author_tool(ask)
    outer_rects = ((0,0,4,8), (4,0,8,4))
    per_panel = {}
    exclusive_a = exclusive_b = shared = 0
    for i in range(3):
        panel = (4*i,0,4*(i+1),8)
        outer = sum(intersect(panel, r) for r in outer_rects)
        inside_a = sum(intersect(panel, r, a) for r in outer_rects)
        inside_b = sum(intersect(panel, r, b) for r in outer_rects)
        overlap = sum(intersect(panel, r, a, b) for r in outer_rects)
        exclusive_a += inside_a - overlap
        exclusive_b += inside_b - overlap
        shared += overlap
        per_panel[f"F1[0,0]/C1[0,0]/B1[0,0]/A{i+1}[0,0]"] = outer - inside_a - inside_b + overlap
    assert shared > 0
    for first,second in itertools.permutations((a,b)):
        status,payload = invoke(source, geometry(holes=(rect(first),rect(second))))
        assert status == "ok", payload
        result = payload[0]
        assert result["area_by_layer"] == {"West roof": sum(per_panel.values())}
        assert result["visible_panels"] == sum(v > 0 for v in per_panel.values())
        if "each panel" in ask:
            assert result["area_by_placement"] == per_panel
            assert min(result["area_by_placement"].values()) >= 0
        if "each overlapping cutout" in ask:
            paths = ("F1[0,0]/C1[0,0]", "F1[0,0]/C1[0,0]/B1[0,0]")
            ea,eb = (exclusive_a,exclusive_b) if first == a else (exclusive_b,exclusive_a)
            groups = {tuple(g["cutout_paths"]):g["area"] for g in result["removed_area_groups"]}
            assert len(groups) == len(result["removed_area_groups"])
            assert groups == {(paths[0],):ea, (paths[1],):eb, paths:shared}
            assert sum(groups.values()) + sum(result["area_by_layer"].values()) == 48
