"""Studio's inverter output engines against the plugin source they port (contract G35).

Covered: the polyline projection, the centroid and the trench grid router (a straight route, an obstacle
skirted, the per-axis cap); LEAFTRENCHAUTO (panel-group INSERTs routed, none on the layer, every route
over the cap); LEAFCABLETOTRAYAUTO (a cable snapped, idempotent, none without a trench); LEAFCABLETOTRAY
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
import random
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


# ------------------------------------------------------------------ trenches --

def test_trench_auto_routes_panel_group_inserts():
    state = make_state(groups=("A5", "A6"))
    groups = [{"handle": "A5", "outlines": [SQUARE]}, {"handle": "A6", "outlines": [shifted(SQUARE, 30.0)]}]
    after, lines = out.trench_routing_auto(state, groups)
    assert lines == ["LEAFTRENCHAUTO: routed 2 panel groups; 0 skipped (existing); 0 failed."]
    assert len(after["_trenches"]) == 2
    rows = out.trench_rows(state, after)
    assert [r["id"]["entity_id"] for r in rows] == ["trench-1", "trench-2"]
    assert all(r["type"] == "trench" for r in rows)
    assert "_trenches" not in state and "_trenches" not in st.publish(after)
    again, lines = out.trench_routing_auto(after, groups)     # idempotent: both served already
    assert lines == ["LEAFTRENCHAUTO: routed 0 panel groups; 2 skipped (existing); 0 failed."]


def test_trench_auto_without_panel_groups_reports_the_plugins_outcome():
    after, lines = out.trench_routing_auto(make_state(), [])
    assert lines == ["LEAFTRENCHAUTO: no panel groups on layer 'Panel Group'. Nothing to route."]
    assert st.report_message("i6", lines) == "no-panel-groups"


def test_trench_auto_over_the_grid_cap_fails_every_route():
    state = make_state(groups=("A5", "A6"))
    groups = [{"handle": "A5", "outlines": [SQUARE]}, {"handle": "A6", "outlines": [shifted(SQUARE, 5000.0)]}]
    after, lines = out.trench_routing_auto(state, groups)
    assert lines == ["LEAFTRENCHAUTO: routed 0 panel groups; 0 skipped (existing); 2 failed."]
    rows, _ = st.step_rows("i6", state, after, lines)
    assert [(r["type"], r["value"]) for r in rows] == [("report", "no-change")]


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
    # R03b: the export form's own sheets (StringHomerunExportForm.cs:349-413), not the InsertSchedules tables;
    # L2 collectors on and L1ToL2Assignments held, so a Feeder Schedule (no session feeders: header only).
    state = schedule_state()
    after, lines, workbook = out.cable_export(state, HOST, {"branch_string_export": "Export All"})
    assert st.publish(after) == st.publish(state)
    names = [name for name, _ in workbook["sheets"]]
    assert names == ["Homeruns", "Equipment Schedule", "Inverter Schedule", "String Schedule", "Feeder Schedule"]
    assert lines == ["CableExport: wrote 5 sheet(s)."]
    text = workbook["text"].split("\n")
    assert text[:5] == ["# Homeruns", "\t".join(out.HOMERUN_HEADERS),       # SelectAll: the homerun first
                        "1 - a\t1\tEnd Homerun\tNA\t20.00\t30.00\t15.00",
                        "1 - a\t1\tString\t14\t10.00\t30.00\t15.00",
                        "1 - a\t2\tString\t13\t10.00\t10.00\t5.00"]
    assert "INV-1\tA\t2\t2/2\t14, 13\t27\t-\t-\t1500\t-\t" in text
    assert "INV-2\tB\t1\t1/2\t12\t12\t-\t-\t1500\t-\t" in text
    assert "2 - b\t3\tString\t12\t10.00\t10.00\t5.00" in text
    assert "INV-1..1" not in workbook["text"] and "INV-1\tString Inverter\tMaker\tM250\t1\t" \
        "250.0kW AC, 800V, 180.5A\tUL 1741\t690.4" in text
    assert "S1-A1\t1\tA\t-\t14\t-\t717.6\t1500\t782.4\t-\t-\t-\t-\t30\t-\t-\t-\tP99.5 Voc" in text
    assert "TOTAL\t\t3 strings\t\t39" in text
    assert text[-3:] == ["# Feeder Schedule", "Combiner Box #\tInverter #\tFeeder Length (ft)", ""]
    with zipfile.ZipFile(io.BytesIO(workbook["bytes"])) as archive:
        assert "xl/worksheets/sheet5.xml" in archive.namelist()
        assert "P99.5 Voc" in archive.read("xl/worksheets/sheet4.xml").decode("utf-8")
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
    assert out.cs_fixed(6.1, 2) == "6.10" and out.cs_fixed(0.125, 2) == "0.13" and out.cs_fixed(2.675, 2) == "2.67"
    with pytest.raises(out.InverterOutputError):
        out.cs_fixed(float("nan"), 2)


# ------------------------------------------------ .NET 8 List<T>.Sort port --

def by_key(a, b):
    return (a[0] > b[0]) - (a[0] < b[0])


def test_dotnet_sort_three_elements_is_unstable_like_net():
    # size 3: SwapIfGreater(0,1), (0,2), (1,2); the tied pair ends reversed.
    assert out.dotnet_list_sort([(1, "a"), (1, "b"), (0, "c")], by_key) == [(0, "c"), (1, "b"), (1, "a")]
    assert out.dotnet_list_sort([(2, "a"), (1, "b")], by_key) == [(1, "b"), (2, "a")]
    assert out.dotnet_list_sort([], by_key) == [] and out.dotnet_list_sort([(5, "x")], by_key) == [(5, "x")]


def test_dotnet_sort_partitions_seventeen_ties_like_net():
    # One PickPivotAndPartition over 17 equal keys (pivot index 8 parked at 15, the Hoare scan swapping
    # 1..7 with 14..8), then two insertion-sorted halves that keep their order.
    keys = [(0, n) for n in range(17)]
    assert [n for _, n in out.dotnet_list_sort(keys, by_key)] == \
        [0, 14, 13, 12, 11, 10, 9, 15, 8, 6, 5, 4, 3, 2, 1, 7, 16]


def test_dotnet_sort_heap_and_insertion_paths():
    keys = [(0, n) for n in range(4)]
    out._heap_sort(keys, 0, 4, by_key)
    assert [n for _, n in keys] == [1, 2, 3, 0]
    keys = [(0, n) for n in range(10)]
    out._insertion_sort(keys, 0, 10, by_key)                  # stable on ties
    assert [n for _, n in keys] == list(range(10))


def test_dotnet_sort_orders_every_input():
    rng = random.Random(2026)
    for size in list(range(0, 40)) + [100, 533, 1000]:
        for spread in (1, 3, 1000):
            keys = [(rng.randrange(spread), n) for n in range(size)]
            result = out.dotnet_list_sort(list(keys), by_key)
            assert [k for k, _ in result] == sorted(k for k, _ in keys) and sorted(result) == sorted(keys)
            heap = list(keys)
            out._intro_sort(heap, 0, len(heap), 0, by_key)    # depth limit 0: HeapSort
            assert [k for k, _ in heap] == sorted(k for k, _ in keys) and sorted(heap) == sorted(keys)


# ------------------------------------------------ i9 against the plugin workbook --
# R03b: the committed i8 state (the i9 step's input) on the i9 host of the evidence producer, against the
# workbook test build 2 of the fixed plugin saved (receipt w7-testbuild2-20260925, i9-homeruns.xlsx), rendered
# by workbook_text's rule. Declared: the inverter rating row (inverter-rating-units.md). Every other row,
# the Homeruns feeders included (their SelectAll order from RouteL2Feeders' drawing order), in place.
FEEDER_ROWS = 14

I8_STATE = ROOT / "docs" / "parity" / "evidence" / "rooftop" / "inverters" / "state-i8.json"
PLUGIN_WORKBOOK_TEXT = ROOT / "docs" / "parity" / "evidence" / "rooftop" / "inverters" / "i9-plugin-workbook-tb2.txt"
PLUGIN_RATING_ROW = "INV-1..5\tString Inverter\tSungrow\tSG250HX\t5\t0.2kW AC, 800V, 180.5A\tUL 1741\t690.4"
STUDIO_RATING_ROW = "INV-1..5\tString Inverter\tSungrow\tSG250HX\t5\t250.0kW AC, 800V, 180.5A\tUL 1741\t690.4"
DECLARED_ROWS = {PLUGIN_RATING_ROW: STUDIO_RATING_ROW}
VOC_COLD = "Voc \u00d7 (1 + \u03b2voc/100 \u00d7 (Tmin \u2212 25\u00b0C)), Tmin = -40\u00b0C"
EXPECTED_EQUIPMENT = [
    "EQUIPMENT SCHEDULE", "", "Tag\tDescription\tManufacturer\tModel\tQty\tRating\tListing\tNEC Ref",
    STUDIO_RATING_ROW,
    "DC-DISC\tDC Disconnect\t(by installer)\t-\t5\t1500V, 30A\tUL 98\t690.13",
    "AC-DISC\tAC Disconnect\t(by installer)\t-\t5\t800V, 226A\tUL 98\t690.54", "",
    "DESIGN PARAMETERS", "Design Min Temp\t-40\u00b0C per NEC 690.7(A)(3)", "", "NEC REFERENCES",
    "690.4 - Installation requirements for PV equipment",
    "690.7(A)(3) - Maximum system voltage: Voc corrected for lowest expected ambient temperature",
    "690.8(A) - Maximum circuit current: Isc \u00d7 1.25 for continuous duty",
    "690.13 - DC photovoltaic disconnecting means",
    "690.54 - Interactive system point of interconnection"]


@pytest.fixture(scope="module")
def i9_text():
    ev = _load("solar_inverter_outputs_evidence", ROOT / "scripts" / "solar_inverter_outputs_evidence.py")
    state = st.load_state(I8_STATE)
    _, _, workbook = out.cable_export(state, ev.host_for("i9"), {"branch_string_export": "Export All"})
    return workbook["text"]


def sections(text):
    """[(sheet name, its lines)] of a workbook_text rendering."""
    out_sections = []
    for line in text.splitlines():
        if line.startswith("# "):
            out_sections.append((line[2:], []))
        else:
            out_sections[-1][1].append(line)
    return out_sections


def test_i9_workbook_sheets_and_the_rows_the_plugin_wrote(i9_text):
    sheets = dict(sections(i9_text))
    assert [name for name, _ in sections(i9_text)] == \
        ["Homeruns", "Equipment Schedule", "Inverter Schedule", "String Schedule"]
    assert sheets["Equipment Schedule"] == EXPECTED_EQUIPMENT
    homeruns = sheets["Homeruns"]
    assert len(homeruns) == 534 and homeruns[0] == "\t".join(out.HOMERUN_HEADERS)
    # The unstable sort's order inside a circuit (List.Sort over the SelectAll order).
    assert homeruns[1:13] == ["1 - a\t1\tEnd Homerun\tNA\t29.69\t163.26\t81.63",
                              "1 - a\t1\tString\t14\t82.05\t163.26\t81.63",
                              "1 - a\t1\tStart Homerun\tNA\t51.53\t163.26\t81.63",
                              "1 - a\t2\tString\t14\t82.07\t199.62\t99.81",
                              "1 - a\t2\tStart Homerun\tNA\t69.05\t199.62\t99.81",
                              "1 - a\t2\tEnd Homerun\tNA\t48.51\t199.62\t99.81",
                              "1 - a\t3\tString\t14\t80.45\t169.62\t84.81",
                              "1 - a\t3\tStart Homerun\tNA\t40.53\t169.62\t84.81",
                              "1 - a\t3\tEnd Homerun\tNA\t48.65\t169.62\t84.81",
                              "1 - a\t4\tEnd Homerun\tNA\t73.15\t226.45\t113.22",
                              "1 - a\t4\tStart Homerun\tNA\t71.23\t226.45\t113.22",
                              "1 - a\t4\tString\t14\t82.07\t226.45\t113.22"]
    feeders = [line.split("\t") for line in homeruns if line.startswith("-\t-\tFeeder\t")]
    # The plugin's order: the feeders enter the sort newest drawn first (F6/3 ... F8/5, handles AB46 down
    # to AB39 in raw/i9.txt), and the introsort leaves them in this order.
    assert [float(cells[4]) for cells in feeders] == \
        [165.42, 211.06, 410.57, 243.43, 203.66, 272.37, 201.43, 163.30, 36.42, 164.29, 248.35, 66.27,
         172.94, 88.37]
    assert all(cells[3:] == ["NA", cells[4], "N/A", "N/A"] for cells in feeders) and homeruns[-14:] == \
        ["\t".join(cells) for cells in feeders]
    inverters = sheets["Inverter Schedule"]
    assert len(inverters) == 39 and inverters[:8] == [
        "Inverter\tMPPT\tStrings\tInputs\tMod/String\tModules\tDC (kW)\tVoc_cold (V)\tMax Vdc\tIsc\u00d71.25 (A)\tStatus",
        "INV-1\tA\t6\t6/2\t14\t84\t-\t-\t1500\t-\t",
        "INV-1\tB\t6\t6/2\t13, 14, 13, 13, 13, 14\t80\t-\t-\t1500\t-\t",
        "INV-1\tC\t6\t6/2\t14, 14, 14, 14, 14, 13\t83\t-\t-\t1500\t-\t",
        "INV-1\tD\t6\t6/2\t14, 13, 13, 14, 14, 14\t82\t-\t-\t1500\t-\t",
        "INV-1\tE\t6\t6/2\t14\t84\t-\t-\t1500\t-\t",
        "INV-1\tF\t6\t6/2\t13, 13, 14, 14, 14, 13\t81\t-\t-\t1500\t-\t",
        "INV-1 Total\t\t36\t\t\t494\t-\t\t\t\tDC/AC: -"]
    assert inverters[-6:] == ["INV-7\tE\t5\t5/2\t13, 12, 13, 13, 13\t64\t-\t-\t1500\t-\t",
                              "INV-7 Total\t\t29\t\t\t386\t-\t\t\t\tDC/AC: -", "",
                              "GRAND TOTAL\t\t173\t\t\t2345\t-\t\t\t\tDC/AC: -", "",
                              "Voc_cold calculated per NEC 690.7(A)(3): " + VOC_COLD]
    strings = sheets["String Schedule"]
    assert len(strings) == 179 and strings[0].split("\t") == list(out.EXPORT_STRING_HEADERS)
    assert strings[1] == "S1-A1\t1\tA\t-\t14\t-\t717.6\t1500\t782.4\t-\t-\t-\t-\t163.3\t-\t-\t-\tP99.5 Voc"
    assert strings[7] == "S1-B7\t1\tB\t-\t13\t-\t666.3\t1500\t833.7\t-\t-\t-\t-\t112\t-\t-\t-\tP99.5 Voc"
    assert strings[173] == "S7-E173\t7\tE\t-\t13\t-\t666.3\t1500\t833.7\t-\t-\t-\t-\t334\t-\t-\t-\tP99.5 Voc"
    assert strings[174:] == [
        "", "TOTAL\t\t173 strings\t\t2345", "",
        "Voc_cold per NEC 690.7(A)(3): " + VOC_COLD + " | Isc\u00d71.25 per NEC 690.8(A)(1) continuous duty",
        "Cable sizing per NEC 310.16 (ampacity) + NEC 210.19 FPN (voltage drop \u2264 2% recommended)"]


def test_i9_workbook_equals_the_plugin_workbook_but_the_declared_rows(i9_text):
    plugin = sections(PLUGIN_WORKBOOK_TEXT.read_text(encoding="utf-8-sig"))
    studio = sections(i9_text)
    assert [name for name, _ in studio] == [name for name, _ in plugin]
    for (name, studio_lines), (_, plugin_lines) in zip(studio, plugin):
        if name == "Homeruns":
            assert all(line.startswith("-\t-\tFeeder\t") for line in plugin_lines[-FEEDER_ROWS:])
        assert studio_lines == [DECLARED_ROWS.get(line, line) for line in plugin_lines], name
    assert sum(line in DECLARED_ROWS for _, lines in plugin for line in lines) == 1
