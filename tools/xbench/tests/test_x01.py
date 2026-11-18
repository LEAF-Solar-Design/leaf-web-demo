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
from xbench_studio import StudioClient, StudioError


PROJECT = {"project_id": "p", "org_id": "o"}
CAP = "private-checkout-capability-123456789"
MODULE_LAYER = "PRIVATE_MODULE_LAYER"
TAG_LAYER = "PRIVATE_TAG_LAYER"
BLOCK = "PRIVATE_PANEL_BLOCK"
STRINGING = {
    "tools": [
        {"name": "solar-panel-groups", "params": {"discover": {
            "BranchMaxOffset": 1, "AlignmentTolerance": 0.1, "InstallationDesign": "Roof"}}},
        {"name": "solar-string-add", "params": {"operation": "add-string", "ordered_panel_refs": ["panel-1"]}},
    ],
    # This mock catalog entry stands in for the deployment's manifest-named tag tool.
    "tag_tool": {"name": "xbench-string-tags", "params": {"height": 0.8}},
}


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


def test_match_moved_handle_keeps_export_reserved_for_pending():
    result = geometry.match_positions([("B", 3, 0), ("A", 0, 0)], [("A", 3, 0)], 0.5)
    assert result == {"matched": 0, "missing": ["B"], "moved": [("A", 3.0)], "extra": 0}


@pytest.mark.parametrize("reverse", [False, True])
def test_match_maximum_cardinality_in_both_input_orders(reverse):
    original = [("a", 0, 0), ("b", 0.3, 0)]
    if reverse:
        original.reverse()
    result = geometry.match_positions(original, [("x", 0.1, 0), ("y", -0.4, 0)], 0.5)
    assert result == {"matched": 2, "missing": [], "moved": [], "extra": 0}


def test_match_alternating_chain_under_two_seconds():
    original = [(f"O{i}", (1999 - i) * 0.3, 0) for i in range(2000)]
    # The leftmost choices consume the next original's export. Matching the
    # final original requires an alternating path back to the far endpoint.
    exported = [(f"E{i}", x + 0.1, y) for i, (_, x, y) in enumerate(original[:-1])]
    exported.append(("endpoint", original[0][1] + 0.4, 0))
    started = time.perf_counter()
    result = geometry.match_positions(original, exported, 0.5)
    elapsed = time.perf_counter() - started
    assert result == {"matched": 2000, "missing": [], "moved": [], "extra": 0}
    assert elapsed < 2


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
    assert geometry.fill_cover(doc, geometry.module_footprints(doc, [MODULE_LAYER], [])) == ([], [])


def test_sortentstable_can_move_fill_below_module(doc):
    module = panel(doc)
    fill = hatch(doc)
    doc.modelspace().set_redraw_order({module.dxf.handle: "20", fill.dxf.handle: "10"})
    assert geometry.fill_cover(doc, geometry.module_footprints(doc, [MODULE_LAYER], [])) == ([], [])


def test_sortentstable_zero_moves_fill_above_module(doc):
    fill = hatch(doc)
    module = panel(doc)
    doc.modelspace().set_redraw_order({module.dxf.handle: "10", fill.dxf.handle: "0"})
    failures, unanalysable = geometry.fill_cover(doc, geometry.module_footprints(doc, [MODULE_LAYER], []))
    [(handle, fill_handle, fraction)] = failures
    assert unanalysable == []
    assert (handle, fill_handle) == (module.dxf.handle, fill.dxf.handle)
    assert fraction == pytest.approx(0.3)


def test_hatch_hole_is_not_coverage(doc):
    panel(doc)
    fill = doc.modelspace().add_hatch()
    fill.paths.add_polyline_path([(-10, -10), (30, -10), (30, 40), (-10, 40)])
    fill.paths.add_polyline_path([(-1, -1), (11, -1), (11, 21), (-1, 21)], flags=0)
    assert geometry.fill_cover(doc, geometry.module_footprints(doc, [MODULE_LAYER], [])) == ([], [])


def test_bow_tie_hatch_is_repaired_and_covers_module(doc, fixture, tmp_path):
    fill = doc.modelspace().add_hatch()
    fill.paths.add_polyline_path([(0, 0), (10, 20), (0, 20), (10, 0)], is_closed=True)
    failures, unanalysable = geometry.fill_cover(doc, geometry.module_footprints(doc, [MODULE_LAYER], []))
    assert unanalysable == []
    assert failures == [(next(iter(doc.modelspace())).dxf.handle, fill.dxf.handle, pytest.approx(0.5))]
    assert pair(doc, fixture, tmp_path)["steps"]["5"] == "FAIL"


@pytest.mark.parametrize("closed", [False, True])
def test_degenerate_hatch_boundary_is_skipped_beside_real_boundary(doc, closed):
    panel(doc)
    fill = hatch(doc)
    fill.paths.add_polyline_path([(0, 0), (1, 0)], is_closed=closed, flags=0)
    failures, unanalysable = geometry.fill_cover(doc, geometry.module_footprints(doc, [MODULE_LAYER], []))
    assert unanalysable == []
    assert failures[0][1:] == (fill.dxf.handle, pytest.approx(0.3))


@pytest.mark.parametrize("width,verdict", [(None, "UNREADABLE"), (0.5, "PASS"), (3, "FAIL")])
def test_degenerate_hatch_does_not_hide_analysable_fills(doc, fixture, tmp_path, width, verdict):
    bad = doc.modelspace().add_hatch()
    bad.paths.add_polyline_path([(0, 0), (1, 0)], is_closed=True)
    if width is not None:
        good = hatch(doc, width=width)
    failures, unanalysable = geometry.fill_cover(doc, geometry.module_footprints(doc, [MODULE_LAYER], []))
    assert unanalysable == [bad.dxf.handle]
    assert bool(failures) == (verdict == "FAIL")
    row = pair(doc, fixture, tmp_path)
    assert row["steps"]["5"] == verdict
    assert "unanalysable fills 1" in row["evidence"]["5"]
    assert bad.dxf.handle in row["evidence"]["5"]
    if verdict == "FAIL":
        assert row["failures"]["5"][0]["fill"]["handle"] == good.dxf.handle


def test_unanalysable_fill_is_reported_without_modules(doc):
    bad = doc.modelspace().add_hatch()
    bad.paths.add_polyline_path([(0, 0), (1, 0)], is_closed=True)
    assert geometry.fill_cover(doc, []) == ([], [bad.dxf.handle])


def test_single_fill_exception_does_not_abort_other_fills(doc, monkeypatch):
    module = panel(doc)
    bad, good = hatch(doc), hatch(doc)
    original = geometry._fill_polygon

    def fill_polygon(entity):
        if entity is bad:
            raise ValueError("unreadable fill")
        return original(entity)

    monkeypatch.setattr(geometry, "_fill_polygon", fill_polygon)
    failures, unanalysable = geometry.fill_cover(doc, geometry.module_footprints(doc, [MODULE_LAYER], []))
    assert unanalysable == [bad.dxf.handle]
    assert failures == [(module.dxf.handle, good.dxf.handle, pytest.approx(0.3))]


def test_unanalysable_fill_evidence_limits_handles(doc, fixture, tmp_path):
    handles = []
    for _ in range(12):
        bad = doc.modelspace().add_hatch()
        bad.paths.add_polyline_path([(0, 0), (1, 0)], is_closed=True)
        handles.append(bad.dxf.handle)
    row = pair(doc, fixture, tmp_path)
    assert row["steps"]["5"] == "UNREADABLE"
    assert row["evidence"]["5"] == f"covering fills []; unanalysable fills 12; handles {handles[:10]}"


@pytest.mark.parametrize("kind", ["SOLID", "TRACE"])
def test_quad_fill_and_five_percent_threshold(doc, kind):
    panel(doc)
    add = doc.modelspace().add_solid if kind == "SOLID" else doc.modelspace().add_trace
    # DXF SOLID/TRACE order is lower-left, lower-right, upper-left, upper-right.
    add([(0, 0), (0.5, 0), (0, 20), (0.5, 20)])
    assert geometry.fill_cover(doc, geometry.module_footprints(doc, [MODULE_LAYER], [])) == ([], [])
    add([(0, 0), (3, 0), (0, 20), (3, 20)])
    failures, unanalysable = geometry.fill_cover(doc, geometry.module_footprints(doc, [MODULE_LAYER], []))
    assert unanalysable == []
    assert failures[0][2] == pytest.approx(0.3)


def test_viewport_scale_and_no_viewport(doc):
    assert geometry.sheet_scale(doc) is None
    viewport(doc)
    assert geometry.sheet_scale(doc) == pytest.approx(0.1)


def test_tag_size_overlap_and_no_text_in_results(doc):
    viewport(doc, view_height=1000 / 25.4)
    small = tag(doc, height=0.5)
    other = tag(doc, point=(0.1, 0.1), height=0.8)
    checks = geometry.tag_checks(doc, [TAG_LAYER], geometry.sheet_scale(doc))
    assert checks["count"] == 2
    assert checks["too_small"] == [(small.dxf.handle, pytest.approx(1.27))]
    assert checks["overlaps"][0][:2] == (small.dxf.handle, other.dxf.handle)
    assert "SECRET-TAG" not in json.dumps(checks)


def test_readable_tags_and_nonoverlap_pass(doc, fixture, tmp_path):
    fixture["stringing"] = deepcopy(STRINGING)
    viewport(doc, view_height=1000 / 25.4)
    tag(doc)
    tag(doc, point=(100, 0))
    row = pair(doc, fixture, tmp_path)
    assert all(row["steps"][str(n)] == "PASS" for n in range(2, 6))
    assert "SECRET-TAG" not in json.dumps(row)


def test_small_tag_failure_names_handle_and_layer(doc, fixture, tmp_path):
    fixture["stringing"] = deepcopy(STRINGING)
    viewport(doc, view_height=1000 / 25.4)
    small = tag(doc, height=0.5)
    row = pair(doc, fixture, tmp_path)
    assert row["steps"]["3"] == "FAIL"
    assert small.dxf.handle in row["evidence"]["3"] and TAG_LAYER in row["evidence"]["3"]


def test_tag_height_at_limit_and_overlap_at_ten_percent(doc):
    from ezdxf import bbox

    height = 1.8 / 25.4
    first = tag(doc, height=height, text="ABCD")
    width = bbox.extents([first]).size.x
    tag(doc, height=height, text="ABCD", point=(width * 0.9, 0))
    checks = geometry.tag_checks(doc, [TAG_LAYER], 25.4)
    assert checks["too_small"] == checks["overlaps"] == []


def test_mtext_checked_without_exposing_content(doc):
    doc.modelspace().add_mtext("PRIVATE\\PSECOND", dxfattribs={"layer": TAG_LAYER, "char_height": 0.1})
    checks = geometry.tag_checks(doc, [TAG_LAYER], 0.1)
    assert checks["count"] == 1 and len(checks["too_small"]) == 1
    assert "PRIVATE" not in json.dumps(checks)


def test_no_tags_is_unreadable_even_without_manifest_tag_layers(doc, fixture, tmp_path):
    viewport(doc)
    fixture.pop("tag_layers")
    row = pair(doc, fixture, tmp_path)
    assert row["steps"]["3"] == "UNREADABLE"
    assert row["evidence"]["3"] == "no recorded stringing settings in manifest"


def test_tags_without_viewport_are_unreadable(doc, fixture, tmp_path):
    fixture["stringing"] = deepcopy(STRINGING)
    tag(doc)
    row = pair(doc, fixture, tmp_path)
    assert row["steps"]["3"] == "UNREADABLE"
    assert "no paper-space model viewport scale" in row["evidence"]["3"]


def test_tags_without_stringing_settings_stay_unreadable(doc, fixture, tmp_path):
    viewport(doc)
    tag(doc)
    row = pair(doc, fixture, tmp_path)
    assert row["steps"]["3"] == "UNREADABLE"
    assert row["evidence"]["3"] == "no recorded stringing settings in manifest"


def test_configured_stringing_without_exported_tags_fails(doc, fixture, tmp_path):
    fixture["stringing"] = deepcopy(STRINGING)
    fixture.pop("tag_layers")
    row = pair(doc, fixture, tmp_path)
    assert row["steps"]["3"] == "FAIL"
    assert row["evidence"]["3"] == "stringing ran but no tags exported"


def test_overlapping_tags_fail_naming_both_handles(doc, fixture, tmp_path):
    fixture.update(stringing=deepcopy(STRINGING), sheet_scale=0.4)
    first = tag(doc)
    second = tag(doc, point=(0.1, 0.1))
    row = pair(doc, fixture, tmp_path)
    assert row["steps"]["3"] == "FAIL"
    assert first.dxf.handle in row["evidence"]["3"] and second.dxf.handle in row["evidence"]["3"]
    assert row["failures"]["3"][0]["fraction"] > 0.1


@pytest.mark.parametrize("units", [0, 1, 6])
@pytest.mark.parametrize("height,expected", [(1.8, "PASS"), (1.7, "FAIL")])
def test_manifest_scale_is_model_units_per_paper_mm(doc, fixture, tmp_path, units, height, expected):
    doc.header["$INSUNITS"] = units
    # The recorded scale takes precedence over this deliberately unsuitable viewport.
    viewport(doc, height=1, view_height=100000)
    entity = tag(doc, height=height * 10)
    fixture.update(insunits=units, sheet_scale=10, stringing=deepcopy(STRINGING))
    row = pair(doc, fixture, tmp_path)
    assert row["steps"]["3"] == expected
    assert row["sheet_scale"] == 10 and row["sheet_scale_source"] == "manifest"
    if expected == "FAIL":
        assert row["failures"]["3"][0]["handle"] == entity.dxf.handle
        assert row["failures"]["3"][0]["paper_mm"] == pytest.approx(height)


def test_manifest_scale_works_without_viewport_for_mtext(doc, fixture, tmp_path):
    fixture.update(sheet_scale=10, stringing=deepcopy(STRINGING))
    doc.modelspace().add_mtext("PRIVATE\\PSECOND", dxfattribs={"layer": TAG_LAYER, "char_height": 18})
    row = pair(doc, fixture, tmp_path)
    assert row["steps"]["3"] == "PASS" and row["tag_count"] == 1
    assert "PRIVATE\\PSECOND" not in json.dumps(row)


@pytest.mark.parametrize("scale", [0, -1, float("inf"), float("nan")])
def test_invalid_manifest_scale_is_unreadable(doc, fixture, tmp_path, scale):
    tag(doc)
    fixture.update(sheet_scale=scale, stringing=deepcopy(STRINGING))
    assert pair(doc, fixture, tmp_path)["steps"]["3"] == "UNREADABLE"


def test_metres_tags_use_viewport_paper_mm_per_model_unit(doc):
    doc.header["$INSUNITS"] = 6
    tag(doc, height=0.018)
    assert geometry.tag_checks(doc, [TAG_LAYER], 100)["too_small"] == []


@pytest.mark.parametrize("units", [0, 1, 6])
@pytest.mark.parametrize("height,expected", [(18, "PASS"), (17, "FAIL")])
def test_viewport_scale_is_paper_mm_per_model_unit(doc, fixture, tmp_path, units, height, expected):
    doc.header["$INSUNITS"] = units
    fixture.update(insunits=units, stringing=deepcopy(STRINGING))
    viewport(doc)
    tag(doc, height=height)
    row = pair(doc, fixture, tmp_path)
    assert row["steps"]["3"] == expected
    assert row["sheet_scale_source"] == "viewport"


def test_viewport_with_paper_inches_converts_to_mm(doc):
    viewport(doc)
    doc.layouts.get("Layout1").dxf.plot_paper_units = 0
    assert geometry.sheet_scale(doc) == pytest.approx(2.54)


@pytest.mark.parametrize("units,tag_height", [(0, 0.8), (1, 0.8), (6, 0.018)])
def test_all_units_grade_positions_tags_layers_and_fills(doc, fixture, tmp_path, units, tag_height):
    fixture["stringing"] = deepcopy(STRINGING)
    doc.header["$INSUNITS"] = units
    viewport(doc, view_height=1000 / (1000 if units == 6 else 25.4))
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
        self.drawing_version = {"drawing_id": "d", "version_id": "v1"}

    def transport(self, method, path, **kw):
        self.calls.append((method, path, deepcopy(kw)))
        if path == "/api/projects" and method == "GET":
            return 200, {"projects": [PROJECT]}
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
            return 200, {"drawing_version": self.drawing_version}
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


class StringingWire(Wire):
    """Manifest-named tools through the same catalog, run, job and DXF routes."""

    def __init__(self, content, export_content, *, asynchronous=True, fail_tool=None,
                 failure="http", report_layers=True):
        super().__init__(content)
        self.export_content = export_content
        self.asynchronous = asynchronous
        self.fail_tool, self.failure = fail_tool, failure
        self.report_layers = report_layers
        self.names = [tool["name"] for tool in STRINGING["tools"]] + [STRINGING["tag_tool"]["name"]]

    def finish(self, tool):
        index = self.names.index(tool)
        self.rev += 1
        self.head = f"v{index + 4}"
        if tool == self.fail_tool:
            error = {"reason_code": "STRINGING_REFUSED", "message": "recorded settings refused " + CAP}
            if self.failure == "nested":
                return {"status": "complete", "result": {"ok": False, "error": error}}
            if self.failure == "refused":
                return {"status": "complete", "result": {"status": "refused", **error}}
            return {"status": "failed", "error": error}
        result = {"tag_layers": [TAG_LAYER]} if self.report_layers and tool == self.names[-1] else {}
        return {"status": "complete", "result": {"ok": True, "result": result}}

    def transport(self, method, path, **kw):
        if path == "/api/tools":
            self.calls.append((method, path, deepcopy(kw)))
            return 200, {"tools": [{"name": name, "catalog_digest": "digest",
                                    "params": {"properties": {"drawing_id": {}, "expected_rev": {}}}}
                                   for name in ["solar-settings", "solar-panels-from-drawing", *self.names]]}
        if path == "/api/run" and kw["json_body"]["tool"] in self.names:
            self.calls.append((method, path, deepcopy(kw)))
            body = kw["json_body"]
            tool = body["tool"]
            assert body["params"]["drawing_id"] == "d"
            assert body["params"]["expected_rev"] == self.rev
            assert body["dwg"] == self.head
            assert kw["headers"]["X-Checkout-Capability"] == CAP
            assert kw["headers"]["Idempotency-Key"]
            if tool == self.fail_tool and self.failure == "http":
                return 422, {"error": {"reason_code": "STRINGING_REFUSED",
                                       "message": "recorded settings refused " + CAP}}
            if self.asynchronous:
                return 202, {"job_id": "run-" + tool}
            return 200, {"run_id": "run-" + tool, **self.finish(tool)}
        if path.startswith("/api/jobs/run-"):
            self.calls.append((method, path, deepcopy(kw)))
            return 200, self.finish(path.removeprefix("/api/jobs/run-"))
        if path.endswith("/dxf") and not path.endswith("/versions/v1/dxf"):
            self.calls.append((method, path, deepcopy(kw)))
            return 200, self.export_content
        return super().transport(method, path, **kw)


def product(fixture, *, wire=None, out_dir=None):
    wire = wire or Wire(Path(fixture["path"]).read_bytes())
    rows = x01.grade_files(StudioClient(wire.transport), project=PROJECT, fixtures=[fixture], out_dir=out_dir)
    return wire, rows[fixture["id"]]


def test_row_initializes_import_diagnostics():
    row = x01._row({"id": "synthetic"})
    assert row["upload_drawing_id"] is None
    assert row["import_provenance"] is None
    assert row["failed_stage"] is None


@pytest.mark.parametrize("method,tool,expected_stage", [
    ("checkout", None, "checkout"),
    ("drawing_context", None, "context"),
    ("run", "solar-settings", "run:solar-settings"),
    ("run", "solar-panels-from-drawing", "run:solar-panels-from-drawing"),
])
def test_product_records_failed_studio_stage(fixture, monkeypatch, method, tool, expected_stage):
    wire = Wire(Path(fixture["path"]).read_bytes())
    client = StudioClient(wire.transport)
    original = getattr(client, method)

    def fail(*args, **kwargs):
        if tool is None or args[0] == tool:
            raise StudioError(503, "SIP_R1_INTAKE_UNAVAILABLE", "Project Solar admission unavailable.")
        return original(*args, **kwargs)

    monkeypatch.setattr(client, method, fail)
    row = x01.grade_files(client, project=PROJECT, fixtures=[fixture])[fixture["id"]]
    assert row["failed_stage"] == expected_stage
    assert row["product_error"]["stage"] == expected_stage
    assert row["product_error"]["status"] == 503
    assert row["product_error"]["code"] == "SIP_R1_INTAKE_UNAVAILABLE"
    assert all(row["steps"][str(n)] == "UNREADABLE" for n in range(1, 6))
    assert wire.released is (method != "checkout")


@pytest.mark.parametrize("source,expected", [
    ({"drawing_id": "uploaded", "version": 1, "kind": "account_upload",
      "intake": {"ref": "intake-ref", "sha256": "a" * 64, "private": "excluded"}},
     {"drawing_id": "uploaded", "version": 1, "intake": {"ref": "intake-ref", "sha256": "a" * 64}}),
    ({"version": 1, "intake": {"ref": "intake-ref"}},
     {"version": 1, "intake": {"ref": "intake-ref"}}),
    ({"drawing_id": "uploaded"}, {"drawing_id": "uploaded"}),
])
def test_product_records_only_allowed_import_provenance(fixture, source, expected):
    wire = Wire(Path(fixture["path"]).read_bytes())
    wire.drawing_version["provenance"] = {"source": source, "private": "excluded"}
    _, row = product(fixture, wire=wire)
    assert row["upload_drawing_id"] == "uploaded"
    assert row["import_provenance"] == expected
    assert row["steps"]["1"] == row["steps"]["2"] == "PASS"


@pytest.mark.parametrize("extra", [{}, {"provenance": None}, {"provenance": {}},
                                   {"provenance": {"source": None}}])
def test_product_missing_import_provenance_is_harmless(fixture, extra):
    wire = Wire(Path(fixture["path"]).read_bytes())
    wire.drawing_version.update(extra)
    _, row = product(fixture, wire=wire)
    assert row["upload_drawing_id"] == "uploaded"
    assert row["import_provenance"] is None
    assert row["steps"]["1"] == row["steps"]["2"] == "PASS"
    assert wire.released


def test_product_upload_id_survives_upload_wait_failure(fixture, monkeypatch):
    client = StudioClient(Wire(Path(fixture["path"]).read_bytes()).transport)

    def wait_upload(upload):
        assert upload == "uploaded"
        raise RuntimeError("conversion unavailable")

    monkeypatch.setattr(client, "wait_upload", wait_upload)
    row = x01.grade_files(client, project=PROJECT, fixtures=[fixture])[fixture["id"]]
    assert row["upload_drawing_id"] == "uploaded"
    assert row["import_provenance"] is None
    assert row["steps"]["1"] == "UNREADABLE"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("report_layers", [False, True])
def test_product_runs_manifest_tools_in_order_with_evidence(doc, fixture, tmp_path, asynchronous, report_layers):
    fixture.update(stringing=deepcopy(STRINGING), sheet_scale=0.4)
    if report_layers:
        fixture["tag_layers"] = ["UNUSED_MANIFEST_LAYER"]
    recorded = deepcopy(fixture["stringing"])
    tag(doc)
    wire = StringingWire(Path(fixture["path"]).read_bytes(), encoded(doc, tmp_path, "export.dxf"),
                         asynchronous=asynchronous, report_layers=report_layers)
    _, row = product(fixture, wire=wire)
    assert all(row["steps"][str(n)] == "PASS" for n in range(1, 6))
    assert row["export_version_id"] == "v6"
    assert fixture["stringing"] == recorded
    runs = row["stringing_runs"]
    assert [run["name"] for run in runs] == wire.names
    for index, run in enumerate(runs):
        assert run["status"] == "complete" and run["run_id"] == "run-" + run["name"]
        assert run["params"]["expected_rev"] == index + 2 and run["params"]["drawing_id"] == "d"
    assert runs[0]["params"]["discover"] == recorded["tools"][0]["params"]["discover"]
    assert runs[1]["params"]["ordered_panel_refs"] == ["panel-1"]
    assert runs[2]["params"]["height"] == 0.8
    writes = [kw["json_body"] for _, path, kw in wire.calls if path == "/api/run"]
    assert [write["tool"] for write in writes] == ["solar-settings", "solar-panels-from-drawing", *wire.names]
    assert [write["dwg"] for write in writes] == ["v1", "v2", "v3", "v4", "v5"]
    assert wire.calls[-2][1] == "/api/projects/p/drawings/d/versions/v6/dxf"
    assert CAP not in json.dumps(row) and "SECRET-TAG" not in json.dumps(row)
    assert wire.released


@pytest.mark.parametrize("tool", ["solar-panel-groups", "solar-string-add", "xbench-string-tags"])
@pytest.mark.parametrize("failure", ["http", "async", "nested", "refused"])
def test_product_stringing_refusal_only_makes_step_three_unreadable(doc, fixture, tmp_path, tool, failure):
    fixture.update(stringing=deepcopy(STRINGING), sheet_scale=0.4)
    tag(doc)
    wire = StringingWire(Path(fixture["path"]).read_bytes(), encoded(doc, tmp_path, "export.dxf"),
                         fail_tool=tool, failure=failure)
    _, row = product(fixture, wire=wire)
    assert all(row["steps"][str(n)] == "PASS" for n in (1, 2, 4, 5))
    assert row["steps"]["3"] == "UNREADABLE"
    assert tool in row["evidence"]["3"] and "STRINGING_REFUSED" in row["evidence"]["3"]
    runs = row["stringing_runs"]
    assert [run["name"] for run in runs] == wire.names[:wire.names.index(tool) + 1]
    assert runs[-1]["status"] == ("refused" if failure in ("http", "refused") else "failed")
    assert runs[-1]["reason_code"] == "STRINGING_REFUSED"
    assert runs[-1]["run_id"] == (None if failure == "http" else "run-" + tool)
    assert row["export_version_id"] == wire.head and wire.released
    assert CAP not in json.dumps(row)


def test_product_stringing_without_tags_fails(fixture):
    fixture["stringing"] = deepcopy(STRINGING)
    data = Path(fixture["path"]).read_bytes()
    _, row = product(fixture, wire=StringingWire(data, data))
    assert row["steps"]["3"] == "FAIL"
    assert row["evidence"]["3"] == "stringing ran but no tags exported"


def test_product_missing_catalog_tool_is_unreadable_and_still_exports(fixture):
    fixture["stringing"] = {"tools": [], "tag_tool": deepcopy(STRINGING["tag_tool"])}
    wire, row = product(fixture)
    assert row["steps"]["3"] == "UNREADABLE"
    assert "xbench-string-tags" in row["evidence"]["3"] and "TOOL_NOT_IN_CATALOG" in row["evidence"]["3"]
    assert all(row["steps"][str(n)] == "PASS" for n in (1, 2, 4, 5))
    assert row["export_version_id"] == "v3" and wire.released


def test_product_flow_original_and_refreshed_export_and_source_unchanged(fixture):
    source = Path(fixture["path"]).read_bytes()
    wire, row = product(fixture)
    assert row["failed_stage"] is None
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
    assert row["steps"]["3"] == "UNREADABLE"
    assert row["evidence"]["3"] == "no recorded stringing settings in manifest"
    assert CAP not in json.dumps(row)


@pytest.mark.parametrize("missing_head_rev", [False, True])
def test_product_reads_revision_from_new_head_after_each_write(fixture, missing_head_rev):
    class VersionedWire(Wire):
        def __init__(self, content):
            super().__init__(content)
            self.context_versions = []

        def transport(self, method, path, **kw):
            if "/context?" in path:
                version = path.split("/drawing-versions/")[1].split("/")[0]
                self.context_versions.append(version)
                graph = {} if missing_head_rev and version != "v1" else {
                    "rev": {"v1": 0, "v2": 1, "v3": 2}[version]}
                return 200, {"context": {"head_version_id": self.head, "intake_sha256": "a" * 64},
                             "intake": {"solar_design_graph": graph}}
            if path == "/api/run":
                body = kw["json_body"]
                if (body["tool"] == "solar-panels-from-drawing"
                        and body["params"]["expected_rev"] != self.rev):
                    return 409, {"error": {"error_code": "STALE_GRAPH_REVISION",
                                           "message": "expected_rev must match the head revision"}}
            return super().transport(method, path, **kw)

    wire = VersionedWire(Path(fixture["path"]).read_bytes())
    _, row = product(fixture, wire=wire)
    assert row["steps"]["1"] == row["steps"]["2"] == "PASS"
    assert row["export_version_id"] == "v3"
    assert wire.context_versions == ["v1", "v1", "v2", "v2", "v3"]
    writes = [kw["json_body"] for _, path, kw in wire.calls if path == "/api/run"]
    assert writes[1]["params"]["expected_rev"] == 1
    assert wire.released


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


@pytest.mark.parametrize("code,expected_code", [
    ("SIP_R1_INTAKE_UNAVAILABLE", "SIP_R1_INTAKE_UNAVAILABLE"),
    ("private-token-123456789012345", "[redacted]"),
])
def test_product_refusal_preserves_reason_code_and_redacts_message(fixture, code, expected_code):
    wire = Wire(Path(fixture["path"]).read_bytes())
    original = wire.transport
    token = "message-token-123456789012345"

    def transport(method, path, **kw):
        if path == "/api/run":
            return 422, {"error": {"reason_code": code,
                                    "message": "Project Solar admission unavailable. " + token}}
        return original(method, path, **kw)

    rows = x01.grade_files(StudioClient(transport), project=PROJECT, fixtures=[fixture])
    row = rows[fixture["id"]]
    note = f"{expected_code}: Project Solar admission unavailable. [redacted]"
    assert row["product_error"]["message"] == note
    assert all(row["evidence"][str(n)] == note for n in range(2, 6))
    assert all(row["steps"][str(n)] == "FAIL" for n in range(2, 6))
    assert token not in json.dumps(row)
    assert wire.released


def test_product_auth_and_transport_are_unreadable(fixture):
    for auth in (True, False):
        wire = Wire(Path(fixture["path"]).read_bytes())
        wire.refuse = 401 if auth else None
        wire.transport_error = not auth
        _, row = product(fixture, wire=wire)
        assert all(row["steps"][str(n)] == "UNREADABLE" for n in range(1, 6))


@pytest.mark.parametrize("message,expected", [
    ("fixture conversion unavailable", "fixture conversion unavailable"),
    ("conversion failed with " + CAP, "conversion failed with [redacted]"),
    ("conversion failed " * 30, ("conversion failed " * 30)[:300]),
])
def test_product_plain_exception_records_bounded_redacted_message(fixture, monkeypatch, message, expected):
    wire = Wire(Path(fixture["path"]).read_bytes())
    client = StudioClient(wire.transport)

    def upload_drawing(*args):
        raise RuntimeError(message)

    monkeypatch.setattr(client, "upload_drawing", upload_drawing)
    row = x01.grade_files(client, project=PROJECT, fixtures=[fixture])[fixture["id"]]
    note = f"fixture unavailable: RuntimeError: {expected}"
    assert all(row["steps"][str(n)] == "UNREADABLE" for n in range(1, 6))
    assert all(row["evidence"][str(n)] == note for n in range(2, 6))
    assert "product_error" not in row and CAP not in json.dumps(row)
    assert row["failed_stage"] == "upload"


def test_source_mutation_fails_integrity(fixture):
    wire = Wire(Path(fixture["path"]).read_bytes())
    original = wire.transport

    def transport(method, path, **kw):
        if path.endswith("/versions/v3/dxf"):
            Path(fixture["path"]).write_bytes(b"mutated test fixture")
        return original(method, path, **kw)

    rows = x01.grade_files(StudioClient(transport), project=PROJECT, fixtures=[fixture])
    assert rows[fixture["id"]]["steps"]["1"] == "FAIL"


@pytest.mark.parametrize("options", [None, {}, {"keep_project": "0"},
                                   {"keep_project": 1}, {"keep_project": "1"}])
def test_run_writes_fixture_receipt_and_plot(fixture, tmp_path, monkeypatch, options):
    wire = Wire(Path(fixture["path"]).read_bytes())
    unavailable = {**fixture, "id": "unavailable", "bytes": fixture["bytes"] + 1}
    ctx = SimpleNamespace(manifest={"fixtures": [fixture, unavailable]}, fixtures=[{"id": fixture["id"], "ok": True}],
                          base="https://example.test", source_sha="b" * 40, out_dir=tmp_path / "evidence")
    if options is not None:
        ctx.options = options

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
    rows = json.loads(raw)
    row = rows[fixture["id"]]
    assert row["plot"] == "plot-synthetic.pdf" and (ctx.out_dir / row["plot"]).is_file()
    assert row["original_version_id"] == "v1" and row["export_version_id"] == "v3"
    assert CAP not in raw and "SECRET-TAG" not in raw
    keep_project = options is not None and options.get("keep_project") == "1"
    assert wire.deleted is (not keep_project)
    assert wire.released
    assert rows["unavailable"]["steps"]["1"] == "UNREADABLE"
    for receipt in rows.values():
        if keep_project:
            assert receipt["kept_project_id"] == PROJECT["project_id"]
        else:
            assert "kept_project_id" not in receipt


@pytest.mark.parametrize("body,status,code", [
    ({"projects": []}, 404, "PROJECT_NOT_FOUND"),
    ({"projects": [{"project_id": "other", "org_id": "o"}]}, 404, "PROJECT_NOT_FOUND"),
    ({"projects": [{"project_id": "p"}]}, 502, "INVALID_PROJECT_RESPONSE"),
    ({}, 502, "INVALID_PROJECT_RESPONSE"),
])
def test_project_read_fails_closed(body, status, code):
    calls = []

    def transport(method, path, **kwargs):
        calls.append((method, path))
        return 200, body

    with pytest.raises(StudioError) as raised:
        StudioClient(transport).project("p")
    assert raised.value.status == status and raised.value.error_code == code
    assert calls == [("GET", "/api/projects")]


@pytest.mark.parametrize("keep_project", [False, True])
def test_run_resumes_only_named_fixture_without_intake_or_project_writes(
        fixture, tmp_path, monkeypatch, keep_project):
    source = Path(fixture["path"]).read_bytes()
    wire = Wire(source)
    options = {"resume_project": "p", "resume_drawing": "d", "resume_version": "v1",
               "resume_fixture": fixture["id"]}
    if keep_project:
        options["keep_project"] = "1"
    untouched = {**fixture, "id": "untouched", "path": str(tmp_path / "does-not-exist")}
    ctx = SimpleNamespace(options=options, manifest={"fixtures": [untouched, fixture]},
                          base="https://example.test", source_sha="b" * 40, out_dir=tmp_path / "evidence")

    @contextmanager
    def signed_in(*args, **kwargs):
        yield "page", {}

    def forbidden(*args, **kwargs):
        pytest.fail("resume attempted intake or project mutation")

    for name in ("upload_drawing", "wait_upload", "adopt_drawing", "create_project", "delete_project"):
        monkeypatch.setattr(StudioClient, name, forbidden)
    monkeypatch.setattr(x01.xbench_session, "signed_in", signed_in)
    monkeypatch.setattr(x01, "_transport", lambda page, **kwargs: wire.transport)
    monkeypatch.setattr(geometry, "render_pdf", lambda doc, path: Path(path).write_bytes(b"%PDF evidence"))
    assert len(x01.run(ctx)) == 7
    rows = json.loads((ctx.out_dir / "fixtures.json").read_text())
    assert list(rows) == [fixture["id"]]
    row = rows[fixture["id"]]
    assert row["resumed"] is True
    assert row["drawing_id"] == "d" and row["original_version_id"] == "v1"
    assert row["failed_stage"] is None and row["export_version_id"] == "v3"
    assert row["source_sha256_before"] == row["source_sha256_after"] == fixture["sha256"]
    assert row["dxf_sha256"] == {"export": fixture["sha256"]}
    assert row["steps"]["2"] == "UNREADABLE"
    assert row["upload_drawing_id"] is None and row["import_provenance"] is None
    assert [(method, path) for method, path, _ in wire.calls[:2]] == [
        ("GET", "/api/projects"), ("POST", "/api/projects/p/drawings/d/checkout")]
    assert [path for _, path, _ in wire.calls if path.endswith("/dxf")] == [
        "/api/projects/p/drawings/d/versions/v3/dxf"]
    writes = [kw["json_body"] for _, path, kw in wire.calls if path == "/api/run"]
    assert [write["tool"] for write in writes] == ["solar-settings", "solar-panels-from-drawing"]
    assert writes[0]["dwg"] == "v1"
    assert all(kw["headers"]["X-Project-Id"] == "p" and kw["headers"]["X-Org-Id"] == "o"
               for _, path, kw in wire.calls if path == "/api/run")
    assert wire.released and not wire.deleted
    assert Path(fixture["path"]).read_bytes() == source


@pytest.mark.parametrize("failed_stage", ["checkout", "run:solar-settings"])
def test_resume_records_first_failure_without_retry(fixture, monkeypatch, failed_stage):
    wire = Wire(Path(fixture["path"]).read_bytes())
    client = StudioClient(wire.transport)
    attempts = []
    method = "checkout" if failed_stage == "checkout" else "run"

    def fail(*args, **kwargs):
        attempts.append(args)
        raise StudioError(503, "SIP_R1_INTAKE_UNAVAILABLE", "Project Solar admission unavailable.")

    monkeypatch.setattr(client, method, fail)
    rows = x01.grade_files(client, project=PROJECT, fixtures=[fixture],
                           resume={"fixture": fixture["id"], "drawing": "d", "version": "v1"})
    row = rows[fixture["id"]]
    assert len(attempts) == 1
    if method == "run":
        assert attempts[0][0] == "solar-settings"
    assert row["failed_stage"] == row["product_error"]["stage"] == failed_stage
    assert row["product_error"]["status"] == 503
    assert row["source_sha256_before"] == row["source_sha256_after"] == fixture["sha256"]
    assert all(row["steps"][str(n)] == "UNREADABLE" for n in range(1, 6))
    assert wire.released is (method == "run")
    assert not any(path.endswith("/dxf") or path == "/api/drawings/upload" for _, path, _ in wire.calls)


@pytest.mark.parametrize("mask", range(1, 15))
def test_partial_resume_options_refuse_before_sign_in(monkeypatch, mask):
    keys = ("resume_project", "resume_drawing", "resume_version", "resume_fixture")
    options = {key: "value" for index, key in enumerate(keys) if mask & (1 << index)}

    def forbidden(*args, **kwargs):
        pytest.fail("partial resume attempted sign-in")

    monkeypatch.setattr(x01.xbench_session, "signed_in", forbidden)
    with pytest.raises(SystemExit) as raised:
        x01.run(SimpleNamespace(options=options))
    assert raised.value.code == 5


@pytest.mark.parametrize("outcome", ["success", "http_failure", "job_failure", "job_success", "error"])
def test_resume_stop_after_first_settings_outcome(fixture, tmp_path, monkeypatch, outcome):
    wire = Wire(Path(fixture["path"]).read_bytes())
    transport = wire.transport

    def scripted(method, path, **kwargs):
        if path == "/api/run":
            wire.calls.append((method, path, deepcopy(kwargs)))
            assert kwargs["json_body"]["tool"] == "solar-settings"
            if outcome == "error":
                raise ConnectionError("offline")
            if outcome == "http_failure":
                return 503, {"error": {"error_code": "UNAVAILABLE", "message": "unavailable"}}
            if outcome.startswith("job_"):
                return 202, {"job_id": "settings"}
            return 200, {"ok": True}
        if path == "/api/jobs/settings" and outcome == "job_failure":
            wire.calls.append((method, path, deepcopy(kwargs)))
            return 200, {"status": "failed", "error": {"error_code": "TOOL_FAILED",
                                                        "message": "settings failed"}}
        return transport(method, path, **kwargs)

    @contextmanager
    def signed_in(*args, **kwargs):
        yield "page", {}

    monkeypatch.setattr(x01.xbench_session, "signed_in", signed_in)
    monkeypatch.setattr(x01, "_transport", lambda page, **kwargs: scripted)
    options = {"resume_project": "p", "resume_drawing": "d", "resume_version": "v1",
               "resume_fixture": fixture["id"], "resume_stop_after": "solar-settings"}
    ctx = SimpleNamespace(options=options, manifest={"fixtures": [fixture]},
                          base="https://example.test", source_sha="b" * 40, out_dir=tmp_path / "evidence")
    assert len(x01.run(ctx)) == 7
    row = json.loads((ctx.out_dir / "fixtures.json").read_text())[fixture["id"]]
    success = outcome in ("success", "job_success")
    assert row["completed_calls"] == ["checkout", "context"] + (["run:solar-settings"] if success else [])
    assert row["stopped_after"] == {
        "tool": "solar-settings", "outcome": "success" if success else "failure",
        "http_status": {"success": 200, "http_failure": 503, "job_failure": 202,
                        "job_success": 202, "error": None}[outcome],
        "job_status": {"job_failure": "failed", "job_success": "complete"}.get(outcome)}
    # The existing finally releases on every exit; all other calls end at this outcome.
    assert wire.released and wire.calls[-1][:2] == ("DELETE", "/api/projects/p/drawings/d/checkout")
    calls = wire.calls[:-1]
    assert calls[-1][1] == ("/api/jobs/settings" if outcome.startswith("job_") else "/api/run")
    assert [kw["json_body"]["tool"] for _, path, kw in calls if path == "/api/run"] == ["solar-settings"]
    assert sum("/context?" in path for _, path, _ in calls) == 1
    assert not any(path.endswith("/dxf") for _, path, _ in calls)
    assert row["export_version_id"] is None
    if success:
        assert row["failed_stage"] is None
        assert all(value == "UNREADABLE" for value in row["steps"].values())
        assert all("not run: stopped after solar-settings per admitted scope" in value
                   for value in row["evidence"].values())
    else:
        assert row["failed_stage"] == "run:solar-settings"
        if outcome != "error":
            assert row["product_error"]["stage"] == "run:solar-settings"


@pytest.mark.parametrize("mask", range(15))
def test_stop_after_without_complete_resume_refuses(monkeypatch, mask):
    keys = ("resume_project", "resume_drawing", "resume_version", "resume_fixture")
    options = {key: "value" for index, key in enumerate(keys) if mask & (1 << index)}
    options["resume_stop_after"] = "solar-settings"

    def forbidden(*args, **kwargs):
        pytest.fail("invalid stop-after attempted sign-in")

    monkeypatch.setattr(x01.xbench_session, "signed_in", forbidden)
    with pytest.raises(SystemExit) as raised:
        x01.run(SimpleNamespace(options=options))
    assert raised.value.code == 5


@pytest.mark.parametrize("fixture_id", ["synthetic", "unknown"])
def test_invalid_resume_options_refuse_before_sign_in(monkeypatch, fixture_id):
    options = {"resume_project": "p", "resume_drawing": "d", "resume_version": "v1",
               "resume_fixture": fixture_id}
    if fixture_id == "synthetic":
        options["resume_version"] = ""

    def forbidden(*args, **kwargs):
        pytest.fail("invalid resume attempted sign-in")

    monkeypatch.setattr(x01.xbench_session, "signed_in", forbidden)
    with pytest.raises(SystemExit) as raised:
        x01.run(SimpleNamespace(options=options, manifest={"fixtures": [{"id": "synthetic"}]}))
    assert raised.value.code == 5


def test_resume_detects_source_mutation(fixture):
    wire = Wire(Path(fixture["path"]).read_bytes())
    original = wire.transport

    def transport(method, path, **kwargs):
        if path.endswith("/versions/v3/dxf"):
            Path(fixture["path"]).write_bytes(b"mutated test fixture")
        return original(method, path, **kwargs)

    rows = x01.grade_files(StudioClient(transport), project=PROJECT, fixtures=[fixture],
                           resume={"fixture": fixture["id"], "drawing": "d", "version": "v1"})
    row = rows[fixture["id"]]
    assert row["source_sha256_before"] != row["source_sha256_after"]
    assert row["steps"]["1"] == "FAIL" and wire.released
