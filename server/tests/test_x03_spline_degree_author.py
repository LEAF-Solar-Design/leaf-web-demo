"""Malformed degree and counts must fail before length work or partial reporting."""
import math

import pytest

from test_x03_oblique_spline_author import placed_dxf, spline_record
from test_x03_offline_drawing_author import invoke, tools_fallback


ASKS = [
    ("Measure the spline beam length by layer and check the degree.",71,10**100,"degree",(2,1),1),
    ("List each NURBS beam length in centimetres after checking its knot count.",72,7,"knot",(3,2),.1),
    ("How long in metres is each spline beam with its declared control point count?",73,4,"control",(1,4),.001),
]


def arc(handle="A1"):
    return spline_record([(5,0,0),(5,5,0),(0,5,0)], [0]*3+[1]*3,
                         [1,math.sqrt(.5),1], (0,0,1),handle)


@pytest.mark.parametrize("ask,code,bad,reason,scale,factor",ASKS)
def test_bad_counts_and_sound_control(ask,code,bad,reason,scale,factor):
    # Zero allowed blends or projections makes an early validation error
    # distinguishable from entering the numerical solver.
    _,bounded,_=tools_fallback.author_tool(ask+" Use at most 0 evaluations.")
    bad_record=[(c,bad if c==code else v) for c,v in arc()]
    for source,records in [(bounded,bad_record),
                           (tools_fallback.author_tool(ask)[1],arc()+[(c,"A2" if c==5 else v) for c,v in bad_record])]:
        status,payload=invoke(source,placed_dxf(records,(*scale,1)))
        assert status == "tool_error" and reason in str(payload).lower(),payload
        assert "budget" not in str(payload).lower() and "lengths_by_layer" not in str(payload)
    # Conflicting repeated declarations may appear in either order.
    for order in (False,True):
        records=[]
        for c,v in arc():
            records.extend(([(c,bad),(c,v)] if order else [(c,v),(c,bad)]) if c==code else [(c,v)])
        status,payload=invoke(bounded,placed_dxf(records,(*scale,1)))
        assert status == "tool_error" and reason in str(payload).lower() and "budget" not in str(payload).lower()
    source=tools_fallback.author_tool(ask)[1]
    status,payload=invoke(source,placed_dxf(arc(),(*scale,1)))
    assert status == "ok",payload
    a,b=5*scale[0],5*scale[1]
    h=math.pi/(2*8192)
    expected=factor*h*math.fsum(math.hypot(a*math.sin((i+.5)*h),b*math.cos((i+.5)*h)) for i in range(8192))
    oracle_error=factor*(math.pi/2)*h*h*(max(a,b)+max(a,b)**2/min(a,b))/24
    result=payload[0]
    tolerance=result["length_relative_tolerance"]
    assert 0<tolerance<=1e-5
    assert abs(result["lengths_by_layer"]["Steel-Beams"]-expected) <= tolerance*expected+oracle_error
