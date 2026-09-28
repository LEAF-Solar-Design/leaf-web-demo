"""W3 AC voltage drop through Studio's registry, read adapter and broker."""
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
TOOL = "solar-nec-ac-voltage-drop"
KEYS = ("current_a", "one_way_length_ft", "r_ohm_per_1000ft", "x_ohm_per_1000ft",
        "power_factor", "phase", "source_voltage")
PARAMS = {"current_a": 10.0, "one_way_length_ft": 100.0, "r_ohm_per_1000ft": 0.5,
          "x_ohm_per_1000ft": 0.1, "power_factor": 1.0, "phase": 1, "source_voltage": 240.0}
V1_SHA = "312f4369740ac74a1558963ceaa77bae31c08ae36c8d6d2b97f43495b1281cac"
V1_FORMULA = ("VD% = (2 × I × (R·cosφ + X·sinφ) × L / 1000) / V "
              "× 100 = (2 × 10 × (0.5·1 + 0.1·0.000) × 100 / 1000) "
              "/ 240 × 100")
V1 = {
    "article": "NEC 210.19(A)(4) Informational Note 4 (AC)",
    "short_description": "AC voltage drop (1-phase, recommended <= 3% for inverter output)",
    "formula": V1_FORMULA,
    "inputs": {"I": 10.0, "L_ft": 100.0, "R_ohm_per_1000ft": 0.5, "X_ohm_per_1000ft": 0.1,
               "powerFactor": 1.0, "phase": 1.0, "V_source": 240.0},
    "result": 0.4166666666666667,
    "units": "% (3% recommended max)",
    "rejected_alternatives": [],
    "source_url": "https://www.nfpa.org/codes-and-standards/nfpa-70",
    "one_liner": ("NEC 210.19(A)(4) Informational Note 4 (AC) — AC voltage drop (1-phase, "
                  "recommended <= 3% for inverter output): " + V1_FORMULA
                  + " = 0.416667 % (3% recommended max)"),
}
DECLARATION = {
    "schema": "leaf.solar-tool.v1",
    "name": "solar-nec-ac-voltage-drop",
    "builtin": "builtins/solar_nec_ac_voltage_drop.py",
    "family": "equipment",
    "adapter": "local-graph-read",
    "entitlement": "run_read",
    "requires_persisted_graph": True,
    "seedable": False,
    "invalid_request_code": "INVALID_VOLTAGE_DROP_REQUEST",
    "readiness": {"kind": "facets", "facets": []},
    "engine": "server-builtin",
    "interaction": {"mode": "form"},
    "record_store": "registry",
    "record": {
        "name": "solar-nec-ac-voltage-drop",
        "version": "1.0.0",
        "description": "NEC 210.19(A)(4) AC voltage drop with reactance, power factor and phase factor, as a cited calculation.",
        "kind": "script",
        "family_id": "equipment",
        "engine_op": "solar_nec_ac_voltage_drop",
        "entry": "builtins/solar_nec_ac_voltage_drop.py",
        "params": {
            "type": "object",
            "properties": {
                "drawing_id": {"type": "string", "maxLength": 128},
                "current_a": {"type": "number", "minimum": 0, "maximum": 10000},
                "one_way_length_ft": {"type": "number", "minimum": 0, "maximum": 100000},
                "r_ohm_per_1000ft": {"type": "number", "minimum": 0, "maximum": 100},
                "x_ohm_per_1000ft": {"type": "number", "minimum": 0, "maximum": 100},
                "power_factor": {"type": "number", "minimum": -10, "maximum": 10},
                "phase": {"type": "integer", "enum": [1, 3]},
                "source_voltage": {"type": "number", "minimum": 0, "maximum": 100000},
            },
            "required": list(KEYS),
            "additionalProperties": False,
        },
        "returns": {"type": "object"},
        "capabilities": ["drawing.read"],
        "allow_local_fallback": False,
    },
    "ledger": ["nec-ac-voltage-drop"],
    "trusted_inputs": [],
    "maturity": "production",
    "wave": 3,
    "order": 20,
    "scenario": "w3-ground-electrical",
}


def test_declaration_pins():
    path = SERVER / "solar_tools/solar_nec_ac_voltage_drop.json"
    assert json.loads(path.read_text(encoding="utf-8")) == DECLARATION
    assert solar_tools.get(TOOL) == DECLARATION
    assert solar_tools.load().get(TOOL) == DECLARATION
    assert solar_tools.trusted_record(TOOL) == DECLARATION["record"]


@pytest.mark.parametrize("case,values,result,size,sha", [
    ("V1", (10, 100, 0.5, 0.1, 1, 1, 240), 0.4166666666666667, 963, V1_SHA),
    ("V2", (50, 200, 0.3, 0.1, 1, 3, 480), 1.0825317547305482, 986,
     "9de426172e2a57b89d2fd46536f54bd066c8c2f5541cd1c9f7391ad451ab9bb0"),
    ("V3", (30, 150, 0.4, 0.08, 0.95, 1, 208), 1.7523172730492027, 972,
     "ec2e2fcd82d000f52a33c4bd671b13d367f2dd91778b65223db693a624f232fa"),
    ("V4-high", (10, 100, 0.5, 0.1, 1.2, 1, 240), 0.4166666666666667, 961,
     "f3eef3219b453e07af30ad1c898cb6872dce56d025e64fcc0b44deef5cee8ca9"),
    ("V4-negative", (10, 100, 0.5, 0.1, -0.5, 1, 240), 0.08333333333333334, 963,
     "8424a53c933ec64b2272d7cc54805c58e2a246a8a4f1f33fa605cc5790db2425"),
    ("V5", (50, 200, 0.3, 0.1, 1, 3, 0), 0.0, 959,
     "21ec14c45a8f7cd05285631427dc102019bcc413b70902b8640bb91e9aab611a"),
    ("V6", (10000, 100000, 100, 100, 1, 1, 1), 20000000000.0, 971,
     "f9fac62f2a63a04012c41e0fdfc79cf1f868422a42b2d6dbe9d8abad65e1281f"),
])
def test_builtin_cases(graph, case, values, result, size, sha):
    params = dict(zip(KEYS, values))
    output = local._load_builtin(TOOL).run(graph, params)
    assert set(output) == set(V1)
    assert len(output) == 9
    assert output["result"] == result
    assert output["rejected_alternatives"] == []
    assert len(canonical_bytes(output)) == size
    assert digest(output) == sha
    if case == "V1":
        assert output == V1
    elif case == "V2":
        assert "√3" in output["formula"]
        assert output["short_description"].startswith("AC voltage drop (3-phase,")


def test_builtin_int_equals_float(graph):
    builtin = local._load_builtin(TOOL)
    integer = builtin.run(graph, dict(PARAMS, current_a=10))
    floating = builtin.run(graph, PARAMS)
    assert integer == floating == V1
    assert canonical_bytes(integer) == canonical_bytes(floating)
    assert len(canonical_bytes(integer)) == 963
    assert digest(integer) == digest(floating) == V1_SHA


@pytest.mark.parametrize("params,code", [
    pytest.param([], "INVALID_VOLTAGE_DROP_REQUEST", id="V8-list"),
    pytest.param(None, "INVALID_VOLTAGE_DROP_REQUEST", id="V8-null"),
    pytest.param({}, "INVALID_VOLTAGE_DROP_REQUEST", id="V8-empty"),
    pytest.param({k: v for k, v in PARAMS.items() if k != "phase"},
                 "INVALID_VOLTAGE_DROP_REQUEST", id="V8-missing-phase"),
    pytest.param(dict(PARAMS, drawing_id="solar"), "INVALID_VOLTAGE_DROP_REQUEST", id="V8-drawing"),
    pytest.param(dict(PARAMS, phase=2), "INVALID_VOLTAGE_DROP_REQUEST", id="V8-phase-2"),
    pytest.param(dict(PARAMS, phase=1.0), "INVALID_VOLTAGE_DROP_REQUEST", id="V8-phase-float"),
    pytest.param(dict(PARAMS, phase=True), "INVALID_VOLTAGE_DROP_REQUEST", id="V8-phase-bool"),
    pytest.param(dict(PARAMS, current_a="10"), "INVALID_VOLTAGE_DROP_REQUEST", id="V8-string"),
    pytest.param(dict(PARAMS, current_a=float("nan")), "INVALID_VOLTAGE_DROP_REQUEST", id="V8-nan"),
    pytest.param(dict(PARAMS, current_a=-1), "VOLTAGE_DROP_INPUT_OUT_OF_RANGE", id="V9-negative"),
    pytest.param(dict(PARAMS, current_a=-0.0), "VOLTAGE_DROP_INPUT_OUT_OF_RANGE", id="V9-negative-zero"),
    pytest.param(dict(PARAMS, current_a=10000.5), "VOLTAGE_DROP_INPUT_OUT_OF_RANGE", id="V9-current"),
    pytest.param(dict(PARAMS, current_a=10**400), "VOLTAGE_DROP_INPUT_OUT_OF_RANGE", id="V9-huge"),
    pytest.param(dict(PARAMS, source_voltage=0.5), "VOLTAGE_DROP_INPUT_OUT_OF_RANGE", id="V9-tiny-voltage"),
    pytest.param(dict(PARAMS, power_factor=10.5), "VOLTAGE_DROP_INPUT_OUT_OF_RANGE", id="V9-pf"),
    pytest.param(dict(PARAMS, r_ohm_per_1000ft=100.1), "VOLTAGE_DROP_INPUT_OUT_OF_RANGE", id="V9-r"),
])
def test_builtin_refusals_are_named(graph, params, code):
    with pytest.raises(GraphValidationError) as exc:
        local._load_builtin(TOOL).run(graph, params)
    assert exc.value.code == code


def test_builtin_is_pure(graph):
    params = copy.deepcopy(PARAMS)
    original_graph, original_params = copy.deepcopy(graph), copy.deepcopy(params)
    builtin = local._load_builtin(TOOL)
    first = builtin.run(graph, params)
    second = builtin.run(graph, params)
    assert first == second == V1
    assert builtin.run(object(), params) == V1
    first["inputs"].clear()
    first["rejected_alternatives"].append("changed")
    assert second == V1
    assert graph == original_graph
    assert params == original_params


def test_parity_receipt_replays(graph):
    path = Path(__file__).resolve().parents[2] / "docs/parity/receipts/nec-ac-voltage-drop/demo-probes.json"
    receipt = json.loads(path.read_text(encoding="utf-8"))
    assert receipt["capability"] == "nec-ac-voltage-drop"
    assert receipt["comparator"]["verdict"] == "pass"
    rows = receipt["comparison"]["plugin"]["after"]["rows"]
    assert len(rows) == 11
    for row in rows:
        f = row["fields"]
        params = {"current_a": f["I_in"], "one_way_length_ft": f["L_in"],
                  "r_ohm_per_1000ft": f["R_in"], "x_ohm_per_1000ft": f["X_in"],
                  "power_factor": f["PF_in"], "phase": f["Phase_in"],
                  "source_voltage": f["V_in"]}
        output = local._load_builtin(TOOL).run(graph, params)
        assert output["article"] == f["Article"]
        assert output["short_description"] == f["ShortDescription"]
        assert output["formula"] == f["Formula"]
        assert output["result"] == f["Result"]
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
        "schema": "leaf.solar-tool-view.v1", "name": "solar-nec-ac-voltage-drop",
        "family": "equipment", "wave": 3, "order": 20, "maturity": "production",
        "engine": "server-builtin", "adapter": "local-graph-read", "entitlement": "run_read",
        "interaction": {"mode": "form"}, "ledger": ["nec-ac-voltage-drop"],
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
    assert result["output"] == V1
    assert result["output_sha256"] == V1_SHA
    assert result["output_bytes"] == 963
    assert env["execution_provenance"]["execution_path"] == "local"
    assert len(api[4]) == 1
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 1


@pytest.mark.parametrize("change", [
    {"phase": 2}, {"current_a": True}, {"x": 1},
], ids=["A6-phase-2", "A6-bool", "A6-extra"])
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
    assert "VOLTAGE_DROP_INPUT_OUT_OF_RANGE" not in response.text
    assert "INVALID_VOLTAGE_DROP_REQUEST" not in response.text
    assert len(api[4]) == 1
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 1
