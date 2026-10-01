"""LEAFTRENCH on the design graph: one trench route between two picked points, and a re-route that stales its riders."""
import copy
from datetime import datetime, timezone
import hashlib
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import catalog
import deps
import entitlements
import product_capability_availability as availability
import solar_ground_dsteps as dsteps
import solar_inverter_outputs as router
import solar_local_graph
import solar_tools
from solar_design_graph import GraphValidationError, validate_graph
from solar_solve_results import require_current_export
from test_w1_design_graph import app_id, graph  # noqa: F401, fixture
from test_w1_local_graph_adapter import held
from test_w1_solve_commit import seed
from test_solar_w2_registration import dispatch, head_graph, latest
import test_solar_ground_route_kinds as rk

TOOL = "solar-trench-manual"
TENANT = "fixture-tenant"
W1_SHA = "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3"
COMPOSITE_SHA = "38457ba9fd78d22e740974b3f9eab253ebcc7b05ff2637f3f43f98c11b81967a"
STAMP = "2026-09-30T00:00:00+00:00"
NEW = app_id("route", 901)
SECOND = app_id("route", 902)
# Picks in DRAWING units (the W1 fixture is an inch drawing): -80 in to 320 in along y = 0.
ADD = {"expected_rev": 0, "start_x": -80, "start_y": 0, "end_x": 320, "end_y": 0}
# The same picks 40 inches higher.
HIGH = {"start_x": -80, "start_y": 40, "end_x": 320, "end_y": 40}
# Re-route the composite fixture's trench (0, 1) to (5, 1) m to the picks (0, 80) to (200, 80) in.
REROUTE = {"expected_rev": 0, "trench_ref": rk.TRENCH_ID, "start_x": 0, "start_y": 80, "end_x": 200, "end_y": 80}
DROP = object()


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


class FixedDatetime:
    @staticmethod
    def now(tz=None):
        return datetime(2026, 9, 30, tzinfo=timezone.utc)


def builtin():
    """The builtin as the rail loads it, with app_id("route", 901) onward as new ids and one fixed time."""
    module = solar_local_graph._load_builtin(TOOL)
    counter = [901]

    def fake_new_id(kind):
        value = app_id(kind, counter[0])
        counter[0] += 1
        return value

    module.new_id = fake_new_id
    module.datetime = FixedDatetime
    return module


def w1(graph):
    g = copy.deepcopy(graph)
    validate_graph(g)
    assert sha(g) == W1_SHA
    return g


def composite(graph, schedule_names_homerun=True):
    """The route-kinds composite: homerun 1 and feeder 2 ride trench 3 (0, 1) to (5, 1) m; the W1 schedule also
    names the homerun when asked, the way solar-schedule's schedules name their homerun routes."""
    g = rk.composite(w1(graph))
    if schedule_names_homerun:
        g["schedules"][0]["source_refs"].append(rk.HOMERUN_ID)
        validate_graph(g)
        assert sha(g) == COMPOSITE_SHA
    return g


def route(g, route_id):
    return next(item for item in g["routes"] if item["id"] == route_id)


def code(fn, *args):
    with pytest.raises(GraphValidationError) as error:
        fn(*args)
    return error.value.code


# --- add-trench --------------------------------------------------------------------------------------------------


def test_trench_manual_adds_one_trench_on_w1(graph):
    g = w1(graph)
    params = dict(ADD)
    result = builtin().add_trench(g, params)
    assert sha(g) == W1_SHA and params == ADD
    after = result["graph"]
    assert (result["trench_ref"], result["rider_refs"], result["schedule_refs"]) == (NEW, [], [])
    assert [item["id"] for item in after["routes"]] == [app_id("route", 1), NEW]
    trench = after["routes"][-1]
    assert trench["points"] == [
        [-2.0320000000000005, 0.0], [-1.0320000000000007, 0.0], [-0.03200000000000139, 0.0],
        [0.9679999999999993, 0.9999999999999992], [1.968, 2.0], [2.967999999999999, 2.0], [3.9679999999999986, 2.0],
        [4.967999999999999, 2.0], [5.968, 2.0], [6.967999999999998, 0.9999999999999992], [7.967999999999998, 0.0]]
    assert sha(trench["points"]) == "a114d95ed3c5bb441a6b405355d29d48e6b8b1b581fb83defab5fe949a280341"
    assert trench["length_ft"] == 38.24427247208786
    assert (trench["route_kind"], trench["from_ref"], trench["to_ref"], trench["wire_gauge"]) == ("trench", None, None, "")
    assert (trench["point_units"], trench["length_units"], trench["rev"]) == ("m", "ft", 1)
    assert trench["trench"] == {"depth_m": 1.0, "width_m": 0.6, "voltage_class": "MIXED"}
    assert "pathway_ref" not in trench
    assert trench["validity"] == {"state": "valid", "reasons": []}
    assert trench["extra"] == {"leaftrench": {
        "source": "derived", "rule": "leaftrench-two-point", "layer": "LEAF-PVCASE-TRENCH",
        "picked_points_du": [[-80, 0], [320, 0]], "segments": 10}}
    assert trench["provenance"]["created_at"] == STAMP
    assert trench["provenance"]["created_by"] == trench["provenance"]["last_writer"] == TOOL
    assert sha(trench) == "9c017263d3ea4e7aed7059c5b297f48f8eb9d64ee227de454057bbc87961676d"
    assert (after["rev"], after["parent_rev"]) == (1, 0)
    assert sha(after) == "7bc07446c3f381d94affa14c0ca0dea66bec8f97bf32615ff96b542ccb5cc7bd"
    for key in ("project", "settings", "electrical_zones", "frames", "panels", "strings", "inverters", "schedules"):
        assert after[key] == g[key], key
    assert after["routes"][0] == g["routes"][0]
    assert require_current_export(after) == after


def test_trench_manual_run_matches_add(graph):
    after = builtin().run(w1(graph), dict(ADD, operation="add-trench"))
    assert sha(after) == "7bc07446c3f381d94affa14c0ca0dea66bec8f97bf32615ff96b542ccb5cc7bd"


@pytest.mark.parametrize("units,picks,points_sha,length_ft", [
    ({"drawing_units": "in", "meters_per_unit": 0.0254, "drawing_unit_is_feet": False}, (-80, 0, 320, 0),
     "a114d95ed3c5bb441a6b405355d29d48e6b8b1b581fb83defab5fe949a280341", 38.24427247208786),
    ({"drawing_units": "m", "meters_per_unit": 1.0, "drawing_unit_is_feet": False},
     (-80 * 0.0254, 0, 320 * 0.0254, 0),
     "38b2ce7476aac16aeb3166546104004180046d7f83e611ae8629f393f8327f0b", 35.526335711109546),
    ({"drawing_units": "ft", "meters_per_unit": 0.3048, "drawing_unit_is_feet": True}, (-80 / 12, 0, 320 / 12, 0),
     "4fdf5ad5e9e99aa4eba0b3ba334744aeb6af9c1c6f69ed4c631eda92945a9729", 35.526335711109546),
], ids=["inches", "metres", "feet"])
def test_trench_manual_routes_in_drawing_units(graph, units, picks, points_sha, length_ft):
    """The same physical picks on one graph labelled in three units: the lattice runs in drawing units, so a
    midpoint on a frame edge falls on a different side in each and the route follows."""
    g = w1(graph)
    g["project"]["units"].update(units)
    start_x, start_y, end_x, end_y = picks
    result = builtin().add_trench(g, {"expected_rev": 0, "start_x": start_x, "start_y": start_y,
                                      "end_x": end_x, "end_y": end_y})
    trench = result["graph"]["routes"][-1]
    assert sha(trench["points"]) == points_sha
    assert trench["length_ft"] == length_ft
    assert len(trench["points"]) == 11


def test_trench_manual_metric_grid_needs_drawing_unit_options(graph):
    options = router.routing_options_for_units("in")
    assert (options["grid_step"], options["grid_padding"]) == (39.37007874015748, 196.8503937007874)
    raw = dsteps.trench_command((-80.0, 0.0), (320.0, 0.0), [])
    assert raw["succeeded"] is False
    assert raw["message"] == ("LEAFTRENCH: route failed - grid 411x11 exceeds MaxGridCellsPerAxis (200); "
                              "increase GridStepM or shrink the extent")
    outlines, segments = builtin().routing_inputs(w1(graph))
    assert outlines == [
        [(19.68503937007874, -39.37007874015748), (137.7952755905512, -39.37007874015748),
         (137.7952755905512, 39.37007874015748), (19.68503937007874, 39.37007874015748)],
        [(184.8503937007874, -12.000000000000002), (208.8503937007874, -12.000000000000002),
         (208.8503937007874, 12.000000000000002), (184.8503937007874, 12.000000000000002)]]
    assert segments == []


@pytest.mark.parametrize("angle,expected", [
    (0, [(9.0, 9.5), (11.0, 9.5), (11.0, 10.5), (9.0, 10.5)]),
    (45, [(8.939339828220179, 8.939339828220179), (11.060660171779821, 8.939339828220179),
          (11.060660171779821, 11.060660171779821), (8.939339828220179, 11.060660171779821)]),
    (90, [(9.5, 9.0), (10.5, 9.0), (10.5, 11.0), (9.5, 11.0)]),
], ids=["0-degrees", "45-degrees", "90-degrees"])
def test_trench_manual_obstacle_box_turns_with_the_panel(angle, expected):
    g = {
        "project": {"units": {"meters_per_unit": 1.0}},
        "frames": [{"id": "f1", "module_width_along_row": 2.0, "module_height_across_row": 1.0}],
        "panels": [{"frame_ref": "f1", "angle": angle, "centre": [10.0, 10.0]}],
        "inverters": [], "routes": [],
    }
    outlines, segments = builtin().routing_inputs(g)
    assert segments == []
    assert len(outlines) == 1 and len(outlines[0]) == len(expected)
    for actual, point in zip(outlines[0], expected):
        assert actual == pytest.approx(point, rel=0, abs=1e-12)


@pytest.mark.parametrize("cap,accepted", [(2, True), (1, False)], ids=["at-cap", "over-cap"])
def test_trench_manual_alignment_segment_cap(monkeypatch, cap, accepted):
    module = builtin()
    monkeypatch.setattr(module, "MAX_TRENCH_SEGMENTS", cap)
    g = {
        "project": {"units": {"meters_per_unit": 1.0}},
        "frames": [{"id": "f1", "module_width_along_row": 2.0, "module_height_across_row": 1.0}],
        "panels": [{"frame_ref": "f1", "angle": 0, "centre": [10.0, 10.0]}],
        "inverters": [],
        "routes": [{"id": "t1", "route_kind": "trench", "points": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]}],
    }
    if accepted:
        _, segments = module.routing_inputs(g)
        assert segments == [((0.0, 0.0), (1.0, 0.0), 0.6), ((1.0, 0.0), (1.0, 1.0), 0.6)]
    else:
        assert code(module.routing_inputs, g) == "TRENCH_GEOMETRY_OUT_OF_RANGE"


def test_trench_manual_router_replays_the_d4_plugin_receipt():
    """The router this tool uses reproduces the plugin's committed LEAFTRENCH on the metre Ground fixture."""
    receipt = json.loads((SERVER.parent / "docs" / "parity" / "receipts" / "trench-routing" / "ground-terrain-d4.json")
                         .read_text(encoding="utf-8"))
    plugin = receipt["comparison"]["plugin"]
    assert plugin["parameters"]["answers"] == ["20,20", "120,70"] and plugin["units"] == "m"
    expected = [[float(x), float(y)] for x, y in (vertex["value"] for vertex in plugin["after"]["rows"][0]["vertices"])]
    ok, path, error = router.route_path((20.0, 20.0), (120.0, 70.0), [], [], router.routing_options_for_units("m"))
    assert (ok, error) == (True, "")
    assert [list(path[0][0])] + [list(b) for _, b in path] == expected
    assert len(expected) == 101
    assert sha(expected) == "48a996622040bc4224519203471e6f4aa56f1b706e4b3f1ae682b768ffac83ca"


def test_trench_manual_second_trench_follows_the_first(graph):
    module = builtin()
    first = module.add_trench(w1(graph), dict(ADD))
    second = module.add_trench(first["graph"], dict(HIGH, expected_rev=1))
    trench = second["graph"]["routes"][-1]
    assert second["trench_ref"] == SECOND
    assert sha(trench["points"]) == "65bb7c258b6dae31fcbf46652090708f305638350dcb0bae0ffe44de0c33b2ca"
    assert trench["length_ft"] == 38.24427247208786
    assert sha(second["graph"]) == "3957523e44efa591e859349f3e3ae63d51589222f58b0ae2ee7f981a119ae0a8"
    alone = builtin().add_trench(w1(graph), dict(HIGH, expected_rev=0))["graph"]["routes"][-1]
    assert sha(alone["points"]) == "f446bfc0e596862abc19bf0ed502955480029f6e532abb5957b40c3aeb0ecbbc"
    assert alone["length_ft"] == 35.526335711109546


# --- reroute-trench ----------------------------------------------------------------------------------------------


def test_trench_manual_reroute_with_the_same_picks_is_refused(graph):
    module = builtin()
    added = module.add_trench(w1(graph), dict(ADD))["graph"]
    before = sha(added)
    assert code(module.reroute_trench, added, dict(ADD, expected_rev=1, trench_ref=NEW)) == "TRENCH_UNCHANGED"
    assert sha(added) == before


def test_trench_manual_reroute_without_riders(graph):
    module = builtin()
    added = module.add_trench(w1(graph), dict(ADD))["graph"]
    result = module.reroute_trench(added, dict(ADD, **HIGH, expected_rev=1, trench_ref=NEW))
    assert (result["trench_ref"], result["rider_refs"], result["schedule_refs"]) == (NEW, [], [])
    after = result["graph"]
    trench = route(after, NEW)
    assert sha(trench["points"]) == "f446bfc0e596862abc19bf0ed502955480029f6e532abb5957b40c3aeb0ecbbc"
    assert trench["length_ft"] == 35.526335711109546
    assert (trench["rev"], trench["validity"]) == (2, {"state": "valid", "reasons": []})
    assert trench["provenance"]["created_at"] == STAMP and trench["provenance"]["last_writer"] == TOOL
    assert trench["extra"]["leaftrench"]["picked_points_du"] == [[-80, 40], [320, 40]]
    assert [item["id"] for item in after["routes"]] == [app_id("route", 1), NEW]
    assert sha(after) == "d9828519213c5a8575a0c54e5ce1be8f3adbe5b68a7da1c63c0aa38564c9474c"


def test_trench_manual_reroute_stales_its_riders(graph):
    g = composite(graph)
    params = dict(REROUTE)
    result = builtin().reroute_trench(g, params)
    assert sha(g) == COMPOSITE_SHA and params == REROUTE
    assert result["rider_refs"] == [rk.HOMERUN_ID, rk.FEEDER_ID]
    assert result["schedule_refs"] == [app_id("schedule", 1)]
    after = result["graph"]
    trench = route(after, rk.TRENCH_ID)
    assert trench["points"] == [[0.0, 2.0], [1.0, 2.0], [1.9999999999999993, 2.0], [3.0, 2.0],
                                [4.000000000000001, 2.0], [5.0, 2.0]]
    assert trench["length_ft"] == 16.404199475065617
    assert (trench["from_ref"], trench["to_ref"]) == (None, None)
    assert trench["trench"] == route(g, rk.TRENCH_ID)["trench"]
    assert trench["provenance"]["created_at"] == route(g, rk.TRENCH_ID)["provenance"]["created_at"]
    states = {e["id"]: (e["validity"]["state"], e["validity"]["reasons"], e["rev"])
              for e in after["routes"] + after["schedules"] + after["strings"] + after["inverters"]}
    assert states == {
        rk.HOMERUN_ID: ("stale", ["trench_changed"], 1), rk.FEEDER_ID: ("stale", ["trench_changed"], 1),
        rk.TRENCH_ID: ("valid", [], 1), app_id("schedule", 1): ("stale", ["trench_changed"], 1),
        rk.S1: ("valid", [], 0), rk.S2: ("valid", [], 0), rk.L1_ID: ("valid", [], 0), rk.L2_ID: ("valid", [], 0)}
    assert [item.get("pathway_ref") for item in after["routes"]] == [rk.TRENCH_ID, rk.TRENCH_ID, None]
    for rider in (rk.HOMERUN_ID, rk.FEEDER_ID):
        assert route(after, rider)["points"] == route(g, rider)["points"]
    assert sha(after) == "9e64f667a5edae01616ce2836554d80351fc864cf594b70bf1a02e44afa3d123"
    assert code(require_current_export, after) == "SOLAR_OUTPUT_NOT_CURRENT"


def test_trench_manual_schedule_without_rider_sources_stays_valid(graph):
    result = builtin().reroute_trench(composite(graph, schedule_names_homerun=False), dict(REROUTE))
    assert result["rider_refs"] == [rk.HOMERUN_ID, rk.FEEDER_ID] and result["schedule_refs"] == []
    assert result["graph"]["schedules"][0]["validity"] == {"state": "valid", "reasons": []}
    assert sha(result["graph"]) == "4b610fdcf8a828a016878cb275b2991a14df78cc2a6dcf78046db7d27d808ae4"


def test_trench_manual_already_stale_rider_keeps_its_first_cause(graph):
    g = composite(graph)
    route(g, rk.FEEDER_ID)["validity"] = {"state": "stale", "reasons": ["upstream_corrected"]}
    result = builtin().reroute_trench(g, dict(REROUTE))
    assert result["rider_refs"] == [rk.HOMERUN_ID, rk.FEEDER_ID]
    assert route(result["graph"], rk.FEEDER_ID)["validity"] == {"state": "stale", "reasons": ["upstream_corrected"]}
    assert route(result["graph"], rk.HOMERUN_ID)["validity"] == {"state": "stale", "reasons": ["trench_changed"]}


# --- refusals ----------------------------------------------------------------------------------------------------


def _w1(graph):
    return w1(graph)


def _string_on_trench(graph):
    return rk.string_on_trench(w1(graph))


def _unresolved(graph):
    g = w1(graph)
    g["project"]["units"]["meters_per_unit"] = 0.03
    return g


def _without(key):
    return {k: v for k, v in ADD.items() if k != key}


REFUSALS = [
    ("string-rider", _string_on_trench, "reroute_trench", dict(REROUTE), "TRENCH_STRING_RIDERS_UNSUPPORTED"),
    ("not-found", composite, "reroute_trench", dict(REROUTE, trench_ref=app_id("route", 77)), "TRENCH_NOT_FOUND"),
    ("not-a-trench", composite, "reroute_trench", dict(REROUTE, trench_ref=rk.HOMERUN_ID), "TRENCH_NOT_FOUND"),
    ("identical-picks", _w1, "add_trench", dict(ADD, end_x=-80), "TRENCH_ENDPOINTS_IDENTICAL"),
    ("past-the-lattice-cap", _w1, "add_trench", dict(ADD, end_x=8000), "TRENCH_ROUTE_FAILED"),
    ("stale-revision", _w1, "add_trench", dict(ADD, expected_rev=1), "STALE_GRAPH_REVISION"),
    ("unresolved-units", _unresolved, "add_trench", dict(ADD), "UNRESOLVED_UNITS"),
    ("boolean-coordinate", _w1, "add_trench", dict(ADD, start_x=True), "INVALID_TRENCH_MANUAL_REQUEST"),
    ("text-coordinate", _w1, "add_trench", dict(ADD, start_x="1"), "INVALID_TRENCH_MANUAL_REQUEST"),
    ("coordinate-over-bound", _w1, "add_trench", dict(ADD, start_x=1e12 + 1), "INVALID_TRENCH_MANUAL_REQUEST"),
    ("missing-key", _w1, "add_trench", _without("end_y"), "INVALID_TRENCH_MANUAL_REQUEST"),
    ("extra-key", _w1, "add_trench", dict(ADD, trench_ref=NEW), "INVALID_TRENCH_MANUAL_REQUEST"),
    ("empty-ref", composite, "reroute_trench", dict(REROUTE, trench_ref=""), "INVALID_TRENCH_MANUAL_REQUEST"),
    ("long-ref", composite, "reroute_trench", dict(REROUTE, trench_ref="x" * 129), "INVALID_TRENCH_MANUAL_REQUEST"),
]


@pytest.mark.parametrize("name,make,operation,params,expected", REFUSALS, ids=[row[0] for row in REFUSALS])
def test_trench_manual_refusals_are_atomic(graph, name, make, operation, params, expected):
    g = make(graph)
    before, request = sha(g), copy.deepcopy(params)
    assert code(getattr(builtin(), operation), g, params) == expected
    assert sha(g) == before and params == request


@pytest.mark.parametrize("exception_class", [OverflowError, ArithmeticError, ValueError, router.InverterOutputError],
                         ids=["overflow", "arithmetic", "value", "inverter-output"])
def test_trench_manual_router_exceptions_are_translated(graph, monkeypatch, exception_class):
    module = builtin()

    def raiser(*args, **kwargs):
        raise exception_class("router failure")

    monkeypatch.setattr(module.router, "route_path", raiser)
    g = _w1(graph)
    params = dict(ADD)
    before, request = sha(g), copy.deepcopy(params)
    assert code(module.add_trench, g, params) == "TRENCH_GEOMETRY_OUT_OF_RANGE"
    assert sha(g) == before and params == request


RUN_REFUSALS = [
    ("unknown-operation", dict(ADD, operation="delete-trench"), "INVALID_TRENCH_MANUAL_REQUEST"),
    ("no-operation", dict(ADD), "INVALID_TRENCH_MANUAL_REQUEST"),
    ("operation-not-text", dict(ADD, operation=1), "INVALID_TRENCH_MANUAL_REQUEST"),
    ("params-not-object", [], "INVALID_TRENCH_MANUAL_REQUEST"),
    ("non-finite", dict(ADD, operation="add-trench", start_x=float("nan")), "NONFINITE_NUMBER"),
]


@pytest.mark.parametrize("name,params,expected", RUN_REFUSALS, ids=[row[0] for row in RUN_REFUSALS])
def test_trench_manual_run_refuses_bad_params(graph, name, params, expected):
    g = w1(graph)
    assert code(builtin().run, g, params) == expected
    assert sha(g) == W1_SHA


# --- registry, schema, readiness and the rail --------------------------------------------------------------------


def test_trench_manual_registry_and_catalog(monkeypatch):
    declaration = solar_tools.get(TOOL)
    on_disk = json.loads((SERVER / "solar_tools" / "solar_trench_manual.json").read_text(encoding="utf-8"))
    assert declaration == on_disk
    assert (declaration["family"], declaration["adapter"], declaration["entitlement"]) == (
        "routing", "local-graph-commit", "run_write")
    assert (declaration["wave"], declaration["order"], declaration["scenario"]) == (3, 50, "w3-ground-routing")
    assert declaration["readiness"] == {"kind": "facets", "facets": []}
    assert declaration["ledger"] == ["trench-routing"] and declaration["trusted_inputs"] == []
    assert declaration["invalid_request_code"] == "INVALID_TRENCH_MANUAL_REQUEST"
    assert TOOL in solar_tools.local_graph_tools() and TOOL not in solar_tools.local_graph_read_tools()
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
    assert family["family_id"] == "routing"
    assert row["solar"] == solar_tools.catalog_view(declaration["record"])
    assert row["params_schema"] == declaration["record"]["params"]


def test_trench_manual_params_schema():
    validator = Draft7Validator(solar_tools.trusted_record(TOOL)["params"])
    valid = dict(ADD, operation="add-trench")
    assert validator.is_valid(valid)
    assert validator.is_valid(dict(REROUTE, operation="reroute-trench", drawing_id="solar"))
    assert validator.is_valid(dict(valid, start_x=-1e12, end_x=1e12, start_y=0.5))
    for key in ("operation", "expected_rev", "start_x", "start_y", "end_x", "end_y"):
        missing = dict(valid)
        del missing[key]
        assert not validator.is_valid(missing), key
    for patch in [{"unknown": 1}, {"operation": "delete-trench"}, {"expected_rev": -1},
                  {"expected_rev": 2147483648}, {"start_x": True}, {"start_x": "1"}, {"end_y": 1e12 + 1},
                  {"trench_ref": ""}, {"trench_ref": "x" * 129}, {"drawing_id": "d" * 129}]:
        assert not validator.is_valid(dict(valid, **patch)), patch


def test_trench_manual_readiness(graph):
    assert availability.w1_local_commit_inputs(w1(graph))[TOOL] == {"input_ready": True, "input_reason": None}
    assert availability.w1_local_commit_inputs(_unresolved(graph))[TOOL] == {
        "input_ready": False, "input_reason": "unresolved_units"}


def test_trench_manual_dispatch_publishes_and_refuses_stale(graph, tmp_path, monkeypatch):
    module = builtin()
    expected = module.add_trench(w1(graph), dict(ADD))["graph"]
    builtin()
    backend, _ = seed(tmp_path, monkeypatch, w1(graph))
    params = dict(ADD, operation="add-trench")
    with held(backend) as fence:
        receipt = dispatch(backend, fence, TOOL, params)
        assert receipt["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
        assert (receipt["before_rev"], receipt["after_rev"], receipt["replayed"]) == (0, 1, False)
        with pytest.raises(GraphValidationError, match="STALE_GRAPH_REVISION"):
            solar_local_graph.run_local_graph_commit(
                backend, TENANT, TOOL, dict(params, drawing_id="solar"), drawing_id="solar",
                source_version=2, holder="fixture-owner", fence=fence, job_id="trench-manual-stale")
    assert latest(backend) == 2
    head = head_graph(backend)
    assert head == expected
    assert sha(head) == "7bc07446c3f381d94affa14c0ca0dea66bec8f97bf32615ff96b542ccb5cc7bd"
