"""W3 conduit fill through Studio's registry, read adapter and broker."""
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
import solar_nec
import solar_tools
from solar_local_graph import stable_numbers
from collections import Counter
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
TOOL = "solar-nec-conduit-fill"
PARAMS = {"conductor_gauge": "10 AWG", "current_carrying_count": 2,
          "egc_gauge": "10 AWG", "conduit_type": "PvcSch40", "conductor_insulation": "THWN2"}
C1_SHA = "aeacfd372240eee8d1acb94de0c848adc3fe1584ec465ebe8f0f53d469cc829d"
C1 = {
    "success": True, "trade_size": "1/2", "conduit_type": "PvcSch40",
    "conduit_type_label": "PVC Sch 40", "conduit_area_sq_in": 0.285,
    "conductor_area_sq_in": 0.0528, "fill_pct": 18.526315789473685,
    "max_fill_pct": 40.0, "max_fill_fraction": 0.4, "total_conductors": 3,
    "failure_reason": None,
    "note": 'NEC Ch9 T1/T4 - 3 conductors in 1/2" PVC Sch 40: fill 18.5% \u2264 40% max',
    "conductors": [
        {"role": "current-carrying", "gauge": "10", "unit": "AWG",
         "insulation": "THWN2", "count": 2, "area_sq_in": 0.0211},
        {"role": "egc", "gauge": "10", "unit": "AWG",
         "insulation": "Bare", "count": 1, "area_sq_in": 0.0106}],
    "conduit_table": [
        {"trade_size": "1/2", "area_sq_in": 0.285},
        {"trade_size": "3/4", "area_sq_in": 0.508},
        {"trade_size": "1", "area_sq_in": 0.832},
        {"trade_size": "1-1/4", "area_sq_in": 1.453},
        {"trade_size": "1-1/2", "area_sq_in": 1.986},
        {"trade_size": "2", "area_sq_in": 3.291},
        {"trade_size": "2-1/2", "area_sq_in": 4.695},
        {"trade_size": "3", "area_sq_in": 7.268},
        {"trade_size": "3-1/2", "area_sq_in": 9.737},
        {"trade_size": "4", "area_sq_in": 12.554}],
}
C5 = {
    "success": False, "trade_size": None, "conduit_type": "EMT",
    "conduit_type_label": "EMT", "conduit_area_sq_in": 0.0,
    "conductor_area_sq_in": 0.0, "fill_pct": 0.0, "max_fill_pct": 0.0,
    "max_fill_fraction": 0.0, "total_conductors": 0,
    "failure_reason": "No conductors specified.", "note": None, "conductors": [],
    "conduit_table": [
        {"trade_size": "1/2", "area_sq_in": 0.304},
        {"trade_size": "3/4", "area_sq_in": 0.533},
        {"trade_size": "1", "area_sq_in": 0.864},
        {"trade_size": "1-1/4", "area_sq_in": 1.496},
        {"trade_size": "1-1/2", "area_sq_in": 2.036},
        {"trade_size": "2", "area_sq_in": 3.356},
        {"trade_size": "2-1/2", "area_sq_in": 5.858},
        {"trade_size": "3", "area_sq_in": 8.846},
        {"trade_size": "3-1/2", "area_sq_in": 11.545},
        {"trade_size": "4", "area_sq_in": 15.901}],
}
DECLARATION = {
    "schema": "leaf.solar-tool.v1",
    "name": "solar-nec-conduit-fill",
    "builtin": "builtins/solar_nec_conduit_fill.py",
    "family": "equipment",
    "adapter": "local-graph-read",
    "entitlement": "run_read",
    "requires_persisted_graph": True,
    "seedable": False,
    "invalid_request_code": "INVALID_CONDUIT_FILL_REQUEST",
    "readiness": {
        "kind": "facets",
        "facets": []
    },
    "engine": "server-builtin",
    "interaction": {
        "mode": "form"
    },
    "record_store": "registry",
    "record": {
        "name": "solar-nec-conduit-fill",
        "version": "1.0.0",
        "description": "NEC Chapter 9 conduit fill: the smallest trade size for N conductors of one gauge plus an optional bare EGC, with Table 1, 4 and 5 values.",
        "kind": "script",
        "family_id": "equipment",
        "engine_op": "solar_nec_conduit_fill",
        "entry": "builtins/solar_nec_conduit_fill.py",
        "params": {
            "type": "object",
            "properties": {
                "drawing_id": {
                    "type": "string",
                    "maxLength": 128
                },
                "conductor_gauge": {
                    "type": "string",
                    "enum": [
                        "14 AWG",
                        "12 AWG",
                        "10 AWG",
                        "8 AWG",
                        "6 AWG",
                        "4 AWG",
                        "3 AWG",
                        "2 AWG",
                        "1 AWG",
                        "1/0 AWG",
                        "2/0 AWG",
                        "3/0 AWG",
                        "4/0 AWG",
                        "250 kcmil",
                        "300 kcmil",
                        "350 kcmil",
                        "400 kcmil",
                        "500 kcmil",
                        "600 kcmil",
                        "750 kcmil",
                        "1000 kcmil"
                    ]
                },
                "current_carrying_count": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 1000
                },
                "egc_gauge": {
                    "type": "string",
                    "enum": [
                        "14 AWG",
                        "12 AWG",
                        "10 AWG",
                        "8 AWG",
                        "6 AWG",
                        "4 AWG",
                        "3 AWG",
                        "2 AWG",
                        "1 AWG",
                        "1/0 AWG",
                        "2/0 AWG",
                        "3/0 AWG",
                        "4/0 AWG",
                        "250 kcmil",
                        "300 kcmil",
                        "350 kcmil",
                        "400 kcmil",
                        "500 kcmil",
                        "600 kcmil",
                        "750 kcmil",
                        "1000 kcmil"
                    ]
                },
                "conduit_type": {
                    "type": "string",
                    "enum": [
                        "EMT",
                        "PvcSch40",
                        "PvcSch80",
                        "RMC",
                        "LFNC_B"
                    ]
                },
                "conductor_insulation": {
                    "type": "string",
                    "enum": [
                        "THWN2",
                        "XHHW2",
                        "USE2"
                    ]
                }
            },
            "required": [
                "conductor_gauge",
                "current_carrying_count",
                "conduit_type",
                "conductor_insulation"
            ],
            "additionalProperties": False
        },
        "returns": {
            "type": "object"
        },
        "capabilities": [
            "drawing.read"
        ],
        "allow_local_fallback": False
    },
    "ledger": [
        "nec-conduit-fill"
    ],
    "trusted_inputs": [],
    "maturity": "production",
    "wave": 3,
    "order": 30,
    "scenario": "w3-ground-electrical"
}


def parameters(gauge="10 AWG", count=1, egc=None, conduit="EMT", insulation="THWN2"):
    result = {"conductor_gauge": gauge, "current_carrying_count": count,
              "conduit_type": conduit, "conductor_insulation": insulation}
    if egc is not None:
        result["egc_gauge"] = egc
    return result


CASES = [
    (("10 AWG", 2, "10 AWG", "PvcSch40", "THWN2"), 984, C1_SHA,
     {"success": True, "trade_size": "1/2", "fill_pct": 18.526315789473685}),
    (("4/0 AWG", 2, "6 AWG", "PvcSch40", "THWN2"), 999,
     "bb054f2db0222cfbba0f8d4ed29334e0a65d257624cfb3612d6ff6e228793f01",
     {"success": True, "trade_size": "1-1/2", "conductor_area_sq_in": 0.6739999999999999,
      "fill_pct": 33.93756294058409}),
    (("10 AWG", 2, None, "EMT", "THWN2"), 876,
     "8025d18097f774a1636424f63d53a7ad68cb933c1b4d7c7d2f7fc82621b6ce48",
     {"success": True, "trade_size": "1/2", "fill_pct": 13.881578947368423,
      "max_fill_pct": 31.0, "max_fill_fraction": 0.31}),
    (("6 AWG", 1, None, "EMT", "THWN2"), 874,
     "62efb28c131fc1ae3bef40291f8faa4719da3feed2bae0de14d0aed20254c5df",
     {"success": True, "trade_size": "1/2", "fill_pct": 16.67763157894737,
      "max_fill_pct": 53.0, "max_fill_fraction": 0.53}),
    (("10 AWG", 0, None, "EMT", "THWN2"), 706,
     "4bdf0b921832fa684842bcbf208565d7b4c8aa2378ea04b6df80926c441f3235",
     C5),
    (("1000 kcmil", 6, None, "LFNC_B", "THWN2"), 875,
     "5710ba8c1b65028e9f96c41e51333b899af605006cd74934c88015bd06372210",
     {"success": False, "conductor_area_sq_in": 8.0868, "max_fill_pct": 40.0,
      "fill_pct": 0.0, "conduit_type_label": "LFNC-B",
      "failure_reason": 'No LFNC-B trade size (up to 4") fits 6 conductors (8.0868 sq in total, 40% max fill).'}),
    (("10 AWG", 0, "250 kcmil", "EMT", "THWN2"), 807,
     "499b9f57e85bb9efc408bde29fdd6b8dd149e6303b97897b8878065d9e888374",
     {"success": False, "failure_reason": "Unknown conductor: 250 kcmil (Bare)",
      "conductor_area_sq_in": 0.0, "total_conductors": 0,
      "conductors": [{"role": "egc", "gauge": "250", "unit": "kcmil",
                      "insulation": "Bare", "count": 1, "area_sq_in": 0.0}]}),
    (("1000 kcmil", 1000, "4/0 AWG", "PvcSch80", "XHHW2"), 1011,
     "b2064c55e153d065896c21d68296d840e1d85245d902fbf18e7eba6e6e53fc71",
     {"success": False, "total_conductors": 1001,
      "conductor_area_sq_in": 1275.0135999999995,
      "failure_reason": 'No PVC Sch 80 trade size (up to 4") fits 1001 conductors (1275.0136 sq in total, 40% max fill).'}),
    (("4/0 AWG", 3, "4 AWG", "PvcSch80", "XHHW2"), 991,
     "3de68853599fa2f8bae338ecae12510efa28212a895dc92cdc55937fbf1c1a8d",
     {"success": True, "trade_size": "2", "conduit_area_sq_in": 2.874,
      "conductor_area_sq_in": 0.9410999999999999, "fill_pct": 32.74530271398747}),
]


def test_conduit_fill_declaration_pins():
    path = SERVER / "solar_tools/solar_nec_conduit_fill.json"
    assert json.loads(path.read_text(encoding="utf-8")) == DECLARATION
    assert solar_tools.get(TOOL) == DECLARATION
    assert solar_tools.load().get(TOOL) == DECLARATION
    assert solar_tools.trusted_record(TOOL) == DECLARATION["record"]


def test_conduit_fill_enums_match_kernel():
    props = DECLARATION["record"]["params"]["properties"]
    gauges = [label + " " + unit for label, unit in zip(solar_nec.WIRE_LABELS, solar_nec.WIRE_UNITS)]
    assert len(gauges) == 21
    assert gauges[0] == "14 AWG"
    assert gauges[-1] == "1000 kcmil"
    assert props["conductor_gauge"]["enum"] == props["egc_gauge"]["enum"] == gauges
    assert props["conduit_type"]["enum"] == list(solar_nec.CONDUIT_TYPES)
    assert props["conductor_insulation"]["enum"] == ["THWN2", "XHHW2", "USE2"]
    assert props["current_carrying_count"] == {"type": "integer", "minimum": 0, "maximum": 1000}


@pytest.mark.parametrize("values,size,sha,expected", CASES, ids=[f"C{i}" for i in range(1, 10)])
def test_conduit_fill_builtin_cases(graph, values, size, sha, expected):
    output = local._load_builtin(TOOL).run(graph, parameters(*values))
    assert set(output) == set(C1)
    assert len(output) == 14
    assert len(output["conduit_table"]) == 10
    assert stable_numbers(output)
    assert len(canonical_bytes(output)) == size
    assert digest(output) == sha
    for key, value in expected.items():
        assert output[key] == value
    assert len(output["conductors"]) == int(values[1] > 0) + int(values[2] is not None)
    if values[3] == "LFNC_B":
        assert [row["area_sq_in"] for row in output["conduit_table"][-4:]] == [0.0] * 4


def test_conduit_fill_c1_full_output(graph):
    assert local._load_builtin(TOOL).run(graph, PARAMS) == C1


def test_conduit_fill_empty_full_output(graph):
    output = local._load_builtin(TOOL).run(graph, parameters(count=0))
    assert output == C5
    for key in ("conduit_area_sq_in", "conductor_area_sq_in", "fill_pct",
                "max_fill_pct", "max_fill_fraction"):
        assert type(output[key]) is float


INVALID_PARAMS = [
    [], None, {}, {k: v for k, v in PARAMS.items() if k != "conduit_type"},
    dict(PARAMS, drawing_id="solar"), dict(PARAMS, x=1),
    dict(PARAMS, conductor_gauge="99 AWG"), dict(PARAMS, conductor_gauge="10 awg"),
    dict(PARAMS, conductor_gauge="10"), dict(PARAMS, egc_gauge=None),
    dict(PARAMS, egc_gauge=""), dict(PARAMS, conduit_type="PVC"),
    dict(PARAMS, conductor_insulation="Bare"), dict(PARAMS, current_carrying_count=True),
    dict(PARAMS, current_carrying_count=2.0), dict(PARAMS, current_carrying_count="2"),
    dict(PARAMS, conductor_gauge="99 AWG", current_carrying_count=-1),
]


@pytest.mark.parametrize("params,code",
    [(p, "INVALID_CONDUIT_FILL_REQUEST") for p in INVALID_PARAMS] +
    [(dict(PARAMS, current_carrying_count=n), "CONDUIT_FILL_INPUT_OUT_OF_RANGE")
     for n in (-1, 1001, 10**400)])
def test_conduit_fill_builtin_refusals_are_named(graph, params, code):
    with pytest.raises(GraphValidationError) as exc:
        local._load_builtin(TOOL).run(graph, params)
    assert exc.value.code == code


def test_conduit_fill_builtin_is_pure(graph):
    params = copy.deepcopy(PARAMS)
    original_graph, original_params = copy.deepcopy(graph), copy.deepcopy(params)
    builtin = local._load_builtin(TOOL)
    first = builtin.run(graph, params)
    second = builtin.run(graph, params)
    assert first == second == C1
    assert builtin.run(object(), params) == C1
    first["conductors"].clear()
    assert second == C1
    assert graph == original_graph
    assert params == original_params


def receipt_rows(filename):
    path = Path(__file__).resolve().parents[2] / "docs/parity/receipts/nec-conduit-fill" / filename
    receipt = json.loads(path.read_text(encoding="utf-8"))
    assert receipt["capability"] == "nec-conduit-fill"
    assert receipt["comparator"]["verdict"] == "pass"
    return receipt["comparison"]["plugin"]["after"]["rows"]


def replay_fraction(graph, n, expected):
    builtin = local._load_builtin(TOOL)
    if not 0 <= n <= 1000:
        with pytest.raises(GraphValidationError) as exc:
            builtin.run(graph, parameters(count=n))
        assert exc.value.code == "CONDUIT_FILL_INPUT_OUT_OF_RANGE"
        assert solar_nec.max_fill_fraction(n) == expected
        return False
    output = builtin.run(graph, parameters(count=n))
    assert output["max_fill_fraction"] == expected
    assert output["total_conductors"] == n
    assert output["max_fill_pct"] == expected * 100.0
    # Fraction receipts do not record sizing success; compare the full kernel
    # result too so an honest unsuccessful bundle cannot become a success.
    sizing = solar_nec.size_for_circuit("10 AWG", n, None, "EMT", "THWN2")
    for key in ("success", "trade_size", "conduit_type", "conduit_area_sq_in",
                "conductor_area_sq_in", "fill_pct", "max_fill_pct",
                "total_conductors", "failure_reason", "note"):
        assert output[key] == getattr(sizing, key)
    assert type(output["success"]) is bool
    return True


def test_conduit_fill_receipt_maxfill_replays(graph):
    rows = receipt_rows("demo-probes-maxfill.json")
    assert len(rows) == 11
    reproduced = 0
    for row in rows:
        assert row["type"] == "nec-max-fill-fraction"
        reproduced += replay_fraction(graph, row["fields"]["N"], row["fields"]["Fill"])
    assert reproduced == 8


def test_conduit_fill_receipt_tables_replays(graph):
    rows = receipt_rows("demo-probes-tables.json")
    assert len(rows) == 55
    assert Counter(row["type"] for row in rows) == {
        "nec-max-fill-fraction": 6, "nec-conductor-area": 19,
        "nec-conduit-area": 16, "nec-conduit-sizing": 14}
    builtin = local._load_builtin(TOOL)
    reproduced = 0
    for row in rows:
        f = row["fields"]
        kind = row["type"]
        if kind == "nec-max-fill-fraction":
            reproduced += replay_fraction(graph, f["conductorCount"], f["csharpFraction"])
            continue
        if kind == "nec-conductor-area":
            label = f["gauge"] + " " + f["unit"]
            if label == "99 AWG":
                with pytest.raises(GraphValidationError) as exc:
                    builtin.run(graph, parameters(gauge=label))
                assert exc.value.code == "INVALID_CONDUIT_FILL_REQUEST"
                assert solar_nec.lookup_conductor_area("99", "AWG", "THWN2") == f["csharpArea"] == 0
                continue
            if f["insulation"] == "Bare":
                output = builtin.run(graph, parameters(count=0, egc=label))
                assert output["conductors"][0]["area_sq_in"] == f["csharpArea"]
                if f["csharpArea"] == 0.0:
                    assert output["success"] is False
                    assert output["failure_reason"] == f"Unknown conductor: {label} (Bare)"
                else:
                    assert output["conductor_area_sq_in"] == f["csharpArea"]
            else:
                output = builtin.run(graph, parameters(gauge=label, insulation=f["insulation"]))
                assert output["conductors"][0]["area_sq_in"] == f["csharpArea"] == output["conductor_area_sq_in"]
        elif kind == "nec-conduit-area":
            output = builtin.run(graph, parameters(conduit=f["conduitType"]))
            table = {item["trade_size"]: item["area_sq_in"] for item in output["conduit_table"]}
            if f["tradeSize"] == "9999":
                assert f["csharpArea"] == 0.0
                assert "9999" not in table
            else:
                assert table[f["tradeSize"]] == f["csharpArea"]
        else:
            carrying = [c for c in f["conductors"] if c["insulation"] != "Bare"]
            bare = [c for c in f["conductors"] if c["insulation"] == "Bare"]
            assert len(bare) <= 1
            assert f["conductors"] == carrying + bare
            assert all(c == carrying[0] for c in carrying)
            label = lambda c: c["gauge"] + " " + c["unit"]
            output = builtin.run(graph, parameters(
                label(carrying[0]) if carrying else "10 AWG", len(carrying),
                label(bare[0]) if bare else None, f["conduitType"],
                carrying[0]["insulation"] if carrying else "THWN2"))
            for key, field in (
                    ("success", "success"), ("trade_size", "tradeSize"),
                    ("conduit_type", "conduitType"), ("conduit_area_sq_in", "conduitAreaSqIn"),
                    ("conductor_area_sq_in", "conductorAreaSqIn"), ("fill_pct", "fillPct"),
                    ("max_fill_pct", "maxFillPct"), ("total_conductors", "totalConductors"),
                    ("failure_reason", "failureReason")):
                assert output[key] == f[field]
            expanded = [{k: c[k] for k in ("gauge", "insulation", "unit")}
                        for c in output["conductors"] for _ in range(c["count"])]
            assert expanded == f["conductors"]
        reproduced += 1
    assert reproduced == 53


def test_conduit_fill_registry_and_catalog_view(api):
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
        "schema": "leaf.solar-tool-view.v1", "name": "solar-nec-conduit-fill",
        "family": "equipment", "wave": 3, "order": 30, "maturity": "production",
        "engine": "server-builtin", "adapter": "local-graph-read", "entitlement": "run_read",
        "interaction": {"mode": "form"}, "ledger": ["nec-conduit-fill"],
    }


def test_conduit_fill_availability_ready_and_reasons(graph, api):
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


def test_conduit_fill_api_run_end_to_end(api):
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
    assert result["output_bytes"] == 984
    assert env["execution_provenance"]["execution_path"] == "local"
    assert len(api[4]) == 1
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 1


@pytest.mark.parametrize("change", [
    {"current_carrying_count": 1001}, {"conductor_gauge": "99 AWG"}, {"x": 1},
], ids=["A5-count", "A5-gauge", "A5-extra"])
def test_conduit_fill_api_run_refusals(api, change):
    payload = body(api)
    payload["params"].update(change)
    response = api[0].post("/api/run?wait=1", json=payload)
    env = response.json()
    assert env.get("ok") is not True
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert all(rec["status"] != "complete" for rec in records)
    errors = [env.get("error", {})] + [rec.get("error") or {} for rec in records]
    assert any(error.get("reason_code") == "tool_params_invalid" for error in errors), env
    assert "CONDUIT_FILL_INPUT_OUT_OF_RANGE" not in response.text
    assert "INVALID_CONDUIT_FILL_REQUEST" not in response.text
    assert len(api[4]) == 1
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 1


def test_conduit_fill_api_run_honest_failure(api):
    payload = body(api)
    payload["params"] = parameters(*CASES[5][0])
    expected = local._load_builtin(TOOL).run(object(), payload["params"])
    assert expected["success"] is False
    assert expected["failure_reason"] == CASES[5][3]["failure_reason"]
    response = api[0].post("/api/run?wait=1", json=payload)
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True
    result = env["result"]
    rec = jobs.get_job(result["job_id"])
    assert rec["status"] == "complete"
    assert rec["dwg_version"] == 1
    assert result["output"] == expected
    assert result["output_sha256"] == CASES[5][2]
    assert result["output_bytes"] == 875
    assert env["execution_provenance"]["execution_path"] == "local"
    assert len(api[4]) == 1
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 1
