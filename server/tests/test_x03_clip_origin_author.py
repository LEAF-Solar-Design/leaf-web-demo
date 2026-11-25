"""Tiny panel/clip intersections checked using rational translated coordinates."""
from decimal import Decimal, localcontext
from fractions import Fraction

import pytest

from test_x03_oblique_spline_author import section
from test_x03_offline_drawing_author import invoke, tools_fallback


ASKS = [
    ("Report the clipped area of each panel near the survey origin.", 1, 2**42),
    ("Measure the tiny visible footprint per PV module at the georeferenced coordinates.", 3, -(2**43)),
    ("Sum panel area after intersecting tiny XCLIP boundaries at the site origin.", 5, 2**42),
]
STEP = Fraction(1,1024)


def drawing(origin,width,collapse=False):
    def coords(vertices):
        with localcontext() as ctx:
            ctx.prec = 64
            def fmt(q):
                q = origin + STEP*q
                return str(Decimal(q.numerator)/Decimal(q.denominator))
            return [tag for x,y in vertices for tag in ((10,fmt(x)), (20,fmt(y)))]

    outline = [(0,"LWPOLYLINE"),(5,"E1"),(70,1),(90,3),*coords([(0,0),(8,0),(0,8)])]
    blocks = [(0,"BLOCK"),(2,"PV Panel"),(10,0),(20,0),*outline,(0,"ENDBLK")]
    root = [(0,"INSERT"),(5,"B1"),(2,"PV Panel"),(8,"Roof"),(10,0),(20,0),
            (102,"{ACAD_XDICTIONARY"),(360,"A0"),(102,"}")]
    right = Fraction(1,2**20) if collapse else width
    clip = coords([(0,0),(right,8)])
    identity = (1,0,0,0,0,1,0,0,0,0,1,0)
    objects = [(0,"DICTIONARY"),(5,"A0"),(330,"B1"),(3,"ACAD_FILTER"),(360,"A1"),
               (0,"DICTIONARY"),(5,"A1"),(330,"A0"),(3,"SPATIAL"),(360,"A2"),
               (0,"SPATIAL_FILTER"),(5,"A2"),(330,"A1"),(70,2),*clip,(71,1),*((40,v) for v in identity*2)]
    tags = section("BLOCKS",blocks)+section("ENTITIES",root)+section("OBJECTS",objects)+[(0,"EOF")]
    return "".join(f"{c}\n{v}\n" for c,v in tags).encode()


@pytest.mark.parametrize("ask,width,site", ASKS)
def test_translated_clip_area(ask,width,site):
    _, source, _ = tools_fallback.author_tool(ask)
    # Integral of the right triangle's height (8-x) over the retained strip.
    exact = STEP**2*(8*width-Fraction(width**2,2))
    for offset in (0,site):
        status,payload = invoke(source,drawing(offset,width))
        assert status == "ok", payload
        result = payload[0]
        assert Fraction(result["area_by_layer"]["Roof"]) == exact
        assert result["visible_panels"] == 1
        if "each panel" in ask or "per PV module" in ask:
            assert Fraction(result["area_by_placement"]["B1[0,0]"]) == exact


def test_source_width_that_collapses_to_one_float_is_refused():
    _, source, _ = tools_fallback.author_tool(ASKS[0][0])
    status,payload = invoke(source,drawing(2**40,1,collapse=True))
    assert status == "tool_error" and "clip" in str(payload).lower(), payload
