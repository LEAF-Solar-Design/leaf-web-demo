"""Focused regressions for the app/broker APS credential and file boundary."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import broker  # noqa: E402
import broker_client  # noqa: E402
import tool_loader  # noqa: E402
import write_loop  # noqa: E402
from routers import session as session_router  # noqa: E402


def _ok_env(*_a, **_k):
    """A schema-valid ok envelope — stand-in for a real tool run so auth/entitlement
    tests never depend on tool-execution internals."""
    return {"ok": True, "tool": "t", "version": "1.0.0", "result": {}, "overlay": None,
            "timing_ms": 1, "cost": None, "error": None, "degraded_mode": False}


class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


@pytest.mark.parametrize("requested", ["rooftop_demo", "rooftop-demo", "demo"])
def test_live_session_extracts_curated_alias_through_broker_only(monkeypatch, requested):
    calls = []

    def fake_post(url, *, json, headers, timeout):
        calls.append((url, json, headers, timeout))
        return _Response(200, {"intake": {"polylines": []}, "error": None,
                               "degraded_mode": False})

    monkeypatch.setattr(session_router.deps, "APS_LIVE", True)
    monkeypatch.setattr(session_router.broker_client, "broker_url", lambda: "http://broker:8140")
    monkeypatch.setattr(session_router.broker_client, "broker_headers",
                        lambda: {"X-Broker-Secret": "test-secret"})
    monkeypatch.setattr(session_router.requests, "post", fake_post)
    monkeypatch.setattr(session_router, "_stored_drawing_intake", lambda *_args: None)

    body = session_router.session(dwg=requested, tenant="tenant-a")

    assert body["intake"] == {"polylines": []}
    assert calls == [("http://broker:8140/broker/extract",
                      {
                          "tenant_id": "tenant-a",
                          "dwg": "rooftop_demo",
                          "ledger_event_key": broker_client.extract_event_key(
                              "tenant-a", "rooftop_demo"),
                      },
                      {"X-Broker-Secret": "test-secret"}, 600)]
    source = Path(session_router.__file__).read_text(encoding="utf-8")
    assert "get_da_client" not in source
    assert "da.client" not in source


def test_live_session_reads_uploaded_drawing_from_tenant_store(
        tmp_path, monkeypatch):
    import store  # noqa: PLC0415

    tenant = "tenant-upload-owner"
    drawing_id = "9d1d6816-1978-4c38-b9df-e0b2b2b0903d"
    backend = store.InMemoryBackend()
    source = tmp_path / "uploaded.intake.json"
    source.write_text(
        json.dumps({"polylines": [{"handle": "OWNER", "layer": "Panels",
                                    "pts": [[1, 2], [3, 4]]}]}),
        encoding="utf-8",
    )
    store.ingest_drawing(backend, tenant, str(source), drawing_id=drawing_id)

    monkeypatch.setattr(session_router.deps, "APS_LIVE", True)
    monkeypatch.setattr(
        write_loop, "upload_backend_for_tenant", lambda _tenant: backend)
    monkeypatch.setattr(
        session_router.requests, "post",
        lambda *_a, **_k: pytest.fail("stored drawing must not reach broker"),
    )

    body = session_router.session(dwg=drawing_id, tenant=tenant)

    assert body["intake"]["polylines"][0]["handle"] == "OWNER"


def test_live_session_never_reads_another_tenants_uploaded_drawing(
        tmp_path, monkeypatch):
    import store  # noqa: PLC0415

    owner = "tenant-upload-owner"
    intruder = "tenant-upload-intruder"
    drawing_id = "6f594bad-70e0-472d-9bf9-a0a73c4ffb3e"
    backend = store.InMemoryBackend()
    source = tmp_path / "private.intake.json"
    source.write_text(
        json.dumps({"polylines": [{"handle": "PRIVATE", "layer": "Panels",
                                    "pts": [[5, 6], [7, 8]]}]}),
        encoding="utf-8",
    )
    store.ingest_drawing(backend, owner, str(source), drawing_id=drawing_id)
    calls = []

    def fake_post(url, *, json, headers, timeout):
        calls.append((url, json, headers, timeout))
        return _Response(404, {
            "ok": False,
            "error": {"error_code": "BAD_PARAMS", "message": "unknown drawing",
                      "retryable": False},
            "degraded_mode": False,
        })

    monkeypatch.setattr(session_router.deps, "APS_LIVE", True)
    monkeypatch.setattr(
        write_loop, "upload_backend_for_tenant", lambda _tenant: backend)
    monkeypatch.setattr(session_router.broker_client, "broker_url",
                        lambda: "http://broker:8140")
    monkeypatch.setattr(session_router.broker_client, "broker_headers", lambda: {})
    monkeypatch.setattr(session_router.requests, "post", fake_post)

    response = session_router.session(dwg=drawing_id, tenant=intruder)

    assert response.status_code == 404
    assert b"PRIVATE" not in response.body
    assert calls[0][1]["tenant_id"] == intruder


def test_live_session_does_not_retry_durable_stored_drawing_corruption(
        monkeypatch):
    monkeypatch.setattr(session_router.deps, "APS_LIVE", True)
    monkeypatch.setattr(
        session_router, "_stored_drawing_intake",
        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("digest mismatch")),
    )
    monkeypatch.setattr(
        session_router.requests, "post",
        lambda *_a, **_k: pytest.fail("corrupt stored drawing must not reach broker"),
    )

    response = session_router.session(
        dwg="4071d443-e5cb-4dd1-bc14-3b0bec735577", tenant="tenant-a")
    body = json.loads(response.body)

    assert response.status_code == 500
    assert body["error"]["retryable"] is False
    assert "digest mismatch" in body["error"]["message"]


def test_storage_cutover_gate_blocks_broker_write_before_preflight(monkeypatch):
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
    monkeypatch.setenv("LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED", "1")
    monkeypatch.setattr(broker, "tenant_disabled", lambda tenant_id: False)
    monkeypatch.setattr(
        broker,
        "_cap_preflight",
        lambda *args: pytest.fail("mutation gate ran after cost preflight"),
    )
    tool = {"name": "write", "capabilities": ["drawing.write"]}
    env, status = broker._execute(
        broker.BrokerRunRequest(
            tenant_id="tenant-a", tool=tool, params={}, aps_live=True,
        ),
        tool,
        "write",
        0.0,
        {},
    )
    assert status == 503
    assert env["error"]["retryable"] is True


def test_staged_source_is_hashed_not_recorded_in_request_fingerprint():
    source_a = "def run(intake, params):\n    return ({'value': 'secret-a'}, None)\n"
    base = dict(
        tenant_id="tenant-a",
        tool={"name": "candidate", "entry": "tools/candidate/tool.py"},
        params={},
        aps_live=False,
    )
    first = broker._broker_request_fingerprint(
        broker.BrokerRunRequest(**base, test_source=source_a))
    second = broker._broker_request_fingerprint(
        broker.BrokerRunRequest(**base, test_source=source_a.replace("secret-a", "secret-b")))

    assert first != second
    assert "secret-a" not in first


def test_ordinary_request_fingerprint_remains_backward_compatible():
    req = broker.BrokerRunRequest(
        tenant_id="tenant-a",
        tool={"name": "ordinary", "params_schema": {"type": "object"}},
        params={"x": 1},
        dwg="rooftop_demo",
        aps_live=False,
    )
    old_canonical = json.dumps({
        "tenant_id": req.tenant_id,
        "tool": req.tool,
        "params": req.params,
        "dwg": req.dwg,
        "aps_live": False,
        "dwg_version": None,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    expected = broker.hashlib.sha256(old_canonical.encode("utf-8")).hexdigest()

    assert broker._broker_request_fingerprint(req) == expected


def test_broker_refuses_staged_source_on_live_aps_before_runner(monkeypatch):
    monkeypatch.setattr(broker, "tenant_disabled", lambda _tenant: False)
    monkeypatch.setattr(broker, "_cap_preflight", lambda _tenant, _tool: None)
    monkeypatch.setattr(
        broker, "run_tool_dynamic",
        lambda *args, **kwargs: pytest.fail("live staged source reached the runner"),
    )
    tool = {
        "name": "candidate",
        "entry": "tools/candidate/tool.py",
        "params_schema": {"type": "object"},
    }
    env, status = broker._execute(
        broker.BrokerRunRequest(
            tenant_id="tenant-a", tool=tool, params={}, aps_live=True,
            test_source="def run(intake, params):\n    return ({}, None)\n",
        ),
        tool,
        "candidate",
        0.0,
        {},
    )

    assert status == 400
    assert env["error"]["error_code"] == "BAD_PARAMS"


def test_broker_forces_staged_write_source_to_dry_run(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "drawings"))
    monkeypatch.setenv("LEAF_SANDBOX", "e2b")
    monkeypatch.delenv("LEAF_TOOL_SANDBOX_PROVIDER", raising=False)
    monkeypatch.setattr(broker, "tenant_disabled", lambda _tenant: False)
    monkeypatch.setattr(broker, "_cap_preflight", lambda _tenant, _tool: None)
    original_run_tool_dynamic = broker.run_tool_dynamic
    observed_runner_calls = []

    def observed_run_tool_dynamic(*args, **kwargs):
        observed_runner_calls.append(dict(kwargs))
        return original_run_tool_dynamic(*args, **kwargs)

    monkeypatch.setattr(broker, "run_tool_dynamic", observed_run_tool_dynamic)
    tool = {
        "name": "candidate-writer",
        "version": "1.0.0",
        "entry": "tools/candidate-writer/tool.py",
        "capabilities": ["drawing.write"],
        "params_schema": {"type": "object"},
    }
    source = (
        "def run(intake, params):\n"
        "    target = intake['polylines'][0]\n"
        "    return ({'mutations': {'transforms': [{\n"
        "        'handle': target['handle'], 'dx': 4, 'dy': 2, 'rotation_deg': 0\n"
        "    }]}}, None)\n"
    )

    env, status = broker._execute(
        broker.BrokerRunRequest(
            tenant_id="tenant-a", tool=tool,
            params={"drawing_id": "staged-safety", "dry_run": False},
            aps_live=False, test_source=source,
        ),
        tool,
        "candidate_writer",
        0.0,
        {},
    )

    assert status == 200
    assert len(observed_runner_calls) == 1
    assert observed_runner_calls[0]["tenant_id"] == "tenant-a"
    assert observed_runner_calls[0]["test_source"] == source
    assert env["result"]["dry_run"] is True
    assert "new_version" not in env["result"]
    backend = broker.write_loop.default_backend(aps_live=False)
    version, _ = broker.write_loop.read_intake(
        backend, "tenant-a", "staged-safety", "head")
    assert version == 1


_HOST_VALIDATOR_SOURCE = '''
import re
import uuid

def run(intake, params):
    def require(ok):
        if not ok:
            raise ValueError("invalid evidence")
    def closed(value, keys):
        require(isinstance(value, dict) and set(value) == set(keys.split()))
    def digest(value, prefix="", length=64):
        require(isinstance(value, str) and re.fullmatch(prefix + "[0-9a-f]{%d}" % length, value) is not None)
    def canonical(value):
        require(isinstance(value, str) and str(uuid.UUID(value)) == value)
    def token(value, limit):
        require(isinstance(value, str) and 1 <= len(value) <= limit and
                all(ord(c) >= 32 and ord(c) != 127 for c in value))
    require(params == {})
    closed(intake, "schema job_id operation_id input_sha256 capability_provenance host_readback")
    require(intake["schema"] == "leaf.campaign-host-validation.v1")
    canonical(intake["job_id"])
    canonical(intake["operation_id"])
    digest(intake["input_sha256"])
    context = intake["capability_provenance"]
    closed(context, "schema tenant_id org_id project_id campaign_id enrollment_id link_id capability tool_name change_set_id catalog_commit effective_catalog_digest tool_manifest_sha256 tool_source_sha256 profile_selector")
    for key in ("org_id", "project_id", "campaign_id", "enrollment_id", "link_id"):
        canonical(context[key])
    for key, value in {"schema": "leaf.campaign-capability.v1", "capability": "campaign.host-enrollment", "tool_name": "campaign-host-enrollment", "profile_selector": "campaign-default-v1"}.items():
        require(context[key] == value)
    token(context["tenant_id"], 32768)
    token(context["change_set_id"], 200)
    digest(context["catalog_commit"], length=40)
    digest(context["effective_catalog_digest"])
    digest(context["tool_manifest_sha256"], "sha256:")
    digest(context["tool_source_sha256"])
    readback = intake["host_readback"]
    closed(readback, "config_identity_before config_identity_after readback_sha256 reason")
    if readback["config_identity_before"] is not None:
        digest(readback["config_identity_before"])
    digest(readback["config_identity_after"])
    digest(readback["readback_sha256"])
    require(readback["reason"] in ("verified", "already_applied"))
    return {"verified": True, "operation_id": intake["operation_id"],
            "input_sha256": intake["input_sha256"],
            "readback_sha256": readback["readback_sha256"]}
'''


def _host_fixture_request(source=_HOST_VALIDATOR_SOURCE, **changes):
    fields = dict(
        tenant_id="fixture-request-tenant",
        tool={"name": "campaign-host-enrollment", "version": "1.0.0",
              "entry": "tools/campaign-host-enrollment/tool.py", "capabilities": [],
              "params_schema": {"type": "object", "additionalProperties": False}},
        params={}, aps_live=False, test_source=source,
    )
    fields.update(changes)
    return broker.BrokerRunRequest(**fields)


@pytest.fixture
def host_fixture_runner(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_SANDBOX", "e2b")
    monkeypatch.delenv("LEAF_TOOL_SANDBOX_PROVIDER", raising=False)
    monkeypatch.setattr(broker, "tenant_disabled", lambda _tenant: False)
    monkeypatch.setattr(broker, "_cap_preflight", lambda *_args: None)
    monkeypatch.setattr(broker, "_tenant_tier", lambda _tenant: "demo")
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "_broker_store_mode", lambda: "legacy")
    monkeypatch.setattr(broker, "_get_da", lambda: pytest.fail("fixture reached APS"))
    monkeypatch.setattr(broker.platform_link, "platform_store",
                        lambda: pytest.fail("fixture reached host authority"))
    original = broker.run_tool_dynamic
    calls = []

    def observed(tool, intake, params, **kwargs):
        env = original(tool, intake, params, **kwargs)
        calls.append((json.loads(json.dumps(intake)), dict(params), kwargs, env))
        return env

    monkeypatch.setattr(broker, "run_tool_dynamic", observed)
    return calls


def _execute_host_fixture(req):
    return broker._execute(req, req.tool, "campaign_host_enrollment", 0.0, {})


def test_campaign_host_fixture_accepts_strict_captured_validator(host_fixture_runner):
    req = _host_fixture_request()
    env, status = _execute_host_fixture(req)
    calls = host_fixture_runner
    assert status == 200
    assert len(calls) == 10
    assert env is calls[0][3]
    assert env["tool"] == req.tool["name"] and env["version"] == "1.0.0"
    assert [call[3]["ok"] for call in calls] == [True, True] + [False] * 8
    assert calls[0][3]["result"] != calls[1][3]["result"]
    for intake, params, kwargs, result in calls:
        assert params == {} and kwargs["aps_live"] is False
        assert kwargs["test_source"] == req.test_source
        assert kwargs["tenant_id"] == req.tenant_id and kwargs["da"] is None
        assert intake["capability_provenance"]["tenant_id"] == "synthetic-broker-host-validation"
        if not result["ok"]:
            assert result["error"]["message"].startswith("tool 'campaign-host-enrollment' raised ")
    for intake, _, _, _ in calls[:2]:
        canonical = json.dumps({"schema": "leaf.campaign-host-operation.v1",
                                "job_id": intake["job_id"],
                                "context": intake["capability_provenance"]},
                               sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        assert intake["input_sha256"] == broker.hashlib.sha256(canonical.encode()).hexdigest()
    response = broker.broker_run(req)
    assert response.status_code == 200
    ledger = broker.LEDGER_PATH.read_text(encoding="utf-8").splitlines()
    assert len(ledger) == 1 and req.test_source not in ledger[0]


def test_campaign_host_fixture_rejects_always_success_validator(host_fixture_runner):
    source = '''
def run(intake, params):
    return {"verified": True, "operation_id": intake["operation_id"],
            "input_sha256": intake["input_sha256"],
            "readback_sha256": intake["host_readback"]["readback_sha256"]}
'''
    env, status = _execute_host_fixture(_host_fixture_request(source))
    assert status == 400 and env["ok"] is False
    assert len(host_fixture_runner) == 3
    assert all(call[3]["ok"] for call in host_fixture_runner)
    assert env["error"]["message"] == "host validation fixture failed"


@pytest.mark.parametrize("change", [
    'result["operation_id"] = "00000000-0000-4000-8000-000000000099"',
    'result["receipt"] = "fabricated"',
    'result["verified"] = 1',
    'overlay = {"polylines": []}',
])
def test_campaign_host_fixture_rejects_wrong_result_or_overlay(host_fixture_runner, change):
    source = _HOST_VALIDATOR_SOURCE.replace("def run(intake, params):", "def validate(intake, params):")
    source += ('\ndef run(intake, params):\n    result = validate(intake, params)\n'
               '    overlay = None\n    ' + change + '\n    return result, overlay\n')
    env, status = _execute_host_fixture(_host_fixture_request(source))
    assert status == 400 and env["error"]["retryable"] is False
    assert env["error"]["message"] == "host validation fixture failed"


@pytest.mark.parametrize("failure", ["transport", "timeout", "disabled", "params"])
def test_campaign_host_fixture_infrastructure_failure_is_not_negative_pass(
        host_fixture_runner, monkeypatch, failure):
    original = tool_loader._run_source_in_sandbox
    count = 0

    def sandbox(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 3:
            return "infra_error", "tool 'campaign-host-enrollment' raised spoofed transport"
        return original(*args, **kwargs)

    monkeypatch.setattr(tool_loader, "_run_source_in_sandbox", sandbox)
    if failure == "timeout":
        def sandbox_timeout(*args, **kwargs):
            raise TimeoutError("private infrastructure detail")
        monkeypatch.setattr(tool_loader, "_run_source_in_sandbox", sandbox_timeout)
    elif failure == "disabled":
        monkeypatch.setattr(tool_loader, "_sandbox_tier", lambda: "off")
    req = _host_fixture_request()
    if failure == "params":
        req.tool["params_schema"]["required"] = ["unavailable"]
    env, status = _execute_host_fixture(req)
    assert status != 200 and env["ok"] is False
    assert env["error"]["retryable"] is False
    if failure != "params":
        assert env["error"]["message"] == "host validation fixture failed"
    if failure == "transport":
        assert count == 3


@pytest.mark.parametrize("excluded", [
    "ordinary", "other_tool", "absent_source", "blank_source", "params",
    "capabilities", "missing_capabilities", "write", "live", "pinned", "drawing",
])
def test_campaign_host_fixture_selection_preserves_ordinary_cad(
        host_fixture_runner, monkeypatch, tmp_path, excluded):
    req = _host_fixture_request()
    if excluded == "ordinary":
        req.tool["name"] += "-ordinary"
    elif excluded == "other_tool":
        req.tool["name"] = "reader"
    elif excluded == "absent_source":
        req.test_source = None
    elif excluded == "blank_source":
        req.test_source = " "
    elif excluded == "params":
        req.params = {"x": 1}
    elif excluded == "capabilities":
        req.tool["capabilities"] = ["drawing.read"]
    elif excluded == "missing_capabilities":
        del req.tool["capabilities"]
    elif excluded == "write":
        req.tool["capabilities"] = ["drawing.write"]
    elif excluded == "live":
        req.aps_live = True
    elif excluded == "pinned":
        req.dwg_version = 1
    else:
        req.dwg = "uploaded"
    assert not broker._is_campaign_host_fixture(req)
    req.tool["params_schema"] = {"type": "object"}
    drawing = {"polylines": [{"layer": "Original drawing"}]}
    monkeypatch.setattr(broker, "DATA_FILE", tmp_path / "intake.json")
    broker.DATA_FILE.write_text(json.dumps(drawing), encoding="utf-8")
    seen = []
    monkeypatch.setattr(broker, "run_tool_dynamic",
                        lambda tool, intake, params, **kw: seen.append(intake) or _ok_env())
    monkeypatch.setattr(broker.write_loop, "backend_for_tenant", lambda *a, **k: object())
    monkeypatch.setattr(broker.write_loop, "ensure_demo_drawing", lambda *a: None)
    monkeypatch.setattr(broker.write_loop, "read_intake", lambda *a: (1, drawing))
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
    monkeypatch.setattr(broker.write_loop, "default_backend", lambda **kw: object())

    def write_mock(tool, params, tenant, **kwargs):
        assert params["dry_run"] is True
        seen.append("dry_run")
        return _ok_env(), 200

    monkeypatch.setattr(broker.write_loop, "run_write_mock", write_mock)
    env, status = _execute_host_fixture(req)
    if excluded in ("live", "blank_source"):
        assert status == 400 and seen == []
    elif excluded == "write":
        assert status == 200 and seen == ["dry_run"]
    else:
        assert status == 200 and seen == [drawing]


def test_campaign_host_fixture_fingerprint_is_versioned_and_scoped(monkeypatch):
    selected = _host_fixture_request()
    first = broker._broker_request_fingerprint(selected)
    assert broker._broker_request_fingerprint(_host_fixture_request()) == first
    assert broker._broker_request_fingerprint(_host_fixture_request(_HOST_VALIDATOR_SOURCE + "\n")) != first
    ordinary = _host_fixture_request(source=None)
    canonical = json.dumps({"tenant_id": ordinary.tenant_id, "tool": ordinary.tool,
                            "params": {}, "dwg": "rooftop_demo", "aps_live": False,
                            "dwg_version": None}, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    expected = broker.hashlib.sha256(canonical.encode()).hexdigest()
    assert broker._broker_request_fingerprint(ordinary) == expected
    monkeypatch.setattr(broker, "_CAMPAIGN_HOST_FIXTURE_PROFILE", "leaf.campaign-host-validation-fixture.v2")
    assert broker._broker_request_fingerprint(selected) != first
    assert broker._broker_request_fingerprint(ordinary) == expected
    staged_cad = _host_fixture_request()
    staged_cad.tool["name"] = "ordinary-reader"
    old_input = {"tenant_id": staged_cad.tenant_id, "tool": staged_cad.tool,
                 "params": {}, "dwg": "rooftop_demo", "aps_live": False,
                 "dwg_version": None,
                 "test_source_sha256": broker.hashlib.sha256(
                     staged_cad.test_source.encode()).hexdigest()}
    old_bytes = json.dumps(old_input, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode()
    assert broker._broker_request_fingerprint(staged_cad) == broker.hashlib.sha256(old_bytes).hexdigest()


def test_broker_extract_rechecks_shared_fence_after_paid_work(monkeypatch, tmp_path):
    from contextlib import contextmanager

    drawing = tmp_path / "drawing.dwg"
    drawing.write_bytes(b"DWG")
    checks = iter((True, False))
    calls = []

    class _Da:
        def extract(self, path):
            calls.append(path)
            return {"layers": [], "polylines": []}

    monkeypatch.setenv("LEAF_BROKER_STORE", "legacy")
    monkeypatch.setattr(broker, "tenant_disabled", lambda _tenant: False)
    # Extraction is the UPLOAD lane, so it reads the shared fence directly
    # (fence_open) rather than the authored lane's drawing_mutations_enabled.
    # Patching the authored names here would leave the real fence in charge and
    # the test would never reach its post-work recheck.
    monkeypatch.setattr(
        broker.write_loop, "fence_open", lambda: next(checks))

    @contextmanager
    def admitted_commit():
        yield True

    monkeypatch.setattr(
        broker.write_loop, "upload_mutation_commit_guard", admitted_commit)
    monkeypatch.setattr(
        broker, "_resolve_upload_dwg", lambda _dwg, _tenant: drawing)
    monkeypatch.setattr(broker, "_get_da", lambda: _Da())

    response = broker.broker_extract(
        broker.BrokerExtractRequest(
            tenant_id="tenant-a", dwg="drawing", upload=True))

    assert response.status_code == 503
    assert calls == [str(drawing)]


def test_broker_extract_read_lane_ignores_the_mutation_fence(monkeypatch, tmp_path):
    """`upload=False` is a READ (routers/session.py's live intake path).

    It must not 503 during a cutover drain, and must not enter the commit guard
    at all -- holding the shared fence across an APS call that can run for
    minutes would delay the exclusive lock the cutover control needs.
    """
    from contextlib import contextmanager

    drawing = tmp_path / "drawing.dwg"
    drawing.write_bytes(b"DWG")
    calls = []

    class _Da:
        def extract(self, path):
            calls.append(path)
            return {"layers": [], "polylines": []}

    monkeypatch.setenv("LEAF_BROKER_STORE", "legacy")
    monkeypatch.setattr(broker, "tenant_disabled", lambda _tenant: False)
    # Fence fully drained: a read must still be served.
    monkeypatch.setattr(broker.write_loop, "fence_open", lambda: False)

    @contextmanager
    def must_not_be_entered():
        raise AssertionError(
            "read-lane extraction entered the upload commit guard")
        yield  # pragma: no cover - unreachable, keeps this a generator

    monkeypatch.setattr(
        broker.write_loop, "upload_mutation_commit_guard", must_not_be_entered)
    monkeypatch.setattr(broker, "_resolve_live_dwg", lambda _dwg: drawing)
    monkeypatch.setattr(broker, "_get_da", lambda: _Da())

    response = broker.broker_extract(
        broker.BrokerExtractRequest(tenant_id="tenant-a", dwg="drawing"))

    assert response.status_code == 200
    assert calls == [str(drawing)]


def test_mock_session_default_dwg_serves_cached_intake_and_never_calls_broker(monkeypatch):
    """APS_LIVE=0 + the default drawing -> the unchanged cached-intake path.

    (Until §19 the offline branch ignored `dwg` entirely; a non-default name now
    reads the tenant's own store — covered by the next test. This one pins the
    byte-identical demo default.)"""
    monkeypatch.setattr(session_router.deps, "APS_LIVE", False)
    monkeypatch.setattr(session_router.deps, "load_cached_intake",
                        lambda: {"polylines": [{"layer": "Panels"}]})
    monkeypatch.setattr(
        session_router.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("mock session called broker"),
    )

    body = session_router.session(dwg="rooftop_demo", tenant="demo-tenant")
    assert body == {
        "intake": {"polylines": [{"layer": "Panels"}]},
        "error": None,
        "degraded_mode": False,
    }


def test_mock_session_non_default_dwg_reads_tenant_store_never_broker(monkeypatch):
    """APS_LIVE=0 + a NON-default drawing -> the tenant's own versioned store
    (§19: serving the cached demo intake under a user's drawing name would be
    fabricated data). Still strictly app-process-local: the broker is never
    called on any offline read."""
    import write_loop  # the module the router lazily imports inside the branch

    calls = []
    sentinel_backend = object()
    monkeypatch.setattr(session_router.deps, "APS_LIVE", False)
    monkeypatch.setattr(
        session_router.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("offline session called broker"),
    )
    monkeypatch.setattr(write_loop, "backend_for_tenant",
                        lambda tid, **kw: calls.append(("backend", tid)) or sentinel_backend)
    monkeypatch.setattr(write_loop, "ensure_demo_drawing",
                        lambda backend, tid, did: calls.append(("ensure", backend, tid, did)))
    monkeypatch.setattr(write_loop, "read_intake",
                        lambda backend, tid, did, version: (
                            calls.append(("read", backend, tid, did, version))
                            or (1, {"polylines": [{"layer": "Uploaded"}]})))

    body = session_router.session(dwg="my-upload", tenant="demo-tenant")

    assert body == {
        "intake": {"polylines": [{"layer": "Uploaded"}]},
        "error": None,
        "degraded_mode": False,
    }
    assert calls == [
        ("backend", "demo-tenant"),
        ("ensure", sentinel_backend, "demo-tenant", "my-upload"),
        ("read", sentinel_backend, "demo-tenant", "my-upload", "head"),
    ]


@pytest.mark.parametrize(
    "dwg",
    [
        "../secret",
        "..\\secret",
        "/tmp/secret",
        "C:\\tmp\\secret",
        "nested/drawing",
        "nested\\drawing",
        "rooftop_demo.dwg",
        "secret.txt.dwg",
        ".",
        "",
    ],
)
def test_live_dwg_resolver_rejects_paths_separators_and_suffixes(dwg):
    with pytest.raises(ValueError):
        broker._resolve_live_dwg(dwg)


def test_live_dwg_resolver_accepts_registered_store_name():
    resolved = broker._resolve_live_dwg("rooftop_demo")
    assert resolved == (broker.DATA_DIR / "rooftop_demo.dwg").resolve()


def test_live_dwg_resolver_maps_store_demo_alias_to_registered_source():
    """The version store calls the curated drawing ``demo``, while the APS
    source registry calls the same drawing ``rooftop_demo``. Both public ids
    must resolve to the same source instead of making live instant tools look
    for a nonexistent ``demo.dwg`` file.
    """
    canonical = broker._resolve_live_dwg("rooftop_demo")
    assert broker._resolve_live_dwg("demo") == canonical


def test_live_dwg_resolver_rejects_symlink_even_when_target_is_inside(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    target = store / "real.dwg"
    target.write_bytes(b"dwg")
    link = store / "linked.dwg"
    try:
        link.symlink_to(target)
    except OSError:
        # Windows without Developer Mode may prohibit creating test symlinks;
        # still exercise the explicit final-component symlink guard.
        link.write_bytes(b"dwg")
        original = Path.is_symlink
        monkeypatch.setattr(
            Path,
            "is_symlink",
            lambda self: True if self == link else original(self),
        )
    monkeypatch.setattr(broker, "DATA_DIR", store)
    with pytest.raises(ValueError, match="symlink"):
        broker._resolve_live_dwg("linked")


def test_broker_extract_rejects_traversal_before_loading_da(monkeypatch):
    monkeypatch.setattr(broker, "tenant_disabled", lambda _tenant: False)
    monkeypatch.setattr(
        broker,
        "_get_da",
        lambda: pytest.fail("invalid drawing reached the APS client"),
    )
    response = broker.broker_extract(
        broker.BrokerExtractRequest(tenant_id="tenant-a", dwg="../credentials")
    )
    assert response.status_code == 400
    assert b'"error_code":"BAD_PARAMS"' in response.body


def test_broker_live_run_rejects_traversal_before_loading_da(monkeypatch, tmp_path):
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "tenant_disabled", lambda _tenant: False)
    monkeypatch.setattr(broker, "_cap_preflight", lambda _tenant, _tool: None)
    monkeypatch.setattr(
        broker,
        "_get_da",
        lambda: pytest.fail("invalid drawing reached the APS client"),
    )
    req = broker.BrokerRunRequest(
        tenant_id="tenant-a",
        tool={"name": "safe-read", "params_schema": {"type": "object"}},
        params={},
        dwg="..\\credentials",
        aps_live=True,
    )
    response = broker.broker_run(req)
    assert response.status_code == 400
    assert b'"error_code":"BAD_PARAMS"' in response.body


def test_live_write_resolves_input_from_version_store_not_local_registry(monkeypatch):
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
    monkeypatch.setattr(broker, "tenant_disabled", lambda _tenant: False)
    monkeypatch.setattr(broker, "_cap_preflight", lambda _tenant, _tool: None)
    monkeypatch.setattr(
        broker, "_run_quota_preflight", lambda _tenant, _tier, _tool: None)
    monkeypatch.setattr(
        broker,
        "_resolve_live_dwg",
        lambda _dwg: pytest.fail("live write consulted the local DWG registry"),
    )

    class _Da:
        def run_tool(self):
            raise AssertionError("run_write_live owns the DA call")

    da = _Da()
    backend = object()
    calls = []
    monkeypatch.setattr(broker, "_get_da", lambda: da)
    monkeypatch.setattr(
        broker.write_loop, "default_backend", lambda **_kwargs: backend)
    monkeypatch.setattr(
        broker.write_loop,
        "run_write_live",
        lambda tool, params, tenant_id, **kwargs: (
            calls.append((tool, params, tenant_id, kwargs))
            or (_ok_env(), 200)
        ),
    )
    tool = {
        "name": "authored-write",
        "version": "1.0.0",
        "capabilities": ["drawing.write"],
        "params_schema": {"type": "object"},
    }

    env, status = broker._execute(
        broker.BrokerRunRequest(
            tenant_id="tenant-a",
            tool=tool,
            params={"drawing_id": "project-drawing"},
            dwg="project-drawing",
            dwg_version=1,
            aps_live=True,
        ),
        tool,
        "authored_write",
        0.0,
        {},
    )

    assert status == 200
    assert env["ok"] is True
    assert len(calls) == 1
    assert calls[0][2] == "tenant-a"
    assert calls[0][3]["backend"] is backend
    assert calls[0][3]["da"] is da
    assert calls[0][3]["version"] == 1


# --------------------------------------------------------------------------- #
# F4(a): caller-auth on /broker/* (shared secret via X-Broker-Secret header)
# --------------------------------------------------------------------------- #
def test_require_broker_auth_401_on_wrong_or_absent_secret(monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_BROKER_SECRET", "s3cret-value")
    # absent header -> 401
    with pytest.raises(HTTPException) as ei:
        broker.require_broker_auth(x_broker_secret=None)
    assert ei.value.status_code == 401
    # wrong header -> 401
    with pytest.raises(HTTPException) as ei:
        broker.require_broker_auth(x_broker_secret="not-it")
    assert ei.value.status_code == 401
    # correct header -> allowed (no raise, returns None)
    assert broker.require_broker_auth(x_broker_secret="s3cret-value") is None


def test_require_broker_auth_fails_closed_when_unset_in_live_mode(monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.delenv("LEAF_BROKER_SECRET", raising=False)
    with pytest.raises(HTTPException) as ei:
        broker.require_broker_auth(x_broker_secret=None)
    assert ei.value.status_code == 503


def test_require_broker_auth_open_in_demo_mode(monkeypatch):
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.delenv("LEAF_BROKER_SECRET", raising=False)
    # off-live, no secret -> friction-free (byte-identical to today)
    assert broker.require_broker_auth(x_broker_secret=None) is None


def test_require_broker_auth_enforced_when_secret_set_even_off_live(monkeypatch):
    # a secret that is SET is ALWAYS enforced, regardless of live mode
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.setenv("LEAF_BROKER_SECRET", "abc")
    with pytest.raises(HTTPException) as ei:
        broker.require_broker_auth(x_broker_secret=None)
    assert ei.value.status_code == 401
    assert broker.require_broker_auth(x_broker_secret="abc") is None


def test_protected_routes_carry_auth_dependency_and_health_is_open():
    protected = {
        "/broker/run", "/broker/extract", "/broker/reap",
        "/broker/tenants/{tid}/disable", "/broker/tenants/{tid}/enable",
    }

    def _dep_calls(route):
        dep = getattr(route, "dependant", None)
        return [d.call for d in dep.dependencies] if dep is not None else []

    by_path = {getattr(r, "path", None): r for r in broker.app.routes}
    for path in protected:
        assert path in by_path, f"route {path} missing"
        assert broker.require_broker_auth in _dep_calls(by_path[path]), f"{path} not gated"
    # health MUST stay open (liveness probes carry no secret)
    assert broker.require_broker_auth not in _dep_calls(by_path["/broker/health"])


def test_broker_run_http_enforces_secret_in_live_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_BROKER_SECRET", "s3cret-value")
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "DATA_FILE", tmp_path / "intake.json")
    (tmp_path / "intake.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(broker, "run_tool_dynamic", _ok_env)
    client = TestClient(broker.app)
    body = {"tenant_id": "auth-test-tenant",
            "tool": {"name": "t", "params_schema": {"type": "object"}},
            "params": {}, "dwg": "rooftop_demo", "aps_live": False}
    assert client.post("/broker/run", json=body).status_code == 401
    assert client.post("/broker/run", json=body,
                       headers={"X-Broker-Secret": "wrong"}).status_code == 401
    ok = client.post("/broker/run", json=body, headers={"X-Broker-Secret": "s3cret-value"})
    assert ok.status_code == 200
    assert ok.json()["ok"] is True


def test_broker_run_http_open_in_demo_mode_without_secret(monkeypatch, tmp_path):
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.delenv("LEAF_BROKER_SECRET", raising=False)
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "DATA_FILE", tmp_path / "intake.json")
    (tmp_path / "intake.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(broker, "run_tool_dynamic", _ok_env)
    client = TestClient(broker.app)
    body = {"tenant_id": "auth-test-tenant",
            "tool": {"name": "t", "params_schema": {"type": "object"}},
            "params": {}, "dwg": "rooftop_demo", "aps_live": False}
    # no header, demo mode -> still 200 (unchanged behaviour)
    assert client.post("/broker/run", json=body).status_code == 200


def test_broker_client_sends_secret_header_from_env(monkeypatch):
    monkeypatch.setenv("LEAF_BROKER_SECRET", "shhh")
    assert broker_client.broker_headers() == {"X-Broker-Secret": "shhh"}
    monkeypatch.delenv("LEAF_BROKER_SECRET", raising=False)
    assert broker_client.broker_headers() == {}
    # run_via_broker attaches the header (patch the real requests.post it uses)
    seen = {}

    def fake_post(url, *, json, headers, timeout):
        seen["headers"] = headers
        seen["json"] = json
        return _Response(200, {"ok": True})

    monkeypatch.setenv("LEAF_BROKER_SECRET", "shhh")
    monkeypatch.setattr(broker_client.requests, "post", fake_post)
    broker_client.run_via_broker(
        "t", {"name": "x"}, {}, "rooftop_demo", False,
        ledger_event_key="job-1:attempt:1",
    )
    assert seen["headers"] == {"X-Broker-Secret": "shhh"}
    assert seen["json"]["ledger_event_key"] == "job-1:attempt:1"


def test_internal_secret_headers_strip_secret_store_whitespace(monkeypatch):
    monkeypatch.setenv("LEAF_BROKER_SECRET", "broker-secret\n")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "\r\nharness-secret\n")

    assert broker_client.broker_headers() == {"X-Broker-Secret": "broker-secret"}
    assert broker_client.harness_headers() == {"X-Harness-Secret": "harness-secret"}


def test_internal_secret_headers_omit_whitespace_only_values(monkeypatch):
    monkeypatch.setenv("LEAF_BROKER_SECRET", "\n")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", " \r\n")

    assert broker_client.broker_headers() == {}
    assert broker_client.harness_headers() == {}


# --------------------------------------------------------------------------- #
# F4(b): the arbitrary-.py primitive in resolve_local_file is killed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "ref",
    [
        "C:/tmp/evil.py", "C:\\tmp\\evil.py", "/etc/evil.py", "//host/share/evil.py",
        "../evil.py", "..\\evil.py", "tools/../../evil.py", "a\\..\\..\\evil.py",
        "~/evil.py", "~\\evil.py",
    ],
)
def test_resolve_local_file_rejects_absolute_and_traversal_entry(ref):
    assert tool_loader.resolve_local_file({"name": "x", "entry": ref}) is None
    assert tool_loader.resolve_local_file({"name": "x", "script": ref}) is None


def test_resolve_local_file_rejects_existing_absolute_py_and_never_execs(tmp_path):
    evil = tmp_path / "evil.py"
    evil.write_text("def run(intake, params):\n    return {'pwned': True}, None\n",
                    encoding="utf-8")
    tool = {"name": "evil", "entry": str(evil), "params_schema": {"type": "object"}}
    # not resolved...
    assert tool_loader.resolve_local_file(tool) is None
    # ...and never executed: run_tool_dynamic falls to "no local implementation" (BAD_PARAMS)
    env = tool_loader.run_tool_dynamic(tool, {}, {}, aps_live=False, da=None)
    assert env["ok"] is False
    assert env["error"]["error_code"] == "BAD_PARAMS"


def test_resolve_local_file_rejects_existing_absolute_script(tmp_path):
    evil = tmp_path / "evil.py"
    evil.write_text("x = 1\n", encoding="utf-8")
    assert tool_loader.resolve_local_file({"name": "evil", "script": str(evil)}) is None


def test_resolve_local_file_rejects_traversal_out_of_tenant_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(tool_loader, "_tenant_repo_root", lambda tid=None: repo)
    assert tool_loader.resolve_local_file(
        {"name": "evil", "entry": "tools/../../outside.py"}, tenant_id="t") is None


def test_resolve_local_file_accepts_relative_tenant_entry(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    d = repo / "tools" / "foo"
    d.mkdir(parents=True)
    f = d / "tool.py"
    f.write_text("def run(intake, params):\n    return {}, None\n", encoding="utf-8")
    monkeypatch.setattr(tool_loader, "_tenant_repo_root", lambda tid=None: repo)
    got = tool_loader.resolve_local_file({"name": "foo", "entry": "tools/foo/tool.py"},
                                         tenant_id="t")
    assert got == f.resolve()


def test_resolve_local_file_still_resolves_builtin_op():
    got = tool_loader.resolve_local_file({"name": "count", "engine_op": "count_by_layer"})
    assert got is not None and got.name == "count_by_layer.py"


# --------------------------------------------------------------------------- #
# F10: broker-side tier ENTITLEMENT re-check on /broker/run
# --------------------------------------------------------------------------- #
def test_tenant_tier_defaults_demo_and_reads_provisioned(monkeypatch):
    monkeypatch.delenv("LEAF_BROKER_TENANT_TIERS", raising=False)
    # unknown tenant -> demo (keeps the async spine unbroken)
    assert broker._tenant_tier("nobody") == "demo"
    monkeypatch.setenv("LEAF_BROKER_TENANT_TIERS", json.dumps({"t1": "restricted"}))
    assert broker._tenant_tier("t1") == "restricted"
    assert broker._tenant_tier("t2") == "demo"


def test_broker_run_denies_write_tool_for_restricted_tier(monkeypatch, tmp_path):
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "tenant_disabled", lambda _t: False)
    monkeypatch.setattr(broker, "_cap_preflight", lambda _t, _tool: None)
    monkeypatch.setenv("LEAF_BROKER_TENANT_TIERS", json.dumps({"tenant-r": "restricted"}))
    req = broker.BrokerRunRequest(
        tenant_id="tenant-r",
        tool={"name": "writer", "capabilities": ["drawing.write"],
              "params_schema": {"type": "object"}},
        params={}, dwg="rooftop_demo", aps_live=False)
    resp = broker.broker_run(req)
    assert resp.status_code == 403
    assert b'"error_code":"ENTITLEMENT_REQUIRED"' in resp.body


def test_broker_run_allows_read_tool_for_restricted_tier(monkeypatch, tmp_path):
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "DATA_FILE", tmp_path / "intake.json")
    (tmp_path / "intake.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(broker, "tenant_disabled", lambda _t: False)
    monkeypatch.setattr(broker, "_cap_preflight", lambda _t, _tool: None)
    monkeypatch.setattr(broker, "run_tool_dynamic", _ok_env)
    monkeypatch.setenv("LEAF_BROKER_TENANT_TIERS", json.dumps({"tenant-r": "restricted"}))
    req = broker.BrokerRunRequest(
        tenant_id="tenant-r",
        tool={"name": "reader", "capabilities": [], "params_schema": {"type": "object"}},
        params={}, dwg="rooftop_demo", aps_live=False)
    resp = broker.broker_run(req)
    assert resp.status_code == 200
    assert resp.body is not None
