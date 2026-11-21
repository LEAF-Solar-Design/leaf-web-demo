"""Equivalent nested INSERT/clip coordinate frames with exact rectangle intersections."""
from fractions import Fraction

import pytest

from test_x03_oblique_spline_author import section
from test_x03_offline_drawing_author import invoke, tools_fallback


ASKS = [
    "Report the clipped area of each panel after regrouping the nested rack blocks.",
    "Measure retained footprint per PV module after applying the rack clipping boundary.",
    "Sum visible panel areas by layer across the regrouped XCLIP placements.",
]


def drawing(grouped, upper, ambiguous=False):
    def block(name, contents):
        return [(0,"BLOCK"), (2,name), (10,0), (20,0), *contents, (0,"ENDBLK")]

    def insert(handle, name, layer, x, y, sx=1, sy=1, clipped=False, copies=1):
        ext = [(102,"{ACAD_XDICTIONARY"), (360,"A0"), (102,"}")] if clipped else []
        return [(0,"INSERT"), (5,handle), (2,name), (8,layer), (10,x), (20,y),
                (41,sx), (42,sy), (70,copies), (44,80), *ext]

    outline = [(0,"LWPOLYLINE"), (5,"EA"), (90,4), (70,1)]
    for x,y in [(0,0),(2,0),(2,2),(0,2)]:
        outline += [(10,x),(20,y)]
    blocks = block("PV Module", outline)
    if grouped:
        # Pull the clip and module placements back through (10-2x, 1+3y).
        members = insert("C1","PV Module","0",0,0) + insert("C2","PV Module","0",1,1)
        blocks += block("Bay",members)
        blocks += block("Rack",insert("B2","Bay","0",10,1,-2,3,clipped=True))
        owner = "B2"
        bounds = (0, .5, upper, 2.5)
    else:
        members = insert("C1","PV Module","0",10,1,-2,3) + insert("C2","PV Module","0",8,4,-2,3)
        blocks += block("Rack",members)
        owner = "B1"
        bounds = (10-2*upper, 2.5, 10, 8.5)
    roots = insert("B1","Rack","Roof",30,40,5,2,clipped=not grouped,copies=2)
    matrix = (1,0,0,0,0,1,0,0,0,0,1,0)
    x,y,u,v = bounds
    duplicate = [(3,"SPATIAL"),(360,"A3")] if ambiguous else []
    def spatial(handle, dx):
        return [(0,"SPATIAL_FILTER"), (5,handle), (330,"A1"), (70,2),
                (10,x+dx), (20,y), (10,u+dx), (20,v), (71,1), *((40,x) for x in matrix*2)]
    objects = [(0,"DICTIONARY"), (5,"A0"), (330,owner), (3,"ACAD_FILTER"), (360,"A1"),
               (0,"DICTIONARY"), (5,"A1"), (330,"A0"), (3,"SPATIAL"), (360,"A2"), *duplicate,
               *spatial("A2",0)]
    if ambiguous:
        objects += spatial("A3",100)
    tags = section("BLOCKS",blocks)+section("ENTITIES",roots)+section("OBJECTS",objects)+[(0,"EOF")]
    return "".join(f"{c}\n{v}\n" for c,v in tags).encode()


@pytest.mark.parametrize("ask,upper", [(ASKS[0],1.5), (ASKS[1],2), (ASKS[2],2.5)])
def test_regroup_preserves_world_areas_and_count(ask,upper):
    _, source, _ = tools_fallback.author_tool(ask)
    upper = Fraction(upper)
    # Intersection in bay coordinates. The absolute world determinant is
    # |(-2)*3*5*2|, independent of which block owns the clip.
    expected = []
    for x,y in [(0,0),(1,1)]:
        width = max(0,min(x+2,upper)-max(x,0))
        height = max(0,min(y+2,Fraction(5,2))-max(y,Fraction(1,2)))
        expected.append(float(60*width*height))
    for grouped in (False,True):
        status,payload = invoke(source,drawing(grouped,float(upper)))
        assert status == "ok", payload
        result = payload[0]
        assert result["area_by_layer"] == pytest.approx({"Roof":2*sum(expected)})
        assert result["visible_panels"] == 4
        if "each panel" in ask or "per PV module" in ask:
            paths = {f"B1[0,{col}]/" + ("B2[0,0]/" if grouped else "") + f"C{i+1}[0,0]": area
                     for col in (0,1) for i,area in enumerate(expected)}
            assert result["area_by_placement"] == pytest.approx(paths)


def test_ambiguous_spatial_filter_identity_refuses_regrouped_area():
    _, source, _ = tools_fallback.author_tool(ASKS[0])
    status,payload = invoke(source,drawing(True,2,ambiguous=True))
    assert status == "tool_error" and "ambiguous clip" in str(payload).lower(), payload
