"""MOVEINV on the design graph: one picked device moved to a picked point, its homeruns rerouted through the
route bridge; a refused move persists nothing, and publication belongs to the commit rail's publish_version.
The combiner move C4 (both A01 legs rerouted, A02 untouched), the W1
rooftop move, trenches and pathways, feeders left in place and staled, schedules staled, every drawing unit,
the named refusals, the kernel answer checks, the registry and the commit rail."""
import copy
import hashlib
import json
import math
import re
import sys
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

SERVER = Path(__file__).resolve().parents[1]
ROOT = SERVER.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import catalog
import deps
import entitlements
import jobs
import product_capability_availability as availability
import solar_electrical_route_bridge as rb
import solar_local_graph
import solar_tools
import store
from solar_design_graph import GraphValidationError, validate_graph
from test_w1_design_graph import app_id, graph  # noqa: F401
from test_w1_local_graph_adapter import held
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_local_graph_rail import _api, body
from test_w1_solve_commit import seed
from test_solar_w2_registration import REF, dispatch, expected_declaration, head_graph, latest
import test_solar_electrical_route_bridge as rbt
import test_solar_ground_route_kinds as rk
import test_solar_ground_topology as topo

TOOL = "solar-equipment-move"
TENANT = "fixture-tenant"
INVALID = "INVALID_EQUIPMENT_MOVE_REQUEST"
W1_SHA = "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3"
C4_SHA = "ec314e216b589f2b3b8e18abf563c31136f626a43e50634f58b39fc0c025dd98"
C4_MOVED_SHA = "909a592d683e10521c1fcd59b0daf74737f671bd4cb5c2daff9943071726a7ed"
CB1, CB2, L2_1 = app_id("inverter", 101), app_id("inverter", 102), app_id("inverter", 103)
POINT = {"type": "array", "minItems": 2, "maxItems": 2,
         "items": {"type": "number", "minimum": -1000000000000, "maximum": 1000000000000}}
DROP = object()


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def builtin():
    return solar_local_graph._load_builtin(TOOL)


def c4(graph):
    """C4: the recorded combiner state (two combiners, two central inverters, strings A01 and A02, four legs,
    two feeders) adopted onto the W1 strings graph through the route bridge."""
    g, _ = rbt.adopt_write(graph, rbt.ct.make_state())
    assert sha(g) == C4_SHA
    return g


def request(ref, point, rev=0):
    return {"expected_rev": rev, "inverter_ref": ref, "point": point}


def move(g, ref, point, rev=None):
    before = copy.deepcopy(g)
    params = request(ref, point, g["rev"] if rev is None else rev)
    out = builtin().move_equipment(g, params)
    assert g == before
    assert builtin().run(copy.deepcopy(g), dict(params, operation="move-equipment")) == out["graph"]
    return out


def route(g, ident):
    return next(item for item in g["routes"] if item["id"] == ident)


def changed_routes(before, after):
    old = {item["id"]: item for item in before["routes"]}
    return [item["id"] for item in after["routes"] if item != old[item["id"]]]


def stale_graph(graph, name):
    g = copy.deepcopy(graph)
    if name == "unrelated-schedule":
        extra = copy.deepcopy(g["schedules"][0])
        extra["id"] = app_id("schedule", 2)
        extra["source_refs"] = [app_id("panel", 1)]
        g["schedules"].append(extra)
    elif name == "already-stale":
        g["schedules"][0]["validity"] = {"state": "stale", "reasons": ["settings_changed"]}
    return validate_graph(g)


# ------------------------------------------------------------------ C4: the combiner move --

def test_equipment_move_c4_moves_the_combiner_and_reroutes_a01(graph):
    g = c4(graph)
    out = move(g, CB1, [300, 200])
    moved = out["graph"]
    assert sha(moved) == C4_MOVED_SHA
    assert sha(out["receipt"]) == "081a641c813740f7a334ca6d7fe65d1c0e3b50fa8e560d4b5469573b84d3e636"
    assert out["receipt"] == {
        "schema": "leaf.solar-equipment-move.v1", "inverter_ref": CB1, "point": [300.0, 200.0],
        "position": [7.62, 5.08], "rerouted_route_refs": [app_id("route", 105), app_id("route", 106)],
        "stale_feeder_refs": [app_id("route", 109)], "stale_schedule_refs": [],
        "lines": ["Inverter 1 moved; 2 homerun(s) rerouted."]}
    assert {key for key in g if g[key] != moved[key]} == {"extra", "inverters", "parent_rev", "rev", "routes"}
    assert (moved["rev"], moved["parent_rev"]) == (1, 0)
    assert [i["id"] for i in moved["inverters"]] == [i["id"] for i in g["inverters"]]
    assert [i["position"] for i in moved["inverters"]] == [[7.62, 5.08], [22.86, 5.08], [0.0, 0.0], [25.4, 0.0]]
    assert [r["id"] for r in moved["routes"]] == [r["id"] for r in g["routes"]]
    assert changed_routes(g, moved) == [app_id("route", n) for n in (105, 106, 109)]
    a01 = [route(moved, app_id("route", n)) for n in (105, 106)]
    assert [(r["route_kind"], r["points"], r["length_ft"], r["wire_gauge"], r["to_ref"], r["validity"]["state"])
            for r in a01] == [
        ("end homerun", [[3.81, 10.16], [7.62, 5.08]], 250.0 / 12.0, "14 AWG", CB1, "valid"),
        ("start homerun", [[1.27, 10.16], [7.62, 5.08]], math.dist((50, 400), (300, 200)) / 12.0, "14 AWG", CB1,
         "valid")]
    # A02's legs and the second feeder are untouched; the moved combiner's feeder keeps its geometry and goes stale.
    for n in (107, 108, 110):
        assert route(moved, app_id("route", n)) == route(g, app_id("route", n))
    feeder, old = route(moved, app_id("route", 109)), route(g, app_id("route", 109))
    assert {k: v for k, v in feeder.items() if k not in ("validity", "rev", "provenance")} == \
        {k: v for k, v in old.items() if k not in ("validity", "rev", "provenance")}
    assert feeder["validity"] == {"state": "stale", "reasons": ["equipment_moved"]}
    assert all(r["rev"] == 1 and r["provenance"]["last_writer"] == TOOL for r in a01 + [feeder])
    # The routes and positions are exactly the route bridge's own write-back of the kernel's C4 answer.
    state, binding = rb.state_from_graph(g)
    after, lines = rb.cab.inverter_move(state, rbt.ct.HOST, ["A9D5", "300,200,0"])
    assert lines == ["Inverter 1 moved; 2 homerun(s) rerouted."]
    bridged, _ = rb.graph_from_state(g, after, binding)
    assert sha(bridged) == "47b615ca05f223ddb7b3156cdc8250e841f8f88bafedce458407a6088598e1a7"
    assert [(r["points"], r["length_ft"]) for r in moved["routes"]] == \
        [(r["points"], r["length_ft"]) for r in bridged["routes"]]
    assert [i["position"] for i in moved["inverters"]] == [i["position"] for i in bridged["inverters"]]


@pytest.mark.parametrize("sources,state,reasons,listed,expected", [
    pytest.param([CB1], "stale", ["equipment_moved"], [app_id("schedule", 1)],
                 "410ae62aaaf68d9cf2d7452335416d6d9bd109ab84b6065f1b1639eb733768f2", id="device"),
    pytest.param([app_id("route", 105)], "stale", ["equipment_moved"], [app_id("schedule", 1)],
                 "032c73e9a48009a6355ccf54bb9f2ad6a6321f36be72e2ed0181ef91639bc158", id="rerouted"),
    pytest.param([app_id("route", 109)], "stale", ["equipment_moved"], [app_id("schedule", 1)],
                 "d78f3c502b2b900097baedeab069b487190de6bac549fc465febcda6f81be1a1", id="feeder"),
    pytest.param([app_id("string", 1)], "stale", ["equipment_moved"], [app_id("schedule", 1)],
                 "2362bb8d35257aaa5dd29cb05a9df59808a8198aa3b0e40fd784ea3dad63b687", id="string"),
    pytest.param([app_id("panel", 1)], "valid", [], [],
                 "969e1f94395b4dcf30036dc1e448c28644aad0ebe6f30adb6f8e1c593462789c", id="unrelated"),
])
def test_equipment_move_c4_schedule_sources(graph, sources, state, reasons, listed, expected):
    g = c4(graph)
    schedule = copy.deepcopy(graph["schedules"][0])
    schedule["source_refs"] = sources
    g["schedules"] = [schedule]
    out = move(validate_graph(g), CB1, [300, 200])
    validity = out["graph"]["schedules"][0]["validity"]
    assert (validity["state"], validity["reasons"]) == (state, reasons)
    assert out["receipt"]["stale_schedule_refs"] == listed
    assert sha(out["graph"]) == expected


def test_equipment_move_second_combiner(graph):
    g = c4(graph)
    out = move(g, CB2, [700, 250.5])
    assert sha(out["graph"]) == "8c856c1c8fb4943501c90545509bb9f3864cedd0b9cd86fbc9f91f48f134b449"
    assert sha(out["receipt"]) == "76e4025683ecc24714eb5771fed90fa20f9ccb2683b5831df6c86d38d62e96b5"
    assert out["receipt"]["position"] == [17.779999999999998, 6.362699999999999]
    assert out["receipt"]["rerouted_route_refs"] == [app_id("route", 107), app_id("route", 108)]
    assert out["receipt"]["stale_feeder_refs"] == [app_id("route", 110)]
    assert [route(out["graph"], app_id("route", n))["length_ft"] for n in (107, 108)] == \
        [24.274221866462003, 17.648231340404752]
    for n in (105, 106, 109):
        assert route(out["graph"], app_id("route", n)) == route(g, app_id("route", n))


def test_equipment_move_i5_combiner_at_scale(graph):
    """The recorded i5 combiner state (C5: 173 strings, 346 legs, 22 devices, 14 feeders) adopted onto a graph:
    combiner 1 moved 120 drawing units right and 48 down reroutes its 20 legs and stales its one feeder."""
    after, _ = rbt.bt.c5()
    g, _ = rbt.adopt_write(graph, after)
    assert sha(g) == "f3d0c2e8485d6d1c5261ac7f6d7d792336bdf642d485f171cbe9e137d5679faa"
    assert (len(g["inverters"]), len(g["routes"])) == (22, 360)
    out = move(g, CB1, [14401.065886049604, 1362.1912760313533])
    assert sha(out["graph"]) == "88dfab60411538d0a9a9261390eea75a1c8267f21f6edc159d386b8a96136d48"
    assert sha(out["receipt"]) == "fbcdebb532cc0d16d5f009f519f5a99b607711c26f845cebe52880eef5128f89"
    assert len(out["receipt"]["rerouted_route_refs"]) == 20
    assert out["receipt"]["stale_feeder_refs"] == [app_id("route", 469)]
    assert out["receipt"]["lines"] == ["Inverter 1 moved; 20 homerun(s) rerouted."]
    assert out["receipt"]["position"] == [365.7870735056599, 34.59965841119637]
    assert len(changed_routes(g, out["graph"])) == 21


# ------------------------------------------------------------------ W1, trenches, schedules, units --

def test_equipment_move_w1_stales_its_schedule(graph):
    g = validate_graph(copy.deepcopy(graph))
    assert sha(g) == W1_SHA
    out = move(g, app_id("inverter", 1), [300, 200])
    moved = out["graph"]
    assert sha(moved) == "31cef82f659a69ec293925c841c72a39e7cec7e994db575132cc56d7b009b958"
    assert sha(out["receipt"]) == "878b9f4c616ae5ed4f79adf42dc06520e1b8548c2161f4d8064f242c79766d1c"
    assert {key for key in g if g[key] != moved[key]} == \
        {"extra", "inverters", "parent_rev", "rev", "routes", "schedules"}
    homerun = moved["routes"][0]
    assert (homerun["points"], homerun["length_ft"], homerun["validity"]) == \
        ([[0, 0], [7.62, 5.08]], 30.046260628866577, {"state": "valid", "reasons": []})
    assert moved["inverters"][0]["position"] == [7.62, 5.08]
    assert moved["schedules"][0]["validity"] == {"state": "stale", "reasons": ["equipment_moved"]}
    assert out["receipt"]["stale_schedule_refs"] == [app_id("schedule", 1)]


def test_equipment_move_trench_and_pathways(graph):
    g = rk.composite(graph)
    out = move(g, rk.L1_ID, [300, 200])
    moved = out["graph"]
    assert sha(moved) == "eff8d00c9af9ff8b961eae5e794f402c33f5f6d4b37cec6442e05f8542a6b4db"
    assert sha(out["receipt"]) == "99c508ddd1d10b42d6b008a976ab070daa0ba8bcd9792bc183463988efb7e68d"
    assert rk.route(moved, rk.TRENCH_ID) == rk.route(g, rk.TRENCH_ID)
    homerun, feeder = rk.route(moved, rk.HOMERUN_ID), rk.route(moved, rk.FEEDER_ID)
    assert rk.route(g, rk.HOMERUN_ID)["pathway_ref"] == rk.TRENCH_ID
    assert (homerun["points"], homerun["pathway_ref"], homerun["length_ft"]) == \
        ([[0, 0], [7.62, 5.08]], None, 30.046260628866577)
    assert (feeder["points"], feeder["pathway_ref"], feeder["length_ft"], feeder["validity"]) == \
        (rk.route(g, rk.FEEDER_ID)["points"], rk.TRENCH_ID, rk.route(g, rk.FEEDER_ID)["length_ft"],
         {"state": "stale", "reasons": ["equipment_moved"]})
    assert out["receipt"]["stale_feeder_refs"] == [rk.FEEDER_ID]
    assert [i["position"] for i in moved["inverters"]] == [[7.62, 5.08], g["inverters"][1]["position"]]


@pytest.mark.parametrize("name,states,listed,expected", [
    ("unrelated-schedule", [("stale", ["equipment_moved"]), ("valid", [])], [app_id("schedule", 1)],
     "fc86d036064f86425aa1bdf19dc8e37a8c2607bdbc8e4d1bf047ed507f4452e4"),
    ("already-stale", [("stale", ["settings_changed"])], [],
     "b42bf26c941fe2efa74830b844df2d9ec981eaa19dadb64f37b7b482850e0678"),
])
def test_equipment_move_schedules(graph, name, states, listed, expected):
    g = stale_graph(graph, name)
    out = move(g, app_id("inverter", 1), [300, 200])
    assert [(s["validity"]["state"], s["validity"]["reasons"]) for s in out["graph"]["schedules"]] == states
    assert out["receipt"]["stale_schedule_refs"] == listed
    assert sha(out["graph"]) == expected


@pytest.mark.parametrize("unit,mpu,is_feet,points,length,expected", [
    ("m", 1.0, False, [[0, 0], [300.0, 200.0]], 1182.9236468057709,
     "01792bf7eb989b8a56ced49c40cbaf176a943b5af86c4ce921ff2d6687bdf58e"),
    ("ft", 0.3048, True, [[0, 0], [91.44, 60.96]], 360.555127546399,
     "00d1c6539772a02f84b37a73370a1d6fd9f40c9d53f0a403664d14a0375f38d2"),
    ("mm", 0.001, False, [[0, 0], [0.3, 0.2]], 1.1829236468057707,
     "ae175dd9facb8be703beda1959f2cca3799d7905b60612aa81259b7b0fffbaa0"),
    ("cm", 0.01, False, [[0, 0], [3.0, 2.0]], 11.829236468057708,
     "ef77ff85afdb5e60fb3cde016b2bdb96a1dca405c9b44fa0eca5400bbf0a9c51"),
])
def test_equipment_move_drawing_units(graph, unit, mpu, is_feet, points, length, expected):
    g = copy.deepcopy(graph)
    g["project"]["units"].update(drawing_units=unit, meters_per_unit=mpu, drawing_unit_is_feet=is_feet)
    out = move(validate_graph(g), app_id("inverter", 1), [300, 200])
    homerun = out["graph"]["routes"][0]
    assert (homerun["points"], homerun["length_ft"]) == (points, length)
    # The kernel's inch feet times meters_per_unit / 0.0254: the true feet of the leg to the last ulp or two.
    assert math.isclose(length, math.dist(points[0], points[1]) / 0.3048, rel_tol=1e-12)
    assert out["graph"]["inverters"][0]["position"] == points[1]
    assert sha(out["graph"]) == expected


# ------------------------------------------------------------------ refusals --

def _shared(graph):
    g = c4(graph)
    for inverter in g["inverters"]:
        if inverter["id"] == CB2:
            inverter["position"] = [2.54, 5.08]
    for item in g["routes"]:
        if item["id"] in (app_id("route", 107), app_id("route", 108)):
            item["points"][-1] = [2.54, 5.08]
    return validate_graph(g)


def _w1(change):
    def build(graph):
        g = copy.deepcopy(graph)
        change(g)
        return validate_graph(g)
    return build


REFUSALS = [
    ("l2-has-no-homeruns", c4, L2_1, [50, 50], None, "NO_HOMERUNS_TO_MOVE"),
    ("no-routes", _w1(lambda g: g.update(routes=[])), app_id("inverter", 1), [300, 200], None,
     "NO_HOMERUNS_TO_MOVE"),
    ("same-point", c4, CB1, [100, 200], None, "EQUIPMENT_POSITION_UNCHANGED"),
    ("missing-device", c4, app_id("inverter", 999), [1, 2], None, "MISSING_EQUIPMENT"),
    ("stale-revision", c4, CB1, [300, 200], 1, "STALE_GRAPH_REVISION"),
    ("shared-position", _shared, CB1, [300, 200], None, "EQUIPMENT_MOVE_MAPPING_FAILED"),
    ("topology-mismatch", _w1(lambda g: g["routes"][0]["points"].__setitem__(-1, [6, 0])), app_id("inverter", 1),
     [300, 200], None, "BRIDGE_ROUTE_TOPOLOGY_MISMATCH"),
    ("far-point", _w1(lambda g: None), app_id("inverter", 1), [1e12, 0], None, "BRIDGE_ROUTE_INVALID"),
    ("units-unresolved", _w1(lambda g: g["project"]["units"].update(meters_per_unit=0.0508)),
     app_id("inverter", 1), [300, 200], None, "UNRESOLVED_UNITS"),
]


@pytest.mark.parametrize("name,build,ref,point,rev,code", REFUSALS, ids=[row[0] for row in REFUSALS])
def test_equipment_move_refusals(graph, name, build, ref, point, rev, code):
    g = build(graph)
    before = copy.deepcopy(g)
    params = request(ref, point, g["rev"] if rev is None else rev)
    with pytest.raises(GraphValidationError) as error:
        builtin().move_equipment(g, params)
    assert error.value.code == code
    with pytest.raises(GraphValidationError) as error:
        builtin().run(g, dict(params, operation="move-equipment"))
    assert error.value.code == code
    assert g == before


def _drop_one(state, host, answers):
    after, lines = KERNEL[0](state, host, answers)
    untouched = next(row for row in after["rows"]["cable"]
                     if row.get("cable_kind") == "dc-homerun" and row["_pair"].startswith("cable:bridge-"))
    after["rows"]["cable"].remove(untouched)
    return after, lines


def _wrong_line(state, host, answers):
    after, _ = KERNEL[0](state, host, answers)
    return after, ["Inverter 1 moved; 3 homerun(s) rerouted."]


def _nothing_redrawn(state, host, answers):
    return copy.deepcopy(state), ["Inverter 1 moved; 0 homerun(s) rerouted."]


def _raises(state, host, answers):
    raise rb.cab.InverterCablingError("no L1 device numbered 1 in the state")


KERNEL = []


@pytest.mark.parametrize("patch", [_raises, _drop_one, _wrong_line, _nothing_redrawn],
                         ids=["kernel-error", "erased-homerun", "line-count", "nothing-redrawn"])
def test_equipment_move_kernel_answers_are_checked(graph, monkeypatch, patch):
    g = c4(graph)
    before = copy.deepcopy(g)
    KERNEL[:] = [rb.cab.inverter_move]
    monkeypatch.setattr(builtin().cab, "inverter_move", patch)
    with pytest.raises(GraphValidationError) as error:
        builtin().move_equipment(g, request(CB1, [300, 200]))
    assert error.value.code == "EQUIPMENT_MOVE_MAPPING_FAILED"
    assert g == before


def test_equipment_move_device_numbering_must_resolve_to_the_bound_row(graph, monkeypatch):
    g = c4(graph)
    real = builtin().bridge.state_from_graph

    def renumbered(value):
        state, binding = real(value)
        for row in state["rows"]["device"]:
            if row["_pair"] == "device:bridge-2":
                row["_number"] = 1
        return state, binding

    monkeypatch.setattr(builtin().bridge, "state_from_graph", renumbered)
    with pytest.raises(GraphValidationError) as error:
        builtin().move_equipment(g, request(CB1, [300, 200]))
    assert error.value.code == "EQUIPMENT_DEVICE_AMBIGUOUS"


def test_equipment_move_never_searches_for_an_optimum(graph, monkeypatch):
    for name in ("inverter_position", "optimum_position", "position_plan"):
        monkeypatch.setattr(builtin().cab, name, lambda *a, **k: pytest.fail("the move never searches"))
    assert sha(move(c4(graph), CB1, [300, 200])["graph"]) == C4_MOVED_SHA


NONFINITE = "NONFINITE_NUMBER"


@pytest.mark.parametrize("params", [
    None, [], 7, {}, {"expected_rev": 0, "inverter_ref": CB1},
    {"expected_rev": 0, "inverter_ref": CB1, "point": [1, 2], "x": 1},
    {"expected_rev": "0", "inverter_ref": CB1, "point": [1, 2]},
    {"expected_rev": True, "inverter_ref": CB1, "point": [1, 2]},
    {"expected_rev": 0, "inverter_ref": "", "point": [1, 2]},
    {"expected_rev": 0, "inverter_ref": "x" * 129, "point": [1, 2]},
    {"expected_rev": 0, "inverter_ref": 101, "point": [1, 2]},
    {"expected_rev": 0, "inverter_ref": CB1, "point": [1]},
    {"expected_rev": 0, "inverter_ref": CB1, "point": [1, 2, 3]},
    {"expected_rev": 0, "inverter_ref": CB1, "point": [True, 2]},
    {"expected_rev": 0, "inverter_ref": CB1, "point": ["1", 2]},
    {"expected_rev": 0, "inverter_ref": CB1, "point": [float("nan"), 2]},
    {"expected_rev": 0, "inverter_ref": CB1, "point": [float("inf"), 2]},
    {"expected_rev": 0, "inverter_ref": CB1, "point": [1.0000001e12, 2]},
    {"expected_rev": 0, "inverter_ref": CB1, "point": {"x": 1, "y": 2}},
    {"expected_rev": 0, "inverter_ref": CB1, "point": [1, 2], "cancel": True},
])
def test_equipment_move_request_shape_fails_closed(graph, params):
    g = c4(graph)
    before, original = copy.deepcopy(g), copy.deepcopy(params)
    # A non-finite number is the JSON bound's own refusal, raised before the shape check.
    code = NONFINITE if isinstance(params, dict) and any(
        type(v) is float and not math.isfinite(v) for v in params.get("point", [])) else INVALID
    with pytest.raises(GraphValidationError) as error:
        builtin().move_equipment(g, params)
    assert error.value.code == code
    if isinstance(params, dict):
        with pytest.raises(GraphValidationError) as error:
            builtin().run(g, dict(params, operation="move-equipment"))
        assert error.value.code == code
    for wrapped in ({"operation": "position-equipment", **request(CB1, [1, 2])}, request(CB1, [1, 2]), None):
        with pytest.raises(GraphValidationError) as error:
            builtin().run(g, wrapped)
        assert error.value.code == INVALID
    assert g == before and params == original


# ------------------------------------------------------------------ registry, schema, readiness --

def test_equipment_move_registry_and_catalog(monkeypatch):
    declaration = solar_tools.get(TOOL)
    actual = copy.deepcopy(declaration)
    description = actual["record"].pop("description")
    assert type(description) is str and description.strip() and len(description) <= 1024
    expected = expected_declaration(
        TOOL, "equipment", 100, INVALID, [], ["inverter-move"], "move-equipment",
        {"inverter_ref": REF, "point": POINT}, ["inverter_ref", "point"])
    expected["readiness"] = {"kind": "hook"}
    assert actual == expected
    tools = solar_tools.local_graph_tools()
    assert TOOL in tools and TOOL not in solar_tools.local_graph_read_tools()
    assert availability.SOLAR_CAPABILITIES[TOOL]["adapter"] == "local-graph-commit"
    assert TOOL not in availability.W1_CAPABILITIES and len(availability.W1_CAPABILITIES) == 9
    assert entitlements.tool_required_capability(solar_tools.trusted_record(TOOL)) == "run_write"
    monkeypatch.setattr(deps, "tenant_repo_dir", lambda tenant: None)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    families = catalog.build_catalog(deps.all_tools(TENANT))
    found = [(family, row) for family in families for row in family["capabilities"] if row["name"] == TOOL]
    assert len(found) == 1
    family, row = found[0]
    assert family["family_id"] == "equipment"
    assert row["solar"] == solar_tools.catalog_view(declaration["record"])
    assert row["params_schema"] == declaration["record"]["params"]


def test_equipment_move_params_schema():
    validator = Draft7Validator(solar_tools.trusted_record(TOOL)["params"])
    valid = dict(request(CB1, [300, 200]), operation="move-equipment")
    assert validator.is_valid(valid)
    assert validator.is_valid(dict(valid, drawing_id="solar", expected_rev=2147483647, point=[-1e12, 1e12]))
    for key in ("operation", "expected_rev", "inverter_ref", "point"):
        missing = dict(valid)
        del missing[key]
        assert not validator.is_valid(missing), key
    for patch in [{"unknown": 1}, {"operation": "position-equipment"}, {"expected_rev": -1},
                  {"expected_rev": 2147483648}, {"inverter_ref": ""}, {"inverter_ref": "x" * 129},
                  {"point": [1]}, {"point": [1, 2, 3]}, {"point": ["1", 2]}, {"point": [1.0000001e12, 0]},
                  {"drawing_id": "d" * 129}]:
        assert not validator.is_valid(dict(valid, **patch)), patch


def test_equipment_move_readiness(graph):
    assert availability.w1_graph_readiness(c4(graph))[TOOL] == {"input_ready": True, "input_reason": None}
    inputs = availability.w1_local_commit_inputs
    assert inputs(validate_graph(copy.deepcopy(graph)))[TOOL] == {"input_ready": True, "input_reason": None}
    assert inputs(topo.bare(graph))[TOOL] == {"input_ready": False,
                                             "input_reason": "equipment_assignment_required"}
    assert inputs(_w1(lambda g: g.update(routes=[]))(graph))[TOOL] == {
        "input_ready": False, "input_reason": "complete_routing_required"}
    assert inputs(_w1(lambda g: g["project"]["units"].update(meters_per_unit=0.0508))(graph))[TOOL] == {
        "input_ready": False, "input_reason": "unresolved_units"}
    # The hook's reasons are keys of the Solar rail's refusal map, found by the web test's own literal scan,
    # and the builtin source carries no dynamic code (the web scan would demand a table row for one).
    source = (SERVER / solar_tools.get(TOOL)["builtin"]).read_text(encoding="utf-8")
    reasons = set(re.findall(r'"input_reason":\s*"([a-z][a-z0-9_]{0,63})"', source))
    assert reasons == {"equipment_assignment_required", "complete_routing_required"}
    assert ".lower()" not in source
    rail = (ROOT / "web" / "src" / "lib" / "ribbonClusters.js").read_text(encoding="utf-8")
    table = rail[rail.index("export const SOLAR_REFUSAL_REASONS"):]
    table = table[:table.index("})")]
    assert reasons <= set(re.findall(r"^\s+([a-z][a-z0-9_]*):", table, re.M))


# ------------------------------------------------------------------ the commit rail --

def test_equipment_move_dispatch_persists_and_reads_back(graph, tmp_path, monkeypatch):
    g = c4(graph)
    expected = move(g, CB1, [300, 200])["graph"]
    backend, _ = seed(tmp_path, monkeypatch, g)
    params = dict(request(CB1, [300, 200]), operation="move-equipment")
    with held(backend) as fence:
        receipt = dispatch(backend, fence, TOOL, params)
        assert receipt["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
        assert (receipt["before_rev"], receipt["after_rev"], receipt["drawing_changed"]) == (0, 1, True)
        with pytest.raises(GraphValidationError, match="STALE_GRAPH_REVISION"):
            solar_local_graph.run_local_graph_commit(
                backend, TENANT, TOOL, dict(params, drawing_id="solar"), drawing_id="solar",
                source_version=2, holder="fixture-owner", fence=fence, job_id="equipment-move-stale")
    assert latest(backend) == 2
    head = head_graph(backend)
    assert head == expected and sha(head) == C4_MOVED_SHA
    # The persisted head projects again: the moved combiner sits at the point, its legs end there.
    state, binding = rb.state_from_graph(head)
    row = next(r for r in state["rows"]["device"] if binding["devices"][r["_pair"]]["id"] == CB1)
    assert tuple(rb.st.point_of(row["position"])) == (300.0, 200.0)
    a01 = rbt.by_handle(head)["A01"]["id"]
    handle = next(h for h, item in binding["strings"].items() if item["id"] == a01)
    legs = [r for r in state["rows"]["cable"] if r["cable_kind"] == "dc-homerun" and r["from"] == handle]
    assert len(legs) == 2 and all(tuple(rb.st.point_of(r["vertices"][-1])) == (300.0, 200.0) for r in legs)


@pytest.mark.parametrize("ref,point,code", [
    (L2_1, [50, 50], "NO_HOMERUNS_TO_MOVE"), (CB1, [100, 200], "EQUIPMENT_POSITION_UNCHANGED"),
    (app_id("inverter", 999), [1, 2], "MISSING_EQUIPMENT"),
])
def test_equipment_move_refusals_are_atomic_on_the_rail(graph, tmp_path, monkeypatch, ref, point, code):
    g = c4(graph)
    backend, _ = seed(tmp_path, monkeypatch, g)
    with held(backend) as fence:
        with pytest.raises(GraphValidationError) as error:
            dispatch(backend, fence, TOOL, dict(request(ref, point), operation="move-equipment"))
    assert error.value.code == code
    assert head_graph(backend) == g
    assert latest(backend) == 1


@pytest.fixture
def api_c4(isolated_jobs, no_network, graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, c4(graph))
    yield from _api(backend, tmp_path, monkeypatch)


def post(api, params):
    api[2][TOOL] = solar_tools.trusted_record(TOOL)
    return api[0].post("/api/run?wait=1", json=body(api, TOOL, params))


def test_equipment_move_run_rail_commits_one_job(api_c4):
    response = post(api_c4, dict(request(CB1, [300, 200]), operation="move-equipment"))
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True and env["execution_provenance"]["execution_mode"] == "local_graph_commit"
    assert env["result"]["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    assert store.load_manifest(api_c4[1], TENANT, "solar")["head"] == 2
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert len(records) == 1 and records[0]["status"] == "complete"
    assert sha(head_graph(api_c4[1])) == C4_MOVED_SHA


@pytest.mark.parametrize("patch", [{"point": DROP}, {"unknown": 1}, {"inverter_ref": ""}],
                         ids=["no-point", "unknown-key", "empty-ref"])
def test_equipment_move_broker_refuses_schema_violations(api_c4, patch):
    params = dict(request(CB1, [300, 200]), operation="move-equipment")
    for key, value in patch.items():
        if value is DROP:
            del params[key]
        else:
            params[key] = value
    response = post(api_c4, params)
    env = response.json()
    assert env.get("ok") is not True, response.text
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert all(record["status"] != "complete" for record in records)
    reasons = [env.get("reason_code"), (env.get("error") or {}).get("reason_code")]
    reasons += [(record.get("error") or {}).get("reason_code") for record in records]
    assert "tool_params_invalid" in reasons, response.text
    manifest = store.load_manifest(api_c4[1], TENANT, "solar")
    assert manifest["head"] == manifest["latest"] == 1


def test_equipment_move_builtin_refusal_reaches_the_rail(api_c4):
    response = post(api_c4, dict(request(L2_1, [50, 50]), operation="move-equipment"))
    assert response.status_code == 400, response.text
    env = response.json()
    assert env.get("ok") is not True
    assert env["error"]["message"] == env["reason_code"] == "NO_HOMERUNS_TO_MOVE"
    assert store.load_manifest(api_c4[1], TENANT, "solar")["head"] == 1
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert len(records) == 1 and records[0]["status"] == "failed"
