"""Guardrails registry reads: pinned graph outputs, plugin receipts, and API proofs."""
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
import solar_guardrails
import solar_local_graph
import solar_local_read as local
import solar_sizing_client
import solar_tools
import store
import write_loop
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError, validate_graph
from solar_sizing_client import digest
from test_w1_design_graph import app_id, entity, graph  # noqa: F401
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_solve_commit import seed
from test_w1_sizing_groups import confirm, passing, service, sizing_params  # noqa: F401
from test_solar_tool_solar_string_edits import variant

SERVER = Path(__file__).resolve().parents[1]
ROOT = SERVER.parent
TENANT = "fixture-tenant"
TOOL = "solar-guardrails-read"
DECLARATION = {
    "schema": "leaf.solar-tool.v1", "name": TOOL,
    "builtin": "builtins/solar_guardrails_read.py", "family": "placement",
    "adapter": "local-graph-read", "entitlement": "run_read",
    "requires_persisted_graph": True, "seedable": False,
    "invalid_request_code": "INVALID_GUARDRAILS_REQUEST",
    "readiness": {"kind": "hook"}, "engine": "server-builtin",
    "interaction": {"mode": "form"}, "record_store": "registry",
    "record": {
        "name": TOOL, "version": "1.0.0",
        "description": "Run the plugin's 14 design guardrails on the stored design: verdicts in palette order and the health banner counts.",
        "kind": "script", "family_id": "placement", "engine_op": "solar_guardrails_read",
        "entry": "builtins/solar_guardrails_read.py",
        "params": {
            "type": "object", "properties": {
                "drawing_id": {"type": "string", "maxLength": 128},
                "mppt_voltage_window": {
                    "type": "object", "properties": {
                        "min_v": {"type": "number", "minimum": 0, "maximum": 100000},
                        "max_v": {"type": "number", "minimum": 0, "maximum": 100000}},
                    "required": ["min_v", "max_v"], "additionalProperties": False}},
            "required": [], "additionalProperties": False},
        "returns": {"type": "object"}, "capabilities": ["drawing.read"],
        "allow_local_fallback": False},
    "ledger": ["guardrails-monitoring"], "trusted_inputs": [], "maturity": "preview",
    "wave": 2, "order": 95, "scenario": "w2-rooftop",
}
G0_NUMBERS = [17, 24, 18, 22, 30, 27, 16, 26, 29, 19, 28, 10, 14, 15, 9, 13, 25, 12, 20, 31, 21, 11, 23]
CANONICAL = {
    "G1": (3133, "069c7c32f6acad06287d0bd4851cbe9d9b4d28dbdc2a62c9996a2efd3480a479"),
    "G2": (3590, "47e277df5aa58fb7b94f243fa485fb1382ddf077028c86d3fa2394bf7f293a75"),
    "G3": (3593, "4178e35282ef0486520df8d90dc30668182d5234ffc029906ace81488c7ca3eb"),
    "G9": (3306, "6819d414fe464797320983419cd2d14bb2a55ce7230d1365434e6243bda40271"),
    "G4": (3133, "76c246ef4c25357ac147d8237bb2af608af6bba73b874091a5d589f6f207451a"),
    "G5": (3349, "bb0360673fd709def5b8654af65100a5d890546be8c5c82404a4602f02595337"),
    "G5N": (3009, "e6d1a573471f676b5173ddf4c292ff31673e9305c2d409e0bbbdc805646d69b9"),
    "G8": (3133, "5d6d62a9561f9ede72b0ecb247a274a9ef6c6d082e65a9634bb67bcaa0ee01c9"),
    "G7": (2881, "2863411dd013fd338b1ddffd5bf1252c2a8e09dd5b43cdf113975d2fcc10b6dd"),
}
BASE_VERDICTS = (
    "NEC-690.7-VOC-COLD=critical NEC-690.8A-ISC=pass NEC-690.9-OCPD=pass "
    "NEC-690.7-VOC-COLD-OPTIMIZER=pass HW-OPTI-COMPAT=pass HW-STRING-DELTA=pass "
    "HW-OPTI-FAMILY=pass DESIGN-DC-AC=warning DESIGN-MPPT-BAL=info "
    "DESIGN-STRINGS-MPPT=pass SAFE-NULL-STATE=pass SAFE-DIV-ZERO=pass SAFE-DB-VALUES=pass"
).split()
VERDICTS = {key: BASE_VERDICTS[:] for key in ("G1", "G4", "G8")}
VERDICTS["G2"] = BASE_VERDICTS[:1] + ["MPPT-WINDOW=error", BASE_VERDICTS[1], "MPPT-WINDOW=pass"] + BASE_VERDICTS[2:]
VERDICTS["G3"] = VERDICTS["G2"][:-3] + ["SAFE-DB-VALUES=warning", "SAFE-NULL-STATE=pass", "SAFE-DIV-ZERO=pass"]
VERDICTS["G9"] = BASE_VERDICTS[:2] + ["MPPT-WINDOW=pass"] + BASE_VERDICTS[2:-3] + [
    "SAFE-DB-VALUES=warning", "SAFE-NULL-STATE=pass", "SAFE-DIV-ZERO=pass"]
VERDICTS["G5N"] = ["NEC-690.7-VOC-COLD=warning"] + BASE_VERDICTS[1:]
VERDICTS["G5"] = VERDICTS["G5N"][:2] + ["MPPT-WINDOW=pass"] * 2 + VERDICTS["G5N"][2:]
VERDICTS["G7"] = (
    "NEC-690.7-VOC-COLD=info NEC-690.8A-ISC=pass NEC-690.9-OCPD=pass "
    "NEC-690.7-VOC-COLD-OPTIMIZER=pass HW-OPTI-COMPAT=pass HW-STRING-DELTA=pass "
    "HW-OPTI-FAMILY=pass DESIGN-DC-AC=info DESIGN-STRINGS-MPPT=info DESIGN-MPPT-BAL=pass "
    "SAFE-NULL-STATE=pass SAFE-DIV-ZERO=pass SAFE-DB-VALUES=pass"
).split()
REPORTS = {key: {"critical-count": 1, "info-count": 1, "pass-count": 10,
                 "status": "CRITICAL", "warning-count": 1} for key in ("G1", "G4", "G8")}
REPORTS["G2"] = dict(REPORTS["G1"], **{"error-count": 1, "pass-count": 11})
REPORTS["G3"] = dict(REPORTS["G1"], **{"error-count": 1, "warning-count": 2})
REPORTS["G9"] = dict(REPORTS["G1"], **{"warning-count": 2})
REPORTS["G5"] = {"info-count": 1, "pass-count": 12, "status": "WARNINGS", "warning-count": 2}
REPORTS["G5N"] = dict(REPORTS["G5"], **{"pass-count": 10})
REPORTS["G7"] = {"info-count": 3, "pass-count": 10, "status": "HEALTHY"}
INPUTS = {key: {"module_source": "sizing-global", "mppt_window": "absent",
                "design_min_temp_c": -18.0, "inverter_count": 1, "string_count": 2,
                "linked_string_count": 2} for key in CANONICAL}
for _key in ("G2", "G3", "G9", "G5"):
    INPUTS[_key]["mppt_window"] = "caller"
INPUTS["G4"]["linked_string_count"] = 0
INPUTS["G8"]["inverter_count"] = 2
for _key in ("G5", "G5N"):
    INPUTS[_key].update(design_min_temp_c=5.0, inverter_count=23, linked_string_count=0)
INPUTS["G7"].update(module_source="sizing-zones", inverter_count=0, string_count=0, linked_string_count=0)


def window(low, high):
    return {"mppt_voltage_window": {"min_v": low, "max_v": high}}


PARAMS = {"G2": window(200, 600), "G3": window(600, 200),
          "G9": window(0, 100000), "G5": window(500, 1500)}


def intake():
    return json.loads((ROOT / "docs/parity/evidence/batch2/g0-intake.json").read_text(encoding="utf-8"))


def located(g, latitude=41.0, longitude=-81.4):
    g["project"].update(zip_code="44224", latitude=latitude, longitude=longitude)
    return g


def sized(g, passing):
    return confirm(g, sizing_params(g, passing))["graph"]


def make_graph(graph, passing, monkeypatch, case):
    if case == "unsized":
        return graph
    if case in ("G7", "GZ"):
        located(graph)
        graph.update(frames=[], strings=[], inverters=[], routes=[], schedules=[])
        for n, panel in enumerate(graph["panels"], 1):
            panel.update(frame_ref=None, matrix_cell=None, angle=0, centre=[n, 0],
                         assignment={"string_ref": None, "seq": None})
            panel["provenance"]["source_handle"] = f"A{n}"
        for n in (4, 5):
            panel = copy.deepcopy(graph["panels"][0])
            panel.update(id=app_id("panel", n), centre=[n, 0])
            panel["provenance"]["source_handle"] = f"A{n}"
            graph["panels"].append(panel)
        first = graph["electrical_zones"][0]
        first.update(module_model="module-A", panel_refs=[p["id"] for p in graph["panels"][:2]])
        second = copy.deepcopy(first)
        second.update(id=app_id("zone-el", 2), name="Zone B", module_model="module-B",
                      panel_refs=[p["id"] for p in graph["panels"][2:]])
        graph["electrical_zones"].append(second)
        powers = {"module-A": 400, "module-B": 550} if case == "GZ" else {"module-A": 595, "module-B": 595}

        def post(request, grant):
            response = copy.deepcopy(passing["response"])
            response["pmp"] = powers[request.module_name]
            return canonical_bytes(response)

        monkeypatch.setattr(solar_sizing_client, "post_string_length", post)
        return confirm(graph, sizing_params(graph, passing, mode="zones"))["graph"]
    if case in ("G4", "G5", "G5N"):
        graph = variant(graph, "N")
    if case in ("G5", "G5N"):
        located(graph, 10.0, 10.0)
        numbers = [device["number"] for device in intake()["devices"]]
        assert numbers == G0_NUMBERS
        graph["inverters"] = [
            entity("inverter", k, number=n, type_key="A", is_l2=False, position=[float(k), 0.0],
                   model="Sungrow SG-HX SG250HX", mppt_count=12, total_dc_inputs=24,
                   max_dc_voltage=1500, max_ac_power_kw=250, is_solaredge=False, input_assignments=[])
            for k, n in enumerate(numbers, 1)]
    elif case == "coordinates-none":
        located(graph, None, None)
    elif case == "coordinates-zero":
        located(graph, 0.0, 0.0)
    else:
        located(graph)
    result = sized(graph, passing)
    if case in ("G8", "mixed"):
        other = copy.deepcopy(result["inverters"][0])
        other.update(id=app_id("inverter", 2), number=2, input_assignments=[])
        if case == "mixed":
            other.update(position=[9.0, 0.0], model="other")
        result["inverters"].append(other)
    if case == "moved":
        result["project"]["latitude"] = 42.0
    return result


@pytest.fixture
def builtin():
    return local._load_builtin(TOOL)


@pytest.fixture
def g1(graph, passing, service):
    return sized(located(graph), passing)


def test_guardrails_read_declaration_pins():
    assert json.loads((SERVER / "solar_tools/solar_guardrails_read.json").read_text(encoding="utf-8")) == DECLARATION
    assert solar_tools.get(TOOL) == DECLARATION
    assert solar_tools.load().get(TOOL) == DECLARATION
    assert solar_tools.trusted_record(TOOL) == DECLARATION["record"]


def test_guardrails_read_params_bounds():
    params = solar_tools.trusted_record(TOOL)["params"]
    assert params == DECLARATION["record"]["params"]
    assert params["properties"]["drawing_id"] == {"type": "string", "maxLength": 128}
    spec = params["properties"]["mppt_voltage_window"]
    assert set(spec["properties"]) == {"min_v", "max_v"}
    assert spec["required"] == ["min_v", "max_v"]
    assert spec["additionalProperties"] is False
    for field in spec["properties"].values():
        assert field == {"type": "number", "minimum": 0, "maximum": 100000}
    assert params["required"] == []
    assert params["additionalProperties"] is False


@pytest.mark.parametrize("step", ["g1", "g2"])
def test_guardrails_read_recorded_rule_set(builtin, step):
    receipt = json.loads((ROOT / f"docs/parity/receipts/guardrails-monitoring/batch2-{step}.json").read_text(encoding="utf-8"))
    rows = builtin.rows_for_intake(intake())
    assert rows == receipt["comparison"]["plugin"]["after"]["rows"]
    assert rows == receipt["comparison"]["studio"]["after"]["rows"]
    assert len(rows) == 19
    assert digest(rows) == "df522052c589f065c9a310f473aeef6e26be185f9792e13f9149a8d3164e8eee"
    verdicts = [row for row in rows if row["type"] == "verdict"]
    expected = ["NEC-690.7-VOC-COLD=pass"] + VERDICTS["G5"][1:]
    assert [row["rule_id"] + "=" + row["status"] for row in verdicts] == expected
    assert {row["rule_id"] for row in verdicts} == {rule[0] for rule in solar_guardrails.RULES}
    assert {row["name"]: row["value"] for row in rows if row["type"] == "report"} == {
        "status": "WARNINGS", "warning-count": 1, "info-count": 1, "pass-count": 13}


@pytest.mark.parametrize("case", list(CANONICAL))
def test_guardrails_read_graph_cases(builtin, graph, passing, service, monkeypatch, case):
    g = make_graph(graph, passing, monkeypatch, case)
    validate_graph(g)
    if case == "G1":
        assert digest(g) == "5127f9adc6ec07baa94ac19206973a2abc87746578b3519ecca8c56b606bb502"
    output = builtin.run(g, PARAMS.get(case, {}))
    assert (len(canonical_bytes(output)), digest(output)) == CANONICAL[case]
    assert solar_local_graph.stable_numbers(output)
    assert output["status"] == REPORTS[case]["status"]
    assert [row["rule_id"] + "=" + row["status"] for row in output["rows"] if row["type"] == "verdict"] == VERDICTS[case]
    assert {row["name"]: row["value"] for row in output["rows"] if row["type"] == "report"} == REPORTS[case]
    assert output["inputs"] == INPUTS[case]


def g1_output():
    rows = [{"id": {"entity_id": "report-" + name}, "type": "report", "quantity": 1,
             "unit": "each", "name": name, "value": value}
            for name, value in sorted(REPORTS["G1"].items())]
    titles = [
        ("electrical", "Cold-temperature Voc limit"),
        ("electrical", "Current 1.25x safety factor"),
        ("electrical", "Overcurrent protection"),
        ("electrical", "Cold Voc per optimizer input"),
        ("hardware", "Optimizer-module compatibility"),
        ("hardware", "String length delta"),
        ("hardware", "Optimizer-inverter family match"),
        ("design", "DC/AC ratio"), ("design", "MPPT balance"),
        ("design", "Strings per MPPT capacity"),
        ("code-safety", "Global state integrity"),
        ("code-safety", "Division by zero guard"),
        ("code-safety", "Database value integrity")]
    for n, ((section, title), verdict) in enumerate(zip(titles, BASE_VERDICTS), 1):
        rule_id, status = verdict.split("=")
        rows.append({"id": {"entity_id": f"verdict-{n}"}, "type": "verdict", "quantity": 1,
                     "unit": "each", "section": section, "rule": title, "rule_id": rule_id, "status": status})
    return {"schema": "leaf.solar-guardrails.v1", "list_mode": "drawing", "status": "CRITICAL",
            "rows": rows, "inputs": copy.deepcopy(INPUTS["G1"])}


def test_guardrails_read_g1_full_output(builtin, g1):
    output = builtin.run(g1, {})
    assert set(output) == {"schema", "list_mode", "status", "rows", "inputs"}
    assert output == g1_output()
    assert digest(output) == CANONICAL["G1"][1]


INVALID_REQUESTS = [
    [], None, {"x": 1}, {"drawing_id": "solar"},
    {"mppt_voltage_window": None}, {"mppt_voltage_window": []},
    {"mppt_voltage_window": {}}, {"mppt_voltage_window": {"min_v": 1}},
    {"mppt_voltage_window": {"min_v": 1, "max_v": 2, "x": 0}},
    window(True, 2), window("1", 2), window(1, float("nan")),
    window(float("inf"), 2), window("x", -1),
]
OUT_OF_RANGE = [window(-1, 2), window(1, 100001), window(1, 10**400), window(-0.5, 2)]


@pytest.mark.parametrize("params,code", [
    *[(params, "INVALID_GUARDRAILS_REQUEST") for params in INVALID_REQUESTS],
    *[(params, "GUARDRAILS_WINDOW_OUT_OF_RANGE") for params in OUT_OF_RANGE],
])
def test_guardrails_read_request_refusals(builtin, g1, params, code):
    with pytest.raises(GraphValidationError) as exc:
        builtin.run(g1, params)
    assert exc.value.code == code


GRAPH_REFUSALS = [
    ("unsized", "GUARDRAILS_SIZING_REQUIRED"),
    ("coordinates-none", "GUARDRAILS_PROJECT_COORDINATES_REQUIRED"),
    ("coordinates-zero", "GUARDRAILS_PROJECT_COORDINATES_REQUIRED"),
    ("GZ", "GUARDRAILS_SIZING_AMBIGUOUS"),
    ("mixed", "GUARDRAILS_MIXED_INVERTERS"),
    ("moved", "GUARDRAILS_SIZING_REQUIRED"),
]


@pytest.mark.parametrize("case,code", GRAPH_REFUSALS)
def test_guardrails_read_graph_refusals(builtin, graph, passing, service, monkeypatch, case, code):
    g = make_graph(graph, passing, monkeypatch, case)
    validate_graph(g)
    with pytest.raises(GraphValidationError) as exc:
        builtin.run(g, {})
    assert exc.value.code == code


def test_guardrails_read_kernel_refusal_is_named(builtin, g1, monkeypatch):
    def fail(*args):
        raise solar_guardrails.GuardrailError("x")

    monkeypatch.setattr(solar_guardrails, "guardrail_rows", fail)
    with pytest.raises(GraphValidationError) as exc:
        builtin.run(g1, {})
    assert exc.value.code == "GUARDRAILS_INPUT_UNSUPPORTED"
    assert builtin.input_readiness(g1) == {"input_ready": False, "input_reason": "guardrails_input_unsupported"}


def test_guardrails_read_integral_float_inverter_number(builtin, g1):
    expected = builtin.run(g1, {})
    g1["inverters"][0]["number"] = 1.0
    validate_graph(g1)
    output = builtin.run(g1, {})
    assert output == expected
    assert output["inputs"]["linked_string_count"] == 2
    assert builtin.input_readiness(g1) == {"input_ready": True, "input_reason": None}


def test_guardrails_read_readiness_handles_snapshot_type_error(builtin, g1, monkeypatch):
    def fail(*args):
        raise TypeError("injected")

    monkeypatch.setattr(solar_guardrails, "build_snapshot", fail)
    assert builtin.input_readiness(g1) == {"input_ready": False, "input_reason": "guardrails_input_unsupported"}


def test_guardrails_read_builtin_is_pure(builtin, g1):
    graph, params = copy.deepcopy(g1), window(200, 600)
    before_graph, before_params = copy.deepcopy(graph), copy.deepcopy(params)
    first, second = builtin.run(graph, params), builtin.run(graph, params)
    assert first == second
    assert graph == before_graph
    assert params == before_params
    first["rows"].clear()
    assert second["rows"]
    assert graph == before_graph


@pytest.mark.parametrize("case,reason", [
    ("G1", None), ("G7", None), ("unsized", "guardrails_sizing_required"),
    ("coordinates-none", "guardrails_project_coordinates_required"),
    ("mixed", "guardrails_mixed_inverters"),
])
def test_guardrails_read_readiness_hook(builtin, graph, passing, service, monkeypatch, case, reason):
    g = make_graph(graph, passing, monkeypatch, case)
    assert builtin.input_readiness(g) == {"input_ready": reason is None, "input_reason": reason}
    if case == "G1":
        assert availability.w1_local_commit_inputs(g)[TOOL] == {"input_ready": True, "input_reason": None}
        g["project"]["units"]["meters_per_unit"] *= 2
        assert availability.w1_local_commit_inputs(g)[TOOL] == {"input_ready": False, "input_reason": "unresolved_units"}

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
                    output = {"schema": "leaf.solar-guardrails.v1", "status": "HEALTHY", "rows": []}
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
def api(isolated_jobs, no_network, graph, passing, service, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, sized(located(graph), passing))
    yield from _api(backend, tmp_path, monkeypatch)


@pytest.fixture
def unsized_api(isolated_jobs, no_network, graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    yield from _api(backend, tmp_path, monkeypatch)


def body(api):
    return {"tool": TOOL, "dwg": "solar", "params": {},
            "catalog_digest": deps.catalog_tool_digest(api[2])}


def test_guardrails_read_registry_and_catalog_view(api):
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
    assert family["family_id"] == "placement"
    assert row["solar"] == {
        "schema": "leaf.solar-tool-view.v1", "name": TOOL, "family": "placement",
        "wave": 2, "order": 95, "maturity": "preview", "engine": "server-builtin",
        "adapter": "local-graph-read", "entitlement": "run_read",
        "interaction": {"mode": "form"}, "ledger": ["guardrails-monitoring"],
    }


@pytest.mark.parametrize("state", ["ready", "adapter-missing", "units-unresolved"])
def test_guardrails_read_availability_ready_and_reasons(api, monkeypatch, state):
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



def test_guardrails_read_api_run_end_to_end(api):
    response = api[0].post("/api/run?wait=1", json=body(api))
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True
    result = env["result"]
    rec = jobs.get_job(result["job_id"])
    assert rec["status"] == "complete"
    assert rec["dwg_version"] == 1
    assert rec["params"] == {"drawing_id": "solar"}
    assert result["output"] == g1_output()
    assert (result["output_bytes"], result["output_sha256"]) == CANONICAL["G1"]
    assert result["request_sha256"] == "1a71aab96db7335eb150eb0a1f242378353fe146375e3f4e4adb02fd21b8ad4d"
    provenance = env["execution_provenance"]
    assert provenance["execution_path"] == "local"
    receipt = local.graph_read_provenance(
        result, rec["params"], TENANT, rec["job_id"], TOOL, 1, backend=api[1])
    for key, value in receipt.items():
        assert provenance[key] == rec["provenance"][key] == value
    assert len(api[4]["requests"]) == 1
    manifest = store.load_manifest(api[1], TENANT, "solar")
    assert manifest["head"] == manifest["latest"] == 1



def test_guardrails_read_api_run_with_window(api, builtin, g1):
    payload = body(api)
    payload["params"] = window(200, 600)
    response = api[0].post("/api/run?wait=1", json=payload)
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True
    result = env["result"]
    rec = jobs.get_job(result["job_id"])
    assert rec["status"] == "complete"
    assert rec["dwg_version"] == 1
    assert rec["params"] == dict(window(200, 600), drawing_id="solar")
    assert result["output"] == builtin.run(g1, window(200, 600))
    assert (result["output_bytes"], result["output_sha256"]) == CANONICAL["G2"]
    assert result["request_sha256"] == "b45ce372128617559fef8526555f6f0f3a39071c7fd1932605c65af8a5227142"
    assert env["execution_provenance"]["execution_path"] == "local"
    assert len(api[4]["requests"]) == 1
    manifest = store.load_manifest(api[1], TENANT, "solar")
    assert manifest["head"] == manifest["latest"] == 1


@pytest.mark.parametrize("fixture,reason", [
    ("api", "DRAWING_ID_CONFLICT"), ("unsized_api", "guardrails_sizing_required"),
])
def test_guardrails_read_api_refusals_before_the_broker(request, fixture, reason):
    api = request.getfixturevalue(fixture)
    payload = body(api)
    if fixture == "api":
        payload["params"]["drawing_id"] = "other"
    response = api[0].post("/api/run?wait=1", json=payload)
    assert response.status_code == 409, response.text
    assert response.json()["reason_code"] == reason
    assert not jobs._query("SELECT job_id FROM jobs")
    assert not api[4]["requests"]


@pytest.mark.parametrize("params", [
    window(1, 100001), {"x": 1}, {"mppt_voltage_window": {"min_v": 1}},
])
def test_guardrails_read_api_param_refusals(api, params):
    payload = body(api)
    payload["params"] = params
    response = api[0].post("/api/run?wait=1", json=payload)
    env = response.json()
    assert env.get("ok") is not True
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert all(rec["status"] != "complete" for rec in records)
    errors = [env.get("error", {})] + [rec.get("error") or {} for rec in records]
    assert any(error.get("reason_code") == "tool_params_invalid" for error in errors), env
    assert "GUARDRAILS_WINDOW_OUT_OF_RANGE" not in response.text
    assert "INVALID_GUARDRAILS_REQUEST" not in response.text
    assert len(api[4]["requests"]) == 1
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 1


def test_guardrails_read_worker_rejects_a_tampered_read_receipt(api):
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
