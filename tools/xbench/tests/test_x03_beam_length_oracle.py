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


@pytest.mark.parametrize("scale,angle", [(1,90),(2,45),(3,180)])
def test_nested_beam_lengths(scale,angle):
    doc = pytest.importorskip("ezdxf").new()
    beam = doc.blocks.new("part")
    beam.add_line((1,1),(4,5))
    frame = doc.blocks.new("frame")
    frame.add_blockref("part",(7,8),dxfattribs={"rotation":angle}).set_scale(scale)
    space = doc.modelspace()
    outer = space.add_blockref("frame",(10,20),dxfattribs={"layer":"upper"}).set_scale(2)
    assert beam_length_quality(space,"m",{"upper":10*scale}) == "PASS"
    outer.dxf.xscale = 4
    assert beam_length_quality(space,"m",{"upper":10*scale}) == "FAIL"
    outer.set_scale(2)
    beam.add_blockref("frame",(0,0))
    assert beam_length_quality(space,"m",{"upper":10*scale}) == "INCOMPLETE"


@pytest.mark.parametrize("units", ["m","mm","ft"])
def test_invalid_report_numeric_range(units):
    doc = pytest.importorskip("ezdxf").new()
    space = doc.modelspace()
    space.add_line((0,0),(3,4))
    for bad in (10**500,float("nan"),float("inf"),float("-inf")):
        assert beam_length_quality(space,units,{"0":bad}) == "FAIL"

@pytest.mark.parametrize("layer", ["rafters", "joists", "braces"])
def test_block_expansion_budget(layer):
    doc = pytest.importorskip("ezdxf").new()
    space = doc.modelspace()
    space.add_line((0, 0), (1, 0), dxfattribs={"layer": layer})
    block = doc.blocks.new("level0")
    block.add_line((0, 0), (1, 0))
    for level in range(1, 70):
        parent = doc.blocks.new(f"level{level}")
        for offset in range(2):
            parent.add_blockref(block.name, (offset, 0))
        block = parent
    ref = space.add_blockref("level2", (0, 0), dxfattribs={"layer": layer})
    assert beam_length_quality(space, "m", {layer: 5}, max_entities=12) == "PASS"
    ref.dxf.name = "level25"
    assert beam_length_quality(space, "m", {layer: 1}, max_entities=100) == "INCOMPLETE"
    ref.dxf.name = block.name
    assert beam_length_quality(space, "m", {layer: 1}) == "INCOMPLETE"


@pytest.mark.parametrize("end,plan,spatial", [((3, 4, 12), 5, 13), ((8, 0, 15), 8, 17),
                                          ((0, 0, 7), 0, 7)])
def test_line_modes_require_evidence(end, plan, spatial):
    from types import SimpleNamespace
    from benches.x03_beam_length_oracle import line_measurement_quality
    def line(start, finish):
        points = {"start": start, "end": finish}
        return SimpleNamespace(dxftype=lambda: "LINE", dxf=SimpleNamespace(layer="L", get=points.get))
    entities = [line((0, 0, 0), end)]
    assert line_measurement_quality(entities, "m", {"L": plan}, mode="plan") == "PASS"
    assert line_measurement_quality(entities, "m", {"L": spatial}, mode="spatial") == "PASS"
    assert line_measurement_quality(entities, "m", {"L": plan}, mode="spatial") == "FAIL"
    assert line_measurement_quality(entities, "m", {"L": spatial}) == "INCOMPLETE"
    assert line_measurement_quality([line((0, 0), end[:2])], "m", {"L": plan}, mode="spatial") == "INCOMPLETE"


@pytest.mark.parametrize("scales,delta", [((-2, 3, 1), (-2, 6, 2)), ((1, -4, 2), (1, -8, 4)),
                                       ((3, 1, -2), (3, 2, -4))])
def test_transformed_block_line_endpoints(scales, delta):
    from math import dist
    doc = pytest.importorskip("ezdxf").new()
    block = doc.blocks.new("beam")
    block.add_line((0, 0, 0), (1, 2, 2))
    space = doc.modelspace()
    space.add_blockref("beam", (10, 20, 30), dxfattribs=dict(
        xscale=scales[0], yscale=scales[1], zscale=scales[2], rotation=90))
    # Translate both endpoints after a quarter-turn about Z.
    end = (10-delta[1], 20+delta[0], 30+delta[2])
    expected = dist((10, 20, 30), end)
    assert beam_length_quality(space, "m", {"0": expected}) == "PASS"
    assert beam_length_quality(space, "m", {"0": 3}) == "FAIL"
    block.add_arc((0, 0), 1, 0, 90)
    assert beam_length_quality(space, "m", {"0": expected}) == "INCOMPLETE"


@pytest.mark.parametrize("normal,u,v", [
    ((0,0,-1), (-1,0,0), (0,1,0)), ((1,0,0), (0,1,0), (0,0,1)),
    ((0,1,1), (-1,0,0), (0,-2**-0.5,2**-0.5)),
])
def test_arc_in_object_plane(normal, u, v):
    from math import acos, dist
    from types import SimpleNamespace
    from benches.x03_beam_length_oracle import beam_length_quality
    radius = 3
    start = [radius*x for x in u]
    end = [radius*x for x in v]
    # Measure the angle between the explicitly rotated world-space radius vectors.
    expected = dist(start, (0,0,0)) * acos(sum(a*b for a,b in zip(start,end))/radius**2)
    doc = pytest.importorskip("ezdxf").new()
    arc = doc.modelspace().add_arc((0,0), radius, 0, 90, dxfattribs={"extrusion":normal})
    assert beam_length_quality([arc], "m", {"0":expected}) == "PASS"
    bad = SimpleNamespace(dxftype=lambda:"ARC", dxf=SimpleNamespace(layer="0", get={"extrusion":(0,0,0)}.get))
    assert beam_length_quality([bad], "m", {"0":expected}) == "INCOMPLETE"


@pytest.mark.parametrize("ratio,start,end,expected", [
    (1, 0, 6.283185307179586, 12.566370614359172),
    (0.5, 0, 1.5707963267948966, 2.422112055136919),
    (0.5, 4.71238898038469, 1.5707963267948966, 4.844224110273838)])
def test_ellipse_arcs_in_metres(ratio, start, end, expected):
    # Radius-2 circumference and independently tabulated complete E(3/4).
    space = pytest.importorskip("ezdxf").new().modelspace()
    entity = space.add_ellipse((0, 0), major_axis=(0, 2, 0), ratio=ratio,
                               start_param=start, end_param=end)
    assert beam_length_quality(space, "m", {"0": expected}) == "PASS"
    assert beam_length_quality(space, "m", {"0": expected + 1e-7}) == "FAIL"
    entity.dxf.discard("major_axis")
    assert beam_length_quality(space, "m", {"0": expected}) == "INCOMPLETE"
