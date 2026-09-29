"""W3 feeder OCPD sizing through Studio's registry, read adapter and broker."""
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
TOOL = "solar-nec-feeder-ocpd"
KEYS = ("continuous_current_a", "is_continuous", "egc_material")
PARAMS = {"continuous_current_a": 20.0, "is_continuous": True, "egc_material": "Copper"}
F1_SHA = "b781885b7074ea7a163b174e0ddd933ee50a3da9f64c729b0e0e7e1b638eb44e"
DECLARATION = {
  "schema": "leaf.solar-tool.v1",
  "name": "solar-nec-feeder-ocpd",
  "builtin": "builtins/solar_nec_feeder_ocpd.py",
  "family": "equipment",
  "adapter": "local-graph-read",
  "entitlement": "run_read",
  "requires_persisted_graph": True,
  "seedable": False,
  "invalid_request_code": "INVALID_FEEDER_OCPD_REQUEST",
  "readiness": {"kind": "facets", "facets": []},
  "engine": "server-builtin",
  "interaction": {"mode": "form"},
  "record_store": "registry",
  "record": {
    "name": "solar-nec-feeder-ocpd",
    "version": "1.0.0",
    "description": "NEC 215.3 and 210.20(A) feeder overcurrent device rating with its NEC 250.122 equipment grounding conductor, as a cited calculation.",
    "kind": "script",
    "family_id": "equipment",
    "engine_op": "solar_nec_feeder_ocpd",
    "entry": "builtins/solar_nec_feeder_ocpd.py",
    "params": {
      "type": "object",
      "properties": {
        "drawing_id": {"type": "string", "maxLength": 128},
        "continuous_current_a": {"type": "number", "minimum": 0, "maximum": 10000},
        "is_continuous": {"type": "boolean"},
        "egc_material": {"type": "string", "enum": ["Copper", "Aluminum"], "maxLength": 8}
      },
      "required": ["continuous_current_a", "is_continuous", "egc_material"],
      "additionalProperties": False
    },
    "returns": {"type": "object"},
    "capabilities": ["drawing.read"],
    "allow_local_fallback": False
  },
  "ledger": ["nec-feeder-ocpd-sizing"],
  "trusted_inputs": [],
  "maturity": "production",
  "wave": 3,
  "order": 40,
  "scenario": "w3-ground-electrical"
}

CASES = [
    ("F1", (20, True, "Copper"), {
        "continuous_current_a": 20.0, "is_continuous": True,
        "egc_material": "Copper", "min_ocpd_a": 25.0, "ocpd_rating_a": 25.0,
        "egc_gauge": "10 AWG",
        "note": "NEC 215.3 \u2014 20.0A \u00d7 1.25 = 25.0A \u2192 25A OCPD; EGC: 10 AWG per NEC 250.122",
    }, 231, "b781885b7074ea7a163b174e0ddd933ee50a3da9f64c729b0e0e7e1b638eb44e"),
    ("F2", (20, True, "Aluminum"), {
        "continuous_current_a": 20.0, "is_continuous": True,
        "egc_material": "Aluminum", "min_ocpd_a": 25.0, "ocpd_rating_a": 25.0,
        "egc_gauge": "8 AWG",
        "note": "NEC 215.3 \u2014 20.0A \u00d7 1.25 = 25.0A \u2192 25A OCPD; EGC: 8 AWG per NEC 250.122",
    }, 231, "b202e239c57809d14d39c855cabcc9866a3ad6aca61116df3dbfa8d42b2faacb"),
    ("F3", (20, False, "Copper"), {
        "continuous_current_a": 20.0, "is_continuous": False,
        "egc_material": "Copper", "min_ocpd_a": 20.0, "ocpd_rating_a": 20.0,
        "egc_gauge": "12 AWG",
        "note": "NEC 210.20(A) \u2014 20.0A \u2192 20A OCPD; EGC: 12 AWG per NEC 250.122",
    }, 216, "c2e4af7b7dab9ecc7a01da4670095b8fb24e0807b60a137d17ea56cee50cffc0"),
    ("F4", (12.5, True, "Copper"), {
        "continuous_current_a": 12.5, "is_continuous": True,
        "egc_material": "Copper", "min_ocpd_a": 15.625, "ocpd_rating_a": 20.0,
        "egc_gauge": "12 AWG",
        "note": "NEC 215.3 \u2014 12.5A \u00d7 1.25 = 15.6A \u2192 20A OCPD; EGC: 12 AWG per NEC 250.122",
    }, 233, "e7e3c050de59bb1c0bdcf1494e192003234f79f7088b56695f2d7f4f8aa3b677"),
    ("F5", (7000, True, "Copper"), {
        "continuous_current_a": 7000.0, "is_continuous": True,
        "egc_material": "Copper", "min_ocpd_a": 8750.0, "ocpd_rating_a": 8750.0,
        "egc_gauge": None,
        "note": "NEC 215.3 \u2014 7000.0A \u00d7 1.25 = 8750.0A \u2192 8750A OCPD; EGC: N/A per NEC 250.122",
    }, 236, "00ffe88baf9aa1977b715a8a0e0dbb1d4d9f86a5828bbf87c0c43ed20fd38531"),
    ("F6", (7000, False, "Copper"), {
        "continuous_current_a": 7000.0, "is_continuous": False,
        "egc_material": "Copper", "min_ocpd_a": 7000.0, "ocpd_rating_a": 7000.0,
        "egc_gauge": None,
        "note": "NEC 210.20(A) \u2014 7000.0A \u2192 7000A OCPD; EGC: N/A per NEC 250.122",
    }, 219, "6aca2839c0ef8af71b7cad88133d49370b74d87533f4fceac3faa10c9113c17d"),
    ("F7", (0, True, "Copper"), {
        "continuous_current_a": 0.0, "is_continuous": True,
        "egc_material": "Copper", "min_ocpd_a": 0.0, "ocpd_rating_a": 15.0,
        "egc_gauge": "14 AWG",
        "note": "NEC 215.3 \u2014 0.0A \u00d7 1.25 = 0.0A \u2192 15A OCPD; EGC: 14 AWG per NEC 250.122",
    }, 227, "d25d50ef9e85d4df7867b008d408284fbc4d312ec4e04a22a197670316615988"),
    ("F8", (10000, True, "Aluminum"), {
        "continuous_current_a": 10000.0, "is_continuous": True,
        "egc_material": "Aluminum", "min_ocpd_a": 12500.0, "ocpd_rating_a": 12500.0,
        "egc_gauge": None,
        "note": "NEC 215.3 \u2014 10000.0A \u00d7 1.25 = 12500.0A \u2192 12500A OCPD; EGC: N/A per NEC 250.122",
    }, 244, "24d4c599268da7c5e3aa57c8215326363283a117ed80febec8ac7c9f06ac4f11"),
    ("F9", (4800.4, True, "Copper"), {
        "continuous_current_a": 4800.4, "is_continuous": True,
        "egc_material": "Copper", "min_ocpd_a": 6000.5, "ocpd_rating_a": 6000.5,
        "egc_gauge": None,
        "note": "NEC 215.3 \u2014 4800.4A \u00d7 1.25 = 6000.5A \u2192 6000.5A OCPD; EGC: N/A per NEC 250.122",
    }, 238, "1cf823b021e7c82debd96caa099ef0aa17039b42583e01bdb0c759d11346b19d"),
    ("F10", (100, False, "Aluminum"), {
        "continuous_current_a": 100.0, "is_continuous": False,
        "egc_material": "Aluminum", "min_ocpd_a": 100.0, "ocpd_rating_a": 100.0,
        "egc_gauge": "6 AWG",
        "note": "NEC 210.20(A) \u2014 100.0A \u2192 100A OCPD; EGC: 6 AWG per NEC 250.122",
    }, 221, "352e141f1bf35a48029c5d9959665f4b22a2644c19497b1635c0b441777ab07c"),
    ("F11", (4800, True, "Copper"), {
        "continuous_current_a": 4800.0, "is_continuous": True,
        "egc_material": "Copper", "min_ocpd_a": 6000.0, "ocpd_rating_a": 6000.0,
        "egc_gauge": "750 kcmil",
        "note": "NEC 215.3 \u2014 4800.0A \u00d7 1.25 = 6000.0A \u2192 6000A OCPD; EGC: 750 kcmil per NEC 250.122",
    }, 249, "4fc0bd5e5f73021f8fda87d1e2b72f838e038c6f170bd7ee2bc268993e11efde"),
    ("F12", (6000, False, "Aluminum"), {
        "continuous_current_a": 6000.0, "is_continuous": False,
        "egc_material": "Aluminum", "min_ocpd_a": 6000.0, "ocpd_rating_a": 6000.0,
        "egc_gauge": "1000 kcmil",
        "note": "NEC 210.20(A) \u2014 6000.0A \u2192 6000A OCPD; EGC: 1000 kcmil per NEC 250.122",
    }, 236, "f47d1e93d67a1e85cfb694b7c9eb8fdfa6de48039c29ed9426f008a79c40ff9d"),
    ("F13", (6000.5, False, "Copper"), {
        "continuous_current_a": 6000.5, "is_continuous": False,
        "egc_material": "Copper", "min_ocpd_a": 6000.5, "ocpd_rating_a": 6000.5,
        "egc_gauge": None,
        "note": "NEC 210.20(A) \u2014 6000.5A \u2192 6000.5A OCPD; EGC: N/A per NEC 250.122",
    }, 221, "c39cc86442cc4d4683214ec73da50c68b2a6a5508fdeae597de040fb53f53d47"),
    ("F14", (0.1, False, "Copper"), {
        "continuous_current_a": 0.1, "is_continuous": False,
        "egc_material": "Copper", "min_ocpd_a": 0.1, "ocpd_rating_a": 15.0,
        "egc_gauge": "14 AWG",
        "note": "NEC 210.20(A) \u2014 0.1A \u2192 15A OCPD; EGC: 14 AWG per NEC 250.122",
    }, 213, "8ff6636d25acc704d77c315ce328e0a9abb2b5c3b8b5fa32a180a0221c2747af"),
]
F1 = CASES[0][2]


def test_feeder_ocpd_declaration_pins():
    path = SERVER / "solar_tools/solar_nec_feeder_ocpd.json"
    assert json.loads(path.read_text(encoding="utf-8")) == DECLARATION
    assert solar_tools.get(TOOL) == DECLARATION
    assert solar_tools.load().get(TOOL) == DECLARATION
    assert solar_tools.trusted_record(TOOL) == DECLARATION["record"]


@pytest.mark.parametrize("case,values,expected,size,sha", CASES)
def test_feeder_ocpd_builtin_cases(graph, case, values, expected, size, sha):
    output = local._load_builtin(TOOL).run(graph, dict(zip(KEYS, values)))
    assert output == expected
    assert len(output) == 7
    assert len(canonical_bytes(output)) == size
    assert digest(output) == sha


def test_feeder_ocpd_int_equals_float(graph):
    builtin = local._load_builtin(TOOL)
    integer = builtin.run(graph, dict(PARAMS, continuous_current_a=20))
    floating = builtin.run(graph, PARAMS)
    assert integer == floating == F1
    assert canonical_bytes(integer) == canonical_bytes(floating)
    assert len(canonical_bytes(integer)) == 231
    assert digest(integer) == digest(floating) == F1_SHA
    integer = builtin.run(graph, dict(PARAMS, continuous_current_a=20, is_continuous=False))
    floating = builtin.run(graph, dict(PARAMS, is_continuous=False))
    assert integer == floating == CASES[2][2]
    assert canonical_bytes(integer) == canonical_bytes(floating)
    assert len(canonical_bytes(integer)) == 216
    assert digest(integer) == digest(floating) == CASES[2][4]


@pytest.mark.parametrize("params,code", [
    ([], "INVALID_FEEDER_OCPD_REQUEST"),
    (None, "INVALID_FEEDER_OCPD_REQUEST"),
    ({}, "INVALID_FEEDER_OCPD_REQUEST"),
    ({k: v for k, v in PARAMS.items() if k != "is_continuous"}, "INVALID_FEEDER_OCPD_REQUEST"),
    (dict(PARAMS, drawing_id="solar"), "INVALID_FEEDER_OCPD_REQUEST"),
    (dict(PARAMS, extra=1), "INVALID_FEEDER_OCPD_REQUEST"),
    (dict(PARAMS, is_continuous=1), "INVALID_FEEDER_OCPD_REQUEST"),
    (dict(PARAMS, is_continuous="true"), "INVALID_FEEDER_OCPD_REQUEST"),
    (dict(PARAMS, is_continuous=None), "INVALID_FEEDER_OCPD_REQUEST"),
    (dict(PARAMS, egc_material="copper"), "INVALID_FEEDER_OCPD_REQUEST"),
    (dict(PARAMS, egc_material="Copper "), "INVALID_FEEDER_OCPD_REQUEST"),
    (dict(PARAMS, egc_material=1), "INVALID_FEEDER_OCPD_REQUEST"),
    (dict(PARAMS, egc_material=None), "INVALID_FEEDER_OCPD_REQUEST"),
    (dict(PARAMS, continuous_current_a="20"), "INVALID_FEEDER_OCPD_REQUEST"),
    (dict(PARAMS, continuous_current_a=None), "INVALID_FEEDER_OCPD_REQUEST"),
    (dict(PARAMS, continuous_current_a=True), "INVALID_FEEDER_OCPD_REQUEST"),
    (dict(PARAMS, continuous_current_a=float("nan")), "INVALID_FEEDER_OCPD_REQUEST"),
    (dict(PARAMS, continuous_current_a=float("inf")), "INVALID_FEEDER_OCPD_REQUEST"),
    (dict(PARAMS, continuous_current_a=-1), "FEEDER_OCPD_INPUT_OUT_OF_RANGE"),
    (dict(PARAMS, continuous_current_a=-0.5), "FEEDER_OCPD_INPUT_OUT_OF_RANGE"),
    (dict(PARAMS, continuous_current_a=-0.0), "FEEDER_OCPD_INPUT_OUT_OF_RANGE"),
    (dict(PARAMS, continuous_current_a=10000.5), "FEEDER_OCPD_INPUT_OUT_OF_RANGE"),
    (dict(PARAMS, continuous_current_a=10001), "FEEDER_OCPD_INPUT_OUT_OF_RANGE"),
    (dict(PARAMS, continuous_current_a=10**400), "FEEDER_OCPD_INPUT_OUT_OF_RANGE"),
])
def test_feeder_ocpd_refusals_are_named(graph, params, code):
    with pytest.raises(GraphValidationError) as exc:
        local._load_builtin(TOOL).run(graph, params)
    assert exc.value.code == code


def test_feeder_ocpd_builtin_is_pure(graph):
    params = copy.deepcopy(PARAMS)
    original_graph, original_params = copy.deepcopy(graph), copy.deepcopy(params)
    builtin = local._load_builtin(TOOL)
    first = builtin.run(graph, params)
    second = builtin.run(graph, params)
    assert first == second == F1
    assert builtin.run(object(), params) == F1
    first["note"] = "changed"
    assert second == F1
    assert graph == original_graph
    assert params == original_params


def test_feeder_ocpd_parity_receipt_replays(graph):
    path = Path(__file__).resolve().parents[2] / "docs/parity/receipts/nec-feeder-ocpd-sizing/demo-probes.json"
    receipt = json.loads(path.read_text(encoding="utf-8"))
    assert receipt["capability"] == "nec-feeder-ocpd-sizing"
    assert receipt["comparator"]["verdict"] == "pass"
    rows = receipt["comparison"]["plugin"]["after"]["rows"]
    assert len(rows) == 11
    for row in rows:
        f = row["fields"]
        params = {"continuous_current_a": f["ContinuousCurrentA"],
                  "is_continuous": f["IsContinuous"], "egc_material": f["EgcMaterial"]}
        output = local._load_builtin(TOOL).run(graph, params)
        assert output == {
            "continuous_current_a": f["ContinuousCurrentA"],
            "is_continuous": f["IsContinuous"],
            "egc_material": f["EgcMaterial"],
            "min_ocpd_a": f["MinOcpdA"],
            "ocpd_rating_a": f["OcpdRatingA"],
            "egc_gauge": f["EgcGauge"],
            "note": f["Note"],
        }
        assert output["ocpd_rating_a"] == row["quantity"]["value"]


def test_feeder_ocpd_note_separators(graph):
    builtin = local._load_builtin(TOOL)
    first = builtin.run(graph, PARAMS)
    for separator in ("\u2014", "\u00d7", "\u2192"):
        assert first["note"].count(separator) == 1
    assert " - " not in first["note"]
    noncontinuous = builtin.run(graph, dict(PARAMS, is_continuous=False))
    assert noncontinuous["note"] == (
        "NEC 210.20(A) \u2014 20.0A \u2192 20A OCPD; EGC: 12 AWG per NEC 250.122")
    assert "\u00d7" not in noncontinuous["note"]
    assert "1.25" not in noncontinuous["note"]
    above_table = builtin.run(graph, dict(PARAMS, continuous_current_a=7000))
    assert above_table["note"].endswith("EGC: N/A per NEC 250.122")
    assert above_table["egc_gauge"] is None


def test_feeder_ocpd_registry_and_catalog_view(api):
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
        "schema": "leaf.solar-tool-view.v1", "name": "solar-nec-feeder-ocpd",
        "family": "equipment", "wave": 3, "order": 40, "maturity": "production",
        "engine": "server-builtin", "adapter": "local-graph-read", "entitlement": "run_read",
        "interaction": {"mode": "form"}, "ledger": ["nec-feeder-ocpd-sizing"],
    }


def test_feeder_ocpd_availability_ready_and_reasons(graph, api):
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


def test_feeder_ocpd_api_run_end_to_end(api):
    response = api[0].post("/api/run?wait=1", json=body(api))
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True
    result = env["result"]
    rec = jobs.get_job(result["job_id"])
    assert rec["status"] == "complete"
    assert rec["dwg_version"] == 1
    assert result["output"] == F1
    assert result["output_sha256"] == F1_SHA
    assert result["output_bytes"] == 231
    assert env["execution_provenance"]["execution_path"] == "local"
    assert len(api[4]) == 1
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 1


@pytest.mark.parametrize("change", [
    {"is_continuous": 1}, {"egc_material": "copper"}, {"x": 1}, {"continuous_current_a": 10001},
], ids=["bool", "material", "extra", "current"])
def test_feeder_ocpd_api_run_refusals(api, change):
    payload = body(api)
    payload["params"].update(change)
    response = api[0].post("/api/run?wait=1", json=payload)
    env = response.json()
    assert env.get("ok") is not True
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert all(rec["status"] != "complete" for rec in records)
    errors = [env.get("error", {})] + [rec.get("error") or {} for rec in records]
    assert any(error.get("reason_code") == "tool_params_invalid" for error in errors), env
    assert "FEEDER_OCPD_INPUT_OUT_OF_RANGE" not in response.text
    assert "INVALID_FEEDER_OCPD_REQUEST" not in response.text
    assert len(api[4]) == 1
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 1
