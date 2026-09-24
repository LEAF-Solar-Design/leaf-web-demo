"""Studio's inverter cabling engines against the plugin source they port (contract G35).

Covered: device numbering from the feeder circuits; the vertical land lanes; the phantom-L2 guard; the
balanced nearest assignment with the no-pass rule and the swap pass; RouteL2Feeders (every feeder
adopted: nothing drawn, the one save, the report; a missing feeder drawn as a comb path); HomerunsAuto
(each string's legs redrawn identical, so the step reports no change; the nearest-combiner fallback);
LEAFLITEFEEDERS (the tail-biased assignment, lane-less comb paths, stored length 0, the refusals);
MOVEINV (the device moved, its homerun legs rerouted, one save, declared); the bounded POSITIONINV
search (outside the outline band, never worse, the time bound failing closed); and LEAFCOMBINERAUTO's
named refusal. States are synthetic and authored here.
"""
from __future__ import annotations

import copy
import importlib.util
import math
from pathlib import Path
import sys

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
cab = _load("solar_inverter_cabling", ROOT / "server" / "solar_inverter_cabling.py")

HOST = {"UseL2Collectors": True, "L1CollectorsPerL2": 4, "RackExtents": [],
        "MovedDevice": ["L1", 1], "PositionDevice": ["L1", 1]}
# Two panel-group outlines: X spans [0, 400] and [600, 1000] -> lanes -100, 500, 1100.
GROUPS = [{"handle": "A5", "outlines": [[[0.0, 300.0], [400.0, 300.0], [400.0, 500.0], [0.0, 500.0]]]},
          {"handle": "A6", "outlines": [[[600.0, 300.0], [1000.0, 300.0], [1000.0, 500.0], [600.0, 500.0]]]}]
L2_1, L2_2, CB_1, CB_2 = (0.0, 0.0), (1000.0, 0.0), (100.0, 200.0), (900.0, 200.0)
STRINGS = {"A01": ((50.0, 400.0), (150.0, 400.0), "+1/1a", CB_1),
           "A02": ((850.0, 400.0), (950.0, 400.0), "+2/2a", CB_2)}


def device(xy, role, number=None):
    row = {"number": None, "role": role, "position": st.coordinate(*xy), "scale": 2.0,
           "rotation": st.angle(0.0), "placement": None, "hardware": None,
           "_detail": {"type_key": "A", "is_l2": role != "combiner", "box_input_count": 0, "colour": 1}}
    if number is not None:
        row["_number"] = number
    return row


def feeder(l1, l2, points, length=0.0):
    return {"cable_kind": "feeder", "from": l1, "to": l2, "vertices": [st.coordinate(*p) for p in points],
            "length": {"kind": "length", "value": length, "unit": "ft"},
            "_detail": {"circuit": f"F{l1}/{l2}", "gauge": "NA", "closed": False}}


def homerun(string, segment, leg, target, circuit):
    return {"cable_kind": "dc-homerun", "segment": segment, "from": string, "to": int(circuit.split("/")[1][:-1]),
            "vertices": [st.coordinate(*leg), st.coordinate(*target)],
            "length": {"kind": "length", "value": math.dist(leg, target) / 12.0, "unit": "ft"},
            "_detail": {"circuit": circuit, "gauge": "14 AWG", "closed": False}}


def make_state(feeders=True, homeruns=True, numbered=False, settings=None):
    devices = [device(L2_1, "inverter", 1 if numbered else None), device(L2_2, "inverter", 2 if numbered else None),
               device(CB_1, "combiner", 1 if numbered else None), device(CB_2, "combiner", 2 if numbered else None)]
    cables = []
    if feeders:
        cables += [feeder(1, 1, [CB_1, (-100.0, 200.0), (-100.0, 0.0), L2_1], 30.0),
                   feeder(2, 2, [CB_2, (1100.0, 200.0), (1100.0, 0.0), L2_2], 30.0)]
    if homeruns:
        for handle, (start, end, circuit, target) in STRINGS.items():
            cables += [homerun(handle, "start", start, target, circuit), homerun(handle, "end", end, target, circuit)]
    base = {"InstallationDesign": "Roof", "HomerunRouting": copy.deepcopy(st.HOMERUN_ROUTING_DEFAULT),
            "L1ToL2Assignments": {"1": 1, "2": 2}}
    return st.validate_state({
        "format": st.STATE_FORMAT, "source": {"dump_sha256": "0" * 64, "reopened": True},
        "rows": {"device": devices,
                 "string-assignment": [{"string": h, "device": int(c.split("/")[1][:-1]), "input": 1, "label": c,
                                        "colour": 1, "_detail": {"circuit": c}}
                                       for h, (_, _, c, _) in STRINGS.items()],
                 "cable": cables, "schedule": [], "lbd": []},
        "setting": dict(settings or base),
        "geometry": {"strings": [{"string": h, "vertices": [list(s), list(e)], "midpoint": None,
                                  "start": list(s), "end": list(e), "panels": []}
                                 for h, (s, e, _, _) in STRINGS.items()],
                     "panel_groups": [{"group": g["handle"], "position": [0.0, 0.0], "scale": 1.0,
                                       "rotation_deg": 0.0} for g in GROUPS]}})


def cables_of(state, kind):
    return [c for c in state["rows"]["cable"] if c["cable_kind"] == kind]


def points(row):
    return [tuple(st.point_of(v)) for v in row["vertices"]]


# ------------------------------------------------------------------ numbering and lanes --

def test_levels_number_devices_from_the_feeder_circuits():
    l1, l2 = cab.levels(make_state())
    assert [(d["number"], d["position"]) for d in l1] == [(1, CB_1), (2, CB_2)]
    assert [(d["number"], d["position"]) for d in l2] == [(1, L2_1), (2, L2_2)]


def test_an_unnumbered_device_is_refused_where_numbers_are_needed():
    with pytest.raises(cab.InverterCablingNotPortedError):
        cab.route_l2_feeders(make_state(feeders=False), GROUPS, HOST)


def test_vertical_lanes_are_the_band_gaps_plus_two_edge_lanes():
    outlines = cab.validate_outlines(GROUPS)
    assert cab.derive_vertical_lanes(outlines) == [-100.0, 500.0, 1100.0]
    assert cab.derive_vertical_lanes([]) == []


def test_nearest_lane_and_dedupe():
    assert cab.nearest_lane_x([], 5.0, 42.0) == 42.0
    assert cab.nearest_lane_x([-100.0, 500.0], 300.0, 0.0) == 500.0
    assert cab.dedupe_path([(0.0, 0.0), (0.0, 0.0), (5.0, 0.0), (10.0, 0.0), (10.0, 5.0)]) == \
        [(0.0, 0.0), (10.0, 0.0), (10.0, 5.0)]


def test_phantom_guard_drops_far_stacked_l2_only():
    l1 = [{"number": 1, "position": (0.0, 0.0)}]
    # Median nearest-combiner distance 50 (index 4 of 8), so the threshold is 200.
    near = [{"number": n, "position": (10.0 * n, 0.0)} for n in (1, 2, 3, 4, 5)]
    far = [{"number": 6, "position": (1e5, 0.0)}, {"number": 7, "position": (1e5 + 50.0, 0.0)}]
    alone = [{"number": 8, "position": (-1e5, 0.0)}]
    kept, dropped = cab.filter_phantom_l2(l1, near + far + alone)
    assert dropped == [6, 7] and [k["number"] for k in kept] == [1, 2, 3, 4, 5, 8]
    assert cab.filter_phantom_l2(l1, near)[1] == []


def test_assignment_respects_the_balanced_cap_and_the_no_pass_rule():
    l2 = [{"number": 1, "position": (0.0, 0.0)}, {"number": 2, "position": (100.0, 0.0)}]
    l1 = [{"number": 1, "position": (-10.0, 0.0)}, {"number": 2, "position": (-20.0, 0.0)}]
    assignments, new = cab.assign_l1_to_l2(l1, l2, user_cap=4)
    # cap ceil(2/2) = 1: the nearer combiner takes L2 1, the other L2 2 (past L2 1, so tripled, but alone).
    assert assignments == {1: 1, 2: 2} and new == 2


# ------------------------------------------------------------------ route-l2-feeders --

def test_route_l2_feeders_adopts_every_existing_feeder_and_reports():
    before = make_state()
    after, lines = cab.route_l2_feeders(before, GROUPS, HOST)
    assert "0 nearest-lane comb feeder(s) drawn." in lines
    assert cables_of(after, "feeder") == cables_of(before, "feeder")
    rows, settings = st.step_rows("i11", before, after, lines)
    assert [r["type"] for r in rows] == ["report", "setting"]
    assert rows[0]["value"] == "no-feeders" and settings[0]["fields"]["name"] == "HomerunRouting"
    assert len(after["setting"]["HomerunRouting"]["CableCatalog"]) == 4


def test_route_l2_feeders_draws_a_missing_feeder_as_a_comb_path():
    before = make_state(feeders=False, numbered=True, settings={"L1ToL2Assignments": {}})
    after, lines = cab.route_l2_feeders(before, GROUPS, HOST)
    drawn = sorted(cables_of(after, "feeder"), key=lambda r: r["from"])
    assert [(r["from"], r["to"]) for r in drawn] == [(1, 1), (2, 2)]
    assert points(drawn[0]) == [CB_1, (-100.0, 200.0), (-100.0, 0.0), L2_1]
    assert drawn[0]["length"]["value"] == pytest.approx((200.0 + 200.0 + 100.0) / 12.0)
    assert after["setting"]["L1ToL2Assignments"] == {"1": 1, "2": 2}
    assert "2 nearest-lane comb feeder(s) drawn." in lines


# ------------------------------------------------------------------ homeruns --

def test_homeruns_auto_redraws_identical_legs_and_reports_no_change():
    before = make_state()
    after, lines = cab.homeruns_auto(before, GROUPS, HOST)
    assert len(cables_of(after, "dc-homerun")) == 4
    rows, _ = st.step_rows("i12", before, after, lines)
    assert [r["type"] for r in rows] == ["report", "setting"] and rows[0]["value"] == "no-change"


def test_homeruns_auto_nearest_combiner_fallback_by_the_marker_midpoint():
    before = make_state(homeruns=False)
    after, _ = cab.homeruns_auto(before, GROUPS, HOST)
    legs = {(r["from"], r["segment"]): r for r in cables_of(after, "dc-homerun")}
    assert points(legs[("A01", "start")]) == [(50.0, 400.0), CB_1]
    assert points(legs[("A02", "end")]) == [(950.0, 400.0), CB_2]
    assert legs[("A02", "end")]["to"] == 2
    assert legs[("A01", "start")]["length"]["value"] == pytest.approx(math.dist((50.0, 400.0), CB_1) / 12.0)


def test_homeruns_auto_without_combiners_prints_the_plugin_line():
    state = make_state()
    state["rows"]["device"] = [d for d in state["rows"]["device"] if d["role"] != "combiner"]
    after, lines = cab.homeruns_auto(state, GROUPS, HOST)
    assert lines == ["No inverters found in drawing. Place inverters first."]
    assert after["rows"] == state["rows"]


# ------------------------------------------------------------------ lite feeders --

def test_lite_feeders_reassign_and_redraw_lane_less_comb_paths():
    before = make_state()
    after, lines = cab.lite_feeders(before, HOST)
    drawn = sorted(cables_of(after, "feeder"), key=lambda r: r["from"])
    assert [(r["from"], r["to"]) for r in drawn] == [(1, 1), (2, 2)]
    assert points(drawn[0]) == [CB_1, (0.0, 200.0), L2_1]
    assert all(r["length"]["value"] == 0.0 for r in drawn)
    assert lines == ["LEAFLITEFEEDERS: 2 orthogonal comb feeder(s) drawn over 0 lanes (replaced 2)."]


def test_lite_assignment_tail_bias_swaps_on_squared_distance():
    invs = [(1, (0.0, 0.0)), (2, (100.0, 0.0))]
    cbs = [(1, (90.0, 0.0)), (2, (10.0, 0.0))]
    assert cab.assign_feeders_lite(cbs, invs, cap=1) == {1: 2, 2: 1}


def test_lite_feeders_refuse_rack_lanes_and_need_the_host_input():
    with pytest.raises(cab.InverterCablingNotPortedError):
        cab.lite_feeders(make_state(), dict(HOST, RackExtents=[[0, 0, 1, 1]]))
    with pytest.raises(cab.InverterCablingError):
        cab.lite_feeders(make_state(), {k: v for k, v in HOST.items() if k != "RackExtents"})


# ------------------------------------------------------------------ move and position --

def test_inverter_move_reroutes_the_moved_devices_homeruns():
    before = make_state()
    after, lines = cab.inverter_move(before, HOST, ["A9D5", "300,200,0"])
    moved = [d for d in after["rows"]["device"] if d["role"] == "combiner"]
    assert (300.0, 200.0) in [tuple(st.point_of(d["position"])) for d in moved]
    rerouted = [r for r in cables_of(after, "dc-homerun") if r["from"] == "A01"]
    assert sorted(points(r)[1] for r in rerouted) == [(300.0, 200.0), (300.0, 200.0)]
    assert all(r["to"] == 1 for r in rerouted)
    untouched = [r for r in cables_of(after, "dc-homerun") if r["from"] == "A02"]
    assert all(points(r)[1] == CB_2 for r in untouched)
    assert len(after["setting"]["HomerunRouting"]["CableCatalog"]) == 4
    rows, _ = st.step_rows("i17", before, after, lines)
    assert sorted({r["type"] for r in rows}) == ["cable", "device", "setting"]


def test_inverter_move_takes_the_acquired_point_as_is():
    # G35b: the answer is the point the jig acquired (host object snap already applied); no snap here,
    # even 10 units from a string endpoint.
    after, _ = cab.inverter_move(make_state(), HOST, ["A9D5", "140.123456789,395"])
    assert (140.123456789, 395.0) in [tuple(st.point_of(d["position"])) for d in after["rows"]["device"]]
    assert (150.0, 400.0) not in [tuple(st.point_of(d["position"])) for d in after["rows"]["device"]]


def test_inverter_position_stays_outside_the_outline_band_and_never_worsens():
    before = make_state()
    after, lines = cab.inverter_position(before, GROUPS, HOST)
    cb = [d for d in after["rows"]["device"] if d["role"] == "combiner" and st.point_of(d["position"])[0] < 500]
    x, y = st.point_of(cb[0]["position"])
    assert y <= 300.0 - cab.OUTLINE_BUFFER + 1e-9
    legs = [(50.0, 400.0), (150.0, 400.0)]
    assert sum(math.dist((x, y), p) for p in legs) <= sum(math.dist(CB_1, p) for p in legs)
    assert "positioned" in lines[0] or "already" in lines[0]


def test_position_search_is_bounded_and_fails_closed_past_its_time():
    ticks = iter(range(10 ** 6))
    with pytest.raises(cab.InverterCablingError):
        cab.optimum_position([(0.0, 0.0), (10.0, 0.0)], (5.0, 5.0), [], budget_s=3.0,
                             clock=lambda: float(next(ticks)))
    with pytest.raises(cab.InverterCablingError):
        cab.optimum_position([(0.0, 0.0)], (5.0, 5.0), [], grid_side=cab.POSITION_GRID_SIDE + 1)
    best, cost = cab.optimum_position([(5.0, 0.0)], (5.0, 5.0), [], grid_side=3)
    assert best == (5.0, 0.0) and cost == 0.0


# ------------------------------------------------------------------ refusals --

def test_combiner_auto_place_is_a_named_refusal():
    with pytest.raises(cab.InverterCablingNotPortedError, match="CombinerPlacementEngine"):
        cab.combiner_auto_place(make_state(), GROUPS, HOST, {"combiner_input_plan": "Apply"})


def test_host_inputs_are_checked():
    with pytest.raises(cab.InverterCablingError):
        cab.route_l2_feeders(make_state(), GROUPS, dict(HOST, Unknown=1))
    with pytest.raises(cab.InverterCablingError):
        cab.inverter_move(make_state(), dict(HOST, MovedDevice=["L3", 1]), ["A9D5", "1,2"])
    with pytest.raises(cab.InverterCablingError):
        cab.inverter_move(make_state(), HOST, ["A9D5", "not a point"])
