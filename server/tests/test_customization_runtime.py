"""Focused runtime gates for the durable customization integration."""
from __future__ import annotations

import json
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
from customization_models import ChangeSetConflictError, ChangeState
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


def test_live_author_reports_unsupported_one_off_mode(tmp_path, monkeypatch):
    service = CustomizationService(
        SQLiteCustomizationStore(tmp_path / "customization.db")
    )
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")
    monkeypatch.setattr(author_router.deps, "auth_live", lambda: True)
    monkeypatch.setattr(author_router, "customization_enabled", lambda *_: True)
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
        lambda tier: {"build": True},
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
