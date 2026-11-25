from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

import customization_postgres_store as postgres
import customization_store
from customization_models import (
    ChangeSetNotFoundError, ChangeState, IdempotencyReplayError,
)
from customization_store import SQLiteCustomizationStore


class _PostgresTestConnection:
    """Execute adapter SQL on SQLite; this is not a live PostgreSQL test."""

    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=()):
        if sql.startswith("SELECT pg_advisory_xact_lock"):
            return None
        if sql.startswith("SELECT table_name FROM information_schema.tables"):
            self.tables = params[0]
            return self
        sql = sql.replace(postgres._PG_NOW, postgres._SQLITE_NOW)
        return self.connection.execute(sql.replace("%s", "?"), params)

    def fetchall(self):
        return [{"table_name": table} for table in self.tables]


class _PostgresTestDatabase:
    def __init__(self, sqlite_store):
        self.store = sqlite_store

    def assert_schema_current(self):
        pass

    @contextmanager
    def connection(self):
        with self.store._connection() as conn:
            yield _PostgresTestConnection(conn)

    cursor = connection

    @contextmanager
    def transaction(self, *, isolation):
        assert isolation == "read committed"
        with self.store._transaction() as conn:
            yield _PostgresTestConnection(conn)


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path, monkeypatch):
    sqlite_store = SQLiteCustomizationStore(tmp_path / "customization.db")
    sqlite_store.initialize()
    if request.param == "sqlite":
        return sqlite_store
    database = _PostgresTestDatabase(sqlite_store)
    monkeypatch.setattr(postgres.platform_link, "platform_db", lambda: database)
    return postgres.PostgresCustomizationStore()


def reserve(store, *, async_=False, **overrides):
    kwargs = dict(
        tenant_id="tenant-a", idempotency_key="request", base_commit="a" * 40,
        desired_platform_release="platform@sha256:abc",
        workspace_contract_digest="d" * 64, author_subject="author",
    )
    kwargs.update(overrides)
    if async_:
        return store.reserve_stage(
            request_description="description", request_fingerprint="e" * 64,
            **kwargs,
        )[0]
    return store.create_change_set(**kwargs)


@pytest.mark.parametrize("target", [None, "branch"])
@pytest.mark.parametrize("async_", [False, True])
def test_target_roundtrip_and_transition(store, target, async_):
    row = reserve(store, tool_target=target, async_=async_)
    assert row.tool_target == target
    reopened = (postgres.PostgresCustomizationStore() if isinstance(
        store, postgres.PostgresCustomizationStore
    ) else SQLiteCustomizationStore(store.database_path))
    assert reopened.get_change_set(
        tenant_id=row.tenant_id, change_set_id=row.change_set_id,
    ).tool_target == target
    assert reopened.get_change_set_by_idempotency(
        tenant_id=row.tenant_id, idempotency_key="request",
    ).tool_target == target
    changed = reopened.transition(
        tenant_id=row.tenant_id, change_set_id=row.change_set_id,
        next_state=ChangeState.STAGING, expected_version=row.version,
        idempotency_key="stage",
    )
    assert changed.tool_target == target


@pytest.mark.parametrize("async_", [False, True])
def test_target_replay_identity(store, async_):
    row = reserve(store, async_=async_, tool_target="branch")
    assert reserve(store, async_=async_, tool_target="branch") == row
    with pytest.raises(IdempotencyReplayError):
        reserve(store, async_=async_)
    studio = reserve(store, async_=async_, idempotency_key="studio")
    assert reserve(store, async_=async_, idempotency_key="studio", tool_target=None) == studio
    with pytest.raises(IdempotencyReplayError):
        reserve(store, async_=async_, idempotency_key="studio", tool_target="branch")


def test_target_tenant_boundary(store):
    row = reserve(store, tool_target="branch")
    with pytest.raises(ChangeSetNotFoundError):
        store.get_change_set(tenant_id="tenant-b", change_set_id=row.change_set_id)
    other = reserve(store, tenant_id="tenant-b")
    assert other.change_set_id != row.change_set_id
    assert other.tool_target is None


@pytest.mark.parametrize("partial", [False, True])
def test_legacy_sqlite_upgrade_retains_rows(tmp_path, partial):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(customization_store._SCHEMA.replace("  tool_target TEXT,\n", ""))
    # Insert through the previous schema without triggering initialization.
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO customization_change_sets "
            "(change_set_id, tenant_id, idempotency_key, state, version, base_commit, "
            "desired_platform_release, workspace_contract_digest, author_subject) "
            "VALUES ('old', 'tenant-a', 'old', 'created', 0, ?, 'release', ?, 'author')",
            ("a" * 40, "d" * 64),
        )
        if partial:
            conn.execute("ALTER TABLE customization_change_sets ADD COLUMN tool_target TEXT")
    for _ in range(2):
        legacy = SQLiteCustomizationStore(path)
        row = legacy.get_change_set(tenant_id="tenant-a", change_set_id="old")
        assert row.tool_target is None
        assert row.base_commit == "a" * 40 and row.version == 0


def test_mapper_accepts_row_without_target(store):
    row = reserve(store)
    with store._connection() as conn:
        old = dict(conn.execute(
            "SELECT * FROM customization_change_sets WHERE change_set_id = ?",
            (row.change_set_id,),
        ).fetchone())
    old.pop("tool_target")
    assert store._change_set(old).tool_target is None


def test_target_migration_is_nullable_and_additive():
    migration = (Path(__file__).resolve().parents[2] / "platform/migrations" /
                 "0108_customization_change_set_target.sql").read_text()
    assert "ALTER TABLE customization_change_sets ADD COLUMN IF NOT EXISTS tool_target TEXT;" in migration
    assert "NOT NULL" not in migration
    assert "Rollback retains" in migration


def test_postgres_target_insert_translation():
    assert postgres._postgres_sql(
        "INSERT INTO customization_change_sets (tool_target) VALUES (?)"
    ) == "INSERT INTO customization_change_sets (tool_target) VALUES (%s)"
