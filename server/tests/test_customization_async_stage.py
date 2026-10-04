from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import customization_stage_worker
import customization_store
import customization_service
import author_quota
import deps
import session_store
from customization_models import (
    ChangeSetNotFoundError,
    ChangeSetConflictError,
    ChangeState,
    IdempotencyReplayError,
)
from customization_service import CustomizationService
from customization_service import CustomizationServiceError
from customization_store import SQLiteCustomizationStore
from routers import author as author_router


BASE = "a" * 40
STAGED = "b" * 40
DIGEST = "c" * 64
WORKSPACE = "d" * 64
DESCRIPTION = "author a bounded write tool"
FINGERPRINT = hashlib.sha256(DESCRIPTION.encode()).hexdigest()
ALICE = "auth0|alice"
MALLORY = "auth0|mallory"


@pytest.fixture
def store(tmp_path):
    result = SQLiteCustomizationStore(tmp_path / "customization.db")
    result.initialize()
    return result


@pytest.fixture
def active_author_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "sessions.db")
    monkeypatch.setattr(session_store, "_conn", None)
    session_store.ensure_started()
    session = session_store.get_or_create_session("tenant-a", "drawing-a")
    session_id = session["session_id"]
    turn_id = "turn-author"
    assert session_store.try_begin_turn(
        session_id, turn_id, 60, tier="hosted_pro", subject=ALICE
    )
    return session_id, turn_id


def reserve(store, *, tenant="tenant-a", key="request-a", change_id=None):
    return store.reserve_stage(
        tenant_id=tenant,
        idempotency_key=key,
        base_commit=BASE,
        desired_platform_release="release-a",
        workspace_contract_digest=WORKSPACE,
        author_subject="auth0|author",
        change_set_id=change_id,
        change_kind="create",
        target_tool_name=None,
        request_description=DESCRIPTION,
        request_fingerprint=FINGERPRINT,
    )


def queued(store, *, tenant="tenant-a", key="request-a"):
    change, created = reserve(store, tenant=tenant, key=key)
    assert created
    return store.transition(
        tenant_id=tenant,
        change_set_id=change.change_set_id,
        next_state=ChangeState.STAGING,
        expected_version=change.version,
        expected_state=ChangeState.CREATED,
        idempotency_key=f"stage:{key}",
    )


def bind_removal_marker(store, change):
    with store._transaction() as conn:
        conn.execute(
            "INSERT INTO customization_removal_requests "
            "(tenant_id, change_set_id, target_tool_name, "
            "expected_catalog_digest, predecessor_change_set_id, "
            "predecessor_catalog_commit, predecessor_catalog_digest, "
            "predecessor_platform_release, "
            "predecessor_workspace_contract_digest) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                change.tenant_id, change.change_set_id, "count-by-layer",
                DIGEST, f"predecessor-{change.change_set_id}", BASE, DIGEST,
                "release-a", WORKSPACE,
            ),
        )


def seed_effective_catalog(store, *, tenant="tenant-a"):
    predecessor = store.create_change_set(
        tenant_id=tenant, idempotency_key=f"{tenant}-predecessor",
        base_commit=BASE, desired_platform_release="release-a",
        workspace_contract_digest=WORKSPACE,
        author_subject="auth0|author",
    )
    with store._transaction() as conn:
        conn.execute(
            "UPDATE customization_change_sets SET state = ?, "
            "staged_commit = ?, catalog_digest = ? WHERE tenant_id = ? "
            "AND change_set_id = ?",
            (
                ChangeState.PUBLISHED.value, BASE, DIGEST, tenant,
                predecessor.change_set_id,
            ),
        )
        conn.execute(
            "INSERT INTO effective_catalogs "
            "(tenant_id, change_set_id, catalog_commit, catalog_digest, "
            "effective_platform_release, workspace_contract_digest) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                tenant, predecessor.change_set_id, BASE, DIGEST,
                "release-a", WORKSPACE,
            ),
        )
    return predecessor


def test_exact_reservation_is_atomic_and_conflicting_prompt_is_rejected(store):
    ids = [
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda cid: reserve(store, change_id=cid), ids))
    assert sum(created for _change, created in results) == 1
    assert len({change.change_set_id for change, _created in results}) == 1

    with pytest.raises(IdempotencyReplayError):
        store.reserve_stage(
            tenant_id="tenant-a", idempotency_key="request-a",
            base_commit=BASE, desired_platform_release="release-a",
            workspace_contract_digest=WORKSPACE, author_subject="auth0|author",
            change_kind="create", target_tool_name=None,
            request_description="different intent",
            request_fingerprint=hashlib.sha256(b"different intent").hexdigest(),
        )


def test_exact_service_replay_never_recharges_or_requeues(store, monkeypatch):
    service = CustomizationService(store)
    charges = []
    monkeypatch.setattr(customization_service, "enabled", lambda *_args: True)
    monkeypatch.setattr(
        customization_service, "_binding",
        lambda _tenant: SimpleNamespace(subject="auth0|author", role="owner"),
    )
    monkeypatch.setattr(
        customization_service.entitlements, "resolve_tier",
        lambda _tenant: "hosted_pro",
    )
    monkeypatch.setattr(
        customization_service.entitlements, "entitlements_for",
        lambda _tier, *_roles: {"build": True},
    )
    monkeypatch.setattr(
        service, "_authority",
        lambda: SimpleNamespace(authorize_stage=lambda **_kwargs: None),
    )
    monkeypatch.setattr(
        service, "_release",
        lambda: SimpleNamespace(
            release_id="release-a", workspace_contract_sha256=WORKSPACE
        ),
    )
    monkeypatch.setattr(
        customization_service, "_harness_misconfigured", lambda: False
    )
    monkeypatch.setattr(
        customization_service, "_ensure_bare_repo", lambda _tenant: object()
    )
    monkeypatch.setattr(customization_service, "_git", lambda *_args: BASE)
    monkeypatch.setattr(
        customization_service.author_quota, "enforce",
        lambda tenant, tier, **_kwargs: charges.append((tenant, tier)),
    )
    tenant = deps.TenantContext(
        "tenant-a", tier="hosted_pro", subject="auth0|author"
    )
    first = service.enqueue_stage(
        tenant=tenant, description=DESCRIPTION, mode="build",
        idempotency_key="request-a",
    )
    replay = service.enqueue_stage(
        tenant=tenant, description=DESCRIPTION, mode="build",
        idempotency_key="request-a",
    )
    assert first["change_set_id"] == replay["change_set_id"]
    assert first["status"] == replay["status"] == "queued"
    assert charges == [("tenant-a", "hosted_pro")]
    assert store.get_change_set(
        tenant_id="tenant-a", change_set_id=first["change_set_id"]
    ).stage_attempt == 0


def _admission_service(store, monkeypatch):
    service = CustomizationService(store)
    monkeypatch.setattr(customization_service, "enabled", lambda *_args: True)
    monkeypatch.setattr(
        customization_service, "_binding",
        lambda _tenant: SimpleNamespace(subject="auth0|author", role="owner"),
    )
    monkeypatch.setattr(
        customization_service.entitlements, "resolve_tier",
        lambda _tenant: "hosted_pro",
    )
    monkeypatch.setattr(
        customization_service.entitlements, "entitlements_for",
        lambda _tier, *_roles: {"build": True},
    )
    monkeypatch.setattr(
        service, "_authority",
        lambda: SimpleNamespace(authorize_stage=lambda **_kwargs: None),
    )
    monkeypatch.setattr(
        service, "_release",
        lambda: SimpleNamespace(
            release_id="release-a", workspace_contract_sha256=WORKSPACE
        ),
    )
    monkeypatch.setattr(customization_service, "_harness_misconfigured", lambda: False)
    monkeypatch.setattr(customization_service, "_ensure_bare_repo", lambda _tenant: object())
    monkeypatch.setattr(customization_service, "_git", lambda *_args: BASE)
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "5")
    monkeypatch.setenv("LEAF_AUTHOR_QUOTA_STORE", "memory")
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "test")
    monkeypatch.setattr(author_quota, "durability_required", lambda: False)
    author_quota.reset_memory_state()
    author_quota.reset_usage_policy()
    tenant = deps.TenantContext(
        "tenant-a", tier="hosted_pro", subject="auth0|author"
    )
    return service, tenant


@pytest.mark.parametrize("window", ["before_charge", "after_charge", "before_queue"])
def test_created_admission_crash_replay_resumes_with_one_charge(
    store, monkeypatch, window
):
    service, tenant = _admission_service(store, monkeypatch)
    real_enforce = author_quota.enforce
    real_transition = store.transition
    crashed = {"done": False}

    if window == "before_charge":
        def crash_charge(*_args, **_kwargs):
            if not crashed["done"]:
                crashed["done"] = True
                raise RuntimeError("crash before charge")
            return real_enforce(*_args, **_kwargs)
        monkeypatch.setattr(author_quota, "enforce", crash_charge)
    elif window == "after_charge":
        def charge_then_crash(*args, **kwargs):
            result = real_enforce(*args, **kwargs)
            if not crashed["done"]:
                crashed["done"] = True
                raise RuntimeError("crash after charge")
            return result
        monkeypatch.setattr(author_quota, "enforce", charge_then_crash)
    else:
        def crash_queue(**kwargs):
            if (kwargs.get("next_state") is ChangeState.STAGING
                    and not crashed["done"]):
                crashed["done"] = True
                raise RuntimeError("crash before queue")
            return real_transition(**kwargs)
        monkeypatch.setattr(store, "transition", crash_queue)

    with pytest.raises(RuntimeError):
        service.enqueue_stage(
            tenant=tenant, description=DESCRIPTION, mode="build",
            idempotency_key="request-a",
        )
    result = service.enqueue_stage(
        tenant=tenant, description=DESCRIPTION, mode="build",
        idempotency_key="request-a",
    )
    assert result["status"] == "queued"
    assert list(author_quota._MEMORY_STATE.values()) == [1]


def test_created_crash_replay_uses_original_day_tier_and_limit(store, monkeypatch):
    service, tenant = _admission_service(store, monkeypatch)
    policy = {"day": "2026-07-30", "limit": 1, "tier": "hosted_pro"}
    monkeypatch.setattr(
        customization_service.entitlements, "resolve_tier",
        lambda _tenant: policy["tier"],
    )
    monkeypatch.setattr(
        author_quota, "usage_policy",
        lambda: SimpleNamespace(
            daily_author_quota=lambda: policy["limit"],
            author_quota_day=lambda _now=None: policy["day"],
        ),
    )
    real_enforce = author_quota.enforce
    crashed = {"done": False}

    def charge_then_crash(*args, **kwargs):
        result = real_enforce(*args, **kwargs)
        if not crashed["done"]:
            crashed["done"] = True
            raise RuntimeError("crash after durable quota decision")
        return result

    monkeypatch.setattr(author_quota, "enforce", charge_then_crash)
    with pytest.raises(RuntimeError):
        service.enqueue_stage(
            tenant=tenant, description=DESCRIPTION, mode="build",
            idempotency_key="request-rollover",
        )

    policy.update(day="2026-07-31", limit=0, tier="demo")
    result = service.enqueue_stage(
        tenant=tenant, description=DESCRIPTION, mode="build",
        idempotency_key="request-rollover",
    )
    assert result["status"] == "queued"
    assert author_quota._MEMORY_STATE == {"2026-07-30:tenant-a": 1}


def test_claim_is_fenced_and_expired_claim_is_recoverable(store, monkeypatch):
    queued(store)
    clock = [1000.0]
    monkeypatch.setattr(customization_store.time, "time", lambda: clock[0])
    first = store.claim_stage(owner="worker-a", lease_seconds=10)
    assert first and first.stage_attempt == 1
    assert store.claim_stage(owner="worker-b", lease_seconds=10) is None
    assert not store.heartbeat_stage(
        tenant_id="tenant-a", change_set_id=first.change_set_id,
        owner="worker-b", lease_seconds=10,
    )
    clock[0] += 11
    recovered = store.claim_stage(owner="worker-b", lease_seconds=10)
    assert recovered and recovered.stage_attempt == 2
    assert not store.defer_stage_claim(
        tenant_id="tenant-a", change_set_id=first.change_set_id,
        owner="worker-a", reason_code="stale", delay_seconds=1,
    )


def test_async_worker_never_claims_bound_removal_before_or_after_lease_time(
    store, monkeypatch,
):
    removal = queued(store, key="remove-bound")
    bind_removal_marker(store, removal)
    clock = [1000.0]
    monkeypatch.setattr(customization_store.time, "time", lambda: clock[0])

    assert store.claim_stage(owner="worker-a", lease_seconds=10) is None
    clock[0] += 11
    assert store.claim_stage(owner="worker-b", lease_seconds=10) is None
    durable = store.get_change_set(
        tenant_id=removal.tenant_id, change_set_id=removal.change_set_id
    )
    assert durable.state is ChangeState.STAGING
    assert durable.stage_attempt == 0


def test_unrelated_removal_bindings_do_not_hide_ordinary_stage_work(store):
    for tenant in ("tenant-removal-a", "tenant-removal-b"):
        removal = queued(store, tenant=tenant, key=f"{tenant}-remove")
        bind_removal_marker(store, removal)
    ordinary = queued(store, tenant="tenant-ordinary", key="ordinary-stage")

    claimed = store.claim_stage(owner="worker-a", lease_seconds=10)

    assert claimed and claimed.change_set_id == ordinary.change_set_id


def test_failed_bound_removal_is_not_resumed_by_async_worker(store):
    removal = queued(store, key="remove-failed")
    bind_removal_marker(store, removal)
    with store._transaction() as conn:
        conn.execute(
            "UPDATE customization_change_sets SET state = ? "
            "WHERE tenant_id = ? AND change_set_id = ?",
            (
                ChangeState.FAILED.value, removal.tenant_id,
                removal.change_set_id,
            ),
        )

    assert store.claim_stage(owner="worker-a", lease_seconds=10) is None


def test_synchronous_removal_records_staged_without_async_worker_claim(
    store, monkeypatch,
):
    seed_effective_catalog(store)
    service = CustomizationService(store)
    monkeypatch.setattr(customization_service, "enabled", lambda *_args: True)
    monkeypatch.setattr(
        customization_service, "_binding",
        lambda _tenant: SimpleNamespace(subject="auth0|author", role="owner"),
    )
    monkeypatch.setattr(service, "_verify_bound_stage_policy", lambda *_args: None)

    def harness_remove(change, _expected_catalog_digest):
        assert store.claim_stage(owner="worker-a", lease_seconds=10) is None
        return {
            "receipt": {
                "contract": customization_service.CONTRACT,
                "tenant_id": change.tenant_id,
                "change_set_id": change.change_set_id,
                "state": "staged",
                "base_commit": change.base_commit,
                "staged_commit": "e" * 40,
                "catalog_digest": "f" * 64,
                "platform_release": change.desired_platform_release,
                "workspace_contract_digest": change.workspace_contract_digest,
                "idempotency_key": change.idempotency_key,
            }
        }

    monkeypatch.setattr(service, "_harness_remove", harness_remove)
    result = service.stage_removal(
        tenant="tenant-a", tool_name="count-by-layer",
        expected_catalog_digest=DIGEST, idempotency_key="remove-sync",
    )

    assert result["receipt"]["state"] == "staged"
    durable = store.get_change_set(
        tenant_id="tenant-a",
        change_set_id=result["receipt"]["change_set_id"],
    )
    assert durable.state is ChangeState.STAGED
    assert durable.stage_attempt == 0


def test_stale_worker_success_cannot_overwrite_reclaimed_generation(store, monkeypatch):
    change = queued(store)
    clock = [1000.0]
    monkeypatch.setattr(customization_store.time, "time", lambda: clock[0])
    first = store.claim_stage(owner="worker-a", lease_seconds=10)
    clock[0] += 11
    second = store.claim_stage(owner="worker-b", lease_seconds=10)
    assert first and second and second.stage_attempt == first.stage_attempt + 1
    with pytest.raises(ChangeSetConflictError):
        store.record_staged(
            tenant_id="tenant-a", change_set_id=change.change_set_id,
            expected_version=change.version, idempotency_key="stale-success",
            staged_commit=STAGED, catalog_digest=DIGEST,
            platform_release="release-a", workspace_contract_digest=WORKSPACE,
            stage_lease_owner="worker-a", stage_attempt=first.stage_attempt,
        )
    durable = store.get_change_set(
        tenant_id="tenant-a", change_set_id=change.change_set_id
    )
    assert durable.state is ChangeState.STAGING
    assert durable.stage_lease_owner == "worker-b"
    # The authenticated harness callback is still authoritative and may win.
    staged = store.record_staged(
        tenant_id="tenant-a", change_set_id=change.change_set_id,
        expected_version=change.version, idempotency_key="callback-success",
        staged_commit=STAGED, catalog_digest=DIGEST,
        platform_release="release-a", workspace_contract_digest=WORKSPACE,
    )
    assert staged.state is ChangeState.STAGED
    assert staged.stage_lease_owner is None


def test_callback_completion_wins_and_clears_worker_lease(store):
    change = queued(store)
    claimed = store.claim_stage(owner="worker-a", lease_seconds=30)
    assert claimed and claimed.change_set_id == change.change_set_id
    staged = store.record_staged(
        tenant_id="tenant-a", change_set_id=change.change_set_id,
        expected_version=change.version, idempotency_key="staged:request-a",
        staged_commit=STAGED, catalog_digest=DIGEST,
        platform_release="release-a", workspace_contract_digest=WORKSPACE,
    )
    assert staged.state is ChangeState.STAGED
    assert staged.stage_lease_owner is None
    assert staged.stage_phase == "staged"
    assert not store.fail_stage_claim(
        tenant_id="tenant-a", change_set_id=change.change_set_id,
        owner="worker-a", reason_code="late", retryable=False,
    )

    service = CustomizationService(store)
    service._staged_tool = lambda _change: {
        "name": "new-tool", "capabilities": ["drawing.write"]
    }
    status = service.stage_status(
        tenant="tenant-a", change_set_id=change.change_set_id
    )
    assert status["status"] == "staged"
    assert status["receipt"]["staged_commit"] == STAGED
    assert status["result"]["tool"]["name"] == "new-tool"


def test_public_stage_route_returns_202_without_calling_synchronous_stage(
    monkeypatch, active_author_turn
):
    called = threading.Event()
    admitted = []

    def hung_stage(**_kwargs):
        called.set()
        time.sleep(5)

    service = SimpleNamespace(
        enqueue_stage=lambda **kwargs: (
            admitted.append(kwargs),
            {
                "contract": "leaf.customization-stage-job.v1",
                "change_set_id": "11111111-1111-4111-8111-111111111111",
                "status": "queued",
            },
        )[1],
        stage=hung_stage,
    )
    monkeypatch.setattr(author_router, "_customization_gate", lambda *_: None)
    monkeypatch.setattr(
        author_router.CustomizationService, "configured",
        classmethod(lambda cls: service),
    )
    started = time.monotonic()
    response = author_router.stage(
        author_router.StageRequest(
            description=DESCRIPTION, mode="build", idempotency_key="request-a"
        ),
        tenant=deps.TenantContext(
            "tenant-a", tier="hosted_pro", subject=ALICE
        ),
        authority_session_id=active_author_turn[0],
        authority_turn_id=active_author_turn[1],
    )
    assert response.status_code == 202
    assert time.monotonic() - started < 0.5
    assert not called.is_set()
    assert admitted[0]["tenant"].subject == ALICE
    assert admitted[0]["authority_session_id"] == active_author_turn[0]
    assert admitted[0]["authority_turn_id"] == active_author_turn[1]


def test_public_stage_route_rejects_partial_authority_tuple(monkeypatch):
    called = False

    def enqueue_stage(**_kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(author_router.deps, "auth_live", lambda: True)
    monkeypatch.setattr(author_router, "customization_enabled", lambda *_: True)
    monkeypatch.setattr(
        author_router.CustomizationService, "configured",
        classmethod(lambda cls: SimpleNamespace(enqueue_stage=enqueue_stage)),
    )
    response = author_router.stage(
        author_router.StageRequest(
            description=DESCRIPTION, mode="build", idempotency_key="request-a"
        ),
        tenant="tenant-a",
        authority_session_id="session-a",
        authority_turn_id=None,
    )
    assert response.status_code == 422
    assert response.body
    assert not called


@pytest.mark.parametrize("route_name", ["author", "stage"])
def test_author_routes_reject_same_tenant_cross_subject_turn(
    monkeypatch, active_author_turn, route_name
):
    dispatched = []
    service = SimpleNamespace(
        enqueue_stage=lambda **kwargs: dispatched.append(kwargs) or {},
        stage=lambda **kwargs: dispatched.append(kwargs) or {},
    )
    monkeypatch.setattr(author_router.deps, "auth_live", lambda: True)
    monkeypatch.setattr(author_router, "customization_enabled", lambda *_: True)
    monkeypatch.setattr(author_router, "_customization_gate", lambda *_: None)
    monkeypatch.setattr(
        author_router.CustomizationService,
        "configured",
        classmethod(lambda cls: service),
    )
    tenant = deps.TenantContext(
        "tenant-a", tier="hosted_pro", subject=MALLORY
    )
    if route_name == "author":
        response = author_router.author(
            author_router.AuthorRequest(description=DESCRIPTION, mode="build"),
            tenant=tenant,
            idempotency_key="request-a",
            authority_session_id=active_author_turn[0],
            authority_turn_id=active_author_turn[1],
        )
    else:
        response = author_router.stage(
            author_router.StageRequest(
                description=DESCRIPTION, mode="build", idempotency_key="request-a"
            ),
            tenant=tenant,
            authority_session_id=active_author_turn[0],
            authority_turn_id=active_author_turn[1],
        )
    assert response.status_code == 409
    assert b"stage_authority_invalid" in response.body
    assert dispatched == []


@pytest.mark.parametrize("route_name", ["author", "stage"])
def test_direct_authenticated_author_cannot_bypass_missing_turn_authority(
    monkeypatch, route_name
):
    dispatched = []
    monkeypatch.setattr(author_router.deps, "auth_live", lambda: True)
    monkeypatch.setattr(author_router, "customization_enabled", lambda *_: True)
    monkeypatch.setattr(author_router, "_customization_gate", lambda *_: None)
    monkeypatch.setattr(
        author_router.CustomizationService,
        "configured",
        classmethod(
            lambda cls: SimpleNamespace(
                enqueue_stage=lambda **kwargs: dispatched.append(kwargs) or {}
            )
        ),
    )
    tenant = deps.TenantContext("tenant-a", tier="hosted_pro", subject=ALICE)
    if route_name == "author":
        response = author_router.author(
            author_router.AuthorRequest(description=DESCRIPTION, mode="build"),
            tenant=tenant,
            idempotency_key="request-a",
        )
    else:
        response = author_router.stage(
            author_router.StageRequest(
                description=DESCRIPTION, mode="build", idempotency_key="request-a"
            ),
            tenant=tenant,
        )
    assert response.status_code == 409
    assert dispatched == []


@pytest.mark.parametrize("route_name", ["author", "stage"])
@pytest.mark.parametrize("tuple_state,expected_detail", [
    ("missing", "missing_authority_tuple"),
    ("rejected", "authority_tuple_rejected"),
])
def test_stage_authority_refusal_names_its_branch_in_the_log_only(
    monkeypatch, caplog, route_name, tuple_state, expected_detail
):
    # The 2026-08-18 staging acceptance failure logged `detail=-`, which
    # distinguished nothing: a client that never sent the authority headers
    # reads identically to a live turn the resolver rejected. The refusal must
    # name its branch in the OPERATOR LOG, and only there — the §10 response
    # keeps the opaque reason code, so the split adds no probing oracle.
    monkeypatch.setattr(author_router.deps, "auth_live", lambda: True)
    monkeypatch.setattr(author_router, "customization_enabled", lambda *_: True)
    monkeypatch.setattr(author_router, "_customization_gate", lambda *_: None)
    headers = {}
    if tuple_state == "rejected":
        # A full tuple the resolver rejects (turn missing, stale, superseded,
        # subject mismatch, or an unresolved binding — all one branch here).
        monkeypatch.setattr(
            author_router.deps, "stage_author_identity", lambda *_: None
        )
        headers = {
            "authority_session_id": "session-a",
            "authority_turn_id": "turn-a",
        }
    tenant = deps.TenantContext("tenant-a", tier="hosted_pro", subject=ALICE)
    with caplog.at_level(logging.DEBUG):
        if route_name == "author":
            response = author_router.author(
                author_router.AuthorRequest(description=DESCRIPTION, mode="build"),
                tenant=tenant,
                idempotency_key="request-a",
                **headers,
            )
        else:
            response = author_router.stage(
                author_router.StageRequest(
                    description=DESCRIPTION, mode="build",
                    idempotency_key="request-a",
                ),
                tenant=tenant,
                **headers,
            )
    assert response.status_code == 409
    refusals = [
        r.getMessage() for r in caplog.records
        if "stage_authority_invalid" in r.getMessage()
    ]
    assert refusals, "the refusal must be logged"
    assert f"detail={expected_detail}" in refusals[0]
    # Log-only by contract: the branch name must never reach the tenant.
    assert expected_detail not in bytes(response.body).decode()


@pytest.mark.parametrize("active_subject", [MALLORY, None])
def test_stage_worker_rejects_subject_swap_or_stale_turn_before_harness(
    store, monkeypatch, active_subject
):
    change, created = store.reserve_stage(
        tenant_id="tenant-a",
        idempotency_key="stage-authority-race",
        base_commit=BASE,
        desired_platform_release="release-a",
        workspace_contract_digest=WORKSPACE,
        author_subject=ALICE,
        request_description=DESCRIPTION,
        request_fingerprint=FINGERPRINT,
        authority_session_id="session-a",
        authority_turn_id="turn-a",
    )
    assert created
    observed = []

    def resolve(tenant_id, session_id, turn_id):
        observed.append((tenant_id, session_id, turn_id))
        return active_subject

    monkeypatch.setattr(deps, "active_stage_author_subject", resolve)
    monkeypatch.setattr(
        customization_service,
        "_harness_config",
        lambda: (_ for _ in ()).throw(AssertionError("harness config reached")),
    )
    with pytest.raises(CustomizationServiceError) as caught:
        CustomizationService(store)._harness_stage(
            "tenant-a", DESCRIPTION, change
        )
    assert caught.value.code == "stage_authority_invalid"
    assert observed == [("tenant-a", "session-a", "turn-a")]


def test_stage_worker_rejects_tenant_substitution_before_harness(
    store, monkeypatch
):
    change, created = store.reserve_stage(
        tenant_id="tenant-a",
        idempotency_key="stage-authority-tenant-race",
        base_commit=BASE,
        desired_platform_release="release-a",
        workspace_contract_digest=WORKSPACE,
        author_subject=ALICE,
        request_description=DESCRIPTION,
        request_fingerprint=FINGERPRINT,
        authority_session_id="session-a",
        authority_turn_id="turn-a",
    )
    assert created
    monkeypatch.setattr(
        deps, "active_stage_author_subject", lambda *_args: ALICE
    )
    monkeypatch.setattr(
        customization_service,
        "_harness_config",
        lambda: (_ for _ in ()).throw(AssertionError("harness config reached")),
    )
    with pytest.raises(CustomizationServiceError) as caught:
        CustomizationService(store)._harness_stage(
            "tenant-b", DESCRIPTION, change
        )
    assert caught.value.code == "stage_authority_invalid"


def test_stage_authority_lookup_failure_returns_no_authority(monkeypatch):
    monkeypatch.setattr(
        session_store,
        "active_turn_subject",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("down")),
    )
    assert deps.active_stage_author_subject(
        "tenant-a", "session-a", "turn-a"
    ) is None


def test_stage_status_is_tenant_scoped_and_omits_private_request(store):
    change = queued(store)
    service = CustomizationService(store)
    body = service.stage_status(tenant="tenant-a", change_set_id=change.change_set_id)
    assert body["status"] == "queued"
    assert DESCRIPTION not in json.dumps(body)
    with pytest.raises(ChangeSetNotFoundError):
        service.stage_status(tenant="tenant-b", change_set_id=change.change_set_id)


def test_publication_reports_staging_without_creating_approval(store, monkeypatch):
    change = queued(store)
    service = CustomizationService(store)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setattr(customization_service, "enabled", lambda *_args: True)
    tenant = deps.TenantContext("tenant-a", tier="hosted_pro", subject=None)
    assert service.request_publication(
        tenant=tenant, change_set_id=change.change_set_id
    ) == {
        "contract": "leaf.customization.v1",
        "change_set_id": change.change_set_id,
        "status": "staging",
    }
    with store._connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM customization_publication_requests"
        ).fetchone()[0]
    assert count == 0


def test_worker_reconciles_durable_callback_and_restart_claim(store):
    change = queued(store)
    service = CustomizationService(store)

    def reconcile(claimed, _body, **_lease):
        store.record_staged(
            tenant_id=claimed.tenant_id,
            change_set_id=claimed.change_set_id,
            expected_version=claimed.version,
            idempotency_key=f"staged:{claimed.idempotency_key}",
            staged_commit=STAGED,
            catalog_digest=DIGEST,
            platform_release="release-a",
            workspace_contract_digest=WORKSPACE,
        )
        return {"receipt": {"state": "staged"}}

    service.dispatch_stage = lambda _claimed: {"receipt": {"state": "staged"}}
    service.reconcile_stage_worker = reconcile
    assert customization_stage_worker.run_once(
        "worker-a", service=service, lease_seconds=1
    )
    durable = store.get_change_set(
        tenant_id="tenant-a", change_set_id=change.change_set_id
    )
    assert durable.state is ChangeState.STAGED
    assert not customization_stage_worker.run_once(
        "worker-b", service=service, lease_seconds=1
    )


def test_ambiguous_transport_failure_waits_past_harness_lease(store):
    change = queued(store)
    service = CustomizationService(store)
    service.dispatch_stage = lambda _change: (_ for _ in ()).throw(
        CustomizationServiceError("customization_harness_unavailable", 503)
    )
    before_ms = int(time.time() * 1000)
    assert customization_stage_worker.run_once(
        "worker-a", service=service, lease_seconds=1
    )
    deferred = store.get_change_set(
        tenant_id="tenant-a", change_set_id=change.change_set_id
    )
    assert deferred.state is ChangeState.STAGING
    assert deferred.stage_next_attempt_at >= before_ms + 120_000
    assert store.claim_stage(owner="worker-b", lease_seconds=1) is None


class _AnsweredWithError(Exception):
    """Transport-layer stand-in: the harness answered an HTTP error."""

    def __init__(self, response):
        super().__init__("harness answered with an error status")
        self.response = response


class _HarnessErrorResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def raise_for_status(self):
        raise _AnsweredWithError(self)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _authorized_stage_change(store, monkeypatch, *, key="job-failure"):
    change, created = store.reserve_stage(
        tenant_id="tenant-a",
        idempotency_key=key,
        base_commit=BASE,
        desired_platform_release="release-a",
        workspace_contract_digest=WORKSPACE,
        author_subject=ALICE,
        request_description=DESCRIPTION,
        request_fingerprint=FINGERPRINT,
        authority_session_id="session-a",
        authority_turn_id="turn-a",
    )
    assert created
    monkeypatch.setattr(deps, "active_stage_author_subject", lambda *_args: ALICE)
    monkeypatch.setattr(
        customization_service, "_harness_config",
        lambda: ("http://harness.invalid", "secret"),
    )
    return change


def test_harness_answered_job_failure_is_terminal_and_carries_reason(
    store, monkeypatch
):
    """The incident class: the harness ran the job and reported WHY it died."""
    change = _authorized_stage_change(store, monkeypatch)
    response = _HarnessErrorResponse(500, {
        "error": {"message": "Agent SDK auth failure: oauth_org_not_allowed"},
    })
    monkeypatch.setattr(
        "requests.post", lambda *_args, **_kwargs: response
    )
    with pytest.raises(CustomizationServiceError) as caught:
        CustomizationService(store)._harness_stage(
            "tenant-a", DESCRIPTION, change
        )
    assert caught.value.code == "customization_author_job_failed"
    assert caught.value.status_code == 502
    assert caught.value.harness_reason == (
        "Agent SDK auth failure: oauth_org_not_allowed"
    )


def test_harness_transport_failure_without_reason_stays_unavailable(
    store, monkeypatch
):
    change = _authorized_stage_change(store, monkeypatch, key="transport-failure")
    response = _HarnessErrorResponse(502, ValueError("not json"))
    monkeypatch.setattr(
        "requests.post", lambda *_args, **_kwargs: response
    )
    with pytest.raises(CustomizationServiceError) as caught:
        CustomizationService(store)._harness_stage(
            "tenant-a", DESCRIPTION, change
        )
    assert caught.value.code == "customization_harness_unavailable"
    assert not hasattr(caught.value, "harness_reason")


@pytest.mark.parametrize("body,expected", [
    # Pinned Agent SDK terminal failures: answered, and surfaced verbatim
    # (the incident class).
    ({"error": {"message": "Agent SDK auth failure: oauth_org_not_allowed"}},
     (True, "Agent SDK auth failure: oauth_org_not_allowed")),
    ({"error": {"message": "Agent SDK auth failure: billing_error"}},
     (True, "Agent SDK auth failure: billing_error")),
    ({"error": {"message": "Agent SDK rate limited (retry after ~42s)"}},
     (True, "Agent SDK rate limited (retry after ~42s)")),
    ({"error": {"message": "Agent SDK rate limited (retry horizon unknown)"}},
     (True, "Agent SDK rate limited (retry horizon unknown)")),
    ({"error": {"message":
      "Agent SDK spend cap exceeded (turns=9 > 8 or cost-tokens=100 > 50)"}},
     (True, "Agent SDK spend cap exceeded (turns=9 > 8 or cost-tokens=100 > 50)")),
    # Shape-marked deliberate refusals: answered, surfaced verbatim.
    ({"grant_required": True,
      "error": {"message": "tenant t has no eligible Claude grant.",
                "code": "grant_required"}},
     (True, "tenant t has no eligible Claude grant.")),
    ({"errorCode": "llm_quota_exhausted",
      "message": "all authorized Claude mounts are temporarily unavailable"},
     (True, "all authorized Claude mounts are temporarily unavailable")),
    # An answered failure with an unpinned message is still TERMINAL — but
    # its message stays reason_code-only: an arbitrary catch-all message can
    # carry a credential fragment, internal URL, or path (sol-critic, PR #553).
    ({"error": {"message": "ENOENT /srv/tenants/t/.git x-oauth-basic@internal"}},
     (True, None)),
    ({"error": {"message": "Agent SDK auth failure: something_else"}},
     (True, None)),
    ({"error": {"message": "Agent SDK auth failure: oauth_org_not_allowed "
                           "plus trailing junk"}}, (True, None)),
    ({"error": {"message": "line one\nline\ttwo"}}, (True, None)),
    ({"error": {"message": "x" * 400}}, (True, None)),
    ({"error": {"message": "   "}}, (True, None)),
    # No usable string message at all: not recognizably a harness answer, so
    # the transport lane (defer/retry) keeps ownership.
    ({"error": {"message": 7}}, (False, None)),
    ({"error": "not a dict"}, (False, None)),
    (["not", "a", "dict"], (False, None)),
    (ValueError("unparseable body"), (False, None)),
])
def test_harness_job_failure_classification(body, expected):
    response = _HarnessErrorResponse(500, body)
    assert CustomizationService._harness_job_failure(response) == expected


def test_multiline_grant_message_is_collapsed_before_surfacing():
    """Sanitization still applies to shape-marked messages."""
    response = _HarnessErrorResponse(401, {
        "grant_required": True,
        "error": {"message": "no linked\nClaude grant", "code": "grant_required"},
    })
    assert CustomizationService._harness_job_failure(response) == (
        True, "no linked Claude grant"
    )


def test_unallowlisted_answered_failure_is_still_terminal_without_a_message(
    store, monkeypatch
):
    """Detection is separate from surfacing (sol-critic round 2): an answered
    catch-all failure whose message is not pinned safe must STILL fail on
    attempt 1 as customization_author_job_failed — with no verbatim reason."""
    change = _authorized_stage_change(store, monkeypatch, key="unsafe-reason")
    response = _HarnessErrorResponse(500, {
        "error": {"message": "ENOENT /srv/tenants/t/.git x-oauth-basic@internal"},
    })
    monkeypatch.setattr("requests.post", lambda *_args, **_kwargs: response)
    with pytest.raises(CustomizationServiceError) as caught:
        CustomizationService(store)._harness_stage(
            "tenant-a", DESCRIPTION, change
        )
    assert caught.value.code == "customization_author_job_failed"
    assert caught.value.harness_reason is None


def test_author_job_failure_fails_first_attempt_and_status_carries_reason(
    store,
):
    """One failed dispatch = terminal FAILED with the harness reason readable
    from stage status — no 3x135s retry window hiding the truth."""
    change = queued(store)
    service = CustomizationService(store)
    error = CustomizationServiceError("customization_author_job_failed", 502)
    error.harness_reason = "Agent SDK auth failure: oauth_org_not_allowed"
    service.dispatch_stage = lambda _change: (_ for _ in ()).throw(error)
    assert customization_stage_worker.run_once(
        "worker-a", service=service, lease_seconds=1
    )
    durable = store.get_change_set(
        tenant_id="tenant-a", change_set_id=change.change_set_id
    )
    assert durable.state is ChangeState.FAILED
    assert durable.stage_attempt == 1
    assert durable.stage_error_code == "customization_author_job_failed"
    assert durable.stage_error_message == (
        "Agent SDK auth failure: oauth_org_not_allowed"
    )
    status = service.stage_status(
        tenant="tenant-a", change_set_id=change.change_set_id
    )
    assert status["status"] == "failed"
    assert status["phase"] == "failed"
    assert status["error"] == {
        "reason_code": "customization_author_job_failed",
        "retryable": True,
        "message": "Agent SDK auth failure: oauth_org_not_allowed",
    }


def test_transport_exhaustion_fails_without_inventing_a_message(
    store, monkeypatch
):
    """The unreachable-harness lane keeps its defer/retry shape, and its
    terminal status stays additive: no message key when none was captured."""
    change = queued(store)
    service = CustomizationService(store)
    service.dispatch_stage = lambda _change: (_ for _ in ()).throw(
        CustomizationServiceError("customization_harness_unavailable", 503)
    )
    clock = [1000.0]
    monkeypatch.setattr(customization_store.time, "time", lambda: clock[0])
    for _attempt in range(3):
        assert customization_stage_worker.run_once(
            "worker-a", service=service, lease_seconds=1
        )
        clock[0] += customization_stage_worker.DEFAULT_RETRY_SECONDS + 1
    durable = store.get_change_set(
        tenant_id="tenant-a", change_set_id=change.change_set_id
    )
    assert durable.state is ChangeState.FAILED
    assert durable.stage_error_code == "customization_harness_unavailable"
    assert durable.stage_error_message is None
    status = service.stage_status(
        tenant="tenant-a", change_set_id=change.change_set_id
    )
    assert status["error"] == {
        "reason_code": "customization_harness_unavailable",
        "retryable": True,
    }


def test_worker_does_not_reconcile_success_after_guard_loss(store, monkeypatch):
    change = queued(store)
    service = CustomizationService(store)
    calls = []
    service.dispatch_stage = lambda _change: {"receipt": {"state": "staged"}}
    service.reconcile_stage_worker = lambda *_args, **_kwargs: calls.append("staged")

    class LostGuard:
        def __init__(self, *_args, **_kwargs):
            self.lost = SimpleNamespace(is_set=lambda: True)
        def start(self):
            pass
        def close(self):
            pass

    monkeypatch.setattr(customization_stage_worker, "StageLeaseGuard", LostGuard)
    assert customization_stage_worker.run_once(
        "worker-a", service=service, lease_seconds=1
    )
    assert calls == []
    durable = store.get_change_set(
        tenant_id="tenant-a", change_set_id=change.change_set_id
    )
    assert durable.state is ChangeState.STAGING


# AG2a uses its own fixture: the admission, raw policy, digest and metadata
# derivation remain real, including across a reopened SQLite authority.
AG2A_GRAPH = "solar-w1-graph"


def _ag2a_snapshot(rows):
    return json.dumps({
        "schema": "leaf.customization-record-fields.v1",
        "tools": {
            row["name"]: {
                "version": row["version"], "graph_input": AG2A_GRAPH,
                "record_sha256": hashlib.sha256(json.dumps(
                    row, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=True, allow_nan=False,
                ).encode("utf-8")).hexdigest(),
            } for row in rows
        },
    }, sort_keys=True, separators=(",", ":"))


class _AG2aCatalog:
    def __init__(self, tmp_path, store):
        self.store = store
        self.service = CustomizationService(store)
        self.work = tmp_path / "work"
        self.bare = tmp_path / "tenant-a.git"
        self.work.mkdir()
        self.git("init", "--initial-branch=main")
        self.git("config", "user.name", "AG2a")
        self.git("config", "user.email", "ag2a@example.invalid")
        self.git("config", "core.autocrlf", "false")
        (self.work / "registry.json").write_text('{"tools":[]}', encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.git("clone", "--bare", str(self.work), str(self.bare))
        self.base = self.git("rev-parse", "HEAD")
        self.charges = []
        self.calls = []
        self.serial = 0
        self.row_options = {}
        self.service._harness_stage = self.harness
        self.tenant = deps.TenantContext("tenant-a", tier="hosted_pro", subject=ALICE)

    def git(self, *args):
        import subprocess
        return subprocess.run(
            ["git", "-C", str(self.work), *args], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ).stdout.strip()

    def request(self, *, key="ag2a", graph=AG2A_GRAPH, enqueue=False, target=None):
        entry = self.service.enqueue_stage if enqueue else self.service.stage
        return entry(
            tenant=self.tenant, description=DESCRIPTION, mode="build",
            idempotency_key=key, graph_input=graph, target_tool_name=target,
        )

    def change(self, key="ag2a"):
        return self.store.get_change_set_by_idempotency(
            tenant_id="tenant-a", idempotency_key=key,
        )

    def harness(self, _tenant, _description, change):
        self.calls.append(change.change_set_id)
        return self.body(change, **self.row_options)

    def body(self, change, *, name="ag2a-graph", capabilities=None, raw_graph=None, remove=None):
        self.serial += 1
        self.git("checkout", "-B", "author", change.base_commit)
        rows = json.loads((self.work / "registry.json").read_text(encoding="utf-8"))["tools"]
        if remove:
            rows = [row for row in rows if row["name"] != remove]
            row = None
        elif change.target_tool_name:
            row = next(row for row in rows if row["name"] == change.target_tool_name)
            parts = row["version"].split(".")
            row["version"] = ".".join([*parts[:2], str(int(parts[2]) + 1)])
        else:
            row = {
                "name": name, "version": "1.0.0", "kind": "python",
                "entry": f"tools/{name}/run.py",
                "capabilities": capabilities if capabilities is not None else ["drawing.read"],
                "params": {}, "returns": {}, "description": "Graph report \u2600",
            }
            rows.append(row)
        if row is not None:
            if raw_graph is not None:
                row["graph_input"] = raw_graph
            entry = self.work / row["entry"]
            entry.parent.mkdir(parents=True, exist_ok=True)
            entry.write_text(f"# version {row['version']} attempt {self.serial}\n", encoding="utf-8")
            (entry.parent / "tool.json").write_text(json.dumps(row), encoding="utf-8")
        raw = json.dumps({"tools": rows}, ensure_ascii=False).encode("utf-8")
        (self.work / "registry.json").write_bytes(raw)
        self.git("add", ".")
        self.git("commit", "-m", f"author {self.serial}")
        commit = self.git("rev-parse", "HEAD")
        customization_service._git(self.bare, "fetch", str(self.work), f"HEAD:refs/ag2a/{self.serial}")
        receipt = {
            "contract": "leaf.customization.v1", "tenant_id": change.tenant_id,
            "change_set_id": change.change_set_id, "state": "staged",
            "base_commit": change.base_commit, "staged_commit": commit,
            "catalog_digest": hashlib.sha256(raw).hexdigest(),
            "platform_release": change.desired_platform_release,
            "workspace_contract_digest": change.workspace_contract_digest,
            "idempotency_key": change.idempotency_key,
        }
        return {"receipt": receipt, **({"tool": dict(row)} if row else {})}

    def predecessor(self):
        result = self.request(key="predecessor")
        change = self.change("predecessor")
        with self.store._transaction() as conn:
            conn.execute(
                "UPDATE customization_change_sets SET state = ? WHERE change_set_id = ?",
                (ChangeState.PUBLISHED.value, change.change_set_id),
            )
            conn.execute(
                "INSERT INTO effective_catalogs (tenant_id, change_set_id, catalog_commit, "
                "catalog_digest, effective_platform_release, workspace_contract_digest) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (change.tenant_id, change.change_set_id, change.staged_commit,
                 change.catalog_digest, change.desired_platform_release, change.workspace_contract_digest),
            )
        customization_service._git(self.bare, "update-ref", "refs/heads/main", change.staged_commit)
        self.charges.clear()
        self.calls.clear()
        return self.change("predecessor"), result["tool"]


@pytest.fixture
def ag2a(tmp_path, store, monkeypatch):
    catalog = _AG2aCatalog(tmp_path, store)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "version": 1, "workspace_contracts": [{"id": "workspace-a", "sha256": WORKSPACE}],
        "releases": [{
            "release_id": "release-a", "workspace_contract": "workspace-a",
            "workspace_contract_sha256": WORKSPACE,
            "rules": [{"path": "registry.json", "mutability": "frozen"},
                      {"path": "tools/**", "mutability": "tenant_owned"}],
        }],
    }), encoding="utf-8")
    monkeypatch.setenv("LEAF_PLATFORM_RELEASE_POLICY_FILE", str(policy))
    monkeypatch.setenv("LEAF_PLATFORM_RELEASE", "release-a")
    monkeypatch.setenv("LEAF_TENANT_GIT_DIR", str(tmp_path))
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_CONFIRMATION_SECRET", "ag2a-secret")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_CONFIRMATION_KEY", "")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_ALLOW_STATIC_BINDINGS", "1")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_TENANT_BINDINGS", json.dumps({
        "tenant-a": {"subject": ALICE, "role": "owner"},
    }))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("LEAF_TENANT_MCP_BROKER_URL", "")
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.invalid")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "secret")
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    monkeypatch.setattr(customization_service.entitlements, "resolve_tier", lambda _tenant: "hosted_pro")
    monkeypatch.setattr(customization_service.entitlements, "entitlements_for", lambda *_args: {"build": True})
    charged_ids = set()
    def charge(_tenant, _tier, **kwargs):
        key = kwargs.get("idempotency_key")
        if key is None or key not in charged_ids:
            catalog.charges.append(key)
        if key is not None:
            charged_ids.add(key)
    monkeypatch.setattr(author_quota, "enforce", charge)
    return catalog


def _ag2a_refusal(code, status):
    return pytest.raises(CustomizationServiceError, match=f"^{code}$")


def test_ag2a_stage_admits_graph(ag2a):
    result = ag2a.request()
    change = ag2a.change()
    raw = dict(result["tool"])
    raw.pop("graph_input")
    assert change.state is ChangeState.STAGED
    assert change.request_graph_input == AG2A_GRAPH
    assert change.catalog_record_fields_json == _ag2a_snapshot([raw])
    assert result["tool"]["graph_input"] == AG2A_GRAPH
    assert len(ag2a.charges) == len(ag2a.calls) == 1


def test_ag2a_enqueue_stage_admits_graph(ag2a):
    result = ag2a.request(enqueue=True)
    change = ag2a.change()
    assert result["status"] == "queued"
    assert change.request_graph_input == AG2A_GRAPH
    assert change.request_description == DESCRIPTION
    assert change.request_fingerprint == FINGERPRINT
    assert change.catalog_record_fields_json is None
    assert ag2a.calls == []


def test_ag2a_exact_queued_replay(ag2a):
    first = ag2a.request(enqueue=True)
    change = ag2a.change()
    second = ag2a.request(enqueue=True)
    assert first == second
    assert ag2a.change() == change
    assert ag2a.charges == [change.change_set_id]
    assert len(ag2a.store.audit_events(tenant_id="tenant-a", change_set_id=change.change_set_id)) == 2


def test_ag2a_exact_completed_replay(ag2a):
    first = ag2a.request()
    change = ag2a.change()
    second = ag2a.request()
    assert first["receipt"] == second["receipt"]
    assert first["tool"] == second["tool"]
    assert ag2a.change() == change
    assert len(ag2a.charges) == len(ag2a.calls) == 1


def test_ag2a_replay_adds_graph(ag2a):
    ag2a.service.enqueue_stage(
        tenant=ag2a.tenant, description=DESCRIPTION, mode="build", idempotency_key="ag2a",
    )
    before = ag2a.change()
    with _ag2a_refusal("idempotency_replay", 409) as caught:
        ag2a.request(enqueue=True)
    assert caught.value.status_code == 409
    assert ag2a.change() == before
    assert len(ag2a.charges) == 1


def test_ag2a_replay_removes_graph(ag2a):
    ag2a.request(enqueue=True)
    before = ag2a.change()
    with _ag2a_refusal("idempotency_replay", 409) as caught:
        ag2a.service.enqueue_stage(
            tenant=ag2a.tenant, description=DESCRIPTION, mode="build", idempotency_key="ag2a",
        )
    assert caught.value.status_code == 409
    assert ag2a.change() == before


def test_ag2a_replay_changes_graph_before_validation(ag2a):
    ag2a.request(enqueue=True)
    before = ag2a.change()
    with _ag2a_refusal("idempotency_replay", 409) as caught:
        ag2a.request(graph="other", enqueue=True)
    assert caught.value.status_code == 409
    assert ag2a.change() == before
    assert len(ag2a.charges) == 1


def test_ag2a_new_invalid_graph(ag2a):
    from tool_record_fields import ToolRecordFieldError
    with pytest.raises(ToolRecordFieldError) as caught:
        ag2a.request(graph="other")
    assert caught.value.field == "graph_input"
    with pytest.raises(ChangeSetNotFoundError):
        ag2a.change()
    assert ag2a.charges == ag2a.calls == []


def test_ag2a_captures_metadata_predecessor(ag2a):
    predecessor, _tool = ag2a.predecessor()
    ag2a.request(graph=None, enqueue=True)
    assert ag2a.change().base_catalog_change_set_id == predecessor.change_set_id
    assert ag2a.change().base_commit == predecessor.staged_commit
    assert ag2a.calls == []


def test_ag2a_predecessor_mismatch(ag2a):
    ag2a.predecessor()
    customization_service._git(ag2a.bare, "update-ref", "refs/heads/main", ag2a.base)
    with _ag2a_refusal("record_fields_predecessor_mismatch", 409) as caught:
        ag2a.request(enqueue=True)
    assert caught.value.status_code == 409
    with pytest.raises(ChangeSetNotFoundError):
        ag2a.change()
    assert ag2a.charges == []


def test_ag2a_metadata_read_failure(ag2a, monkeypatch):
    import sqlite3
    ag2a.predecessor()
    def fail(**_kwargs):
        raise sqlite3.OperationalError("authority read failed")
    monkeypatch.setattr(ag2a.store, "get_effective_catalog", fail)
    with _ag2a_refusal("record_fields_read_failed", 503) as caught:
        ag2a.request(enqueue=True)
    assert caught.value.status_code == 503
    with pytest.raises(ChangeSetNotFoundError):
        ag2a.change()
    assert ag2a.charges == []


def test_ag2a_reservation_write_failure(ag2a):
    with ag2a.store._transaction() as conn:
        conn.execute("CREATE TRIGGER ag2a_reserve_fail BEFORE INSERT ON customization_change_sets "
                     "BEGIN SELECT RAISE(ABORT, 'reservation failure'); END")
    with _ag2a_refusal("record_fields_write_failed", 503) as caught:
        ag2a.request()
    assert caught.value.status_code == 503
    with pytest.raises(ChangeSetNotFoundError):
        ag2a.change()
    assert ag2a.charges == ag2a.calls == []


def test_ag2a_unrelated_addition_inherits_snapshot(ag2a):
    predecessor, _tool = ag2a.predecessor()
    ag2a.row_options = {"name": "ag2a-ordinary"}
    result = ag2a.request(graph=None)
    assert "graph_input" not in result["tool"]
    assert ag2a.change().catalog_record_fields_json == predecessor.catalog_record_fields_json


def test_ag2a_revision_inherits_and_rebinds_graph(ag2a):
    predecessor, _tool = ag2a.predecessor()
    result = ag2a.request(graph=None, target="ag2a-graph")
    raw = dict(result["tool"])
    raw.pop("graph_input")
    assert raw["version"] == "1.0.1"
    assert result["tool"]["graph_input"] == AG2A_GRAPH
    assert ag2a.change().catalog_record_fields_json == _ag2a_snapshot([raw])
    assert ag2a.change().catalog_record_fields_json != predecessor.catalog_record_fields_json


def test_ag2a_shared_helper_derives_removal(ag2a):
    from dataclasses import replace
    ag2a.predecessor()
    ag2a.request(graph=None, enqueue=True, target="ag2a-graph")
    change = ag2a.change()
    body = ag2a.body(change, remove="ag2a-graph")
    proposed = replace(change, staged_commit=body["receipt"]["staged_commit"],
                       catalog_digest=body["receipt"]["catalog_digest"])
    assert ag2a.service._derive_catalog_record_fields(
        proposed, removal_target="ag2a-graph"
    ) == _ag2a_snapshot([])
    assert ag2a.change().catalog_record_fields_json is None


def test_ag2a_unreserved_harness_graph(ag2a):
    ag2a.row_options = {"raw_graph": AG2A_GRAPH}
    with _ag2a_refusal("graph_input_unreserved", 422) as caught:
        ag2a.request(graph=None)
    assert caught.value.status_code == 422
    assert ag2a.change().state is ChangeState.STAGING
    assert ag2a.change().catalog_record_fields_json is None
    assert len(ag2a.charges) == len(ag2a.calls) == 1


def test_ag2a_unsupported_graph_consumer(ag2a):
    ag2a.row_options = {"capabilities": ["drawing.write"]}
    with _ag2a_refusal("graph_input_consumer_unsupported", 422) as caught:
        ag2a.request()
    assert caught.value.status_code == 422
    assert ag2a.change().state is ChangeState.STAGING
    assert len(ag2a.charges) == 1


def test_ag2a_restart_before_callback(ag2a):
    ag2a.request(enqueue=True)
    change = ag2a.change()
    body = ag2a.body(change)
    reopened = SQLiteCustomizationStore(ag2a.store.database_path)
    service = CustomizationService(reopened)
    assert service.record_staged_callback(tenant_id="tenant-a", receipt=body["receipt"])["accepted"]
    assert ag2a.change().catalog_record_fields_json == _ag2a_snapshot([body["tool"]])
    assert service.stage_status(tenant=ag2a.tenant, change_set_id=change.change_set_id)["result"]["tool"]["graph_input"] == AG2A_GRAPH
    assert len(ag2a.charges) == 1


def test_ag2a_callback_first(ag2a):
    def callback_first(_tenant, _description, change):
        body = ag2a.body(change)
        ag2a.service.record_staged_callback(tenant_id="tenant-a", receipt=body["receipt"])
        ag2a.callback_winner = ag2a.change()
        return body
    ag2a.service._harness_stage = callback_first
    result = ag2a.request()
    assert ag2a.change() == ag2a.callback_winner
    assert result["tool"]["graph_input"] == AG2A_GRAPH
    assert ag2a.service.stage_status_change(ag2a.change())["result"]["tool"] == result["tool"]
    assert len(ag2a.charges) == 1


def test_ag2a_response_first(ag2a):
    result = ag2a.request()
    before = ag2a.change()
    events = ag2a.store.audit_events(tenant_id="tenant-a")
    assert ag2a.service.record_staged_callback(tenant_id="tenant-a", receipt=result["receipt"])["accepted"]
    assert ag2a.change() == before
    assert ag2a.store.audit_events(tenant_id="tenant-a") == events
    assert len(ag2a.charges) == 1


def test_ag2a_callback_restage_race(ag2a):
    ag2a.request(enqueue=True)
    body = ag2a.body(ag2a.change())
    barrier = threading.Barrier(2)
    def callback():
        barrier.wait(timeout=10)
        return ag2a.service.record_staged_callback(tenant_id="tenant-a", receipt=body["receipt"])
    def restage():
        barrier.wait(timeout=10)
        with _ag2a_refusal("idempotency_replay", 409) as caught:
            ag2a.service.stage(
                tenant=ag2a.tenant, description=DESCRIPTION, mode="build", idempotency_key="ag2a",
            )
        return caught.value.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(callback)
        second = pool.submit(restage)
        assert first.result(timeout=20)["accepted"]
        assert second.result(timeout=20) == 409
    assert ag2a.change().request_graph_input == AG2A_GRAPH
    assert ag2a.change().catalog_record_fields_json == _ag2a_snapshot([body["tool"]])
    assert len(ag2a.charges) == 1


def test_ag2a_stale_worker_cannot_replace_snapshot(ag2a, monkeypatch):
    ag2a.request(enqueue=True)
    clock = [time.time()]
    monkeypatch.setattr(customization_store.time, "time", lambda: clock[0])
    old = ag2a.store.claim_stage(owner="old-worker", lease_seconds=10)
    body = ag2a.body(old)
    clock[0] += 11
    winner = ag2a.store.claim_stage(owner="new-worker", lease_seconds=10)
    ag2a.service.reconcile_stage_worker(winner, body, lease_owner="new-worker", lease_attempt=winner.stage_attempt)
    before = ag2a.change()
    with _ag2a_refusal("record_fields_stage_conflict", 409) as caught:
        ag2a.service.reconcile_stage_worker(old, body, lease_owner="old-worker", lease_attempt=old.stage_attempt)
    assert caught.value.status_code == 409
    assert ag2a.change() == before
    assert len(ag2a.charges) == 1


def test_ag2a_changed_completion_replay(ag2a):
    ag2a.request()
    before = ag2a.change()
    events = ag2a.store.audit_events(tenant_id="tenant-a")
    changed = ag2a.body(before, name="ag2a-other")
    with _ag2a_refusal("staged_receipt_mismatch", 409) as caught:
        ag2a.service.record_staged_callback(tenant_id="tenant-a", receipt=changed["receipt"])
    assert caught.value.status_code == 409
    assert ag2a.change() == before
    assert ag2a.store.audit_events(tenant_id="tenant-a") == events


def test_ag2a_snapshot_write_failure(ag2a):
    with ag2a.store._transaction() as conn:
        conn.execute("CREATE TRIGGER ag2a_snapshot_fail BEFORE UPDATE OF catalog_record_fields_json "
                     "ON customization_change_sets WHEN NEW.catalog_record_fields_json IS NOT NULL "
                     "BEGIN SELECT RAISE(ABORT, 'snapshot failure'); END")
    with _ag2a_refusal("record_fields_write_failed", 503) as caught:
        ag2a.request()
    assert caught.value.status_code == 503
    change = ag2a.change()
    assert change.state is ChangeState.STAGING
    assert change.catalog_record_fields_json is None
    assert change.staged_commit is None
    assert all(event.next_state is not ChangeState.STAGED for event in ag2a.store.audit_events(tenant_id="tenant-a"))
    assert len(ag2a.charges) == 1


def test_ag2a_invalid_stored_snapshot(ag2a):
    result = ag2a.request()
    before = ag2a.change()
    invalid = json.loads(before.catalog_record_fields_json)
    invalid["tools"]["ag2a-graph"]["record_sha256"] = "0" * 64
    for snapshot in (None, "{malformed", json.dumps(invalid), _ag2a_snapshot([])):
        with ag2a.store._transaction() as conn:
            conn.execute("UPDATE customization_change_sets SET catalog_record_fields_json = ? "
                         "WHERE change_set_id = ?", (snapshot, before.change_set_id))
        with _ag2a_refusal("record_fields_invalid", 503) as caught:
            ag2a.request()
        assert caught.value.status_code == 503
        with _ag2a_refusal("record_fields_invalid", 503):
            ag2a.service.stage_status(tenant=ag2a.tenant, change_set_id=before.change_set_id)
        with _ag2a_refusal("record_fields_invalid", 503):
            ag2a.service.record_staged_callback(tenant_id="tenant-a", receipt=result["receipt"])
    with ag2a.store._transaction() as conn:
        conn.execute("UPDATE customization_change_sets SET catalog_record_fields_json = ?, "
                     "base_catalog_change_set_id = ? WHERE change_set_id = ?",
                     (before.catalog_record_fields_json, before.change_set_id, before.change_set_id))
    with _ag2a_refusal("record_fields_invalid", 503):
        ag2a.service.stage_status(tenant=ag2a.tenant, change_set_id=before.change_set_id)
    assert len(ag2a.charges) == 1


def _ag2a_corrupt(store, change_set_id):
    with store._transaction() as conn:
        conn.execute("UPDATE customization_change_sets SET catalog_record_fields_json = ? "
                     "WHERE change_set_id = ?", ("{malformed", change_set_id))


def test_ag2a_corrupt_predecessor_snapshot_refused(ag2a):
    predecessor, _tool = ag2a.predecessor()
    _ag2a_corrupt(ag2a.store, predecessor.change_set_id)
    for enqueue in (True, False):
        with _ag2a_refusal("record_fields_invalid", 503) as caught:
            ag2a.request(enqueue=enqueue)
        assert caught.value.status_code == 503
        with pytest.raises(ChangeSetNotFoundError):
            ag2a.change()
    assert ag2a.charges == ag2a.calls == []


def test_ag2a_corrupt_predecessor_before_callback_refused(ag2a):
    predecessor, _tool = ag2a.predecessor()
    ag2a.request(enqueue=True)
    queued = ag2a.change()
    receipt = ag2a.body(queued)["receipt"]
    _ag2a_corrupt(ag2a.store, predecessor.change_set_id)
    with _ag2a_refusal("record_fields_invalid", 503) as caught:
        ag2a.service.record_staged_callback(tenant_id="tenant-a", receipt=receipt)
    assert caught.value.status_code == 503
    after = ag2a.change()
    assert after.state == queued.state
    assert after.staged_commit == queued.staged_commit
    assert after.catalog_record_fields_json == queued.catalog_record_fields_json
