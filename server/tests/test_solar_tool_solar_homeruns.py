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
        "sha256:0872050b6d4aec9ac3145ba9893ded785943097bc8b60d57c8a857e6699b24d7")
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
