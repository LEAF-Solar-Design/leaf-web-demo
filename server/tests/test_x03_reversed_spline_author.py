"""Direction reversal of an asymmetrically refined rational quarter ellipse."""
import math

import pytest

from test_x03_oblique_spline_author import section, spline_record
from test_x03_offline_drawing_author import invoke, tools_fallback
from test_x03_spline_refinement_author import blend


ASKS = [
    ("Measure each spline beam length and report its endpoints in parameter order.", 2, 3, 1),
    ("List NURBS beam lengths in centimetres and show the direction of each curve.", -3, 1, .1),
    ("How long in metres is each reversed spline beam and what is its endpoint orientation?", 1, 4, .001),
]


def drawing(reverse, sx, sy):
    # Insert knot 1/4 on the base quarter circle so reversing the knot
    # sequence and weights is observable even though the base weights agree.
    w = math.sqrt(.5)
    p, q, r = (5,0,1), (5*w,5*w,w), (0,5,1)
    controls = [p, blend(p,q,.25), blend(q,r,.25), r]
    knots = [4,4,4,5,8,8,8]
    if reverse:
        controls.reverse()
        knots = [12-k for k in reversed(knots)]
    curve = spline_record([(x/v,y/v,0) for x,y,v in controls], knots, [v for _,_,v in controls], (0,0,1))
    block = [(0,"BLOCK"), (2,"Member"), (10,1), (20,2), *curve, (0,"ENDBLK")]
    insert = [(0,"INSERT"), (5,"C1"), (2,"Member"), (8,"Steel-Beams"), (10,11), (20,17),
              (41,sx), (42,sy)]
    tags = (section("HEADER", [(9,"$INSUNITS"),(70,4)]) + section("BLOCKS",block)
            + section("ENTITIES",insert) + [(0,"EOF")])
    return "".join(f"{c}\n{v}\n" for c,v in tags).encode()


@pytest.mark.parametrize("ask,sx,sy,factor", ASKS)
def test_reversal_changes_orientation_only(ask,sx,sy,factor):
    _, source, _ = tools_fallback.author_tool(ask)
    a,b = 5*abs(sx), 5*abs(sy)
    h = (math.pi/2)/8192
    expected = factor*h*math.fsum(math.hypot(a*math.sin((i+.5)*h), b*math.cos((i+.5)*h))
                                  for i in range(8192))
    # Midpoint remainder from |speed''| <= max axis + max axis squared / min axis.
    bound = factor*(math.pi/2)*h*h*(max(a,b)+max(a,b)**2/min(a,b))/24
    start, end = [11+4*sx,17-2*sy,0], [11-sx,17+3*sy,0]
    lengths = []
    for reverse in (False,True):
        status,payload = invoke(source, drawing(reverse,sx,sy))
        assert status == "ok", payload
        result = payload[0]
        assert result["endpoint_units"] == "drawing_units"
        member = result["lengths_by_handle"]["C1/D1"]
        assert list(member["ends"][0]) == pytest.approx(end if reverse else start)
        assert list(member["ends"][1]) == pytest.approx(start if reverse else end)
        length = member[result["units"]]
        tolerance = result["length_relative_tolerance"]
        assert 0 < tolerance <= 1e-5
        assert abs(length - expected) <= bound + expected*tolerance
        lengths.append(length)
    assert lengths[0] == pytest.approx(lengths[1],rel=1e-5)
