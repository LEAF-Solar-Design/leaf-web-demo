from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from dataclasses import replace
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


SOURCE = b"# exact source\r\nprint('hello')\n\x00\xff"
SOURCE_PATH = "tools/recovered-tool/tool.py"


def _source_response(recovery, change, query="view=source"):
    return recovery.client.get(f"/api/author/stages/{change.change_set_id}?{query}")


def _source_hop(monkeypatch, *, data=SOURCE, headers=None, status=200, read=None):
    import requests

    raw = io.BytesIO(data)
    response = SimpleNamespace(status_code=status, headers={
        "Content-Length": str(len(data)), "Content-Type": "application/octet-stream",
        "X-Leaf-Source-Commit": STAGED, "X-Leaf-Tool-Slug": "recovered-tool",
        "X-Leaf-Source-Blob": hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest(),
        "X-Leaf-Source-Sha256": hashlib.sha256(data).hexdigest(),
        **(headers or {}),
    }, raw=SimpleNamespace(read=read or (lambda n, **_kw: raw.read(n))), close=Mock())
    post = Mock(return_value=response)
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.invalid")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "hermetic-secret")
    monkeypatch.setattr(requests, "post", post)
    return post, response


def _source_tool(monkeypatch, **fields):
    tool = {"name": "recovered-tool", "kind": "script", "entry": SOURCE_PATH, **fields}
    reader = Mock(return_value=tool)
    monkeypatch.setattr(CustomizationService, "_staged_tool", staticmethod(reader))
    return reader


def test_source_exact_bytes_and_durable_identity(recovery, monkeypatch):
    change = _reserve(recovery, state=ChangeState.STAGED)
    tool = _pinned_reads(monkeypatch, change.tenant_id)
    tool.update(kind="script", entry=SOURCE_PATH)
    post, response = _source_hop(monkeypatch)
    before = _snapshot(recovery.store)
    result = _source_response(recovery, change)
    assert result.status_code == 200
    assert result.content == SOURCE
    assert result.headers["content-type"] == "application/octet-stream"
    assert result.headers["content-length"] == str(len(SOURCE))
    assert result.headers["x-leaf-source-path"] == SOURCE_PATH
    for key in ("X-Leaf-Source-Commit", "X-Leaf-Source-Blob", "X-Leaf-Source-Sha256"):
        assert result.headers[key] == response.headers[key]
    assert "no-store" in result.headers["cache-control"]
    assert result.headers["x-content-type-options"] == "nosniff"
    post.assert_called_once_with(
        "http://harness.invalid/internal/author/staged-source", timeout=(5, 30),
        headers={"X-Harness-Secret": "hermetic-secret", "X-Tenant-Id": change.tenant_id,
                 "Accept-Encoding": "identity"},
        json={"tenant_id": change.tenant_id, "changeSetId": change.change_set_id,
              "stagedCommit": change.staged_commit, "slug": "recovered-tool"},
        stream=True, allow_redirects=False,
    )
    response.close.assert_called_once()
    assert _snapshot(recovery.store) == before


def test_source_default_status_unchanged(recovery, monkeypatch):
    change = _reserve(recovery, state=ChangeState.STAGED)
    _pinned_reads(monkeypatch, change.tenant_id)
    post, _ = _source_hop(monkeypatch)
    response = _source_response(recovery, change, "")
    assert response.status_code == 200
    assert response.json() == recovery.service.stage_status_change(change)
    assert "x-leaf-source-path" not in response.headers
    post.assert_not_called()


@pytest.mark.parametrize("query", [
    "view=source&ref=main", "view=source&repo=elsewhere", "view=source&path=tool.py",
    "view=source&slug=other", "view=source&tenant_id=tenant-b",
    "view=source&staged_commit=" + BASE, "view=source&view=source", "view=other",
])
def test_source_rejects_caller_authority(recovery, monkeypatch, query):
    change = _reserve(recovery, state=ChangeState.STAGED)
    reader = _source_tool(monkeypatch)
    post, _ = _source_hop(monkeypatch)
    response = _source_response(recovery, change, query)
    _assert_refusal(response, 422, "invalid_stage_source_request")
    assert "no-store" in response.headers["cache-control"]
    reader.assert_not_called()
    post.assert_not_called()


@pytest.mark.parametrize("identity,status,reason", [
    ({"tenant": "tenant-b"}, 404, "stage_source_not_available"),
    ({"subject": MALLORY}, 404, "stage_source_not_available"),
    ({"subject": None}, 403, "tenant_identity_binding_unavailable"),
    ({"subject": "  "}, 403, "tenant_identity_binding_unavailable"),
    ({"role": "viewer"}, 403, "tenant_role_denied"),
    ({"tier": "restricted"}, 403, "builder_entitlement_missing"),
])
def test_source_identity_refusals(recovery, monkeypatch, identity, status, reason):
    change = _reserve(recovery, state=ChangeState.STAGED)
    recovery.identity(**identity)
    reader = _source_tool(monkeypatch)
    post, _ = _source_hop(monkeypatch)
    response = _source_response(recovery, change)
    _assert_refusal(response, status, reason)
    reader.assert_not_called()
    post.assert_not_called()


@pytest.mark.parametrize("state", [state.value for state in ChangeState if state is not ChangeState.STAGED])
def test_source_requires_active_staged_state(recovery, monkeypatch, state):
    change = _reserve(recovery, state=ChangeState.STAGED)
    _set_columns(recovery, change, f"state = '{state}'")
    reader = _source_tool(monkeypatch)
    post, _ = _source_hop(monkeypatch)
    _assert_refusal(_source_response(recovery, change), 404, "stage_source_not_available")
    reader.assert_not_called()
    post.assert_not_called()


@pytest.mark.parametrize("fields", [
    {"name": "../escape"}, {"name": "a/b"}, {"name": "a\\b"}, {"name": "a" * 65},
    {"entry": "tools/recovered-tool/../secret"}, {"entry": "/etc/passwd"},
    {"entry": "tools/other/tool.py"}, {"kind": "primitive"},
])
def test_source_requires_declared_safe_entry(recovery, monkeypatch, fields):
    change = _reserve(recovery, state=ChangeState.STAGED)
    _source_tool(monkeypatch, **fields)
    post, _ = _source_hop(monkeypatch)
    _assert_refusal(_source_response(recovery, change), 404, "stage_source_not_available")
    post.assert_not_called()


@pytest.mark.parametrize("mutation", ["subject", "tenant", "state", "commit", "binding", "auth", "declaration"])
def test_source_rechecks_after_read(recovery, monkeypatch, mutation):
    change = _reserve(recovery, state=ChangeState.STAGED)
    reader = _source_tool(monkeypatch)
    post, _ = _source_hop(monkeypatch)
    original = post.return_value

    def mutate(*_args, **_kwargs):
        if mutation == "subject":
            _set_columns(recovery, change, f"author_subject = '{MALLORY}'")
        elif mutation == "tenant":
            recovery.identity(tenant="tenant-b")
        elif mutation == "state":
            _set_columns(recovery, change, "state = 'published'")
        elif mutation == "commit":
            _set_columns(recovery, change, f"staged_commit = '{BASE}'")
        elif mutation == "binding":
            recovery.identity(role="viewer")
        elif mutation == "auth":
            monkeypatch.setattr(deps, "auth_live", lambda: False)
        else:
            reader.return_value = {"name": "other", "kind": "script", "entry": "tools/other/tool.py"}
        return original

    post.side_effect = mutate
    response = _source_response(recovery, change)
    assert response.status_code in (403, 404, 503)
    assert SOURCE not in response.content
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize("headers", [
    {"Content-Length": "262145"}, {"Content-Length": "-1"}, {"Content-Length": ""},
    {"Content-Length": "01"}, {"Content-Length": "1"}, {"Content-Length": "200"},
    {"X-Leaf-Source-Commit": BASE}, {"X-Leaf-Source-Blob": "0" * 40},
    {"X-Leaf-Source-Sha256": "0" * 64}, {"X-Leaf-Tool-Slug": "other"},
    {"Content-Encoding": "gzip"}, {"Content-Type": "text/html"},
])
def test_source_refuses_invalid_transport_metadata(recovery, monkeypatch, headers):
    change = _reserve(recovery, state=ChangeState.STAGED)
    _source_tool(monkeypatch)
    post, response = _source_hop(monkeypatch, headers=headers)
    _assert_refusal(_source_response(recovery, change), 503, "stage_source_failed")
    response.close.assert_called_once()


def test_source_cap_precedes_read_and_redirects_release_nothing(recovery, monkeypatch):
    change = _reserve(recovery, state=ChangeState.STAGED)
    _source_tool(monkeypatch)
    read = Mock(side_effect=AssertionError("invalid response was buffered"))
    for status, headers in ((200, {"Content-Length": "262145"}), (302, {}), (404, {})):
        _, response = _source_hop(monkeypatch, headers=headers, status=status, read=read)
        _assert_refusal(_source_response(recovery, change), 503, "stage_source_failed")
        response.close.assert_called_once()
    read.assert_not_called()


def test_source_exact_cap_and_script_declaration(recovery, monkeypatch):
    change = _reserve(recovery, state=ChangeState.STAGED)
    _source_tool(monkeypatch, kind="script", entry="tools/recovered-tool/tool.py")
    data = b"x" * customization_service.MAX_STAGED_SOURCE_BYTES
    _source_hop(monkeypatch, data=data)
    response = _source_response(recovery, change)
    assert response.status_code == 200
    assert response.content == data
    assert response.headers["x-leaf-source-path"] == "tools/recovered-tool/tool.py"


def test_source_missing_row_auth_off_and_disabled(recovery, monkeypatch):
    change = SimpleNamespace(change_set_id=_key())
    post, _ = _source_hop(monkeypatch)
    _assert_refusal(_source_response(recovery, change), 404, "stage_source_not_available")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "off")
    _assert_refusal(_source_response(recovery, change), 404, "customization_stage_disabled")
    monkeypatch.setattr(deps, "auth_live", lambda: False)
    _assert_refusal(_source_response(recovery, change), 503, "customization_auth_required")
    post.assert_not_called()


def test_source_rejects_misbound_store_row(recovery, monkeypatch):
    change = _reserve(recovery, state=ChangeState.STAGED)
    post, _ = _source_hop(monkeypatch)
    for fields in ({"tenant_id": "tenant-b"}, {"change_set_id": _key()}, {"author_subject": ""},
                   {"staged_commit": None}, {"catalog_digest": None}, {"tool_target": "branch"}):
        monkeypatch.setattr(recovery.store, "get_change_set", Mock(return_value=replace(change, **fields)))
        _assert_refusal(_source_response(recovery, change), 404, "stage_source_not_available")
    post.assert_not_called()


def test_source_rechecks_after_metadata_before_dispatch(recovery, monkeypatch):
    change = _reserve(recovery, state=ChangeState.STAGED)
    reader = _source_tool(monkeypatch)
    tool = reader.return_value

    def revoke(_change):
        _set_columns(recovery, change, "state = 'superseded'")
        return tool

    reader.side_effect = revoke
    post, _ = _source_hop(monkeypatch)
    _assert_refusal(_source_response(recovery, change), 404, "stage_source_not_available")
    post.assert_not_called()


def test_source_requires_existing_secret_and_sanitizes_failures(recovery, monkeypatch):
    change = _reserve(recovery, state=ChangeState.STAGED)
    _source_tool(monkeypatch)
    post, _ = _source_hop(monkeypatch)
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "")
    _assert_refusal(_source_response(recovery, change), 503, "stage_source_failed")
    post.assert_not_called()
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "hermetic-secret")
    post.side_effect = OSError("secret=do-not-release /private/repo")
    response = _source_response(recovery, change)
    _assert_refusal(response, 503, "stage_source_failed")
    assert "do-not-release" not in response.text
    assert "/private/repo" not in response.text


def test_source_bounds_each_read_before_retaining_chunks(recovery, monkeypatch):
    change = _reserve(recovery, state=ChangeState.STAGED)
    _source_tool(monkeypatch)
    requested = []

    def overrun(n, **_kwargs):
        requested.append(n)
        return b"x" * (n + 1)

    _, response = _source_hop(monkeypatch, read=overrun)
    _assert_refusal(_source_response(recovery, change), 503, "stage_source_failed")
    assert requested == [len(SOURCE) + 1]
    response.close.assert_called_once()


def test_view_declaration_cannot_label_a_script_blob(recovery, monkeypatch):
    change = _reserve(recovery, state=ChangeState.STAGED)
    _source_tool(monkeypatch, kind="view", entry="tools/recovered-tool/view.html")
    _assert_refusal(_source_response(recovery, change), 404, "stage_source_not_available")
