from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from customization_models import (
    ChangeSetConflictError,
    ChangeSetNotFoundError,
    ChangeState,
)
from customization_postgres_store import PostgresCustomizationStore
from customization_store import SQLiteCustomizationStore
from platform_link import platform_db
from scripts import reconcile_customization_authority as authority_reconcile


BASE = "a" * 40
STAGED = "b" * 40
DIGEST = "c" * 64
WORKSPACE = "d" * 64


def _published_authority(store, tenant: str, prefix: str) -> dict:
    staged = stage(store, create(store, tenant, f"{prefix}-create"), prefix)
    confirmation_id = f"{prefix}-confirmation"
    store.put_confirmation(
        confirmation_id=confirmation_id,
        payload={"tenant_id": tenant, "change_set_id": staged.change_set_id},
        signature=f"{prefix}-signature",
    )
    store.get_or_create_publication_request(
        tenant_id=tenant, change_set_id=staged.change_set_id
    )
    store.bind_publication_confirmation(
        tenant_id=tenant,
        change_set_id=staged.change_set_id,
        confirmation_id=confirmation_id,
    )
    awaiting = store.transition(
        tenant_id=tenant,
        change_set_id=staged.change_set_id,
        next_state=ChangeState.AWAITING_APPROVAL,
        expected_version=staged.version,
        idempotency_key=f"{prefix}-awaiting",
    )
    approved = store.transition(
        tenant_id=tenant,
        change_set_id=staged.change_set_id,
        next_state=ChangeState.APPROVED,
        expected_version=awaiting.version,
        idempotency_key=f"{prefix}-approved",
        approver_subject="auth0|approver",
    )
    publishing = store.transition(
        tenant_id=tenant,
        change_set_id=staged.change_set_id,
        next_state=ChangeState.PUBLISHING,
        expected_version=approved.version,
        idempotency_key=f"{prefix}-publishing",
    )
    store.publish(
        tenant_id=tenant,
        change_set_id=staged.change_set_id,
        expected_version=publishing.version,
        idempotency_key=f"{prefix}-published",
    )
    snapshot = store.capture_deployment_snapshot(
        platform_release="platform@sha256:abc",
        idempotency_key=f"{prefix}-snapshot",
    )
    assert store.verify_deployment_snapshot(
        snapshot_id=snapshot["snapshot_id"],
        action="verify",
        idempotency_key=f"{prefix}-verify",
    )["verified"]
    return {
        "tenant_id": tenant,
        "snapshot_id": snapshot["snapshot_id"],
    }


def _postgres_snapshot(database):
    with database.transaction(isolation="serializable") as connection:
        return authority_reconcile._postgres_snapshot(connection)


def _authority_subset(source: dict[str, list[dict]], first: dict) -> dict:
    declared = authority_reconcile.TABLE_COLUMNS
    if set(source) != set(declared):
        raise RuntimeError("customization authority fixture has an invalid table set")

    subset: dict[str, list[dict]] = {}
    for table, columns in declared.items():
        if "tenant_id" in columns:
            key = "tenant_id"
        elif "snapshot_id" in columns:
            key = "snapshot_id"
        else:
            raise RuntimeError(
                f"customization authority fixture has no partition key for {table}"
            )
        subset[table] = [row for row in source[table] if row[key] == first[key]]
    return subset


def test_authority_subset_follows_declared_inventory(monkeypatch):
    declared = {
        "tenant_rows": ("row_id", "tenant_id"),
        "snapshot_rows": ("row_id", "snapshot_id"),
        "new_reconciled_rows": ("row_id", "tenant_id"),
    }
    source = {
        "tenant_rows": [
            {"row_id": "a", "tenant_id": "tenant-a"},
            {"row_id": "b", "tenant_id": "tenant-b"},
        ],
        "snapshot_rows": [
            {"row_id": "a", "snapshot_id": "snapshot-a"},
            {"row_id": "b", "snapshot_id": "snapshot-b"},
        ],
        "new_reconciled_rows": [
            {"row_id": "a", "tenant_id": "tenant-a"},
            {"row_id": "b", "tenant_id": "tenant-b"},
        ],
    }
    monkeypatch.setattr(authority_reconcile, "TABLE_COLUMNS", declared)

    assert _authority_subset(
        source, {"tenant_id": "tenant-a", "snapshot_id": "snapshot-a"}
    ) == {
        "tenant_rows": [{"row_id": "a", "tenant_id": "tenant-a"}],
        "snapshot_rows": [{"row_id": "a", "snapshot_id": "snapshot-a"}],
        "new_reconciled_rows": [{"row_id": "a", "tenant_id": "tenant-a"}],
    }


@pytest.mark.parametrize(
    ("declared", "source"),
    (
        ({"declared": ("row_id", "tenant_id")}, {}),
        ({}, {"unknown": []}),
        ({"unsupported": ("row_id",)}, {"unsupported": []}),
    ),
)
def test_authority_subset_refuses_inventory_drift(monkeypatch, declared, source):
    monkeypatch.setattr(authority_reconcile, "TABLE_COLUMNS", declared)

    with pytest.raises(RuntimeError, match="invalid table set|no partition key"):
        _authority_subset(
            source, {"tenant_id": "tenant-a", "snapshot_id": "snapshot-a"}
        )


@pytest.fixture(scope="module")
def authority_backfill_gate(tmp_path_factory):
    if not os.environ.get("PG_CUSTOMIZATION_TEST_URL"):
        pytest.skip("PG_CUSTOMIZATION_TEST_URL is not configured")
    os.environ["DATABASE_URL"] = os.environ["PG_CUSTOMIZATION_TEST_URL"]
    database = platform_db()
    database.reset_pool()
    database.apply_migration()
    sqlite_store = SQLiteCustomizationStore(
        tmp_path_factory.mktemp("customization-authority") / "customization.db"
    )
    sqlite_store.initialize()
    first = _published_authority(sqlite_store, "pg-backfill-a", "pg-backfill-a")
    _published_authority(sqlite_store, "pg-backfill-b", "pg-backfill-b")
    try:
        sqlite_path = Path(sqlite_store.database_path)
        source = authority_reconcile._sqlite_snapshot(sqlite_path)
        subset = _authority_subset(source, first)
        assert all(
            subset[table]
            for table in authority_reconcile.TABLE_COLUMNS
            if source[table]
        )
        assert all(
            len(subset[table]) < len(source[table])
            for table in authority_reconcile.TABLE_COLUMNS
            if source[table]
        )
        assert not any(
            authority_reconcile.authority_counts(_postgres_snapshot(database)).values()
        )
        with database.transaction(isolation="serializable") as connection:
            authority_reconcile._insert_snapshot(connection, subset)

        partial_receipt = authority_reconcile.reconcile(
            sqlite_path=sqlite_path, mode="backfill"
        )
        assert partial_receipt["parity"]

        create(sqlite_store, "pg-rollback-tenant", "pg-rollback-create")
        before_failure = _postgres_snapshot(database)
        real_insert = authority_reconcile._insert_snapshot

        def insert_then_violate_unique(connection, snapshot):
            real_insert(connection, snapshot)
            for table, rows in snapshot.items():
                if not rows:
                    continue
                columns = authority_reconcile.TABLE_COLUMNS[table]
                placeholders = ",".join(["%s"] * len(columns))
                connection.execute(
                    f"INSERT INTO {table} ({','.join(columns)}) "
                    f"VALUES ({placeholders})",
                    tuple(rows[0][column] for column in columns),
                )
                break

        with patch.object(
            authority_reconcile,
            "_insert_snapshot",
            side_effect=insert_then_violate_unique,
        ):
            with pytest.raises(Exception):
                authority_reconcile.reconcile(
                    sqlite_path=sqlite_path, mode="backfill"
                )
        assert _postgres_snapshot(database) == before_failure
        assert authority_reconcile.reconcile(
            sqlite_path=sqlite_path, mode="backfill"
        )["parity"]
        assert authority_reconcile.reconcile(
            sqlite_path=sqlite_path, mode="parity"
        )["parity"]
        postgres_store = PostgresCustomizationStore()
        postgres_store.initialize()
        create(
            postgres_store,
            "pg-retained-target-tenant",
            "pg-retained-target-create",
        )
        superset_receipt = authority_reconcile.reconcile(
            sqlite_path=sqlite_path, mode="parity"
        )
        repeated_superset_receipt = authority_reconcile.reconcile(
            sqlite_path=sqlite_path, mode="backfill"
        )
        assert superset_receipt["source_incorporated"]
        assert not superset_receipt["exact_equal"]
        assert superset_receipt["target_only_counts"][
            "customization_change_sets"
        ] == 1
        assert superset_receipt["target_only_counts"][
            "customization_audit_events"
        ] == 1
        assert repeated_superset_receipt["final_target_digest"] == (
            superset_receipt["final_target_digest"]
        )
        assert repeated_superset_receipt["target_counts"] == (
            superset_receipt["target_counts"]
        )
        yield {
            "partial_receipt": partial_receipt,
            "rollback_snapshot": before_failure,
            "superset_receipt": superset_receipt,
        }
    finally:
        database.reset_pool()


@pytest.fixture(scope="module")
def store(authority_backfill_gate):
    result = PostgresCustomizationStore()
    result.initialize()
    yield result


def test_postgres_incremental_backfill_and_rollback_gate(authority_backfill_gate):
    assert authority_backfill_gate["partial_receipt"]["parity"]
    assert authority_backfill_gate["rollback_snapshot"]
    assert authority_backfill_gate["superset_receipt"]["source_incorporated"]
    assert not authority_backfill_gate["superset_receipt"]["exact_equal"]


def test_postgres_migration_order_keeps_stage_authority_and_mcp_journal(
    authority_backfill_gate,
):
    database = platform_db()
    with database.transaction() as connection:
        columns = {
            row["column_name"]
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'customization_change_sets'"
            ).fetchall()
        }
        migrations = [
            row["name"]
            for row in connection.execute(
                "SELECT name FROM leaf_schema_migrations "
                "WHERE name IN ("
                "'0030_tenant_mcp_approvals.sql', "
                "'0031_customization_stage_authority.sql'"
                ") ORDER BY name"
            ).fetchall()
        ]
        approval_table = connection.execute(
            "SELECT to_regclass("
            "current_schema() || '.harness_tenant_mcp_approvals'"
            ") AS table_name"
        ).fetchone()["table_name"]
    assert {"authority_session_id", "authority_turn_id"} <= columns
    assert migrations == [
        "0030_tenant_mcp_approvals.sql",
        "0031_customization_stage_authority.sql",
    ]
    assert approval_table == "harness_tenant_mcp_approvals"


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


def test_postgres_stage_recovery_parity(store, monkeypatch) -> None:
    import hashlib
    from uuid import uuid4

    import customization_service
    import deps
    from customization_authority import TenantBinding
    from customization_service import CustomizationService

    tenant_id = f"pg-recovery-{uuid4().hex[:12]}"
    key = str(uuid4())
    subject = "auth0|recovery-author"
    description = "postgres stage recovery parity"
    change, created = store.reserve_stage(
        tenant_id=tenant_id, idempotency_key=key,
        base_commit=BASE, desired_platform_release="platform@sha256:abc",
        workspace_contract_digest=WORKSPACE, author_subject=subject,
        change_kind="create", target_tool_name=None,
        request_description=description,
        request_fingerprint=hashlib.sha256(description.encode("utf-8")).hexdigest(),
        authority_session_id=str(uuid4()), authority_turn_id=str(uuid4()),
    )
    assert created
    change = store.transition(
        tenant_id=tenant_id, change_set_id=change.change_set_id,
        next_state=ChangeState.STAGING, expected_version=change.version,
        expected_state=ChangeState.CREATED, idempotency_key=str(uuid4()),
    )
    monkeypatch.setattr(customization_service, "enabled", lambda *_args: True)
    monkeypatch.setattr(
        customization_service, "_binding",
        lambda tenant: TenantBinding(str(tenant), tenant.subject, "owner", True),
    )
    monkeypatch.setattr(customization_service.entitlements, "resolve_tier", lambda _tenant: "hosted_pro")
    monkeypatch.setattr(customization_service.entitlements, "resolve_roles", lambda _tenant: ((), False))
    monkeypatch.setattr(customization_service.entitlements, "entitlements_for", lambda *_args: {"build": True})
    service = CustomizationService(store)
    author = deps.TenantContext(tenant_id, tier="hosted_pro", subject=subject)
    other = deps.TenantContext(tenant_id, tier="hosted_pro", subject="auth0|other-author")
    before = _postgres_snapshot(platform_db())
    assert service.recover_stage(tenant=author, idempotency_key=key) == {
        "contract": "leaf.customization-stage-recovery.v1", "status": "found",
        "job": service.stage_status_change(change),
    }
    miss = {"contract": "leaf.customization-stage-recovery.v1", "status": "not_found"}
    assert service.recover_stage(tenant=other, idempotency_key=key) == miss
    assert service.recover_stage(tenant=author, idempotency_key=str(uuid4())) == miss
    assert _postgres_snapshot(platform_db()) == before


def test_postgres_async_stage_claim_race_is_single_owner(store) -> None:
    description = "postgres async race"
    change, created = store.reserve_stage(
        tenant_id="pg-stage-race", idempotency_key="pg-stage-race",
        base_commit=BASE, desired_platform_release="platform@sha256:abc",
        workspace_contract_digest=WORKSPACE, author_subject="auth0|author",
        change_kind="create", target_tool_name=None,
        request_description=description,
        request_fingerprint=__import__("hashlib").sha256(
            description.encode()
        ).hexdigest(),
    )
    assert created
    store.transition(
        tenant_id=change.tenant_id, change_set_id=change.change_set_id,
        next_state=ChangeState.STAGING, expected_version=change.version,
        expected_state=ChangeState.CREATED, idempotency_key="pg-stage-race-queued",
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(
            lambda owner: store.claim_stage(owner=owner, lease_seconds=30),
            ("pg-worker-a", "pg-worker-b"),
        ))
    assert sum(claim is not None for claim in claims) == 1


def test_postgres_async_worker_skips_bound_removal_and_claims_ordinary(store) -> None:
    tenant = "pg-removal-worker-boundary"
    _published_authority(store, tenant, "pg-removal-worker-predecessor")
    effective = store.get_effective_catalog(tenant_id=tenant)
    removal = store.create_change_set(
        tenant_id=tenant, idempotency_key="pg-removal-worker-removal",
        base_commit=effective.catalog_commit,
        desired_platform_release=effective.effective_platform_release,
        workspace_contract_digest=effective.workspace_contract_digest,
        author_subject="auth0|author", change_kind="revise",
        target_tool_name="count-by-layer",
    )
    store.bind_removal_request(
        tenant_id=tenant, change_set_id=removal.change_set_id,
        target_tool_name="count-by-layer",
        expected_catalog_digest=effective.catalog_digest,
    )
    store.transition(
        tenant_id=tenant, change_set_id=removal.change_set_id,
        next_state=ChangeState.STAGING, expected_version=removal.version,
        expected_state=ChangeState.CREATED,
        idempotency_key="pg-removal-worker-staging",
    )

    description = "postgres ordinary stage remains claimable"
    ordinary, created = store.reserve_stage(
        tenant_id=tenant, idempotency_key="pg-removal-worker-ordinary",
        base_commit=BASE, desired_platform_release="platform@sha256:abc",
        workspace_contract_digest=WORKSPACE, author_subject="auth0|author",
        change_kind="create", target_tool_name=None,
        request_description=description,
        request_fingerprint=__import__("hashlib").sha256(
            description.encode()
        ).hexdigest(),
    )
    assert created
    store.transition(
        tenant_id=tenant, change_set_id=ordinary.change_set_id,
        next_state=ChangeState.STAGING, expected_version=ordinary.version,
        expected_state=ChangeState.CREATED,
        idempotency_key="pg-removal-worker-ordinary-staging",
    )

    claimed = store.claim_stage(owner="pg-worker-a", lease_seconds=30)

    assert claimed and claimed.change_set_id == ordinary.change_set_id
    assert store.claim_stage(owner="pg-worker-b", lease_seconds=30) is None
    durable_removal = store.get_change_set(
        tenant_id=tenant, change_set_id=removal.change_set_id
    )
    assert durable_removal.state is ChangeState.STAGING
    assert durable_removal.stage_attempt == 0


def test_postgres_stale_worker_cannot_commit_after_lease_reclaim(store) -> None:
    description = "postgres stale worker fence"
    change, created = store.reserve_stage(
        tenant_id="pg-stage-fence", idempotency_key="pg-stage-fence",
        base_commit=BASE, desired_platform_release="platform@sha256:abc",
        workspace_contract_digest=WORKSPACE, author_subject="auth0|author",
        change_kind="create", target_tool_name=None,
        request_description=description,
        request_fingerprint=__import__("hashlib").sha256(
            description.encode()
        ).hexdigest(),
    )
    assert created
    store.transition(
        tenant_id=change.tenant_id, change_set_id=change.change_set_id,
        next_state=ChangeState.STAGING, expected_version=change.version,
        expected_state=ChangeState.CREATED, idempotency_key="pg-stage-fence-queued",
    )
    first = store.claim_stage(owner="pg-worker-a", lease_seconds=0.01)
    assert first is not None
    time.sleep(0.03)
    second = store.claim_stage(owner="pg-worker-b", lease_seconds=30)
    assert second is not None
    assert second.change_set_id == first.change_set_id
    assert second.stage_attempt == first.stage_attempt + 1

    with pytest.raises(ChangeSetConflictError, match="generation"):
        store.record_staged(
            tenant_id=first.tenant_id,
            change_set_id=first.change_set_id,
            expected_version=first.version,
            idempotency_key="pg-stage-fence-stale-success",
            staged_commit=STAGED,
            catalog_digest=DIGEST,
            platform_release="platform@sha256:abc",
            workspace_contract_digest=WORKSPACE,
            stage_lease_owner="pg-worker-a",
            stage_attempt=first.stage_attempt,
        )
    durable = store.get_change_set(
        tenant_id=second.tenant_id, change_set_id=second.change_set_id
    )
    assert durable.state is ChangeState.STAGING
    assert durable.stage_lease_owner == "pg-worker-b"
    assert durable.stage_attempt == second.stage_attempt

    # A callback is authoritative and may complete the current generation.
    store.record_staged(
        tenant_id=second.tenant_id,
        change_set_id=second.change_set_id,
        expected_version=second.version,
        idempotency_key="pg-stage-fence-callback",
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


def test_r1_postgres_authority_parity(store, tmp_path, monkeypatch) -> None:
    import copy
    import json
    from uuid import uuid4

    from customization_models import IdempotencyReplayError, InvalidTransitionError
    from tool_record_fields import ToolRecordFieldError, GRAPH_INPUT_SOLAR_W1
    from test_customization_record_fields_store import S, SNAPSHOT

    prefix = "r1-" + str(uuid4())
    tenant = prefix + "-tenant"

    def reservation(key, **metadata):
        return store.reserve_stage(
            tenant_id=tenant, idempotency_key=prefix + key, base_commit=BASE,
            desired_platform_release="platform@sha256:abc", workspace_contract_digest=WORKSPACE,
            author_subject="auth0|author", request_description="exact description",
            request_fingerprint="e" * 64, **metadata)

    row, created = reservation("request", request_graph_input=GRAPH_INPUT_SOLAR_W1)
    assert created
    events = store.audit_events(tenant_id=tenant, change_set_id=row.change_set_id)
    assert reservation("request", request_graph_input=GRAPH_INPUT_SOLAR_W1) == (row, False)
    for value in (None, "other"):
        with pytest.raises(IdempotencyReplayError):
            reservation("request", request_graph_input=value)
    for invalid in ("other", "", 1, " " + GRAPH_INPUT_SOLAR_W1):
        with pytest.raises(ToolRecordFieldError):
            reservation("invalid", request_graph_input=invalid)
    assert store.audit_events(tenant_id=tenant, change_set_id=row.change_set_id) == events
    row = store.transition(tenant_id=tenant, change_set_id=row.change_set_id,
                           next_state=ChangeState.STAGING, expected_version=row.version,
                           idempotency_key=prefix + "staging")
    receipt = dict(tenant_id=tenant, change_set_id=row.change_set_id, expected_version=row.version,
                   idempotency_key=prefix + "completion", staged_commit=BASE, catalog_digest=DIGEST,
                   platform_release="platform@sha256:abc", workspace_contract_digest=WORKSPACE)
    with pytest.raises(ChangeSetConflictError):
        store.record_staged(**receipt)
    with pytest.raises(InvalidTransitionError):
        store.transition(tenant_id=tenant, change_set_id=row.change_set_id,
                         next_state=ChangeState.STAGED, expected_version=row.version,
                         idempotency_key=prefix + "bypass")
    before = store.get_change_set(tenant_id=tenant, change_set_id=row.change_set_id)
    events = store.audit_events(tenant_id=tenant, change_set_id=row.change_set_id)
    append = store._append_audit

    def fail(*args, **kwargs):
        append(*args, **kwargs)
        raise RuntimeError("injected record-fields audit failure")

    with monkeypatch.context() as patcher:
        patcher.setattr(store, "_append_audit", fail)
        with pytest.raises(RuntimeError, match="injected record-fields"):
            store.record_staged(**receipt, catalog_record_fields_json=S)
    assert store.get_change_set(tenant_id=tenant, change_set_id=row.change_set_id) == before
    assert store.audit_events(tenant_id=tenant, change_set_id=row.change_set_id) == events
    with store._transaction() as conn:
        conn.execute("UPDATE customization_change_sets SET stage_attempt = 2, stage_lease_owner = ?, "
                     "stage_lease_expires_at = ? WHERE change_set_id = ?",
                     (prefix + "worker", int(time.time() * 1000) + 60000, row.change_set_id))
    with pytest.raises(ChangeSetConflictError):
        store.record_staged(**receipt, catalog_record_fields_json=S,
                            stage_lease_owner="stale-worker", stage_attempt=1)
    staged = store.record_staged(**receipt, catalog_record_fields_json=S,
                                 stage_lease_owner=prefix + "worker", stage_attempt=2)
    assert staged.request_graph_input == GRAPH_INPUT_SOLAR_W1 and staged.catalog_record_fields_json == S
    events = store.audit_events(tenant_id=tenant, change_set_id=row.change_set_id)
    assert store.record_staged(**receipt, catalog_record_fields_json=json.dumps(SNAPSHOT, indent=2)) == staged
    for overrides in ({"catalog_record_fields_json": None}, {"staged_commit": STAGED},
                      {"catalog_digest": "f" * 64}, {"platform_release": "changed"}):
        with pytest.raises(IdempotencyReplayError):
            store.record_staged(**{**receipt, "catalog_record_fields_json": S, **overrides})
    with pytest.raises(ChangeSetConflictError):
        store.record_staged(**{**receipt, "idempotency_key": prefix + "later"}, catalog_record_fields_json=S)
    assert store.audit_events(tenant_id=tenant, change_set_id=row.change_set_id) == events
    for state in (ChangeState.AWAITING_APPROVAL, ChangeState.APPROVED, ChangeState.PUBLISHING):
        staged = store.transition(tenant_id=tenant, change_set_id=row.change_set_id,
                                  next_state=state, expected_version=staged.version,
                                  idempotency_key=prefix + state.value, approver_subject="auth0|approver")
    store.publish(tenant_id=tenant, change_set_id=row.change_set_id, expected_version=staged.version,
                  idempotency_key=prefix + "publish")
    child, _ = reservation("child", base_catalog_change_set_id=row.change_set_id)
    assert child.base_catalog_change_set_id == row.change_set_id
    assert reservation("child", base_catalog_change_set_id=row.change_set_id) == (child, False)
    with pytest.raises(IdempotencyReplayError):
        reservation("child")
    for predecessor, error in (("malformed", ValueError), (str(uuid4()), ChangeSetNotFoundError),
                               (child.change_set_id, ChangeSetConflictError)):
        with pytest.raises(error):
            reservation("refused-predecessor", base_catalog_change_set_id=predecessor)
    self_id = str(uuid4())
    with pytest.raises(ChangeSetConflictError):
        reservation("self-predecessor", change_set_id=self_id, base_catalog_change_set_id=self_id)
    sync_kwargs = dict(tenant_id=tenant, idempotency_key=prefix + "sync", base_commit=BASE,
                       desired_platform_release="platform@sha256:abc", workspace_contract_digest=WORKSPACE,
                       author_subject="auth0|author", request_graph_input=GRAPH_INPUT_SOLAR_W1,
                       base_catalog_change_set_id=row.change_set_id)
    sync = store.create_change_set(**sync_kwargs)
    assert store.create_change_set(**sync_kwargs) == sync
    with pytest.raises(IdempotencyReplayError):
        store.create_change_set(**{**sync_kwargs, "request_graph_input": None})
    legacy_kwargs = {**sync_kwargs, "idempotency_key": prefix + "legacy",
                     "request_graph_input": None, "base_catalog_change_set_id": None}
    legacy = store.create_change_set(**legacy_kwargs)
    with pytest.raises(IdempotencyReplayError):
        store.create_change_set(**{**legacy_kwargs, "request_graph_input": GRAPH_INPUT_SOLAR_W1})
    legacy = stage(store, legacy, prefix + "legacy")
    assert (legacy.request_graph_input, legacy.base_catalog_change_set_id,
            legacy.catalog_record_fields_json) == (None, None, None)

    sqlite = SQLiteCustomizationStore(tmp_path / "record-fields.db")
    sqlite_row = sqlite.create_change_set(
        tenant_id=prefix + "-backfill", idempotency_key=prefix + "backfill", base_commit=BASE,
        desired_platform_release="platform@sha256:abc", workspace_contract_digest=WORKSPACE,
        author_subject="auth0|author", request_graph_input=GRAPH_INPUT_SOLAR_W1)
    sqlite_row = sqlite.transition(tenant_id=sqlite_row.tenant_id, change_set_id=sqlite_row.change_set_id,
                                   next_state=ChangeState.STAGING, expected_version=sqlite_row.version,
                                   idempotency_key=prefix + "backfill-stage")
    sqlite_row = sqlite.record_staged(
        **{**receipt, "tenant_id": sqlite_row.tenant_id, "change_set_id": sqlite_row.change_set_id,
           "expected_version": sqlite_row.version, "idempotency_key": prefix + "backfill-complete"},
        catalog_record_fields_json=S)
    path = Path(sqlite.database_path)
    assert authority_reconcile.reconcile(sqlite_path=path, mode="backfill")["source_incorporated"]
    loaded = store.get_change_set(tenant_id=sqlite_row.tenant_id, change_set_id=sqlite_row.change_set_id)
    assert all(getattr(loaded, column) == getattr(sqlite_row, column)
               for column in authority_reconcile.TABLE_COLUMNS["customization_change_sets"])
    before = _postgres_snapshot(platform_db())
    assert authority_reconcile.reconcile(sqlite_path=path, mode="backfill")["source_incorporated"]
    assert _postgres_snapshot(platform_db()) == before
    with store._transaction() as conn:
        conn.execute("UPDATE customization_change_sets SET request_description = ? WHERE change_set_id = ?",
                     ("conflicting target", sqlite_row.change_set_id))
    before = copy.deepcopy(_postgres_snapshot(platform_db()))
    with pytest.raises(RuntimeError, match="customization authority has a conflicting row in"):
        authority_reconcile.reconcile(sqlite_path=path, mode="backfill")
    assert _postgres_snapshot(platform_db()) == before
    with store._transaction() as conn:
        conn.execute("UPDATE customization_change_sets SET catalog_record_fields_json = ? WHERE change_set_id = ?",
                     ("invalid JSON", row.change_set_id))
    try:
        with pytest.raises(ToolRecordFieldError):
            store.get_change_set(tenant_id=tenant, change_set_id=row.change_set_id)
    finally:
        with store._transaction() as conn:
            conn.execute("UPDATE customization_change_sets SET catalog_record_fields_json = ? WHERE change_set_id = ?",
                         (S, row.change_set_id))
            conn.execute("UPDATE customization_change_sets SET request_description = NULL WHERE change_set_id = ?",
                         (sqlite_row.change_set_id,))
