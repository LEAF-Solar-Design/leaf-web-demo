"""W1 cloud proposal through the existing jobs and broker rails.

Offline responses are synthetic. For live verification set W1_REQUIRE_LIVE=1
and W1_LIVE_CONFIG_FILE to a private JSON file with environment="staging",
server_url (HTTPS origin), tenant_id, grant_ref, catalog_digest, and request
(a sanitized W0 request matching the provisional model). The same named grant
must be provisioned privately on the broker. LEAF_CLOUD_GRANTS_FILE supplies
the test client's staging Auth0 token; no credential belongs in this config.
Missing live input is a failure when requested, never a skip.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))

import leaf_cloud_client as cloud
import leaf_cloud_grants as grants


@pytest.fixture
def recorded():
    return json.loads((SERVER / "tests/fixtures/w1_stringer_recorded_response.json").read_text())


@pytest.fixture
def params(recorded):
    return {"grant_ref": "w1-staging", "request": copy.deepcopy(recorded["request"])}


@pytest.fixture
def rails(monkeypatch, tmp_path):
    monkeypatch.setenv("BROKER_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("BROKER_TENANTS", str(tmp_path / "tenants.json"))
    monkeypatch.setenv("LEAF_BROKER_STORE", "legacy")
    monkeypatch.setenv("LEAF_JOB_STORE", "sqlite")
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "staging")
    import broker
    import jobs

    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "_tenants", {"w1-tenant": {"tier": "demo", "disabled": False}})
    monkeypatch.setattr(broker, "_cap_preflight", lambda *a: None)
    monkeypatch.setattr(broker, "_emit_aps_metric", lambda *a: None)
    monkeypatch.setattr(broker, "_get_da", lambda: pytest.fail("APS must not execute"))
    monkeypatch.setattr(broker, "run_tool_dynamic", lambda *a, **k: pytest.fail("no local solver"))
    monkeypatch.setattr(jobs, "job_store_mode", lambda: "sqlite")
    monkeypatch.setattr(cloud.requests.sessions.Session, "request",
                        lambda *a, **k: pytest.fail("offline network forbidden"))
    tool = next(t for t in json.loads((SERVER / "catalog_tools.json").read_text())["tools"]
                if t["name"] == cloud.TOOL_NAME)
    return broker, jobs, tool


def broker_call(rails, params):
    broker, _, tool = rails
    response = broker._broker_run(broker.BrokerRunRequest(
        tenant_id="w1-tenant", tool=tool, params=params, job_id="w1-job",
        ledger_event_key="w1-job:broker-run", aps_live=False))
    return response.status_code, json.loads(response.body)


def test_recorded_proposal_and_ledger(rails, params, recorded, monkeypatch):
    raw = cloud.canonical_bytes(recorded["response"])
    monkeypatch.setattr(cloud, "resolve_grant", lambda *a: grants.CloudGrant("w1-tenant", ""))
    monkeypatch.setattr(cloud, "post_stringer", lambda request, grant: raw)
    status, env = broker_call(rails, params)
    assert status == 200 and env["ok"] is True
    result = env["result"]
    assert result["drawing_changed"] is False
    assert result["job_id"] == "w1-job"
    assert result["proposal"] == recorded["response"]
    assert result["request_sha256"] == hashlib.sha256(cloud.canonical_bytes(params["request"])).hexdigest()
    assert result["response_sha256"] == hashlib.sha256(raw).hexdigest()
    entries = rails[0].LEDGER_PATH.read_text().splitlines()
    assert len(entries) == 1
    entry = json.loads(entries[0])
    assert entry["status"] == "ok" and entry["aps_live"] is False
    assert entry["aps_endpoint"] is None
    assert "grant_ref" not in entry and "params" not in entry


@pytest.mark.parametrize("classification,status", [
    ("cloud_auth_missing", 401), ("cloud_tenant_unauthorized", 403),
    ("cloud_upstream_failure", 502), ("cloud_response_invalid", 502),
])
def test_classified_broker_errors(rails, params, monkeypatch, classification, status):
    def fail(*args):
        raise grants.CloudError(classification, status)
    monkeypatch.setattr(cloud, "resolve_grant", fail)
    actual, env = broker_call(rails, params)
    assert actual == status
    assert env["ok"] is False and env["degraded_mode"] is False
    assert env["error"]["classification"] == classification
    assert not rails[1]._allows_local_fallback(dict(rails[2], allow_local_fallback=True))


@pytest.mark.parametrize("mutation", ["secret", "unknown", "duplicate", "nonfinite", "empty"])
def test_invalid_inputs_fail_closed(params, mutation):
    if mutation == "secret":
        params["access_token"] = "not-a-credential"
    elif mutation == "unknown":
        params["request"]["electrical"]["unexpected"] = 1
    elif mutation == "duplicate":
        params["request"]["matrix_cells"][1]["panel_id"] = 1
    elif mutation == "nonfinite":
        params["request"]["electrical"]["module_voc"] = float("nan")
    else:
        params["request"]["panel_groups"] = []
    with pytest.raises(grants.CloudError, match="cloud_request_invalid"):
        cloud.validate_params(params)


def test_grant_missing_expired_and_wrong_tenant(monkeypatch, tmp_path):
    monkeypatch.delenv("LEAF_CLOUD_GRANTS_FILE", raising=False)
    with pytest.raises(grants.CloudError, match="cloud_auth_missing"):
        grants.resolve_grant("w1-staging", "w1-tenant")
    path = tmp_path / "private-grants.json"
    monkeypatch.setenv("LEAF_CLOUD_GRANTS_FILE", str(path))
    # This marker cannot authenticate; no real token is stored in the test.
    grant = {"tenant_id": "other", "access_token": "synthetic-invalid",
             "audience": "https://api.leafdesign.ai", "expires_at": time.time() - 1}
    path.write_text(json.dumps({"w1-staging": grant}))
    with pytest.raises(grants.CloudError, match="cloud_auth_missing"):
        grants.resolve_grant("w1-staging", "w1-tenant")
    grant["expires_at"] = time.time() + 300
    path.write_text(json.dumps({"w1-staging": grant}))
    with pytest.raises(grants.CloudError, match="cloud_tenant_unauthorized"):
        grants.resolve_grant("w1-staging", "w1-tenant")


@pytest.mark.parametrize("raw", [b"{}", b"null", b'{"strings":[]}',
    b'{"strings":[{"panel_ids":[99]}]}', b'{"strings":[{"panel_ids":[1,1,2]}]}'])
def test_invalid_response_rejected(params, monkeypatch, raw):
    monkeypatch.setattr(cloud, "resolve_grant", lambda *a: grants.CloudGrant("w1-tenant", ""))
    monkeypatch.setattr(cloud, "post_stringer", lambda *a: raw)
    with pytest.raises(grants.CloudError, match="cloud_response_invalid"):
        cloud.proposal(params, "w1-tenant", "job")


@pytest.mark.parametrize("status,classification", [
    (401, "cloud_auth_missing"), (403, "cloud_tenant_unauthorized"),
    (500, "cloud_upstream_failure"), (302, "cloud_upstream_failure"),
])
def test_transport_statuses_and_timeout(params, monkeypatch, status, classification):
    class Reply:
        status_code = status
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    def post(url, **kwargs):
        assert url == cloud.SOLVER_URL
        assert kwargs["timeout"] == (5, 45)
        assert kwargs["allow_redirects"] is False
        assert json.loads(kwargs["data"]) == params["request"]
        assert "Authorization" in kwargs["headers"]
        return Reply()

    monkeypatch.setattr(cloud.requests, "post", post)
    with pytest.raises(grants.CloudError, match=classification):
        cloud.post_stringer(cloud.validate_params(params).request, grants.CloudGrant("w1-tenant", ""))


def test_transport_exception_is_sanitized(params, monkeypatch):
    def fail(*args, **kwargs):
        raise cloud.requests.Timeout("private transport detail")
    monkeypatch.setattr(cloud.requests, "post", fail)
    with pytest.raises(grants.CloudError, match="^cloud_upstream_failure$"):
        cloud.post_stringer(cloud.validate_params(params).request, grants.CloudGrant("w1-tenant", ""))


def test_broker_rechecks_entitlement_and_catalog(rails, params, monkeypatch):
    broker, _, tool = rails
    monkeypatch.setattr(broker, "_tenant_tier", lambda *a: "guest")
    status, env = broker_call(rails, params)
    assert status == 403 and env["error"]["error_code"] == "ENTITLEMENT_REQUIRED"
    tool["capabilities"] = []
    status, env = broker_call(rails, params)
    assert status == 400


def test_api_run_durable_job_to_broker(rails, params, recorded, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import jobs as route
    import broker_client

    broker, jobs, tool = rails
    class InlineExecutor:
        def submit(self, fn, *args, **kwargs):
            fn(*args, **kwargs)

    monkeypatch.setattr(jobs, "ensure_started", lambda: jobs._db())
    monkeypatch.setattr(jobs, "_write_terminal_receipt", lambda *a: None)
    monkeypatch.setattr(jobs, "_executors", {jobs.lane_for(tool, False): InlineExecutor()})
    monkeypatch.setattr(cloud, "resolve_grant", lambda *a: grants.CloudGrant("w1-tenant", ""))
    monkeypatch.setattr(cloud, "post_stringer", lambda *a: cloud.canonical_bytes(recorded["response"]))
    monkeypatch.setattr(route.deps, "find_tool", lambda *a: tool)
    monkeypatch.setattr(route.deps, "effective_tools_with_provenance", lambda *a: [])
    monkeypatch.setattr(route.deps, "backedge_run_identity", lambda tenant, *a: tenant)
    monkeypatch.setattr(route.deps, "auth_live", lambda: True)
    monkeypatch.setattr(route, "_checkout_identity", lambda *a: (None, None))
    monkeypatch.setattr(jobs.platform_link, "resolve_submission_context", lambda *a: None)
    monkeypatch.setattr(jobs.platform_link, "on_submit", lambda *a, **k: None)
    monkeypatch.setattr(jobs.platform_link, "on_running", lambda *a, **k: None)

    def transport(url, *, json, headers, timeout):
        assert url.endswith("/broker/run") and timeout > 0
        response = broker._broker_run(broker.BrokerRunRequest(**json))
        class Reply:
            def json(self):
                return __import__("json").loads(response.body)
        return Reply()

    monkeypatch.setattr(broker_client.requests, "post", transport)
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[route.deps.require_tenant] = lambda: route.deps.TenantContext(
        "w1-tenant", tier="demo", subject="w1-synthetic-subject", authority_resolved=True)
    response = TestClient(app).post("/api/run?wait=1", json={
        "tool": tool["name"], "params": params,
        "catalog_digest": route.deps.catalog_tool_digest(tool)})
    assert response.status_code == 200, response.text
    env = response.json()
    rec = jobs.get_job(env["result"]["job_id"])
    assert rec["status"] == "complete"
    assert rec["params"] == params
    assert env["execution_provenance"]["execution_path"] == "cloud"
    assert env["execution_provenance"]["execution_mode"] == "leaf_cloud_service"
    assert env["execution_provenance"]["solver"] == env["result"]["solver"]
    assert env["execution_provenance"]["response_sha256"] == env["result"]["response_sha256"]
    assert rec["provenance"] == env["execution_provenance"]
    assert env["result"]["drawing_changed"] is False
    app.dependency_overrides[route.deps.require_tenant] = lambda: "w1-tenant"
    denied = TestClient(app).post("/api/run", json={
        "tool": tool["name"], "params": params,
        "catalog_digest": route.deps.catalog_tool_digest(tool)})
    assert denied.status_code == 401
    assert denied.json()["error"]["message"] == "cloud_auth_missing"


@pytest.mark.parametrize("mutation", [
    "local", "aps", "fallback", "missing_context", "tenant", "job", "request",
    "solver", "response_hash", "empty_proposal", "provenance", "other_tool",
])
def test_terminal_cloud_proof_fails_closed(rails, params, recorded, monkeypatch, mutation):
    _, jobs, tool = rails
    monkeypatch.setattr(cloud, "resolve_grant", lambda *a: grants.CloudGrant("w1-tenant", ""))
    monkeypatch.setattr(cloud, "post_stringer", lambda *a: cloud.canonical_bytes(recorded["response"]))
    result = cloud.proposal(params, "w1-tenant", "w1-job")
    provenance = {"attempt": 1, "execution_path": "cloud"}
    provenance.update(cloud.proposal_provenance(result, params, "w1-tenant", "w1-job"))
    execution = {"tool": tool, "aps_live": False, "cloud_service": {"tenant_id": "w1-tenant"}}
    env = {"ok": True, "result": result, "execution_provenance": provenance}
    jobs._validate_terminal_context("complete", env, provenance, 1, execution,
                                    job_id="w1-job", durable_params=params)
    if mutation == "local":
        provenance["execution_path"] = "local"
    elif mutation == "aps":
        execution["aps_live"] = True
    elif mutation == "fallback":
        provenance["fallback"] = True
    elif mutation == "missing_context":
        execution.pop("cloud_service")
    elif mutation == "tenant":
        result["tenant_id"] = "other"
    elif mutation == "job":
        result["job_id"] = "other"
    elif mutation == "request":
        result["request_sha256"] = "0" * 64
    elif mutation == "solver":
        result["solver"] = {"endpoint": "untrusted", "adapter_version": "1.0.0"}
    elif mutation == "response_hash":
        result["response_sha256"] = "missing"
    elif mutation == "empty_proposal":
        result["proposal"] = {"strings": []}
    elif mutation == "provenance":
        provenance["response_sha256"] = "0" * 64
    else:
        execution["tool"] = {"name": "count-by-layer"}
    with pytest.raises(ValueError):
        jobs._validate_terminal_context("complete", env, provenance, 1, execution,
                                        job_id="w1-job", durable_params=params)


def test_staging_live():
    if os.environ.get("W1_REQUIRE_LIVE") != "1":
        pytest.skip("live staging probe is opt-in")
    config_path = os.environ.get("W1_LIVE_CONFIG_FILE")
    if not config_path:
        pytest.fail("required staging evidence unavailable: W1_LIVE_CONFIG_FILE missing", pytrace=False)
    # Keep exceptions, HTTP bodies and private credentials out of pytest output.
    failure = True
    try:
        config = json.loads(Path(config_path).read_bytes())
        origin = urlsplit(config["server_url"])
        if (config.get("environment") != "staging" or origin.scheme != "https"
                or not origin.hostname or origin.username or origin.password
                or origin.query or origin.fragment or origin.path not in ("", "/")):
            raise ValueError()
        import requests
        import broker
        broker.ALLOWED_HOSTS.add(origin.hostname)
        try:
            grant = grants.resolve_grant(config["grant_ref"], config["tenant_id"])
            params = {"grant_ref": config["grant_ref"], "request": config["request"]}
            parsed = cloud.validate_params(params)
            base = config["server_url"].rstrip("/")
            headers = {"Authorization": "Bearer " + grant.access_token}
            response = requests.post(base + "/api/run", json={
                "tool": cloud.TOOL_NAME, "params": params,
                "catalog_digest": config["catalog_digest"]}, headers=headers,
                timeout=(5, 30), allow_redirects=False)
            if response.status_code != 202:
                raise ValueError()
            job_id = response.json()["job_id"]
            if not re.fullmatch(r"[a-zA-Z0-9-]{1,64}", job_id):
                raise ValueError()
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                response = requests.get(base + "/api/jobs/" + job_id, headers=headers,
                                        timeout=(5, 10), allow_redirects=False)
                if response.status_code != 200:
                    raise ValueError()
                rec = response.json()
                if rec.get("status") == "complete":
                    result = rec["result"]["result"]
                    proposal = cloud.StringerResponse.model_validate(result["proposal"])
                    panels = [p for s in proposal.strings for p in s.panel_ids]
                    failure = not (
                        result["job_id"] == job_id and result["tenant_id"] == config["tenant_id"]
                        and result["drawing_changed"] is False
                        and result["solver"]["endpoint"] == cloud.SOLVER_URL
                        and result["request_sha256"] == hashlib.sha256(
                            cloud.canonical_bytes(parsed.request.model_dump())).hexdigest()
                        and re.fullmatch(r"[0-9a-f]{64}", result["response_sha256"])
                        and len(panels) == len(set(panels))
                        and set(panels) == {c.panel_id for c in parsed.request.matrix_cells})
                    break
                if rec.get("status") not in ("submitted", "running"):
                    raise ValueError()
                time.sleep(0.5)
        finally:
            if origin.hostname not in {"api.leafdesign.ai", "developer.api.autodesk.com", "localhost", "127.0.0.1"}:
                broker.ALLOWED_HOSTS.discard(origin.hostname)
    except Exception:
        failure = True
    if failure:
        pytest.fail("required staging job evidence unavailable or invalid", pytrace=False)
