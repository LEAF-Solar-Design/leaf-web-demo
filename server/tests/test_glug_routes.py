import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import deps
import glug_routes


def _tenant(tenant_id="tenant-glug", subject="auth0|board-admin", resolved=True):
    return deps.TenantContext(
        tenant_id, org_id=tenant_id, subject=subject,
        authority_resolved=resolved, backedge=not resolved,
    )


class FakeExecutor:
    approvals = object()
    def pin_receipt(self):
        return {"contract": "glug.mushy-pin.v1", "workspace": "glug"}


class FakeStore:
    def __init__(self):
        self.jobs = {}
    def get(self, job_id, *, actor_id):
        value = self.jobs.get((job_id, actor_id))
        return value


class FakeService:
    def __init__(self):
        self.store = FakeStore()
        self.pool = None
        self.calls = []
    def create(self, **kwargs):
        self.calls.append(kwargs)
        job = {"contract": "glug.mushy-job.v1", "id": "job-1",
               "job_type": kwargs["requested_power"], "status": "queued"}
        self.store.jobs[("job-1", kwargs["actor_id"])] = job
        return job, True
    def issue_approval(self, **kwargs):
        self.calls.append(kwargs)
        return {"contract": "glug.mushy-publication-approval.v1", "id": "approval-1"}


def _client(service=None, tenant=None):
    app = FastAPI()
    app.include_router(glug_routes.router)
    app.dependency_overrides[deps.require_tenant] = lambda: tenant or _tenant()
    glug_routes.set_executor(FakeExecutor())
    glug_routes.set_job_service(service or FakeService())
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setenv("GLUG_MUSHY_CONTROL_TENANT_ID", "tenant-glug")
    monkeypatch.setenv("GLUG_MUSHY_CONTROL_SUBJECTS", "auth0|board-admin,auth0|backup")
    glug_routes.set_executor(None)
    glug_routes.set_job_service(None)
    yield
    glug_routes.set_executor(None)
    glug_routes.set_job_service(None)


def test_browser_uses_strict_durable_job_and_server_stamps_actor():
    service = FakeService()
    client = _client(service)
    response = client.post("/api/glug/mushy/jobs", json={
        "workspace_id": "glug", "requested_power": "stage_change",
        "instruction": "Change the welcome copy.", "idempotency_key": "request-1",
    })
    assert response.status_code == 202
    assert service.calls[0]["actor_id"] == "auth0|board-admin"
    assert response.json()["job"]["id"] == "job-1"
    assert client.get("/api/glug/mushy/jobs/job-1").status_code == 200


def test_publication_request_has_only_origin_job_and_approval_authority():
    service = FakeService()
    client = _client(service)
    response = client.post("/api/glug/mushy/jobs", json={
        "workspace_id": "glug", "requested_power": "create_pull_request",
        "origin_job_id": "job-stage", "approval_id": "approval-1",
        "idempotency_key": "publish-1",
    })
    assert response.status_code == 202
    for forbidden in ("stage_receipt", "commit", "branch", "base_ref"):
        body = {
            "workspace_id": "glug", "requested_power": "create_pull_request",
            "origin_job_id": "job-stage", "approval_id": "approval-1",
            "idempotency_key": "publish-2", forbidden: "attacker",
        }
        assert client.post("/api/glug/mushy/jobs", json=body).status_code == 422


def test_board_approval_route_is_closed_and_actor_scoped():
    service = FakeService()
    response = _client(service).post("/api/glug/mushy/approvals", json={
        "workspace_id": "glug", "origin_job_id": "job-stage",
        "publication_power": "create_review_branch", "idempotency_key": "approve-1",
    })
    assert response.status_code == 201
    assert service.calls[0]["actor_id"] == "auth0|board-admin"
    assert _client(service).post("/api/glug/mushy/approvals", json={
        "workspace_id": "glug", "origin_job_id": "job-stage",
        "publication_power": "create_review_branch", "idempotency_key": "approve-2",
        "commit": "0" * 40,
    }).status_code == 422


@pytest.mark.parametrize("path", [
    "/api/glug/mushy/claim", "/api/glug/mushy/execute", "/api/glug/mushy/publish",
])
def test_legacy_direct_routes_are_fixed_migration_refusals(path):
    response = _client().post(path, json={"stage_receipt": {"attacker": True}})
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "durable_job_required"


def test_all_routes_reject_wrong_tenant_or_subject():
    service = FakeService()
    request = {"workspace_id": "glug", "requested_power": "code_question",
               "instruction": "What is Glug?", "idempotency_key": "ask-1"}
    wrong_subject = _client(service, _tenant(subject="auth0|attacker")).post(
        "/api/glug/mushy/jobs", json=request)
    wrong_tenant = _client(service, _tenant(tenant_id="other")).post(
        "/api/glug/mushy/jobs", json=request)
    assert wrong_subject.status_code == wrong_tenant.status_code == 403
    assert service.calls == []


def test_mutation_route_fails_closed_when_live_mounts_are_absent(monkeypatch):
    glug_routes.set_executor(None)
    glug_routes.set_job_service(None)
    for key in (
        "GLUG_MUSHY_CANONICAL_GIT_SOURCE", "GLUG_MUSHY_WORKSPACE_ROOT",
        "LEAF_GLUG_MUSHY_ARTIFACT_ROOT", "GLUG_MUSHY_JOB_DATABASE",
    ):
        monkeypatch.delenv(key, raising=False)
    response = _client(FakeService())
    glug_routes.set_executor(None)
    glug_routes.set_job_service(None)
    result = response.post("/api/glug/mushy/jobs", json={
        "workspace_id": "glug", "requested_power": "code_question",
        "instruction": "Where is Home?", "idempotency_key": "missing-mount-1",
    })
    assert result.status_code == 503
    assert result.json()["error"]["code"] == "executor_unavailable"


def test_signed_server_proxy_can_forward_only_an_actor_identity(monkeypatch):
    service = FakeService()
    secret = "proxy-signing-secret-with-at-least-32-bytes"
    timestamp = str(int(time.time()))
    actor = "glug-account-board-1"
    path = "/api/glug/mushy/jobs"
    payload = {"workspace_id": "glug", "requested_power": "code_question",
               "instruction": "Where is Home?", "idempotency_key": "proxy-ask-1"}
    body = json.dumps(payload, separators=(",", ":"))
    def signature(*, signed_path=path, signed_body=body):
        digest = hashlib.sha256(signed_body.encode()).hexdigest()
        canonical = f"v1\n{actor}\n{timestamp}\nPOST\n{signed_path}\n{digest}"
        return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    monkeypatch.setenv(
        "GLUG_MUSHY_CONTROL_SUBJECTS", "auth0|board-admin,auth0|backup,auth0|proxy")
    monkeypatch.setenv("GLUG_MUSHY_PROXY_SUBJECT", "auth0|proxy")
    monkeypatch.setenv("GLUG_MUSHY_PROXY_SIGNING_SECRET", secret)
    client = _client(service, _tenant(subject="auth0|proxy"))
    direct = client.post(path, content=body, headers={"Content-Type": "application/json"})
    assert direct.status_code == 403
    response = client.post(
        path, content=body,
        headers={"X-Glug-Board-Actor": actor, "X-Glug-Board-Timestamp": timestamp,
                 "X-Glug-Board-Signature": signature(), "Content-Type": "application/json"},
    )
    assert response.status_code == 202
    assert service.calls[0]["actor_id"] == actor
    path_tamper = client.post(
        path, content=body,
        headers={"X-Glug-Board-Actor": actor, "X-Glug-Board-Timestamp": timestamp,
                 "X-Glug-Board-Signature": signature(signed_path=path + "/tampered"),
                 "Content-Type": "application/json"},
    )
    changed = body.replace("proxy-ask-1", "proxy-ask-2")
    body_tamper = client.post(
        path, content=changed,
        headers={"X-Glug-Board-Actor": actor, "X-Glug-Board-Timestamp": timestamp,
                 "X-Glug-Board-Signature": signature(), "Content-Type": "application/json"},
    )
    assert path_tamper.status_code == body_tamper.status_code == 403


def test_router_exposes_no_merge_deploy_or_app_store_power():
    paths = {route.path for route in glug_routes.router.routes}
    assert "/api/glug/mushy/jobs" in paths
    assert "/api/glug/mushy/approvals" in paths
    assert all(not any(word in path for word in ("merge", "deploy", "app-store")) for path in paths)
