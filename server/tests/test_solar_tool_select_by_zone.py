"""Select-by-zone proves the read kind through the catalog, API, jobs and broker."""
import copy
import json
from pathlib import Path

import pytest

import broker_client
import deps
import entitlements
import jobs
import product_capability_availability as availability
import solar_local_read as local
import solar_sizing_client
import solar_tools
import store
import write_loop
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError
from solar_sizing_client import digest
from test_w1_design_graph import graph  # noqa: F401
from test_w1_local_graph_adapter import held
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_solve_commit import seed

SERVER = Path(__file__).resolve().parents[1]
TENANT = "fixture-tenant"
TOOL = "solar-select-by-zone"
PANELS = ["leaf:panel:00000000-0000-4000-8000-%012d" % number for number in (1, 2, 3)]
SELECTED = {"status": "selected", "panel_refs": PANELS}
CANONICAL_OUTPUTS = {
    "selected": (186, "507886d2ecd51d59fbeb788048a180d4989a4183cad10771c1ee43f5589672f2"),
    "missing": (36, "a6558496a5b75a1e3a47bf3e56f0e25666296cb77457b934966b85f8efcf49d2"),
    "cancelled": (38, "26a95df6c769fe092fa85233d3c7412baa2bc060f48d9402eefec463a3fa636d"),
    "no-zones": (37, "f7d9e60f7952d906c8205df761fad7704ff8dd2dcaeb7b1c85b9c147780dff06"),
    "empty": (34, "6c93580e19a189890d76055a83d72c3ad22ec936748469ce6155b7377aa50c28"),
}


def test_declaration_pins():
    record = {
        "name": TOOL, "version": "1.0.0",
        "description": "List the panels of a named electrical zone, in the zone's stored order.",
        "kind": "script", "family_id": "placement", "engine_op": "solar_select_by_zone",
        "entry": "builtins/solar_select_by_zone.py",
        "params": {
            "type": "object",
            "properties": {"drawing_id": {"type": "string", "maxLength": 128},
                           "zone_name": {"type": "string", "maxLength": 1024}},
            "required": ["zone_name"], "additionalProperties": False,
        },
        "returns": {"type": "object"}, "capabilities": ["drawing.read"],
        "allow_local_fallback": False,
    }
    expected = {
        "schema": "leaf.solar-tool.v1", "name": TOOL,
        "builtin": "builtins/solar_select_by_zone.py", "family": "placement",
        "adapter": "local-graph-read", "entitlement": "run_read",
        "requires_persisted_graph": True, "seedable": False,
        "invalid_request_code": "INVALID_ZONE_SELECTION",
        "readiness": {"kind": "facets", "facets": ["electrical_zones"]},
        "engine": "server-builtin", "interaction": {"mode": "form"}, "record_store": "registry",
        "record": record, "ledger": ["select-by-zone"], "trusted_inputs": [],
        "maturity": "production", "wave": 2, "order": 10, "scenario": "w2-rooftop",
    }
    assert solar_tools.get(TOOL) == expected
    assert solar_tools.load().get(TOOL) == expected
    assert solar_tools.trusted_record(TOOL) == record
    assert entitlements.tool_required_capability(record) == "run_read"
    assert TOOL not in availability.W1_CAPABILITIES
    assert TOOL not in {row["name"] for row in json.loads(
        (SERVER / "write_tools.json").read_text(encoding="utf-8"))["tools"]}


@pytest.mark.parametrize("case,params,expected", [
    ("B1", {"zone_name": "Roof"}, SELECTED),
    ("B2", {"zone_name": "  rOOF\t"}, SELECTED),
    ("B3", {"zone_name": "Attic"}, {"status": "missing", "panel_refs": []}),
    ("B4", {"zone_name": "   "}, {"status": "cancelled", "panel_refs": []}),
    ("B5", {"zone_name": "Roof"}, {"status": "no-zones", "panel_refs": []}),
    ("B6", {"zone_name": ""}, {"status": "no-zones", "panel_refs": []}),
    ("B7", {"zone_name": "Roof"}, {"status": "empty", "panel_refs": []}),
    ("B8", {"zone_name": "ROOF"}, {"status": "selected", "panel_refs": [PANELS[2]]}),
    ("B9", {"zone_name": "Roof"}, SELECTED),
    ("B10-number", {"zone_name": 7}, "INVALID_ZONE_SELECTION"),
    ("B10-null", {"zone_name": None}, "INVALID_ZONE_SELECTION"),
    ("B10-missing", {}, "INVALID_ZONE_SELECTION"),
    ("B10-list", [], "INVALID_ZONE_SELECTION"),
    ("B11", {"zone_name": "Roof"}, "ZONE_DATA_UNSUPPORTED"),
])
def test_builtin_cases(graph, case, params, expected):
    if case in ("B5", "B6"):
        graph["electrical_zones"] = []
    elif case == "B7":
        graph["electrical_zones"][0]["panel_refs"] = []
    elif case == "B8":
        graph["electrical_zones"] = [
            {"name": "roof", "panel_refs": [PANELS[2]]},
            {"name": "Roof", "panel_refs": PANELS[:]},
        ]
    elif case == "B9":
        graph["electrical_zones"].insert(0, {"panel_refs": [PANELS[0]]})
    elif case == "B11":
        graph["electrical_zones"] = [{"name": "x" * 1025, "panel_refs": [PANELS[0]]}]
    builtin = local._load_builtin(TOOL)
    if isinstance(expected, str):
        with pytest.raises(GraphValidationError) as exc:
            builtin.run(graph, params)
        assert exc.value.code == expected
    else:
        output = builtin.run(graph, params)
        assert output == expected
        if case != "B8":
            size, sha = CANONICAL_OUTPUTS[output["status"]]
            assert len(canonical_bytes(output)) == size
            assert digest(output) == sha


def test_builtin_is_pure(graph):
    params = {"zone_name": "  rOOF\t"}
    original_graph, original_params = copy.deepcopy(graph), copy.deepcopy(params)
    builtin = local._load_builtin(TOOL)
    first = builtin.run(graph, params)
    second = builtin.run(graph, params)
    assert first == second == SELECTED
    assert graph == original_graph
    assert params == original_params
    first["panel_refs"].clear()
    assert graph == original_graph
    assert second == SELECTED


def _api(backend, tmp_path, monkeypatch):
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
    monkeypatch.setattr(broker, "run_tool_dynamic", lambda *a, **k: pytest.fail("dynamic dispatch"))
    monkeypatch.setattr(deps, "tenant_repo_dir", lambda tenant: None)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    monkeypatch.setattr(deps, "AUTHORED_STORE", tmp_path / "absent-authored.json")
    record = deps.find_tool(TOOL, TENANT)
    assert record == solar_tools.trusted_record(TOOL)
    assert (record, deps.TOOL_SOURCE_WRITE_SEED) in deps.effective_tools_with_provenance(TENANT)

    class InlineExecutor:
        def submit(self, fn, *args, **kwargs):
            fn(*args, **kwargs)

    monkeypatch.setattr(jobs, "_executors", {jobs.lane_for(record, False): InlineExecutor()})
    monkeypatch.setattr(route.deps, "backedge_run_identity", lambda tenant, *a: tenant)
    monkeypatch.setattr(route.deps, "auth_live", lambda: False)
    monkeypatch.setattr(jobs.platform_link, "resolve_submission_context", lambda *a: None)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    monkeypatch.setattr(route.catalog, "live_aps_runtime_authorized", lambda *a, **k: True)
    monkeypatch.delenv("LEAF_ENTITLEMENTS_FILE", raising=False)
    monkeypatch.delenv("LEAF_EXACT_WRITE_PINS_REQUIRED", raising=False)
    mode = {"tamper": False, "requests": []}

    def transport(url, *, json, headers, timeout):
        assert url.endswith("/broker/run")
        assert json["aps_live"] is False
        assert json["checkout_holder"] == store.ANONYMOUS_HOLDER
        assert json["checkout_fence"] is None
        mode["requests"].append(copy.deepcopy(json))
        response = broker._broker_run(broker.BrokerRunRequest(**json))

        class Reply:
            status_code = response.status_code

            def json(self):
                body = __import__("json").loads(response.body)
                if mode["tamper"] and body.get("ok") is True:
                    output = {"status": "missing", "panel_refs": []}
                    body["result"].update(output=output, output_sha256=digest(output),
                                          output_bytes=len(canonical_bytes(output)))
                return body

        return Reply()

    monkeypatch.setattr(broker_client.requests, "post", transport)
    app = FastAPI()
    app.include_router(route.router)
    tenant = route.deps.TenantContext(
        TENANT, tier="demo", subject="fixture-subject", authority_resolved=True)
    app.dependency_overrides[route.deps.require_tenant] = lambda: tenant
    # The router's checkout identity and the availability evaluator remain real.
    with TestClient(app) as client:
        yield client, backend, record, tenant, mode


@pytest.fixture
def api(isolated_jobs, no_network, graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    yield from _api(backend, tmp_path, monkeypatch)


@pytest.fixture
def no_zones_api(isolated_jobs, no_network, graph, tmp_path, monkeypatch):
    graph["electrical_zones"] = []
    graph["frames"][0]["electrical_zone_ref"] = None
    backend, _ = seed(tmp_path, monkeypatch, graph)
    yield from _api(backend, tmp_path, monkeypatch)


def body(api):
    return {"tool": TOOL, "dwg": "solar", "params": {"zone_name": "Roof"},
            "catalog_digest": deps.catalog_tool_digest(api[2])}


@pytest.mark.parametrize("state", ["ready", "adapter-missing", "units-unresolved"])
def test_availability_ready_and_reasons(api, monkeypatch, state):
    # R12 uses the actual API tenant and persisted graph.
    if state == "adapter-missing":
        monkeypatch.setitem(availability.SOLAR_CAPABILITIES[TOOL], "adapter", None)
    elif state == "units-unresolved":
        monkeypatch.setattr(solar_sizing_client, "units_resolved", lambda graph: False)
    actual = entitlements.w1_tool_availability({"name": TOOL}, api[3], "solar")
    if state == "ready":
        assert actual["engine_ready"] is True
        assert actual["input_ready"] is True
        assert actual["runnable"] is True
    elif state == "adapter-missing":
        assert actual["engine_ready"] is False
        assert actual["engine_reason"] == "broker_adapter_unavailable"
        assert actual["runnable"] is False
    else:
        assert actual["engine_ready"] is True
        assert actual["input_ready"] is False
        assert actual["input_reason"] == "unresolved_units"
        response = api[0].post("/api/run?wait=1", json=body(api))
        assert response.status_code == 409, response.text
        assert response.json()["reason_code"] == "unresolved_units"
        assert not jobs._query("SELECT job_id FROM jobs")


def test_api_run_end_to_end(api):
    # R7 resolves the new registry record through the real catalog fold.
    response = api[0].post("/api/run?wait=1", json=body(api))
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True
    result = env["result"]
    rec = jobs.get_job(result["job_id"])
    assert rec["status"] == "complete"
    assert rec["dwg_version"] == 1
    assert rec["params"] == {"drawing_id": "solar", "zone_name": "Roof"}
    assert result["output"] == SELECTED
    assert (result["output_bytes"], result["output_sha256"]) == CANONICAL_OUTPUTS["selected"]
    assert result["request_sha256"] == "df0bc999488ca7eaffc5192d55b11f175876fd4124f0fd64bbfe50ac91402b03"
    provenance = env["execution_provenance"]
    assert provenance["execution_path"] == "local"
    receipt = local.graph_read_provenance(
        result, rec["params"], TENANT, rec["job_id"], TOOL, 1, backend=api[1])
    for key, value in receipt.items():
        assert provenance[key] == rec["provenance"][key] == value
    assert len(api[4]["requests"]) == 1
    manifest = store.load_manifest(api[1], TENANT, "solar")
    assert manifest["head"] == manifest["latest"] == 1


def test_api_run_pins_one_head_for_readiness_and_execution(api, monkeypatch):
    from types import SimpleNamespace
    from routers import jobs as route

    head_reads = []
    versions = []
    evaluate = entitlements.w1_tool_availability

    def changing_manifest(backend, tenant_id, drawing_id):
        head_reads.append((backend, tenant_id, drawing_id))
        return {"head": 1 if len(head_reads) == 1 else 2}

    def availability_for_version(*args, **kwargs):
        versions.append(kwargs["version"])
        return evaluate(*args, **kwargs)

    monkeypatch.setattr(route, "_store", lambda: SimpleNamespace(
        ANONYMOUS_HOLDER=store.ANONYMOUS_HOLDER, load_manifest=changing_manifest))
    monkeypatch.setattr(entitlements, "w1_tool_availability", availability_for_version)
    response = api[0].post("/api/run?wait=1", json=body(api))
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    rec = jobs.get_job(result["job_id"])
    assert rec["status"] == "complete"
    assert versions == [rec["dwg_version"]] == [1]
    assert result["source_version"] == 1
    assert head_reads == [(api[1], TENANT, "solar")]


@pytest.mark.parametrize("headers", [{}, {"X-Checkout-Capability": "stale-capability"}])
def test_api_run_reads_under_a_held_checkout(api, headers):
    # R8 includes an irrelevant stale checkout header.
    with held(api[1]):
        response = api[0].post("/api/run?wait=1", json=body(api), headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["result"]["output"] == SELECTED
        assert store.load_manifest(api[1], TENANT, "solar")["head"] == 1


@pytest.mark.parametrize("case,fixture,reason", [
    ("R9", "api", "DRAWING_ID_CONFLICT"),
    ("R10", "no_zones_api", "electrical_zones_required"),
])
def test_api_run_refusals(request, case, fixture, reason):
    api = request.getfixturevalue(fixture)
    payload = body(api)
    if case == "R9":
        payload["params"]["drawing_id"] = "other"
    response = api[0].post("/api/run?wait=1", json=payload)
    assert response.status_code == 409, response.text
    assert response.json()["reason_code"] == reason
    assert not jobs._query("SELECT job_id FROM jobs")
    assert not api[4]["requests"]


def test_worker_rejects_a_tampered_read_receipt(api):
    # R11: the real broker succeeded; only its returned receipt is forged.
    api[4]["tamper"] = True
    response = api[0].post("/api/run?wait=1", json=body(api))
    assert response.status_code == 500, response.text
    assert response.json()["error"]["message"] == "graph read terminal proof rejected"
    rows = jobs._query("SELECT job_id FROM jobs")
    assert len(rows) == 1
    rec = jobs.get_job(rows[0]["job_id"])
    assert rec["status"] == "failed"
    assert rec["error"]["message"] == "graph read terminal proof rejected"
    assert len(api[4]["requests"]) == 1
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 1
