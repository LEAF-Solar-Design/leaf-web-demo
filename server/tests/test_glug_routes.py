import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import deps
import glug_routes
from glug_executor import GlugExecutorError


BASE = "205317570ea1a0299a93c694af2480ed3ed4c5b3"


def _tenant(
    tenant_id="tenant-glug", subject="auth0|board-admin", *, resolved=True,
    backedge=False,
):
    return deps.TenantContext(
        tenant_id,
        org_id=tenant_id,
        subject=subject,
        authority_resolved=resolved,
        backedge=backedge,
    )


class FakeExecutor:
    def __init__(self):
        self.pin_calls = 0
        self.claims = []
        self.executions = []
        self.publications = []
        self.failure = None

    def pin_receipt(self):
        self.pin_calls += 1
        return {"contract": "glug.mushy-pin.v1", "workspace": "glug"}

    def issue_claim(self, request, *, actor_id):
        if self.failure:
            raise self.failure
        self.claims.append((request, actor_id))
        return {
            "contract": "glug.mushy-claim.v1", "id": "claim-1",
            "workspace": "glug", "actor_digest": "8" * 64,
            "power": request["requested_power"], "base_commit": BASE,
            "issued_at": "2026-09-01T11:58:00Z",
            "expires_at": "2026-09-01T12:03:00Z", "signature": "9" * 64,
        }

    def execute(self, request, *, actor_id):
        if self.failure:
            raise self.failure
        self.executions.append((request, actor_id))
        return {"receipt": {"contract": "glug.mushy-stage-receipt.v1"}}

    def publish(self, request, *, actor_id):
        if self.failure:
            raise self.failure
        self.publications.append((request, actor_id))
        return {"contract": "glug.mushy-review-publication.v1"}


def _client(executor, tenant=None):
    app = FastAPI()
    app.include_router(glug_routes.router)
    resolved_tenant = _tenant() if tenant is None else tenant
    app.dependency_overrides[deps.require_tenant] = lambda: resolved_tenant
    glug_routes.set_executor(executor)
    return TestClient(app, raise_server_exceptions=False)


def _execution(**overrides):
    value = {
        "workspace_id": "glug", "requested_power": "stage_change",
        "instruction": "Change the Glug welcome copy.",
        "claim": {
            "contract": "glug.mushy-claim.v1", "id": "claim-1",
            "workspace": "glug", "actor_digest": "8" * 64,
            "power": "stage_change", "base_commit": BASE,
            "issued_at": "2026-09-01T11:58:00Z",
            "expires_at": "2026-09-01T12:03:00Z", "signature": "9" * 64,
        },
    }
    value.update(overrides)
    return value


@pytest.fixture(autouse=True)
def _reset_executor(monkeypatch):
    monkeypatch.setenv("GLUG_MUSHY_CONTROL_TENANT_ID", "tenant-glug")
    monkeypatch.setenv("GLUG_MUSHY_CONTROL_SUBJECTS", "auth0|board-admin, auth0|backup")
    glug_routes.set_executor(None)
    yield
    glug_routes.set_executor(None)


def test_routes_are_exact_and_server_stamps_actor():
    executor = FakeExecutor()
    client = _client(executor)
    pin = client.get("/api/glug/mushy/pin")
    claim = client.post("/api/glug/mushy/claim", json={
        "workspace_id": "glug", "requested_power": "stage_change",
    })
    execute = client.post("/api/glug/mushy/execute", json=_execution())
    publish = client.post("/api/glug/mushy/publish", json={
        "workspace_id": "glug", "requested_power": "create_review_branch",
        "approval_id": "approval-1", "stage_receipt": {"safe": True},
    })
    assert pin.status_code == 200
    assert claim.status_code == 201
    assert execute.status_code == 200
    assert publish.status_code == 201
    assert executor.claims[0][1] == "auth0|board-admin"
    assert executor.executions[0][1] == "auth0|board-admin"
    assert executor.publications[0][1] == "auth0|board-admin"


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("GET", "/api/glug/mushy/pin", None),
        ("POST", "/api/glug/mushy/claim", {
            "workspace_id": "glug", "requested_power": "stage_change",
        }),
        ("POST", "/api/glug/mushy/execute", _execution()),
        ("POST", "/api/glug/mushy/publish", {
            "workspace_id": "glug", "requested_power": "create_review_branch",
            "approval_id": "approval-1", "stage_receipt": {"safe": True},
        }),
    ],
)
def test_every_route_rejects_subject_outside_server_allowlist(method, path, payload):
    executor = FakeExecutor()
    response = _client(
        executor, _tenant(subject="auth0|not-allowed"),
    ).request(method, path, json=payload)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "control_authority_denied"
    assert executor.pin_calls == 0
    assert executor.claims == []
    assert executor.executions == []
    assert executor.publications == []


def test_control_gate_rejects_wrong_tenant_plain_and_subjectless_identities():
    executor = FakeExecutor()
    wrong_tenant = _client(
        executor, _tenant(tenant_id="another-tenant"),
    ).post("/api/glug/mushy/claim", json={
        "workspace_id": "glug", "requested_power": "stage_change",
    })
    plain = _client(executor, "tenant-glug").get("/api/glug/mushy/pin")
    subjectless = _client(
        executor, _tenant(subject=None, resolved=False, backedge=True),
    ).post("/api/glug/mushy/publish", json={
        "workspace_id": "glug", "requested_power": "create_review_branch",
        "approval_id": "approval-1", "stage_receipt": {"safe": True},
    })
    assert wrong_tenant.status_code == 403
    assert plain.status_code == 403
    assert subjectless.status_code == 403
    assert executor.pin_calls == 0
    assert executor.claims == []
    assert executor.publications == []


def test_control_gate_fails_closed_when_server_authority_is_unconfigured(monkeypatch):
    monkeypatch.delenv("GLUG_MUSHY_CONTROL_TENANT_ID")
    monkeypatch.delenv("GLUG_MUSHY_CONTROL_SUBJECTS")
    executor = FakeExecutor()
    response = _client(executor).get("/api/glug/mushy/pin")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "control_authority_unavailable"
    assert executor.pin_calls == 0


def test_route_models_reject_extra_top_level_and_claim_fields():
    executor = FakeExecutor()
    client = _client(executor)
    claim_base = client.post("/api/glug/mushy/claim", json={
        "workspace_id": "glug", "requested_power": "stage_change",
        "base_commit": BASE,
    })
    claim_role = client.post("/api/glug/mushy/claim", json={
        "workspace_id": "glug", "requested_power": "stage_change",
        "tenant_id": "attacker", "role": "board_admin",
    })
    extra = client.post("/api/glug/mushy/execute", json=_execution(extra=True))
    base = client.post(
        "/api/glug/mushy/execute", json=_execution(base_commit=BASE))
    nested = _execution()
    nested["claim"]["repository"] = "attacker/repo"
    nested_response = client.post("/api/glug/mushy/execute", json=nested)
    publish_extra = client.post("/api/glug/mushy/publish", json={
        "workspace_id": "glug", "requested_power": "create_pull_request",
        "approval_id": "approval-1", "stage_receipt": {}, "merge": True,
    })
    assert claim_base.status_code == 422
    assert claim_role.status_code == 422
    assert extra.status_code == 422
    assert base.status_code == 422
    assert nested_response.status_code == 422
    assert publish_extra.status_code == 422
    assert executor.executions == []
    assert executor.claims == []
    assert executor.publications == []


def test_executor_refusal_keeps_denied_power_unavailable():
    executor = FakeExecutor()
    executor.failure = GlugExecutorError(
        "power_unavailable", "Requested power is unavailable", 403)
    response = _client(executor).post(
        "/api/glug/mushy/execute", json=_execution(requested_power="treasury_action"))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "power_unavailable"


def test_router_has_no_merge_deploy_app_store_finance_or_member_mutation_path():
    paths = {route.path for route in glug_routes.router.routes}
    assert paths == {
        "/api/glug/mushy/pin",
        "/api/glug/mushy/claim",
        "/api/glug/mushy/execute",
        "/api/glug/mushy/publish",
    }
    forbidden = ("merge", "deploy", "app-store", "finance", "member")
    assert all(not any(word in path for word in forbidden) for path in paths)
