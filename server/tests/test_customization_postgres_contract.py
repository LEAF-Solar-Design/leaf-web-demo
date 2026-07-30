from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import customization_postgres_store
from customization_postgres_store import _postgres_sql


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (
    ROOT / "platform" / "migrations" / "0020_customization_authority.sql"
).read_text(encoding="utf-8")
INVENTORY = (
    ROOT / "platform" / "authority-inventory.json"
).read_text(encoding="utf-8")
RECONCILE_PATH = ROOT / "scripts" / "reconcile_customization_authority.py"
APP_DOCKERFILE = ROOT / "deploy" / "Dockerfile.app"
SPEC = importlib.util.spec_from_file_location("customization_reconcile", RECONCILE_PATH)
assert SPEC and SPEC.loader
RECONCILE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECONCILE)


def test_sql_translation_covers_the_frozen_sqlite_dialect() -> None:
    translated = _postgres_sql(
        "INSERT OR IGNORE INTO customization_publication_requests "
        "(tenant_id, change_set_id) VALUES (?, ?)"
    )
    assert "INSERT OR IGNORE" not in translated
    assert "VALUES (%s, %s) ON CONFLICT DO NOTHING" in translated

    timestamp = _postgres_sql(
        "UPDATE customization_change_sets SET "
        "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE tenant_id = ?"
    )
    assert "strftime" not in timestamp
    assert "clock_timestamp()" in timestamp
    assert "tenant_id = %s" in timestamp

    ordered = _postgres_sql(
        "SELECT * FROM customization_confirmations "
        "ORDER BY created_at DESC, rowid DESC"
    )
    assert "rowid" not in ordered
    assert "confirmation_id DESC" in ordered


def test_postgres_store_initialization_is_validation_only() -> None:
    source = Path(customization_postgres_store.__file__).read_text(encoding="utf-8")
    assert "assert_schema_current()" in source
    assert "information_schema.tables" in source
    assert "CREATE TABLE" not in source
    assert "apply_migration" not in source


def test_migration_owns_every_reconciliation_table() -> None:
    for table in RECONCILE.TABLE_COLUMNS:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in MIGRATION
    assert "customization_recovery_idx" in MIGRATION
    assert "customization_confirmation_lookup_idx" in MIGRATION


def test_reconciliation_digest_is_order_stable_and_data_sensitive() -> None:
    first = {
        table: [] for table in RECONCILE.TABLE_COLUMNS
    }
    second = dict(reversed(list(first.items())))
    assert RECONCILE.authority_digest(first) == RECONCILE.authority_digest(second)

    second["customization_change_sets"] = [{"tenant_id": "tenant-a"}]
    assert RECONCILE.authority_digest(first) != RECONCILE.authority_digest(second)


def test_reconciliation_source_writes_are_schema_init_only() -> None:
    """The source contract, updated with the live 2026-07-28 activation fix.

    The script now runs the app's own idempotent schema initialization and
    guarded legacy migrations on the SQLite source before snapshotting
    (docs/POSTGRES-CUTOVER.md discloses this), because a never-touched or
    pre-migration store otherwise fails as "incomplete" and an unmigrated
    legacy store could be under-read. The SNAPSHOT itself must stay
    read-only, and the script must contain no destructive SQL of its own;
    the only sanctioned write path is SQLiteCustomizationStore.initialize().
    """
    source = RECONCILE_PATH.read_text(encoding="utf-8")
    assert "?mode=ro" in source
    assert "DELETE FROM" not in source
    assert "TRUNCATE " not in source
    assert "DROP TABLE" not in source
    assert "Target-only rows are retained" in source
    assert '"source_incorporated": True' in source
    assert '"exact_equal": exact_equal' in source
    assert "conflicting row" in source
    assert "duplicate primary key" in source
    assert "pg_advisory_xact_lock" in source
    assert "SQLiteCustomizationStore" in source
    assert ".initialize()" in source
    # The ensure step must run before the snapshot inside reconcile().
    body = source[source.index("def reconcile(") :]
    assert body.index("_ensure_source_schema(") < body.index("_sqlite_snapshot(")


def test_inventory_declares_postgres_selector_backfill_and_parity() -> None:
    assert '"name": "LEAF_CUSTOMIZATION_STORE"' in INVENTORY
    assert "scripts/reconcile_customization_authority.py --mode backfill" in INVENTORY
    assert "scripts/reconcile_customization_authority.py --mode parity" in INVENTORY


def test_app_image_contains_the_reconciliation_command() -> None:
    dockerfile = APP_DOCKERFILE.read_text(encoding="utf-8")
    assert (
        "COPY scripts/reconcile_customization_authority.py "
        "/app/scripts/reconcile_customization_authority.py"
    ) in dockerfile


@pytest.mark.parametrize("mode", ["backfill", "parity"])
def test_authority_counts_cover_every_table(mode: str) -> None:
    snapshot = {table: [] for table in RECONCILE.TABLE_COLUMNS}
    receipt = {
        "mode": mode,
        "counts": RECONCILE.authority_counts(snapshot),
    }
    assert set(receipt["counts"]) == set(RECONCILE.TABLE_COLUMNS)
