"""Source-only proofs for migration provenance and selector-aware readiness."""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import re

import pytest

from leaf_platform import db


class _Result:
    def __init__(self, *, one=None):
        self._one = one

    def fetchone(self):
        return self._one


class _LedgerConnection:
    def __init__(self):
        self.ledger = {}
        self.migration_statements = []

    def execute(self, statement, params=None):
        normalized = " ".join(statement.split())
        if normalized.startswith("CREATE TABLE IF NOT EXISTS leaf_schema_migrations"):
            return _Result()
        if normalized.startswith("SELECT pg_advisory_xact_lock"):
            return _Result()
        if normalized.startswith("SELECT sha256 FROM leaf_schema_migrations"):
            digest = self.ledger.get(params["name"])
            return _Result(one=None if digest is None else {"sha256": digest})
        if normalized.startswith("INSERT INTO leaf_schema_migrations"):
            if params["name"] in self.ledger:
                raise AssertionError("duplicate migration ledger insert")
            self.ledger[params["name"]] = params["sha256"]
            return _Result()
        self.migration_statements.append(normalized)
        return _Result()


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def connection(self):
        yield self.conn


def test_apply_migration_records_deterministic_hashes_and_is_idempotent(
    monkeypatch, tmp_path,
):
    package = tmp_path / "platform"
    migrations = package / "migrations"
    migrations.mkdir(parents=True)
    first = migrations / "0001_first.sql"
    second = migrations / "0002_second.sql"
    first.write_bytes(b"CREATE TABLE first_table (id TEXT);\n")
    second.write_bytes(b"CREATE TABLE second_table (id TEXT);\n")
    conn = _LedgerConnection()

    monkeypatch.setattr(db, "_PKG_DIR", package)
    monkeypatch.setattr(db, "get_pool", lambda: _Pool(conn))

    db.apply_migration()
    db.apply_migration()

    assert conn.migration_statements == [
        "CREATE TABLE first_table (id TEXT)",
        "CREATE TABLE second_table (id TEXT)",
    ]
    assert conn.ledger == {
        "0001_first.sql": sha256(first.read_bytes()).hexdigest(),
        "0002_second.sql": sha256(second.read_bytes()).hexdigest(),
    }


def test_apply_migration_fails_before_running_changed_source(monkeypatch, tmp_path):
    package = tmp_path / "platform"
    migrations = package / "migrations"
    migrations.mkdir(parents=True)
    migration = migrations / "0001_first.sql"
    migration.write_text("SELECT 1;\n", encoding="utf-8")
    conn = _LedgerConnection()

    monkeypatch.setattr(db, "_PKG_DIR", package)
    monkeypatch.setattr(db, "get_pool", lambda: _Pool(conn))
    db.apply_migration()
    migration.write_text("SELECT 2;\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="migration hash drift: 0001_first.sql"):
        db.apply_migration()
    assert conn.migration_statements == ["SELECT 1"]


def test_migration_proof_reports_missing_and_hash_drift_without_rejecting_newer_rows():
    manifest = db.migration_manifest()
    applied = [dict(item) for item in manifest[1:]]
    applied[0]["sha256"] = "0" * 64
    applied.append({"name": "9999_future.sql", "sha256": "f" * 64})

    proof = db._migration_proof(applied)

    assert proof["missing_migrations"] == [manifest[0]["name"]]
    assert proof["migration_hash_drift"] == [manifest[1]["name"]]
    assert proof["applied_migration_count"] == len(manifest) - 1


def test_every_postgres_selector_adds_its_complete_authority_contract():
    for selector, values in db._AUTHORITY_SELECTORS.items():
        for value, authority in values.items():
            required = db.required_columns_for_selected_authorities({selector: value})
            for table, columns in db._AUTHORITY_REQUIRED_COLUMNS[authority].items():
                assert columns <= required[table], (selector, value, table)


def test_every_shipped_table_has_a_readiness_contract():
    created = set()
    for path in (db._PKG_DIR / "migrations").glob("[0-9][0-9][0-9][0-9]_*.sql"):
        created.update(re.findall(
            r"CREATE TABLE IF NOT EXISTS\s+([a-z_][a-z0-9_]*)",
            path.read_text(encoding="utf-8"),
            flags=re.IGNORECASE,
        ))
    proven = set(db._REQUIRED_COLUMNS)
    for tables in db._AUTHORITY_REQUIRED_COLUMNS.values():
        proven.update(tables)

    assert created <= proven


def test_legacy_selectors_preserve_the_base_schema_contract():
    required = db.required_columns_for_selected_authorities({
        "LEAF_JOBS_STORE": "legacy",
        "LEAF_CALLBACK_REPLAY_STORE": "legacy",
        "LEAF_SESSIONS_STORE": "legacy",
        "LEAF_AGENT_STORE": "legacy",
        "LEAF_BROKER_STORE": "legacy",
        "LEAF_GUEST_CAP_STORE": "memory",
        "LEAF_DRAWING_STORE": "legacy",
        "LEAF_UPLOAD_STORE": "legacy",
        "LEAF_HARNESS_SESSION_STORE": "file",
    })

    assert required == db._REQUIRED_COLUMNS


class _SchemaCursor:
    def __init__(self, column_rows, ledger_rows, catalog_rows):
        self.column_rows = column_rows
        self.ledger_rows = ledger_rows
        self.catalog_rows = catalog_rows
        self.rows = []
        self.one = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement, _params=None):
        if "information_schema.columns" in statement:
            self.rows = self.column_rows
        elif "FROM pg_constraint" in statement:
            self.rows = self.catalog_rows["constraints"]
        elif "FROM pg_indexes" in statement:
            self.rows = self.catalog_rows["indexes"]
        elif "FROM pg_trigger" in statement:
            self.rows = self.catalog_rows["triggers"]
        elif statement.startswith("SELECT name, sha256"):
            self.rows = self.ledger_rows
        elif statement.startswith("SELECT current_database()"):
            self.one = {"database": "leaf", "schema": "public"}
        else:
            raise AssertionError(f"unexpected schema query: {statement}")

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.one


class _SchemaConnection:
    def __init__(self, column_rows, ledger_rows, catalog_rows=None):
        self.column_rows = column_rows
        self.ledger_rows = ledger_rows
        self.catalog_rows = catalog_rows or {
            "constraints": [], "indexes": [], "triggers": [],
        }

    def cursor(self):
        return _SchemaCursor(self.column_rows, self.ledger_rows, self.catalog_rows)


def _complete_catalog_rows(environ):
    required = db.required_catalog_for_selected_authorities(environ)
    return {
        "constraints": [{"conname": name} for name in required["constraints"]],
        "indexes": [{"indexname": name} for name in required["indexes"]],
        "triggers": [{"tgname": name} for name in required["triggers"]],
    }


def test_selected_authority_missing_table_fails_readiness_without_live_database(
    monkeypatch,
):
    monkeypatch.setenv("LEAF_JOBS_STORE", "postgres")
    required = db.required_columns_for_selected_authorities()
    required[db._MIGRATION_LEDGER_TABLE] = set(db._MIGRATION_LEDGER_COLUMNS)
    absent = "async_job_terminal_conflicts"
    column_rows = [
        {"table_name": table, "column_name": column}
        for table, columns in required.items() if table != absent
        for column in columns
    ]
    ledger_rows = db.migration_manifest()
    conn = _SchemaConnection(
        column_rows, ledger_rows,
        _complete_catalog_rows({"LEAF_JOBS_STORE": "postgres"}),
    )
    monkeypatch.setattr(db, "get_pool", lambda: _Pool(conn))

    status = db.schema_status()

    assert status["ok"] is False
    assert status["missing"][absent] == sorted(
        db._AUTHORITY_REQUIRED_COLUMNS["jobs"][absent])
    assert status["missing_migrations"] == []
    with pytest.raises(RuntimeError, match=absent):
        db.assert_schema_current()


def test_complete_ledger_cannot_bootstrap_past_missing_runtime_constraint(
    monkeypatch,
):
    """An old CREATE IF NOT EXISTS table must not become ready from ledger rows."""
    environ = {"LEAF_UPLOAD_STORE": "postgres"}
    monkeypatch.setenv("LEAF_UPLOAD_STORE", "postgres")
    required = db.required_columns_for_selected_authorities(environ)
    required[db._MIGRATION_LEDGER_TABLE] = set(db._MIGRATION_LEDGER_COLUMNS)
    column_rows = [
        {"table_name": table, "column_name": column}
        for table, columns in required.items()
        for column in columns
    ]
    catalog_rows = _complete_catalog_rows(environ)
    missing_constraint = "drawing_store_versions_tenant_id_drawing_id_fkey"
    catalog_rows["constraints"] = [
        row for row in catalog_rows["constraints"]
        if row["conname"] != missing_constraint
    ]
    conn = _SchemaConnection(column_rows, db.migration_manifest(), catalog_rows)
    monkeypatch.setattr(db, "get_pool", lambda: _Pool(conn))

    status = db.schema_status()

    assert status["missing"] == {}
    assert status["missing_migrations"] == []
    assert status["missing_constraints"] == [missing_constraint]
    assert status["ok"] is False
    with pytest.raises(RuntimeError, match=missing_constraint):
        db.assert_schema_current()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("missing_migrations", ["0007_missing.sql"], "missing migrations"),
        ("migration_hash_drift", ["0008_changed.sql"], "migration hash drift"),
    ],
)
def test_assert_schema_current_fails_explicitly_without_exposing_database_url(
    monkeypatch, field, value, message,
):
    status = {
        "ok": False,
        "missing": {},
        "missing_migrations": [],
        "migration_hash_drift": [],
    }
    status[field] = value
    secret = "postgresql://leaf:do-not-log-this@db.internal/leaf"
    monkeypatch.setenv("DATABASE_URL", secret)
    monkeypatch.setattr(db, "schema_status", lambda: status)

    with pytest.raises(RuntimeError, match=message) as exc_info:
        db.assert_schema_current()

    assert value[0] in str(exc_info.value)
    assert secret not in str(exc_info.value)
    assert "do-not-log-this" not in str(exc_info.value)
