"""Synthetic DXFs only; no corpus or real-data file access."""

import io
import math

import pytest

ezdxf = pytest.importorskip("ezdxf")

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
    out = list(anonymize(doc).modelspace())
    assert [e.dxftype() for e in out] == ["LINE", "CIRCLE", "LINE"]
    assert distance(out[0].dxf.start, out[0].dxf.end) == pytest.approx(12)
    assert distance(out[0].dxf.start, out[2].dxf.start) == pytest.approx(0, abs=1e-8)
    assert distance(out[0].dxf.end, out[2].dxf.end) == pytest.approx(0, abs=1e-8)
    assert distance(out[0].dxf.start, out[1].dxf.center) == pytest.approx(0, abs=1e-8)
    assert out[1].dxf.radius == pytest.approx(6)


@pytest.mark.parametrize("kind", ["POINT", "ELLIPSE", "SPLINE", "POLYLINE", "HATCH", "SOLID"])
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


@pytest.mark.parametrize("point", [(float("nan"), 0), (0, float("inf")), (0, 0, 1)])
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
    ("elevation", 2), ("extrusion", (0, 1, 0)),
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
    a, b = anonymize(doc).modelspace()
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
    poly, arc = anonymize(doc).modelspace()
    assert poly.get_points("xyb")[0][2] == pytest.approx(-.5)
    assert distance(*poly.get_points("xy")) == pytest.approx(4)
    assert arc.dxf.radius == pytest.approx(4)
    assert (arc.dxf.end_angle-arc.dxf.start_angle) % 360 == pytest.approx(90)


def test_nonuniform_curves_refused_and_straight_lines_preserved():
    doc = drawing()
    block = doc.blocks.new("Private scaled")
    block.add_line((0, 0), (1, 1))
    doc.modelspace().add_blockref("Private scaled", (0, 0), dxfattribs={
        "layer": "Private Modules", "xscale": 2, "yscale": 3})
    line, = anonymize(doc).modelspace()
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
