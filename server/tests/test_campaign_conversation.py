"""Conversation-authenticated finish/status admission (server/routers/campaign_conversation.py).

Binary acceptance:
  * a live authority tuple (X-Authority-Session-Id + X-Authority-Turn-Id)
    resolving to the app-owned session's own org/project is the ONLY source
    of project identity -- never a request body field;
  * a missing/half-present tuple, a stale/foreign turn, a session with no
    project link, and a revoked project role each fail BEFORE the campaign
    engine is ever called;
  * unknown/privileged fields (org_id, commands, evidence, status flags) are
    rejected before admission;
  * the same authority session + the same canonical finish intent replays
    the identical idempotency key across retried turns;
  * status reuses the existing release snapshot projection unchanged.
"""
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import deps
import platform_link
import session_store
import campaign_release_service as service
from routers import campaigns, campaign_conversation

ORG_ID = "org-conversation-tenant"
PROJECT_ID = str(uuid.uuid4())
SESSION_ID = "app-session-1"
TURN_ID = "app-turn-1"
HEADERS = {"X-Authority-Session-Id": SESSION_ID, "X-Authority-Turn-Id": TURN_ID}

FINISH_BODY = {
    "title": "Finish", "prompt": "Deliver the file",
    "delivery_profile": "cad_file", "intended_user": "owner",
    "workflow": "download", "artifact_refs": [],
}


@pytest.fixture
def tenant():
    return deps.TenantContext(ORG_ID, subject="mallory")


@pytest.fixture
def client(monkeypatch, tenant):
    app = FastAPI()
    app.include_router(campaign_conversation.router)
    app.dependency_overrides[deps.require_tenant] = lambda: tenant

    def fake_stage_author_identity(t, session_id, turn_id):
        if session_id == SESSION_ID and turn_id == TURN_ID:
            return tenant
        return None

    def fake_get_session(session_id):
        if session_id == SESSION_ID:
            return {"tenant_id": ORG_ID, "org_id": ORG_ID, "project_id": PROJECT_ID}
        return None

    monkeypatch.setattr(deps, "stage_author_identity", fake_stage_author_identity)
    monkeypatch.setattr(session_store, "get_session", fake_get_session)
    monkeypatch.setattr(
        platform_link, "require_project_session_access",
        lambda session, tenant, write=False, binding=None: session)
    return TestClient(app)


def _never_called(*_args, **_kwargs):
    raise AssertionError("engine call must not run before authority admits it")


# --------------------------------------------------------------------------- #
# authority admission -- fails before mutation
# --------------------------------------------------------------------------- #
def test_missing_authority_tuple_rejected(client, monkeypatch):
    monkeypatch.setattr(campaigns, "_finish_campaign", _never_called)
    result = client.post("/api/campaigns/conversation/finish", json=FINISH_BODY)
    assert result.status_code == 422
    assert result.json()["error"]["error_code"] == "missing_authority_tuple"


def test_half_present_authority_tuple_rejected(client, monkeypatch):
    monkeypatch.setattr(campaigns, "_finish_campaign", _never_called)
    result = client.post(
        "/api/campaigns/conversation/finish",
        headers={"X-Authority-Session-Id": SESSION_ID}, json=FINISH_BODY)
    assert result.status_code == 422
    assert result.json()["error"]["error_code"] == "invalid_authority_tuple"


def test_stale_or_foreign_turn_rejected(client, monkeypatch):
    monkeypatch.setattr(campaigns, "_finish_campaign", _never_called)
    result = client.post(
        "/api/campaigns/conversation/finish",
        headers={"X-Authority-Session-Id": SESSION_ID, "X-Authority-Turn-Id": "wrong-turn"},
        json=FINISH_BODY)
    assert result.status_code == 409
    assert result.json()["error"]["error_code"] == "stage_authority_invalid"


def test_session_with_no_project_link_rejected(client, monkeypatch):
    monkeypatch.setattr(campaigns, "_finish_campaign", _never_called)
    monkeypatch.setattr(session_store, "get_session",
                        lambda session_id: {"tenant_id": ORG_ID, "org_id": None, "project_id": None})
    result = client.post("/api/campaigns/conversation/finish", headers=HEADERS, json=FINISH_BODY)
    assert result.status_code == 404
    assert result.json()["error"]["error_code"] == "project_unavailable"


def test_revoked_project_role_rejected(client, monkeypatch):
    monkeypatch.setattr(campaigns, "_finish_campaign", _never_called)

    def revoked(session, tenant, write=False, binding=None):
        raise platform_link.ProjectSessionForbidden("role revoked")

    monkeypatch.setattr(platform_link, "require_project_session_access", revoked)
    result = client.post("/api/campaigns/conversation/finish", headers=HEADERS, json=FINISH_BODY)
    assert result.status_code == 403
    assert result.json()["error"]["error_code"] == "forbidden"


def test_unknown_session_rejected(client, monkeypatch):
    monkeypatch.setattr(campaigns, "_finish_campaign", _never_called)
    result = client.post(
        "/api/campaigns/conversation/finish",
        headers={"X-Authority-Session-Id": "no-such-session", "X-Authority-Turn-Id": TURN_ID},
        json=FINISH_BODY)
    assert result.status_code == 409
    assert result.json()["error"]["error_code"] == "stage_authority_invalid"


# --------------------------------------------------------------------------- #
# finish: closed schema, session-derived project, stable idempotency
# --------------------------------------------------------------------------- #
def test_finish_rejects_unknown_or_privileged_fields(client, monkeypatch):
    monkeypatch.setattr(campaigns, "_finish_campaign", _never_called)
    for extra in ({"org_id": ORG_ID}, {"grants": ["x"]}, {"status": "finished"},
                  {"evidence": {}}, {"source_revision": "abc"}, {"commands": ["x"]}):
        body = dict(FINISH_BODY, **extra)
        result = client.post("/api/campaigns/conversation/finish", headers=HEADERS, json=body)
        assert result.status_code == 400, extra


def test_finish_derives_project_from_session_never_from_body(client, monkeypatch):
    calls = []

    def fake_finish(tenant, project, title, prompt, finish, key, **authority):
        assert authority == {'authority_session_id': SESSION_ID, 'authority_turn_id': TURN_ID}
        calls.append((str(tenant), project, title, prompt, finish, key))
        return {"campaign_id": "cid", "completion": {"release": None}}

    monkeypatch.setattr(campaigns, "_finish_campaign", fake_finish)
    result = client.post("/api/campaigns/conversation/finish", headers=HEADERS, json=FINISH_BODY)
    assert result.status_code == 200
    assert len(calls) == 1
    tenant_arg, project_arg, title_arg, prompt_arg, finish_arg, _key = calls[0]
    assert project_arg == PROJECT_ID
    assert tenant_arg == ORG_ID
    assert title_arg == FINISH_BODY["title"]
    assert prompt_arg == FINISH_BODY["prompt"]
    assert finish_arg == {
        "delivery_profile": "cad_file", "intended_user": "owner",
        "workflow": "download", "artifact_refs": [],
    }
    assert result.json()["ok"] is True


def test_finish_idempotency_key_is_stable_across_retry_turns(client, monkeypatch):
    keys = []

    def fake_finish(tenant, project, title, prompt, finish, key, **authority):
        keys.append(key)
        return {"campaign_id": "cid", "completion": {"release": None}}

    monkeypatch.setattr(campaigns, "_finish_campaign", fake_finish)
    for _ in range(2):
        result = client.post("/api/campaigns/conversation/finish", headers=HEADERS, json=FINISH_BODY)
        assert result.status_code == 200
    assert len(keys) == 2
    assert keys[0] == keys[1]


def test_finish_idempotency_key_changes_with_intent(client, monkeypatch):
    keys = []

    def fake_finish(tenant, project, title, prompt, finish, key, **authority):
        keys.append(key)
        return {"campaign_id": "cid", "completion": {"release": None}}

    monkeypatch.setattr(campaigns, "_finish_campaign", fake_finish)
    client.post("/api/campaigns/conversation/finish", headers=HEADERS, json=FINISH_BODY)
    other = dict(FINISH_BODY, title="A different finish title")
    client.post("/api/campaigns/conversation/finish", headers=HEADERS, json=other)
    assert keys[0] != keys[1]


def test_finish_propagates_engine_conflict(client, monkeypatch):
    def conflicting(tenant, project, title, prompt, finish, key, **authority):
        raise service.delivery.DeliveryConflict("Finish idempotency collision")

    monkeypatch.setattr(campaigns, "_finish_campaign", conflicting)
    result = client.post("/api/campaigns/conversation/finish", headers=HEADERS, json=FINISH_BODY)
    assert result.status_code == 409


# --------------------------------------------------------------------------- #
# status: campaign_id required, release_id optional, closed schema
# --------------------------------------------------------------------------- #
def test_status_requires_campaign_id(client, monkeypatch):
    monkeypatch.setattr(service, "snapshot", _never_called)
    result = client.post("/api/campaigns/conversation/status", headers=HEADERS, json={})
    assert result.status_code == 400


def test_status_rejects_unknown_fields(client, monkeypatch):
    monkeypatch.setattr(service, "snapshot", _never_called)
    result = client.post(
        "/api/campaigns/conversation/status", headers=HEADERS,
        json={"campaign_id": str(uuid.uuid4()), "checks": []})
    assert result.status_code == 400


def test_status_dispatches_with_session_derived_project(client, monkeypatch):
    calls = []

    def fake_snapshot(tenant, project, campaign_id, release_id=None):
        calls.append((str(tenant), project, campaign_id, release_id))
        return {"release": None, "stages": [], "decisions": [], "coverage": [],
                "remaining": [], "deliverables": [], "next_action": None}

    monkeypatch.setattr(service, "snapshot", fake_snapshot)
    campaign_id = str(uuid.uuid4())
    result = client.post(
        "/api/campaigns/conversation/status", headers=HEADERS, json={"campaign_id": campaign_id})
    assert result.status_code == 200
    assert calls == [(ORG_ID, PROJECT_ID, campaign_id, None)]
    body = result.json()["completion"]
    for key in ("remaining", "deliverables", "coverage", "next_action"):
        assert key in body


def test_status_with_release_id_dispatches_release_snapshot(client, monkeypatch):
    calls = []

    def fake_snapshot(tenant, project, campaign_id, release_id=None):
        calls.append(release_id)
        return {"release": None}

    monkeypatch.setattr(service, "snapshot", fake_snapshot)
    campaign_id, release_id = str(uuid.uuid4()), str(uuid.uuid4())
    result = client.post(
        "/api/campaigns/conversation/status", headers=HEADERS,
        json={"campaign_id": campaign_id, "release_id": release_id})
    assert result.status_code == 200
    assert calls == [release_id]


def test_status_fails_before_dispatch_on_revoked_role(client, monkeypatch):
    monkeypatch.setattr(service, "snapshot", _never_called)

    def revoked(session, tenant, write=False, binding=None):
        raise platform_link.ProjectSessionForbidden("role revoked")

    monkeypatch.setattr(platform_link, "require_project_session_access", revoked)
    result = client.post(
        "/api/campaigns/conversation/status", headers=HEADERS,
        json={"campaign_id": str(uuid.uuid4())})
    assert result.status_code == 403
