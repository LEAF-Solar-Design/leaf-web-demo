"""World-space arc oracle from explicit OCS tangents and two placement matrices."""
import math

import pytest

from test_x03_offline_drawing_author import invoke
from tools_fallback import author_tool


CASES = [
    ("Measure tilted ARC beam lengths per layer.", (0, 3, 4), 15, 117, 1),
    ("List the curved beam lengths in centimetres for arcs crossing zero degrees.", (0, -4, 3), 315, 75, .1),
    ("Sum the oblique arc beam lengths by layer in metres.", (0, 3, -4), 37, 330, .001),
]


def drawing(records):
    def block(name, content):
        return [(0, "BLOCK"), (2, name), (10, 0), (20, 0), *content, (0, "ENDBLK")]

    def insert(name, layer, handle, scale, rotation):
        return [(0, "INSERT"), (5, handle), (2, name), (8, layer), (10, 3), (20, 9),
                *zip((41,42,43), scale), (50, rotation)]

    blocks = block("Member", records) + block("Frame", insert("Member", "0", "C1", (-2,1,3), 30))
    entities = insert("Frame", "Roof-Beams", "B1", (1,4,2), 0)
    wire = [(0,"SECTION"), (2,"HEADER"), (9,"$INSUNITS"), (70,4), (0,"ENDSEC"),
            (0,"SECTION"), (2,"BLOCKS"), *blocks, (0,"ENDSEC"), (0,"SECTION"), (2,"ENTITIES"),
            *entities, (0,"ENDSEC"), (0,"EOF")]
    return "".join(f"{k}\n{v}\n" for k,v in wire).encode()


def placed(v):
    x,y,z = v
    angle = math.radians(30)
    return (-2*x*math.cos(angle)-y*math.sin(angle),
            4*(-2*x*math.sin(angle)+y*math.cos(angle)), 6*z)


def integral(radius, start, sweep, ux, uy):
    n = 8192
    h = sweep/n
    speeds = []
    for i in range(n):
        a = start+(i+.5)*h
        tangent = tuple(radius*(-u*math.sin(a)+v*math.cos(a)) for u,v in zip(ux,uy))
        speeds.append(math.hypot(*placed(tangent)))
    # Singular values are in [1,12]. Midpoint remainder bounds |speed''|.
    return abs(h)*math.fsum(speeds), abs(sweep)*h*h*(12+144)*radius/24


@pytest.mark.parametrize("ask,normal,start,end,factor", CASES)
def test_nested_ocs_arc(ask, normal, start, end, factor):
    tags = [(0,"ARC"), (5,"D1"), (8,"0"), (10,900), (20,-7), (40,6), (50,start), (51,end),
            *zip((210,220,230), normal)]
    _, source, _ = author_tool(ask)
    status, payload = invoke(source, drawing(tags))
    assert status == "ok", payload
    result = payload[0]
    # For these YZ-plane normals, the Autodesk horizontal OCS axis lies on +/-X.
    _, y, z = normal
    sign = -1 if y > 0 else 1
    expected, error = integral(6, math.radians(start), math.radians((end-start)%360),
                               (sign,0,0), (0,sign*z/5,-sign*y/5))
    tol = result["length_relative_tolerance"]
    assert 0 < tol <= 1e-5
    assert abs(result["lengths_by_layer"]["Roof-Beams"]-expected*factor) <= (error+expected*tol)*factor
