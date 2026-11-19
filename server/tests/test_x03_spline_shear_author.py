"""World tangent integration through two noncommuting scaled, rotated INSERTs."""
import math

import pytest

from test_x03_oblique_spline_author import section, spline_record
from test_x03_offline_drawing_author import invoke, tools_fallback


ASKS = [
    ("Measure the spline beam length under the nested shear transform.", (2,1), 31, (1,4), -23, 1),
    ("List each NURBS beam length in centimetres through the rotated unequal scales.", (-3,1), -38, (2,5), 17, .1),
    ("How long in metres is each spline beam after the nested block placements?", (1,4), 57, (6,.5), 41, .001),
]


def drawing(inner,theta,outer,phi):
    arc = spline_record([(5,0,0),(5,5,0),(0,5,0)],[0]*3+[1]*3,[1,math.sqrt(.5),1],(0,0,1))
    def block(name,members):
        return [(0,"BLOCK"),(2,name),(10,3),(20,-4),*members,(0,"ENDBLK")]
    def insert(handle,name,layer,scale,angle):
        return [(0,"INSERT"),(5,handle),(2,name),(8,layer),(10,-8),(20,9),
                (41,scale[0]),(42,scale[1]),(50,angle)]
    blocks = block("Shape",arc) + block("Rack",insert("C1","Shape","0",inner,theta))
    tags = (section("HEADER",[(9,"$INSUNITS"),(70,4)])+section("BLOCKS",blocks)
            +section("ENTITIES",insert("B1","Rack","Steel-Beams",outer,phi))+[(0,"EOF")])
    return "".join(f"{c}\n{v}\n" for c,v in tags).encode()


@pytest.mark.parametrize("ask,inner,theta,outer,phi,factor",ASKS)
def test_sheared_quarter_circle_speed(ask,inner,theta,outer,phi,factor):
    def transform(v,scale,angle):
        c,s = math.cos(math.radians(angle)),math.sin(math.radians(angle))
        x,y = v
        return (c*scale[0]*x-s*scale[1]*y, s*scale[0]*x+c*scale[1]*y)
    def world(v):
        return transform(transform(v,inner,theta),outer,phi)
    u,v = world((1,0)),world((0,1))
    assert abs(sum(a*b for a,b in zip(u,v))) > .01*math.hypot(*u)*math.hypot(*v)
    n = 16384
    h = math.pi/(2*n)
    expected = factor*h*math.fsum(math.hypot(*world((-5*math.sin((i+.5)*h),5*math.cos((i+.5)*h))))
                                  for i in range(n))
    # Singular value bounds from the product of each factor's extreme scales.
    maximum = 5*max(map(abs,inner))*max(map(abs,outer))
    minimum = 5*min(map(abs,inner))*min(map(abs,outer))
    bound = factor*(math.pi/2)*h*h*(maximum+maximum**2/minimum)/24
    _, source, _ = tools_fallback.author_tool(ask)
    status,payload = invoke(source,drawing(inner,theta,outer,phi))
    assert status == "ok", payload
    result = payload[0]
    tolerance = result["length_relative_tolerance"]
    assert 0 < tolerance <= 1e-5
    measured = result["lengths_by_layer"]["Steel-Beams"]
    assert abs(measured-expected) <= tolerance*expected + bound
    if "each" in ask:
        assert result["lengths_by_handle"]["B1/C1/D1"][result["units"]] == measured


def test_collapsed_placement_cannot_report_a_zero_length_beam():
    ask,inner,theta,_,phi,_ = ASKS[0]
    _, source, _ = tools_fallback.author_tool(ask)
    status,payload = invoke(source,drawing(inner,theta,(0,0),phi))
    assert status == "tool_error" and "collapsed spline" in str(payload).lower(), payload
