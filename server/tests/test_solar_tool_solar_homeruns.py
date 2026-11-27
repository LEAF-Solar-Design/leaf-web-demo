"""Terminal leads on the intake graph, committed through the Studio job rail."""
import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import deps
import jobs
import product_capability_availability as availability
import solar_local_graph
import solar_tools
import store
import write_loop
from solar_design_graph import GraphValidationError
from solar_graph_context import resolve_graph_context
from solar_joint_homerun_routing import route_together
from solar_sizing_client import digest
from test_w1_design_graph import app_id, graph  # noqa: F401
from test_w1_equipment import case, equipment, licensed  # noqa: F401
from test_w1_graph_versions import drawing, commit, request_for, TENANT as BUNDLE_TENANT, DRAWING  # noqa: F401
from test_w1_local_graph_adapter import seed, held
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_local_graph_rail import api, _api, body  # noqa: F401

TOOL = "solar-homeruns"
TENANT = "fixture-tenant"
S1 = "leaf:string:00000000-0000-4000-8000-000000000001"
S2 = "leaf:string:00000000-0000-4000-8000-000000000002"
ROUTE_IDS = [
    "leaf:route:00000000-0000-4000-8000-000000000001",
    "leaf:route:912d9168-74b2-4997-a6e4-1f11adb24709",
    "leaf:route:c80159af-4565-403d-a8af-7388f19009cc",
    "leaf:route:3539b776-d49d-47aa-b6be-61109c1c9d7f",
]
NEW_START_ID = "leaf:route:1a9024d1-3e5a-4c6f-bb2f-7ef7bd93640c"


@pytest.fixture
def assigned(case):
    source, params, intake = case
    return equipment.assign_equipment(
        source, params, drawing_intake=intake, licensed_equipment=licensed)["graph"]


@pytest.fixture
def assigned_api(isolated_jobs, no_network, assigned, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, assigned)
    yield from _api(backend, tmp_path, monkeypatch)


def builtin():
    return solar_local_graph._load_builtin(TOOL)


def dispatch(backend, fence, params, *, version=1, job_id="homeruns-job"):
    return solar_local_graph.run_local_graph_commit(
        backend, TENANT, TOOL, params, drawing_id="solar", source_version=version,
        holder="fixture-owner", fence=fence, job_id=job_id)


def assert_leads(output, ids=ROUTE_IDS):
    assert (output["rev"], output["parent_rev"]) == (2, 1)
    assert [route["id"] for route in output["routes"]] == ids
    expected = [
        (S1, "start homerun", 1, 0, 4 / .3048),
        (S1, "end homerun", 2, 0, 3 / .3048),
        (S2, "start homerun", 3, 1, 2 / .3048),
        (S2, "end homerun", 3, 1, 2 / .3048),
    ]
    for route, (string, kind, panel, slot, length) in zip(output["routes"], expected):
        assert route["from_ref"] == string
        assert route["route_kind"] == kind
        assert route["to_ref"] == app_id("inverter", 1)
        assert route["points"] == [[float(panel), 0.0, 0.0], [5.0, 0.0, 0.0]]
        assert route["length_ft"] == length
        assert route["wire_gauge"] == "10 AWG"
        assert route["point_units"] == "m" and route["length_units"] == "ft"
        assert route["extra"] == {
            "terminal_panel_ref": app_id("panel", panel), "input_number": slot,
            "mppt_letter": "A", "layer": "Homeruns",
            "routing_policy": "direct-terminal-lead-v1",
        }
        assert route["rev"] == 2
        assert route["validity"] == {"state": "valid", "reasons": []}
        provenance = route["provenance"]
        assert provenance["created_by"] == provenance["last_writer"] == provenance["tool_id"] == TOOL
        assert provenance["source_rev"] == 1
        assert provenance["created_at"] == "2026-09-17T00:00:00Z"
    assert len(output["schedules"]) == 1
    schedule = output["schedules"][0]
    assert schedule["validity"] == {"state": "stale", "reasons": ["ROUTES_CHANGED"]}
    assert schedule["rev"] == 2
    assert schedule["provenance"]["last_writer"] == TOOL
    assert schedule["provenance"]["source_rev"] == 1
    assert availability.w1_graph_readiness(output)["solar-schedule"] == {
        "input_ready": True, "input_reason": None}


def test_declaration_pins_the_local_graph_commit_adapter():
    declaration = solar_tools.get(TOOL)
    assert declaration["adapter"] == "local-graph-commit"
    assert declaration["invalid_request_code"] == "INVALID_ROUTING_REQUEST"
    assert declaration["engine"] == "server-builtin"
    assert declaration["readiness"] == {"kind": "w1-chain"}
    assert declaration["family"] == "stringing"
    assert declaration["ledger"] == ["homeruns"]
    assert "solar-homeruns" in solar_local_graph.LOCAL_GRAPH_TOOLS
    assert availability.capability_adapter(TOOL) == "local-graph-commit"
    assert availability.is_local_graph_commit({"name": TOOL}) is True
    assert availability.is_cloud_proposal({"name": TOOL}) is False
    module = builtin()
    assert Path(module.__file__).resolve() == (SERVER / "builtins/solar_homeruns.py").resolve()
    assert callable(module.run)
    record = solar_tools.trusted_record(TOOL)
    assert deps.catalog_tool_digest(record) == (
        "sha256:715ba5c397fa342d7b0b8a3be7115126741aa70f88d4bf0f966ae10612f8ce8d")
    view = solar_tools.catalog_view(record)
    assert view["adapter"] == "local-graph-commit"
    assert view["engine"] == "server-builtin"


def test_run_routes_both_leads_of_every_string_to_its_assigned_input(assigned):
    before = copy.deepcopy(assigned)
    output = builtin().run(assigned, {"expected_rev": 1})
    assert assigned == before
    assert_leads(output)


def test_run_is_deterministic_and_reuses_prior_lead_ids(assigned):
    before = copy.deepcopy(assigned)
    first = builtin().run(copy.deepcopy(assigned), {"expected_rev": 1})
    second = builtin().run(copy.deepcopy(assigned), {"expected_rev": 1})
    assert first == second and digest(first) == digest(second)
    assert_leads(first)
    assert first["routes"][0]["id"] == assigned["routes"][0]["id"]
    assert first["routes"][0]["provenance"]["created_at"] == assigned["routes"][0]["provenance"]["created_at"]
    without_routes = copy.deepcopy(assigned)
    without_routes["routes"] = []
    output = builtin().run(without_routes, {"expected_rev": 1})
    assert_leads(output, [NEW_START_ID, *ROUTE_IDS[1:]])
    assert without_routes["routes"] == []
    assert assigned == before


def test_run_keeps_a_prior_lead_timestamp_distinct_from_its_string(assigned):
    source = copy.deepcopy(assigned)
    source["routes"][0]["provenance"]["created_at"] = "2026-09-18T00:00:00Z"
    assert source["strings"][0]["provenance"]["created_at"] == "2026-09-17T00:00:00Z"
    output = builtin().run(source, {"expected_rev": 1})
    assert output["routes"][0]["id"] == source["routes"][0]["id"]
    assert output["routes"][0]["provenance"]["created_at"] == "2026-09-18T00:00:00Z"


@pytest.mark.parametrize("case_id,reason", [
    ("C12", "routing_topology_required"),
    ("C14", "degenerate_route"),
], ids=["C12", "C14"])
def test_readiness_refuses_what_run_refuses(assigned, case_id, reason):
    assert availability.w1_graph_readiness(copy.deepcopy(assigned))["solar-homeruns"] == {
        "input_ready": True, "input_reason": None}
    source = copy.deepcopy(assigned)
    if case_id == "C12":
        source["strings"][0]["wire_gauge"] = ""
    else:
        source["inverters"][0]["position"] = [1.0, 0.0, 0.0]
    readiness = availability.w1_graph_readiness(copy.deepcopy(source))
    assert readiness["solar-homeruns"] == {
        "input_ready": False, "input_reason": reason}
    assert readiness["solar-schedule"] == {
        "input_ready": False, "input_reason": "complete_routing_required"}
    with pytest.raises(GraphValidationError) as error:
        builtin().run(source, {"expected_rev": 1})
    assert error.value.code == reason.upper()


def test_readiness_routes_once_per_call(assigned, monkeypatch):
    import solar_wiring_client

    routed = builtin().run(copy.deepcopy(assigned), {"expected_rev": 1})
    real_local_routes = solar_wiring_client.local_routes
    calls = 0

    def counted_local_routes(graph):
        nonlocal calls
        calls += 1
        return real_local_routes(graph)

    monkeypatch.setattr(solar_wiring_client, "local_routes", counted_local_routes)
    availability.w1_graph_readiness(copy.deepcopy(assigned))
    assert calls == 1
    calls = 0
    readiness = availability.w1_graph_readiness(routed)
    assert calls == 1
    assert readiness["solar-schedule"] == {"input_ready": True, "input_reason": None}


def test_run_cancel_returns_the_checked_graph_unchanged(assigned):
    before = copy.deepcopy(assigned)
    output = builtin().run(assigned, {"expected_rev": 1, "cancel": True})
    assert output == assigned == before
    assert output is not assigned
    assert output["rev"] == 1


@pytest.mark.parametrize("case_id,code", [
    ("C4", "INVALID_ROUTING_REQUEST"),
    ("C5", "INVALID_ROUTING_REQUEST"),
    ("C6", "INVALID_ROUTING_REQUEST"),
    ("C7", "INVALID_ROUTING_REQUEST"),
    ("C8", "STALE_GRAPH_REVISION"),
    ("C9", "STALE_GRAPH_REVISION"),
    ("C10", "STALE_GRAPH_REVISION"),
    ("C11", "UNRESOLVED_UNITS"),
    ("C12", "ROUTING_TOPOLOGY_REQUIRED"),
    ("C13", "EQUIPMENT_ASSIGNMENT_REQUIRED"),
    ("C14", "DEGENERATE_ROUTE"),
    ("C15", "EQUIPMENT_ASSIGNMENT_REQUIRED"),
    ("C16", "PERSISTED_GRAPH_UNAVAILABLE"),
    ("C17", "EQUIPMENT_ASSIGNMENT_REQUIRED"),
])
def test_run_refusals_are_named(assigned, case_id, code):
    source = copy.deepcopy(assigned)
    params = {"expected_rev": 1}
    if case_id == "C4":
        params = []
    elif case_id == "C5":
        params["route"] = 1
    elif case_id == "C6":
        params["cancel"] = 1
    elif case_id == "C7":
        params["drawing_id"] = "solar"
    elif case_id == "C8":
        params["expected_rev"] = 0
    elif case_id == "C9":
        params = {}
    elif case_id == "C10":
        params["expected_rev"] = True
    elif case_id == "C11":
        source["project"]["units"]["meters_per_unit"] = 1
    elif case_id == "C12":
        source["strings"][0]["wire_gauge"] = ""
    elif case_id == "C13":
        source["strings"][0]["validity"]["state"] = "stale"
    elif case_id == "C14":
        source["inverters"][0]["position"] = [1.0, 0.0, 0.0]
    elif case_id == "C15":
        del source["extra"]["equipment"]
    elif case_id == "C16":
        source["extra"]["equipment"] = None
    else:
        source = graph.__wrapped__()
        params = {"expected_rev": 0}
    before = copy.deepcopy(source)
    original_params = copy.deepcopy(params)
    with pytest.raises(GraphValidationError) as error:
        builtin().run(source, params)
    assert error.value.code == code
    assert source == before
    assert params == original_params


def test_run_never_reaches_the_licensed_boundary(assigned, monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("local homeruns must not call licensed_preview")

    monkeypatch.setattr(builtin(), "licensed_preview", refuse)
    assert_leads(builtin().run(assigned, {"expected_rev": 1}))


def test_studio_path_commits_replays_and_proves(assigned, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, assigned)
    params = {"drawing_id": "solar", "expected_rev": 1}
    with held(backend) as fence:
        receipt = dispatch(backend, fence, params)
        assert receipt["schema_version"] == "leaf.solar-graph-commit.v1"
        assert receipt["adapter"] == "local-graph-commit"
        assert receipt["tool"] == TOOL
        assert receipt["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
        assert receipt["before_rev"] == 1 and receipt["after_rev"] == 2
        assert receipt["drawing_changed"] is True and receipt["replayed"] is False
        assert receipt["request_sha256"] == solar_local_graph.request_digest(
            TOOL, "solar", 1, {"expected_rev": 1})
        replay = dispatch(backend, fence, params)
        assert replay["replayed"] is True
        assert replay["new_version"] == receipt["new_version"]
        assert replay["graph_sha256"] == receipt["graph_sha256"]
        assert store.load_manifest(backend, TENANT, "solar")["head"] == 2
        proof = solar_local_graph.graph_commit_provenance(
            receipt, params, TENANT, "homeruns-job", TOOL, 1, backend=backend)
        assert proof == {
            "adapter": "local-graph-commit", "execution_mode": "local_graph_commit",
            "graph_sha256": receipt["graph_sha256"], "intake_sha256": receipt["intake_sha256"],
            "request_sha256": receipt["request_sha256"], "new_version": 2, "source_version": 1,
        }
        for field, value in (("after_rev", 3), ("graph_sha256", "0" * 64),
                             ("tool", "solar-settings"), ("before_rev", 0)):
            tampered = dict(receipt, **{field: value})
            with pytest.raises(ValueError, match="^graph commit terminal proof rejected$"):
                solar_local_graph.graph_commit_provenance(
                    tampered, params, TENANT, "homeruns-job", TOOL, 1, backend=backend)
        stored = resolve_graph_context(backend, TENANT, "solar")["graph"]
        assert_leads(stored)
        next_receipt = dispatch(backend, fence, {"drawing_id": "solar", "expected_rev": 2},
                                version=2, job_id="homeruns-next-job")
        assert next_receipt["new_version"] == {"drawing_id": "solar", "version": 3, "parent": 2}
        assert store.load_manifest(backend, TENANT, "solar")["head"] == 3
        rerouted = resolve_graph_context(backend, TENANT, "solar")["graph"]
        assert [route["id"] for route in rerouted["routes"]] == ROUTE_IDS


def test_dispatcher_refusals_are_named(assigned, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, assigned)
    with held(backend) as fence:
        dispatch(backend, fence, {"drawing_id": "solar", "expected_rev": 1})
        for params, code in (
            ([], "INVALID_ROUTING_REQUEST"),
            ({"drawing_id": "solar", "expected_rev": 1}, "STALE_GRAPH_REVISION"),
            ({"drawing_id": "solar", "expected_rev": 2, "cancel": True}, "GRAPH_COMMIT_CANCELLED"),
        ):
            with pytest.raises(GraphValidationError) as error:
                dispatch(backend, fence, params, version=2, job_id="homeruns-refused-job")
            assert error.value.code == code
            assert store.load_manifest(backend, TENANT, "solar")["head"] == 2


def test_dwg_bundle_drawing_refuses_before_the_builtin(drawing, graph, monkeypatch):
    backend, fence = drawing
    commit(drawing, request_for(backend, graph))

    def refuse(*args, **kwargs):
        pytest.fail("DWG bundle must refuse before loading the builtin")

    monkeypatch.setattr(solar_local_graph, "_load_builtin", refuse)
    with pytest.raises(GraphValidationError) as error:
        solar_local_graph.run_local_graph_commit(
            backend, BUNDLE_TENANT, TOOL, {"drawing_id": DRAWING, "expected_rev": 0},
            drawing_id=DRAWING, source_version=2, holder="writer", fence=fence,
            job_id="dwg-homeruns-job")
    assert error.value.code == "LICENSED_GRAPH_COMMIT_REQUIRED"
    assert store.load_manifest(backend, BUNDLE_TENANT, DRAWING)["head"] == 2


def test_api_run_commits_homeruns_through_the_rail(assigned_api):
    response = assigned_api[0].post(
        "/api/run?wait=1", json=body(assigned_api, TOOL, {"expected_rev": 1}))
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True
    receipt = env["result"]
    assert receipt["tool"] == TOOL
    assert receipt["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    job = jobs.get_job(receipt["job_id"])
    assert job["status"] == "complete"
    assert job["provenance"]["execution_path"] == "local"
    assert job["provenance"] == env["execution_provenance"]
    assert store.load_manifest(assigned_api[1], TENANT, "solar")["head"] == 2
    assert_leads(resolve_graph_context(assigned_api[1], TENANT, "solar")["graph"])


def route_constraints():
    return {
        "keep_outs": [{"name": "Equipment pad", "min_xy": [3.25, 0], "max_xy": [4.25, .25]}],
        "aisles": [
            {"name": "North aisle", "points": [[3, 1, 0], [4.5, 1, 0]], "lead_capacity": 2},
            {"name": "South aisle", "points": [[3, -2, 0], [4.5, -2, 0]], "lead_capacity": 2},
        ],
    }


@pytest.mark.parametrize("count,aisle_leads,capacity,refusal", [
    (132, 0, 0, None),
    (132, 2, 2, None),
    (132, 64, 64, None),
    (132, 2, 1, "JOINT_ROUTING_INFEASIBLE"),
    (132, 65, 64, "JOINT_ROUTING_LEAD_LIMIT"),
    (513, 0, 0, "JOINT_ROUTING_LEAD_LIMIT"),
], ids=["clear-roof", "mixed-roof", "full-aisle", "capacity-refusal",
       "solver-limit", "drawing-limit"])
def test_large_homerun_batches_allocate_only_leads_that_need_aisles(count, aisle_leads, capacity, refusal):
    constraints = route_constraints()
    constraints["aisles"] = constraints["aisles"][:1]
    constraints["aisles"][0]["lead_capacity"] = capacity
    # Alternate the group order in the batch. Clear leads cross x=3.25 above
    # both the pad and the aisle, so they should consume no shared capacity.
    needs_aisle = {index * 2 for index in range(aisle_leads)}
    routes = [{
        "from_ref": app_id("string", index + 1), "route_kind": "start homerun",
        "points": [[3, 0 if index in needs_aisle else 3, 0], [5, 0, 0]],
        "extra": {},
    } for index in range(count)]
    original = copy.deepcopy(routes)
    if refusal:
        with pytest.raises(GraphValidationError) as error:
            route_together(routes, constraints)
        assert error.value.code == refusal
        assert routes == original
        if refusal == "JOINT_ROUTING_INFEASIBLE":
            assert error.value.path == f"{routes[2]['from_ref']}/start homerun"
        return

    assert route_together(routes, constraints) is routes
    for index, route in enumerate(routes):
        info = route["extra"]["routing"]
        if index in needs_aisle:
            assert info == {"aisle": "North aisle", "direct_keep_outs": ["Equipment pad"],
                            "aisle_leads": aisle_leads, "aisle_capacity": capacity}
            assert route["points"] == [[3, 0, 0], [3, 1, 0], [4.5, 1, 0], [5, 0, 0]]
        else:
            assert info == {"aisle": None, "direct_keep_outs": []}
            assert route["points"] == original[index]["points"]


@pytest.mark.parametrize("variant", ["shared", "reversed", "direct"])
def test_api_joint_routing_commits_complete_capacity_allocation(assigned_api, variant):
    constraints = route_constraints()
    if variant == "reversed":
        constraints["aisles"].reverse()
        for aisle in constraints["aisles"]:
            aisle["points"].reverse()
    elif variant == "direct":
        constraints["keep_outs"][0]["min_xy"][1] = .125
    params = {"expected_rev": 1, "routing_constraints": constraints}
    response = assigned_api[0].post("/api/run?wait=1", json=body(assigned_api, TOOL, params))
    assert response.status_code == 200, response.text
    assert response.json()["result"]["new_version"]["version"] == 2
    stored = resolve_graph_context(assigned_api[1], TENANT, "solar")["graph"]
    routes = stored["routes"]
    assert [route["id"] for route in routes] == ROUTE_IDS
    assert all(route["wire_gauge"] == "10 AWG" for route in routes)
    assert [route["extra"]["routing_policy"] for route in routes] == ["shared-aisle-terminal-lead-v1"] * 4
    if variant == "direct":
        assert all(len(route["points"]) == 2 for route in routes)
        assert all(route["extra"]["routing"] == {"aisle": None, "direct_keep_outs": []}
                   for route in routes)
        return

    # Both aisles are feasible for every lead. The scarce shorter aisle saves
    # more for the two x=3 leads than for the x=1 and x=2 leads seen first.
    assert [route["extra"]["routing"]["aisle"] for route in routes] == [
        "South aisle", "South aisle", "North aisle", "North aisle"]
    for index, route in enumerate(routes):
        info = route["extra"]["routing"]
        assert info["direct_keep_outs"] == ["Equipment pad"]
        assert info["aisle_leads"] == info["aisle_capacity"] == 2
        y = -2 if index < 2 else 1
        assert route["points"] == [[min(index + 1, 3), 0, 0], [3, y, 0], [4.5, y, 0], [5, 0, 0]]
        if index < 2:
            assert info["shorter_aisle_at_capacity"]["name"] == "North aisle"
            assert info["shorter_aisle_at_capacity"]["length_m"] < route["length_ft"] * .3048
        else:
            assert "shorter_aisle_at_capacity" not in info


@pytest.mark.parametrize("centerline,overlaps_direct", [
    ([[3, 0, 0], [4, 0, 0]], True),
    ([[3, 0, 0], [3, 1, 0], [4.5, 1, 0]], False),
    ([[3, 0, 0], [4.25, 0, 0], [5, 0, 0]], True),
], ids=["collinear", "bend", "at-inverter"])
def test_api_joint_routing_at_aisle_endpoint(assigned_api, centerline, overlaps_direct):
    constraints = {
        "keep_outs": [] if overlaps_direct else route_constraints()["keep_outs"],
        "aisles": [{"name": "North aisle", "points": centerline, "lead_capacity": 4}],
    }
    params = {"expected_rev": 1, "routing_constraints": constraints}
    response = assigned_api[0].post("/api/run?wait=1", json=body(assigned_api, TOOL, params))
    assert response.status_code == 200, response.text
    routes = resolve_graph_context(assigned_api[1], TENANT, "solar")["graph"]["routes"]
    assert [route["id"] for route in routes] == ROUTE_IDS
    inverter = [5, 0, 0]
    tail = centerline if centerline[-1] == inverter else [*centerline, inverter]
    assert [route["points"] for route in routes] == [
        [[1, 0, 0], *tail],
        [[2, 0, 0], *tail],
        tail,
        tail,
    ]
    assert all(route["extra"]["routing"] == {
        "aisle": "North aisle",
        "direct_keep_outs": [] if overlaps_direct else ["Equipment pad"],
        "aisle_leads": 4,
        "aisle_capacity": 4,
    } for route in routes)


def _failed_routing_run(assigned_api, params, wait, reason):
    client = assigned_api[0]
    response = client.post(
        "/api/run", params={"wait": int(wait)}, json=body(assigned_api, TOOL, params),
    )
    if wait:
        assert response.status_code == 400, response.text
        assert response.json()["reason_code"] == reason, response.text
    else:
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "failed", response.text
    error = response.json()["error"]
    if not wait or reason == "JOINT_ROUTING_INFEASIBLE":
        assert error["reason_code"] == reason
    return error


@pytest.mark.parametrize("wait", [True, False], ids=["wait", "saved-job"])
@pytest.mark.parametrize("variant,reason", [
    ("capacity", "JOINT_ROUTING_INFEASIBLE"),
    ("overlapping_alias", "INVALID_ROUTING_CONSTRAINTS"),
    ("direct_overlap_at_capacity", "JOINT_ROUTING_INFEASIBLE"),
])
def test_api_joint_routing_refusal_preserves_saved_version(assigned_api, variant, reason, wait):
    constraints = route_constraints()
    if variant == "capacity":
        constraints["aisles"][0]["lead_capacity"] = 1
    elif variant == "direct_overlap_at_capacity":
        constraints = {
            "keep_outs": [],
            "aisles": [{"name": "Shared aisle", "points": [[3, 0, 0], [4, 0, 0]],
                        "lead_capacity": 3}],
        }
    else:
        constraints["aisles"][1]["points"] = [[3, 1, 0], [4, 1, 0]]
    params = {"expected_rev": 1, "routing_constraints": constraints}
    original = resolve_graph_context(assigned_api[1], TENANT, "solar")["graph"]
    public_error = _failed_routing_run(assigned_api, params, wait, reason)
    assert store.load_manifest(assigned_api[1], TENANT, "solar")["head"] == 1
    assert resolve_graph_context(assigned_api[1], TENANT, "solar")["graph"] == original
    with pytest.raises(GraphValidationError) as error:
        builtin().run(original, params)
    assert error.value.code == reason
    if variant in {"capacity", "direct_overlap_at_capacity"}:
        string_ref, kind = error.value.path.rsplit("/", 1)
        assert string_ref in {S1, S2} and kind in {"start homerun", "end homerun"}
        assert public_error["unassigned_leads"] == [{"string_ref": string_ref, "route_kind": kind}]
        assert f"{string_ref}/{kind}" in public_error["message"]
    else:
        assert "unassigned_leads" not in public_error


@pytest.mark.parametrize("wait", [True, False], ids=["wait", "saved-job"])
@pytest.mark.parametrize("centerline", [
    [[3, 1, 0], [4.5, 1, 0]],
    [[3, -1, 0], [4.5, -1, 0]],
    [[3, 2, 0], [3.25, 1, 0], [3.5, 2, 0]],
    [[3, 2, 0], [3.25, 1, 0]],
], ids=["north-side", "south-side", "corner", "endpoint"])
def test_api_joint_routing_refuses_aisle_touching_closed_keep_out(assigned_api, centerline, wait):
    constraints = {
        "keep_outs": [{"name": "Equipment pad", "min_xy": [3.25, -1], "max_xy": [4.25, 1]}],
        "aisles": [{"name": "Only aisle", "points": centerline, "lead_capacity": 4}],
    }
    params = {"expected_rev": 1, "routing_constraints": constraints}
    original = resolve_graph_context(assigned_api[1], TENANT, "solar")["graph"]
    public_error = _failed_routing_run(assigned_api, params, wait, "JOINT_ROUTING_INFEASIBLE")
    assert store.load_manifest(assigned_api[1], TENANT, "solar")["head"] == 1
    assert resolve_graph_context(assigned_api[1], TENANT, "solar")["graph"] == original
    with pytest.raises(GraphValidationError) as error:
        builtin().run(original, params)
    assert error.value.code == "JOINT_ROUTING_INFEASIBLE"
    expected = [(string_ref, kind) for string_ref in (S1, S2)
                for kind in ("start homerun", "end homerun")]
    assert set(error.value.path.split(", ")) == {f"{ref}/{kind}" for ref, kind in expected}
    leads = [(lead["string_ref"], lead["route_kind"]) for lead in public_error["unassigned_leads"]]
    assert sorted(leads) == sorted(expected)
    assert all(f"{ref}/{kind}" in public_error["message"] for ref, kind in expected)


def test_api_run_refuses_until_equipment_is_assigned(api):
    response = api[0].post("/api/run?wait=1", json=body(api, TOOL, {"expected_rev": 0}))
    assert response.status_code == 409, response.text
    env = response.json()
    assert env["reason_code"] == "equipment_assignment_required"
    assert env["availability"]["engine_ready"] is True
    assert env["availability"]["input_ready"] is False
    assert "broker_adapter_unavailable" not in env["availability"]["refusal_reasons"]
    assert not jobs._query("SELECT job_id FROM jobs")
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 1


def test_readiness_follows_the_producer_chain(assigned, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, assigned)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    ready = availability.w1_input_readiness(TENANT, "solar")[TOOL]
    assert ready == {"input_ready": True, "input_reason": None}
    assert availability.w1_availability(TOOL, entitled=True, inputs=ready) == {
        "entitled": True, "engine_ready": True, "implemented": True,
        "input_ready": True, "input_reason": None, "entitlement_reason": None,
        "engine_reason": None, "implementation_reason": None,
        "refusal_reasons": [], "runnable": True,
    }
    unassigned_path = tmp_path / "unassigned"
    unassigned_path.mkdir()
    backend, _ = seed(unassigned_path, monkeypatch, graph.__wrapped__())
    assert availability.w1_input_readiness(TENANT, "solar")[TOOL] == {
        "input_ready": False, "input_reason": "equipment_assignment_required"}


def test_parity_receipt_replays_through_the_studio_producer():
    # This is the G35 producer's receipt, not a graph-native parity claim.
    name = "solar_inverter_cabling_evidence"
    producer = sys.modules.get(name)
    if producer is None:
        spec = importlib.util.spec_from_file_location(
            name, SERVER.parent / "scripts/solar_inverter_cabling_evidence.py")
        producer = importlib.util.module_from_spec(spec)
        sys.modules[name] = producer
        spec.loader.exec_module(producer)
    docs, refused = producer.run_steps(
        producer.DEFAULT_STATES, producer.load_intake_groups(producer.DEFAULT_INTAKE), only="i12")
    assert refused == {}
    receipt = json.loads((SERVER.parent / "docs/parity/receipts/homeruns/rooftop-inverters-i12.json")
                         .read_text(encoding="utf-8"))
    assert receipt["comparator"]["verdict"] == "pass"
    assert receipt["capability"] == docs["i12"]["provenance"]["capability"] == "homeruns"
    assert receipt["capability"] in solar_tools.get(TOOL)["ledger"]
    assert docs["i12"]["output_sha256"] == (
        "dac8fe3a40025f3a7d2ed9f3016c0748e7595a3171cb5c01d709a7235a7247aa")
    for side in ("studio", "plugin"):
        for key in ("output_sha256", "input_sha256", "fixture_sha256"):
            assert docs["i12"][key] == receipt["comparison"][side][key]
    assert docs["i12"]["after"] == receipt["comparison"]["studio"]["after"]


def _box(assigned, left, right, layer="Keepout", **properties):
    scale = assigned["project"]["units"]["meters_per_unit"]
    return {"handle": "B8", "layer": layer, "closed": True, **properties,
            "pts": [[x / scale, y / scale] for x, y in
                    [(left, -.4), (right, -.4), (right, .4), (left, .4)]]}


def _save(assigned, monkeypatch, directory, polylines):
    monkeypatch.setenv("LEAF_DRAWING_STORE", "legacy")
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
    monkeypatch.delenv("LEAF_DRAWING_MUTATIONS_FENCE_FILE", raising=False)
    monkeypatch.setenv("LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED", "1")
    backend = store.FilesystemBackend(str(directory / "drawings"))
    path = directory / "seed.json"
    path.write_text(json.dumps({
        "dwg": {}, "layers": [], "polylines": polylines, "inserts": [], "faces3d": [],
        "blockdefs": [], "geodata": None, "solar_design_graph": assigned,
        "solar_design_graph_sha256": digest(assigned),
    }), encoding="utf-8")
    store.ingest_drawing(backend, TENANT, str(path), drawing_id="solar")
    return backend


ALL_LEADS = {f"{string}/{terminal} homerun" for string in (S1, S2) for terminal in ("start", "end")}


@pytest.mark.parametrize("left,right,layer,leads", [
    (3.5, 4.0, "Keepout", ALL_LEADS),
    (1.5, 1.75, "PV_KEEP_OUT", {f"{S1}/start homerun"}),
    (4.75, 5.25, "Roof Keepouts", ALL_LEADS),
], ids=["crosses-all", "one-lead", "assigned-inverter-in-keepout"])
def test_saved_keepout_refuses_with_exact_manual_leads(assigned, isolated_jobs, no_network,
                                                     monkeypatch, tmp_path, left, right, layer, leads):
    backend = _save(assigned, monkeypatch, tmp_path, [_box(assigned, left, right, layer)])
    _, key, _ = store.resolve_version_entry(backend, TENANT, "solar", 1)
    parent = backend.get(key)
    prefix = "Route these homerun leads manually. "
    for api in _api(backend, tmp_path, monkeypatch):
        response = api[0].post("/api/run?wait=1", json=body(api, TOOL, {"expected_rev": assigned["rev"]}))
        assert response.status_code == 400, response.text
        envelope = response.json()
        assert envelope["reason_code"] == "JOINT_ROUTING_INFEASIBLE"
        message = envelope["error"]["message"]
        assert message.startswith(prefix)
        assert set(message.removeprefix(prefix).split(", ")) == leads
        unassigned_leads = envelope["error"]["unassigned_leads"]
        assert {f"{lead['string_ref']}/{lead['route_kind']}" for lead in unassigned_leads} == leads
    record, = jobs._query("SELECT job_id FROM jobs")
    job = jobs.get_job(record["job_id"])
    assert job["status"] == "failed"
    assert job["error"]["message"] == message
    assert job["error"]["unassigned_leads"] == unassigned_leads
    assert store.load_manifest(backend, TENANT, "solar")["head"] == 1
    assert backend.get(key) == parent


def test_saved_keepout_combines_with_explicit_detour(assigned, no_network, monkeypatch, tmp_path):
    polylines = [_box(assigned, 3.5, 4.0), _box(assigned, 4.75, 5.25, space="paper")]
    backend = _save(assigned, monkeypatch, tmp_path, polylines)
    requested = {"keep_outs": [{"name": "Drawing keepout 1", "min_xy": [0, -2], "max_xy": [1, -1]}],
                 "aisles": [{"name": "North aisle", "points": [[3, 1, 0], [4.5, 1, 0]],
                             "lead_capacity": 4}]}
    with held(backend) as fence:
        receipt = dispatch(backend, fence, {"drawing_id": "solar", "expected_rev": assigned["rev"],
                                            "routing_constraints": requested})
    _, key, _ = store.resolve_version_entry(backend, TENANT, "solar", 2)
    saved = json.loads(backend.get(key))
    result = saved["solar_design_graph"]
    assert saved["polylines"] == polylines
    assert receipt["graph_sha256"] == digest(result)
    assert len(result["routes"]) == 4
    for route in result["routes"]:
        assert route["points"][1:] == [[3, 1, 0], [4.5, 1, 0], [5, 0, 0]]
        assert route["extra"]["routing"]["direct_keep_outs"] == ["Drawing keepout 2"]
        assert route["validity"]["state"] == "valid"


def test_saved_curve_refuses_on_api_rail(assigned, isolated_jobs, no_network, monkeypatch, tmp_path):
    backend = _save(assigned, monkeypatch, tmp_path, [_box(assigned, 3.5, 4.0, bulges=[1, 0, 0, 0])])
    for api in _api(backend, tmp_path, monkeypatch):
        response = api[0].post("/api/run?wait=1", json=body(api, TOOL, {"expected_rev": assigned["rev"]}))
        assert response.status_code == 400, response.text
        assert response.json()["reason_code"] == "INVALID_ROUTING_CONSTRAINTS", response.text
    assert store.load_manifest(backend, TENANT, "solar")["head"] == 1


def _aisles(assigned, lines, layer="Roof Aisles", **properties):
    scale = assigned["project"]["units"]["meters_per_unit"]
    return [{"handle": f"A{index}", "layer": layer, "closed": False, **properties,
             "pts": [[x / scale, y / scale] for x, y in line]}
            for index, line in enumerate(lines)]


@pytest.mark.parametrize("lines,first_points,detour", [
    ([[(1, 0), (1, 2), (5, 2), (5, 0)], [(2, 0), (2, 3)], [(3, 0), (3, 2)]],
     [[1, 0, 0], [1, 2, 0], [2, 2, 0], [3, 2, 0], [5, 2, 0], [5, 0, 0]], 4),
    ([[(1, 0), (5, 0)], [(2, 0), (4, 0), (2, 0)]],
     [[1, 0, 0], [2, 0, 0], [3, 0, 0], [4, 0, 0], [5, 0, 0]], 0),
    ([[(1, 0), (3, 0)], [(5, 0), (5, -1), (3, -1), (3, 0)]],
     [[1, 0, 0], [2, 0, 0], [3, 0, 0], [3, -1, 0], [5, -1, 0], [5, 0, 0]], 2),
], ids=["offset-crossing-and-tee", "aligned-midsegment-and-overlap", "reversed-endpoint-elbows"])
def test_selected_saved_aisles_persist_exact_terminal_routes(
        assigned, isolated_jobs, no_network, monkeypatch, tmp_path, lines, first_points, detour):
    polylines = _aisles(assigned, lines)
    polylines += _aisles(assigned, [[(1, 0), (5, 0)]], space="paper", bulges=[1])
    polylines += _aisles(assigned, [[(1, 0), (5, 0)]], layer="Other Aisles")
    backend = _save(assigned, monkeypatch, tmp_path, polylines)
    for api in _api(backend, tmp_path, monkeypatch):
        response = api[0].post("/api/run?wait=1", json=body(
            api, TOOL, {"expected_rev": 1, "saved_aisle_layer": "roof aisles"}))
        assert response.status_code == 200, response.text
    _, key, _ = store.resolve_version_entry(backend, TENANT, "solar", 2)
    saved = json.loads(backend.get(key))
    assert saved["polylines"] == polylines
    result = saved["solar_design_graph"]
    assert [route["id"] for route in result["routes"]] == ROUTE_IDS
    assert result["routes"][0]["points"] == first_points
    for route, terminal in zip(result["routes"], (1, 2, 3, 3)):
        assert route["points"][0] == [terminal, 0, 0]
        assert route["points"][-1] == [5, 0, 0]
        expected = 5 - terminal + detour
        assert route["length_ft"] == pytest.approx(expected / .3048)
        assert route["extra"]["routing_policy"] == "saved-aisle-centerline-v1"
        assert route["extra"]["saved_aisle_layer"] == "roof aisles"
        assert route["validity"]["state"] == "valid"
    assert result["schedules"][0]["validity"]["state"] == "stale"


@pytest.mark.parametrize("blocked", [False, True], ids=["disconnected", "saved-keepout"])
def test_selected_saved_aisles_refuse_atomically(
        assigned, isolated_jobs, no_network, monkeypatch, tmp_path, blocked):
    lines = [[(1, 0), (2, 0), (2, 2), (5, 2), (5, 0)]]
    if blocked:
        lines.append([(3, 0), (3, 2)])
    polylines = _aisles(assigned, lines)
    if blocked:
        polylines.append(_box(assigned, 2.75, 3.25))
    backend = _save(assigned, monkeypatch, tmp_path, polylines)
    _, key, _ = store.resolve_version_entry(backend, TENANT, "solar", 1)
    parent = backend.get(key)
    for api in _api(backend, tmp_path, monkeypatch):
        response = api[0].post("/api/run?wait=1", json=body(
            api, TOOL, {"expected_rev": 1, "saved_aisle_layer": "roof aisles"}))
        assert response.status_code == 400, response.text
        envelope = response.json()
        assert envelope["reason_code"] == "JOINT_ROUTING_INFEASIBLE"
        assert envelope["error"]["unassigned_leads"] == [
            {"string_ref": S2, "route_kind": "start homerun"},
            {"string_ref": S2, "route_kind": "end homerun"},
        ]
        message = envelope["error"]["message"].removeprefix("Route these homerun leads manually. ")
        assert message.split(", ") == [f"{S2}/start homerun", f"{S2}/end homerun"]
    assert store.load_manifest(backend, TENANT, "solar")["head"] == 1
    assert backend.get(key) == parent


@pytest.mark.parametrize("layer,properties,reason", [
    ("Missing", {}, "SAVED_AISLE_LAYER_EMPTY"),
    ("Roof Aisles", {"bulges": [1, 0]}, "INVALID_SAVED_AISLE_GEOMETRY"),
])
def test_selected_saved_aisle_geometry_errors(
        assigned, isolated_jobs, no_network, monkeypatch, tmp_path, layer, properties, reason):
    polylines = _aisles(assigned, [[(1, 0), (5, 0)]], **properties)
    backend = _save(assigned, monkeypatch, tmp_path, polylines)
    for api in _api(backend, tmp_path, monkeypatch):
        response = api[0].post("/api/run?wait=1", json=body(
            api, TOOL, {"expected_rev": 1, "saved_aisle_layer": layer}))
        assert response.status_code == 400, response.text
        assert response.json()["reason_code"] == reason
    assert store.load_manifest(backend, TENANT, "solar")["head"] == 1
