"""Transform native bulged edges, including the implicit closing segment."""
import math

import pytest

from test_x03_ocs_arc_author import drawing, integral, placed
from test_x03_offline_drawing_author import invoke
from tools_fallback import author_tool


CASES = [
    ("Measure bulged beam lengths per layer, including closed paths.", .5, 0, 1),
    ("List curved beam lengths in centimetres including clockwise bulges on closed polylines.", -1.5, 0, .1),
    ("Sum beam lengths by layer in metres, including the closing-edge bulge.", .8, 2, .001),
]


@pytest.mark.parametrize("ask,bulge,edge,factor", CASES)
def test_closed_bulged_path(ask, bulge, edge, factor):
    points = [(0,0),(4,1),(1,3)]
    tags = [(0,"LWPOLYLINE"),(5,"A1"),(8,"0"),(90,3),(70,1)]
    for i,(x,y) in enumerate(points):
        tags.extend([(10,x),(20,y),(42,bulge if i==edge else 0)])
    _, source, _ = author_tool(ask)
    status,payload = invoke(source,drawing(tags))
    assert status == "ok", payload
    expected = chord_total = error = 0.
    for i, ((x,y),(u,v)) in enumerate(zip(points, points[1:]+points[:1])):
        dx,dy = u-x,v-y
        chord = math.hypot(*placed((dx,dy,0)))
        chord_total += chord
        if i != edge:
            expected += chord
            continue
        # Signed perpendicular offset from chord midpoint to the center.
        offset = (1-bulge**2)/(4*bulge)
        cx,cy = (x+u)/2-offset*dy, (y+v)/2+offset*dx
        radius = math.hypot(x-cx, y-cy)
        arc,error = integral(radius, math.atan2(y-cy,x-cx), 4*math.atan(bulge),
                             (1,0,0), (0,1,0))
        expected += arc
    result = payload[0]
    measured = result["lengths_by_layer"]["Roof-Beams"]
    tol = result["length_relative_tolerance"]
    assert measured > chord_total*factor
    assert 0 < tol <= 1e-5
    assert abs(measured-expected*factor) <= (error+expected*tol)*factor
