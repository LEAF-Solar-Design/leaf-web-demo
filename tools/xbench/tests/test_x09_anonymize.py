"""Synthetic DXFs only; no corpus or real-data file access."""

import io
import math

import pytest

ezdxf = pytest.importorskip("ezdxf")
from ezdxf.disassemble import recursive_decompose

from benches import x09_anonymize as anon


def drawing():
    doc = ezdxf.new("R2010")
    for name in ("Private Modules", "Private Roof", "Unselected"):
        doc.layers.new(name)
    doc.header["$INSUNITS"] = 6
    return doc


def anonymize(doc, **kwargs):
    return anon.anonymize_document(doc, kwargs.get("modules", ["Private Modules"]),
                                  kwargs.get("roofs", ["Private Roof"]))


def geometry(doc):
    return list(recursive_decompose(doc.modelspace()))


def distance(a, b):
    return math.hypot(a[0]-b[0], a[1]-b[1])


def area(points):
    # Translate before shoelace to avoid cancellation after random translation.
    ox, oy = points[0][:2]
    points = [(p[0]-ox, p[1]-oy) for p in points]
    return abs(sum(a[0]*b[1]-a[1]*b[0]
                   for a, b in zip(points, points[1:]+points[:1]))) / 2


def test_fresh_geometry_dimensions_area_and_privacy():
    doc = drawing()
    space = doc.modelspace()
    line = space.add_line((1, 2), (4, 6), dxfattribs={"layer": "Private Modules"})
    doc.appids.new("Private Application")
    line.set_xdata("Private Application", [(1000, "Private payload")])
    line.new_extension_dict().add_xrecord("Private Record").extend([(1, "Private record text")])
    space.add_lwpolyline([(0, 0), (10, 0), (10, 6), (0, 6)], close=True,
                         dxfattribs={"layer": "Private Roof"})
    space.add_circle((2, 3), 4, dxfattribs={"layer": "Private Modules"})
    space.add_arc((2, 3), 5, 20, 140, dxfattribs={"layer": "Private Roof"})
    space.add_text("Private customer", dxfattribs={"layer": "Private Modules"})
    space.add_mtext("Private address", dxfattribs={"layer": "Private Roof"})
    space.add_point((9, 9), dxfattribs={"layer": "Unselected"})
    doc.header["$PROJECTNAME"] = "Private project"
    result = anonymize(doc)
    assert result is not doc
    assert result.header["$INSUNITS"] == 6
    entities = list(result.modelspace())
    assert [e.dxftype() for e in entities] == ["LINE", "LWPOLYLINE", "CIRCLE", "ARC"]
    assert distance(entities[0].dxf.start, entities[0].dxf.end) == pytest.approx(5)
    points = entities[1].get_points("xy")
    assert area(points) == pytest.approx(60)
    assert distance(points[0], points[1]) == pytest.approx(10)
    assert distance(points[1], points[2]) == pytest.approx(6)
    assert entities[2].dxf.radius == pytest.approx(4)
    arc = entities[3]
    assert arc.dxf.radius == pytest.approx(5)
    assert (arc.dxf.end_angle-arc.dxf.start_angle) % 360 == pytest.approx(120)
    assert distance(entities[0].dxf.start, entities[2].dxf.center) == pytest.approx(math.sqrt(2))
    assert all(e.dxf.layer in {"MODULE", "ROOF"} for e in entities)
    assert all(not e.has_xdata("Private Application") and not e.has_extension_dict for e in entities)
    assert set(layer.dxf.name for layer in result.layers) == {"0", "Defpoints", "MODULE", "ROOF"}
    text = io.StringIO()
    result.write(text)
    assert "Private" not in text.getvalue()
    assert len(doc.modelspace()) == 7


def test_nested_insert_basepoints_rotations_scales_and_shared_frame():
    doc = drawing()
    child = doc.blocks.new("Private child", base_point=(1, 1))
    child.add_line((1, 1), (3, 1))
    child.add_circle((1, 1), 1, dxfattribs={"layer": "Private Modules"})
    child.add_attdef("Private tag", (0, 0), text="Private attribute")
    parent = doc.blocks.new("Private parent", base_point=(2, 0))
    parent.add_blockref("Private child", (4, 0),
                        dxfattribs={"rotation": 90, "xscale": 2, "yscale": 2})
    doc.modelspace().add_blockref("Private parent", (10, 20), dxfattribs={
        "layer": "Private Modules", "rotation": 90, "xscale": 3, "yscale": 3})
    # Flattened source line: (10,26) -> (-2,26), circle centered (10,26).
    doc.modelspace().add_line((10, 26), (-2, 26), dxfattribs={"layer": "Private Roof"})
    result = anonymize(doc)
    assert [e.dxftype() for e in result.modelspace()] == ["INSERT", "LINE"]
    out = geometry(result)
    assert [e.dxftype() for e in out] == ["LINE", "CIRCLE", "LINE"]
    assert distance(out[0].dxf.start, out[0].dxf.end) == pytest.approx(12)
    assert distance(out[0].dxf.start, out[2].dxf.start) == pytest.approx(0, abs=1e-8)
    assert distance(out[0].dxf.end, out[2].dxf.end) == pytest.approx(0, abs=1e-8)
    assert distance(out[0].dxf.start, out[1].dxf.center) == pytest.approx(0, abs=1e-8)
    assert out[1].dxf.radius == pytest.approx(6)


@pytest.mark.parametrize("kind", ["SPLINE", "POLYLINE", "HATCH", "SOLID"])
def test_selected_unsupported_geometry_raises(kind):
    doc = drawing()
    doc.modelspace().new_entity(kind, dxfattribs={"layer": "Private Modules"})
    with pytest.raises(ValueError, match="unsupported"):
        anonymize(doc)


@pytest.mark.parametrize("modules,roofs", [
    ([], ["Private Roof"]), (["Private Modules"], []),
    ("Private Modules", ["Private Roof"]), (None, ["Private Roof"]),
    (["missing"], ["Private Roof"]), ([""], ["Private Roof"]),
    (["Private Modules", "PRIVATE MODULES"], ["Private Roof"]),
    (["Private Modules"], ["PRIVATE MODULES"]), ([17], ["Private Roof"]),
])
def test_malformed_allowlists(modules, roofs):
    with pytest.raises(ValueError):
        anonymize(drawing(), modules=modules, roofs=roofs)


@pytest.mark.parametrize("point", [(float("nan"), 0), (0, float("inf")), (0, 0, float("nan"))])
def test_invalid_coordinates(point):
    doc = drawing()
    doc.modelspace().add_line(point, (1, 1), dxfattribs={"layer": "Private Modules"})
    with pytest.raises(ValueError):
        anonymize(doc)


@pytest.mark.parametrize("child_layer", ["Private Roof", "Unselected", "Missing"])
def test_incompatible_or_missing_block_child_layer(child_layer):
    doc = drawing()
    block = doc.blocks.new("Private block")
    block.add_line((0, 0), (1, 0), dxfattribs={"layer": child_layer})
    doc.modelspace().add_blockref("Private block", (0, 0), dxfattribs={"layer": "Private Modules"})
    with pytest.raises(ValueError):
        anonymize(doc)


def test_missing_top_level_layer_is_refused():
    doc = drawing()
    doc.modelspace().add_line((0, 0), (1, 1), dxfattribs={"layer": "Missing"})
    with pytest.raises(ValueError, match="layer"):
        anonymize(doc)


@pytest.mark.parametrize("attribute,value", [
    ("elevation", float("inf")), ("extrusion", (0, 1, 0)),
    ("thickness", 1), ("const_width", float("inf")),
])
def test_nonplanar_or_nonfinite_polyline_attributes(attribute, value):
    doc = drawing()
    poly = doc.modelspace().add_lwpolyline([(0, 0), (1, 0), (1, 1)],
                                          dxfattribs={"layer": "Private Modules"})
    setattr(poly.dxf, attribute, value)
    with pytest.raises(ValueError):
        anonymize(doc)


def test_insert_array_spacing_and_negative_ocs():
    doc = drawing()
    block = doc.blocks.new("Private array")
    block.add_line((0, 0), (1, 0))
    doc.modelspace().add_blockref("Private array", (0, 0), dxfattribs={
        "layer": "Private Modules", "rotation": 90, "xscale": 2,
        "yscale": 2, "column_count": 2, "column_spacing": 7,
        "extrusion": (0, 0, -1)})
    result = anonymize(doc)
    reference, = result.modelspace()
    assert reference.dxftype() == "INSERT"
    assert reference.dxf.column_count == 2
    a, b = geometry(result)
    assert distance(a.dxf.start, a.dxf.end) == pytest.approx(2)
    assert distance(a.dxf.start, b.dxf.start) == pytest.approx(7)


def test_per_file_random_rigid_transform():
    doc = drawing()
    doc.modelspace().add_line((0, 0), (3, 4), dxfattribs={"layer": "Private Modules"})
    first, = anonymize(doc).modelspace()
    second, = anonymize(doc).modelspace()
    assert first.dxf.start != second.dxf.start
    assert distance(first.dxf.start, first.dxf.end) == pytest.approx(5)
    assert distance(second.dxf.start, second.dxf.end) == pytest.approx(5)


def test_mirrored_bulge_and_arc_sweep():
    doc = drawing()
    block = doc.blocks.new("Private mirror")
    block.add_lwpolyline([(0, 0, .5), (2, 0, 0)], format="xyb")
    block.add_arc((0, 0), 2, 10, 100)
    doc.modelspace().add_blockref("Private mirror", (0, 0), dxfattribs={
        "layer": "Private Modules", "xscale": -2, "yscale": 2})
    poly, arc = geometry(anonymize(doc))
    # A decomposed mirrored INSERT can express reflection through negative OCS
    # extrusion instead of a reversed stored bulge. Check world orientation.
    assert poly.get_points("xyb")[0][2] * poly.dxf.extrusion.z == pytest.approx(-.5)
    assert distance(*poly.get_points("xy")) == pytest.approx(4)
    assert arc.dxf.radius == pytest.approx(4)
    assert (arc.dxf.end_angle-arc.dxf.start_angle) % 360 == pytest.approx(90)


def test_nonuniform_curves_refused_and_straight_lines_preserved():
    doc = drawing()
    block = doc.blocks.new("Private scaled")
    block.add_line((0, 0), (1, 1))
    doc.modelspace().add_blockref("Private scaled", (0, 0), dxfattribs={
        "layer": "Private Modules", "xscale": 2, "yscale": 3})
    line, = geometry(anonymize(doc))
    assert distance(line.dxf.start, line.dxf.end) == pytest.approx(math.sqrt(13))
    block.add_circle((0, 0), 1)
    with pytest.raises(ValueError, match="uniform"):
        anonymize(doc)


def test_cycles_and_limits(monkeypatch):
    doc = drawing()
    block = doc.blocks.new("Private cycle")
    block.add_blockref("Private cycle", (0, 0))
    doc.modelspace().add_blockref("Private cycle", (0, 0), dxfattribs={"layer": "Private Modules"})
    with pytest.raises(ValueError, match="cyclic"):
        anonymize(doc)
    doc = drawing()
    doc.modelspace().add_line((0, 0), (1, 1), dxfattribs={"layer": "Private Modules"})
    monkeypatch.setattr(anon, "MAX_ENTITIES", 0)
    with pytest.raises(ValueError, match="limit"):
        anonymize(doc)


def test_cli_refuses_paths_without_reading_real_data(monkeypatch):
    def refused(*args, **kwargs):
        raise ValueError("synthetic path refusal")
    monkeypatch.setattr(anon, "_safe_path", refused)
    with pytest.raises(SystemExit) as exc:
        anon.main(["--input", "synthetic.dxf", "--output", "other.dxf",
                   "--module-layer", "Private Modules", "--roof-layer", "Private Roof"])
    assert exc.value.code == 2


def test_repeated_modules_keep_one_neutral_block_type():
    doc = drawing()
    block = doc.blocks.new("Private module")
    for inset in (0, .1, .2):
        block.add_lwpolyline([(inset, inset), (2-inset, inset),
                             (2-inset, 1-inset), (inset, 1-inset)], close=True)
    for index in range(3):
        doc.modelspace().add_blockref("Private module", (index*10, 0),
                                     dxfattribs={"layer": "Private Modules"})
    out = anonymize(doc)
    references = list(out.modelspace())
    assert len(references) == 3
    assert all(e.dxftype() == "INSERT" for e in references)
    names = {e.dxf.name for e in references}
    assert names == {"B000001"}
    assert len(out.blocks.get("B000001")) == 3
    assert len(geometry(out)) == 9
    assert distance(references[0].dxf.insert, references[1].dxf.insert) == pytest.approx(10)
    text = io.StringIO()
    out.write(text)
    assert "Private" not in text.getvalue()


def test_absolute_block_coordinates_are_not_retained(monkeypatch):
    class FixedRandom:
        def __init__(self):
            self.values = iter((math.pi/2, 12345, 67890, 24680))

        def uniform(self, *args):
            return next(self.values)

    monkeypatch.setattr(anon.secrets, "SystemRandom", FixedRandom)
    doc = drawing()
    block = doc.blocks.new("Private georeferenced module", base_point=(0, 0))
    block.add_lwpolyline([(1_000_000, 2_000_000), (1_000_010, 2_000_000),
                         (1_000_010, 2_000_004), (1_000_000, 2_000_004)], close=True)
    doc.modelspace().add_blockref("Private georeferenced module", (10, 20),
                                 dxfattribs={"layer": "Private Modules"})
    out = anonymize(doc)
    reference, = out.modelspace()
    rebuilt = out.blocks.get(reference.dxf.name)
    poly, = rebuilt
    assert tuple(rebuilt.block.dxf.base_point) == (0, 0, 0)
    assert max(abs(value) for point in poly.get_points("xy") for value in point) <= 10
    assert tuple(reference.dxf.insert)[:2] == pytest.approx((-2_000_020+12345, 1_000_010+67890))
    world, = geometry(out)
    assert area(world.get_points("xy")) == pytest.approx(40)



def test_relative_heights_and_sloping_line_are_preserved():
    doc = drawing()
    space = doc.modelspace()
    for height in (120, 127):
        space.add_lwpolyline([(0, 0), (2, 0), (2, 1)],
                            dxfattribs={"layer": "Private Modules", "elevation": height})
    space.add_line((0, 0, 120), (3, 4, 127), dxfattribs={"layer": "Private Roof"})
    space.add_point((0, 0, 127), dxfattribs={"layer": "Private Roof"})
    a, b, line, point = anonymize(doc).modelspace()
    assert b.dxf.elevation-a.dxf.elevation == pytest.approx(7)
    assert line.dxf.start.z == pytest.approx(a.dxf.elevation)
    assert line.dxf.end.z == pytest.approx(b.dxf.elevation)
    assert (line.dxf.end-line.dxf.start).magnitude == pytest.approx(math.sqrt(74))
    assert point.dxf.location.z == pytest.approx(b.dxf.elevation)


def test_negative_ocs_elevation_uses_world_height():
    doc = drawing()
    doc.modelspace().add_lwpolyline([(0, 0), (2, 0)], dxfattribs={
        "layer": "Private Modules", "elevation": 8, "extrusion": (0, 0, -1)})
    doc.modelspace().add_point((0, 0, -8), dxfattribs={"layer": "Private Roof"})
    poly, point = anonymize(doc).modelspace()
    assert poly.dxf.elevation == pytest.approx(point.dxf.location.z)
    assert (next(poly.vertices_in_wcs())-point.dxf.location).magnitude == pytest.approx(0, abs=1e-8)


def test_nested_insert_z_basepoint_scale_and_privacy(monkeypatch):
    class FixedRandom:
        def uniform(self, lower, upper):
            return math.pi/3 if upper == math.tau else 34567
    monkeypatch.setattr(anon.secrets, "SystemRandom", FixedRandom)
    doc = drawing()
    child = doc.blocks.new("Private elevated child", base_point=(1, 2, 100000))
    child.add_line((1, 2, 100000), (4, 2, 100002))
    parent = doc.blocks.new("Private elevated parent", base_point=(2, 0, 10))
    parent.add_blockref("Private elevated child", (2, 0, 13),
                        dxfattribs={"zscale": 3})
    doc.modelspace().add_blockref("Private elevated parent", (5, 6, 20),
                                 dxfattribs={"layer": "Private Modules", "zscale": 2,
                                            "column_count": 2, "column_spacing": 17,
                                            "extrusion": (0, 0, -1)})
    source = geometry(doc)
    result = anonymize(doc)
    output = geometry(result)
    assert len(output) == len(source) == 2
    for before, after in zip(source, output):
        assert (before.dxf.end-before.dxf.start).magnitude == pytest.approx(
            (after.dxf.end-after.dxf.start).magnitude)
    assert (output[0].dxf.start-output[1].dxf.start).magnitude == pytest.approx(17)
    assert output[0].dxf.end.z-output[0].dxf.start.z == pytest.approx(
        source[0].dxf.end.z-source[0].dxf.start.z)
    ref, = result.modelspace()
    line, = result.blocks.get(ref.dxf.name)
    assert tuple(line.dxf.start) == pytest.approx((0, 0, 0))
    stream = io.StringIO()
    result.write(stream)
    assert "Private" not in stream.getvalue()
    assert ref.dxf.insert.z != pytest.approx(20)


def test_ellipse_arc_shape_height_and_metadata_are_preserved():
    doc = drawing()
    ellipse = doc.modelspace().add_ellipse((2, 3, 100), (4, 1, 0), .4, .2, 4.1,
                                          dxfattribs={"layer": "Private Roof"})
    doc.appids.new("Private ellipse app")
    ellipse.set_xdata("Private ellipse app", [(1000, "Private ellipse payload")])
    doc.modelspace().add_point((2, 3, 107), dxfattribs={"layer": "Private Modules"})
    result = anonymize(doc)
    out, point = result.modelspace()
    assert out.dxftype() == "ELLIPSE"
    assert out.dxf.major_axis.magnitude == pytest.approx(ellipse.dxf.major_axis.magnitude)
    assert out.dxf.ratio == pytest.approx(.4)
    assert (out.dxf.end_param-out.dxf.start_param) % math.tau == pytest.approx(3.9)
    assert point.dxf.location.z-out.dxf.center.z == pytest.approx(7)
    assert not out.has_xdata("Private ellipse app")
    assert (point.dxf.location-out.dxf.center).magnitude == pytest.approx(7)
    stream = io.StringIO()
    result.write(stream)
    assert "Private" not in stream.getvalue()


@pytest.mark.parametrize("attribute,value", [
    ("ratio", float("nan")), ("major_axis", (1, 0, 1)),
    ("center", (0, 0, float("inf"))), ("end_param", float("nan")),
])
def test_invalid_ellipse_geometry_is_refused(attribute, value):
    doc = drawing()
    ellipse = doc.modelspace().add_ellipse((0, 0), (2, 0), .5,
                                          dxfattribs={"layer": "Private Roof"})
    setattr(ellipse.dxf, attribute, value)
    with pytest.raises(ValueError):
        anonymize(doc)



def test_elevated_reflected_block_preserves_ellipse_and_point_frame():
    doc = drawing()
    block = doc.blocks.new("Private curved roof", base_point=(1, 2, 50))
    block.add_ellipse((3, 4, 53), (5, 0, 0), .3, .1, 4.2)
    block.add_point((3, 4, 59))
    doc.modelspace().add_blockref("Private curved roof", (20, 30, 80), dxfattribs={
        "layer": "Private Roof", "rotation": 35, "xscale": -2, "yscale": 2,
        "zscale": 3, "extrusion": (0, 0, -1)})
    source_ellipse, source_point = geometry(doc)
    ellipse, point = geometry(anonymize(doc))
    assert ellipse.dxftype() == "ELLIPSE"
    assert point.dxftype() == "POINT"
    assert ellipse.dxf.major_axis.magnitude == pytest.approx(source_ellipse.dxf.major_axis.magnitude)
    assert ellipse.dxf.ratio == pytest.approx(source_ellipse.dxf.ratio)
    assert (ellipse.dxf.center-point.dxf.location).magnitude == pytest.approx(
        (source_ellipse.dxf.center-source_point.dxf.location).magnitude)
    assert point.dxf.location.z-ellipse.dxf.center.z == pytest.approx(
        source_point.dxf.location.z-source_ellipse.dxf.center.z)
