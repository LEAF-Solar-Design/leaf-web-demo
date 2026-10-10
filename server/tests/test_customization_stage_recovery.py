from __future__ import annotations

import hashlib
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import author_quota
import customization_service
import deps
import session_store
from customization_models import ChangeState
from customization_service import CustomizationService, CustomizationServiceError
from customization_store import SQLiteCustomizationStore
from envelopes import install_error_handlers
from routers import author as author_router


R = "leaf.customization-stage-recovery.v1"
MISS = {"contract": R, "status": "not_found"}
PENDING = {"contract": R, "status": "admission_pending", "retry_after_ms": 1000}
BASE = "a" * 40
STAGED = "b" * 40
DIGEST = "c" * 64
WORKSPACE = "d" * 64
DESCRIPTION = "author a bounded write tool"
FINGERPRINT = hashlib.sha256(DESCRIPTION.encode("utf-8")).hexdigest()
ALICE = "auth0|alice"
MALLORY = "auth0|mallory"
RECOVER = "/api/author/stage/recover"


def _key():
    return str(uuid4())


def _snapshot(store):
    with store._connection() as conn:
        return tuple(conn.iterdump())


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    store = SQLiteCustomizationStore(tmp_path / "customization.db")
    store.initialize()
    service = CustomizationService(store)
    state = SimpleNamespace(store=store, service=service)
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_ALLOW_STATIC_BINDINGS", "1")
    monkeypatch.setenv("LEAF_TENANT_MCP_BROKER_URL", "")
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    monkeypatch.setattr(customization_service.entitlements, "resolve_tier", lambda tenant: tenant.tier)
    monkeypatch.setattr(customization_service.entitlements, "resolve_roles", lambda tenant: (tenant.roles, tenant.elevated))
    monkeypatch.setattr(
        customization_service.entitlements, "entitlements_for",
        lambda tier, *_roles: {"build": tier == "hosted_pro"},
    )
    monkeypatch.setattr(CustomizationService, "configured", classmethod(lambda cls: service))

    def identity(tenant="tenant-a", subject=ALICE, role="owner", tier="hosted_pro"):
        state.tenant = deps.TenantContext(tenant, tier=tier, subject=subject)
        monkeypatch.setenv("LEAF_CUSTOMIZATION_TENANT_BINDINGS", json.dumps({
            tenant: {"subject": subject, "role": role},
        }))

    state.identity = identity
    identity()
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(author_router.router)
    app.dependency_overrides[deps.require_tenant] = lambda: state.tenant
    state.app = app
    with TestClient(app) as client:
        state.client = client
        yield state


@pytest.fixture
def live_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "sessions.db")
    monkeypatch.setattr(session_store, "_conn", None)
    session_store.ensure_started()
    session = session_store.get_or_create_session("tenant-a", "drawing-a")
    session_id, turn_id = session["session_id"], _key()
    assert session_store.try_begin_turn(
        session_id, turn_id, 60, tier="hosted_pro", subject=ALICE
    )
    yield session_id, turn_id
    session_store._conn.close()


def _reserve(recovery, *, state=ChangeState.STAGING, authority=None):
    session_id, turn_id = authority or (_key(), _key())
    change, created = recovery.store.reserve_stage(
        tenant_id=str(recovery.tenant), idempotency_key=_key(),
        base_commit=BASE, desired_platform_release="release-a",
        workspace_contract_digest=WORKSPACE, author_subject=recovery.tenant.subject,
        change_kind="create", target_tool_name=None,
        request_description=DESCRIPTION, request_fingerprint=FINGERPRINT,
        authority_session_id=session_id, authority_turn_id=turn_id,
    )
    assert created
    if state is ChangeState.CREATED:
        return change
    change = recovery.store.transition(
        tenant_id=change.tenant_id, change_set_id=change.change_set_id,
        next_state=ChangeState.STAGING, expected_version=change.version,
        expected_state=ChangeState.CREATED, idempotency_key=_key(),
    )
    if state is ChangeState.STAGED:
        return recovery.store.record_staged(
            tenant_id=change.tenant_id, change_set_id=change.change_set_id,
            expected_version=change.version, idempotency_key=_key(),
            staged_commit=STAGED, catalog_digest=DIGEST,
            platform_release="release-a", workspace_contract_digest=WORKSPACE,
        )
    if state is ChangeState.FAILED:
        claimed = recovery.store.claim_stage(owner="recovery-test-worker", lease_seconds=30)
        assert claimed and claimed.change_set_id == change.change_set_id
        return recovery.store.fail_stage_claim(
            tenant_id=change.tenant_id, change_set_id=change.change_set_id,
            owner="recovery-test-worker", reason_code="author_job_failed", retryable=False,
        )
    return change


def _recover(recovery, key, **kwargs):
    return recovery.client.post(RECOVER, json={"idempotency_key": key}, **kwargs)


def _assert_found(recovery, change, **kwargs):
    response = _recover(recovery, change.idempotency_key, **kwargs)
    assert response.status_code == 200
    assert response.json() == {
        "contract": R, "status": "found",
        "job": recovery.service.stage_status_change(change),
    }
    return response.json()["job"]


def _pinned_reads(monkeypatch, tenant_id):
    tool = {"name": "recovered-tool", "version": "1.0.0", "entry": "tools/recovered-tool.py"}
    monkeypatch.setattr(customization_service, "_bare_repo", lambda tenant: tenant)

    def blob(bare, object_name):
        assert bare == tenant_id
        registries = {
            f"{STAGED}:registry.json": {"tools": [tool]},
            f"{BASE}:registry.json": {"tools": []},
        }
        return json.dumps(registries[object_name]).encode("utf-8")

    monkeypatch.setattr(customization_service, "_git_blob", blob)
    return tool


def _set_columns(recovery, change, assignments):
    with recovery.store._transaction() as conn:
        conn.execute(
            f"UPDATE customization_change_sets SET {assignments} WHERE change_set_id = ?",
            (change.change_set_id,),
        )


def _assert_refusal(response, status, reason):
    assert response.status_code == status
    assert response.json()["reason_code"] == reason
    assert "job" not in response.json()


def test_recover_found_queued(recovery):
    change = _reserve(recovery)
    job = _assert_found(recovery, change)
    assert job["status"] == "queued"
    assert job["poll_url"] == f"/api/author/stages/{change.change_set_id}"


def test_recover_after_turn_ended(recovery, live_turn):
    change = _reserve(recovery, authority=live_turn)
    session_store.end_turn(*live_turn)
    assert deps.stage_author_identity(recovery.tenant, *live_turn) is None
    _assert_found(recovery, change)


def test_recover_bypasses_turn_lookup(recovery, monkeypatch):
    change = _reserve(recovery)
    monkeypatch.setenv("LEAF_TENANT_MCP_BROKER_URL", "http://broker.invalid")
    lookup = Mock(side_effect=AssertionError("recovery looked up turn authority"))
    monkeypatch.setattr(deps, "stage_author_identity", lookup)
    for headers in ({}, {"X-Authority-Session-Id": _key()}, {
        "X-Authority-Session-Id": _key(), "X-Authority-Turn-Id": _key(),
    }):
        _assert_found(recovery, change, headers=headers)
    lookup.assert_not_called()


def test_recover_miss(recovery):
    response = _recover(recovery, _key())
    assert response.status_code == 404
    assert response.json() == MISS


def test_recover_other_subject(recovery):
    change = _reserve(recovery)
    with recovery.store._transaction() as conn:
        conn.execute(
            "UPDATE customization_change_sets SET request_fingerprint = ? WHERE change_set_id = ?",
            ("0" * 64, change.change_set_id),
        )
    recovery.identity(subject=MALLORY)
    response = _recover(recovery, change.idempotency_key)
    assert response.status_code == 404
    assert response.json() == MISS


def test_recover_other_tenant(recovery):
    change = _reserve(recovery)
    recovery.identity(tenant="tenant-b")
    response = _recover(recovery, change.idempotency_key)
    assert response.status_code == 404
    assert response.json() == MISS


def test_recover_malformed_request(recovery, monkeypatch):
    lookup = Mock(side_effect=AssertionError("invalid body read the store"))
    monkeypatch.setattr(recovery.store, "get_change_set_by_idempotency", lookup)
    for body in ({}, {"idempotency_key": 17}, {"idempotency_key": _key(), "extra": True},
                 {"idempotency_key": "k" * 201}, {"idempotency_key": " \t\n"}):
        response = recovery.client.post(RECOVER, json=body)
        assert response.status_code == 422
    _assert_refusal(response, 422, "invalid_recovery_request")
    lookup.assert_not_called()


def test_recover_created(recovery, monkeypatch):
    change = _reserve(recovery, state=ChangeState.CREATED)
    before = _snapshot(recovery.store)
    projection = Mock(side_effect=AssertionError("pending admission projected as a job"))
    monkeypatch.setattr(recovery.service, "stage_status_change", projection)
    response = _recover(recovery, change.idempotency_key)
    assert response.status_code == 202
    assert response.json() == PENDING
    assert _snapshot(recovery.store) == before
    projection.assert_not_called()


def test_recover_staged(recovery, monkeypatch):
    change = _reserve(recovery, state=ChangeState.STAGED)
    tool = {"name": "recovered-tool", "version": "1.0.0", "entry": "tools/recovered-tool.py"}
    reads = []
    monkeypatch.setattr(customization_service, "_bare_repo", lambda tenant: tenant)

    def blob(bare, object_name):
        assert bare == change.tenant_id
        reads.append(object_name)
        registries = {
            f"{STAGED}:registry.json": {"tools": [tool]},
            f"{BASE}:registry.json": {"tools": []},
            "refs/heads/main:registry.json": {"tools": [{"name": "different-tool"}]},
        }
        return json.dumps(registries[object_name]).encode("utf-8")

    monkeypatch.setattr(customization_service, "_git_blob", blob)
    job = _assert_found(recovery, change)
    assert job["status"] == "staged"
    assert job["receipt"] == recovery.service._raw_receipt(change)
    assert job["receipt"]["staged_commit"] == STAGED
    assert job["result"]["tool"] == tool
    assert reads == [f"{STAGED}:registry.json", f"{BASE}:registry.json"] * 2


def test_recover_failed(recovery):
    change = _reserve(recovery, state=ChangeState.FAILED)
    job = _assert_found(recovery, change)
    assert job["status"] == "failed"
    assert job["error"]["reason_code"] == change.stage_error_code == "author_job_failed"


def test_recover_never_mutates(recovery, monkeypatch):
    terminal = _reserve(recovery, state=ChangeState.FAILED)
    found = _reserve(recovery)
    pending = _reserve(recovery, state=ChangeState.CREATED)
    monkeypatch.setenv("LEAF_AUTHOR_QUOTA_STORE", "memory")
    monkeypatch.setattr(author_quota, "_MEMORY_STATE", {})
    monkeypatch.setattr(author_quota, "_MEMORY_ATTEMPTS", {})
    author_quota.charge(
        "tenant-a", "2026-10-10", 10, idempotency_key=found.change_set_id, tier="hosted_pro",
    )
    before = _snapshot(recovery.store)
    quota_before = (dict(author_quota._MEMORY_STATE), dict(author_quota._MEMORY_ATTEMPTS))
    spies = []
    for target, name in (
        (recovery.service, "_reserve_stage"), (author_quota, "enforce"),
        (recovery.store, "transition"), (recovery.service, "dispatch_stage"),
        (recovery.service, "_harness_stage"), (recovery.service, "_stage_admission_base"),
        (recovery.service, "_release"), (recovery.service, "_authority"),
        (recovery.service, "enqueue_stage"), (recovery.service, "stage"),
    ):
        spy = Mock(side_effect=AssertionError(f"recovery called {name}"))
        monkeypatch.setattr(target, name, spy)
        spies.append(spy)
    for key, status in ((found.idempotency_key, 200), (_key(), 404),
                        (pending.idempotency_key, 202), (terminal.idempotency_key, 200)):
        assert _recover(recovery, key).status_code == status
    assert _snapshot(recovery.store) == before
    assert (author_quota._MEMORY_STATE, author_quota._MEMORY_ATTEMPTS) == quota_before
    for spy in spies:
        spy.assert_not_called()


def test_recover_preserves_binding(recovery, monkeypatch):
    change = _reserve(recovery)
    claimed = recovery.store.claim_stage(owner="bound-worker", lease_seconds=30)
    assert claimed and claimed.change_set_id == change.change_set_id
    monkeypatch.setenv("LEAF_PLATFORM_RELEASE", "release-after-request")
    release = Mock(side_effect=AssertionError("recovery read current release"))
    catalog = Mock(side_effect=AssertionError("recovery read current catalog"))
    monkeypatch.setattr(recovery.service, "_release", release)
    monkeypatch.setattr(recovery.store, "get_effective_catalog", catalog)
    monkeypatch.setattr(customization_service, "_git", Mock(return_value="e" * 40))
    job = _assert_found(recovery, claimed)
    after = recovery.store.get_change_set(
        tenant_id=claimed.tenant_id, change_set_id=claimed.change_set_id,
    )
    assert after == claimed
    assert job["change_set_id"] == claimed.change_set_id
    assert job["attempt"] == claimed.stage_attempt == 1
    assert (after.authority_session_id, after.authority_turn_id) == (
        change.authority_session_id, change.authority_turn_id,
    )
    assert after.base_commit == BASE
    assert after.desired_platform_release == "release-a"
    release.assert_not_called()
    catalog.assert_not_called()
    customization_service._git.assert_not_called()


def test_recover_requires_authentication(recovery, monkeypatch):
    recovery.app.dependency_overrides.clear()
    lookup = Mock(side_effect=AssertionError("unauthenticated caller read the store"))
    monkeypatch.setattr(recovery.store, "get_change_set_by_idempotency", lookup)
    response = _recover(recovery, _key())
    assert response.status_code == 401
    assert "job" not in response.json()
    lookup.assert_not_called()


def test_recover_refuses_subjectless(recovery, monkeypatch):
    change = _reserve(recovery)
    recovery.identity(subject=None)
    lookup = Mock(side_effect=AssertionError("subjectless caller read the store"))
    monkeypatch.setattr(recovery.store, "get_change_set_by_idempotency", lookup)
    _assert_refusal(_recover(recovery, change.idempotency_key), 403, "tenant_identity_binding_unavailable")
    lookup.assert_not_called()


def test_recover_rechecks_access(recovery, monkeypatch):
    change = _reserve(recovery)
    lookup = Mock(side_effect=AssertionError("refused caller read the store"))
    monkeypatch.setattr(recovery.store, "get_change_set_by_idempotency", lookup)
    recovery.identity(role="viewer")
    _assert_refusal(_recover(recovery, change.idempotency_key), 403, "tenant_role_denied")
    recovery.identity(tier="restricted")
    _assert_refusal(_recover(recovery, change.idempotency_key), 403, "builder_entitlement_missing")
    lookup.assert_not_called()


def test_recover_store_failure(recovery, monkeypatch):
    for error in (sqlite3.DatabaseError("unreadable store"), OSError("unavailable store"),
                  RuntimeError("store runtime failure")):
        lookup = Mock(side_effect=error)
        monkeypatch.setattr(recovery.store, "get_change_set_by_idempotency", lookup)
        _assert_refusal(_recover(recovery, _key()), 503, "customization_stage_failed")
        with pytest.raises(CustomizationServiceError, match="^customization_stage_failed$") as caught:
            recovery.service.recover_stage(tenant=recovery.tenant, idempotency_key=_key())
        assert caught.value.status_code == 503
        assert caught.value.__cause__ is error
        assert lookup.call_count == 2


def test_recover_non_stage_row_is_miss(recovery):
    change = recovery.store.create_change_set(
        tenant_id="tenant-a", idempotency_key=_key(),
        base_commit=BASE, desired_platform_release="release-a",
        workspace_contract_digest=WORKSPACE, author_subject=ALICE,
        change_kind="revise", target_tool_name="removed-tool",
    )
    assert change.request_description is change.request_fingerprint is None
    response = _recover(recovery, change.idempotency_key)
    assert response.status_code == 404
    assert response.json() == MISS


def test_recover_invalid_stored_outcome(recovery, monkeypatch):
    corrupt = _reserve(recovery)
    with recovery.store._transaction() as conn:
        conn.execute(
            "UPDATE customization_change_sets SET request_fingerprint = ? WHERE change_set_id = ?",
            ("0" * 64, corrupt.change_set_id),
        )
    staged = _reserve(recovery, state=ChangeState.STAGED)
    before = _snapshot(recovery.store)
    reserve = Mock(side_effect=AssertionError("corrupt outcome admitted new work"))
    monkeypatch.setattr(recovery.service, "_reserve_stage", reserve)
    _assert_refusal(_recover(recovery, corrupt.idempotency_key), 503, "customization_stage_failed")
    monkeypatch.setattr(
        CustomizationService, "_staged_tool",
        staticmethod(Mock(side_effect=OSError("pinned commit unreadable"))),
    )
    _assert_refusal(_recover(recovery, staged.idempotency_key), 503, "customization_stage_failed")
    assert _snapshot(recovery.store) == before
    reserve.assert_not_called()


INCONSISTENT = (
    ("staged-without-commit", ChangeState.STAGED, "staged_commit = NULL", "queued"),
    ("staged-without-digest", ChangeState.STAGED, "catalog_digest = NULL", "queued"),
    ("failed-with-commit", ChangeState.FAILED,
     f"staged_commit = '{STAGED}', catalog_digest = '{DIGEST}'", "staged"),
    ("staging-with-commit", ChangeState.STAGING,
     f"staged_commit = '{STAGED}', catalog_digest = '{DIGEST}'", "staged"),
    ("published-without-commit", ChangeState.STAGED,
     "state = 'published', staged_commit = NULL", "queued"),
    ("rejected-stage-row", ChangeState.STAGED, "state = 'rejected'", "staged"),
)


@pytest.mark.parametrize(
    "stored_state,assignments,projected", [case[1:] for case in INCONSISTENT],
    ids=[case[0] for case in INCONSISTENT],
)
def test_recover_inconsistent_stored_state(recovery, monkeypatch, stored_state, assignments, projected):
    change = _reserve(recovery, state=stored_state)
    _set_columns(recovery, change, assignments)
    _pinned_reads(monkeypatch, change.tenant_id)
    original = recovery.service.stage_status_change
    seen = []

    def projection(row):
        job = original(row)
        seen.append(job["status"])
        return job

    monkeypatch.setattr(recovery.service, "stage_status_change", projection)
    before = _snapshot(recovery.store)
    _assert_refusal(_recover(recovery, change.idempotency_key), 503, "customization_stage_failed")
    with pytest.raises(CustomizationServiceError, match="^customization_stage_failed$") as caught:
        recovery.service.recover_stage(tenant=recovery.tenant, idempotency_key=change.idempotency_key)
    assert caught.value.status_code == 503
    assert seen == [projected, projected]
    assert _snapshot(recovery.store) == before


def test_recover_later_stage_states(recovery, monkeypatch):
    tool = None
    for state in ("awaiting_approval", "approved", "publishing", "published",
                  "superseded", "rolled_back"):
        change = _reserve(recovery, state=ChangeState.STAGED)
        tool = _pinned_reads(monkeypatch, change.tenant_id)
        _set_columns(recovery, change, f"state = '{state}'")
        stored = recovery.store.get_change_set(
            tenant_id=change.tenant_id, change_set_id=change.change_set_id,
        )
        assert stored.state.value == state
        job = _assert_found(recovery, stored)
        assert job["status"] == "staged"
        assert job["result"]["tool"] == tool


def test_normal_admission_stays_strict(recovery, live_turn, monkeypatch):
    change = _reserve(recovery, authority=live_turn)
    monkeypatch.setattr(
        recovery.service, "_authority",
        lambda: SimpleNamespace(authorize_stage=lambda **_kwargs: None),
    )
    monkeypatch.setattr(
        recovery.service, "_release",
        lambda: SimpleNamespace(release_id="release-a", workspace_contract_sha256=WORKSPACE),
    )
    session_store.end_turn(*live_turn)
    next_turn = _key()
    assert session_store.try_begin_turn(
        live_turn[0], next_turn, 60, tier="hosted_pro", subject=ALICE
    )
    body = {"description": DESCRIPTION, "mode": "build", "idempotency_key": change.idempotency_key}
    before = _snapshot(recovery.store)
    response = recovery.client.post("/api/author/stage", json=body, headers={
        "X-Authority-Session-Id": live_turn[0], "X-Authority-Turn-Id": next_turn,
    })
    _assert_refusal(response, 409, "idempotency_replay")
    monkeypatch.setenv("LEAF_TENANT_MCP_BROKER_URL", "http://broker.invalid")
    response = recovery.client.post("/api/author/stage", json=body)
    _assert_refusal(response, 409, "stage_authority_invalid")
    session_store.end_turn(live_turn[0], next_turn)
    response = recovery.client.post("/api/author/stage", json=body, headers={
        "X-Authority-Session-Id": live_turn[0], "X-Authority-Turn-Id": next_turn,
    })
    _assert_refusal(response, 409, "stage_authority_invalid")
    assert _snapshot(recovery.store) == before


def test_recover_disabled_rollout(recovery, monkeypatch):
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "off")
    lookup = Mock(side_effect=AssertionError("disabled rollout read the store"))
    monkeypatch.setattr(recovery.store, "get_change_set_by_idempotency", lookup)
    configured = Mock(side_effect=AssertionError("disabled rollout configured the service"))
    monkeypatch.setattr(CustomizationService, "configured", configured)
    expected = author_router._customization_gate(5, recovery.tenant)
    response = _recover(recovery, _key())
    assert response.status_code == expected.status_code == 404
    assert response.json() == json.loads(expected.body)
    configured.assert_not_called()
    with pytest.raises(CustomizationServiceError, match="^customization_stage_disabled$") as caught:
        recovery.service.recover_stage(tenant=recovery.tenant, idempotency_key=_key())
    assert caught.value.status_code == 404
    lookup.assert_not_called()
