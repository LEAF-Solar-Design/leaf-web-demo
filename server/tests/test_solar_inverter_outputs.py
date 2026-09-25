"""Studio's inverter output engines against the plugin source they port (contract G35).

Covered: the polyline projection, the centroid and the trench grid router (a straight route, an obstacle
skirted, the per-axis cap, the metric grid options in drawing units, R27); LEAFTRENCHAUTO (panel-group
INSERTs routed, none on the layer, every metric route over the cap, the same layout routed in inches); LEAFCABLETOTRAYAUTO (a cable snapped, idempotent, none without a trench); LEAFCABLETOTRAY
(no trench, the one settings save, no cable); HomerunAdjust and LEAFDEVICESPATTERN (their reports and
refusals); AddLBD and LEAFPLACELBD (the offset marker, the closest point on the feeder); InsertSchedules
(the string, combiner and equipment cell grids, the stacked positions, the index order, the standard
layout); the Export All workbook and its G21 file row; the C# number formats. States are synthetic and
authored here.
"""
from __future__ import annotations

import copy
import importlib.util
import io
import math
from pathlib import Path
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


st = _load("solar_inverter_state", ROOT / "server" / "solar_inverter_state.py")
out = _load("solar_inverter_outputs", ROOT / "server" / "solar_inverter_outputs.py")

SQUARE = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]]
HOST = {"UseL2Collectors": True, "UseOptimizers": False,
        "InverterCatalogRecord": {"companyName": "Maker", "modelName": "M250", "seriesName": "S",
                                  "maxDCPower": "375", "maxDCVoltage": "1500", "minDCVoltageFeed": "500",
                                  "mpptVoltageRangeMin": "500", "mpptVoltageRangeMax": "1500",
                                  "numMpptTrackers": "12", "DCInputers": "24", "maxACPower": "250",
                                  "nominalACVoltage": "800", "maxACCurrent": "180.5"},
        "ModuleCatalogRecord": None,
        "StringSizerStandard": {"Conditions": "P99.5 Voc", "max_module_voltage": 51.2558131874,
                                "string_design_voltage": 1500},
        "DrawingTextSize": 0.2, "ScheduleLayoutHeights": {},
        "AddLbdPick": [99.0, 101.0], "CablePicks": {"F1": [40.0, 7.0]}}


def device(x, y, role="combiner"):
    return {"number": None, "role": role, "position": st.coordinate(x, y), "scale": 2.0,
            "rotation": st.angle(0.0), "placement": None, "hardware": None,
            "_detail": {"type_key": "A", "is_l2": role != "combiner", "box_input_count": 20, "colour": 1}}


def cable(points, kind="dc-homerun", source="1000", target=1, circuit="+1/1a", segment="end"):
    row = {"cable_kind": kind, "from": source, "to": target,
           "vertices": [st.coordinate(x, y) for x, y in points],
           "length": {"kind": "length", "value": 1.0, "unit": "ft"},
           "_detail": {"circuit": circuit, "gauge": "NA", "closed": False}}
    if kind == "dc-homerun":
        row["segment"] = segment
    return row


def string_row(handle, circuit, panels):
    return {"string": handle, "device": 1, "input": 1, "label": circuit, "colour": 1,
            "_detail": {"circuit": circuit, "panel_count": str(panels)}}


def make_state(strings=(), cables=(), devices=(), settings=None, groups=(), geometry=None, lbd=()):
    return st.validate_state({
        "format": st.STATE_FORMAT, "source": {"dump_sha256": "0" * 64, "reopened": True},
        "rows": {"device": [copy.deepcopy(d) for d in devices],
                 "string-assignment": [copy.deepcopy(s) for s in strings],
                 "cable": [copy.deepcopy(c) for c in cables], "schedule": [],
                 "lbd": [copy.deepcopy(item) for item in lbd]},
        "setting": dict(settings or {"InstallationDesign": "Roof",
                                     "HomerunRouting": copy.deepcopy(st.HOMERUN_ROUTING_DEFAULT)}),
        "geometry": {"strings": list(geometry or []),
                     "panel_groups": [{"group": g, "position": [0.0, 0.0], "scale": 1.0, "rotation_deg": 0.0}
                                      for g in groups]}})


def shifted(poly, dx, dy=0.0):
    return [[x + dx, y + dy] for x, y in poly]


# --------------------------------------------------------------- geometry --

def test_project_to_polyline_clamps_each_segment():
    pts = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]
    assert out.project_to_polyline(5.0, 3.0, pts) == (5.0, 0.0)
    assert out.project_to_polyline(12.0, 5.0, pts) == (10.0, 5.0)
    assert out.project_to_polyline(-4.0, -4.0, pts) == (0.0, 0.0)


def test_centroid_and_its_degenerate_fallback():
    assert out.centroid([tuple(p) for p in SQUARE]) == pytest.approx((5.0, 5.0))
    assert out.centroid([(0.0, 0.0), (4.0, 0.0), (8.0, 0.0)]) == (4.0, 0.0)


def test_polyline_length_straight_and_bulged():
    assert out.polyline_length([(0.0, 0.0), (3.0, 4.0)]) == 5.0
    # a bulge of 1 is a half circle over the chord
    assert out.polyline_length([(0.0, 0.0), (2.0, 0.0)], [1.0]) == pytest.approx(math.pi)


def test_route_path_straight_on_the_lattice():
    ok, path, message = out.route_path((0.0, 0.0), (5.0, 0.0), [], [])
    assert ok and message == ""
    assert path[0][0] == (0.0, 0.0) and path[-1][1] == (5.0, 0.0) and len(path) == 5


def test_route_path_skirts_an_obstacle():
    box = [(3.0, -2.0), (7.0, -2.0), (7.0, 2.0), (3.0, 2.0)]
    ok, path, _ = out.route_path((0.0, 0.0), (10.0, 0.0), [box], [])
    assert ok and path[-1][1] == (10.0, 0.0)
    for (ax, ay), (bx, by) in path:
        assert not out.point_in_polygon((ax + bx) / 2, (ay + by) / 2, box)


def test_route_path_refuses_past_the_grid_cap():
    ok, path, message = out.route_path((0.0, 0.0), (1000.0, 0.0), [], [])
    assert not ok and path == [] and "exceeds MaxGridCellsPerAxis (200)" in message


def test_routing_options_convert_metres_to_inches():
    o = out.routing_options_for_units("in")
    assert o["grid_step"] == pytest.approx(39.37007874015748, rel=1e-12)
    assert o["grid_padding"] == pytest.approx(196.8503937007874, rel=1e-12)
    rest = {k: v for k, v in o.items() if k not in ("grid_step", "grid_padding")}
    assert rest == {k: v for k, v in out.ROUTING_OPTIONS.items() if k not in ("grid_step", "grid_padding")}
    assert out.ROUTING_OPTIONS["grid_step"] == 1.0 and out.ROUTING_OPTIONS["grid_padding"] == 5.0


def test_routing_options_other_units():
    mm = out.routing_options_for_units("mm")
    assert mm["grid_step"] == pytest.approx(1000.0) and mm["grid_padding"] == pytest.approx(5000.0)
    ft = out.routing_options_for_units("ft")
    assert ft["grid_step"] == pytest.approx(1.0 / 0.3048) and ft["grid_padding"] == pytest.approx(5.0 / 0.3048)


@pytest.mark.parametrize("units", ["m", None, "", "Unitless", "furlong", 0, 6, ["in"]])
def test_routing_options_metres_and_unknown_units_unchanged(units):
    assert out.routing_options_for_units(units) == out.ROUTING_OPTIONS


def test_route_path_in_inches_routes_what_the_unconverted_grid_refused():
    ok, path, message = out.route_path((0.0, 0.0), (1000.0, 0.0), [], [], out.routing_options_for_units("in"))
    assert ok and message == "" and path
    step = out.routing_options_for_units("in")["grid_step"]
    assert path[0][0][0] == pytest.approx(0.0, abs=1e-9) and path[0][0][1] == pytest.approx(0.0, abs=1e-9)
    assert abs(path[-1][1][0] - 1000.0) <= step / 2 and path[-1][1][1] == pytest.approx(0.0, abs=1e-9)


# ------------------------------------------------------------------ trenches --

def test_trench_auto_routes_panel_group_inserts():
    # A metric drawing (R27: metres keep the 1-unit grid; the default is the G35 state's inches).
    state = make_state(groups=("A5", "A6"))
    groups = [{"handle": "A5", "outlines": [SQUARE]}, {"handle": "A6", "outlines": [shifted(SQUARE, 30.0)]}]
    after, lines = out.trench_routing_auto(state, groups, units="m")
    assert lines == ["LEAFTRENCHAUTO: routed 2 panel groups; 0 skipped (existing); 0 failed."]
    assert len(after["_trenches"]) == 2
    rows = out.trench_rows(state, after)
    assert [r["id"]["entity_id"] for r in rows] == ["trench-1", "trench-2"]
    assert all(r["type"] == "trench" for r in rows)
    assert "_trenches" not in state and "_trenches" not in st.publish(after)
    again, lines = out.trench_routing_auto(after, groups, units="m")     # idempotent: both served already
    assert lines == ["LEAFTRENCHAUTO: routed 0 panel groups; 2 skipped (existing); 0 failed."]


def test_trench_auto_without_panel_groups_reports_the_plugins_outcome():
    after, lines = out.trench_routing_auto(make_state(), [])
    assert lines == ["LEAFTRENCHAUTO: no panel groups on layer 'Panel Group'. Nothing to route."]
    assert st.report_message("i6", lines) == "no-panel-groups"


def test_trench_auto_over_the_grid_cap_fails_every_route():
    # Metres: 5000 m over 1 m cells is past the cap (R27 leaves metric drawings unchanged).
    state = make_state(groups=("A5", "A6"))
    groups = [{"handle": "A5", "outlines": [SQUARE]}, {"handle": "A6", "outlines": [shifted(SQUARE, 5000.0)]}]
    after, lines = out.trench_routing_auto(state, groups, units="m")
    assert lines == ["LEAFTRENCHAUTO: routed 0 panel groups; 0 skipped (existing); 2 failed."]
    rows, _ = st.step_rows("i6", state, after, lines)
    assert [(r["type"], r["value"]) for r in rows] == [("report", "no-change")]


@pytest.mark.parametrize("kwargs", [{}, {"units": "in"}])
def test_trench_auto_in_inches_routes_what_the_unconverted_grid_failed(kwargs):
    # The same 5000-unit layout in inches (the default, the G35 state's unit): 39.37 in cells fit the cap.
    state = make_state(groups=("A5", "A6"))
    groups = [{"handle": "A5", "outlines": [SQUARE]}, {"handle": "A6", "outlines": [shifted(SQUARE, 5000.0)]}]
    after, lines = out.trench_routing_auto(state, groups, **kwargs)
    assert lines == ["LEAFTRENCHAUTO: routed 2 panel groups; 0 skipped (existing); 0 failed."]
    assert len(after["_trenches"]) == 2
    assert [r["type"] for r in out.trench_rows(state, after)] == ["trench", "trench"]


def test_cable_to_tray_auto_snaps_the_interior_vertices():
    state = make_state(cables=[cable([(0.0, 0.0), (5.0, 1.0), (10.0, 0.0)])])
    state["_trenches"] = [{"vertices": [[0.0, 0.0], [10.0, 0.0]]}]
    after, lines = out.cable_to_tray_snap_auto(state)
    assert lines == ["LEAFCABLETOTRAYAUTO: snapped 1 cables; 0 skipped; 0 bend-radius warnings."]
    assert [st.point_of(v) for v in after["rows"]["cable"][0]["vertices"]] == [[0.0, 0.0], [5.0, 0.0], [10.0, 0.0]]
    rows, _ = st.step_rows("i7", state, after, lines)
    assert [(r["type"], r["change"]) for r in rows] == [("cable", "changed")]
    again, lines = out.cable_to_tray_snap_auto(after)
    assert lines == ["LEAFCABLETOTRAYAUTO: snapped 0 cables; 1 skipped; 0 bend-radius warnings."]
    assert st.report_message("i7", lines) == "none-snapped"


def test_cable_to_tray_auto_without_a_trench_snaps_none():
    state = make_state(cables=[cable([(0.0, 0.0), (5.0, 1.0), (10.0, 0.0)])],
                       geometry=[{"string": "1000", "vertices": [[0.0, 0.0], [0.0, 9.0]]}])
    after, lines = out.cable_to_tray_snap_auto(state)
    assert lines == ["LEAFCABLETOTRAYAUTO: snapped 0 cables; 2 skipped; 0 bend-radius warnings."]
    rows, _ = st.step_rows("i7", state, after, lines)
    assert [(r["type"], r["value"]) for r in rows] == [("report", "none-snapped")]


def test_cable_to_tray_without_a_trench_reports_and_saves_the_settings_once():
    state = make_state(cables=[cable([(0.0, 0.0), (100.0, 0.0)], kind="feeder", source=1, target=1,
                                     circuit="F1/1")])
    after, lines = out.cable_to_tray_snap(state, HOST, ["F1"])
    assert lines == ["LEAFCABLETOTRAY: No trench within 5.0m of selected cable. Run LEAFTRENCH first."]
    rows, settings = st.step_rows("i20", state, after, lines)
    assert [(r["type"], r.get("value") if r["type"] == "report" else r["name"]) for r in rows] == \
        [("report", "no-trench"), ("setting", "HomerunRouting")]
    assert len(after["setting"]["HomerunRouting"]["CableCatalog"]) == 4


def test_cable_to_tray_without_a_cable_is_cancelled_and_refuses_an_unknown_pick():
    after, lines = out.cable_to_tray_snap(make_state(), HOST, ["F1"])
    assert lines == ["LEAFCABLETOTRAY: cancelled."]
    with pytest.raises(out.InverterOutputError):
        out.cable_to_tray_snap(make_state(), HOST, ["ZZ"])


def test_homerun_adjust_and_pattern_place_report_and_refuse():
    state = make_state()
    _, lines = out.homerun_adjust(state)
    assert st.report_message("i16", lines) == "no-homerun-trunk"
    _, lines = out.devices_pattern_place(state)
    assert st.report_message("i21", lines) == "no-tracker-rows"
    trunk = copy.deepcopy(state)
    trunk["_homerun_trunk"] = [{"vertices": [[0, 0], [1, 1]]}]
    with pytest.raises(out.InverterOutputError):
        out.homerun_adjust(trunk)
    rows = copy.deepcopy(state)
    rows["_tracker_rows"] = [{"row": 1}]
    with pytest.raises(out.InverterOutputError):
        out.devices_pattern_place(rows)


# ---------------------------------------------------------------------- LBD --

def test_add_lbd_offsets_the_marker_toward_the_pick():
    state = make_state(devices=[device(100.0, 100.0), device(500.0, 500.0)])
    after, lines = out.lbd_add(state, HOST)
    marker = after["rows"]["lbd"][0]
    x, y = st.point_of(marker["position"])
    assert math.hypot(x - 100.0, y - 100.0) == pytest.approx(1.5)
    assert x < 100.0 < y and marker["feeder"] == {"ref": "device", "id": None} and marker["lbd_kind"] == "marker"
    rows, _ = st.step_rows("i14", state, after, lines)
    assert [(r["id"]["entity_id"], r["change"]) for r in rows] == [("lbd-1", "added")]
    assert out.lbd_add(make_state(), HOST)[1] == ["AddLBD: cancelled."]


def test_place_lbd_on_the_closest_point_of_the_feeder():
    state = make_state(cables=[cable([(0.0, 0.0), (100.0, 0.0)], kind="feeder", source=1, target=1,
                                     circuit="F1/1")])
    after, lines = out.lbd_place(state, HOST, ["F1"])
    block = after["rows"]["lbd"][0]
    assert st.point_of(block["position"]) == [40.0, 0.0]
    assert block["feeder"] == {"ref": "cable", "id": None} and block["lbd_kind"] == "block"
    assert lines[0] == "LEAFPLACELBD: placed LBD at (40.00, 0.00) on layer LBD."


# ---------------------------------------------------------------- schedules --

def schedule_state():
    strings = [string_row("1000", "+1/1a", 14), string_row("1001", "+2/1a", 13), string_row("1002", "+3/2b", 12)]
    geometry = [{"string": h, "vertices": [[0.0, 0.0], [120.0, 0.0]]} for h in ("1000", "1001", "1002")]
    settings = {"InstallationDesign": "Roof", "HomerunRouting": copy.deepcopy(st.HOMERUN_ROUTING_DEFAULT),
                "L1ToL2Assignments": {"1": 1, "2": 1}}
    return make_state(strings=strings, geometry=geometry, settings=settings,
                      cables=[cable([(0.0, 0.0), (0.0, 240.0)])])


def test_insert_schedules_builds_the_three_tables_stacked_down():
    state = schedule_state()
    after, lines = out.insert_schedules(state, HOST, ["100,200"])
    assert lines == ["Inserted 3 schedule table(s)."]
    tables = {t["cells"][0][0]: t for t in after["rows"]["schedule"]}
    equip, comb, strings = tables["EQUIPMENT SCHEDULE"], tables["COMBINER / INVERTER SCHEDULE"], tables["STRING SCHEDULE"]
    assert st.point_of(equip["position"]) == [100.0, 200.0]
    assert st.point_of(comb["position"]) == pytest.approx([100.0, 191.7])
    assert st.point_of(strings["position"]) == pytest.approx([100.0, 187.9])
    assert (strings["index"], comb["index"], equip["index"]) == (1, 2, 3)
    assert (strings["rows"], strings["cols"]) == (6, 18) and strings["cells"][1][:2] == ["CB", "Inv"]
    assert strings["cells"][2] == ["1", "1", "1", "-", "14", "-", "717.6", "1500", "782.4", "-", "-", "-", "-",
                                   "30", "-", "-", "-", "P99.5 Voc"]
    assert strings["cells"][3][:9] == ["1", "1", "2", "-", "13", "-", "666.3", "1500", "833.7"]
    assert strings["cells"][4][:9] == ["2", "1", "3", "-", "12", "-", "615.1", "1500", "884.9"]
    assert strings["cells"][4][13] == "10"
    assert strings["cells"][5] == ["TOTAL", "", "3", "", "39", "", "", "", "", "", "", "", "-", "", "", "", "", ""]
    assert comb["cells"][2:] == [["INV-1", "CB-1", "2", "14, 13", "27", "-", "-", "-"],
                                 ["INV-1", "CB-2", "1", "12", "12", "-", "-", "-"],
                                 ["INV-1 Total", "", "3", "", "39", "-", "0", "-"],
                                 ["GRAND TOTAL", "", "3", "", "39", "-", "-", "-"]]
    assert (equip["rows"], equip["cols"]) == (15, 2)
    assert equip["cells"][0] == ["EQUIPMENT SCHEDULE", ""] and equip["cells"][7] == ["Max DC Voltage (V)", "1,500"]
    assert equip["cells"][10] == ["Number of MPPTs", "12"] and equip["cells"][14] == ["Max AC Current (A)", "180.5"]
    rows, _ = st.step_rows("i8", state, after, lines)
    assert [(r["id"]["entity_id"], r["index"], r["change"]) for r in rows] == \
        [("schedule-1", 1, "added"), ("schedule-2", 2, "added"), ("schedule-3", 3, "added")]


def test_insert_schedules_standard_layout_and_a_measured_height():
    host = dict(HOST, UseL2Collectors=False, ScheduleLayoutHeights={"INVERTER SCHEDULE": 9.5})
    after, _ = out.insert_schedules(schedule_state(), host, ["0,100"])
    tables = {t["cells"][0][0]: t for t in after["rows"]["schedule"]}
    assert tables["STRING SCHEDULE"]["cells"][1][:2] == ["Inv", "MPPT"]
    assert tables["STRING SCHEDULE"]["cells"][4][:2] == ["2", "B"]
    assert tables["INVERTER SCHEDULE"]["cells"][2] == ["INV-1", "A", "2", "14, 13", "27", "-", "-", "-"]
    assert st.point_of(tables["STRING SCHEDULE"]["position"]) == pytest.approx([0.0, 100.0 - 8.3 - 10.3])


def test_insert_schedules_refuses_a_bad_point():
    with pytest.raises(out.InverterOutputError):
        out.insert_schedules(schedule_state(), HOST, ["here"])


# ------------------------------------------------------------- cable export --

def test_cable_export_writes_the_workbook_and_its_file_row():
    state = schedule_state()
    after, lines, workbook = out.cable_export(state, HOST, {"branch_string_export": "Export All"})
    assert st.publish(after) == st.publish(state)
    names = [name for name, _ in workbook["sheets"]]
    assert names == ["Equipment Schedule", "Combiner Inverter Schedule", "String Schedule"]
    with zipfile.ZipFile(io.BytesIO(workbook["bytes"])) as archive:
        assert "xl/worksheets/sheet3.xml" in archive.namelist()
        assert "P99.5 Voc" in archive.read("xl/worksheets/sheet3.xml").decode("utf-8")
    assert out.write_workbook(workbook["sheets"]) == workbook["bytes"]      # deterministic
    row = out.file_row(workbook["text"])
    assert "".join(row["chunks"]) == workbook["text"] and row["role"] == "string-export-xlsx"
    assert row["lines"] == workbook["text"].count("\n") and "# String Schedule\n" in workbook["text"]
    with pytest.raises(out.InverterOutputError):
        out.cable_export(state, HOST, {"branch_string_export": "Close"})
    assert out.cable_export(make_state(), HOST, {"branch_string_export": "Export All"})[2] is None


def test_g21_chunks_split_after_line_feeds():
    text = "abc\n" * 10
    chunks = out.g21_chunks(text, 8)
    assert "".join(chunks) == text and all(len(c) <= 8 and c.endswith("\n") for c in chunks)
    with pytest.raises(out.InverterOutputError):
        out.g21_chunks("x" * 20, 8)


def test_cs_number_formats():
    assert out.cs_format(1500, "N0") == "1,500" and out.cs_format(180.5, "N1") == "180.5"
    assert out.cs_format(2.5, "N0") == "3" and out.cs_round(0.25, 1) == 0.2
    assert out.cs_text(112.0) == "112" and out.cs_text(163.3) == "163.3"
    assert out.safe_format("abc", "N0") == "abc" and out.safe_format_int("12") == "12"
    assert out.device_number_of("+22/1d") == 1 and out.device_number_of("-") == -1
