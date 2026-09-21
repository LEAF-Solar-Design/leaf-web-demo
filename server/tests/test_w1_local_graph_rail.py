"""Solar table activation through the durable jobs and real broker rail."""
import copy
import json
import sys
from pathlib import Path

import jsonschema
import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import broker_client
import catalog
import deps
import jobs
import product_capability_availability as availability
import solar_local_graph as local
import store
import write_loop
from solar_graph_context import resolve_graph_context
from solar_sizing_client import digest
from test_w1_design_graph import graph  # noqa: F401
from test_w1_equipment import case  # noqa: F401
from test_w1_graph_versions import drawing, commit, request_for, TENANT as BUNDLE_TENANT, DRAWING  # noqa: F401
from test_w1_local_graph_adapter import seed, held
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_solve_commit import transfer

TENANT = "fixture-tenant"
UNWIRED = (
    "solar-size-strings", "solar-panel-groups", "solar-commit-solve",
    "solar-assign-equipment", "solar-homeruns", "solar-schedule",
)


@pytest.fixture
def api(isolated_jobs, no_network, graph, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import jobs as route

    monkeypatch.setenv("BROKER_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("BROKER_TENANTS", str(tmp_path / "tenants.json"))
    monkeypatch.setenv("LEAF_BROKER_STORE", "legacy")
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "staging")
    import broker

    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "_tenants", {TENANT: {"tier": "demo", "disabled": False}})
    monkeypatch.setattr(broker, "_cap_preflight", lambda *a: None)
    monkeypatch.setattr(broker, "_emit_aps_metric", lambda *a: None)
    monkeypatch.setattr(broker, "_get_da", lambda: pytest.fail("APS must not execute"))
    backend, _ = seed(tmp_path, monkeypatch, graph)
    tools = {tool["name"]: tool for tool in
             json.loads((SERVER / "write_tools.json").read_text())["tools"]}

    class InlineExecutor:
        def submit(self, fn, *args, **kwargs):
            fn(*args, **kwargs)

    monkeypatch.setattr(jobs, "_executors", {
        jobs.lane_for(tool, False): InlineExecutor() for tool in tools.values()})
    monkeypatch.setattr(route.deps, "find_tool", lambda name, *a: tools.get(name))
    monkeypatch.setattr(route.deps, "effective_tools_with_provenance", lambda *a: [])
    monkeypatch.setattr(route.deps, "backedge_run_identity", lambda tenant, *a: tenant)
    monkeypatch.setattr(route.deps, "auth_live", lambda: False)
    monkeypatch.setattr(jobs.platform_link, "resolve_submission_context", lambda *a: None)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    monkeypatch.setattr(route.catalog, "live_aps_runtime_authorized", lambda *a, **k: True)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    monkeypatch.delenv("LEAF_ENTITLEMENTS_FILE", raising=False)
    monkeypatch.delenv("LEAF_EXACT_WRITE_PINS_REQUIRED", raising=False)

    def transport(url, *, json, headers, timeout):
        assert url.endswith("/broker/run")
        assert json["aps_live"] is False
        response = broker._broker_run(broker.BrokerRunRequest(**json))

        class Reply:
            status_code = response.status_code

            def json(self):
                return __import__("json").loads(response.body)

        return Reply()

    monkeypatch.setattr(broker_client.requests, "post", transport)
    app = FastAPI()
    app.include_router(route.router)
    tenant = route.deps.TenantContext(
        TENANT, tier="demo", subject="fixture-subject", authority_resolved=True)
    app.dependency_overrides[route.deps.require_tenant] = lambda: tenant
    with held(backend) as fence:
        monkeypatch.setattr(route, "_checkout_identity", lambda *a: ("fixture-owner", fence))
        with TestClient(app) as client:
            yield client, backend, tools, route, broker, tenant


def body(api, name="solar-settings", params=None):
    return {"tool": name, "dwg": "solar", "params": copy.deepcopy(params) if params is not None else {
        "expected_rev": 0, "changes": {"panels_in_sequence": 3}},
        "catalog_digest": deps.catalog_tool_digest(api[2][name])}


@pytest.fixture
def committed(api):
    response = api[0].post("/api/run?wait=1", json=body(api))
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True
    return env, jobs.get_job(env["result"]["job_id"])


def test_settings_commit_pins_parent(api, committed):
    _, rec = committed
    assert rec["status"] == "complete"
    assert rec["dwg_version"] == 1
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 2
    before = resolve_graph_context(api[1], TENANT, "solar", 1)
    after = resolve_graph_context(api[1], TENANT, "solar", 2)
    assert after["representation"] == "intake"
    assert after["graph"]["settings"]["panels_in_sequence"] == 3
    assert before["graph"]["settings"]["panels_in_sequence"] != 3


def test_receipt_and_durable_proof(api, committed):
    env, rec = committed
    receipt = env["result"]
    assert receipt["schema_version"] == local.RESULT_SCHEMA
    assert receipt["adapter"] == "local-graph-commit"
    assert receipt["job_id"] == rec["job_id"]
    assert receipt["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    assert receipt["replayed"] is False
    proof = {
        "execution_mode": "local_graph_commit", "adapter": "local-graph-commit",
        "request_sha256": receipt["request_sha256"], "graph_sha256": receipt["graph_sha256"],
        "intake_sha256": receipt["intake_sha256"], "source_version": 1, "new_version": 2,
    }
    assert all(rec["provenance"][key] == value for key, value in proof.items())
    assert rec["provenance"]["execution_path"] == "local"
    assert rec["provenance"] == env["execution_provenance"]


def test_broker_ledger(api, committed):
    broker = api[4]
    entry = json.loads(broker.LEDGER_PATH.read_text().splitlines()[-1])
    assert entry["tool"] == "solar-settings"
    assert entry["aps_live"] is False
    assert entry["aps_endpoint"] == broker.APS_ENDPOINT
    assert isinstance(entry["aps_endpoint"], str) and entry["aps_endpoint"]
    jsonschema.validate(entry, json.loads((SERVER / "broker_ledger.schema.json").read_text()))


def test_stale_parent_fails_without_commit(api, committed):
    request = body(api)
    request["params"]["changes"]["panels_in_sequence"] = 4
    request["dwg_version"] = 1
    response = api[0].post("/api/run?wait=1", json=request)
    assert response.status_code == 409, response.text
    assert response.json()["reason_code"] == "not_current_head"
    assert response.json()["availability"]["input_ready"] is False
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 2
    rows = jobs._query("SELECT job_id FROM jobs")
    assert len(rows) == 1
    assert rows[0]["job_id"] == committed[1]["job_id"]
    # The adapter and broker suites still prove STALE_GRAPH_REVISION for races
    # that reach the adapter after admission.


def test_correction_commit(api, graph):
    response = api[0].post("/api/run?wait=1", json=body(api, "solar-correct-string", transfer(graph)))
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert response.json()["result"]["tool"] == "solar-correct-string"
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 2


def test_checkout_required(api, monkeypatch):
    monkeypatch.setattr(api[3], "_checkout_identity", lambda *a: (store.ANONYMOUS_HOLDER, None))
    response = api[0].post("/api/run?wait=1", json=body(api))
    assert response.status_code == 403, response.text
    assert response.json()["reason_code"] == "CHECKOUT_REQUIRED"
    assert not jobs._query("SELECT job_id FROM jobs")
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 1


def test_mutation_gate_prevents_commit(api, monkeypatch):
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
    response = api[0].post("/api/run?wait=1", json=body(api))
    assert response.json()["ok"] is False
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 1
    assert all(jobs.get_job(row["job_id"])["status"] != "complete"
               for row in jobs._query("SELECT job_id FROM jobs"))


@pytest.mark.parametrize("name", UNWIRED)
def test_other_persisted_capabilities_refuse_before_submission(api, name):
    response = api[0].post("/api/run?wait=1", json=body(api, name))
    assert response.status_code == 409, response.text
    assert "broker_adapter_unavailable" in response.json()["availability"]["refusal_reasons"]
    assert not jobs._query("SELECT job_id FROM jobs")
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 1


def test_catalog_engine_readiness(api, graph):
    families = catalog.build_catalog(deps.all_tools(TENANT))
    availability.annotate_w1_availability(
        families, api[5], "solar", project_id=graph["project"]["id"])
    states = {row["name"]: row["availability"] for family in families
              for row in family["capabilities"] if row["name"] in availability.W1_CAPABILITIES}
    assert states["solar-settings"]["engine_ready"] is True
    assert states["solar-correct-string"]["engine_ready"] is True
    assert sum(state["engine_ready"] is True for state in states.values()) == 3


def test_intake_catalog_uses_adapter_format(api, graph):
    families = catalog.build_catalog(deps.all_tools(TENANT))
    availability.annotate_w1_availability(
        families, api[5], "solar", project_id=graph["project"]["id"])
    states = {row["name"]: row["availability"] for family in families
              for row in family["capabilities"] if row["name"] in availability.W1_CAPABILITIES}
    settings = states["solar-settings"]
    assert settings["engine_ready"] is True
    assert settings["input_ready"] is True
    assert settings["runnable"] is True
    sizing = states["solar-size-strings"]
    assert sizing["input_ready"] is False
    assert sizing["input_reason"] == "persisted_graph_unavailable"
    assert sizing["refusal_reasons"][0] == "broker_adapter_unavailable"


@pytest.mark.parametrize("name", ["solar-settings", "solar-correct-string"])
def test_bundle_refuses_before_submission(api, drawing, case, monkeypatch, name):
    backend, _ = drawing
    graph, _, _ = case
    commit(drawing, request_for(backend, graph))
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    tenant = deps.TenantContext(
        BUNDLE_TENANT, tier="demo", subject="fixture-subject", authority_resolved=True)
    api[0].app.dependency_overrides[deps.require_tenant] = lambda: tenant
    request = body(api, name)
    request["dwg"] = DRAWING
    head = store.load_manifest(backend, BUNDLE_TENANT, DRAWING)["head"]
    response = api[0].post("/api/run?wait=1", json=request)
    assert response.status_code == 409, response.text
    assert response.json()["reason_code"] == "licensed_graph_commit_required"
    assert response.json()["availability"]["engine_ready"] is True
    assert response.json()["availability"]["input_ready"] is False
    assert not jobs._query("SELECT job_id FROM jobs")
    assert store.load_manifest(backend, BUNDLE_TENANT, DRAWING)["head"] == head


def test_plain_intake_refuses_before_submission(api):
    holder, fence = api[3]._checkout_identity()
    write_loop._put_bytes_version(
        api[1], TENANT, "solar", json.dumps({"layers": [], "polylines": []}).encode(),
        1, {}, holder=holder, fence=fence, require_parent_is_head=True)
    response = api[0].post("/api/run?wait=1", json=body(api))
    assert response.status_code == 409, response.text
    assert response.json()["reason_code"] == "persisted_graph_unavailable"
    assert not jobs._query("SELECT job_id FROM jobs")


def test_correction_requires_strings(api, graph):
    params = transfer(graph)
    empty = copy.deepcopy(graph)
    empty.update(strings=[], routes=[], schedules=[])
    for panel in empty["panels"]:
        panel["assignment"] = {"string_ref": None, "seq": None}
    for frame in empty["frames"]:
        frame["sequences"] = []
        for record in frame["panel_assignments"]:
            record.update(string_ref=None, seq=None, inverter_id=None, string_input_number=None)
        for row in frame["matrix"]:
            for cell in row:
                cell.update(seq=None, inverter_id=None, string_input_number=None)
    for inverter in empty["inverters"]:
        inverter["input_assignments"] = []
    intake = {"layers": [], "polylines": [], "solar_design_graph": empty,
              "solar_design_graph_sha256": digest(empty)}
    holder, fence = api[3]._checkout_identity()
    write_loop._put_bytes_version(
        api[1], TENANT, "solar", json.dumps(intake).encode(),
        1, {}, holder=holder, fence=fence, require_parent_is_head=True)
    response = api[0].post("/api/run?wait=1", json=body(api, "solar-correct-string", params))
    assert response.status_code == 409, response.text
    assert response.json()["reason_code"] == "strings_required"
    assert not jobs._query("SELECT job_id FROM jobs")


@pytest.mark.parametrize("name", ["solar-settings", "solar-correct-string"])
def test_unresolved_units_refuse_before_submission(api, graph, name):
    params = transfer(graph) if name == "solar-correct-string" else None
    graph["project"]["units"]["meters_per_unit"] = 1
    intake = {"layers": [], "polylines": [], "solar_design_graph": graph,
              "solar_design_graph_sha256": digest(graph)}
    holder, fence = api[3]._checkout_identity()
    write_loop._put_bytes_version(
        api[1], TENANT, "solar", json.dumps(intake).encode(),
        1, {}, holder=holder, fence=fence, require_parent_is_head=True)
    request = body(api, name, params)
    request["dwg_version"] = 2
    response = api[0].post("/api/run?wait=1", json=request)
    assert response.status_code == 409, response.text
    assert response.json()["reason_code"] == "unresolved_units"
    assert response.json()["availability"]["engine_ready"] is True
    assert response.json()["availability"]["input_ready"] is False
    assert not jobs._query("SELECT job_id FROM jobs")
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 2


def test_settings_commit_survives_malformed_equipment(api, case):
    graph, _, _ = case
    graph["extra"]["equipment"] = None
    previous_rev = graph["rev"]
    intake = {"layers": [], "polylines": [], "solar_design_graph": graph,
              "solar_design_graph_sha256": digest(graph)}
    holder, fence = api[3]._checkout_identity()
    write_loop._put_bytes_version(
        api[1], TENANT, "solar", json.dumps(intake).encode(),
        1, {}, holder=holder, fence=fence, require_parent_is_head=True)
    request = body(api, params={
        "expected_rev": previous_rev, "changes": {"panels_in_sequence": 3}})
    request["dwg_version"] = 2
    response = api[0].post("/api/run?wait=1", json=request)
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 3
    stored = resolve_graph_context(api[1], TENANT, "solar", "head")["graph"]
    assert stored["settings"]["panels_in_sequence"] == 3
    assert stored["rev"] == previous_rev + 1


def test_intake_catalog_survives_malformed_equipment(api, case):
    graph, _, _ = case
    graph["extra"]["equipment"] = None
    intake = {"layers": [], "polylines": [], "solar_design_graph": graph,
              "solar_design_graph_sha256": digest(graph)}
    holder, fence = api[3]._checkout_identity()
    write_loop._put_bytes_version(
        api[1], TENANT, "solar", json.dumps(intake).encode(),
        1, {}, holder=holder, fence=fence, require_parent_is_head=True)
    families = catalog.build_catalog(deps.all_tools(TENANT))
    availability.annotate_w1_availability(
        families, api[5], "solar", project_id=graph["project"]["id"])
    states = {row["name"]: row["availability"] for family in families
              for row in family["capabilities"] if row["name"] in availability.W1_CAPABILITIES}
    assert states["solar-settings"]["input_ready"] is True
    assert states["solar-settings"]["runnable"] is True
    assert states["solar-homeruns"]["input_ready"] is False
    assert states["solar-homeruns"]["input_reason"] == "persisted_graph_unavailable"


def test_digit_string_version_reads_like_its_integer(api, graph):
    # The capabilities route hands the gate the request's version as text; the resolver takes "head" or an int.
    project = graph["project"]["id"]
    by_int = availability.w1_input_readiness(api[5], "solar", project_id=project, version=1)
    by_text = availability.w1_input_readiness(api[5], "solar", project_id=project, version="1")
    assert by_text == by_int
    assert by_text["solar-settings"]["input_ready"] is True
    assert by_text["solar-size-strings"] == {
        "input_ready": False, "input_reason": "persisted_graph_unavailable"}
