"""Offline broker coverage for W5's unwired local graph commit adapter."""
import copy
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import write_loop
import store
import solar_local_graph as local
from envelopes import DEFAULT_HTTP_STATUS, ErrorCode
from product_capability_availability import W1_CAPABILITIES
from solar_design_graph import GraphValidationError
from test_w1_local_graph_adapter import seed, held, latest, settings
from test_w1_design_graph import graph  # noqa: F401

TENANT = "fixture-tenant"
JOB = "w1-broker-job"


def forbidden(*args, **kwargs):
    pytest.fail("unexpected execution")


@pytest.fixture
def rails(tmp_path, monkeypatch, graph):
    monkeypatch.setenv("BROKER_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("BROKER_TENANTS", str(tmp_path / "tenants.json"))
    monkeypatch.setenv("LEAF_BROKER_STORE", "legacy")
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "staging")
    import broker
    import requests

    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "_tenants", {TENANT: {"tier": "demo", "disabled": False}})
    monkeypatch.setattr(broker, "_cap_preflight", lambda *a: None)
    monkeypatch.setattr(broker, "_emit_aps_metric", lambda *a: None)
    monkeypatch.setattr(broker, "_get_da", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    backend, _ = seed(tmp_path, monkeypatch, graph)
    def backend_for_tenant(*args, **kwargs):
        assert args == (TENANT,)
        assert kwargs == {"aps_live": False, "da": None}
        return backend
    monkeypatch.setattr(write_loop, "backend_for_tenant", backend_for_tenant)
    tool = next(t for t in json.loads((SERVER / "write_tools.json").read_text())["tools"]
                if t["name"] == "solar-settings")
    with held(backend) as fence:
        yield broker, backend, tool, fence


@pytest.fixture
def enabled(rails, monkeypatch):
    monkeypatch.setitem(W1_CAPABILITIES["solar-settings"], "adapter", "local-graph-commit")
    monkeypatch.setattr(rails[0], "run_tool_dynamic", forbidden)
    monkeypatch.setattr(write_loop, "run_write_mock", forbidden)
    monkeypatch.setattr(write_loop, "run_write_live", forbidden)
    return rails


def call(rails, **overrides):
    broker, _, tool, fence = rails
    fields = dict(tenant_id=TENANT, tool=copy.deepcopy(tool), params=settings(),
                  dwg="solar", dwg_version=1, job_id=JOB, aps_live=False,
                  checkout_holder="fixture-owner", checkout_fence=fence)
    fields.update(overrides)
    # Prove the branch's own check as defence in depth, because the wire already refuses bools.
    parsed_fields = fields.copy()
    for field in ("dwg_version", "checkout_fence"):
        if type(fields[field]) is bool:
            parsed_fields[field] = 1
    request = broker.BrokerRunRequest(**parsed_fields)
    for field in ("dwg_version", "checkout_fence"):
        if type(fields[field]) is bool:
            setattr(request, field, fields[field])
    response = broker._broker_run(request)
    return response.status_code, json.loads(response.body)


@pytest.mark.parametrize("field", ["dwg_version", "checkout_fence"])
@pytest.mark.parametrize("value", [True, 1.0, "1"])
def test_wire_identity_refuses_coercion(enabled, monkeypatch, field, value):
    broker, _, tool, fence = enabled
    monkeypatch.setattr(broker, "_broker_run", forbidden)
    monkeypatch.setattr(local, "run_local_graph_commit", forbidden)
    fields = dict(tenant_id=TENANT, tool=copy.deepcopy(tool), params=settings(),
                  dwg="solar", dwg_version=1, job_id=JOB, aps_live=False,
                  checkout_holder="fixture-owner", checkout_fence=fence)
    fields[field] = value
    with pytest.raises(ValidationError):
        broker.BrokerRunRequest.model_validate(fields)


@pytest.mark.parametrize("field", ["dwg_version", "checkout_fence"])
@pytest.mark.parametrize("value", [1, None])
def test_wire_identity_accepts_int_and_none(enabled, field, value):
    broker, _, tool, fence = enabled
    fields = dict(tenant_id=TENANT, tool=copy.deepcopy(tool), params=settings(),
                  dwg="solar", dwg_version=1, job_id=JOB, aps_live=False,
                  checkout_holder="fixture-owner", checkout_fence=fence)
    fields[field] = value
    request = broker.BrokerRunRequest.model_validate(fields)
    assert getattr(request, field) is value


def test_shipped_kind_is_off(rails, monkeypatch):
    assert W1_CAPABILITIES["solar-settings"]["adapter"] is None
    monkeypatch.setattr(local, "run_local_graph_commit", forbidden)
    monkeypatch.setattr(write_loop, "default_backend", lambda *a, **k: rails[1])
    call(rails)


def test_commit(enabled):
    status, env = call(enabled)
    assert status == 200 and env["ok"] is True and env["degraded_mode"] is False
    result = env["result"]
    assert set(result) == {
        "schema_version", "adapter", "tenant_id", "job_id", "tool", "project_id",
        "drawing_id", "request_sha256", "new_version", "before_graph_sha256",
        "graph_sha256", "intake_sha256", "before_rev", "after_rev", "drawing_changed", "replayed",
    }
    assert result["schema_version"] == local.RESULT_SCHEMA
    assert result["adapter"] == local.ADAPTER_KIND
    assert result["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    assert (result["tenant_id"], result["job_id"], result["tool"]) == (TENANT, JOB, "solar-settings")
    assert result["replayed"] is False
    assert latest(enabled[1]) == 2


def test_replay(enabled):
    first_status, first = call(enabled)
    status, second = call(enabled)
    assert first_status == status == 200
    assert second["result"]["replayed"] is True
    assert second["result"]["new_version"] == first["result"]["new_version"]
    assert latest(enabled[1]) == 2


def test_terminal_proof(enabled):
    status, env = call(enabled)
    assert status == 200
    result = env["result"]
    proof = local.graph_commit_provenance(
        result, settings(), TENANT, JOB, "solar-settings", 1, backend=enabled[1])
    assert proof == {
        "execution_mode": "local_graph_commit", "adapter": local.ADAPTER_KIND,
        "request_sha256": result["request_sha256"], "graph_sha256": result["graph_sha256"],
        "intake_sha256": result["intake_sha256"], "source_version": 1, "new_version": 2,
    }


@pytest.mark.parametrize("mode", ["live", "file_only", "source", "version", "entry"])
def test_forbidden_modes(enabled, monkeypatch, mode):
    monkeypatch.setattr(local, "run_local_graph_commit", forbidden)
    overrides = {}
    if mode == "live":
        overrides["aps_live"] = True
    elif mode == "file_only":
        overrides.update(file_only=True, test_source="x")
    elif mode == "source":
        overrides["test_source"] = "x"
    else:
        tool = copy.deepcopy(enabled[2])
        tool[mode] = "9.9.9" if mode == "version" else "elsewhere.py"
        overrides["tool"] = tool
    _, env = call(enabled, **overrides)
    assert env["ok"] is False
    expected = "file_only_request_invalid" if mode == "file_only" else "local_graph_commit_invalid"
    assert env["error"]["reason_code"] == expected
    assert latest(enabled[1]) == 1


@pytest.mark.parametrize("field,value,reason", [
    ("job_id", None, "JOB_IDENTITY_MISSING"),
    ("dwg_version", None, "INVALID_PARENT_VERSION"),
    ("dwg_version", 0, "INVALID_PARENT_VERSION"),
    ("dwg_version", True, "INVALID_PARENT_VERSION"),
    ("checkout_holder", None, "CHECKOUT_REQUIRED"),
    ("checkout_holder", "", "CHECKOUT_REQUIRED"),
    ("checkout_holder", store.ANONYMOUS_HOLDER, "CHECKOUT_REQUIRED"),
    ("checkout_fence", None, "CHECKOUT_REQUIRED"),
    ("checkout_fence", 0, "CHECKOUT_REQUIRED"),
    ("checkout_fence", True, "CHECKOUT_REQUIRED"),
    ("drawing_id", "other", "DRAWING_ID_CONFLICT"),
])
def test_identity_refused_before_store(enabled, monkeypatch, field, value, reason):
    monkeypatch.setattr(local, "run_local_graph_commit", forbidden)
    monkeypatch.setattr(write_loop, "backend_for_tenant", forbidden)
    monkeypatch.setattr(enabled[0], "_start_admitted_execution", forbidden)
    overrides = {field: value}
    if field == "drawing_id":
        overrides = {"params": {**settings(), "drawing_id": value}}
    status, env = call(enabled, **overrides)
    code = ErrorCode.FORBIDDEN if reason == "CHECKOUT_REQUIRED" else ErrorCode.BAD_PARAMS
    assert status == DEFAULT_HTTP_STATUS[code]
    assert env["ok"] is False and env["error"]["reason_code"] == reason
    assert latest(enabled[1]) == 1


@pytest.mark.parametrize("reason,code", [
    ("STALE_GRAPH_REVISION", ErrorCode.BAD_PARAMS),
    ("JOB_BINDING_REUSED", ErrorCode.BAD_PARAMS),
    ("LICENSED_GRAPH_COMMIT_REQUIRED", ErrorCode.BAD_PARAMS),
    ("CHECKOUT_DENIED", ErrorCode.FORBIDDEN),
    ("GRAPH_COMMIT_READBACK_FAILED", ErrorCode.INTERNAL),
    ("GRAPH_COMMIT_CANCELLED", ErrorCode.BAD_PARAMS),
])
def test_adapter_refusals(enabled, monkeypatch, reason, code):
    def refuse(*args, **kwargs):
        raise GraphValidationError(reason)
    monkeypatch.setattr(local, "run_local_graph_commit", refuse)
    status, env = call(enabled)
    assert status == DEFAULT_HTTP_STATUS[code]
    assert env["ok"] is False and env["degraded_mode"] is False
    assert env["error"]["reason_code"] == env["error"]["message"] == reason
    assert env["error"]["error_code"] == code
    assert env["error"]["retryable"] is False


def test_real_stale_parent(enabled):
    assert call(enabled)[0] == 200
    _, env = call(enabled, job_id="another-job")
    assert env["ok"] is False and env["error"]["reason_code"] == "STALE_GRAPH_REVISION"
    assert latest(enabled[1]) == 2


def test_invalid_refusal_code(enabled, monkeypatch):
    def refuse(*args, **kwargs):
        raise GraphValidationError("lower case")
    monkeypatch.setattr(local, "run_local_graph_commit", refuse)
    _, env = call(enabled)
    assert env["error"]["reason_code"] == env["error"]["message"] == "GRAPH_COMMIT_REFUSED"


def test_transient_store_failure(enabled, monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("solar graph store access failed")
    monkeypatch.setattr(local, "run_local_graph_commit", refuse)
    status, env = call(enabled)
    assert status == DEFAULT_HTTP_STATUS[ErrorCode.INTERNAL]
    assert env["ok"] is False and env["degraded_mode"] is False
    assert env["error"]["error_code"] == ErrorCode.INTERNAL
    assert env["error"]["retryable"] is True
    assert env["error"]["reason_code"] == "GRAPH_STORE_UNAVAILABLE"
    assert env["error"]["message"] == "graph store unavailable"
    serialized = json.dumps(env)
    assert "solar graph store access failed" not in serialized
    assert str(SERVER) not in serialized and str(enabled[0].LEDGER_PATH.parent) not in serialized


def test_mutation_gate(enabled, monkeypatch):
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
    monkeypatch.setattr(local, "run_local_graph_commit", forbidden)
    _, env = call(enabled)
    assert env["ok"] is False
    assert latest(enabled[1]) == 1


def test_backend_acquisition_runtime_error(enabled, monkeypatch):
    def refuse(*args, **kwargs):
        raise RuntimeError("no client")
    monkeypatch.setattr(write_loop, "backend_for_tenant", refuse)
    monkeypatch.setattr(local, "run_local_graph_commit", forbidden)
    status, env = call(enabled)
    assert status == DEFAULT_HTTP_STATUS[ErrorCode.INTERNAL]
    assert env["ok"] is False and env["degraded_mode"] is False
    assert env["error"]["error_code"] == ErrorCode.INTERNAL
    assert env["error"]["retryable"] is True
    assert env["error"]["reason_code"] == "GRAPH_STORE_UNAVAILABLE"
    assert env["error"]["message"] == "graph store unavailable"
    assert "no client" not in json.dumps(env)
    assert latest(enabled[1]) == 1


def test_adapter_runtime_error_is_not_store_failure(enabled, monkeypatch):
    def refuse(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(local, "run_local_graph_commit", refuse)
    status, env = call(enabled)
    assert status == DEFAULT_HTTP_STATUS[ErrorCode.INTERNAL]
    assert env["ok"] is False
    assert env["error"]["error_code"] == ErrorCode.INTERNAL
    assert env["error"].get("reason_code") != "GRAPH_STORE_UNAVAILABLE"
    assert "boom" not in env["error"]["message"]
    assert latest(enabled[1]) == 1


def test_ledger(enabled):
    assert call(enabled)[0] == 200
    entry = json.loads(enabled[0].LEDGER_PATH.read_text().splitlines()[-1])
    assert entry["aps_live"] is False and entry["aps_endpoint"] is None
    assert entry["tool"] == "solar-settings"


def test_refused_live_ledger_is_local(enabled, monkeypatch):
    monkeypatch.setattr(local, "run_local_graph_commit", forbidden)
    _, env = call(enabled, aps_live=True)
    assert env["ok"] is False
    assert env["error"]["reason_code"] == "local_graph_commit_invalid"
    entry = json.loads(enabled[0].LEDGER_PATH.read_text().splitlines()[-1])
    assert entry["aps_live"] is False and entry["aps_endpoint"] is None


def test_packaged_builtin_ignores_decoy(enabled, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "builtins").mkdir()
    (tmp_path / "builtins" / "solar_settings.py").write_text('raise RuntimeError("decoy loaded")\n')
    local._load_builtin.cache_clear()
    try:
        assert call(enabled)[0] == 200
        module = local._load_builtin("solar-settings")
        assert Path(module.__file__).resolve().parent == (SERVER / "builtins").resolve()
    finally:
        local._load_builtin.cache_clear()


def test_params_schema_refuses_first(enabled, monkeypatch):
    monkeypatch.setattr(local, "run_local_graph_commit", forbidden)
    _, env = call(enabled, params={"expected_rev": 0, "nope": 1})
    assert env["ok"] is False and env["error"]["reason_code"] == "tool_params_invalid"
    assert latest(enabled[1]) == 1


@pytest.mark.parametrize("qa_hooks", ["1", "0"])
def test_qa_key_is_refused(enabled, monkeypatch, qa_hooks):
    monkeypatch.setenv("LEAF_QA_HOOKS", qa_hooks)
    monkeypatch.setattr(local, "run_local_graph_commit", forbidden)
    _, env = call(enabled, params={**settings(), "_qa_sleep_s": 0})
    assert env["ok"] is False
    assert env["error"]["reason_code"] == "tool_params_invalid"
    assert latest(enabled[1]) == 1


def test_other_kinds_still_pop_qa_key():
    source = (SERVER / "broker.py").read_text(encoding="utf-8")
    assert '    if not local_graph:\n        qa_sleep = params.pop("_qa_sleep_s", None)\n' in source
