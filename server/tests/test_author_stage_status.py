"""Exact authenticated status backedge, real producer/store, no worker or provider."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

import auth
import customization_service
import deps
import tenancy
from customization_models import ChangeState
from customization_service import CustomizationService
from customization_store import SQLiteCustomizationStore
from routers import author as author_router


TENANT = "11111111-1111-4111-8111-111111111111"
FOREIGN = "22222222-2222-4222-8222-222222222222"
ID = "19097a36-1234-4234-8234-123456789abc"
PATH = f"/api/author/stages/{ID}"
SECRET = "test-dispatch-secret"
BASE = "a" * 40
STAGED = "b" * 40
DIGEST = "c" * 64
WORKSPACE = "d" * 64


def _headers(tenant=TENANT):
    return {"X-Dispatch-Secret": SECRET, "X-Tenant-Id": tenant}


def _snapshot(store):
    # Includes exact attempts, leases, publication rows, confirmations, effective
    # catalog and audit rows, not just the JSON returned by the read.
    with store._connection() as conn:
        return tuple(conn.iterdump())


@pytest.fixture
def status_api(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", SECRET)
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    monkeypatch.setattr(deps, "backedge_tier", lambda tenant: "hosted_pro")
    monkeypatch.setattr(author_router, "customization_enabled", lambda wave, tenant: True)
    store = SQLiteCustomizationStore(tmp_path / "customization.db")
    store.initialize()
    service = CustomizationService(store)
    monkeypatch.setattr(CustomizationService, "configured", classmethod(lambda cls: service))

    # The real require_tenant runs. Only token verification and its external
    # authority repositories are hermetic seams; X-Tenant-Id is not JWT authority.
    def verify(authorization):
        if authorization != "Bearer REDACTED-author-stage-fixture":
            raise HTTPException(status_code=401, detail="Authentication required")
        return {"sub": "auth0|status-reader"}

    monkeypatch.setattr(auth, "verify_platform_token", verify)
    monkeypatch.setattr(auth, "extract_tenant_claims", lambda payload: {"tenant_id": TENANT, "tier": "hosted_pro"})
    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority", lambda subject: (TENANT, "hosted_pro"))
    monkeypatch.setattr(tenancy, "get_store", lambda: SimpleNamespace(resolve_workspace=lambda tenant: None))

    # Existing stage projection reads a receipt-bound Git catalog when staged.
    # Supply bytes at that storage boundary, preserving the actual producer.
    tool = {"name": "panel-gap-checker", "version": "1.0.0", "entry": "tools/panel-gap-checker.py"}
    monkeypatch.setattr(customization_service, "_bare_repo", lambda tenant: tenant)

    def blob(bare, object_name):
        assert bare == TENANT
        assert object_name in (f"{BASE}:registry.json", f"{STAGED}:registry.json")
        return json.dumps({"tools": [tool] if object_name.startswith(STAGED) else []}).encode()

    monkeypatch.setattr(customization_service, "_git_blob", blob)
    app = FastAPI()
    app.include_router(author_router.router)
    # No dependency override: exercising require_tenant is the purpose here.
    with TestClient(app) as client:
        yield SimpleNamespace(store=store, service=service, client=client)


def _seed(api, status):
    change = api.store.create_change_set(
        tenant_id=TENANT, change_set_id=ID, idempotency_key=str(uuid4()),
        base_commit=BASE, desired_platform_release="release-a",
        workspace_contract_digest=WORKSPACE, author_subject="auth0|status-author",
    )
    change = api.store.transition(
        tenant_id=TENANT, change_set_id=ID, next_state=ChangeState.STAGING,
        expected_version=change.version, expected_state=ChangeState.CREATED,
        idempotency_key=str(uuid4()),
    )
    if status in ("running", "failed"):
        change = api.store.claim_stage(owner="test-worker", lease_seconds=300)
        assert change is not None
    if status == "failed":
        change = api.store.fail_stage_claim(
            tenant_id=TENANT, change_set_id=ID, owner="test-worker",
            reason_code="author_job_failed", retryable=False,
            reason_message="Author worker unavailable.",
        )
    elif status == "staged":
        change = api.store.record_staged(
            tenant_id=TENANT, change_set_id=ID, expected_version=change.version,
            idempotency_key=str(uuid4()), staged_commit=STAGED, catalog_digest=DIGEST,
            platform_release="release-a", workspace_contract_digest=WORKSPACE,
        )
    assert change is not None
    return change


@pytest.mark.parametrize("status", ["queued", "running", "staged", "failed"])
def test_exact_backedge_reads_actual_producer_without_mutating(status_api, monkeypatch, status):
    api = status_api
    change = _seed(api, status)
    mutators = []
    for name in ("stage", "enqueue_stage", "request_publication", "_publish"):
        spy = Mock(side_effect=AssertionError(f"status read called {name}"))
        monkeypatch.setattr(api.service, name, spy)
        mutators.append(spy)
    before = _snapshot(api.store)
    response = api.client.get(PATH, headers=_headers())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == api.service.stage_status_change(change)
    assert body["contract"] == "leaf.customization-stage-job.v1"
    assert body["change_set_id"] == ID
    assert body["status"] == status
    assert body["phase"] == ("authoring" if status == "running" else status)
    assert body["attempt"] == change.stage_attempt
    assert type(body["attempt"]) is int
    assert isinstance(body["updated_at"], str)
    assert body["updated_at"] == change.updated_at
    assert body["updated_at"].endswith("Z")
    if status == "failed":
        assert body["error"] == {
            "reason_code": "author_job_failed", "retryable": False,
            "message": "Author worker unavailable.",
        }
    if status == "staged":
        assert body["receipt"]["staged_commit"] == STAGED
        assert body["result"]["tool"]["name"] == "panel-gap-checker"
    # Repeated explicit reads do not enqueue work or bump an attempt.
    assert api.client.get(PATH, headers=_headers()).json() == body
    assert _snapshot(api.store) == before
    for spy in mutators:
        spy.assert_not_called()


def test_foreign_tenant_is_existing_404_and_read_does_not_mutate(status_api):
    _seed(status_api, "failed")
    before = _snapshot(status_api.store)
    response = status_api.client.get(PATH, headers=_headers(FOREIGN))
    assert response.status_code == 404
    assert response.json()["reason_code"] == "stage_not_available"
    assert ID not in response.text
    assert _snapshot(status_api.store) == before


def test_missing_owned_stage_is_not_fabricated(status_api):
    before = _snapshot(status_api.store)
    response = status_api.client.get(PATH, headers=_headers())
    assert response.status_code == 404
    assert response.json()["reason_code"] == "stage_not_available"
    assert _snapshot(status_api.store) == before


def test_jwt_uses_server_authority_instead_of_caller_tenant(status_api):
    _seed(status_api, "queued")
    before = _snapshot(status_api.store)
    response = status_api.client.get(PATH, headers={
        "Authorization": "Bearer REDACTED-author-stage-fixture", "X-Tenant-Id": FOREIGN,
    })
    assert response.status_code == 200, response.text
    assert response.json()["change_set_id"] == ID
    assert _snapshot(status_api.store) == before


@pytest.mark.parametrize("headers", [
    {}, {"X-Tenant-Id": TENANT}, {"X-Dispatch-Secret": SECRET},
    {"X-Tenant-Id": TENANT, "X-Dispatch-Secret": "wrong"},
])
def test_missing_authentication_is_refused(status_api, headers):
    _seed(status_api, "queued")
    before = _snapshot(status_api.store)
    assert status_api.client.get(PATH, headers=headers).status_code == 401
    assert _snapshot(status_api.store) == before


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
def test_other_methods_are_not_backedges_or_stage_reads(status_api, method):
    _seed(status_api, "queued")
    before = _snapshot(status_api.store)
    assert not deps._dispatch_backedge_route(method, PATH)
    response = status_api.client.request(method, PATH, headers=_headers())
    assert response.status_code == 405
    assert _snapshot(status_api.store) == before


@pytest.mark.parametrize("path", [
    PATH + "/", PATH + "/publish", PATH + "/receipt", PATH + "%2fextra",
    PATH + "%252fextra", PATH + "%5cextra", PATH + "suffix",
    "/api/author/stages/not-a-uuid", "/api/author/stages/" + ID.upper(),
    "/api/author/stages/" + ID.replace("-", ""),
    "/api/author/stages", "/api/author/stage", "/api/author", "/api/author/stages/" + ID + "\n",
])
def test_backedge_allowlist_is_one_canonical_uuid_only(path):
    assert not deps._dispatch_backedge_route("GET", path)


@pytest.mark.parametrize("path", [
    PATH + "/publish", PATH + "%2fextra", PATH + "%252fextra", PATH + "%5cextra",
    "/api/author/stages/not-a-uuid", "/api/author/stages/" + ID.upper(),
    "/api/author/stages/" + ID.replace("-", ""),
    "/api/author/stages/%31" + ID[1:],
])
def test_invalid_wire_path_is_never_admitted_by_real_dependency(status_api, path):
    _seed(status_api, "queued")
    before = _snapshot(status_api.store)
    # Routing may reject first; if it reaches the route dependency, dispatch
    # credentials cannot authenticate it, including encoded canonical digits.
    response = status_api.client.get(path, headers=_headers(), follow_redirects=False)
    assert response.status_code in (401, 404)
    assert _snapshot(status_api.store) == before


def test_raw_encoded_segment_cannot_hide_behind_decoded_asgi_path(status_api):
    request = Request({
        "type": "http", "method": "GET", "path": PATH,
        "raw_path": ("/api/author/stages/%31" + ID[1:]).encode(),
        "query_string": b"", "headers": [], "scheme": "http", "server": ("test", 80),
    })
    with pytest.raises(HTTPException) as failure:
        deps.require_tenant(request, x_tenant_id=TENANT, x_dispatch_secret=SECRET,
                            authorization=None, x_guest_session=None, x_demo_session=None)
    assert failure.value.status_code == 401


def test_existing_mutation_allowlist_is_preserved():
    assert deps._dispatch_backedge_route("GET", PATH)
    assert deps._dispatch_backedge_route("POST", "/api/author")
    assert deps._dispatch_backedge_route("POST", "/api/author/publication-requests")
    assert not deps._dispatch_backedge_route("POST", "/api/author/stage")
    assert not deps._dispatch_backedge_route("POST", "/api/author/stage/recover")
    assert not deps._dispatch_backedge_route("GET", "/api/author/publication-requests")
