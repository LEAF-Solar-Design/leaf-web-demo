"""Explicit conductor choices, selective invalidation and the real local rail."""
import copy
import sys
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import catalog
import checkout_capability
import deps
import jobs
import product_capability_availability as availability
import solar_local_graph
import solar_tools
import store
from solar_design_graph import GraphValidationError
from solar_graph_context import resolve_graph_context
from solar_solve_results import sync_assignments
from solar_wiring_client import local_routes
from test_w1_design_graph import app_id, graph  # noqa: F401
from test_w1_equipment import case, equipment, licensed  # noqa: F401
from test_w1_local_graph_adapter import held
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_local_graph_rail import api, body  # noqa: F401
from test_w1_solve_commit import seed
from test_solar_w2_registration import dispatch, head_graph, latest

TOOL = "solar-string-conductors"
TENANT = "fixture-tenant"


def request_for(graph, gauge="10 AWG"):
    return {"operation": "set-conductors", "expected_rev": graph["rev"],
            "assignments": [{"string_ref": string["id"], "wire_gauge": gauge}
                            for string in graph["strings"]]}


def run(graph, params=None):
    return solar_local_graph._load_builtin(TOOL).run(
        graph, request_for(graph) if params is None else params)


@pytest.fixture
def unchosen(graph):
    for string in graph["strings"]:
        string["wire_gauge"] = ""
    return graph


def add_route_dependencies(graph):
    """Both endpoint directions, a shared inverter, and distinct schedules."""
    first, second = graph["strings"]
    template = copy.deepcopy(graph["routes"][0])
    graph["routes"] = []
    inverter = graph["inverters"][0]["id"]
    for number, (source, target) in enumerate(
            ((first["id"], inverter), (second["id"], inverter), (inverter, first["id"])), 1):
        route = copy.deepcopy(template)
        route.update(id=app_id("route", number), from_ref=source, to_ref=target,
                     validity={"state": "valid", "reasons": []})
        graph["routes"].append(route)
    template = copy.deepcopy(graph["schedules"][0])
    graph["schedules"] = []
    for number, route in enumerate(graph["routes"], 1):
        schedule = copy.deepcopy(template)
        schedule.update(id=app_id("schedule", number), source_refs=[route["id"]],
                        validity={"state": "valid", "reasons": []})
        graph["schedules"].append(schedule)
    # Direct references to both strings must stale; only the unchanged string must not.
    template.update(id=app_id("schedule", 4), source_refs=[first["id"], second["id"]],
                    validity={"state": "valid", "reasons": []})
    graph["schedules"].append(template)
    unchanged = copy.deepcopy(template)
    unchanged.update(id=app_id("schedule", 5), source_refs=[second["id"]])
    graph["schedules"].append(unchanged)


def test_conductors_set_every_string_atomically(unchosen):
    before = copy.deepcopy(unchosen)
    after = run(unchosen)
    assert unchosen == before
    assert (after["rev"], after["parent_rev"]) == (before["rev"] + 1, before["rev"])
    for old, new in zip(before["strings"], after["strings"]):
        assert new["wire_gauge"] == "10 AWG"
        assert new["rev"] == after["rev"]
        assert new["provenance"]["last_writer"] == TOOL
        for key in ("ordered_panel_refs", "module_count", "circuit_tag", "inverter_ref", "validity"):
            assert new[key] == old[key]
    for key in ("project", "settings", "electrical_zones", "frames", "panels", "inverters"):
        assert after[key] == before[key]


def test_conductors_change_only_dependent_routes_and_schedules(unchosen):
    chosen = run(unchosen)
    add_route_dependencies(chosen)
    before = copy.deepcopy(chosen)
    params = request_for(chosen, "8 AWG")
    params["assignments"] = params["assignments"][:1]
    after = run(chosen, params)
    assert chosen == before
    assert [s["wire_gauge"] for s in after["strings"]] == ["8 AWG", "10 AWG"]
    assert after["strings"][1] == before["strings"][1]
    for collection in ("routes", "schedules"):
        for index in (0, 2):
            changed = after[collection][index]
            assert changed["validity"] == {"state": "stale", "reasons": ["conductor_changed"]}
            assert changed["rev"] == after["rev"]
            assert changed["provenance"]["last_writer"] == TOOL
        assert after[collection][1] == before[collection][1]
    assert after["schedules"][3]["validity"] == {
        "state": "stale", "reasons": ["conductor_changed"]}
    assert after["schedules"][3]["rev"] == after["rev"]
    assert after["schedules"][3]["provenance"]["last_writer"] == TOOL
    assert after["schedules"][4] == before["schedules"][4]
    for key in ("inverters", "settings", "frames", "panels", "electrical_zones"):
        assert after[key] == before[key]
    assert after["inverters"][0]["input_assignments"] == before["inverters"][0]["input_assignments"]
    assert after["settings"]["global_string_sizing_confirmed"] is True


def test_conductors_same_gauge_invalidates_nothing(graph):
    add_route_dependencies(graph)
    before = copy.deepcopy(graph)
    after = run(graph)
    assert graph == before
    assert after["rev"] == before["rev"] + 1
    for old, new in zip(before["strings"], after["strings"]):
        assert new["validity"] == old["validity"]
        assert new["wire_gauge"] == old["wire_gauge"]
        assert {key: value for key, value in new.items() if key not in {"rev", "provenance"}} == {
            key: value for key, value in old.items() if key not in {"rev", "provenance"}}
    for key in ("routes", "schedules", "inverters", "frames", "settings"):
        assert after[key] == before[key]


@pytest.mark.parametrize("defect,code", [
    ("duplicate", "DUPLICATE_STRING_ASSIGNMENT"),
    ("missing", "MISSING_STRING"),
    ("empty-gauge", "INVALID_WIRE_GAUGE"),
    ("unknown-gauge", "INVALID_WIRE_GAUGE"),
    ("empty-batch", "INVALID_CONDUCTOR_REQUEST"),
    ("extra-item-key", "INVALID_CONDUCTOR_REQUEST"),
    ("operation", "INVALID_CONDUCTOR_REQUEST"),
    ("late-missing", "MISSING_STRING"),
])
def test_conductors_refusals_are_atomic(unchosen, tmp_path, monkeypatch, defect, code):
    params = request_for(unchosen)
    if defect == "duplicate":
        params["assignments"][1] = copy.deepcopy(params["assignments"][0])
    elif defect == "missing":
        params["assignments"][0]["string_ref"] = app_id("string", 99)
    elif defect == "empty-gauge":
        params["assignments"][0]["wire_gauge"] = ""
    elif defect == "unknown-gauge":
        params["assignments"][0]["wire_gauge"] = "9 AWG"
    elif defect == "empty-batch":
        params["assignments"] = []
    elif defect == "extra-item-key":
        params["assignments"][0]["extra"] = True
    elif defect == "operation":
        params["operation"] = "other"
    elif defect == "late-missing":
        params["assignments"][1]["string_ref"] = app_id("string", 99)
    before, original_params = copy.deepcopy(unchosen), copy.deepcopy(params)
    backend, _ = seed(tmp_path, monkeypatch, unchosen)
    with pytest.raises(GraphValidationError) as error:
        run(unchosen, params)
    assert error.value.code == code
    with held(backend) as fence:
        with pytest.raises(GraphValidationError) as error:
            dispatch(backend, fence, TOOL, params)
    assert error.value.code == code
    assert unchosen == before and params == original_params
    assert head_graph(backend) == before
    assert latest(backend) == 1
    assert store.load_manifest(backend, TENANT, "solar")["head"] == 1


@pytest.mark.parametrize("phase,code", [
    ("shape", "INVALID_CONDUCTOR_REQUEST"),
    ("gauge", "INVALID_WIRE_GAUGE"),
    ("duplicate", "DUPLICATE_STRING_ASSIGNMENT"),
])
def test_conductors_validation_order_covers_whole_batch(graph, phase, code):
    missing = {"string_ref": app_id("string", 99), "wire_gauge": "10 AWG"}
    params = request_for(graph)
    params["assignments"] = [missing, copy.deepcopy(missing)]
    if phase == "shape":
        params["assignments"][0]["wire_gauge"] = "9 AWG"
        params["assignments"][1]["extra"] = True
    elif phase == "gauge":
        params["assignments"][1]["wire_gauge"] = "9 AWG"
    before = copy.deepcopy(graph)
    with pytest.raises(GraphValidationError) as error:
        run(graph, params)
    assert error.value.code == code
    assert graph == before


@pytest.mark.parametrize("patch", [
    {"unknown": True}, {"cancel": 1}, {"expected_rev": True},
    {"drawing_id": 1}, {"assignments": {}}, {"assignments": [None]},
    {"assignments": [{"string_ref": "x", "wire_gauge": 10}]},
    {"assignments": [{"string_ref": "x", "wire_gauge": "10 AWG"}] * 4097},
])
def test_conductors_request_shape_fails_closed(graph, patch):
    params = request_for(graph)
    params.update(patch)
    before = copy.deepcopy(graph)
    with pytest.raises(GraphValidationError, match="INVALID_CONDUCTOR_REQUEST"):
        run(graph, params)
    assert graph == before


def test_conductors_cancel_and_stale_revision(unchosen):
    params = dict(request_for(unchosen), cancel=True)
    before = copy.deepcopy(unchosen)
    assert run(unchosen, params) == before
    assert unchosen == before
    params["expected_rev"] += 1
    with pytest.raises(GraphValidationError, match="STALE_GRAPH_REVISION"):
        run(unchosen, params)
    assert unchosen == before


def test_conductors_stale_string_is_not_repaired(unchosen):
    stale = {"state": "stale", "reasons": ["settings_changed"]}
    unchosen["strings"][0]["validity"] = copy.deepcopy(stale)
    after = run(unchosen)
    assert after["strings"][0]["wire_gauge"] == "10 AWG"
    assert after["strings"][0]["validity"] == stale
    with pytest.raises(GraphValidationError, match="ROUTING_TOPOLOGY_REQUIRED"):
        local_routes(after)


def test_conductors_explicit_choice_unblocks_local_routes(unchosen):
    with pytest.raises(GraphValidationError, match="ROUTING_TOPOLOGY_REQUIRED"):
        local_routes(unchosen)
    after = run(unchosen, request_for(unchosen, "8 AWG"))
    routes = local_routes(after)
    assert len(routes) == 2 * len(after["strings"])
    for string in after["strings"]:
        leads = [route for route in routes if route["from_ref"] == string["id"]]
        assert {route["route_kind"] for route in leads} == {"start homerun", "end homerun"}
        assert all(route["wire_gauge"] == string["wire_gauge"] == "8 AWG" for route in leads)


def test_conductors_available_while_homeruns_refused(case):
    source, params, intake = case
    assigned = equipment.assign_equipment(
        source, params, drawing_intake=intake, licensed_equipment=licensed)["graph"]
    assert availability.w1_graph_readiness(copy.deepcopy(assigned))["solar-homeruns"] == {
        "input_ready": True, "input_reason": None}
    for string in assigned["strings"]:
        string["wire_gauge"] = ""
    states = availability.w1_graph_readiness(copy.deepcopy(assigned))
    assert states[TOOL] == {"input_ready": True, "input_reason": None}
    assert states["solar-homeruns"] == {
        "input_ready": False, "input_reason": "routing_topology_required"}
    after = run(assigned, request_for(assigned))
    assert availability.w1_graph_readiness(copy.deepcopy(after))["solar-homeruns"] == {
        "input_ready": True, "input_reason": None}
    after["strings"][0]["validity"] = {"state": "stale", "reasons": ["settings_changed"]}
    assert availability.w1_graph_readiness(after)[TOOL]["input_ready"] is True
    empty = graph.__wrapped__()
    empty.update(strings=[], routes=[], schedules=[])
    for inverter in empty["inverters"]:
        inverter["input_assignments"] = []
    sync_assignments(empty)
    assert availability.w1_graph_readiness(empty)[TOOL] == {
        "input_ready": False, "input_reason": "strings_required"}


def test_conductors_registry_and_catalog(monkeypatch):
    monkeypatch.setattr(deps, "tenant_repo_dir", lambda tenant: None)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    declaration = next(row for row in solar_tools.entries() if row["name"] == TOOL)
    assert declaration["adapter"] == "local-graph-commit"
    assert declaration["family"] == "stringing"
    assert declaration["ledger"] == ["homeruns"]
    assert declaration["readiness"] == {"kind": "facets", "facets": ["strings"]}
    assert TOOL in solar_tools.local_graph_tools()
    assert TOOL not in availability.W1_CAPABILITIES
    assert len(availability.W1_CAPABILITIES) == 9
    families = catalog.build_catalog(deps.all_tools(TENANT))
    found = [(family, row) for family in families for row in family["capabilities"]
             if row["name"] == TOOL]
    assert len(found) == 1
    family, row = found[0]
    assert family["family_id"] == "stringing"
    assert row["solar"] == solar_tools.catalog_view(declaration["record"])
    assert row["params_schema"] == declaration["record"]["params"]


def test_conductors_params_schema(graph):
    schema = solar_tools.trusted_record(TOOL)["params"]
    validator = Draft7Validator(schema)
    valid = request_for(graph)
    assert validator.is_valid(valid)
    missing = copy.deepcopy(valid)
    del missing["assignments"][0]["wire_gauge"]
    assert not validator.is_valid(missing)
    unknown = copy.deepcopy(valid)
    unknown["assignments"][0]["wire_gauge"] = "9 AWG"
    assert not validator.is_valid(unknown)
    from solar_nec import WIRE_LABELS, WIRE_UNITS
    assert schema["properties"]["assignments"]["items"]["properties"]["wire_gauge"]["enum"] == [
        f"{label} {unit}" for label, unit in zip(WIRE_LABELS, WIRE_UNITS)]


@pytest.fixture
def conductor_api(unchosen, request):
    # Resolve api only after clearing the gauges, so its real persisted v1 is unchosen.
    client = request.getfixturevalue("api")
    client[2][TOOL] = solar_tools.trusted_record(TOOL)
    return client


@pytest.fixture
def conductor_commit(conductor_api, unchosen):
    response = conductor_api[0].post(
        "/api/run?wait=1", json=body(conductor_api, TOOL, request_for(unchosen)))
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    return response.json()


def test_conductors_run_rail_commits_one_job(conductor_api, conductor_commit, unchosen):
    env = conductor_commit
    assert env["execution_provenance"]["execution_mode"] == "local_graph_commit"
    assert env["result"]["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    assert store.load_manifest(conductor_api[1], TENANT, "solar")["head"] == 2
    rows = jobs._query("SELECT job_id FROM jobs")
    assert len(rows) == 1
    assert rows[0]["job_id"] == env["result"]["job_id"]
    assert jobs.get_job(rows[0]["job_id"])["status"] == "complete"
    after = head_graph(conductor_api[1])
    assert after["rev"] == unchosen["rev"] + 1
    assert [s["wire_gauge"] for s in after["strings"]] == ["10 AWG", "10 AWG"]
    for old, new in zip(unchosen["strings"], after["strings"]):
        for key in ("ordered_panel_refs", "module_count", "circuit_tag", "inverter_ref"):
            assert new[key] == old[key]


def test_conductors_drawing_undo_redo(conductor_api, conductor_commit, monkeypatch):
    from routers import drawings

    client, backend, _, route, _, tenant = conductor_api
    client.app.include_router(drawings.router)
    client.app.dependency_overrides[deps.require_active_tenant] = lambda: tenant
    monkeypatch.setenv("LEAF_CHECKOUT_CAP_SECRET", "conductor-test-checkout-secret")
    _, fence = route._checkout_identity()
    headers = {checkout_capability.CAPABILITY_HEADER: checkout_capability.mint(tenant, "solar", fence)}
    original = resolve_graph_context(backend, TENANT, "solar", 1)["graph"]
    committed = head_graph(backend)
    assert [s["wire_gauge"] for s in original["strings"]] == ["", ""]
    undone = client.post("/api/drawings/solar/undo", headers=headers)
    assert undone.status_code == 200, undone.text
    assert store.load_manifest(backend, TENANT, "solar")["head"] == 1
    assert head_graph(backend) == original
    redone = client.post("/api/drawings/solar/redo", headers=headers)
    assert redone.status_code == 200, redone.text
    assert store.load_manifest(backend, TENANT, "solar")["head"] == 2
    assert head_graph(backend) == committed
    assert [s["wire_gauge"] for s in committed["strings"]] == ["10 AWG", "10 AWG"]
