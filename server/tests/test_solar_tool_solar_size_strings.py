"""String sizing committed through the intake graph rail under the tenant's cloud grant."""
import copy
import json
import sys
import time
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import catalog
import deps
import jobs
import product_capability_availability as availability
import solar_local_graph
import solar_sizing_client
import solar_tools
import store
from leaf_cloud_client import canonical_bytes
from leaf_cloud_grants import CloudError, CloudGrant
from product_capability_availability import is_cloud_proposal, is_local_graph_commit
from solar_design_graph import GraphValidationError
from solar_graph_context import resolve_graph_context
from test_w1_design_graph import graph  # noqa: F401
from test_w1_graph_versions import drawing, commit, request_for, TENANT as BUNDLE_TENANT, DRAWING  # noqa: F401
from test_w1_local_graph_adapter import held
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_local_graph_rail import api, body  # noqa: F401
from test_w1_solve_commit import seed

TOOL = "solar-size-strings"
TENANT_ID = "fixture-tenant"
TOKEN = "fixture-token-must-not-persist"
FIXTURES = SERVER / "tests" / "fixtures"
RECORDED = json.loads((FIXTURES / "w1_string_length_recorded_response.json").read_text(encoding="utf-8"))
PLUGIN = json.loads((FIXTURES / "w1_plugin_stringsizer_response.json").read_text(encoding="utf-8"))
PER = 52.58 * (1 + (-0.13145 / 100) * (-2.700000047683716 - 25))
DECLARATION = {
    "schema": "leaf.solar-tool.v1",
    "name": "solar-size-strings",
    "builtin": "builtins/solar_size_strings.py",
    "family": "stringing",
    "adapter": "local-graph-commit",
    "entitlement": "run_write",
    "requires_persisted_graph": True,
    "seedable": False,
    "invalid_request_code": "INVALID_SIZING_REQUEST",
    "readiness": {"kind": "w1-chain"},
    "engine": "cloud-service",
    "interaction": {"mode": "form"},
    "record_store": "write_seed",
    "record": None,
    "ledger": ["string-sizer"],
    "trusted_inputs": [],
    "maturity": "production",
    "wave": 1,
    "order": 20,
    "scenario": "w1-rooftop",
}


@pytest.fixture
def service(monkeypatch):
    """The String Sizer, recorded: each call is kept and answered from reply['value']."""
    calls = []
    reply = {"value": canonical_bytes(RECORDED["response"])}

    def post(request, grant):
        calls.append(request.wire())
        if isinstance(reply["value"], Exception):
            raise reply["value"]
        return reply["value"]

    monkeypatch.setattr(solar_sizing_client, "post_string_length", post)
    return calls, reply


@pytest.fixture(autouse=True)
def project_zip(graph):
    graph["project"]["zip_code"] = "44224"


def grant(monkeypatch):
    monkeypatch.setattr(solar_sizing_client, "resolve_grant",
                        lambda reference, tenant: CloudGrant(tenant, TOKEN))


@pytest.fixture
def granted(monkeypatch, service):
    grant(monkeypatch)
    return service


def params(graph, **changes):
    value = {"expected_rev": 0, "mode": "global",
             "requests": {graph["settings"]["id"]: copy.deepcopy(RECORDED["request"])},
             "grant_ref": "fixture-grant", "confirm": True}
    value.update(changes)
    return value


def zone_params(graph, module_name="fixture-module"):
    request = dict(copy.deepcopy(RECORDED["request"]), module_name=module_name,
                   full_inverter_name="fixture-inverter")
    return params(graph, mode="zones", requests={graph["electrical_zones"][0]["id"]: request})


def builtin():
    return solar_local_graph._load_builtin(TOOL)


def dispatch(backend, fence, request, job_id="size-job"):
    return solar_local_graph.run_local_graph_commit(
        backend, TENANT_ID, TOOL, request, drawing_id="solar", source_version=1,
        holder="fixture-owner", fence=fence, job_id=job_id)


def latest(backend):
    return store.load_manifest(backend, TENANT_ID, "solar")["latest"]


def refusal(backend, request):
    with held(backend) as fence:
        with pytest.raises(GraphValidationError) as error:
            dispatch(backend, fence, request)
    return error.value.code


def test_declaration_is_local_graph_commit():
    data = (SERVER / "solar_tools" / "solar_size_strings.json").read_bytes()
    declaration = json.loads(data)
    assert data == (json.dumps(declaration, indent=2) + "\n").encode("utf-8")
    assert list(declaration.items()) == list(DECLARATION.items())
    assert solar_tools.get(TOOL)["adapter"] == "local-graph-commit"
    assert TOOL in solar_local_graph.local_graph_tools()
    assert availability.capability_adapter(TOOL) == "local-graph-commit"
    assert is_local_graph_commit({"name": TOOL})
    assert not is_cloud_proposal({"name": TOOL})
    tools = json.loads((SERVER / "write_tools.json").read_text(encoding="utf-8"))["tools"]
    row = next(row for row in tools if row["name"] == TOOL)
    assert deps.catalog_tool_digest(row) == (
        "sha256:9d195167218f59395ed9fe168864801eca88158c6af988fb0a84f5fcc5dd1bf2")


def test_dispatch_commits_the_sizing_evidence(graph, tmp_path, monkeypatch, granted):
    calls, _ = granted
    backend, _ = seed(tmp_path, monkeypatch, graph)
    request = params(graph)
    before = copy.deepcopy(request)
    with held(backend) as fence:
        result = dispatch(backend, fence, request)
    assert request == before
    assert result["schema_version"] == "leaf.solar-graph-commit.v1"
    assert result["adapter"] == "local-graph-commit"
    assert result["tool"] == TOOL
    assert result["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    assert result["before_rev"] == 0 and result["after_rev"] == 1
    assert result["drawing_changed"] is True and result["replayed"] is False
    assert calls == [RECORDED["request"]]

    stored = resolve_graph_context(backend, TENANT_ID, "solar", 2)["graph"]
    settings = stored["settings"]
    assert settings["panels_in_sequence"] == 27
    assert settings["voc_cold"] == {
        "passes": True, "override_accepted": False, "suggested_string_length": 0,
        "per_module": PER, "string_voltage": PER * 27, "max_dc_voltage": 1500.0}
    assert settings["voc_cold"]["string_voltage"] == 1471.3521631279848
    assert settings["global_string_sizing_confirmed"] is True
    evidence = settings["extra"]["string_sizing"]
    assert evidence["mode"] == "global"
    assert set(evidence["records"]) == {graph["settings"]["id"]}
    for record in evidence["records"].values():
        assert set(record) == {"adapter_version", "endpoint", "job_id", "request", "request_sha256",
                               "response", "response_sha256", "sizing", "tenant_id",
                               "wire_response_sha256"}
        assert record["job_id"] == "size-job"
        assert record["tenant_id"] == TENANT_ID
        assert record["adapter_version"] == "2.0.0"
        assert record["endpoint"] == "https://api.leafdesign.ai/string-length"
    solar_sizing_client.require_sizing(stored)
    assert availability.w1_graph_readiness(stored)["solar-panel-groups"] == {
        "input_ready": True, "input_reason": None}

    proof = solar_local_graph.graph_commit_provenance(
        result, dict(request, drawing_id="solar"), TENANT_ID, "size-job", TOOL, 1, backend=backend)
    assert proof == {
        "execution_mode": "local_graph_commit", "adapter": "local-graph-commit",
        "request_sha256": result["request_sha256"], "graph_sha256": result["graph_sha256"],
        "intake_sha256": result["intake_sha256"], "source_version": 1, "new_version": 2}

    _, key = store.resolve_version(backend, TENANT_ID, "solar", 2)
    data = backend.get(key)
    assert TOKEN.encode("utf-8") not in data
    assert b"fixture-grant" not in data
    assert TOKEN not in json.dumps(result)


def test_redelivery_calls_the_service_and_the_store_replays(graph, tmp_path, monkeypatch, granted):
    calls, _ = granted
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with held(backend) as fence:
        first = dispatch(backend, fence, params(graph))
        replay = dispatch(backend, fence, params(graph))
        assert replay["replayed"] is True
        assert replay["new_version"] == first["new_version"]
        assert latest(backend) == 2
        assert len(calls) == 2
        with pytest.raises(GraphValidationError) as error:
            dispatch(backend, fence, zone_params(graph))
    assert error.value.code == "JOB_BINDING_REUSED"
    assert latest(backend) == 2


def test_zones_commit(graph, tmp_path, monkeypatch, granted):
    calls, _ = granted
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with held(backend) as fence:
        result = dispatch(backend, fence, zone_params(graph), job_id="zones-job")
    assert result["after_rev"] == 1
    stored = resolve_graph_context(backend, TENANT_ID, "solar", 2)["graph"]
    assert stored["electrical_zones"][0]["panels_in_sequence"] == 27
    assert stored["settings"]["global_string_sizing_confirmed"] is False
    assert stored["settings"]["panels_in_sequence"] == graph["settings"]["panels_in_sequence"] == 2
    assert len(calls) == 1


def _refused_request(graph, defect):
    if defect == "no_confirm":
        request = params(graph)
        del request["confirm"]
        return request
    return {
        "confirm_false": lambda: params(graph, confirm=False),
        "confirm_text": lambda: params(graph, confirm="yes"),
        "unknown_key": lambda: params(graph, bogus=1),
        "not_object": lambda: [1],
        "stale_rev": lambda: params(graph, expected_rev=5),
        "mode": lambda: params(graph, mode="diagonal"),
        "zones_keyed_by_settings": lambda: params(graph, mode="zones"),
        "no_requests": lambda: {key: value for key, value in params(graph).items()
                                if key != "requests"},
        "grant_ref": lambda: params(graph, grant_ref="bad ref!"),
        "model": lambda: zone_params(graph, module_name="other-module"),
        "cancel_only": lambda: {"expected_rev": 0, "cancel": True},
        "cancel": lambda: params(graph, cancel=True),
    }[defect]()


@pytest.mark.parametrize("defect,code", [
    ("no_confirm", "INVALID_SIZING_REQUEST"),
    ("confirm_false", "INVALID_SIZING_REQUEST"),
    ("confirm_text", "INVALID_SIZING_REQUEST"),
    ("unknown_key", "INVALID_SIZING_REQUEST"),
    ("not_object", "INVALID_SIZING_REQUEST"),
    ("stale_rev", "STALE_GRAPH_REVISION"),
    ("mode", "INVALID_SIZING_MODE"),
    ("zones_keyed_by_settings", "INVALID_SIZING_COVERAGE"),
    ("no_requests", "INVALID_SIZING_COVERAGE"),
    ("grant_ref", "CLOUD_REQUEST_INVALID"),
    ("model", "SIZING_MODEL_MISMATCH"),
    ("cancel_only", "GRAPH_COMMIT_CANCELLED"),
    ("cancel", "GRAPH_COMMIT_CANCELLED"),
])
def test_refusals_before_any_call(graph, tmp_path, monkeypatch, granted, defect, code):
    calls, _ = granted
    backend, _ = seed(tmp_path, monkeypatch, graph)
    assert refusal(backend, _refused_request(graph, defect)) == code
    assert calls == []
    assert latest(backend) == 1


@pytest.mark.parametrize("grants,code", [
    (None, "CLOUD_AUTH_MISSING"),
    ({"tenant_id": "other", "expires_at": 3600}, "CLOUD_TENANT_UNAUTHORIZED"),
    ({"tenant_id": TENANT_ID, "expires_at": -1}, "CLOUD_AUTH_MISSING"),
])
def test_grant_refusals(graph, tmp_path, monkeypatch, service, grants, code):
    calls, _ = service
    backend, _ = seed(tmp_path, monkeypatch, graph)
    monkeypatch.delenv("LEAF_CLOUD_GRANTS_FILE", raising=False)
    if grants is not None:
        path = tmp_path / "grants.json"
        path.write_text(json.dumps({"fixture-grant": {
            "tenant_id": grants["tenant_id"], "audience": "https://api.leafdesign.ai",
            "expires_at": time.time() + grants["expires_at"], "access_token": "t"}}),
            encoding="utf-8")
        monkeypatch.setenv("LEAF_CLOUD_GRANTS_FILE", str(path))
    assert refusal(backend, params(graph)) == code
    assert calls == []
    assert latest(backend) == 1


@pytest.mark.parametrize("reply,code", [
    (CloudError("cloud_upstream_failure", 502), "CLOUD_UPSTREAM_FAILURE"),
    (b"{}", "CLOUD_RESPONSE_INVALID"),
    (b'{"a":1,"a":2}', "CLOUD_RESPONSE_INVALID"),
    (canonical_bytes(PLUGIN["response"]), "COLD_VOLTAGE_FAILED"),
])
def test_upstream_refusals(graph, tmp_path, monkeypatch, granted, reply, code):
    calls, answer = granted
    answer["value"] = reply
    backend, _ = seed(tmp_path, monkeypatch, graph)
    assert refusal(backend, params(graph)) == code
    assert len(calls) == 1
    assert latest(backend) == 1


@pytest.mark.parametrize("code,error_code,status,reason", [
    ("CLOUD_AUTH_MISSING", "FORBIDDEN", 403, "CLOUD_AUTH_MISSING"),
    ("CLOUD_TENANT_UNAUTHORIZED", "FORBIDDEN", 403, "CLOUD_TENANT_UNAUTHORIZED"),
    ("CLOUD_UPSTREAM_FAILURE", "WORKITEM_FAILED", 502, "CLOUD_UPSTREAM_FAILURE"),
    ("CLOUD_RESPONSE_INVALID", "WORKITEM_FAILED", 502, "CLOUD_RESPONSE_INVALID"),
    ("CLOUD_REQUEST_INVALID", "BAD_PARAMS", 400, "CLOUD_REQUEST_INVALID"),
    ("COLD_VOLTAGE_FAILED", "BAD_PARAMS", 400, "COLD_VOLTAGE_FAILED"),
    ("CHECKOUT_DENIED", "FORBIDDEN", 403, "CHECKOUT_DENIED"),
    ("GRAPH_COMMIT_READBACK_FAILED", "INTERNAL", 500, "GRAPH_COMMIT_READBACK_FAILED"),
    ("bad code", "BAD_PARAMS", 400, "GRAPH_COMMIT_REFUSED"),
])
def test_broker_names_every_refusal(api, code, error_code, status, reason):
    env, http_status = api[4]._graph_commit_refused(code)
    assert http_status == status
    assert env["error"]["error_code"] == error_code
    assert env["error"]["reason_code"] == reason
    assert env["error"]["retryable"] is False


@pytest.mark.parametrize("defect,status,error_code,reason", [
    ("no_grant", 403, "FORBIDDEN", "CLOUD_AUTH_MISSING"),
    ("upstream", 502, "WORKITEM_FAILED", "CLOUD_UPSTREAM_FAILURE"),
    ("preview", 400, "BAD_PARAMS", "INVALID_SIZING_REQUEST"),
])
def test_studio_route_names_the_refusal(api, graph, service, monkeypatch, defect, status,
                                        error_code, reason):
    _, answer = service
    monkeypatch.delenv("LEAF_CLOUD_GRANTS_FILE", raising=False)
    if defect != "no_grant":
        grant(monkeypatch)
    if defect == "upstream":
        answer["value"] = CloudError("cloud_upstream_failure", 502)
    request = params(graph, expected_rev=graph["rev"], confirm=defect != "preview")
    response = api[0].post("/api/run?wait=1", json=body(api, TOOL, request))
    assert response.status_code == status, response.text
    env = response.json()
    assert env["ok"] is False
    assert env["error"]["error_code"] == error_code
    assert env["reason_code"] == reason
    assert env["error"]["retryable"] is False
    rows = jobs._query("SELECT job_id FROM jobs")
    assert len(rows) == 1
    assert jobs.get_job(rows[0]["job_id"])["status"] == "failed"
    assert store.load_manifest(api[1], TENANT_ID, "solar")["head"] == 1


def test_studio_route_commit_keeps_no_secret(api, graph, granted):
    calls, _ = granted
    response = api[0].post("/api/run?wait=1", json=body(api, TOOL, params(graph)))
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True
    job = jobs.get_job(env["result"]["job_id"])
    assert job["status"] == "complete"
    assert TOKEN not in json.dumps(job, default=str)
    assert TOKEN not in response.text
    assert store.load_manifest(api[1], TENANT_ID, "solar")["head"] == 2
    assert len(calls) == 1


def test_catalog_reports_engine_ready(api, graph):
    families = catalog.build_catalog(deps.all_tools(TENANT_ID))
    availability.annotate_w1_availability(
        families, api[5], "solar", project_id=graph["project"]["id"])
    states = {row["name"]: row["availability"] for family in families
              for row in family["capabilities"] if row["name"] == TOOL}
    assert states[TOOL] == {
        "engine_ready": True, "engine_reason": None, "entitled": True,
        "entitlement_reason": None, "implementation_reason": None, "implemented": True,
        "input_ready": True, "input_reason": None, "refusal_reasons": [], "runnable": True}


def test_bundle_drawing_never_calls_the_service(drawing, graph, granted):
    calls, _ = granted
    backend, fence = drawing
    commit(drawing, request_for(backend, graph))
    with pytest.raises(GraphValidationError) as error:
        solar_local_graph.run_local_graph_commit(
            backend, BUNDLE_TENANT, TOOL, params(graph), drawing_id=DRAWING, source_version=2,
            holder="writer", fence=fence, job_id="dwg-size-job")
    assert error.value.code == "LICENSED_GRAPH_COMMIT_REQUIRED"
    assert calls == []


def test_unbound_run_still_refuses(graph):
    with pytest.raises(RuntimeError, match="broker"):
        builtin().run(copy.deepcopy(graph), {})


def manual_params(**changes):
    return dict({"expected_rev": 0, "mode": "manual-global", "confirm": True}, **changes)


@pytest.fixture
def manual_no_cloud(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail("Manual confirmation called cloud sizing")

    monkeypatch.setattr(solar_sizing_client, "resolve_grant", forbidden)
    monkeypatch.setattr(solar_sizing_client, "post_string_length", forbidden)
    return calls


def test_W21C2_manual_commit(graph, tmp_path, monkeypatch, manual_no_cloud):
    graph["project"]["zip_code"] = ""
    backend, _ = seed(tmp_path, monkeypatch, graph)
    request = manual_params()
    before_graph, before_request = copy.deepcopy(graph), copy.deepcopy(request)
    with held(backend) as fence:
        result = dispatch(backend, fence, request)
    stored = resolve_graph_context(backend, TENANT_ID, "solar", 2)["graph"]
    assert (stored["rev"], stored["parent_rev"]) == (1, 0)
    assert result["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    assert graph == before_graph and request == before_request
    for key in graph.keys() - {"settings", "rev", "parent_rev"}:
        assert stored[key] == graph[key]
    settings = stored["settings"]
    assert settings["panels_in_sequence"] == 2
    assert settings["global_string_sizing_confirmed"] is True
    assert settings["voc_cold"] == {
        "passes": None, "override_accepted": False, "suggested_string_length": None,
        "per_module": None, "string_voltage": None, "max_dc_voltage": None}
    evidence = {"mode": "manual-global", "basis_sha256": solar_sizing_client.sizing_basis(stored),
                "panels_in_sequence": 2, "records": {}}
    assert settings["extra"]["string_sizing"] == evidence
    checked = solar_sizing_client.require_sizing(stored)
    assert checked == evidence and checked is not settings["extra"]["string_sizing"]
    checked["records"]["changed"] = {}
    assert settings["extra"]["string_sizing"] == evidence
    assert manual_no_cloud == []
    for length in (1, 4096):
        source = copy.deepcopy(graph)
        source["settings"]["panels_in_sequence"] = length
        candidate = builtin().size_strings(source, manual_params(), tenant_id=TENANT_ID, job_id="manual")
        assert candidate["graph"]["settings"]["panels_in_sequence"] == length
        assert candidate["records"] == {} and candidate["confirmed"] is True


def test_W21C2_manual_route_without_grant(api, graph, manual_no_cloud):
    response = api[0].post("/api/run?wait=1", json=body(api, TOOL, manual_params()))
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True
    assert jobs.get_job(env["result"]["job_id"])["status"] == "complete"
    assert store.load_manifest(api[1], TENANT_ID, "solar")["head"] == 2
    assert manual_no_cloud == []


def test_W21C2_manual_refusals(graph, tmp_path, monkeypatch, manual_no_cloud):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    missing = manual_params()
    del missing["confirm"]
    requests = [missing, manual_params(confirm=False), manual_params(confirm="true"),
                manual_params(cancel="true")]
    requests.extend(manual_params(**{key: value}) for key, value in
                    (("requests", {}), ("grant_ref", "unused"), ("panels_in_sequence", 12)))
    for request in requests:
        assert refusal(backend, request) == "INVALID_SIZING_REQUEST"
        assert latest(backend) == 1
    assert refusal(backend, manual_params(expected_rev=5)) == "STALE_GRAPH_REVISION"
    for length in (0, 4097):
        source = copy.deepcopy(graph)
        source["settings"]["panels_in_sequence"] = length
        with pytest.raises(GraphValidationError) as error:
            builtin().size_strings(source, manual_params(), tenant_id=TENANT_ID, job_id="manual")
        assert error.value.code == "INVALID_SIZING_REQUEST"
    assert latest(backend) == 1 and manual_no_cloud == []


def test_W21C2_manual_cancel(graph, tmp_path, monkeypatch, manual_no_cloud):
    before = copy.deepcopy(graph)
    request = manual_params(cancel=True)
    result = builtin().size_strings(graph, request, tenant_id=TENANT_ID, job_id="manual")
    assert result == {"graph": before, "confirmed": False, "records": {}}
    assert graph == before
    backend, _ = seed(tmp_path, monkeypatch, graph)
    assert refusal(backend, request) == "GRAPH_COMMIT_CANCELLED"
    assert latest(backend) == 1 and manual_no_cloud == []


def test_W21C2_manual_replay(graph, tmp_path, monkeypatch, manual_no_cloud):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with held(backend) as fence:
        first = dispatch(backend, fence, manual_params())
        replay = dispatch(backend, fence, manual_params())
    assert replay["replayed"] is True
    assert replay["new_version"] == first["new_version"]
    assert latest(backend) == 2 and manual_no_cloud == []


def test_W21C2_manual_evidence_tampering(graph, manual_no_cloud):
    confirmed = builtin().run_bound(graph, manual_params(), tenant_id=TENANT_ID, job_id="manual")
    mutations = [
        lambda g: g["settings"]["extra"]["string_sizing"].update(basis_sha256="0" * 64),
        lambda g: g["settings"].update(panels_in_sequence=3),
        lambda g: g["settings"].update(global_string_sizing_confirmed=False),
        lambda g: g["settings"].update(global_string_sizing_confirmed=1),
        lambda g: g["settings"]["extra"]["string_sizing"].update(records={"fake": {}}),
        lambda g: g["settings"]["extra"]["string_sizing"].update(records=[]),
        lambda g: g["settings"]["extra"]["string_sizing"].update(extra=True),
        lambda g: g["settings"]["extra"]["string_sizing"].pop("panels_in_sequence"),
        lambda g: g["panels"][0].update(angle=90),
    ]
    for key in confirmed["settings"]["voc_cold"]:
        mutations.append(lambda g, key=key: g["settings"]["voc_cold"].update({key: 0}))
    for value in (True, 2.0, "2", 0, 4097):
        mutations.append(lambda g, value=value: g["settings"]["extra"]["string_sizing"].update(
            panels_in_sequence=value))
    for mutate in mutations:
        candidate = copy.deepcopy(confirmed)
        mutate(candidate)
        with pytest.raises(GraphValidationError) as error:
            solar_sizing_client.require_sizing(candidate)
        assert error.value.code == "SIZING_CONFIRMATION_REQUIRED"
    assert manual_no_cloud == []


def test_W21C2_settings_invalidates_manual(graph, manual_no_cloud):
    confirmed = builtin().run_bound(graph, manual_params(), tenant_id=TENANT_ID, job_id="manual")
    changed = solar_local_graph._load_builtin("solar-settings").run(
        confirmed, {"expected_rev": 1, "changes": {"panels_in_sequence": 12}})
    assert changed["settings"]["panels_in_sequence"] == 12
    assert changed["settings"]["global_string_sizing_confirmed"] is False
    assert "string_sizing" not in changed["settings"]["extra"]
    with pytest.raises(GraphValidationError) as error:
        solar_sizing_client.require_sizing(changed)
    assert error.value.code == "SIZING_CONFIRMATION_REQUIRED"
    assert manual_no_cloud == []


def test_W21C2_manual_power_boundary(graph, manual_no_cloud):
    confirmed = builtin().run_bound(graph, manual_params(), tenant_id=TENANT_ID, job_id="manual")
    assert availability.w1_graph_readiness(confirmed)["solar-panel-groups"] == {
        "input_ready": False, "input_reason": "module_power_required"}
    from solar_sizing_power import frame_module_power, sizing_power_ready
    assert sizing_power_ready(confirmed) is False
    with pytest.raises(GraphValidationError) as error:
        frame_module_power(confirmed, confirmed["frames"][0]["panel_refs"],
                           confirmed["frames"][0]["electrical_zone_ref"])
    assert error.value.code == "MODULE_POWER_REQUIRED"
    assert manual_no_cloud == []
