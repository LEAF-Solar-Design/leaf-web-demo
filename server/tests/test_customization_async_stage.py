from __future__ import annotations

import hashlib
import json
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
