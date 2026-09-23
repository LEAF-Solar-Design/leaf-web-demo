"""Studio's ground build-out engines against the plugin, computed.

Three layers, hermetic, none skipping:

  1. The plugin's OWN unit tests, ported case for case where they test these
     engines (read 2026-09-23 at C:/tmp/solar-parity/wt-b25-s17):
       Tests/Tests/LeafBomTests.cs         TrackerBomCalculator.Compute and ToCsv:
                                           argument validation, totals, the pile
                                           ceiling, explicit pile counts, physical
                                           length with overhang, DC capacity
       Tests/Tests/RoadGeometryTests.cs    RoadCommand.ComputeOffsetPolyline
  2. The licensed outputs of the 2026-09-23 terrain capture, COMPUTED from the
     committed terrain intake (docs/parity/evidence/ground/terrain/intake.json)
     through Studio's own state after a13 (G13): LEAFBOM's CSV byte for byte and its
     summary (1434 tracker sections, 69678 module slots, 89811.6 m of tube, 19008
     piles, 27871.2 kWp), LEAFTUBE3D's 1434 tubes, LEAFGRADEMULTI's pad at 0.26 m with
     cut 8938.1 m3 and fill 8972.7 m3, LEAFDRAWROAD's 460.00 and LEAFROAD's 260.0 m
     centerlines with the edges, cross section and label the capture drew.
  3. Hand-computed rules (the tube box, the fillet, the .NET custom format) and
     bounds and malformed-input refusals.
"""
from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
INTAKE = ROOT / "docs" / "parity" / "evidence" / "ground" / "terrain" / "intake.json"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bo = _load("solar_ground_buildout", ROOT / "server" / "solar_ground_buildout.py")

MODULE = {"cross_axis_m": 2.1, "along_axis_m": 1.0, "pmax_w": 0.0}   # LeafBomTests.DefaultModule, no Pmax
# The b1 CSV as the plugin wrote it (UTF-8 with a BOM, CRLF line ends).
LICENSED_BOM_CSV = (
    b"\xef\xbb\xbf"
    b"Category,Description,Unit,Quantity\r\n"
    b"Module,PV Module \xe2\x80\x94 2.100 m \xc3\x97 1.000 m portrait,ea,69678\r\n"
    b"Torque Tube,\"Torque tube / tracker rail (1434 sections, 89811.6 m total)\",m,89811.55\r\n"
    b"Torque Tube,Torque tube section count,ea,1434\r\n"
    b"Pile,\"Ground pile / foundation (at 5.0 m c/c spacing, estimated)\",ea,19008\r\n"
    b"Drive Unit,Single-axis tracker drive unit (one per table section),ea,1434\r\n"
    b"DC Capacity,Total DC nameplate (69678 \xc3\x97 400 Wp),kWp,27871.20\r\n"
)


def row(slots, length_m, overhang_m=0.0):
    return bo._tracker_row((0.0, 0.0), (length_m, 0.0), slots, length_m, overhang_m, 0)


def flat(points):
    """Points as one flat list of numbers (pytest.approx takes no nested sequences)."""
    return [float(v) for p in points for v in p]


# ------------------------------------------------ 1. the plugin's own cases --

def test_compute_argument_validation():
    for rows, module, spacing in ((None, MODULE, 5.0), ([], None, 5.0), ([], MODULE, 0.0), ([], MODULE, -1.0)):
        with pytest.raises(bo.BuildoutInputError):
            bo.compute_bom(rows, module, spacing)
    assert bo.DEFAULT_PILE_SPACING_M == 5.0


def test_compute_empty_rows_is_all_zero_and_the_csv_starts_with_its_header():
    r = bo.compute_bom([], MODULE)
    assert (r["total_rows"], r["total_modules"], r["total_tube_length_m"], r["total_piles"]) == (0, 0, 0.0, 0)
    assert bo.bom_csv_text(r).split("\r\n")[0] == "Category,Description,Unit,Quantity"
    assert [line["category"] for line in r["lines"]] == ["Module", "Pile", "Drive Unit"]


def test_compute_single_row_totals():
    r = bo.compute_bom([row(22, 10.3)], MODULE)
    assert (r["total_rows"], r["total_modules"], r["total_drive_units"]) == (1, 22, 1)
    assert r["total_tube_length_m"] == pytest.approx(10.3, abs=1e-9)


def test_compute_piles_are_the_per_row_ceiling_at_least_one():
    assert bo.compute_bom([row(10, 3.0)], MODULE, 5.0)["total_piles"] == 1
    assert bo.compute_bom([row(10, 8.0)], MODULE, 5.0)["total_piles"] == 2
    assert bo.compute_bom([row(10, 8.0), row(10, 3.0)], MODULE, 5.0)["total_piles"] == 3


def test_compute_row_pile_count_override_uses_the_drawing_count():
    r0 = dict(row(10, 8.0), pile_count_override=4)
    r = bo.compute_bom([r0], MODULE, 5.0)
    assert r["total_piles"] == 4
    assert [l["description"] for l in r["lines"] if l["category"] == "Pile"] == \
        ["Ground pile / foundation (from drawing XData)"]


def test_compute_multiple_rows_sum_slots_tube_and_drives():
    r = bo.compute_bom([row(10, 7.5), row(15, 7.5), row(20, 7.5)], MODULE)
    assert r["total_modules"] == 45 and r["total_tube_length_m"] == pytest.approx(22.5, abs=1e-9)
    assert bo.compute_bom([row(1, 1.0)] * 5, MODULE)["total_drive_units"] == 5


def test_physical_length_includes_the_overhang_at_both_ends():
    r0 = row(10, 10.0, 0.1)
    assert bo.physical_length_m(r0) == pytest.approx(10.2, abs=1e-9)
    assert bo.compute_bom([r0], MODULE)["total_tube_length_m"] == pytest.approx(10.2, abs=1e-9)


def test_zero_pmax_has_no_dc_line_and_a_pmax_has_one():
    assert not [l for l in bo.compute_bom([row(20, 10.0)], MODULE)["lines"] if l["category"] == "DC Capacity"]
    r = bo.compute_bom([row(20, 10.0)], dict(MODULE, pmax_w=550.0))
    assert r["total_dc_capacity_kwp"] == pytest.approx(11.0, abs=1e-9)
    assert bo.bom_csv_text(r).endswith("DC Capacity,Total DC nameplate (20 \u00d7 550 Wp),kWp,11.00\r\n")


def test_offset_polyline_cases():
    def offset(pts, d):
        return flat(bo.compute_offset_polyline(pts, d))
    assert offset([(0, 0), (10, 0)], 2.0) == pytest.approx(flat([(0, 2), (10, 2)]))
    assert offset([(0, 0), (10, 0)], -2.0) == pytest.approx(flat([(0, -2), (10, -2)]))
    assert offset([(0, 0), (0, 10)], 3.0) == pytest.approx(flat([(-3, 0), (-3, 10)]))
    s = 1.0 / math.sqrt(2.0)
    assert offset([(0, 0), (10, 10)], 1.0) == pytest.approx(flat([(-s, s), (10 - s, 10 + s)]), abs=1e-9)
    assert offset([(0, 0), (10, 0), (10, 10)], 1.0) == pytest.approx(flat([(0, 1), (10 - s, s), (9, 10)]))
    assert len(bo.compute_offset_polyline([(0, 0), (0, 0), (10, 0)], 1.0)) == 3
    assert offset([(0, 0), (4, 1), (7, 3)], 0.0) == pytest.approx(flat([(0, 0), (4, 1), (7, 3)]))
    for bad in (None, [(0, 0)]):
        with pytest.raises(bo.BuildoutInputError):
            bo.compute_offset_polyline(bad, 1.0)


# ----------------------------------------- 2. the licensed outputs, computed --

@pytest.fixture(scope="module")
def chain():
    """Studio's own state after a13 from the committed intake (the b-steps' input)."""
    bev = _load("solar_ground_buildout_evidence", ROOT / "scripts" / "solar_ground_buildout_evidence.py")
    intake = json.loads(INTAKE.read_text(encoding="utf-8"))
    return {"bev": bev, "intake": intake, "state": bev.a13_state(intake)}


def test_leafbom_writes_the_licensed_csv_byte_for_byte(chain):
    bev, state = chain["bev"], chain["state"]
    out = bev.bo.bom_command(bev.tracker_entities(state), bev.stored_module(state), meters_per_unit=1.0)
    assert out["succeeded"] and out["rows_found"] == 1434
    assert out["csv_bytes"] == LICENSED_BOM_CSV
    assert out["report"] == {"tracker-sections": 1434, "module-slots": 69678, "tube-length": "89811.6",
                             "estimated-piles": 19008, "drives": 1434, "dc-capacity-kwp": "27871.2"}
    assert "  DC capacity (nameplate): 27871.2 kWp (27.871 MWp)" in out["messages"]


def test_leaftube3d_draws_one_terrain_following_tube_per_row(chain):
    bev, state = chain["bev"], chain["state"]
    settings = bev.layout.load_settings(state["settings"])
    out = bev.bo.torque_tube_command(bev.tracker_entities(state), state["grid"], settings["TorqueTubeHeightM"],
                                     settings["TrackerTorqueTubeRadiusM"], 1.0)
    assert out["has_terrain"] and (out["height_m"], out["radius_m"]) == (1.5, 0.08)
    assert out["rows_found"] == 1434 and len(out["solids"]) == 1434
    assert "  Height: 1.50 m  |  OD: 160 mm" in out["messages"]
    assert all(lo < hi for s in out["solids"] for lo, hi in zip(s["bbox_min"], s["bbox_max"]))


def test_leafgrademulti_grades_the_boundary_pad_as_captured(chain):
    state, intake = chain["state"], chain["intake"]
    out = chain["bev"].bo.grade_multi(state["grid"], [intake["boundary"]], 1.0, mode="Auto")
    (pad,) = out["pads"]
    assert pad["label"]["text"] == "PAD 1\\P0.26 m" and pad["label"]["at"] == [250.0, 150.0]
    assert pad["elevation_m"] == pytest.approx(0.26064200685635874, abs=1e-12)
    assert "  Pad 1: elev = 0.26 m  cut=8938.1 m\u00b3  fill=8972.7 m\u00b3" in out["messages"]
    assert "  Net (cut-fill): -F1 m\u00b3 (import fill required)" in out["messages"]
    assert out["settings"] == {"GradingElevationM": pad["elevation_m"], "GradingMode": 0}


def test_leafdrawroad_on_the_captured_centerline():
    out = bo.draw_road([(20.0, 150.0), (480.0, 150.0)])
    assert out["message"] == "LEAFDRAWROAD: road width=4.00 radius=12.00 offset=1.00 centerline-length=460.00"
    assert [(l["layer"], l["role"]) for l in out["lines"]] == [
        ("0", "centerline"), ("LEAF-PVCASE-ROAD", "edge"), ("LEAF-PVCASE-ROAD", "edge"),
        ("LEAF-PVCASE-ROAD-OFFSET", "offset"), ("LEAF-PVCASE-ROAD-OFFSET", "offset")]
    assert [l["vertices"] for l in out["lines"]] == [
        [(20.0, 150.0), (480.0, 150.0)], [(20.0, 152.0), (480.0, 152.0)], [(20.0, 148.0), (480.0, 148.0)],
        [(20.0, 153.0), (480.0, 153.0)], [(20.0, 147.0), (480.0, 147.0)]]
    assert all(l["bulges"] == [0.0, 0.0] and l["closed"] is False for l in out["lines"])


def test_leafroad_on_the_captured_centerline():
    out = bo.road_design([(250.0, 20.0), (250.0, 280.0)])
    assert [(l["layer"], l["vertices"][0][0]) for l in out["lines"]] == [
        ("LEAF-ROAD", 250.0), ("LEAF-ROAD-EDGE", 247.0), ("LEAF-ROAD-EDGE", 253.0),
        ("LEAF-ROAD-EDGE", 246.0), ("LEAF-ROAD-EDGE", 254.0), ("LEAF-ROAD-XSEC", 246.0)]
    xsec = out["lines"][-1]
    assert xsec["role"] == "cross-section"
    assert flat(xsec["vertices"]) == pytest.approx(flat([(246.0, 134.0), (247.0, 134.4), (250.0, 135.0),
                                                         (253.0, 134.4), (254.0, 134.0)]), abs=1e-9)
    (label,) = out["labels"]
    assert label["text"] == "CROSS-SECTION (1:10 V.E.)\\PRoad: 6.0 m  |  Shoulder: 1.0 m  |  Xslope: 2.0%"
    assert label["at"] == pytest.approx((250.0, 132.5)) and label["height"] == 0.5
    assert "  Centerline length   : 260.0 m" in out["messages"]
    assert "  Carriageway surface area  : 1560 m\u00b2" in out["messages"]
    assert "  Total width (w/ shoulders): 8.0 m" in out["messages"]


# ------------------------------------------------ 3. rules and refusals --

def test_frame_polyline_reads_as_a_row_along_its_long_edge_pairs():
    r = bo.polyline_tracker_row({"vertices": [(490.916, 281.448), (498.834, 281.448), (498.834, 297.084),
                                              (490.916, 297.084)]})
    assert r["axis_start"] == pytest.approx((494.875, 281.448)) and r["axis_end"] == pytest.approx((494.875, 297.084))
    assert r["length_m"] == pytest.approx(15.636, abs=1e-9) and (r["module_slots"], r["row_index"]) == (0, 0)
    assert bo.polyline_tracker_row({"layer": "leaf-trackers", "vertices": [(0, 0), (1, 0), (1, 5), (0, 5)]})
    assert bo.polyline_tracker_row({"layer": "LEAF-COLLISION", "vertices": [(0, 0), (1, 0), (1, 5), (0, 5)]}) is None
    assert bo.polyline_tracker_row({"vertices": [(0, 0), (1, 0), (1, 5)]}) is None


def test_drawn_row_reads_its_table_length_or_its_axis():
    ent = {"axis_start": (496.787801215011, 0.01), "axis_end": (496.787801215011, 299.99), "row_length_m": 299.88,
           "rail_overhang_m": 0.05, "slots": 294, "row_index": 117}
    r = bo.drawn_tracker_row(ent)
    assert (r["module_slots"], r["row_index"], r["length_m"]) == (294, 117, 299.88)
    assert bo.physical_length_m(r) == pytest.approx(299.98, abs=1e-9)
    assert bo.drawn_tracker_row(dict(ent, row_length_m=0.0))["length_m"] == pytest.approx(299.98, abs=1e-9)
    assert bo.drawn_tracker_row(dict(ent, schema_version=0)) is None
    assert bo.drawn_tracker_row({k: v for k, v in ent.items() if k != "axis_end"}) is None


def test_reader_keeps_drawing_order_and_refuses_unknown_kinds():
    ents = [{"kind": "tracker", "axis_start": (0, 0), "axis_end": (0, 10), "slots": 3, "row_index": 1},
            {"kind": "polyline", "vertices": [(0, 0), (1, 0), (1, 5), (0, 5)]}]
    assert [r["module_slots"] for r in bo.read_tracker_rows(ents)] == [3, 0]
    with pytest.raises(bo.BuildoutInputError):
        bo.read_tracker_rows([{"kind": "circle"}])
    with pytest.raises(bo.BuildoutInputError):
        bo.read_tracker_rows([{"kind": "polyline", "vertices": [(0, 0), (1, 0), (1, float("nan")), (0, 5)]}])


def test_bom_command_with_no_rows_writes_no_file():
    out = bo.bom_command([], MODULE)
    assert not out["succeeded"] and out["csv_bytes"] is None and out["rows_found"] == 0


def test_csv_escaping_quotes_commas_and_doubles_quotes():
    assert bo.escape_csv("a,b") == '"a,b"' and bo.escape_csv('say "hi"') == '"say ""hi"""'
    assert bo.escape_csv("plain") == "plain" and bo.escape_csv(None) == ""


def test_tube_parameters_default_when_unset():
    assert bo.tube_parameters(0.0, 0.0) == (1.5, 0.08)
    assert bo.tube_parameters(2.0, 0.1) == (2.0, 0.1)


def test_flat_tube_box_is_the_cylinder_box():
    ents = [{"kind": "tracker", "axis_start": (0.0, 0.0), "axis_end": (10.0, 0.0), "slots": 1, "row_index": 0}]
    out = bo.torque_tube_command(ents)
    (s,) = out["solids"]
    assert not out["has_terrain"]
    assert s["bbox_min"] == pytest.approx((0.0, -0.08, 1.42)) and s["bbox_max"] == pytest.approx((10.0, 0.08, 1.58))


def test_tube_follows_the_terrain_at_its_axis_ends():
    grid = {"rows": 2, "cols": 2, "elevations": [0.0, 1.0, 0.0, 1.0],
            "x_min": 0.0, "x_max": 10.0, "y_min": -5.0, "y_max": 5.0}
    ents = [{"kind": "tracker", "axis_start": (0.0, 0.0), "axis_end": (10.0, 0.0), "slots": 1, "row_index": 0}]
    out = bo.torque_tube_command(ents, grid)
    (s,) = out["solids"]
    assert out["has_terrain"] and s["start"] == pytest.approx((0.0, 0.0, 1.5)) and s["end"] == pytest.approx((10.0, 0.0, 2.5))
    r = 0.08
    assert s["bbox_min"] == pytest.approx((-r * math.sqrt(1 / 101), -r, 1.5 - r * math.sqrt(100 / 101)))
    assert s["bbox_max"] == pytest.approx((10 + r * math.sqrt(1 / 101), r, 2.5 + r * math.sqrt(100 / 101)))


def test_a_zero_length_row_draws_no_tube_and_bad_parameters_refuse():
    ents = [{"kind": "tracker", "axis_start": (5.0, 5.0), "axis_end": (5.0, 5.0), "slots": 1, "row_index": 0}]
    out = bo.torque_tube_command(ents)
    assert out["rows_found"] == 1 and out["solids"] == []
    rows = bo.read_tracker_rows(ents)
    with pytest.raises(bo.BuildoutInputError):
        bo.compute_tube_segments(rows, -1.0, 0.08)
    with pytest.raises(bo.BuildoutInputError):
        bo.compute_tube_segments(rows, 1.5, 0.0)


def test_grade_multi_without_terrain_is_manual_at_zero():
    out = bo.grade_multi(None, [[[0, 0], [10, 0], [10, 10], [0, 10]], [[0, 0], [1, 1]]])
    assert out["mode"] == "Manual" and len(out["pads"]) == 1
    (pad,) = out["pads"]
    assert pad["label"] == {"layer": "LEAF-GRADE", "text": "PAD 1\\P0.00 m", "at": [5.0, 5.0], "height": 0.5}
    assert out["settings"] == {"GradingElevationM": 0.0, "GradingMode": 0}
    assert "  Pad 1: elev = 0.00 m" in out["messages"]


def test_grade_multi_refusals_and_the_empty_selection():
    assert not bo.grade_multi(None, [])["succeeded"]
    with pytest.raises(bo.BuildoutInputError):
        bo.grade_multi(None, [[[0, 0], [1, 0], [1, 1]]], mode="Sideways")
    with pytest.raises(bo.BuildoutBoundsError):
        bo.grade_multi(None, [[[0, 0], [1, 0], [1, 1]]] * (bo.MAX_PADS + 1))


def test_net_cut_fill_prints_the_custom_format_literally():
    assert [bo.net_cut_fill_text(v) for v in (-34.6, 12.0, 0.2, -0.49)] == ["-F1", "+F1", "0", "0"]


def test_fillet_rounds_a_right_angle_and_skips_collinear_and_short_legs():
    verts, bulges = bo.fillet_polyline([(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)], 12.0)
    assert flat(verts) == pytest.approx(flat([(0.0, 0.0), (88.0, 0.0), (100.0, 12.0), (100.0, 100.0)]))
    assert bulges == pytest.approx([0.0, -math.tan(math.pi / 8), 0.0, 0.0])
    assert bo.fillet_polyline([(0.0, 0.0), (50.0, 0.0), (100.0, 0.0)], 12.0) == \
        ([(0.0, 0.0), (50.0, 0.0), (100.0, 0.0)], [0.0, 0.0, 0.0])
    assert bo.fillet_polyline([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)], 12.0)[1] == [0.0, 0.0, 0.0]


def test_road_prompts_refuse_what_the_plugin_refuses():
    with pytest.raises(bo.BuildoutInputError):
        bo.draw_road([(0.0, 0.0), (10.0, 0.0)], width=0.0)
    with pytest.raises(bo.BuildoutInputError):
        bo.road_design([(0.0, 0.0), (10.0, 0.0)], width_du=-6.0)
    with pytest.raises(bo.BuildoutInputError):
        bo.road_design([(0.0, 0.0), (5.0, 0.0), (10.0, 0.0)], drawn=True)
    assert bo.road_design([(0.0, 0.0), (10.0, 0.0)], cross_slope_pct=25.0)["summary"]["cross_slope_pct"] == 10.0
    with pytest.raises(bo.BuildoutBoundsError):
        bo.draw_road([(0.0, float(i)) for i in range(bo.MAX_CENTERLINE_VERTICES + 1)], drawn=False)
