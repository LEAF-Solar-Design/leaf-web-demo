from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from customization_models import (
    ChangeSetConflictError,
    ChangeSetNotFoundError,
    ChangeState,
)
from customization_postgres_store import PostgresCustomizationStore
from customization_store import SQLiteCustomizationStore
from platform_link import platform_db
from scripts.reconcile_customization_authority import reconcile


pytestmark = pytest.mark.skipif(
    not os.environ.get("PG_CUSTOMIZATION_TEST_URL"),
    reason="PG_CUSTOMIZATION_TEST_URL is not configured",
)
BASE = "a" * 40
STAGED = "b" * 40
DIGEST = "c" * 64
WORKSPACE = "d" * 64


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    os.environ["DATABASE_URL"] = os.environ["PG_CUSTOMIZATION_TEST_URL"]
    database = platform_db()
    database.reset_pool()
    database.apply_migration()
    sqlite_store = SQLiteCustomizationStore(
        tmp_path_factory.mktemp("customization-authority") / "customization.db"
    )
    sqlite_store.initialize()
    stage(
        sqlite_store,
        create(sqlite_store, "pg-backfill-tenant", "pg-backfill-create"),
        "pg-backfill",
    )
    try:
        sqlite_path = Path(sqlite_store.database_path)
        assert reconcile(sqlite_path=sqlite_path, mode="backfill")["parity"]
        assert reconcile(sqlite_path=sqlite_path, mode="parity")["parity"]
        result = PostgresCustomizationStore()
        result.initialize()
        yield result
    finally:
        database.reset_pool()


def create(store, tenant: str, key: str):
    return store.create_change_set(
        tenant_id=tenant,
        idempotency_key=key,
        base_commit=BASE,
        desired_platform_release="platform@sha256:abc",
        workspace_contract_digest=WORKSPACE,
        author_subject="auth0|author",
    )


def stage(store, change, prefix: str):
    staging = store.transition(
        tenant_id=change.tenant_id,
        change_set_id=change.change_set_id,
        next_state=ChangeState.STAGING,
        expected_version=change.version,
        idempotency_key=f"{prefix}-staging",
    )
    return store.record_staged(
        tenant_id=change.tenant_id,
        change_set_id=change.change_set_id,
        expected_version=staging.version,
        idempotency_key=f"{prefix}-staged",
        staged_commit=STAGED,
        catalog_digest=DIGEST,
        platform_release="platform@sha256:abc",
        workspace_contract_digest=WORKSPACE,
    )


def test_postgres_full_publication_and_tenant_isolation(store) -> None:
    staged = stage(store, create(store, "pg-tenant-a", "pg-create-a"), "pg-a")
    awaiting = store.transition(
        tenant_id=staged.tenant_id,
        change_set_id=staged.change_set_id,
        next_state=ChangeState.AWAITING_APPROVAL,
        expected_version=staged.version,
        idempotency_key="pg-awaiting-a",
    )
    approved = store.transition(
        tenant_id=awaiting.tenant_id,
        change_set_id=awaiting.change_set_id,
        next_state=ChangeState.APPROVED,
        expected_version=awaiting.version,
        idempotency_key="pg-approved-a",
        approver_subject="auth0|approver",
    )
    publishing = store.transition(
        tenant_id=approved.tenant_id,
        change_set_id=approved.change_set_id,
        next_state=ChangeState.PUBLISHING,
        expected_version=approved.version,
        idempotency_key="pg-publishing-a",
    )
    effective = store.publish(
        tenant_id=publishing.tenant_id,
        change_set_id=publishing.change_set_id,
        expected_version=publishing.version,
        idempotency_key="pg-published-a",
    )
    assert effective.catalog_commit == STAGED
    assert store.audit_events(
        tenant_id="pg-tenant-a", change_set_id=publishing.change_set_id
    )[-1].next_state is ChangeState.PUBLISHED
    with pytest.raises(ChangeSetNotFoundError):
        store.get_change_set(
            tenant_id="pg-tenant-b", change_set_id=publishing.change_set_id
        )


def test_postgres_confirmation_consumption_is_single_use(store) -> None:
    store.put_confirmation(
        confirmation_id="pg-confirmation-a",
        payload={"tenant_id": "pg-tenant-a", "change_set_id": "change-a"},
        signature="signature-a",
    )
    assert store.consume_confirmation(
        confirmation_id="pg-confirmation-a", signature="signature-a"
    )
    assert not store.consume_confirmation(
        confirmation_id="pg-confirmation-a", signature="signature-a"
    )


def test_postgres_optimistic_race_allows_one_writer(store) -> None:
    change = create(store, "pg-tenant-race", "pg-create-race")

    def attempt(key: str) -> str:
        try:
            store.transition(
                tenant_id=change.tenant_id,
                change_set_id=change.change_set_id,
                next_state=ChangeState.STAGING,
                expected_version=change.version,
                idempotency_key=key,
            )
            return "ok"
        except ChangeSetConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ("pg-race-a", "pg-race-b")))
    assert sorted(outcomes) == ["conflict", "ok"]


def test_postgres_deployment_snapshot_is_idempotent(store) -> None:
    first = store.capture_deployment_snapshot(
        platform_release="platform@sha256:abc",
        idempotency_key="pg-deployment-snapshot",
    )
    second = store.capture_deployment_snapshot(
        platform_release="platform@sha256:abc",
        idempotency_key="pg-deployment-snapshot",
    )
    assert first["snapshot_id"] == second["snapshot_id"]
    assert store.verify_deployment_snapshot(
        snapshot_id=first["snapshot_id"],
        action="verify",
        idempotency_key="pg-deployment-verify",
    )["verified"]
