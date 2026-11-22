"""Independent rectangular intersections on a shared panel definition and nested rack."""
from fractions import Fraction

import pytest

from test_x03_oblique_spline_author import section
from test_x03_offline_drawing_author import invoke, tools_fallback


ASKS = [
    ("Report the clipped area of each panel separately for the repeated racks.", .5),
    ("Measure retained footprint per PV module under the distinct instance clipping boundaries.", 1),
    ("List visible area for every panel in each XCLIP placement.", 1.5),
]


def drawing(limit,reverse=False,duplicate=False):
    def block(name,members):
        return [(0,"BLOCK"),(2,name),(10,0),(20,0),*members,(0,"ENDBLK")]

    def insert(handle,name,x,sx=1,ext=None):
        tags = [(0,"INSERT"),(5,handle),(2,name),(8,"Roof" if ext else "0"),(10,x),(20,0),(41,sx)]
        return tags + ([(102,"{ACAD_XDICTIONARY"),(360,ext),(102,"}")] if ext else [])

    def objects(prefix,owner,left,right):
        identity=(1,0,0,0,0,1,0,0,0,0,1,0)
        def spatial(right):
            return [(0,"SPATIAL_FILTER"),(5,prefix+"2"),(330,prefix+"1"),(70,2),
                    (10,left),(20,.5),(10,right),(20,2),(71,1),*((40,v) for v in identity*2)]
        return ([(0,"DICTIONARY"),(5,prefix+"0"),(330,owner),(3,"ACAD_FILTER"),(360,prefix+"1"),
                 (0,"DICTIONARY"),(5,prefix+"1"),(330,prefix+"0"),(3,"SPATIAL"),(360,prefix+"2")]
                + spatial(right) + (spatial(right+2) if duplicate and owner=="B2" else []))

    outline = [(0,"LWPOLYLINE"),(5,"E1"),(70,1),(90,4)]
    for x,y in ((0,0),(2,0),(2,2),(0,2)):
        outline.extend([(10,x),(20,y)])
    blocks = block("PV Panel",outline)+block("Rack",insert("C1","PV Panel",0)+insert("C2","PV Panel",2))
    roots = [insert("B1","Rack",0,2,"A0"),insert("B2","Rack",20,-1,"D0")]
    clips = [objects("A","B1",0,limit),objects("D","B2",1,4)]
    if reverse:
        roots.reverse()
        clips.reverse()
    tags=section("BLOCKS",blocks)+section("ENTITIES",sum(roots,[]))+section("OBJECTS",sum(clips,[]))+[(0,"EOF")]
    return "".join(f"{c}\n{v}\n" for c,v in tags).encode()


@pytest.mark.parametrize("ask,limit",ASKS)
def test_distinct_clips_on_shared_definition(ask,limit):
    _,source,_ = tools_fallback.author_tool(ask)
    expected = {}
    for handle,lo,hi,scale in (("B1",0,Fraction(limit),2),("B2",1,4,1)):
        for i,x in enumerate((0,2),start=1):
            width = max(0,min(x+2,hi)-max(x,lo))
            expected[f"{handle}[0,0]/C{i}[0,0]"] = Fraction(3,2)*width*scale
    for reverse in (False,True):
        status,payload = invoke(source,drawing(limit,reverse))
        assert status == "ok",payload
        result=payload[0]
        assert result["area_by_placement"] == expected
        assert result["area_by_layer"] == {"Roof":sum(expected.values())}
        assert result["visible_panels"] == sum(v>0 for v in expected.values())


def test_conflicting_filter_records_cannot_override_one_instance():
    _,source,_ = tools_fallback.author_tool(ASKS[0][0])
    for reverse in (False,True):
        status,payload = invoke(source,drawing(1,reverse,duplicate=True))
        assert status == "tool_error" and "clip" in str(payload).lower(),payload
