"""Studio's inverter device engines against the plugin source they port (contract G35).

Covered: PointIsInside and the move out of a panel-group outline; the grid fallback's cell centres;
AddAllInverters (the inverter count, both dialogs and their answers, the fallback marker, the L2 role
and scale, nothing placed without unassigned strings, the refusals); on the committed rooftop chain
intake's panel-group outlines with 173 unassigned strings, the eight positions the plugin's fallback
placed; ADDINVERTER (the number, the snapped point, the combiner record, AddLater and the refusals);
INVBALANCE (no L1 block, no swap, nothing written); LEAFADOPTL2INVERTERS (every device adopted, the
catalog hardware, the one settings save, cancel); LEAFSKIDRECONCILE (mismatch and reconciled); and
the inverter catalog lookup. States are synthetic and authored here.
"""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHAIN_INTAKE = ROOT / "docs" / "parity" / "evidence" / "rooftop" / "chain" / "intake.json"


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


st = _load("solar_inverter_state", ROOT / "server" / "solar_inverter_state.py")
dev = _load("solar_inverter_devices", ROOT / "server" / "solar_inverter_devices.py")

HOST = {"UseL2Collectors": True, "UseCombinerBox": False, "UsePatternPlacement": False,
        "StringsPerCentralInverter": 24, "CombinerBoxConnections": 20, "SuggestedInverterCount": None,
        "L1CollectorsPerL2": 4, "L2InverterSelection": "TMEIC NINJA-5.05",
        "CentralInverterSymbolScale": 6.0, "CombinerSymbolScale": 2.0, "OsnapApertureDrawingUnits": 50.0}
ADD_ALL = {"inverters_count_differs_from_stringsizer": "Yes", "low_utilization_on_last_inverter": "Keep current"}
SQUARE = [[0.0, 0.0], [100.0, 0.0], [100.0, 100.0], [0.0, 100.0]]


def device(x, y, role="inverter"):
    return {"number": None, "role": role, "position": st.coordinate(x, y), "scale": 2.0,
            "rotation": st.angle(0.0), "placement": None, "hardware": None,
            "_detail": {"type_key": "A", "is_l2": role != "combiner", "box_input_count": 0, "colour": 1}}


def make_state(n_strings=0, devices=(), groups=("A5",), settings=None, cables=()):
    strings = [f"{0x1000 + i:X}" for i in range(n_strings)]
    return st.validate_state({
        "format": st.STATE_FORMAT, "source": {"dump_sha256": "0" * 64, "reopened": True},
        "rows": {"device": [copy.deepcopy(d) for d in devices],
                 "string-assignment": [{"string": h, "device": 0, "input": 24, "label": "-", "colour": 254,
                                        "_detail": {"circuit": "-"}} for h in strings],
                 "cable": [copy.deepcopy(c) for c in cables], "schedule": [], "lbd": []},
        "setting": dict(settings or {"InstallationDesign": "Roof",
                                     "HomerunRouting": copy.deepcopy(st.HOMERUN_ROUTING_DEFAULT)}),
        "geometry": {"strings": [{"string": h, "vertices": [[10.0 * i, 0.0], [10.0 * i, 5.0]]}
                                 for i, h in enumerate(strings)],
                     "panel_groups": [{"group": g, "position": [0.0, 0.0], "scale": 1.0, "rotation_deg": 0.0}
                                      for g in groups]}})


def positions(state):
    return [st.point_of(d["position"]) for d in state["rows"]["device"]]


# --------------------------------------------------------------- geometry --

def test_point_is_inside_the_plugin_quadrant_test():
    assert dev.point_is_inside(50, 50, SQUARE)
    assert not dev.point_is_inside(150, 50, SQUARE)
    assert not dev.point_is_inside(50, 50, [])
    assert dev.point_is_inside(50, 50, list(reversed(SQUARE)))


def test_a_point_inside_moves_past_the_nearest_edge():
    x, y = dev.move_out_of_outlines(90.0, 40.0, [{"handle": "A5", "outlines": [SQUARE]}])
    assert (x, y) == pytest.approx((150.0, 40.0))
    assert dev.move_out_of_outlines(150.0, 40.0, [{"handle": "A5", "outlines": [SQUARE]}]) == (150.0, 40.0)


def test_grid_points_follow_the_extents_aspect():
    points = dev.grid_placement_points(8, (0.0, 0.0, 400.0, 200.0))
    assert len(points) == 8 and points[0] == (50.0, 50.0) and points[3] == (350.0, 50.0)
    assert points[4] == (50.0, 150.0)
    assert dev.grid_placement_points(0, (0, 0, 1, 1)) == []
    assert dev.grid_placement_points(1, (5.0, 5.0, 5.0, 5.0)) == [(5.5, 5.5)]


# -------------------------------------------------------- AddAllInverters --

def outlines(*polys):
    return [{"handle": "A5", "outlines": [list(map(list, p)) for p in polys]}]


def test_add_all_places_the_l2_fleet_by_the_grid_fallback():
    state = make_state(49)
    after, lines = dev.inverter_add_all(state, outlines([[0, 0], [400, 0], [400, 200], [0, 200]]), HOST, ADD_ALL)
    assert len(after["rows"]["device"]) == 3           # ceil(49 / 24)
    assert all(d["role"] == "inverter" and d["placement"] == dev.FALLBACK_MARKER and d["scale"] == 6.0
               and d["number"] is None and d["hardware"] is None for d in after["rows"]["device"])
    assert "Falling back to deterministic grid placement." in lines[1]
    assert state["rows"]["device"] == []                # the input is never mutated
    assert sorted(d["_number"] for d in after["rows"]["device"]) == [1, 2, 3]


def test_add_all_answers_and_empty_paths():
    square = outlines([[0, 0], [400, 0], [400, 200], [0, 200]])
    after, _ = dev.inverter_add_all(make_state(49), square, HOST, dict(ADD_ALL, inverters_count_differs_from_stringsizer="No"))
    assert after["rows"]["device"] == []
    after, _ = dev.inverter_add_all(make_state(49), square, HOST, dict(ADD_ALL, low_utilization_on_last_inverter="Cancel"))
    assert after["rows"]["device"] == []
    # 48 strings fill two inverters exactly: no low-utilization dialog, so its answer is not read.
    after, _ = dev.inverter_add_all(make_state(48), square, HOST, {"inverters_count_differs_from_stringsizer": "Yes"})
    assert len(after["rows"]["device"]) == 2
    # a matching StringSizer count raises no count dialog either.
    after, _ = dev.inverter_add_all(make_state(48), square, dict(HOST, SuggestedInverterCount=2), {})
    assert len(after["rows"]["device"]) == 2
    after, lines = dev.inverter_add_all(make_state(0), square, HOST, ADD_ALL)
    assert after["rows"]["device"] == [] and "No unassigned strings" in lines[-1]


def test_add_all_refusals():
    square = outlines([[0, 0], [400, 0], [400, 200], [0, 200]])
    with pytest.raises(dev.InverterNotPortedError):
        dev.inverter_add_all(make_state(49, devices=[device(1, 1)]), square, HOST, ADD_ALL)
    with pytest.raises(dev.InverterNotPortedError):
        dev.inverter_add_all(make_state(49), square, HOST, dict(ADD_ALL, low_utilization_on_last_inverter="Apply recommended"))
    with pytest.raises(dev.InverterNotPortedError):
        dev.inverter_add_all(make_state(49, settings={"InstallationDesign": "Ground"}), square,
                             dict(HOST, UsePatternPlacement=None), ADD_ALL)
    with pytest.raises(dev.InverterDeviceError, match="not this drawing"):
        dev.inverter_add_all(make_state(49, groups=("B7",)), square, HOST, ADD_ALL)
    with pytest.raises(dev.InverterDeviceError):
        dev.inverter_add_all(make_state(49), square, dict(HOST, Unknown=1), ADD_ALL)
    with pytest.raises(dev.InverterDeviceError):
        dev.inverter_add_all(make_state(49), square, dict(HOST, StringsPerCentralInverter=0), ADD_ALL)


def test_add_all_on_the_committed_intake_outlines_places_the_plugin_positions():
    intake = json.loads(CHAIN_INTAKE.read_text(encoding="utf-8"))
    groups = [{"handle": g["handle"], "outlines": g["outlines"]} for g in intake["panel_groups"]]
    state = make_state(173, groups=tuple(g["handle"] for g in groups))
    after, _ = dev.inverter_add_all(state, groups, HOST, ADD_ALL)
    # The eight points the captured AddAllInverters fallback placed (grid centres, four of them
    # moved out of a panel-group outline), in row order.
    expected = [[16758.35632672141, 1576.0978668011005], [14298.59861051801, 1644.1001185578311],
                [20260.8519577411, 2025.7374216010467], [18680.172866981633, 2030.8313904763866],
                [18436.98933559366, 3800.2254863345493], [14522.58277007009, 3954.9962530092293],
                [20260.8519577411, 3954.9962530092293], [16613.12670764324, 4121.285469013931]]
    got = positions(after)
    assert len(got) == 8
    for point, want in zip(got, expected):
        assert point == pytest.approx(want, abs=1e-6)


# ------------------------------------------------------------ ADDINVERTER --

FEEDER = {"cable_kind": "feeder", "from": 14, "to": 8, "vertices": [st.coordinate(260.0, 89.0), st.coordinate(260.0, 190.0)],
          "length": {"kind": "length", "value": 1.0, "unit": "ft"}}


def test_add_one_combiner_snaps_and_defers():
    state = make_state(2, devices=[device(150.0, 89.0, "combiner")], cables=[FEEDER])
    after, lines = dev.inverter_add(state, HOST, ["15", "300,89.2,0", "AddLater"], {"select_equipment_type": "Combiner box"})
    added = [d for d in after["rows"]["device"] if d.get("_number") == 15]
    assert len(added) == 1
    (row,) = added
    assert st.point_of(row["position"]) == [260.0, 89.0]       # snapped to the feeder vertex 40 away
    assert (row["role"], row["scale"], row["placement"], row["_detail"]["box_input_count"]) == ("combiner", 2.0, None, 20)
    assert lines[-1].startswith("String assignment deferred.")
    far, _ = dev.inverter_add(state, dict(HOST, OsnapApertureDrawingUnits=10.0), ["15", "300,89.2,0", "AddLater"],
                              {"select_equipment_type": "Combiner box"})
    assert [260.0, 89.0] not in positions(far) and [300.0, 89.2] in positions(far)


def test_add_one_central_inverter_takes_no_strings():
    after, _ = dev.inverter_add(make_state(0), HOST, ["", "5,5", "Manual"], {"select_equipment_type": "Central inverter"})
    (row,) = after["rows"]["device"]
    assert (row["role"], row["scale"], row["_number"], row["_detail"]["box_input_count"]) == ("inverter", 6.0, 1, 0)


@pytest.mark.parametrize("answers, forms", [
    (["0", "1,1", "AddLater"], {"select_equipment_type": "Combiner box"}),
    (["15", "1;1", "AddLater"], {"select_equipment_type": "Combiner box"}),
    (["15", "1,1", "Later"], {"select_equipment_type": "Combiner box"}),
    (["15", "1,1"], {"select_equipment_type": "Combiner box"}),
    (["15", "1,1", "AddLater"], {"select_equipment_type": "Other"}),
])
def test_add_one_refuses_malformed_answers(answers, forms):
    with pytest.raises(dev.InverterDeviceError):
        dev.inverter_add(make_state(0), HOST, answers, forms)


def test_add_one_manual_selection_is_not_ported():
    with pytest.raises(dev.InverterNotPortedError):
        dev.inverter_add(make_state(0), HOST, ["15", "1,1", "Manual"], {"select_equipment_type": "Combiner box"})


# ------------------------------------------------------------- INVBALANCE --

def test_balance_without_l1_blocks_writes_nothing():
    state = make_state(3, devices=[device(1, 1), device(5, 5)])
    forms = {"branch_inverter_manager_tab": "Central Inverters",
             "branch_inverter_manager_action": "Auto-Balance All", "branch_inverter_manager": "Close"}
    after, lines = dev.inverter_balance(state, HOST, forms)
    assert st.publish(after) == st.publish(state) and lines == []
    rows, _ = st.step_rows("i4", state, after, lines)
    assert [(r["type"], r["value"]) for r in rows] == [("report", "no-imbalance")]
    with pytest.raises(dev.InverterNotPortedError):
        dev.inverter_balance(make_state(3, devices=[device(1, 1, "combiner")]), HOST, forms)


# ------------------------------------------------------ LEAFADOPTL2INVERTERS --

ADOPT = {"select_l2_inverter_hardware_mapping": "default", "select_l2_inverter_hardware": "Adopt"}


def test_adopt_makes_every_device_a_fixed_l2_inverter():
    state = make_state(0, devices=[device(9, 1, "combiner"), device(1, 1), device(5, 0, "combiner")])
    after, _ = dev.adopt_l2_inverters(state, HOST, ADOPT)
    assert all(d["role"] == "l2-inverter" and d["placement"] == dev.FIXED_L2 and
               d["hardware"] == {"model": "TMEIC NINJA-5.05", "ac_kw": 5050.0, "match_score": 100}
               for d in after["rows"]["device"])
    # numbered in X order: (1,1) -> 1, (5,0) -> 2, (9,1) -> 3.
    assert {tuple(st.point_of(d["position"])): d["_number"] for d in after["rows"]["device"]} == \
        {(1.0, 1.0): 1, (5.0, 0.0): 2, (9.0, 1.0): 3}
    assert after["setting"]["HomerunRouting"]["CableCatalog"] == st.DEFAULT_CATALOG * 2
    rows, _ = st.step_rows("i19", state, after, [])
    assert [r["type"] for r in rows] == ["device"] * 3 + ["setting"]
    assert all(r["change"] == "changed" for r in rows[:3])


def test_adopt_cancel_and_unknown_hardware_write_nothing():
    state = make_state(0, devices=[device(1, 1)])
    for forms, host in ((dict(ADOPT, select_l2_inverter_hardware="Cancel"), HOST),
                        (ADOPT, dict(HOST, L2InverterSelection=None)),
                        (ADOPT, dict(HOST, L2InverterSelection="Unknown 9000"))):
        after, lines = dev.adopt_l2_inverters(state, host, forms)
        assert st.publish(after) == st.publish(state) and "cancelled" in lines[0]


def test_catalog_lookup_by_display_name_model_and_alias():
    assert dev.catalog_find("TMEIC NINJA-5.05") == ("TMEIC NINJA-5.05", 5050.0)
    assert dev.catalog_find("ninja 4.20") == ("TMEIC NINJA-4.20", 4200.0)
    assert dev.catalog_find("TMEIC-840") == ("TMEIC NINJA-840", 840.0)
    assert dev.catalog_find("") is None and dev.catalog_find("SMA 100") is None


# -------------------------------------------------------- LEAFSKIDRECONCILE --

def test_skid_reconcile_prints_the_verdict_and_changes_nothing():
    assignments = {str(i): (i + 1) // 2 for i in range(1, 15)}   # 14 L1 on 7 L2
    state = make_state(0, settings={"InstallationDesign": "Roof", "L1ToL2Assignments": assignments,
                                    "HomerunRouting": copy.deepcopy(st.HOMERUN_ROUTING_DEFAULT)})
    after, lines = dev.skid_reconcile(state, HOST)
    assert st.publish(after) == st.publish(state)
    assert lines[:2] == ["LEAFSKIDRECONCILE: MISMATCH.", "  L1 collectors: 14, L2 input slots: 28 (7 L2 x 4 slots)."]
    assert lines[2] == "  Slot mismatch: 14 string(s) vs 28 combiner slot(s)."
    rows, _ = st.step_rows("i10", state, after, lines)
    assert [(r["type"], r["value"]) for r in rows] == [("report", "mismatch")]
    exact = dict(state["setting"], L1ToL2Assignments={"1": 1, "2": 1})
    _, lines = dev.skid_reconcile(dict(state, setting=exact), dict(HOST, L1CollectorsPerL2=2))
    assert lines[0] == "LEAFSKIDRECONCILE: RECONCILED."
    _, lines = dev.skid_reconcile(dict(state, setting=dict(exact, L1ToL2Assignments={})), HOST)
    assert "no L1->L2 combiner assignments found" in lines[0]
    with pytest.raises(dev.InverterDeviceError):
        dev.skid_reconcile(dict(state, setting=dict(exact, L1ToL2Assignments={"x": 1})), HOST)


def test_reconcile_counts_unassigned_and_oversized():
    result = dev.reconcile([1, 2, 3, 4], {1: 5, 2: 5, 3: 5, 4: 9}, {5: 2})
    assert not result["reconciled"] and result["unassigned"] == [4] and result["oversized"] == [(5, 3, 2)]
    assert result["messages"][-1] == "Combiner 5 oversized: 3 string(s) assigned, capacity 2 (over by 1)."


# ------------------------------------------------ add all, string inverters --

LEGACY = dict(HOST, UseL2Collectors=False, SessionColorCounter=1)
LEGACY_SETTINGS = {"InstallationDesign": "Roof", "NumMppt": 3, "StringPerMppt": 3,
                   "HomerunRouting": copy.deepcopy(st.HOMERUN_ROUTING_DEFAULT)}


def test_add_all_without_l2_places_string_inverters_and_assigns_their_strings():
    state = make_state(30, settings=LEGACY_SETTINGS)
    square = outlines([[0, 0], [400, 0], [400, 200], [0, 200]])
    after, lines = dev.inverter_add_all(state, square, LEGACY, ADD_ALL)
    devices = after["rows"]["device"]
    assert len(devices) == 2 and all(d["role"] == "combiner" and d["scale"] == 2.0
                                     and d["placement"] == dev.FALLBACK_MARKER for d in devices)
    assert [d["_detail"]["colour"] for d in sorted(devices, key=lambda d: d["_number"])] == [2, 5]
    rows = after["rows"]["string-assignment"]
    loads = {}
    for row in rows:
        loads[row["device"]] = loads.get(row["device"], 0) + 1
    assert loads == {1: 21, 2: 9}                             # the nearest point (x 100 or 300), under capacity
    labels = sorted(int(row["label"][1:].split("/")[0]) for row in rows)
    assert labels == list(range(1, 31))                       # one sequence across the inverters
    first = min(rows, key=lambda row: int(row["label"][1:].split("/")[0]))
    assert first["label"].endswith("a") and first["input"] == 1
    assert "Falling back to deterministic grid placement." in lines[0]


def test_add_all_without_l2_refuses_existing_string_inverters():
    with pytest.raises(dev.InverterNotPortedError):
        dev.inverter_add_all(make_state(10, devices=[device(1, 1, role="combiner")], settings=LEGACY_SETTINGS),
                             outlines(SQUARE), LEGACY, ADD_ALL)


def test_midpoint_and_nearest_capacity_rules():
    assert dev.polyline_midpoint([[0, 0], [10, 0], [10, 10]]) == (10.0, 0.0)
    assert dev.polyline_midpoint([[3, 4]]) == (3.0, 4.0)
    points = [(0.0, 0.0), (100.0, 0.0)]
    assert dev.nearest_placement_point(points, [0, 0], (10.0, 0.0), 2) == 0
    assert dev.nearest_placement_point(points, [2, 0], (10.0, 0.0), 2) == 1   # the nearest is full
    assert dev.nearest_placement_point(points, [2, 2], (10.0, 0.0), 2) == 0   # all full: the nearest
