import pytest
from benches.x03_beam_length_oracle import beam_length_quality


@pytest.mark.parametrize("units,factor", [("m",1), ("mm",0.001), ("ft",0.3048)])
def test_grouped_lengths_in_metres(units, factor):
    doc = pytest.importorskip("ezdxf").new()
    space = doc.modelspace()
    for layer, end in [("upper",(3,4,0)), ("upper",(0,0,2)), ("lower",(-4,0,3))]:
        space.add_line((0,0,0), end, dxfattribs={"layer":layer})
    expected = {"upper":7*factor, "lower":5*factor}
    assert beam_length_quality(space, units, expected) == "PASS"
    assert beam_length_quality(space, units, dict(expected, upper=0)) == "FAIL"
    assert beam_length_quality(space, None, expected) == "INCOMPLETE"


@pytest.mark.parametrize("kind", ["SPLINE", "missing endpoint"])
def test_incomplete_beam_coverage(kind):
    doc = pytest.importorskip("ezdxf").new()
    space = doc.modelspace()
    space.add_line((0,0),(2,0))
    if kind == "SPLINE":
        space.add_spline([(0,0),(1,2),(2,3)])
    else:
        beam = space.add_line((0,0),(1,0))
        beam.dxf.discard("end")
    assert beam_length_quality(space,"m",{"0":2}) == "INCOMPLETE"


@pytest.mark.parametrize("unit,factor,start,end,arc_pi",[
    ("m",1,350,80,1), ("mm",0.001,180,0,2), ("ft",0.3048,0,270,3)])
def test_mixed_beam_lengths(unit,factor,start,end,arc_pi):
    from math import pi
    doc = pytest.importorskip("ezdxf").new()
    space = doc.modelspace()
    space.add_line((0,0),(3,4), dxfattribs={"layer":"beams"})
    space.add_arc((0,0),2,start,end, dxfattribs={"layer":"beams"})
    assert beam_length_quality(space,unit,{"beams":(5+arc_pi*pi)*factor}) == "PASS"
    assert beam_length_quality(space,unit,{"beams":5*factor}) == "FAIL"


@pytest.mark.parametrize("closed,bulge,expected", [(False,0,5), (True,0,10), (False,-1,2.5*3.141592653589793)])
def test_polyline_beams(closed, bulge, expected):
    doc = pytest.importorskip("ezdxf").new()
    space = doc.modelspace()
    line = space.add_lwpolyline([(0,0,bulge),(3,4,0)],format="xyb",close=closed)
    assert beam_length_quality(space,"m",{"0":expected}) == "PASS"
    line.set_points([(0,0,float("nan")),(3,4,0)],format="xyb")
    assert beam_length_quality(space,"m",{"0":expected}) == "INCOMPLETE"
