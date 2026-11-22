"""Homogeneous weight-scale invariance at normal and subnormal IEEE-754 limits."""
import math

import pytest

from test_x03_oblique_spline_author import placed_dxf, spline_record
from test_x03_offline_drawing_author import invoke, tools_fallback


ASKS = [
    ("Measure spline beam length after scaling all rational weights.", (2,3), 1),
    ("List each NURBS beam length in centimetres with normalized homogeneous weights.", (-3,1), .1),
    ("How long in metres is each spline beam after a common positive weight rescaling?", (1,4), .001),
]


def drawing(exponent,sx,sy):
    weights = [math.ldexp(v,exponent) for v in (1.,2.,4.)]
    curve = spline_record([(0,0,0),(1,0,0),(2,2,0)], [0]*3+[1]*3, weights, (0,0,1))
    return placed_dxf(curve,(sx,sy,1))


def speed(t,sx,sy):
    # Independent quadratic Bernstein homogeneous numerators and denominator.
    # H=(4t+4t^2,8t^2,(1+t)^2); differentiate the projected point Hxy/Hw.
    x,y,w = 4*t+4*t*t,8*t*t,(1+t)**2
    dx,dy,dw = 4+8*t,16*t,2*(1+t)
    return math.hypot(sx*(dx*w-x*dw),sy*(dy*w-y*dw))/w**2


@pytest.mark.parametrize("ask,scale,factor",ASKS)
def test_common_weight_scale(ask,scale,factor):
    _, source, _ = tools_fallback.author_tool(ask)
    sx,sy = scale
    n = 16384
    expected = factor*math.fsum(speed((i+.5)/n,sx,sy) for i in range(n))/n
    # x=2s, y=2s^2, s=2t/(1+t). Hence |s'|<=2, |s''|<=4,
    # |s'''|<=12, and min |r'| >= |sx|. Bound |speed''| by
    # |r'''| + |r''|^2 / min |r'|.
    acc = math.hypot(8*sx,32*sy)
    jerk = math.hypot(24*sx,144*sy)
    error = factor*(jerk+acc*acc/abs(sx))/(24*n*n)
    measured = []
    for exponent in (0,1021,-1074):
        status,payload = invoke(source,drawing(exponent,*scale))
        assert status == "ok", payload
        result = payload[0]
        length = result["lengths_by_layer"]["Steel-Beams"]
        tol = result["length_relative_tolerance"]
        assert 0 < tol <= 1e-5
        assert math.isfinite(length) and abs(length-expected) <= tol*expected+error
        measured.append(length)
    assert measured[0] == measured[1] == measured[2]
