"""W3 ampacity correction through Studio's registry, read adapter and broker."""
import copy
import json
from pathlib import Path

import pytest

import broker_client
import catalog
import deps
import entitlements
import jobs
import product_capability_availability as availability
import solar_local_read as local
import solar_tools
import store
import write_loop
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError
from solar_sizing_client import digest
from test_w1_design_graph import graph  # noqa: F401
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_solve_commit import seed

SERVER = Path(__file__).resolve().parents[1]
TENANT = "fixture-tenant"
TOOL = "solar-nec-ampacity-correction"
PARAMS = {"base_ampacity": 100.0, "temp_factor": 0.82, "conduit_factor": 0.7}
C1_SHA = "af0d667d1b2824ff985022fb52a6cc1907d9fc42c9abebbf97b124d942fc3fd4"
C1 = {
    "article": "NEC 310.16",
    "short_description": "Conductor ampacity with temperature and conduit-fill correction",
    "formula": "I_corrected = I_base \u00d7 temp_factor \u00d7 conduit_factor = 100 \u00d7 0.82 \u00d7 0.7",
    "inputs": {"I_base": 100.0, "temp_factor": 0.82, "conduit_factor": 0.7},
    "result": 57.4,
    "units": "A",
    "rejected_alternatives": [{
        "description": "I_base with no temperature correction",
        "would_have_resulted_in": 70.0,
        "why_rejected": "Required when ambient exceeds 30\u00b0C.",
    }],
    "source_url": "https://www.nfpa.org/codes-and-standards/nfpa-70",
    "one_liner": "NEC 310.16 \u2014 Conductor ampacity with temperature and conduit-fill correction: I_corrected = I_base \u00d7 temp_factor \u00d7 conduit_factor = 100 \u00d7 0.82 \u00d7 0.7 = 57.4 A",
}
DECLARATION = {
    "schema": "leaf.solar-tool.v1",
    "name": "solar-nec-ampacity-correction",
    "builtin": "builtins/solar_nec_ampacity_correction.py",
    "family": "equipment",
    "adapter": "local-graph-read",
    "entitlement": "run_read",
    "requires_persisted_graph": True,
    "seedable": False,
    "invalid_request_code": "INVALID_AMPACITY_REQUEST",
    "readiness": {"kind": "facets", "facets": []},
    "engine": "server-builtin",
    "interaction": {"mode": "form"},
    "record_store": "registry",
    "record": {
        "name": "solar-nec-ampacity-correction",
        "version": "1.0.0",
        "description": "NEC 310.16 conductor ampacity with temperature and conduit-fill correction, as a cited calculation.",
        "kind": "script",
        "family_id": "equipment",
        "engine_op": "solar_nec_ampacity_correction",
        "entry": "builtins/solar_nec_ampacity_correction.py",
        "params": {
            "type": "object",
            "properties": {
                "drawing_id": {"type": "string", "maxLength": 128},
                "base_ampacity": {"type": "number", "minimum": 0, "maximum": 10000},
                "temp_factor": {"type": "number", "minimum": 0, "maximum": 10},
                "conduit_factor": {"type": "number", "minimum": 0, "maximum": 10},
            },
            "required": ["base_ampacity", "temp_factor", "conduit_factor"],
            "additionalProperties": False,
        },
        "returns": {"type": "object"},
        "capabilities": ["drawing.read"],
        "allow_local_fallback": False,
    },
    "ledger": ["nec-ampacity-correction"],
    "trusted_inputs": [],
    "maturity": "production",
    "wave": 3,
    "order": 10,
    "scenario": "w3-ground-electrical",
}


def test_declaration_pins():
    path = SERVER / "solar_tools/solar_nec_ampacity_correction.json"
    assert json.loads(path.read_text(encoding="utf-8")) == DECLARATION
    assert solar_tools.get(TOOL) == DECLARATION
    assert solar_tools.load().get(TOOL) == DECLARATION
    assert solar_tools.trusted_record(TOOL) == DECLARATION["record"]


@pytest.mark.parametrize("case,values,result,rejected,size,sha", [
    ("C1", (100.0, 0.82, 0.7), 57.4, 70.0, 736, C1_SHA),
    ("C3", (600, 0.88, 0.8), 422.40000000000003, 480.0, 752,
     "dca183cf7fb1c846d3c338a10fe9a06e92a26b714fe95e6828e92288e31c18e9"),
    ("C4", (1, 1, 1), 1.0, 1.0, 714,
     "0b91122795f41ff1e6b8bd4393d47563a1717177cc573ce72da450e8297e23a9"),
    ("C5", (0, 0.82, 0.7), 0.0, 0.0, 725,
     "402a645a539b779afd3528f64acef66c2151eeb84bae67879b4b9f3f465f0d12"),
    ("C6", (10000, 10, 10), 1000000.0, 100000.0, 747,
     "39658e32bc678ee0a5f86a3e98e5510f6f7e8914db92655ee0c1a5aec9a7adcb"),
    ("C7", (40, 1.05, 1), 42.0, 40.0, 727,
     "02fccbd9a9a05b8e80430bce488e5f7b2bfc33015095cd292e229d0e5c21ea97"),
])
def test_builtin_cases(graph, case, values, result, rejected, size, sha):
    params = dict(zip(("base_ampacity", "temp_factor", "conduit_factor"), values))
    output = local._load_builtin(TOOL).run(graph, params)
    assert set(output) == set(C1)
    assert len(output) == 9
    assert output["result"] == result
    assert output["rejected_alternatives"][0]["would_have_resulted_in"] == rejected
    assert len(canonical_bytes(output)) == size
    assert digest(output) == sha
    if case == "C1":
        assert output == C1
    elif case == "C3":
        assert repr(output["result"]) == "422.40000000000003"
        assert output["one_liner"].endswith("= 422.4 A")
    elif case == "C4":
        assert output["formula"].endswith("= 1 \u00d7 1 \u00d7 1")


def test_builtin_int_equals_float(graph):
    builtin = local._load_builtin(TOOL)
    integer = builtin.run(graph, dict(PARAMS, base_ampacity=100))
    floating = builtin.run(graph, PARAMS)
    assert integer == floating == C1
    assert canonical_bytes(integer) == canonical_bytes(floating)
    assert len(canonical_bytes(integer)) == 736
    assert digest(integer) == digest(floating) == C1_SHA


@pytest.mark.parametrize("params,code", [
    pytest.param([], "INVALID_AMPACITY_REQUEST", id="C8-list"),
    pytest.param(None, "INVALID_AMPACITY_REQUEST", id="C8-null"),
    pytest.param({}, "INVALID_AMPACITY_REQUEST", id="C8-empty"),
    pytest.param({"base_ampacity": 100.0, "temp_factor": 0.82},
                 "INVALID_AMPACITY_REQUEST", id="C8-missing"),
    pytest.param(dict(PARAMS, drawing_id="solar"), "INVALID_AMPACITY_REQUEST", id="C8-drawing"),
    pytest.param(dict(PARAMS, cancel=True), "INVALID_AMPACITY_REQUEST", id="C8-cancel"),
    pytest.param(dict(PARAMS, base_ampacity=True), "INVALID_AMPACITY_REQUEST", id="C9-bool"),
    pytest.param(dict(PARAMS, base_ampacity="100"), "INVALID_AMPACITY_REQUEST", id="C9-string"),
    pytest.param(dict(PARAMS, base_ampacity=None), "INVALID_AMPACITY_REQUEST", id="C9-null"),
    pytest.param(dict(PARAMS, base_ampacity=float("nan")), "INVALID_AMPACITY_REQUEST", id="C9-nan"),
    pytest.param(dict(PARAMS, base_ampacity=float("inf")), "INVALID_AMPACITY_REQUEST", id="C9-inf"),
    pytest.param(dict(PARAMS, base_ampacity=-1), "AMPACITY_INPUT_OUT_OF_RANGE", id="C10-negative"),
    pytest.param(dict(PARAMS, base_ampacity=-0.0), "AMPACITY_INPUT_OUT_OF_RANGE", id="C10-negative-zero"),
    pytest.param(dict(PARAMS, base_ampacity=10000.5), "AMPACITY_INPUT_OUT_OF_RANGE", id="C10-base"),
    pytest.param(dict(PARAMS, base_ampacity=10**400), "AMPACITY_INPUT_OUT_OF_RANGE", id="C10-huge"),
    pytest.param(dict(PARAMS, temp_factor=10.000001), "AMPACITY_INPUT_OUT_OF_RANGE", id="C10-temp"),
    pytest.param(dict(PARAMS, conduit_factor=11), "AMPACITY_INPUT_OUT_OF_RANGE", id="C10-conduit"),
])
def test_builtin_refusals_are_named(graph, params, code):
    with pytest.raises(GraphValidationError) as exc:
        local._load_builtin(TOOL).run(graph, params)
    assert exc.value.code == code


@pytest.mark.parametrize("key,value,result", [
    ("base_ampacity", 0, 0.0),
    ("base_ampacity", 10000, 10000.0),
    ("temp_factor", 0, 0.0),
    ("temp_factor", 10, 10.0),
    ("conduit_factor", 0, 0.0),
    ("conduit_factor", 10, 10.0),
])
def test_builtin_bounds_are_inclusive(graph, key, value, result):
    params = {"base_ampacity": 1, "temp_factor": 1, "conduit_factor": 1}
    params[key] = value
    assert local._load_builtin(TOOL).run(graph, params)["result"] == result


def test_builtin_is_pure(graph):
    params = copy.deepcopy(PARAMS)
    original_graph, original_params = copy.deepcopy(graph), copy.deepcopy(params)
    builtin = local._load_builtin(TOOL)
    first = builtin.run(graph, params)
    second = builtin.run(graph, params)
    assert first == second == C1
    assert builtin.run(object(), params) == C1
    first["inputs"].clear()
    first["rejected_alternatives"][0]["description"] = "changed"
    assert second == C1
    assert graph == original_graph
    assert params == original_params


def test_parity_receipt_replays(graph):
    path = Path(__file__).resolve().parents[2] / "docs/parity/receipts/nec-ampacity-correction/demo-probes.json"
    receipt = json.loads(path.read_text(encoding="utf-8"))
    assert receipt["capability"] == "nec-ampacity-correction"
    assert receipt["comparator"]["verdict"] == "pass"
    rows = receipt["comparison"]["plugin"]["after"]["rows"]
    assert len(rows) == 11
    for row in rows:
        f = row["fields"]
        params = {"base_ampacity": f["BaseA"], "temp_factor": f["TempFactor"],
                  "conduit_factor": f["ConduitFactor"]}
        output = local._load_builtin(TOOL).run(graph, params)
        assert output["article"] == f["Article"]
        assert output["formula"] == f["Formula"]
        assert output["result"] == f["Result"]
        assert output["rejected_alternatives"][0]["would_have_resulted_in"] == f["RejectedResult"]
        assert output["units"] == f["Units"]


def test_registry_and_catalog_view(api):
    assert solar_tools.get(TOOL) == DECLARATION
    assert TOOL in solar_tools.local_graph_read_tools()
    assert TOOL not in solar_tools.local_graph_tools()
    assert availability.SOLAR_CAPABILITIES[TOOL]["adapter"] == "local-graph-read"
    assert TOOL not in availability.W1_CAPABILITIES
    assert entitlements.tool_required_capability(solar_tools.trusted_record(TOOL)) == "run_read"
    families = catalog.build_catalog(deps.all_tools(TENANT))
    matches = [(family, row) for family in families for row in family["capabilities"]
               if row["name"] == TOOL]
    assert len(matches) == 1
    family, row = matches[0]
    assert family["family_id"] == "equipment"
    assert row["solar"] == {
        "schema": "leaf.solar-tool-view.v1", "name": "solar-nec-ampacity-correction",
        "family": "equipment", "wave": 3, "order": 10, "maturity": "production",
        "engine": "server-builtin", "adapter": "local-graph-read", "entitlement": "run_read",
        "interaction": {"mode": "form"}, "ledger": ["nec-ampacity-correction"],
    }


def test_availability_ready_and_reasons(graph, api):
    assert availability.w1_local_commit_inputs(graph)[TOOL] == {
        "input_ready": True, "input_reason": None}
    unresolved = copy.deepcopy(graph)
    unresolved["project"]["units"]["meters_per_unit"] *= 2
    assert availability.w1_local_commit_inputs(unresolved)[TOOL] == {
        "input_ready": False, "input_reason": "unresolved_units"}
    actual = entitlements.w1_tool_availability({"name": TOOL}, api[3], "solar")
    assert actual["engine_ready"] is True
    assert actual["input_ready"] is True
    assert actual["runnable"] is True


@pytest.fixture
def api(isolated_jobs, no_network, graph, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import jobs as route

    backend, _ = seed(tmp_path, monkeypatch, graph)
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
    requests = []

    def transport(url, *, json, headers, timeout):
        assert url.endswith("/broker/run")
        assert json["aps_live"] is False
        assert json["checkout_holder"] == store.ANONYMOUS_HOLDER
        assert json["checkout_fence"] is None
        requests.append(copy.deepcopy(json))
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
    with TestClient(app) as client:
        yield client, backend, record, tenant, requests


def body(api):
    return {"tool": TOOL, "dwg": "solar", "params": copy.deepcopy(PARAMS),
            "catalog_digest": deps.catalog_tool_digest(api[2])}


def test_api_run_end_to_end(api):
    response = api[0].post("/api/run?wait=1", json=body(api))
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True
    result = env["result"]
    rec = jobs.get_job(result["job_id"])
    assert rec["status"] == "complete"
    assert rec["dwg_version"] == 1
    assert result["output"] == C1
    assert result["output_sha256"] == C1_SHA
    assert result["output_bytes"] == 736
    assert result["request_sha256"] == "46eeaf4819647267f7c19bbce6a5c9ebd96d49632962f4a2926e23a3987525b2"
    assert env["execution_provenance"]["execution_path"] == "local"
    assert len(api[4]) == 1
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 1


@pytest.mark.parametrize("change", [
    {"temp_factor": 11}, {"base_ampacity": True}, {"x": 1},
], ids=["A6-maximum", "A6-bool", "A6-extra"])
def test_api_run_refusals(api, change):
    payload = body(api)
    payload["params"].update(change)
    response = api[0].post("/api/run?wait=1", json=payload)
    env = response.json()
    assert env.get("ok") is not True
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert all(rec["status"] != "complete" for rec in records)
    errors = [env.get("error", {})] + [rec.get("error") or {} for rec in records]
    assert any(error.get("reason_code") == "tool_params_invalid" for error in errors), env
    assert "AMPACITY_INPUT_OUT_OF_RANGE" not in response.text
    assert "INVALID_AMPACITY_REQUEST" not in response.text
    assert len(api[4]) == 1
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 1
