"""Generated DXFs and scripted product I/O for the X01 deliverable grader."""

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from benches import x01
import make_synthetic_metres as metres
import x01_geometry as geometry
from xbench_studio import StudioClient


PROJECT = {"project_id": "p", "org_id": "o"}
CAP = "private-checkout-capability-123456789"
MODULE_LAYER = "PRIVATE_MODULE_LAYER"
TAG_LAYER = "PRIVATE_TAG_LAYER"
BLOCK = "PRIVATE_PANEL_BLOCK"


@pytest.fixture
def doc():
    pytest.importorskip("ezdxf")
    pytest.importorskip("shapely")
    ezdxf, _, _, _ = geometry.dependencies()
    drawing = ezdxf.new("R2010")
    drawing.header["$INSUNITS"] = 1
    drawing.layers.new(MODULE_LAYER)
    drawing.layers.new(TAG_LAYER)
    return drawing


def panel(doc, *, point=(0, 0), layer=MODULE_LAYER):
    x, y = point
    return doc.modelspace().add_lwpolyline([(x, y), (x + 10, y), (x + 10, y + 20), (x, y + 20)],
                                         close=True, dxfattribs={"layer": layer})


def block_panel(doc, *, point=(0, 0), **attribs):
    if BLOCK not in doc.blocks:
        block = doc.blocks.new(BLOCK)
        block.add_lwpolyline([(0, 0), (10, 0), (10, 20), (0, 20)], close=True)
    return doc.modelspace().add_blockref(BLOCK, point, dxfattribs={"layer": MODULE_LAYER, **attribs})


def hatch(doc, *, width=3):
    entity = doc.modelspace().add_hatch()
    entity.paths.add_polyline_path([(0, 0), (width, 0), (width, 20), (0, 20)], is_closed=True)
    return entity


def viewport(doc, *, height=100, view_height=1000):
    return doc.layouts.get("Layout1").add_viewport(center=(50, 50), size=(100, height),
                                                 view_center_point=(0, 0), view_height=view_height)


def tag(doc, *, height=0.8, point=(0, 0), text="SECRET-TAG"):
    return doc.modelspace().add_text(text, dxfattribs={"layer": TAG_LAYER, "height": height,
                                                    "insert": point})


def encoded(doc, tmp_path, name="drawing.dxf"):
    path = tmp_path / name
    doc.saveas(path)
    return path.read_bytes()


@pytest.fixture
def fixture(doc, tmp_path):
    panel(doc)
    data = encoded(doc, tmp_path)
    return {"id": "synthetic", "path": str(tmp_path / "drawing.dxf"), "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "modules": 1, "insunits": 1,
            "module_layers": [MODULE_LAYER], "module_blocks": [], "tag_layers": [TAG_LAYER]}


def pair(doc, fixture, tmp_path):
    original = Path(fixture["path"]).read_bytes()
    return x01.grade_pair(fixture, original, encoded(doc, tmp_path, "export.dxf"))


def test_load_round_trip_units(doc, tmp_path):
    panel(doc)
    loaded = geometry.load(encoded(doc, tmp_path))
    assert geometry.insunits(loaded) == 1
    assert len(geometry.module_points(loaded, [MODULE_LAYER], [])) == 1


def test_load_crlf_preserves_units_and_module_geometry(doc, tmp_path):
    doc.header["$INSUNITS"] = 1
    entity = panel(doc)
    data = encoded(doc, tmp_path).replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    loaded = geometry.load(data)
    assert geometry.insunits(loaded) == 1
    assert geometry.module_points(loaded, [MODULE_LAYER], []) == [(entity.dxf.handle, 5, 10)]


@pytest.mark.parametrize("units", [0, 1, 6])
def test_insunits(doc, units):
    doc.header["$INSUNITS"] = units
    assert geometry.insunits(doc) == units


def test_load_refuses_oversized_before_parsing(monkeypatch):
    monkeypatch.setattr(geometry, "MAX_DXF_BYTES", 4)
    with pytest.raises(ValueError, match="64 MiB"):
        geometry.load(b"12345")


def test_missing_dependencies_message(monkeypatch):
    import builtins

    original = builtins.__import__

    def importing(name, *args, **kwargs):
        if name == "ezdxf":
            raise ImportError("deliberately unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)
    with pytest.raises(RuntimeError, match="X01 needs ezdxf and shapely: pip install ezdxf shapely"):
        geometry.load(b"DXF")


def test_polyline_centroid_and_model_space_only(doc):
    entity = panel(doc)
    doc.layouts.get("Layout1").add_lwpolyline([(0, 0), (1, 0), (1, 1), (0, 1)],
                                             close=True, dxfattribs={"layer": MODULE_LAYER})
    assert geometry.module_points(doc, [MODULE_LAYER], []) == [(entity.dxf.handle, 5, 10)]


def test_polyline_requires_closed_four_vertices(doc):
    doc.modelspace().add_lwpolyline([(0, 0), (1, 0), (1, 1), (0, 1)], dxfattribs={"layer": MODULE_LAYER})
    doc.modelspace().add_lwpolyline([(0, 0), (1, 0), (0, 1)], close=True, dxfattribs={"layer": MODULE_LAYER})
    panel(doc, layer="0")
    assert geometry.module_points(doc, [MODULE_LAYER], []) == []


def test_insert_selection_and_position(doc):
    entity = block_panel(doc, point=(17, 29))
    panel(doc)
    assert geometry.module_points(doc, [MODULE_LAYER.lower()], [BLOCK.lower()]) == [(entity.dxf.handle, 17, 29)]
    assert geometry.module_points(doc, [MODULE_LAYER], ["OTHER"]) == []


def test_insert_footprint_transforms_scale_rotation_and_translation(doc):
    entity = block_panel(doc, point=(100, 200), rotation=90, xscale=2, yscale=2)
    [(handle, polygon)] = geometry.module_footprints(doc, [MODULE_LAYER], [BLOCK])
    assert handle == entity.dxf.handle
    assert polygon.area == pytest.approx(800)
    assert polygon.bounds == pytest.approx((60, 200, 100, 220))


def test_insert_footprint_union_not_bounding_box(doc):
    entity = block_panel(doc)
    doc.blocks.get(BLOCK).add_lwpolyline([(10, 0), (20, 0), (20, 5), (10, 5)], close=True)
    [(handle, polygon)] = geometry.module_footprints(doc, [MODULE_LAYER], [BLOCK])
    assert handle == entity.dxf.handle and polygon.area == pytest.approx(250)


def test_match_handle_first_and_moved():
    result = geometry.match_positions([("A", 0, 0), ("B", 10, 0)], [("A", 3, 0), ("C", 10.1, 0)], 0.5)
    assert result == {"matched": 1, "missing": [], "moved": [("A", 3.0)], "extra": 0}


def test_match_missing_extra_and_one_to_one():
    result = geometry.match_positions([("A", 0, 0), ("B", 0.1, 0), ("C", 20, 0)],
                                      [("X", 0.05, 0), ("Y", 50, 0)], 0.5)
    assert result == {"matched": 1, "missing": ["B", "C"], "moved": [], "extra": 1}


def test_match_nearest_across_negative_cell_boundary():
    assert geometry.match_positions([("A", -0.01, 0)], [("X", 0.1, 0), ("Y", -0.02, 0)], 0.5) == {
        "matched": 1, "missing": [], "moved": [], "extra": 1}


@pytest.mark.parametrize("tol", [0, -1, float("inf"), float("nan")])
def test_match_rejects_invalid_tolerance(tol):
    with pytest.raises(ValueError, match="tolerance"):
        geometry.match_positions([], [], tol)


def test_match_ten_thousand_renumbered_points_under_two_seconds():
    original = [(f"O{i}", (i % 100) * 10, (i // 100) * 10) for i in range(10000)]
    exported = [(f"E{i}", x + 0.1, y - 0.1) for i, (_, x, y) in enumerate(original)]
    started = time.perf_counter()
    result = geometry.match_positions(original, exported, 0.5)
    elapsed = time.perf_counter() - started
    assert result == {"matched": 10000, "missing": [], "moved": [], "extra": 0}
    assert elapsed < 2


def test_moved_module_fails_step_two(doc, fixture, tmp_path):
    entity = next(iter(doc.modelspace()))
    entity.translate(2, 0, 0)
    row = pair(doc, fixture, tmp_path)
    assert row["steps"]["2"] == "FAIL"
    assert row["failures"]["2"][0]["handle"] == entity.dxf.handle
    assert row["failures"]["2"][0]["layer"] == MODULE_LAYER


def test_missing_module_fails_step_two(doc, fixture, tmp_path):
    entity = next(iter(doc.modelspace()))
    handle = entity.dxf.handle
    doc.modelspace().delete_entity(entity)
    row = pair(doc, fixture, tmp_path)
    assert row["steps"]["2"] == "FAIL" and row["positions"]["missing"] == [handle]


def test_exact_oracle_count_required(doc, fixture, tmp_path):
    fixture["modules"] = 2
    assert pair(doc, fixture, tmp_path)["steps"]["2"] == "FAIL"


@pytest.mark.parametrize("flag", ["off", "frozen", "non-plotting"])
def test_bad_module_layer_fails_and_names_layer(doc, fixture, tmp_path, flag):
    layer = doc.layers.get(MODULE_LAYER)
    if flag == "off":
        layer.off()
    elif flag == "frozen":
        layer.freeze()
    else:
        layer.dxf.plot = 0
    row = pair(doc, fixture, tmp_path)
    assert row["steps"]["2"] == "PASS" and row["steps"]["4"] == "FAIL"
    assert MODULE_LAYER in row["evidence"]["4"] and flag in row["evidence"]["4"]


def test_tag_layer_is_also_checked(doc, fixture, tmp_path):
    entity = tag(doc)
    doc.layers.get(TAG_LAYER).freeze()
    row = pair(doc, fixture, tmp_path)
    assert row["steps"]["4"] == "FAIL" and entity.dxf.handle in row["evidence"]["4"]


def test_layer_flags_and_handle_or_layer_selection(doc):
    entity = panel(doc)
    doc.layers.get(MODULE_LAYER).dxf.plot = 0
    assert geometry.layer_flags(doc)[MODULE_LAYER] == {"off": False, "frozen": False, "plot": False}
    expected = [(entity.dxf.handle, MODULE_LAYER, "non-plotting")]
    assert geometry.entities_on_bad_layers(doc, [MODULE_LAYER]) == expected
    assert geometry.entities_on_bad_layers(doc, [entity.dxf.handle]) == expected
    assert geometry.entities_on_bad_layers(doc, [TAG_LAYER]) == []


def test_hatch_after_module_thirty_percent_fails(doc, fixture, tmp_path):
    fill = hatch(doc)
    row = pair(doc, fixture, tmp_path)
    assert row["steps"]["5"] == "FAIL"
    failure = row["failures"]["5"][0]
    assert failure["fraction"] == pytest.approx(0.3)
    assert failure["fill"]["handle"] == fill.dxf.handle
    assert failure["layer"] == MODULE_LAYER


def test_hatch_before_module_does_not_cover(doc):
    hatch(doc)
    panel(doc)
    assert geometry.fill_cover(doc, geometry.module_footprints(doc, [MODULE_LAYER], [])) == []


def test_sortentstable_can_move_fill_below_module(doc):
    module = panel(doc)
    fill = hatch(doc)
    doc.modelspace().set_redraw_order({module.dxf.handle: "20", fill.dxf.handle: "10"})
    assert geometry.fill_cover(doc, geometry.module_footprints(doc, [MODULE_LAYER], [])) == []


def test_sortentstable_zero_moves_fill_above_module(doc):
    fill = hatch(doc)
    module = panel(doc)
    doc.modelspace().set_redraw_order({module.dxf.handle: "10", fill.dxf.handle: "0"})
    [(handle, fill_handle, fraction)] = geometry.fill_cover(doc, geometry.module_footprints(doc, [MODULE_LAYER], []))
    assert (handle, fill_handle) == (module.dxf.handle, fill.dxf.handle)
    assert fraction == pytest.approx(0.3)


def test_hatch_hole_is_not_coverage(doc):
    panel(doc)
    fill = doc.modelspace().add_hatch()
    fill.paths.add_polyline_path([(-10, -10), (30, -10), (30, 40), (-10, 40)])
    fill.paths.add_polyline_path([(-1, -1), (11, -1), (11, 21), (-1, 21)], flags=0)
    assert geometry.fill_cover(doc, geometry.module_footprints(doc, [MODULE_LAYER], [])) == []


@pytest.mark.parametrize("kind", ["SOLID", "TRACE"])
def test_quad_fill_and_five_percent_threshold(doc, kind):
    panel(doc)
    add = doc.modelspace().add_solid if kind == "SOLID" else doc.modelspace().add_trace
    # DXF SOLID/TRACE order is lower-left, lower-right, upper-left, upper-right.
    add([(0, 0), (0.5, 0), (0, 20), (0.5, 20)])
    assert geometry.fill_cover(doc, geometry.module_footprints(doc, [MODULE_LAYER], [])) == []
    add([(0, 0), (3, 0), (0, 20), (3, 20)])
    assert geometry.fill_cover(doc, geometry.module_footprints(doc, [MODULE_LAYER], []))[0][2] == pytest.approx(0.3)


def test_viewport_scale_and_no_viewport(doc):
    assert geometry.sheet_scale(doc) is None
    viewport(doc)
    assert geometry.sheet_scale(doc) == pytest.approx(0.1)


def test_tag_size_overlap_and_no_text_in_results(doc):
    viewport(doc)
    small = tag(doc, height=0.5)
    other = tag(doc, point=(0.1, 0.1), height=0.8)
    checks = geometry.tag_checks(doc, [TAG_LAYER], geometry.sheet_scale(doc))
    assert checks["count"] == 2
    assert checks["too_small"] == [(small.dxf.handle, pytest.approx(1.27))]
    assert checks["overlaps"][0][:2] == (small.dxf.handle, other.dxf.handle)
    assert "SECRET-TAG" not in json.dumps(checks)


def test_readable_tags_and_nonoverlap_pass(doc, fixture, tmp_path):
    viewport(doc)
    tag(doc)
    tag(doc, point=(100, 0))
    row = pair(doc, fixture, tmp_path)
    assert all(row["steps"][str(n)] == "PASS" for n in range(2, 6))
    assert "SECRET-TAG" not in json.dumps(row)


def test_small_tag_failure_names_handle_and_layer(doc, fixture, tmp_path):
    viewport(doc)
    small = tag(doc, height=0.5)
    row = pair(doc, fixture, tmp_path)
    assert row["steps"]["3"] == "FAIL"
    assert small.dxf.handle in row["evidence"]["3"] and TAG_LAYER in row["evidence"]["3"]


def test_tag_height_at_limit_and_overlap_at_ten_percent(doc):
    height = 1.8 / 25.4
    tag(doc, height=height, text="ABCD")
    tag(doc, height=height, text="ABCD", point=(height * 0.6 * 4 * 0.9, 0))
    checks = geometry.tag_checks(doc, [TAG_LAYER], 1)
    assert checks["too_small"] == checks["overlaps"] == []


def test_mtext_checked_without_exposing_content(doc):
    doc.modelspace().add_mtext("PRIVATE\\PSECOND", dxfattribs={"layer": TAG_LAYER, "char_height": 0.1})
    checks = geometry.tag_checks(doc, [TAG_LAYER], 0.1)
    assert checks["count"] == 1 and len(checks["too_small"]) == 1
    assert "PRIVATE" not in json.dumps(checks)


def test_no_tags_is_unreadable_even_without_manifest_tag_layers(doc, fixture, tmp_path):
    viewport(doc)
    fixture.pop("tag_layers")
    assert pair(doc, fixture, tmp_path)["steps"]["3"] == "UNREADABLE"


def test_tags_without_viewport_are_unreadable(doc, fixture, tmp_path):
    tag(doc)
    assert pair(doc, fixture, tmp_path)["steps"]["3"] == "UNREADABLE"


def test_metres_tags_use_thousand_mm_per_unit(doc):
    doc.header["$INSUNITS"] = 6
    tag(doc, height=0.018)
    assert geometry.tag_checks(doc, [TAG_LAYER], 0.1)["too_small"] == []


@pytest.mark.parametrize("units,tag_height", [(0, 0.8), (1, 0.8), (6, 0.018)])
def test_all_units_grade_positions_tags_layers_and_fills(doc, fixture, tmp_path, units, tag_height):
    doc.header["$INSUNITS"] = units
    viewport(doc)
    tag(doc, height=tag_height)
    data = encoded(doc, tmp_path)
    fixture.update(insunits=units, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    row = pair(doc, fixture, tmp_path)
    assert row["observed_insunits"] == units and not row["units_mismatch"]
    assert all(row["steps"][str(n)] == "PASS" for n in range(2, 6))


def test_metres_default_tolerance_is_half_an_inch(doc, fixture, tmp_path):
    doc.header["$INSUNITS"] = 6
    fixture["insunits"] = 6
    encoded(doc, tmp_path)
    next(iter(doc.modelspace())).translate(0.02, 0, 0)
    assert pair(doc, fixture, tmp_path)["steps"]["2"] == "FAIL"


def test_synthetic_metres_generator_scales_rotates_and_removes_source_names(doc, tmp_path):
    panel(doc)
    tag(doc)
    original = encoded(doc, tmp_path)
    output = tmp_path / "metres.dxf"
    result = metres.generate(tmp_path / "drawing.dxf", [MODULE_LAYER], [], output,
                             rotate_deg=90, offset=(1000, 2000))
    generated = geometry.load(output.read_bytes())
    assert geometry.insunits(generated) == 6
    [(handle, polygon)] = geometry.module_footprints(generated, ["MODULES"], [])
    assert polygon.area == pytest.approx(200 * 0.0254 ** 2)
    assert polygon.bounds == pytest.approx((999.492, 2000, 1000, 2000.254))
    assert result == {"modules": 1, "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                      "bytes": output.stat().st_size}
    assert (tmp_path / "drawing.dxf").read_bytes() == original
    content = output.read_text()
    assert all(name not in content for name in (MODULE_LAYER, TAG_LAYER, BLOCK, "SECRET-TAG"))
    assert [entity.dxftype() for entity in generated.modelspace()] == ["LWPOLYLINE"]


def test_synthetic_metres_cli_with_insert(doc, tmp_path, capsys):
    block_panel(doc, point=(10, 20))
    encoded(doc, tmp_path)
    output = tmp_path / "metres.dxf"
    assert metres.main(["--in", str(tmp_path / "drawing.dxf"), "--layers", MODULE_LAYER,
                        "--blocks", BLOCK, "--out", str(output), "--rotate-deg", "0",
                        "--offset", "0", "0"]) == 0
    assert json.loads(capsys.readouterr().out)["modules"] == 1
    generated = geometry.load(output.read_bytes())
    [(handle, x, y)] = geometry.module_points(generated, ["MODULES"], [])
    assert (x, y) == pytest.approx((15 * 0.0254, 30 * 0.0254))
    assert BLOCK not in output.read_text() and MODULE_LAYER not in output.read_text()


def test_render_pdf(doc, tmp_path):
    pytest.importorskip("matplotlib")
    panel(doc)
    path = tmp_path / "evidence.pdf"
    geometry.render_pdf(doc, path)
    assert path.read_bytes().startswith(b"%PDF")


def scored_rows():
    rows = {}
    for units in (0, 1, 6):
        row = x01._row({"id": str(units)})
        row["observed_insunits"] = units
        row["steps"] = {str(n): "PASS" for n in range(1, 6)}
        row["evidence"] = {str(n): "satisfied" for n in range(1, 6)}
        rows[str(units)] = row
    return rows


def test_score_all_seven_steps_and_units_set():
    steps = x01.score(scored_rows())
    assert [step.n for step in steps] == list(range(1, 8))
    assert all(step.verdict == "PASS" for step in steps)


def test_score_units_missing_and_unreadable():
    rows = scored_rows()
    rows.pop("6")
    assert x01.score(rows)[5].verdict == "FAIL"
    rows["0"]["steps"]["3"] = "UNREADABLE"
    assert x01.score(rows)[5].verdict == "UNREADABLE"
    assert all(step.verdict == "UNREADABLE" for step in x01.score({}))


def test_score_failure_precedence_and_step_seven_names_item():
    rows = scored_rows()
    rows["0"]["steps"]["3"] = "UNREADABLE"
    rows["6"]["steps"]["4"] = "FAIL"
    rows["6"]["evidence"]["4"] = "module AB on layer HIDDEN is frozen"
    steps = x01.score(rows)
    assert steps[3].verdict == steps[5].verdict == steps[6].verdict == "FAIL"
    assert "AB" in steps[6].evidence and "HIDDEN" in steps[6].evidence
    rows["6"]["steps"]["2"] = "FAIL"
    assert x01.score(rows)[6].verdict == "UNREADABLE"


def test_step_seven_no_visual_failure_passes():
    rows = scored_rows()
    rows["1"]["steps"]["2"] = "FAIL"
    assert x01.score(rows)[6].verdict == "PASS"


class Wire:
    """Exercise the real StudioClient, including normal DXF and job routes."""

    def __init__(self, content):
        self.content = content
        self.head, self.rev = "v1", 0
        self.calls = []
        self.refuse = None
        self.async_refuse = False
        self.transport_error = False
        self.released = False
        self.deleted = False

    def transport(self, method, path, **kw):
        self.calls.append((method, path, deepcopy(kw)))
        if path == "/api/projects" and method == "POST":
            return 200, {"created": True, "project": PROJECT}
        if path == "/api/projects/p" and method == "DELETE":
            self.deleted = True
            return 200, {}
        if path == "/api/drawings/upload":
            if self.transport_error:
                raise ConnectionError("offline")
            assert self.content in kw["data"]
            return 202, {"drawing_id": "uploaded"}
        if path.endswith("/upload-status"):
            return 200, {"status": "ready"}
        if path.endswith("/drawing-versions/import"):
            assert kw["json_body"]["source"]["version"] == 1
            return 200, {"drawing_version": {"drawing_id": "d", "version_id": "v1"}}
        if path.endswith("/dxf"):
            return 200, self.content
        if path.endswith("/checkout"):
            if method == "DELETE":
                assert kw["headers"]["X-Checkout-Capability"] == CAP
                self.released = True
                return 200, {}
            return 200, {"checkout_capability": CAP, "checkout": {}}
        if "/context?" in path:
            return 200, {"context": {"head_version_id": self.head, "intake_sha256": "a" * 64},
                         "intake": {"solar_design_graph": {"rev": self.rev}}}
        if path == "/api/tools":
            return 200, {"tools": [{"name": name, "catalog_digest": "digest"}
                                    for name in ("solar-settings", "solar-panels-from-drawing")]}
        if path == "/api/run":
            tool = kw["json_body"]["tool"]
            assert kw["headers"]["X-Checkout-Capability"] == CAP
            if tool == "solar-panels-from-drawing" and self.refuse:
                return self.refuse, {"error": {"error_code": "IMPORT_REFUSED",
                                              "reason_code": "NO_PANELS_RECOGNISED",
                                              "message": "module layer filter refused " + CAP}}
            return 202, {"job_id": "settings" if tool == "solar-settings" else "panels"}
        if path.startswith("/api/jobs/"):
            if path.endswith("panels") and self.async_refuse:
                return 200, {"status": "failed", "error": {"reason_code": "NO_PANELS_RECOGNISED",
                                                            "message": "panel import refused"}}
            self.rev += 1
            self.head = "v2" if path.endswith("settings") else "v3"
            return 200, {"status": "complete"}
        raise AssertionError((method, path))


def product(fixture, *, wire=None, out_dir=None):
    wire = wire or Wire(Path(fixture["path"]).read_bytes())
    rows = x01.grade_files(StudioClient(wire.transport), project=PROJECT, fixtures=[fixture], out_dir=out_dir)
    return wire, rows[fixture["id"]]


def test_product_flow_original_and_refreshed_export_and_source_unchanged(fixture):
    source = Path(fixture["path"]).read_bytes()
    wire, row = product(fixture)
    assert row["steps"]["1"] == row["steps"]["2"] == "PASS"
    assert row["original_version_id"] == "v1" and row["export_version_id"] == "v3"
    assert row["dxf_sha256"] == {"original": fixture["sha256"], "export": fixture["sha256"]}
    assert row["source_sha256_before"] == row["source_sha256_after"] == fixture["sha256"]
    assert wire.released and Path(fixture["path"]).read_bytes() == source
    gets = [path for method, path, _ in wire.calls if path.endswith("/dxf")]
    assert gets == ["/api/projects/p/drawings/d/versions/v1/dxf", "/api/projects/p/drawings/d/versions/v3/dxf"]
    writes = [kw["json_body"] for _, path, kw in wire.calls if path == "/api/run"]
    assert writes[0]["params"]["changes"] == {"panel_layer_contains": MODULE_LAYER}
    assert writes[0]["params"]["initialize"]["units"]["drawing_units"] == "in"
    assert writes[1]["dwg"] == "v2" and writes[1]["params"]["expected_rev"] == 1
    assert CAP not in json.dumps(row)


def test_product_metres_initialization_and_custom_filter(doc, fixture, tmp_path):
    doc.header["$INSUNITS"] = 6
    data = encoded(doc, tmp_path)
    fixture.update(insunits=6, bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), panel_layer_contains="custom")
    wire, row = product(fixture)
    settings = next(kw["json_body"]["params"] for _, path, kw in wire.calls if path == "/api/run")
    assert settings["initialize"]["units"]["drawing_units"] == "m"
    assert settings["changes"]["panel_layer_contains"] == "custom"
    assert row["observed_insunits"] == 6 and row["steps"]["2"] == "PASS"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_product_panels_refusal_fails_steps_two_to_five(fixture, asynchronous):
    wire = Wire(Path(fixture["path"]).read_bytes())
    wire.async_refuse = asynchronous
    wire.refuse = None if asynchronous else 422
    wire, row = product(fixture, wire=wire)
    assert all(row["steps"][str(n)] == "FAIL" for n in range(2, 6))
    assert all("NO_PANELS_RECOGNISED" in row["evidence"][str(n)] for n in range(2, 6))
    assert "refused" in row["evidence"]["2"] and CAP not in json.dumps(row)
    assert wire.released


def test_product_auth_and_transport_are_unreadable(fixture):
    for auth in (True, False):
        wire = Wire(Path(fixture["path"]).read_bytes())
        wire.refuse = 401 if auth else None
        wire.transport_error = not auth
        _, row = product(fixture, wire=wire)
        assert all(row["steps"][str(n)] == "UNREADABLE" for n in range(1, 6))


def test_source_mutation_fails_integrity(fixture):
    wire = Wire(Path(fixture["path"]).read_bytes())
    original = wire.transport

    def transport(method, path, **kw):
        if path.endswith("/versions/v3/dxf"):
            Path(fixture["path"]).write_bytes(b"mutated test fixture")
        return original(method, path, **kw)

    rows = x01.grade_files(StudioClient(transport), project=PROJECT, fixtures=[fixture])
    assert rows[fixture["id"]]["steps"]["1"] == "FAIL"


def test_run_writes_fixture_receipt_and_plot(fixture, tmp_path, monkeypatch):
    wire = Wire(Path(fixture["path"]).read_bytes())
    ctx = SimpleNamespace(manifest={"fixtures": [fixture]}, fixtures=[{"id": fixture["id"], "ok": True}],
                          base="https://example.test", source_sha="b" * 40, out_dir=tmp_path / "evidence")

    @contextmanager
    def signed_in(repo, source_sha, *, origin):
        assert repo == Path(x01.__file__).resolve().parents[3]
        assert source_sha == ctx.source_sha and origin == ctx.base
        yield "page", {}

    monkeypatch.setattr(x01.xbench_session, "signed_in", signed_in)
    monkeypatch.setattr(x01, "_transport", lambda page, **kwargs: wire.transport)
    monkeypatch.setattr(geometry, "render_pdf", lambda doc, path: Path(path).write_bytes(b"%PDF evidence"))
    assert len(x01.run(ctx)) == 7
    raw = (ctx.out_dir / "fixtures.json").read_text()
    row = json.loads(raw)[fixture["id"]]
    assert row["plot"] == "plot-synthetic.pdf" and (ctx.out_dir / row["plot"]).is_file()
    assert row["original_version_id"] == "v1" and row["export_version_id"] == "v3"
    assert CAP not in raw and "SECRET-TAG" not in raw
    assert wire.deleted and wire.released
