"""Focused runtime gates for the durable customization integration."""
from __future__ import annotations

import json
import hashlib
import platform as stdlib_platform
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app
import customization_service
from customization_flags import enabled
from customization_models import ChangeSetConflictError, ChangeState, IdempotencyReplayError
from customization_service import CustomizationService, CustomizationServiceError
from customization_store import SQLiteCustomizationStore
from customization_authority import TenantBinding
from routers import author as author_router
from routers import ops as ops_router


BASE = "a" * 40
STAGED = "b" * 40
DIGEST = "c" * 64
WORKSPACE = "d" * 64


@pytest.fixture(autouse=True)
def reset_configured_service_cache():
    customization_service.reset_configured_services()
    yield
    customization_service.reset_configured_services()


def staged_change(store, tenant_id="tenant-a", suffix="a"):
    created = store.create_change_set(
        tenant_id=tenant_id,
        idempotency_key=f"create-{suffix}",
        base_commit=BASE,
        desired_platform_release="release-a",
        workspace_contract_digest=WORKSPACE,
        author_subject=f"auth0|author-{suffix}",
    )
    staging = store.transition(
        tenant_id=tenant_id,
        change_set_id=created.change_set_id,
        next_state=ChangeState.STAGING,
        expected_version=created.version,
        idempotency_key=f"staging-{suffix}",
    )
    return store.record_staged(
        tenant_id=tenant_id,
        change_set_id=created.change_set_id,
        expected_version=staging.version,
        idempotency_key=f"staged-{suffix}",
        staged_commit=STAGED,
        catalog_digest=DIGEST,
        platform_release="release-a",
        workspace_contract_digest=WORKSPACE,
    )


def test_ensure_bare_repo_provisions_first_time_tenant(tmp_path, monkeypatch):
    bare = tmp_path / "tenant-a.git"
    calls = []

    def resolve(_tenant_id):
        calls.append("resolve")
        if len(calls) == 1:
            raise CustomizationServiceError("tenant_repository_unavailable", 503)
        return bare

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"tenant_id": "tenant-a", "base_commit": BASE}

    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal:8150")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "secret")
    monkeypatch.setattr(customization_service, "_bare_repo", resolve)
    monkeypatch.setattr(customization_service, "_git", lambda *args: BASE)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: Response())

    assert customization_service._ensure_bare_repo("tenant-a") == bare
    assert calls == ["resolve", "resolve"]


def test_ensure_bare_repo_repairs_existing_repo_without_main(tmp_path, monkeypatch):
    bare = tmp_path / "tenant-a.git"
    git_calls = []
    post_calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"tenant_id": "tenant-a", "base_commit": BASE}

    def resolve(_tenant_id):
        return bare

    def git(*args):
        git_calls.append(args)
        if len(git_calls) == 1:
            raise CustomizationServiceError("tenant_repository_unavailable", 503)
        return BASE

    def post(*args, **kwargs):
        post_calls.append((args, kwargs))
        return Response()

    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal:8150")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "secret")
    monkeypatch.setattr(customization_service, "_bare_repo", resolve)
    monkeypatch.setattr(customization_service, "_git", git)
    monkeypatch.setattr(requests, "post", post)

    assert customization_service._ensure_bare_repo("tenant-a") == bare
    assert len(post_calls) == 1
    assert git_calls == [
        (bare, "rev-parse", "--verify", "refs/heads/main"),
        (bare, "rev-parse", "--verify", "refs/heads/main"),
    ]


def test_ensure_bare_repo_waits_for_shared_ref_visibility(tmp_path, monkeypatch):
    bare = tmp_path / "tenant-a.git"
    git_attempts = 0
    sleeps = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"tenant_id": "tenant-a", "base_commit": BASE}

    def git(*_args):
        nonlocal git_attempts
        git_attempts += 1
        if git_attempts < 4:
            raise CustomizationServiceError(
                "tenant_repository_unavailable", 503, "main_ref_not_observed"
            )
        return BASE

    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal:8150")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "secret")
    monkeypatch.setattr(customization_service, "_bare_repo", lambda _tenant_id: bare)
    monkeypatch.setattr(customization_service, "_git", git)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: Response())
    monkeypatch.setattr(customization_service.time, "sleep", sleeps.append)

    assert customization_service._ensure_bare_repo("tenant-a") == bare
    assert git_attempts == 4
    assert sleeps == [1.0, 2.0]


def test_ensure_bare_repo_bounds_shared_ref_visibility_wait(tmp_path, monkeypatch):
    bare = tmp_path / "tenant-a.git"
    sleeps = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"tenant_id": "tenant-a", "base_commit": BASE}

    def missing_main(*_args):
        raise CustomizationServiceError(
            "tenant_repository_unavailable", 503, "main_ref_not_observed"
        )

    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal:8150")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "secret")
    monkeypatch.setattr(customization_service, "_bare_repo", lambda _tenant_id: bare)
    monkeypatch.setattr(customization_service, "_git", missing_main)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: Response())
    monkeypatch.setattr(customization_service.time, "sleep", sleeps.append)

    with pytest.raises(CustomizationServiceError) as caught:
        customization_service._ensure_bare_repo("tenant-a")

    assert "provision_ref_not_visible" in caught.value.detail
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 15.0, 30.0]
    assert sum(sleeps) == 60.0


def test_ensure_bare_repo_rejects_unverified_harness_receipt(tmp_path, monkeypatch):
    bare = tmp_path / "tenant-a.git"
    attempts = 0

    def resolve(_tenant_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise CustomizationServiceError("tenant_repository_unavailable", 503)
        return bare

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"tenant_id": "tenant-b", "base_commit": BASE}

    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal:8150")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "secret")
    monkeypatch.setattr(customization_service, "_bare_repo", resolve)
    monkeypatch.setattr(customization_service, "_git", lambda *args: BASE)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: Response())

    with pytest.raises(CustomizationServiceError, match="tenant_repository_unavailable"):
        customization_service._ensure_bare_repo("tenant-a")


def test_stage_worker_preserves_exact_authority_headers(tmp_path, monkeypatch):
    store = SQLiteCustomizationStore(tmp_path / "customization.db")
    store.initialize()
    change, created = store.reserve_stage(
        tenant_id="tenant-a",
        idempotency_key="stage-authority-a",
        base_commit=BASE,
        desired_platform_release="release-a",
        workspace_contract_digest=WORKSPACE,
        author_subject="auth0|alice",
        change_set_id="11111111-1111-4111-8111-111111111111",
        request_description="make a tool",
        request_fingerprint="e" * 64,
        authority_session_id="session-a",
        authority_turn_id="turn-a",
    )
    assert created is True
    assert change.authority_session_id == "session-a"
    assert change.authority_turn_id == "turn-a"
    change = store.transition(
        tenant_id="tenant-a",
        change_set_id=change.change_set_id,
        next_state=ChangeState.STAGING,
        expected_version=change.version,
        idempotency_key="stage-authority-transition",
        expected_state=ChangeState.CREATED,
    )
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"receipt": {}}

    def post(*args, **kwargs):
        calls.append((args, kwargs))
        return Response()

    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal:8150")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "harness-secret")
    monkeypatch.setattr(
        customization_service.deps,
        "active_stage_author_subject",
        lambda *_args: "auth0|alice",
    )
    monkeypatch.setattr(requests, "post", post)

    CustomizationService(store).dispatch_stage(change)

    assert calls[0][1]["headers"] == {
        "X-Harness-Secret": "harness-secret",
        "X-Authority-Session-Id": "session-a",
        "X-Authority-Turn-Id": "turn-a",
    }


def test_stage_idempotency_rejects_authority_swap(tmp_path):
    store = SQLiteCustomizationStore(tmp_path / "customization.db")
    store.initialize()
    request = dict(
        tenant_id="tenant-a",
        idempotency_key="stage-authority-a",
        base_commit=BASE,
        desired_platform_release="release-a",
        workspace_contract_digest=WORKSPACE,
        author_subject="auth0|alice",
        change_set_id="11111111-1111-4111-8111-111111111111",
        request_description="make a tool",
        request_fingerprint="e" * 64,
        authority_session_id="session-a",
        authority_turn_id="turn-a",
    )
    store.reserve_stage(**request)

    with pytest.raises(IdempotencyReplayError):
        store.reserve_stage(**{**request, "authority_turn_id": "turn-b"})


def publish_change(store, tenant_id="tenant-a", suffix="a"):
    staged = staged_change(store, tenant_id, suffix)
    store.put_confirmation(
        confirmation_id=f"confirmation-{suffix}",
        payload={"tenant_id": tenant_id, "change_set_id": staged.change_set_id},
        signature=f"signature-{suffix}",
    )
    publishing = store.prepare_publish(
        tenant_id=tenant_id,
        change_set_id=staged.change_set_id,
        confirmation_id=f"confirmation-{suffix}",
        confirmation_signature=f"signature-{suffix}",
        approver_subject=f"auth0|approver-{suffix}",
        idempotency_key=f"publish-{suffix}",
    )
    store.publish(
        tenant_id=tenant_id,
        change_set_id=publishing.change_set_id,
        expected_version=publishing.version,
        idempotency_key=f"published-{suffix}",
        approver_subject=f"auth0|approver-{suffix}",
    )
    return staged.change_set_id


def test_rollout_flags_are_strict_and_r6_depends_on_r5(monkeypatch):
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "wat")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "all")
    assert enabled(5, "tenant-a") is False
    assert enabled(6, "tenant-a") is False

    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "internal")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "internal")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_INTERNAL_TENANTS", "tenant-a")
    assert enabled(5, "tenant-a") is True
    assert enabled(6, "tenant-a") is True
    assert enabled(6, "tenant-b") is False
    assert enabled(7, "tenant-a") is False


def test_off_rollout_flags_do_not_initialize_customization_sqlite(tmp_path, monkeypatch):
    database = tmp_path / "customization.db"
    monkeypatch.setenv("LEAF_CUSTOMIZATION_DB", str(database))
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "off")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "off")

    app.initialize_customization_store()

    assert not database.exists()


@pytest.mark.parametrize("runtime", ["production", "staging"])
def test_deployed_customization_refuses_auth_off(monkeypatch, runtime):
    monkeypatch.setenv("LEAF_RUNTIME_ENV", runtime)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "off")

    with pytest.raises(
        RuntimeError, match="customization requires live authentication"
    ):
        app.initialize_customization_store()


def test_rollout_off_stage_route_does_not_open_store(monkeypatch):
    calls = []
    monkeypatch.setattr(author_router.deps, "auth_live", lambda: True)
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "off")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "off")
    monkeypatch.setattr(
        author_router.CustomizationService,
        "configured",
        classmethod(lambda cls: calls.append(True)),
    )

    response = author_router.stage(
        author_router.StageRequest(
            description="make a tool", mode="build", idempotency_key="request"
        ),
        tenant="tenant-a",
    )

    assert response.status_code == 404
    assert calls == []


def test_shared_efs_sqlite_cannot_activate(monkeypatch):
    monkeypatch.setenv("LEAF_CUSTOMIZATION_DB", "/data/state/customization.db")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "off")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "off")

    with pytest.raises(
        CustomizationServiceError,
        match="customization_shared_sqlite_unsupported",
    ):
        CustomizationService.configured()


def test_postgres_store_requires_database_url(monkeypatch):
    monkeypatch.setenv("LEAF_CUSTOMIZATION_STORE", "postgres")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(
        CustomizationServiceError,
        match="customization_database_url_required",
    ):
        CustomizationService.configured()


def test_postgres_store_selector_uses_migration_owned_store(monkeypatch):
    initialized = []

    class FakePostgresStore:
        def initialize(self):
            initialized.append(True)

    monkeypatch.setenv("LEAF_CUSTOMIZATION_STORE", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://configured-without-connecting")
    monkeypatch.setattr(
        customization_service, "PostgresCustomizationStore", FakePostgresStore
    )

    first = CustomizationService.configured()
    second = CustomizationService.configured()

    assert first is second
    assert isinstance(first.store, FakePostgresStore)
    assert initialized == [True]


def test_customization_store_selector_fails_closed(monkeypatch):
    monkeypatch.setenv("LEAF_CUSTOMIZATION_STORE", "autoload")

    with pytest.raises(
        CustomizationServiceError,
        match="customization_store_unsupported",
    ):
        CustomizationService.configured()


def test_dark_rollout_ignores_existing_unsupported_shared_sqlite(tmp_path, monkeypatch):
    database = tmp_path / "customization.db"
    database.write_bytes(b"not a supported authority")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "off")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "off")
    monkeypatch.setattr(customization_service, "database_path", lambda: database)
    monkeypatch.setattr(customization_service, "_shared_sqlite_path", lambda path: True)
    monkeypatch.setattr(
        CustomizationService,
        "configured",
        classmethod(lambda cls: pytest.fail("dark rollout opened shared SQLite")),
    )

    assert customization_service.effective_catalog_dir("tenant-a") is None


def test_enabled_rollout_rejects_existing_unsupported_shared_sqlite(
    tmp_path, monkeypatch
):
    database = tmp_path / "customization.db"
    database.write_bytes(b"not a supported authority")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "off")
    monkeypatch.setattr(customization_service, "database_path", lambda: database)
    monkeypatch.setattr(customization_service, "_shared_sqlite_path", lambda path: True)

    with pytest.raises(
        CustomizationServiceError,
        match="customization_shared_sqlite_unsupported",
    ):
        customization_service.effective_catalog_dir("tenant-a")


def test_dark_rollout_pin_ignores_existing_unsupported_shared_sqlite(
    tmp_path, monkeypatch
):
    database = tmp_path / "customization.db"
    database.write_bytes(b"not a supported authority")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "off")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "off")
    monkeypatch.setattr(customization_service, "database_path", lambda: database)
    monkeypatch.setattr(customization_service, "_shared_sqlite_path", lambda path: True)
    monkeypatch.setattr(
        CustomizationService,
        "configured",
        classmethod(lambda cls: pytest.fail("dark rollout opened shared SQLite")),
    )

    assert customization_service.effective_catalog_pin("tenant-a") is None


def test_enabled_rollout_pin_rejects_existing_unsupported_shared_sqlite(
    tmp_path, monkeypatch
):
    database = tmp_path / "customization.db"
    database.write_bytes(b"not a supported authority")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "off")
    monkeypatch.setattr(customization_service, "database_path", lambda: database)
    monkeypatch.setattr(customization_service, "_shared_sqlite_path", lambda path: True)

    with pytest.raises(
        CustomizationServiceError,
        match="customization_shared_sqlite_unsupported",
    ):
        customization_service.effective_catalog_pin("tenant-a")


def test_enabled_rollout_pin_rejects_missing_sqlite_authority(
    tmp_path, monkeypatch
):
    database = tmp_path / "missing-customization.db"
    monkeypatch.setenv("LEAF_CUSTOMIZATION_STORE", "sqlite")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "off")
    monkeypatch.setattr(customization_service, "database_path", lambda: database)
    monkeypatch.setattr(customization_service, "_shared_sqlite_path", lambda path: False)

    with pytest.raises(
        CustomizationServiceError,
        match="effective_catalog_authority_unavailable",
    ):
        customization_service.effective_catalog_pin("tenant-a")


@pytest.mark.parametrize(
    "database",
    (
        "/data/state/../state/customization.db",
        "/data/state/nested/../customization.db",
    ),
)
def test_shared_efs_sqlite_rejects_normalized_paths(database, monkeypatch):
    monkeypatch.setenv("LEAF_CUSTOMIZATION_DB", database)

    with pytest.raises(
        CustomizationServiceError,
        match="customization_shared_sqlite_unsupported",
    ):
        CustomizationService.configured()


def test_shared_efs_sqlite_ops_routes_never_create_database(tmp_path, monkeypatch):
    shared = Path("/data/state") / f"codex-pr72-{tmp_path.name}.db"
    assert not shared.exists()
    monkeypatch.setenv("LEAF_CUSTOMIZATION_DB", str(shared))
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "off")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "off")
    monkeypatch.setenv("LEAF_OPS_SECRET", "ops-secret")
    verify = ops_router.DeploymentVerifyRequest(
        snapshot_id="snapshot",
        expected_effective_catalog_release="catalog",
        expected_platform_release="release",
    )
    snapshot = ops_router.DeploymentSnapshotRequest(snapshot_id="snapshot")

    responses = [
        ops_router.customization_deployment_snapshot(
            x_ops_secret="ops-secret"
        ),
        ops_router.customization_deployment_verify(
            verify, x_ops_secret="ops-secret"
        ),
        ops_router.customization_deployment_rollback(
            snapshot, x_ops_secret="ops-secret"
        ),
        ops_router.customization_deployment_rollback_verify(
            snapshot, x_ops_secret="ops-secret"
        ),
    ]

    assert [response.status_code for response in responses] == [503, 503, 503, 503]
    assert not shared.exists()


def test_configured_memoizes_service_per_canonical_database_path(
    tmp_path, monkeypatch
):
    database = tmp_path / "customization.db"
    monkeypatch.setenv("LEAF_CUSTOMIZATION_DB", str(database))
    calls = []
    original = SQLiteCustomizationStore.initialize

    def initialize_once(store):
        calls.append(store.database_path)
        return original(store)

    monkeypatch.setattr(SQLiteCustomizationStore, "initialize", initialize_once)

    first = CustomizationService.configured()
    second = CustomizationService.configured()

    assert first is second
    assert len(calls) == 1


def test_database_binding_uses_collision_safe_platform_alias(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost/test")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setitem(sys.modules, "platform", stdlib_platform)
    store = customization_service.platform_link.platform_store()
    binding = SimpleNamespace(
        platform_tenant_id="tenant-a", binding_id="binding-a"
    )
    monkeypatch.setattr(
        store, "resolve_active_identity_binding", lambda authority, subject: binding
    )
    monkeypatch.setattr(
        store, "active_identity_role", lambda tenant_id, binding_id: "owner"
    )
    tenant = customization_service.deps.TenantContext(
        "tenant-a", org_id="tenant-a", subject="auth0|user"
    )

    resolved = customization_service._binding(tenant)

    assert resolved == TenantBinding(
        "tenant-a", "auth0|user", "owner", True
    )
    assert store.__name__ == "leaf_platform.store"


def test_durable_confirmation_is_single_use(tmp_path):
    store = SQLiteCustomizationStore(tmp_path / "customization.db")
    store.put_confirmation(confirmation_id="confirmation", payload={"bound": True}, signature="sig")
    assert store.get_confirmation(confirmation_id="confirmation") == {
        "payload": {"bound": True}, "signature": "sig", "consumed": False,
    }
    assert store.consume_confirmation(confirmation_id="confirmation", signature="sig") is True
    assert store.consume_confirmation(confirmation_id="confirmation", signature="sig") is False


def test_confirmation_consume_and_approval_transitions_are_atomic_and_idempotent(tmp_path):
    store = SQLiteCustomizationStore(tmp_path / "customization.db")
    staged = staged_change(store)
    store.put_confirmation(
        confirmation_id="confirmation",
        payload={"tenant_id": "tenant-a", "change_set_id": staged.change_set_id},
        signature="signature",
    )
    publishing = store.prepare_publish(
        tenant_id="tenant-a",
        change_set_id=staged.change_set_id,
        confirmation_id="confirmation",
        confirmation_signature="signature",
        approver_subject="auth0|approver",
        idempotency_key="publish",
    )
    assert publishing.state is ChangeState.PUBLISHING
    assert store.get_confirmation(confirmation_id="confirmation")["consumed"] is True
    replay = store.prepare_publish(
        tenant_id="tenant-a",
        change_set_id=staged.change_set_id,
        confirmation_id="confirmation",
        confirmation_signature="signature",
        approver_subject="auth0|approver",
        idempotency_key="publish",
    )
    assert replay == publishing
    with pytest.raises(ChangeSetConflictError):
        store.prepare_publish(
            tenant_id="tenant-a",
            change_set_id=staged.change_set_id,
            confirmation_id="confirmation",
            confirmation_signature="signature",
            approver_subject="auth0|approver",
            idempotency_key="different-operation",
        )


def test_only_one_publish_per_tenant_can_remain_in_recovery(tmp_path):
    store = SQLiteCustomizationStore(tmp_path / "customization.db")
    first = staged_change(store, suffix="a")
    second = staged_change(store, suffix="b")
    for suffix, change in (("a", first), ("b", second)):
        store.put_confirmation(
            confirmation_id=f"confirmation-{suffix}",
            payload={"tenant_id": "tenant-a", "change_set_id": change.change_set_id},
            signature=f"signature-{suffix}",
        )
    store.prepare_publish(
        tenant_id="tenant-a",
        change_set_id=first.change_set_id,
        confirmation_id="confirmation-a",
        confirmation_signature="signature-a",
        approver_subject="auth0|approver-a",
        idempotency_key="publish-a",
    )

    with pytest.raises(
        ChangeSetConflictError, match="requires recovery first"
    ):
        store.prepare_publish(
            tenant_id="tenant-a",
            change_set_id=second.change_set_id,
            confirmation_id="confirmation-b",
            confirmation_signature="signature-b",
            approver_subject="auth0|approver-b",
            idempotency_key="publish-b",
        )


def test_deployment_snapshot_restores_all_effective_catalogs_and_audits(tmp_path):
    store = SQLiteCustomizationStore(tmp_path / "customization.db")
    first = publish_change(store, "tenant-a", "a")
    second = publish_change(store, "tenant-b", "b")
    snapshot = store.capture_deployment_snapshot(
        platform_release="prod-old", idempotency_key="snapshot"
    )
    payload = json.loads(snapshot["payload_json"])
    assert [row["tenant_id"] for row in payload] == ["tenant-a", "tenant-b"]

    with store._transaction() as conn:
        conn.execute("DELETE FROM effective_catalogs WHERE tenant_id = ?", ("tenant-b",))
    broken = store.verify_deployment_snapshot(
        snapshot_id=snapshot["snapshot_id"],
        action="verify",
        idempotency_key="verify-broken",
    )
    assert broken["verified"] is False

    restored = store.restore_deployment_snapshot(
        snapshot_id=snapshot["snapshot_id"], idempotency_key="restore"
    )
    assert restored["platform_release"] == "prod-old"
    assert store.get_effective_catalog(tenant_id="tenant-a").change_set_id == first
    assert store.get_effective_catalog(tenant_id="tenant-b").change_set_id == second
    verified = store.verify_deployment_snapshot(
        snapshot_id=snapshot["snapshot_id"],
        action="restore_verify",
        idempotency_key="verify-restored",
    )
    assert verified["verified"] is True


def test_independent_confirmation_rejects_the_harness_dispatch_secret(
    monkeypatch,
):
    calls = []
    fake = SimpleNamespace(
        confirm=lambda **kwargs: calls.append(kwargs) or {"confirmation_id": "ok"}
    )
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", "harness-secret")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_APPROVAL_SECRET", "approval-secret")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "all")
    monkeypatch.setattr(author_router.deps, "auth_live", lambda: True)
    monkeypatch.setattr(
        author_router.CustomizationService,
        "configured",
        classmethod(lambda cls: fake),
    )
    request = author_router.InternalConfirmRequest(change_set_id="change")

    denied = author_router.confirm(
        request,
        x_tenant_id="tenant-a",
        x_approval_secret="harness-secret",
    )
    approved = author_router.confirm(
        request,
        x_tenant_id="tenant-a",
        x_approval_secret="approval-secret",
    )

    assert denied.status_code == 403
    assert approved == {"confirmation_id": "ok"}
    assert calls == [{"tenant_id": "tenant-a", "change_set_id": "change"}]


def test_internal_confirmation_hides_cross_tenant_change_without_mutation(
    tmp_path, monkeypatch,
):
    store = SQLiteCustomizationStore(tmp_path / "customization.db")
    service = CustomizationService(store)
    staged = staged_change(store, tenant_id="tenant-a", suffix="route")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "all")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_APPROVAL_SECRET", "approval-secret")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_CONFIRMATION_SECRET", "signing-secret")
    monkeypatch.setenv(
        "LEAF_CUSTOMIZATION_INTERNAL_APPROVER_SUBJECT", "auth0|approver"
    )
    monkeypatch.setattr(
        author_router.CustomizationService,
        "configured",
        classmethod(lambda cls: service),
    )
    route_app = FastAPI()
    route_app.include_router(author_router.router)
    client = TestClient(route_app, raise_server_exceptions=False)
    body = {"change_set_id": staged.change_set_id}
    headers = {"X-Approval-Secret": "approval-secret"}

    with store._connection() as conn:
        confirmations_before = conn.execute(
            "SELECT COUNT(*) AS n FROM customization_confirmations"
        ).fetchone()["n"]
    denied = client.post(
        "/internal/customization/confirm",
        json=body,
        headers={**headers, "X-Tenant-Id": "tenant-b"},
    )

    assert denied.status_code == 404
    assert denied.json()["reason_code"] == "confirmation_not_available"
    assert store.get_change_set(
        tenant_id="tenant-a", change_set_id=staged.change_set_id
    ).state is ChangeState.STAGED
    with store._connection() as conn:
        confirmations_after_denial = conn.execute(
            "SELECT COUNT(*) AS n FROM customization_confirmations"
        ).fetchone()["n"]
    assert confirmations_after_denial == confirmations_before

    approved = client.post(
        "/internal/customization/confirm",
        json=body,
        headers={**headers, "X-Tenant-Id": "tenant-a"},
    )
    assert approved.status_code == 200
    assert isinstance(approved.json().get("confirmation_id"), str)
    assert store.get_change_set(
        tenant_id="tenant-a", change_set_id=staged.change_set_id
    ).state is ChangeState.STAGED
    with store._connection() as conn:
        confirmations_after_owner = conn.execute(
            "SELECT COUNT(*) AS n FROM customization_confirmations"
        ).fetchone()["n"]
    assert confirmations_after_owner == confirmations_before + 1


def test_independent_confirmation_rejects_non_ascii_secret(monkeypatch):
    calls = []
    monkeypatch.setenv("LEAF_CUSTOMIZATION_APPROVAL_SECRET", "approval-secret")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "all")
    monkeypatch.setattr(author_router.deps, "auth_live", lambda: True)
    monkeypatch.setattr(
        author_router.CustomizationService,
        "configured",
        classmethod(
            lambda cls: SimpleNamespace(
                confirm=lambda **kwargs: calls.append(kwargs)
            )
        ),
    )

    response = author_router.confirm(
        author_router.InternalConfirmRequest(change_set_id="change"),
        x_tenant_id="tenant-a",
        x_approval_secret="approval-secrét",
    )

    assert response.status_code == 403
    assert calls == []


def test_tenant_identity_is_trimmed_once_before_flags_and_storage(
    tmp_path, monkeypatch
):
    service = CustomizationService(SQLiteCustomizationStore(tmp_path / "customization.db"))
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "off")

    with pytest.raises(CustomizationServiceError, match="customization_stage_disabled"):
        service.stage(
            tenant=" tenant-a ", description="make a tool", mode="build",
            idempotency_key="request-a",
        )

    with pytest.raises(CustomizationServiceError, match="tenant_identity_invalid"):
        service.stage(
            tenant="../tenant-a", description="make a tool", mode="build",
            idempotency_key="request-b",
        )


def test_live_author_fails_closed_when_r5_is_disabled(monkeypatch):
    legacy_calls = []
    monkeypatch.setattr(author_router.deps, "auth_live", lambda: True)
    monkeypatch.setattr(author_router, "customization_enabled", lambda *_: False)
    monkeypatch.setattr(
        author_router,
        "_legacy_author",
        lambda *_: legacy_calls.append(True),
    )

    response = author_router.author(
        author_router.AuthorRequest(description="make a tool"),
        tenant="tenant-a",
        idempotency_key="request-a",
    )

    assert response.status_code == 404
    body = json.loads(response.body)
    assert body["reason_code"] == "customization_stage_disabled"
    assert body["error"]["message"] == (
        "Tool authoring is not enabled for this workspace in this environment. "
        "The approved request was not executed."
    )
    assert "refused" not in body["error"]["message"].lower()
    assert legacy_calls == []


def test_live_author_preserves_requested_mode(monkeypatch):
    calls = []
    service = SimpleNamespace(
        stage=lambda **kwargs: calls.append(kwargs) or {"status": "staged"}
    )
    monkeypatch.setattr(author_router.deps, "auth_live", lambda: True)
    monkeypatch.setattr(author_router, "customization_enabled", lambda *_: True)
    monkeypatch.setattr(
        author_router.deps, "stage_author_identity", lambda tenant, *_: tenant
    )
    monkeypatch.setattr(
        author_router.CustomizationService,
        "configured",
        classmethod(lambda cls: service),
    )

    response = author_router.author(
        author_router.AuthorRequest(description="make a tool", mode="one_off"),
        tenant="tenant-a",
        idempotency_key="request-a",
    )

    assert response == {"status": "staged"}
    assert calls == [{
        "tenant": "tenant-a",
        "description": "make a tool",
        "mode": "one_off",
        "idempotency_key": "request-a",
    }]


def test_tenant_row_removal_uses_authenticated_tenant_gate_and_exact_intent(monkeypatch):
    calls = []
    tenant = SimpleNamespace(tenant_id="tenant-a", subject="auth0|owner")
    monkeypatch.setattr(author_router, "_customization_gate", lambda wave, seen: None)
    monkeypatch.setattr(
        author_router.CustomizationService,
        "configured",
        classmethod(lambda cls: SimpleNamespace(
            stage_removal=lambda **kwargs: calls.append(kwargs) or {
                "contract": "leaf.customization.v1", "state": "staged"
            }
        )),
    )
    request = author_router.RemovalRequest(
        tool_name="count-by-layer",
        expected_catalog_digest="a" * 64,
        idempotency_key="remove-count-by-layer",
    )

    result = author_router.remove_tool(request, tenant=tenant)

    assert result["state"] == "staged"
    assert calls == [{
        "tenant": tenant,
        "tool_name": "count-by-layer",
        "expected_catalog_digest": "a" * 64,
        "idempotency_key": "remove-count-by-layer",
    }]


def test_removal_authority_route_is_closed_and_authenticated(monkeypatch):
    calls = []
    tenant = SimpleNamespace(tenant_id="tenant-a", subject="auth0|owner")
    expected = {
        "contract": "leaf.customization-removal-authority.v1",
        "tool_name": "count-by-layer",
        "effective_catalog_digest": "a" * 64,
        "target_row_count": 1,
        "target_provenance": "tenant_repo",
        "removal_authorized": True,
    }
    monkeypatch.setattr(author_router, "_customization_gate", lambda wave, seen: None)
    monkeypatch.setattr(
        author_router.CustomizationService,
        "configured",
        classmethod(lambda cls: SimpleNamespace(
            removal_authority=lambda **kwargs: calls.append(kwargs) or expected
        )),
    )

    result = author_router.removal_authority(
        author_router.RemovalAuthorityRequest(tool_name="count-by-layer"),
        tenant=tenant,
    )

    assert result == expected
    assert set(result) == {
        "contract", "tool_name", "effective_catalog_digest", "target_row_count",
        "target_provenance", "removal_authorized",
    }
    assert calls == [{"tenant": tenant, "tool_name": "count-by-layer"}]
    assert not ({"tenant_id", "subject", "path", "commit", "registry"} & set(result))
    with pytest.raises(ValueError):
        author_router.RemovalAuthorityRequest(
            tool_name="count-by-layer", tenant_id="forbidden"
        )


def test_removal_authority_refuses_read_only_role(monkeypatch):
    service = CustomizationService(SimpleNamespace())
    monkeypatch.setattr(customization_service, "_binding", lambda _tenant:
                        TenantBinding("tenant-a", "auth0|reader", "read_only", True))
    with pytest.raises(CustomizationServiceError, match="tenant_role_denied"):
        service.removal_authority(tenant="tenant-a", tool_name="count-by-layer")


@pytest.mark.parametrize(
    ("rows", "digest_matches", "reason"),
    [
        ([{"name": "count-by-layer"}, {"name": "count-by-layer"}], True,
         "removal_target_ambiguous"),
        ([{"name": "count-by-layer"}], False,
         "effective_catalog_digest_mismatch"),
        ([{"not_name": "count-by-layer"}], True, "effective_catalog_malformed"),
    ],
)
def test_removal_authority_fails_closed_on_invalid_pinned_registry(
    rows, digest_matches, reason, tmp_path, monkeypatch
):
    raw = json.dumps({"tools": rows}, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    current = SimpleNamespace(catalog_commit=BASE, catalog_digest=(
        digest if digest_matches else "f" * 64
    ))
    service = CustomizationService(SimpleNamespace(
        get_effective_catalog=lambda **_kwargs: current
    ))
    monkeypatch.setattr(customization_service, "_binding", lambda _tenant:
                        TenantBinding("tenant-a", "auth0|owner", "owner", True))
    monkeypatch.setattr(customization_service, "_bare_repo", lambda _tenant: tmp_path)
    monkeypatch.setattr(customization_service, "_git_blob", lambda *_args: raw)

    with pytest.raises(CustomizationServiceError, match=reason):
        service.removal_authority(
            tenant="tenant-a",
            tool_name="count-by-layer",
        )


@pytest.mark.parametrize("role", ["owner", "editor"])
def test_removal_authority_reads_only_pinned_tenant_registry(
    role, tmp_path, monkeypatch
):
    raw = json.dumps({
        "tools": [
            {"name": "other", "value": "preserved"},
            {"name": "count-by-layer", "entry": "tenant.py"},
        ]
    }, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    current = SimpleNamespace(catalog_commit=BASE, catalog_digest=digest)
    service = CustomizationService(SimpleNamespace(
        get_effective_catalog=lambda **kwargs: current
    ))
    monkeypatch.setattr(customization_service, "_binding", lambda _tenant:
                        TenantBinding("tenant-a", "auth0|owner", role, True))
    monkeypatch.setattr(customization_service, "_bare_repo", lambda _tenant: tmp_path)
    monkeypatch.setattr(customization_service, "_git_blob", lambda *_args: raw)

    assert service.removal_authority(
        tenant="tenant-a",
        tool_name="count-by-layer",
    ) == {
        "contract": "leaf.customization-removal-authority.v1",
        "tool_name": "count-by-layer",
        "effective_catalog_digest": digest,
        "target_row_count": 1,
        "target_provenance": "tenant_repo",
        "removal_authorized": True,
    }


def test_removal_authority_reports_closed_absent_postcondition(tmp_path, monkeypatch):
    raw = b'{"tools":[{"name":"other"}]}'
    digest = hashlib.sha256(raw).hexdigest()
    service = CustomizationService(SimpleNamespace(
        get_effective_catalog=lambda **_kwargs: SimpleNamespace(
            catalog_commit=BASE, catalog_digest=digest
        )
    ))
    monkeypatch.setattr(customization_service, "_binding", lambda _tenant:
                        TenantBinding("tenant-a", "auth0|owner", "owner", True))
    monkeypatch.setattr(customization_service, "_bare_repo", lambda _tenant: tmp_path)
    monkeypatch.setattr(customization_service, "_git_blob", lambda *_args: raw)

    assert service.removal_authority(
        tenant="tenant-a", tool_name="count-by-layer"
    ) == {
        "contract": "leaf.customization-removal-authority.v1",
        "tool_name": "count-by-layer",
        "effective_catalog_digest": digest,
        "target_row_count": 0,
        "target_provenance": "operator_owned_engine_expected",
        "removal_authorized": False,
    }


def test_live_author_reports_unsupported_one_off_mode(tmp_path, monkeypatch):
    service = CustomizationService(
        SQLiteCustomizationStore(tmp_path / "customization.db")
    )
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")
    monkeypatch.setattr(author_router.deps, "auth_live", lambda: True)
    monkeypatch.setattr(author_router, "customization_enabled", lambda *_: True)
    monkeypatch.setattr(
        author_router.deps, "stage_author_identity", lambda tenant, *_: tenant
    )
    monkeypatch.setattr(
        author_router.CustomizationService,
        "configured",
        classmethod(lambda cls: service),
    )

    response = author_router.author(
        author_router.AuthorRequest(description="make a tool", mode="one_off"),
        tenant="tenant-a",
        idempotency_key="request-a",
    )

    assert response.status_code == 422
    body = json.loads(response.body)
    assert body["reason_code"] == "invalid_stage_request"
    assert body["error"]["message"] == (
        "The requested authoring mode is not supported by the protected authoring path. "
        "Use build mode."
    )


def test_live_author_requires_stable_idempotency_key_when_r5_is_enabled(monkeypatch):
    configured_calls = []
    monkeypatch.setattr(author_router.deps, "auth_live", lambda: True)
    monkeypatch.setattr(author_router, "customization_enabled", lambda *_: True)
    monkeypatch.setattr(
        author_router.deps, "stage_author_identity", lambda tenant, *_: tenant
    )
    monkeypatch.setattr(
        author_router.CustomizationService,
        "configured",
        classmethod(lambda cls: configured_calls.append(True)),
    )

    response = author_router.author(
        author_router.AuthorRequest(description="make a tool"),
        tenant="tenant-a",
        idempotency_key=None,
    )

    assert response.status_code == 422
    assert json.loads(response.body)["reason_code"] == "idempotency_key_required"
    assert configured_calls == []


@pytest.mark.parametrize("include_tool", [False, True])
def test_stage_callback_completed_response_preserves_validated_tool_or_retries_receipt_only(
    tmp_path, monkeypatch, include_tool
):
    store = SQLiteCustomizationStore(tmp_path / "customization.db")
    service = CustomizationService(store)
    release = SimpleNamespace(
        release_id="release-a", workspace_contract_sha256=WORKSPACE
    )
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")
    # A callback-completed retry presupposes a configured harness; stage()
    # refuses an unconfigured one (URL and secret) before charging the
    # authoring quota.
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal:8150")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "secret")
    monkeypatch.setattr(
        customization_service,
        "_binding",
        lambda tenant: TenantBinding(str(tenant), "auth0|author", "owner", True),
    )
    monkeypatch.setattr(
        customization_service.entitlements, "resolve_tier", lambda tenant: "pro"
    )
    monkeypatch.setattr(
        customization_service.entitlements,
        "entitlements_for",
        lambda tier, *_roles: {"build": True},
    )
    monkeypatch.setattr(customization_service, "_bare_repo", lambda tenant: Path("."))
    monkeypatch.setattr(customization_service, "_git", lambda *args: BASE)
    monkeypatch.setattr(service, "_release", lambda: release)
    monkeypatch.setattr(
        service,
        "_authority",
        lambda: SimpleNamespace(authorize_stage=lambda **kwargs: None),
    )
    monkeypatch.setattr(service, "_verify_catalog", lambda *args: None)
    policy_calls = []
    monkeypatch.setattr(
        service,
        "_verify_stage_policy",
        lambda change, body=None: policy_calls.append((change.state, body)),
    )

    proposed_tool = {
        "name": "centered-test-prism",
        "capabilities": ["drawing.write"],
    }

    def callback_completed(change_tenant, description, change):
        receipt = {
            "contract": "leaf.customization.v1",
            "tenant_id": change_tenant,
            "change_set_id": change.change_set_id,
            "state": "staged",
            "base_commit": change.base_commit,
            "staged_commit": STAGED,
            "catalog_digest": DIGEST,
            "platform_release": change.desired_platform_release,
            "workspace_contract_digest": change.workspace_contract_digest,
            "idempotency_key": change.idempotency_key,
        }
        current = store.get_change_set(
            tenant_id=change_tenant, change_set_id=change.change_set_id
        )
        store.record_staged(
            tenant_id=change_tenant,
            change_set_id=change.change_set_id,
            expected_version=current.version,
            idempotency_key=f"staged:{change.idempotency_key}",
            staged_commit=STAGED,
            catalog_digest=DIGEST,
            platform_release=change.desired_platform_release,
            workspace_contract_digest=change.workspace_contract_digest,
        )
        return {
            "receipt": receipt,
            **(
                {"tool": proposed_tool, "preview": {"summary": "Adds a prism"}}
                if include_tool
                else {}
            ),
        }

    monkeypatch.setattr(service, "_harness_stage", callback_completed)

    result = service.stage(
        tenant="tenant-a",
        description="make a tool",
        mode="build",
        idempotency_key="request-a",
    )

    assert result["receipt"]["state"] == "staged"
    assert result["receipt"]["staged_commit"] == STAGED
    if include_tool:
        assert result["tool"] == proposed_tool
        assert result["preview"] == {"summary": "Adds a prism"}
        assert policy_calls[0][0] is ChangeState.STAGED
        assert policy_calls[0][1]["tool"] == proposed_tool
    else:
        assert "tool" not in result
        assert policy_calls == [(ChangeState.STAGED, None)]


def test_rollback_requires_r6_and_owner_or_editor(tmp_path, monkeypatch):
    service = CustomizationService(SQLiteCustomizationStore(tmp_path / "customization.db"))
    with pytest.raises(CustomizationServiceError, match="customization_rollback_disabled"):
        service.rollback(
            tenant="tenant-a", change_set_id="change", idempotency_key="rollback"
        )

    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "all")
    monkeypatch.setattr(
        customization_service,
        "_binding",
        lambda tenant: TenantBinding(str(tenant), "reviewer", "reviewer", True),
    )
    with pytest.raises(CustomizationServiceError, match="tenant_role_denied"):
        service.rollback(
            tenant="tenant-a", change_set_id="change", idempotency_key="rollback"
        )


def _ag2b_setup(tmp_path, monkeypatch):
    # Reuse AG2a's real Git/policy fixture without changing its acceptance rows.
    from test_customization_async_stage import ag2a
    store = SQLiteCustomizationStore(tmp_path / "customization.db")
    store.initialize()
    lane = ag2a.__wrapped__(tmp_path, store, monkeypatch)
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "all")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_STORE", "sqlite")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_DB", str(store.database_path))
    monkeypatch.setenv("LEAF_EFFECTIVE_TENANTS_DIR", str(tmp_path / "effective"))
    monkeypatch.setattr(CustomizationService, "configured", classmethod(lambda cls: lane.service))
    lane.publishes = []

    def publish(change):
        lane.publishes.append(change.change_set_id)
        head = customization_service._git(lane.bare, "rev-parse", "refs/heads/main")
        assert head in {change.base_commit, change.staged_commit}
        customization_service._git(lane.bare, "update-ref", "refs/heads/main", change.staged_commit)
        return change.staged_commit

    lane.service._harness_publish = publish
    lane.service._harness_remove = lambda change, _digest: lane.body(change, remove=change.target_tool_name)
    return lane


@pytest.fixture
def ag2b(tmp_path, monkeypatch):
    return _ag2b_setup(tmp_path, monkeypatch)


def _ag2b_stage(lane, key="first", *, graph="solar-w1-graph", target=None):
    lane.row_options = {"name": key}
    lane.request(key=key, graph=graph, target=target)
    return lane.change(key)


def _ag2b_approval(lane, change):
    from customization_authority import PublishRequest, StagedChange, StaffAuthority
    approval = lane.service._authority().issue_publish_confirmation(
        staged_change=StagedChange(
            change.tenant_id, change.change_set_id, change.staged_commit,
            change.catalog_digest, change.desired_platform_release,
            change.workspace_contract_digest, change.author_subject, True,
        ),
        author_binding=TenantBinding(change.tenant_id, change.author_subject, "owner", True),
        staff_authority=StaffAuthority("auth0|independent-approver", True, True),
    )
    request = PublishRequest(
        change.change_set_id, change.staged_commit, change.catalog_digest,
        change.desired_platform_release, change.workspace_contract_digest,
    )
    return request, approval.confirmation_id


def _ag2b_publish(lane, change, approval=None):
    request, confirmation_id = approval or _ag2b_approval(lane, change)
    lane.service.publish(
        tenant=lane.tenant, request=request, confirmation_id=confirmation_id,
        idempotency_key="publish-" + change.idempotency_key,
    )
    return lane.change(change.idempotency_key)


def _ag2b_loaders():
    import deps
    return (
        lambda: deps.load_tenant_repo_tools("tenant-a"),
        lambda: dict(deps._strict_provenance_tiers("tenant-a"))[deps.TOOL_SOURCE_TENANT_REPO],
    )


def _ag2b_refuses(code, call, status=503):
    with pytest.raises(CustomizationServiceError) as caught:
        call()
    assert caught.value.code == code
    assert caught.value.status_code == status


def _ag2b_corrupt(lane, change, snapshot):
    with lane.store._transaction() as conn:
        conn.execute(
            "UPDATE customization_change_sets SET catalog_record_fields_json = ? WHERE change_set_id = ?",
            (snapshot, change.change_set_id),
        )


def _ag2b_wrong_snapshot(change):
    snapshot = json.loads(change.catalog_record_fields_json)
    next(iter(snapshot["tools"].values()))["record_sha256"] = "0" * 64
    return json.dumps(snapshot)


def _ag2b_set_pin(lane, change):
    with lane.store._transaction() as conn:
        conn.execute(
            "UPDATE effective_catalogs SET change_set_id = ?, catalog_commit = ?, catalog_digest = ?, "
            "effective_platform_release = ?, workspace_contract_digest = ? WHERE tenant_id = ?",
            (change.change_set_id, change.staged_commit, change.catalog_digest,
             change.desired_platform_release, change.workspace_contract_digest, change.tenant_id),
        )


def _ag2b_remove(lane, key="remove"):
    pin = lane.store.get_effective_catalog(tenant_id="tenant-a")
    lane.service.stage_removal(
        tenant=lane.tenant, tool_name="first", expected_catalog_digest=pin.catalog_digest,
        idempotency_key=key,
    )
    return lane.change(key)


def test_ag2b_three_publications(ag2b):
    from test_customization_async_stage import _ag2a_snapshot
    changes = [_ag2b_publish(ag2b, _ag2b_stage(ag2b, key)) for key in ("first", "second", "third")]
    raw = ag2b.service._record_fields_rows(changes[-1], changes[-1].staged_commit)
    assert changes[-1].catalog_record_fields_json == _ag2a_snapshot(list(raw.values()))
    for load in _ag2b_loaders():
        rows = load()
        assert {row["name"] for row in rows} == {"first", "second", "third"}
        assert all(row["graph_input"] == "solar-w1-graph" for row in rows)


def test_ag2b_unrelated_addition(ag2b):
    first = _ag2b_publish(ag2b, _ag2b_stage(ag2b))
    before = _ag2b_loaders()[0]()
    ordinary = _ag2b_publish(ag2b, _ag2b_stage(ag2b, "ordinary", graph=None))
    assert ordinary.catalog_record_fields_json == first.catalog_record_fields_json
    for load in _ag2b_loaders():
        rows = {row["name"]: row for row in load()}
        assert rows["first"] == before[0]
        assert "graph_input" not in rows["ordinary"]


def test_ag2b_revision_inherits_graph(ag2b):
    from test_customization_async_stage import _ag2a_snapshot
    first = _ag2b_publish(ag2b, _ag2b_stage(ag2b))
    revised = _ag2b_publish(ag2b, _ag2b_stage(ag2b, "revision", graph=None, target="first"))
    raw = ag2b.service._record_fields_rows(revised, revised.staged_commit)["first"]
    assert raw["version"] == "1.0.1"
    assert revised.catalog_record_fields_json == _ag2a_snapshot([raw])
    assert revised.catalog_record_fields_json != first.catalog_record_fields_json
    assert _ag2b_loaders()[0]()[0]["graph_input"] == "solar-w1-graph"


def test_ag2b_removal_updates_snapshot(ag2b):
    from test_customization_async_stage import _ag2a_snapshot
    first = _ag2b_publish(ag2b, _ag2b_stage(ag2b))
    removal = _ag2b_remove(ag2b)
    assert removal.base_catalog_change_set_id == first.change_set_id
    assert removal.catalog_record_fields_json == _ag2a_snapshot([])
    assert ag2b.service.stage_removal(
        tenant=ag2b.tenant, tool_name="first", expected_catalog_digest=first.catalog_digest,
        idempotency_key="remove",
    )["receipt"]["change_set_id"] == removal.change_set_id
    assert ag2b.change("remove").base_catalog_change_set_id == first.change_set_id
    _ag2b_publish(ag2b, removal)
    for load in _ag2b_loaders():
        assert load() == []


def test_ag2b_rollback_restores_snapshot(ag2b):
    import deps
    first = _ag2b_publish(ag2b, _ag2b_stage(ag2b))
    before = deps.catalog_tool_view(_ag2b_loaders()[0]()[0])
    _ag2b_publish(ag2b, _ag2b_remove(ag2b))
    ag2b.service.rollback(tenant=ag2b.tenant, change_set_id=first.change_set_id, idempotency_key="rollback")
    assert ag2b.store.get_effective_catalog(tenant_id="tenant-a").change_set_id == first.change_set_id
    assert ag2b.change("first").catalog_record_fields_json == first.catalog_record_fields_json
    for load in _ag2b_loaders():
        assert deps.catalog_tool_view(load()[0]) == before


def test_ag2b_fresh_loader_agreement(ag2b):
    import deps
    first = _ag2b_publish(ag2b, _ag2b_stage(ag2b))
    raw_before = customization_service._git_blob(ag2b.bare, f"{first.staged_commit}:registry.json")
    ag2b.store = SQLiteCustomizationStore(ag2b.store.database_path)
    ag2b.service = CustomizationService(ag2b.store)
    compat, strict = (load() for load in _ag2b_loaders())
    assert compat == strict
    assert compat[0]["graph_input"] == "solar-w1-graph"
    assert deps.catalog_tool_view(compat[0]) == deps.catalog_tool_view(strict[0])
    undecorated = dict(compat[0])
    del undecorated["graph_input"]
    assert deps.catalog_tool_digest(undecorated) != deps.catalog_tool_digest(compat[0])
    assert customization_service._git_blob(ag2b.bare, f"{first.staged_commit}:registry.json") == raw_before
    assert "graph_input" not in json.loads(raw_before)["tools"][0]


def test_ag2b_loader_retries_pin_race(ag2b, monkeypatch):
    first = _ag2b_publish(ag2b, _ag2b_stage(ag2b))
    second = _ag2b_publish(ag2b, _ag2b_stage(ag2b, "second"))
    materialize = customization_service.effective_catalog_dir
    for load in _ag2b_loaders():
        _ag2b_set_pin(ag2b, first)
        calls = []
        def move(tenant):
            root = materialize(tenant)
            calls.append(root)
            if len(calls) == 1:
                _ag2b_set_pin(ag2b, second)
            return root
        monkeypatch.setattr(customization_service, "effective_catalog_dir", move)
        rows = load()
        assert len(calls) == 2
        assert {row["name"] for row in rows} == {"first", "second"}
        assert all(row["graph_input"] == "solar-w1-graph" for row in rows)


def test_ag2b_loader_refuses_repeated_pin_race(ag2b, monkeypatch):
    changes = [_ag2b_publish(ag2b, _ag2b_stage(ag2b, key)) for key in ("first", "second", "third")]
    materialize = customization_service.effective_catalog_dir
    for load in _ag2b_loaders():
        _ag2b_set_pin(ag2b, changes[0])
        calls = []
        def move(tenant):
            root = materialize(tenant)
            calls.append(root)
            _ag2b_set_pin(ag2b, changes[len(calls)])
            return root
        monkeypatch.setattr(customization_service, "effective_catalog_dir", move)
        _ag2b_refuses("effective_catalog_unavailable", load)
        assert len(calls) == 2


def test_ag2b_projection_without_durable_row_resolves_legacy(ag2b, monkeypatch):
    # The durable row is the authority: a projection with no row is no pin, so the
    # loader hands resolution back to the legacy path (and its own rollout rules).
    assert ag2b.service._record_fields_pin("tenant-a") is None
    monkeypatch.setattr(customization_service, "effective_catalog_pin", lambda _tenant: {
        "catalog_commit": "a" * 40, "effective_catalog_digest": "b" * 64,
    })
    assert customization_service.load_authoritative_tenant_tools("tenant-a") is None


def test_ag2b_pin_vanishing_on_retry_refuses(ag2b, monkeypatch):
    _ag2b_publish(ag2b, _ag2b_stage(ag2b))
    materialize = customization_service.effective_catalog_dir
    for load in _ag2b_loaders():
        change = ag2b.change("first")
        _ag2b_set_pin(ag2b, change)
        calls = []
        def vanish(tenant):
            root = materialize(tenant)
            calls.append(root)
            with ag2b.store._transaction() as conn:
                conn.execute("DELETE FROM effective_catalogs WHERE tenant_id = ?", ("tenant-a",))
            return root
        monkeypatch.setattr(customization_service, "effective_catalog_dir", vanish)
        _ag2b_refuses("effective_catalog_unavailable", load)
        assert len(calls) == 1
        monkeypatch.setattr(customization_service, "effective_catalog_dir", materialize)
        with ag2b.store._transaction() as conn:
            conn.execute(
                "INSERT INTO effective_catalogs (tenant_id, change_set_id, catalog_commit, catalog_digest, "
                "effective_platform_release, workspace_contract_digest) VALUES (?, ?, ?, ?, ?, ?)",
                ("tenant-a", change.change_set_id, change.staged_commit, change.catalog_digest,
                 change.desired_platform_release, change.workspace_contract_digest),
            )


def _ag2b_first_pin_fallback(lane, monkeypatch, entrance, *, null_snapshot=False):
    change = _ag2b_stage(lane)
    projection = customization_service.effective_catalog_pin
    calls = []

    def publish_after_absence(tenant):
        pin = projection(tenant)
        calls.append(pin)
        if len(calls) == 1:
            assert pin is None
            published = _ag2b_publish(lane, change)
            if null_snapshot:
                _ag2b_corrupt(lane, published, None)
        return pin

    monkeypatch.setattr(customization_service, "effective_catalog_pin", publish_after_absence)
    load = _ag2b_loaders()[entrance]
    if null_snapshot:
        _ag2b_refuses("record_fields_invalid", load)
    else:
        rows = load()
        assert len(rows) == 1
        assert rows[0]["name"] == "first"
        assert rows[0]["graph_input"] == "solar-w1-graph"
        raw = customization_service._git_blob(lane.bare, f"{change.staged_commit}:registry.json")
        assert "graph_input" not in json.loads(raw)["tools"][0]
    assert len(calls) >= 2


def test_ag2b_compat_first_pin_fallback_verifies_graph(ag2b, monkeypatch):
    _ag2b_first_pin_fallback(ag2b, monkeypatch, 0)


def test_ag2b_strict_first_pin_fallback_verifies_graph(ag2b, monkeypatch):
    _ag2b_first_pin_fallback(ag2b, monkeypatch, 1)


def test_ag2b_compat_first_pin_fallback_refuses_null_snapshot(ag2b, monkeypatch):
    _ag2b_first_pin_fallback(ag2b, monkeypatch, 0, null_snapshot=True)


def test_ag2b_strict_first_pin_fallback_refuses_null_snapshot(ag2b, monkeypatch):
    _ag2b_first_pin_fallback(ag2b, monkeypatch, 1, null_snapshot=True)


def _ag2b_durable_absence_fallback(lane, monkeypatch, entrance, *, persists=False):
    _ag2b_publish(lane, _ag2b_stage(lane))
    durable_pin = lane.service._record_fields_pin
    calls = []

    def absent_first_read(tenant):
        calls.append(tenant)
        if len(calls) == 1 or persists:
            return None
        return durable_pin(tenant)

    monkeypatch.setattr(lane.service, "_record_fields_pin", absent_first_read)
    load = _ag2b_loaders()[entrance]
    if persists:
        _ag2b_refuses("effective_catalog_unavailable", load)
    else:
        rows = load()
        assert len(rows) == 1
        assert rows[0]["name"] == "first"
        assert rows[0]["graph_input"] == "solar-w1-graph"
    assert len(calls) >= 2


def test_ag2b_compat_projection_durable_absence_reverifies(ag2b, monkeypatch):
    _ag2b_durable_absence_fallback(ag2b, monkeypatch, 0)


def test_ag2b_strict_projection_durable_absence_reverifies(ag2b, monkeypatch):
    _ag2b_durable_absence_fallback(ag2b, monkeypatch, 1)


def test_ag2b_compat_fallback_refuses_persistent_durable_absence(ag2b, monkeypatch):
    _ag2b_durable_absence_fallback(ag2b, monkeypatch, 0, persists=True)


def test_ag2b_strict_fallback_refuses_persistent_durable_absence(ag2b, monkeypatch):
    _ag2b_durable_absence_fallback(ag2b, monkeypatch, 1, persists=True)


def test_ag2b_missing_snapshot_refuses_publish(ag2b):
    change = _ag2b_stage(ag2b)
    approval = _ag2b_approval(ag2b, change)
    _ag2b_corrupt(ag2b, change, None)
    _ag2b_refuses("record_fields_invalid", lambda: _ag2b_publish(ag2b, change, approval))
    assert not ag2b.store.get_confirmation(confirmation_id=approval[1])["consumed"]
    assert ag2b.publishes == []
    assert ag2b.service._record_fields_pin("tenant-a") is None


def test_ag2b_incomplete_snapshot_refuses_publish(ag2b):
    _ag2b_publish(ag2b, _ag2b_stage(ag2b))
    change = _ag2b_stage(ag2b, "second")
    approval = _ag2b_approval(ag2b, change)
    snapshot = json.loads(change.catalog_record_fields_json)
    del snapshot["tools"]["first"]
    _ag2b_corrupt(ag2b, change, json.dumps(snapshot))
    before = ag2b.store.get_effective_catalog(tenant_id="tenant-a")
    _ag2b_refuses("record_fields_invalid", lambda: _ag2b_publish(ag2b, change, approval))
    assert not ag2b.store.get_confirmation(confirmation_id=approval[1])["consumed"]
    assert ag2b.store.get_effective_catalog(tenant_id="tenant-a") == before
    assert len(ag2b.publishes) == 1


def test_ag2b_wrong_row_binding_refuses_reload(ag2b):
    first = _ag2b_publish(ag2b, _ag2b_stage(ag2b))
    _ag2b_corrupt(ag2b, first, _ag2b_wrong_snapshot(first))
    for load in _ag2b_loaders():
        _ag2b_refuses("record_fields_invalid", load)


def test_ag2b_metadata_read_failure(ag2b, monkeypatch):
    import sqlite3
    _ag2b_publish(ag2b, _ag2b_stage(ag2b))
    change = _ag2b_stage(ag2b, "second")
    approval = _ag2b_approval(ag2b, change)
    before = ag2b.store.get_effective_catalog(tenant_id="tenant-a")
    def fail(**kwargs):
        raise sqlite3.OperationalError("authority read failed")
    monkeypatch.setattr(ag2b.store, "get_change_set", fail)
    _ag2b_refuses("record_fields_read_failed", lambda: _ag2b_publish(ag2b, change, approval))
    for load in _ag2b_loaders():
        _ag2b_refuses("record_fields_read_failed", load)
    assert ag2b.store.get_effective_catalog(tenant_id="tenant-a") == before
    assert not ag2b.store.get_confirmation(confirmation_id=approval[1])["consumed"]


def _ag2b_prepare(lane, change, approval):
    request, confirmation_id = approval
    confirmation, signature = lane.service._authority().verify_publish_confirmation(
        tenant_id="tenant-a", request=request, confirmation_id=confirmation_id,
    )
    return lane.store.prepare_publish(
        tenant_id="tenant-a", change_set_id=change.change_set_id,
        confirmation_id=confirmation_id, confirmation_signature=signature,
        approver_subject=confirmation.approver_subject,
        idempotency_key="publish-" + change.idempotency_key,
    )


def test_ag2b_publish_callback_verifies_snapshot(ag2b):
    change = _ag2b_stage(ag2b)
    _ag2b_prepare(ag2b, change, _ag2b_approval(ag2b, change))
    receipt = ag2b.service._raw_receipt(change)
    _ag2b_corrupt(ag2b, change, _ag2b_wrong_snapshot(change))
    _ag2b_refuses("record_fields_invalid", lambda: ag2b.service.authorize_publish_callback(
        tenant_id="tenant-a", receipt=receipt, expected_main_sha=change.base_commit,
    ))
    assert ag2b.publishes == []


def test_ag2b_publishing_recovery_verifies_snapshot(ag2b):
    change = _ag2b_stage(ag2b)
    approval = _ag2b_approval(ag2b, change)
    _ag2b_prepare(ag2b, change, approval)
    customization_service._git(ag2b.bare, "update-ref", "refs/heads/main", change.staged_commit)
    recovered = _ag2b_publish(ag2b, change, approval)
    assert recovered.state is ChangeState.PUBLISHED
    second = _ag2b_stage(ag2b, "second")
    second_approval = _ag2b_approval(ag2b, second)
    _ag2b_prepare(ag2b, second, second_approval)
    customization_service._git(ag2b.bare, "update-ref", "refs/heads/main", second.staged_commit)
    _ag2b_corrupt(ag2b, second, _ag2b_wrong_snapshot(second))
    before = ag2b.store.get_effective_catalog(tenant_id="tenant-a")
    _ag2b_refuses("record_fields_invalid", lambda: _ag2b_publish(ag2b, second, second_approval))
    assert len(ag2b.publishes) == 1
    assert ag2b.change("second").state is ChangeState.PUBLISHING
    assert ag2b.store.get_effective_catalog(tenant_id="tenant-a") == before


def test_ag2b_published_replay_verifies_snapshot(ag2b):
    change = _ag2b_stage(ag2b)
    approval = _ag2b_approval(ag2b, change)
    published = _ag2b_publish(ag2b, change, approval)
    assert _ag2b_publish(ag2b, published, approval) == published
    _ag2b_corrupt(ag2b, published, _ag2b_wrong_snapshot(published))
    _ag2b_refuses("record_fields_invalid", lambda: _ag2b_publish(ag2b, published, approval))
    assert len(ag2b.publishes) == 1


def test_ag2b_moved_predecessor_preserves_approval(ag2b):
    first = _ag2b_publish(ag2b, _ag2b_stage(ag2b))
    change = _ag2b_stage(ag2b, "second")
    approval = _ag2b_approval(ag2b, change)
    competing = _ag2b_publish(ag2b, _ag2b_stage(ag2b, "competing"))
    assert change.base_catalog_change_set_id == first.change_set_id
    before = ag2b.store.get_effective_catalog(tenant_id="tenant-a")
    _ag2b_refuses("record_fields_predecessor_mismatch", lambda: _ag2b_publish(ag2b, change, approval), 409)
    assert not ag2b.store.get_confirmation(confirmation_id=approval[1])["consumed"]
    assert ag2b.store.get_effective_catalog(tenant_id="tenant-a") == before
    assert before.change_set_id == competing.change_set_id
    assert len(ag2b.publishes) == 2


def test_ag2b_publish_write_failure_preserves_recovery(ag2b):
    _ag2b_publish(ag2b, _ag2b_stage(ag2b))
    change = _ag2b_stage(ag2b, "second")
    approval = _ag2b_approval(ag2b, change)
    before = ag2b.store.get_effective_catalog(tenant_id="tenant-a")
    with ag2b.store._transaction() as conn:
        conn.execute("CREATE TRIGGER ag2b_effective_fail BEFORE UPDATE ON effective_catalogs "
                     "BEGIN SELECT RAISE(ABORT, 'effective write failed'); END")
    _ag2b_refuses("record_fields_write_failed", lambda: _ag2b_publish(ag2b, change, approval))
    assert customization_service._git(ag2b.bare, "rev-parse", "refs/heads/main") == change.staged_commit
    assert ag2b.store.get_effective_catalog(tenant_id="tenant-a") == before
    assert ag2b.change("second").state is ChangeState.PUBLISHING
    assert ag2b.store.get_confirmation(confirmation_id=approval[1])["consumed"]
    with ag2b.store._transaction() as conn:
        conn.execute("DROP TRIGGER ag2b_effective_fail")
    assert _ag2b_publish(ag2b, change, approval).state is ChangeState.PUBLISHED


def test_ag2b_rollback_rejects_invalid_target_snapshot(ag2b):
    first = _ag2b_publish(ag2b, _ag2b_stage(ag2b))
    _ag2b_publish(ag2b, _ag2b_remove(ag2b))
    before = ag2b.store.get_effective_catalog(tenant_id="tenant-a")
    _ag2b_corrupt(ag2b, first, _ag2b_wrong_snapshot(first))
    _ag2b_refuses("record_fields_invalid", lambda: ag2b.service.rollback(
        tenant=ag2b.tenant, change_set_id=first.change_set_id, idempotency_key="rollback",
    ))
    assert ag2b.store.get_effective_catalog(tenant_id="tenant-a") == before


def test_ag2b_harness_wire_unchanged(tmp_path, monkeypatch):
    from test_customization_async_stage import DESCRIPTION, active_author_turn
    fixed_id = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(customization_service, "uuid4", lambda: fixed_id)
    wires = []
    raws = []
    for index, graph in enumerate(("solar-w1-graph", None)):
        root = tmp_path / str(index)
        root.mkdir()
        lane = _ag2b_setup(root, monkeypatch)
        session_id, turn_id = active_author_turn.__wrapped__(root, monkeypatch)
        calls = []
        def post(url, **kwargs):
            calls.append((url, kwargs["json"]))
            change = lane.change("fixed")
            if url.endswith("/author/stage"):
                body = lane.body(change, name="fixed")
            else:
                customization_service._git(lane.bare, "update-ref", "refs/heads/main", change.staged_commit)
                body = {"commit": change.staged_commit}
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)
        monkeypatch.setattr(requests, "post", post)
        lane.service._harness_stage = CustomizationService._harness_stage.__get__(lane.service)
        lane.service._harness_publish = CustomizationService._harness_publish.__get__(lane.service)
        lane.service.stage(
            tenant=lane.tenant, description=DESCRIPTION, mode="build", idempotency_key="fixed",
            graph_input=graph, authority_session_id=session_id, authority_turn_id=turn_id,
        )
        change = lane.change("fixed")
        _ag2b_publish(lane, change)
        assert calls[0][1]["description"] == DESCRIPTION
        assert calls[0][1]["changeSetId"] == fixed_id
        assert set(calls[1][1]) == {"tenant_id", "receipt", "expectedMainSha"}
        assert "graph_input" not in calls[0][1]
        assert "graph_input" not in calls[1][1]["receipt"]
        assert set(calls[1][1]["receipt"]) == {
            "contract", "tenant_id", "change_set_id", "state", "base_commit",
            "staged_commit", "catalog_digest", "platform_release",
            "workspace_contract_digest", "idempotency_key",
        }
        wires.append((set(calls[0][1]), set(calls[1][1]["receipt"])))
        raw = customization_service._git_blob(lane.bare, f"{change.staged_commit}:registry.json")
        raws.append(raw)
        assert "graph_input" not in json.loads(raw)["tools"][0]
        manifest = customization_service._git_blob(lane.bare, f"{change.staged_commit}:tools/fixed/tool.json")
        assert "graph_input" not in json.loads(manifest)
    assert wires[0] == wires[1]
    assert raws[0] == raws[1]
