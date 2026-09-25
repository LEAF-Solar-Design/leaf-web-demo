"""Studio's inverter cabling engines against the plugin source they port (contract G35).

Covered: device numbering from the feeder circuits; the vertical land lanes; the phantom-L2 guard; the
balanced nearest assignment with the no-pass rule and the swap pass; RouteL2Feeders (every feeder
adopted: nothing drawn, the one save, the report; a missing feeder drawn as a comb path); HomerunsAuto
(each string's legs redrawn identical, so the step reports no change; the nearest-combiner fallback);
LEAFLITEFEEDERS (the tail-biased assignment, lane-less comb paths, stored length 0, the refusals);
MOVEINV (the device moved, its homerun legs rerouted, one save, declared); the POSITIONINV search on
the plugin's bounded plan (InverterMoveSearchTests' cases, the first strictly shortest candidate outside
the outline band, the plugin's failure line, the time bound failing closed); and LEAFCOMBINERAUTO
(the seeded MPPT inputs; the committed i4 state and combiner intake reproducing the committed i5 delta
row for row; its refusals). States are synthetic and authored here, except the LEAFCOMBINERAUTO
fixture, which is the committed G35 chain (docs/parity/evidence/rooftop).
"""
from __future__ import annotations

import copy
import importlib.util
import json
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


# InverterMoveSearchTests.cs:11-25 (Branch2025 #312): (width, height, cap) from (-100, -200).
@pytest.mark.parametrize("width, height, cap", [(2000000, 3000000, 10000), (2000000, 1, 10000),
                                                (1, 3000000, 10000), (2000000, 3000000, 1), (201, 301, 17)])
def test_position_plan_large_or_narrow_extent_respects_the_cap(width, height, cap):
    plan = cab.position_plan(-100, -200, width - 100, height - 200, 20, cap)
    assert 1 <= len(plan) <= cap
    assert plan[0] == (-100.0, -200.0)
    assert all(-100 <= x < width - 100 and -200 <= y < height - 200 for x, y in plan)
    assert len(set(plan)) == len(plan)
    assert plan == sorted(plan, key=lambda p: (p[1], p[0]))  # row-major: rows of y, x ascending in each


def test_position_plan_small_extent_keeps_the_twenty_unit_step_and_row_order():
    # InverterMoveSearchTests.cs:27-37.
    assert cab.position_plan(10, 30, 70, 70, 20, cab.POSITION_MAX_CANDIDATES) == [
        (10.0, 30.0), (30.0, 30.0), (50.0, 30.0), (10.0, 50.0), (30.0, 50.0), (50.0, 50.0)]
    assert (cab.POSITION_STEP, cab.POSITION_MAX_CANDIDATES) == (20.0, 10000)


def test_position_plan_non_multiple_extent_includes_the_last_partial_cell():
    # InverterMoveSearchTests.cs:39-45.
    plan = cab.position_plan(0, 0, 41, 21, 20, 6)
    assert len(plan) == 6 and plan[-1] == (40.0, 20.0)


def test_position_plan_empty_extent_and_invalid_arguments():
    # InverterMoveSearchTests.cs:47-60: empty for a zero extent; the plugin's throws fail closed here.
    assert cab.position_plan(0, 0, 0, 100, 20, 10) == []
    assert cab.position_plan(0, 0, 100, 0, 20, 10) == []
    for args in [(0, 0, 10, 10, 0, 10), (0, 0, 10, 10, 20, 0), (10, 0, 0, 10, 20, 10),
                 (0, 0, math.inf, 10, 20, 10), (0, 0, 10, 10, math.nan, 10), (0, 0, 10, 10, 20, 1.5)]:
        with pytest.raises(cab.InverterCablingError):
            cab.position_plan(*args)


def test_position_plan_doubles_the_step_until_the_cap_holds():
    # 11 x 16 = 176 > 17 -> step 40 (6 x 8 = 48) -> step 80 (3 x 4 = 12).
    plan = cab.position_plan(-100, -200, 101, 101, 20, 17)
    assert plan == [(-100.0 + 80.0 * c, -200.0 + 80.0 * r) for r in range(4) for c in range(3)]
    # A tiny step on a huge extent terminates at the one-cell plan (the step capped at the longer side).
    assert cab.position_plan(0, 0, 1e300, 1e300, 1e-300, 1) == [(0.0, 0.0)]


def reference_optimum(legs, extents, outlines, literal=True):
    """StringHomeRunCmd.cs:845-882: over the plan in order, reject by outline, keep the first strictly
    shorter. literal=False tests the outline only for a would-be winner (a rejected point never updates
    the best, so the accepted sequence is the same) to keep the committed-chain run fast."""
    best, best_cost = None, None
    for p in cab.position_plan(*extents, cab.POSITION_STEP, cab.POSITION_MAX_CANDIDATES):
        if literal and cab._near_outline(p[0], p[1], outlines, cab.OUTLINE_BUFFER):
            continue
        value = sum(cab._dist(p, leg) for leg in legs)
        if (best_cost is None or value < best_cost) and \
                (literal or not cab._near_outline(p[0], p[1], outlines, cab.OUTLINE_BUFFER)):
            best, best_cost = p, value
    return best, best_cost


def test_position_search_keeps_the_first_strictly_shortest_planned_point():
    legs = [(0.0, 0.0), (100.0, 0.0)]
    # Rows y = -50, -30, -10, 10, 30: (50, -10) and (50, 10) score exactly the same; the first row wins.
    best, cost = cab.optimum_position(legs, (-50.0, -50.0, 150.0, 50.0), [])
    assert best == (50.0, -10.0) and cost == 2 * math.sqrt(2600.0)
    # An outline band around the winner moves it to the first strictly shortest point outside the band.
    outlines = [[(40.0, -20.0), (60.0, -20.0), (60.0, 0.0), (40.0, 0.0)]]
    best, cost = cab.optimum_position(legs, (-50.0, -50.0, 150.0, 50.0), outlines)
    assert best == reference_optimum(legs, (-50.0, -50.0, 150.0, 50.0), outlines)[0]
    assert not cab._near_outline(best[0], best[1], outlines, cab.OUTLINE_BUFFER)
    # No planned point survives the band: no position (the plugin's failure branch).
    everywhere = [[(-1e4, -1e4), (1e4, -1e4), (1e4, 1e4), (-1e4, 1e4)]]
    assert cab.optimum_position(legs, (-50.0, -50.0, 150.0, 50.0), everywhere) == (None, None)
    assert cab.optimum_position(legs, (0.0, 0.0, 0.0, 50.0), []) == (None, None)


def test_inverter_position_moves_to_the_plan_optimum():
    # The strings' extents [50, 150] x [400, 400] grown 50: x 0..180, y 350..430; (100, 390) is the
    # first of the two shortest points (y 390 and 410 tie).
    before = make_state()
    after, lines = cab.inverter_position(before, [], HOST)
    positions = [tuple(st.point_of(d["position"])) for d in after["rows"]["device"] if d["role"] == "combiner"]
    assert sorted(positions) == [(100.0, 390.0), CB_2]
    rerouted = [r for r in cables_of(after, "dc-homerun") if r["from"] == "A01"]
    assert sorted(points(r)[1] for r in rerouted) == [(100.0, 390.0), (100.0, 390.0)]
    assert lines[0].startswith("Inverter 1 positioned; 2 homerun(s) rerouted;")


def test_inverter_position_reports_the_plugins_failure_when_the_band_covers_the_plan():
    # Changed (R09b): the plan spans only the strings' extents grown 50, all inside the A5 band, so the
    # plugin finds no position and moves nothing; the old 64 x 64 grid reached below the band.
    before = make_state()
    after, lines = cab.inverter_position(before, GROUPS, HOST)
    assert lines == ["Failed to find optimum position for Inverter: 1"]
    assert after == before


def test_position_search_fails_closed_past_its_time_and_never_changes_a_finished_answer():
    ticks = iter(range(10 ** 6))
    with pytest.raises(cab.InverterCablingError, match="exceeded"):
        cab.optimum_position([(0.0, 0.0), (10.0, 0.0)], (-50.0, -50.0, 60.0, 50.0), [], budget_s=3.0,
                             clock=lambda: float(next(ticks)))
    legs, extents = [(0.0, 0.0), (10.0, 0.0)], (-50.0, -50.0, 60.0, 50.0)
    assert cab.optimum_position(legs, extents, [], clock=lambda: 0.0) == \
        cab.optimum_position(legs, extents, [], budget_s=math.inf) == reference_optimum(legs, extents, [])


def test_inverter_position_on_the_committed_chain_picks_the_plan_optimum():
    # The G35 POSITIONINV fixture: device L1 14 of the committed state-i16 against the chain intake's
    # panel groups (scripts/solar_inverter_cabling_evidence.py CAPTURE_HOST PositionDevice).
    chain = json.loads((EVIDENCE / "chain" / "intake.json").read_text(encoding="utf-8"))
    groups = [{"handle": g.get("handle"), "outlines": g.get("outlines") or []} for g in chain["panel_groups"]]
    before = st.load_state(EVIDENCE / "inverters" / "state-i16.json")
    host = dict(HOST, PositionDevice=["L1", 14])
    target = cab._find_device(before, "L1", 14)
    legs = cab._device_homerun_legs(before, target["position"])
    assert legs
    extents = cab._position_extents(before, target["position"], legs)
    outlines = cab.validate_outlines(groups)
    expected, _ = reference_optimum(legs, extents, outlines, literal=False)
    plan = cab.position_plan(*extents)
    assert 1 <= len(plan) <= cab.POSITION_MAX_CANDIDATES
    after, lines = cab.inverter_position(before, groups, host)
    if expected is None:
        assert lines == ["Failed to find optimum position for Inverter: 14"]
    elif expected == target["position"]:
        assert lines == ["Inverter 14 is already at its optimum position."]
    else:
        # The moved device leaves the feeder vertex that numbered it (levels), so it is found by position:
        # exactly the picked device moved, to the plan optimum, and its homerun legs now end there.
        before_positions = sorted(tuple(st.point_of(d["position"])) for d in before["rows"]["device"])
        after_positions = sorted(tuple(st.point_of(d["position"])) for d in after["rows"]["device"])
        vacated = list(before_positions)
        vacated.remove(target["position"])
        assert after_positions == sorted(vacated + [expected])
        assert "positioned" in lines[0] and f"{len(legs)} homerun(s) rerouted" in lines[0]
        assert sorted(cab._device_homerun_legs(after, expected)) == sorted(legs)
        assert expected in plan


# ------------------------------------------------------------------ combiner-auto-place --

EVIDENCE = ROOT / "docs" / "parity" / "evidence" / "rooftop"
PLAN = {"combiner_input_plan": "Apply"}
CAPTURE = dict(HOST, CombinerSymbolScale=2.891214911191)


def committed_fixture():
    intake = json.loads((EVIDENCE / "inverters" / "combiner-intake.json").read_text(encoding="utf-8-sig"))
    chain = json.loads((EVIDENCE / "chain" / "intake.json").read_text(encoding="utf-8"))
    groups = [{"handle": g.get("handle"), "outlines": g.get("outlines") or []} for g in chain["panel_groups"]]
    return st.load_state(EVIDENCE / "inverters" / "state-i4.json"), intake, groups


def assert_rows_close(expected, actual, path="rows"):
    if isinstance(expected, dict):
        assert expected.keys() == actual.keys(), path
        for key in expected:
            assert_rows_close(expected[key], actual[key], f"{path}/{key}")
    elif isinstance(expected, list):
        assert len(expected) == len(actual), path
        for i, (a, b) in enumerate(zip(expected, actual)):
            assert_rows_close(a, b, f"{path}/{i}")
    elif isinstance(expected, float) or isinstance(actual, float):
        assert actual == pytest.approx(expected, abs=1e-6), path
    else:
        assert expected == actual, path


def test_seeded_mppt_inputs_count_each_l2s_combiners_in_number_order():
    # MpptBalanceAnalyzer.cs:854-877: grouped by L2, sorted by L1 number, slot = index mod MPPT count.
    assert cab.seed_input_assignments({3: 1, 1: 1, 2: 1, 4: 2, 5: 1}, 3) == {1: 0, 2: 1, 3: 2, 5: 0, 4: 0}
    assert cab.seed_input_assignments({1: 1}, 0) == {}
    assert cab.seed_input_assignments({}, 6) == {}


def test_combiner_auto_place_reproduces_the_committed_i5_delta():
    before, intake, groups = committed_fixture()
    after, lines = cab.combiner_auto_place(before, groups, CAPTURE, PLAN, intake)
    expected, _ = st.step_rows("i5", before, st.load_state(EVIDENCE / "inverters" / "state-i5.json"))
    actual, settings = st.step_rows("i5", before, after, lines)
    kinds = {}
    for row in actual:
        key = (row["type"], row.get("cable_kind"))
        kinds[key] = kinds.get(key, 0) + 1
    assert kinds == {("cable", "dc-homerun"): 346, ("cable", "feeder"): 14, ("device", None): 14,
                     ("setting", None): 4}
    assert_rows_close(expected, actual)
    assert [s["fields"]["name"] for s in settings] == ["CombinerStringL1Assignments", "HomerunRouting",
                                                       "L1ToL2Assignments", "L1ToL2InputAssignments"]
    # Two drawing-properties saves: the association persist and RouteL2Feeders' (the G27 catalog growth).
    grown = len(after["setting"]["HomerunRouting"]["CableCatalog"]) - \
        len(before["setting"]["HomerunRouting"]["CableCatalog"])
    assert grown == 4
    assert "HomerunsAuto: drew 346 straight-line DC homerun(s)." in lines
    assert "14 nearest-lane comb feeder(s) drawn." in lines
    assert before == committed_fixture()[0]  # the input state is never mutated


def test_combiner_auto_place_refusals():
    before, intake, groups = committed_fixture()
    with pytest.raises(cab.InverterCablingError, match="form value"):
        cab.combiner_auto_place(before, groups, CAPTURE, {"combiner_input_plan": "Cancel"}, intake)
    with pytest.raises(cab.InverterCablingError, match="intake"):
        cab.combiner_auto_place(before, groups, CAPTURE, PLAN, None)
    with pytest.raises(cab.InverterCablingNotPortedError, match="L1/L2 mode"):
        cab.combiner_auto_place(before, groups, dict(CAPTURE, UseL2Collectors=False), PLAN, intake)
    # A drawing that already holds L1 combiners takes a path this port does not cover.
    with pytest.raises(cab.InverterCablingNotPortedError, match="existing L1"):
        cab.combiner_auto_place(make_state(), GROUPS, CAPTURE, PLAN, intake)
    # An intake that is not this drawing's (its L2 blocks are elsewhere) fails closed.
    elsewhere = make_state()
    elsewhere["rows"]["device"] = [d for d in elsewhere["rows"]["device"] if d["role"] != "combiner"]
    with pytest.raises(cab.InverterCablingError, match="L2"):
        cab.combiner_auto_place(elsewhere, GROUPS, CAPTURE, PLAN, intake)
    with pytest.raises(cab.InverterCablingError, match="symbol scale"):
        cab.combiner_auto_place(before, groups, HOST, PLAN, intake)


# ------------------------------------------------------------------ refusals --


def test_host_inputs_are_checked():
    with pytest.raises(cab.InverterCablingError):
        cab.route_l2_feeders(make_state(), GROUPS, dict(HOST, Unknown=1))
    with pytest.raises(cab.InverterCablingError):
        cab.inverter_move(make_state(), dict(HOST, MovedDevice=["L3", 1]), ["A9D5", "1,2"])
    with pytest.raises(cab.InverterCablingError):
        cab.inverter_move(make_state(), HOST, ["A9D5", "not a point"])
