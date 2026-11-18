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


@pytest.mark.parametrize("kind", ["ARC", "LWPOLYLINE", "missing endpoint"])
def test_incomplete_beam_coverage(kind):
    doc = pytest.importorskip("ezdxf").new()
    space = doc.modelspace()
    space.add_line((0,0),(2,0))
    if kind == "ARC":
        space.add_arc((0,0),1,0,90)
    elif kind == "LWPOLYLINE":
        space.add_lwpolyline([(0,0),(1,2)])
    else:
        beam = space.add_line((0,0),(1,0))
        beam.dxf.discard("end")
    assert beam_length_quality(space,"m",{"0":2}) == "INCOMPLETE"
