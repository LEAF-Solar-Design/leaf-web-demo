"""An exact rational subdivision preserves each span and the total centerline."""
from fractions import Fraction
import math

import pytest

from test_x03_oblique_spline_author import placed_dxf, spline_record
from test_x03_offline_drawing_author import invoke, tools_fallback


ASKS = [
    ("List each spline beam length and the total by layer after subdivision.", (1,3), (2,3,1), 1),
    ("Measure the total length in centimetres of joined NURBS beam segments.", (2,), (-3,2,1), .1),
    ("Report the total spline beam length and its spans in metres.", (1,2,3), (1,4,1), .001),
]


def split(net,t):
    left,right = [net[0]], [net[-1]]
    while len(net) > 1:
        net = [tuple((1-t)*x+t*y for x,y in zip(p,q)) for p,q in zip(net,net[1:])]
        left.append(net[0])
        right.append(net[-1])
    return left, list(reversed(right))


def members(cuts, together):
    # Homogeneous controls trace (2*s, 2*s^2) under s=2*t/(1+t).
    net = [(0,0,1),(2,0,2),(8,8,4)]
    pieces = []
    start = 0
    for t in cuts:
        part,net = split(net,(t-start)/(1-start))
        pieces.append(part)
        start = t
    pieces.append(net)
    edges = [0,*cuts,1]
    if together:
        controls = pieces[0] + [p for piece in pieces[1:] for p in piece[1:]]
        knots = [0]*3+[k for k in cuts for _ in range(2)]+[1]*3
        records = [(controls,knots)]
    else:
        records = [(p,[lo]*3+[hi]*3) for p,lo,hi in zip(pieces,edges,edges[1:])]
    tags = []
    for i,(poles,knots) in enumerate(records):
        tags += spline_record([(float(x/w),float(y/w),0) for x,y,w in poles],
                              list(map(float,knots)),[float(w) for _,_,w in poles],
                              (0,0,1),handle=f"A{i+1}")
    return tags


def integral(t,sx,sy):
    # Exact antiderivative of |(2*sx, 4*sy*s)|, independent of the DXF subdivision.
    s = float(2*t/(1+t))
    a,b = 2*abs(sx),4*abs(sy)
    return (s*math.hypot(a,b*s) + a*a/b*math.asinh(b*s/a))/2


@pytest.mark.parametrize("ask,quarters,scale,factor", ASKS)
def test_subdivision_is_additive(ask,quarters,scale,factor):
    cuts = [Fraction(q,4) for q in quarters]
    edges = [0,*cuts,1]
    expected = [factor*(integral(b,*scale[:2])-integral(a,*scale[:2])) for a,b in zip(edges,edges[1:])]
    _, source, _ = tools_fallback.author_tool(ask)
    for split_at,together in (([],True),(cuts,True),(cuts,False)):
        status,payload = invoke(source,placed_dxf(members(split_at,together),scale))
        assert status == "ok", payload
        result = payload[0]
        tolerance = result["length_relative_tolerance"]
        assert 0 < tolerance <= 1e-5
        if "joined" in ask:
            chain = result["chains_by_layer"]["Steel-Beams"]
            assert chain["continuous"] and not chain["gaps"]
            assert len(chain["segments"]) == (1 if together else len(expected))
            total = chain["segment_length"]
        else:
            total = result["lengths_by_layer"]["Steel-Beams"]
            measured = result["lengths_by_handle"]
            lengths = [measured[k][result["units"]] for k in sorted(measured)]
            assert lengths == pytest.approx([sum(expected)] if together else expected, rel=tolerance)
            if "spans" in ask and together and split_at:
                spans = result["spline_spans_by_handle"]["C1/A1"]
                assert not spans["jumps"]
                assert [s["length"] for s in spans["spans"]] == pytest.approx(expected, rel=tolerance)
        assert total == pytest.approx(sum(expected),rel=tolerance)
