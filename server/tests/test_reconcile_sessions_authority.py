"""Contract tests for scripts/reconcile_sessions_authority.py.

The seven schema traps between the legacy SQLite session store and the
PostgreSQL authority ARE the acceptance surface for this reconciler: an
implementation that ignores them produces a green parity run that is lying.
Every trap below has a test that fails when its guard is removed, so these are
mutation-proven rather than merely present.

The live tests require an explicit DATABASE_URL and skip cleanly otherwise. The
pure-logic tests always run.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
RECONCILE_PATH = ROOT / "scripts" / "reconcile_sessions_authority.py"
SESSIONS_MIGRATION = (
    ROOT / "platform" / "migrations" / "0012_sessions.sql"
).read_text(encoding="utf-8")
INVENTORY = (ROOT / "platform" / "authority-inventory.json").read_text(encoding="utf-8")
APP_DOCKERFILE = ROOT / "deploy" / "Dockerfile.app"
MODEL_MIGRATION = "0019_sessions_model.sql"
SUBJECT_MIGRATION = "0022_sessions_active_turn_subject.sql"

_SPEC = importlib.util.spec_from_file_location(
    "reconcile_sessions_authority", RECONCILE_PATH
)
assert _SPEC and _SPEC.loader
RECONCILE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(RECONCILE)

requires_database = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="PostgreSQL integration test requires explicit DATABASE_URL",
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


class Source:
    """A legacy SQLite session store built through the app's own bootstrap."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.tenant = _unique("tenant")

    def session(self, **overrides):
        row = {
            "session_id": _unique("session"),
            "tenant_id": self.tenant,
            "drawing_id": _unique("drawing"),
            "status": "active",
            "created_at": 1754500000.5,
            "updated_at": 1754500001.5,
            "last_seq": 0,
            "active_turn_id": None,
            "turn_started_at": None,
            "active_turn_tier": None,
            "active_turn_subject": None,
            "model": None,
        }
        row.update(overrides)
        self._insert("sessions", row)
        return row

    def event(self, session_id: str, **overrides):
        row = {
            "session_id": session_id,
            "seq": 1,
            "turn_id": _unique("turn"),
            "type": "message",
            "data_json": json.dumps({"z": 1, "a": {"nested": [1, 2]}}),
            "created_at": 1754500002.25,
        }
        row.update(overrides)
        self._insert("session_events", row)
        return row

    def approval(self, session_id: str, **overrides):
        row = {
            "confirmation_id": _unique("confirmation"),
            "session_id": session_id,
            "tenant_id": self.tenant,
            "turn_id": _unique("turn"),
            "tool": "write_file",
            "params_json": json.dumps({"path": "a.dwg"}),
            "capability": "drawing.write",
            "rationale": "because",
            "kind": "tool",
            "payload_json": None,
            "decided": 1,
            "approved": 1,
            "decided_by": "auth0|alice",
            "created_at": 1754500003.125,
            "expires_at": 1754500903.125,
            "consumed": 1,
        }
        row.update(overrides)
        self._insert("approvals", row)
        return row

    def _insert(self, table: str, row: dict) -> None:
        columns = ",".join(row)
        marks = ",".join("?" for _ in row)
        with RECONCILE._legacy_connection(self.path) as conn:
            conn.execute(
                f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(row.values())
            )
            conn.commit()

    def raw(self, statement: str, args: tuple = ()) -> None:
        with RECONCILE._legacy_connection(self.path) as conn:
            conn.execute(statement, args)
            conn.commit()

    def drop_identity_index(self) -> None:
        """Rewrite `sessions` the way a database predating UNIQUE was written.

        SQLite refuses to DROP the auto-index backing a UNIQUE constraint, so
        the only faithful way to reproduce a legacy store that carries duplicate
        (tenant_id, drawing_id) pairs is to recreate the table without it.
        """
        with RECONCILE._legacy_connection(self.path) as conn:
            columns = ", ".join(
                f"{name} {kind}" for name, kind in (
                    ("session_id", "TEXT PRIMARY KEY"), ("tenant_id", "TEXT NOT NULL"),
                    ("drawing_id", "TEXT NOT NULL"), ("status", "TEXT NOT NULL"),
                    ("created_at", "REAL"), ("updated_at", "REAL"),
                    ("last_seq", "INTEGER DEFAULT 0"), ("active_turn_id", "TEXT"),
                    ("turn_started_at", "REAL"), ("active_turn_tier", "TEXT"),
                    ("active_turn_subject", "TEXT"), ("model", "TEXT"),
                )
            )
            conn.execute("ALTER TABLE sessions RENAME TO sessions_indexed")
            conn.execute(f"CREATE TABLE sessions ({columns})")
            conn.execute("INSERT INTO sessions SELECT * FROM sessions_indexed")
            conn.execute("DROP TABLE sessions_indexed")
            conn.commit()


@pytest.fixture
def source(tmp_path) -> Source:
    return Source(tmp_path / "sessions.db")


@pytest.fixture
def target():
    database = RECONCILE._platform_db()
    database.assert_schema_current()
    return database


def _rows(database, table: str, **where) -> list[dict]:
    clause = " AND ".join(f"{column} = %s" for column in where)
    with database.cursor() as cur:
        cur.execute(f"SELECT * FROM {table} WHERE {clause}", tuple(where.values()))
        return [dict(row) for row in cur.fetchall()]


# --------------------------------------------------------------------------- #
# pure logic: interface, ordering, and normalization
# --------------------------------------------------------------------------- #
def test_interface_mirrors_the_customization_reconciler():
    """The inventory records this exact command shape, so pin it."""
    for name in (
        "_platform_db", "authority_digest", "authority_counts", "_is_locked_error",
        "_ensure_source_schema", "_sqlite_snapshot", "_postgres_snapshot",
        "_insert_snapshot", "_indexed_snapshot", "_missing_source_rows",
        "_target_only_counts", "reconcile", "main",
    ):
        assert hasattr(RECONCILE, name), f"missing {name}"
    parser_error = subprocess.run(
        [sys.executable, str(RECONCILE_PATH), "--mode", "sideways", "--sqlite", "x"],
        capture_output=True, text=True,
    )
    assert parser_error.returncode != 0
    assert "backfill" in parser_error.stderr and "parity" in parser_error.stderr


def test_inventory_declares_both_commands_for_the_sessions_authority():
    inventory = json.loads(INVENTORY)
    entry = next(
        item for item in inventory["authorities"]
        if item["id"] == "app_sessions_and_approvals"
    )
    assert entry["backfill"]["command"] == (
        "python scripts/reconcile_sessions_authority.py --mode backfill"
    )
    assert entry["parity"]["command"] == (
        "python scripts/reconcile_sessions_authority.py --mode parity"
    )
    assert entry["backfill"]["status"] != "new_writes_only"
    assert entry["parity"]["status"] != "runtime_shadow_available"


def test_inventory_records_the_measured_staging_selection():
    """The field this lane closed. It read 'is not recorded' before."""
    inventory = json.loads(INVENTORY)
    entry = next(
        item for item in inventory["authorities"]
        if item["id"] == "app_sessions_and_approvals"
    )
    staging = entry["current_selection"]["staging"]
    assert staging["value"] == "dual_write_shadow"
    assert staging["status"] == "measured"
    # The evidence must name the task definition it was read from.
    assert "leaf-platform-app-alt:27" in staging["evidence"]


def test_app_image_contains_the_reconciliation_command():
    dockerfile = APP_DOCKERFILE.read_text(encoding="utf-8")
    assert (
        "COPY scripts/reconcile_sessions_authority.py "
        "/app/scripts/reconcile_sessions_authority.py"
    ) in dockerfile


def test_default_sqlite_path_tracks_the_app_resolution(monkeypatch, tmp_path):
    """A hardcoded path would reconcile a database nothing writes.

    Staging leaves SESSIONS_DB unset while deploy/README.md documents
    /data/state/sessions.db, so the only always-correct source is whatever
    session_store itself resolved.
    """
    store = RECONCILE._session_store()
    redirected = tmp_path / "redirected.db"
    monkeypatch.setattr(store, "DB_PATH", redirected)
    assert RECONCILE.default_sqlite_path() == redirected


def test_backfill_inserts_sessions_before_events():
    """TRAP 3: app_session_events.session_id REFERENCES app_sessions.

    _insert_snapshot iterates TABLE_COLUMNS directly, so the declaration order
    IS the insert order and reordering it would make every event insert fail.
    """
    assert list(RECONCILE.TABLE_COLUMNS) == [
        "app_sessions", "app_session_events", "app_approvals",
    ]
    assert "REFERENCES app_sessions(session_id) ON DELETE CASCADE" in SESSIONS_MIGRATION


def test_every_reconciled_table_is_owned_by_the_sessions_migration():
    for table in RECONCILE.TABLE_COLUMNS:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in SESSIONS_MIGRATION
    assert set(RECONCILE.LEGACY_TABLES.values()) == {
        "sessions", "session_events", "approvals",
    }


def test_json_columns_are_compared_parsed_not_as_text():
    """TRAP 1: data_json is TEXT in SQLite and JSONB in PostgreSQL."""
    text = '{"b": 1, "a": 2}'
    reordered = {"a": 2, "b": 1}
    assert RECONCILE._normalize(RECONCILE.JSON, text) == reordered
    # A raw string comparison would never match the dict PostgreSQL hands back.
    assert text != reordered


def test_float_columns_are_compared_exactly_with_no_tolerance():
    """TRAP 4: REAL vs DOUBLE PRECISION.

    _shadow_equal compares whole dicts with a bare !=, so a tolerance here would
    certify a pair that a live shadow read then rejects.
    """
    value = 1754500000.123456
    assert RECONCILE._normalize(RECONCILE.FLOAT, value) == value
    one_ulp = value + 2.4e-7
    assert RECONCILE._normalize(RECONCILE.FLOAT, one_ulp) != value


def test_integer_flags_normalize_to_booleans_preserving_null():
    """TRAP 5: approvals.decided/approved/consumed are INTEGER 0/1 in SQLite."""
    assert RECONCILE._normalize(RECONCILE.BOOL, 1) is True
    assert RECONCILE._normalize(RECONCILE.BOOL, 0) is False
    assert RECONCILE._normalize(RECONCILE.BOOL, None) is None


def test_migration_gated_columns_name_their_migration():
    """TRAP 7: model arrives at 0019 and active_turn_subject at 0022."""
    assert RECONCILE._MIGRATION_COLUMNS[("app_sessions", "model")] == MODEL_MIGRATION
    assert (
        RECONCILE._MIGRATION_COLUMNS[("app_sessions", "active_turn_subject")]
        == SUBJECT_MIGRATION
    )


def test_key_sample_is_bounded_and_carries_no_tenant_identifiers():
    keys = [(f"session-{index}", index) for index in range(100)]
    sample = RECONCILE._key_sample(keys)
    assert len(sample) == RECONCILE._KEY_SAMPLE_LIMIT
    assert sample[0] == "session-0:0"


def test_locked_error_detection_covers_both_sqlite_phrasings():
    assert RECONCILE._is_locked_error(sqlite3.OperationalError("database is locked"))
    assert RECONCILE._is_locked_error(sqlite3.OperationalError("database is busy"))
    assert not RECONCILE._is_locked_error(sqlite3.OperationalError("no such table"))


def test_ensure_source_schema_retries_only_lock_errors_then_gives_up(monkeypatch):
    attempts = {"count": 0}

    def always_locked(_path):
        attempts["count"] += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(RECONCILE, "_legacy_connection", always_locked)
    with pytest.raises(RuntimeError, match="stayed locked"):
        RECONCILE._ensure_source_schema(
            Path("unused"), attempts=3, backoff_seconds=0, sleep=lambda _s: None
        )
    assert attempts["count"] == 3

    def other_error(_path):
        raise sqlite3.OperationalError("no such table: sessions")

    monkeypatch.setattr(RECONCILE, "_legacy_connection", other_error)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        RECONCILE._ensure_source_schema(
            Path("unused"), attempts=3, backoff_seconds=0, sleep=lambda _s: None
        )


# --------------------------------------------------------------------------- #
# live: core semantics
# --------------------------------------------------------------------------- #
@requires_database
def test_empty_source_reconciles_and_inserts_nothing(source):
    RECONCILE._ensure_source_schema(source.path)
    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    assert receipt["reconciled"] is True
    assert receipt["inserted_total"] == 0
    assert receipt["source_counts"] == {
        "app_sessions": 0, "app_session_events": 0, "app_approvals": 0,
    }
    parity = RECONCILE.reconcile(sqlite_path=source.path, mode="parity")
    assert parity["reconciled"] is True


@requires_database
def test_parity_refuses_a_source_that_was_never_bootstrapped(source, tmp_path):
    empty = tmp_path / "never-touched.db"
    sqlite3.connect(str(empty)).close()
    with pytest.raises(RuntimeError, match="SQLite sessions source is incomplete"):
        RECONCILE.reconcile(sqlite_path=empty, mode="parity")


@requires_database
def test_backfill_then_parity_passes_and_carries_every_column(source, target):
    session = source.session(
        last_seq=1, active_turn_id="turn-live", turn_started_at=1754500004.75,
        active_turn_tier="hosted_pro", active_turn_subject="auth0|alice",
        model="claude-sonnet-5",
    )
    source.event(session["session_id"])
    approval = source.approval(session["session_id"])

    before = RECONCILE.reconcile(sqlite_path=source.path, mode="parity")
    assert before["reconciled"] is False
    assert before["tables"]["app_sessions"]["source_only_count"] == 1

    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    assert receipt["inserted"] == {
        "app_sessions": 1, "app_session_events": 1, "app_approvals": 1,
    }
    assert receipt["reconciled"] is True

    stored = _rows(target, "app_sessions", session_id=session["session_id"])[0]
    assert stored["active_turn_subject"] == "auth0|alice"
    assert stored["model"] == "claude-sonnet-5"
    assert stored["turn_started_at"] == 1754500004.75

    kept = _rows(target, "app_approvals", confirmation_id=approval["confirmation_id"])[0]
    # TRAP 5: SQLite INTEGER 1 must land as PostgreSQL BOOLEAN true.
    assert kept["decided"] is True and kept["approved"] is True
    assert kept["consumed"] is True
    assert kept["params_json"] == {"path": "a.dwg"}
    assert kept["payload_json"] is None

    after = RECONCILE.reconcile(sqlite_path=source.path, mode="parity")
    assert after["reconciled"] is True


@requires_database
def test_target_only_rows_are_expected_and_never_a_failure(source, target):
    """Staging dual-writes while this runs, so PostgreSQL leads the source."""
    session = source.session()
    RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    with target.transaction() as conn:
        conn.execute(
            "INSERT INTO app_sessions (session_id, tenant_id, drawing_id, status,"
            " created_at, updated_at, last_seq) VALUES (%s,%s,%s,'active',%s,%s,0)",
            (_unique("pg-only"), source.tenant, _unique("drawing"), 1.0, 1.0),
        )
    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="parity")
    assert receipt["reconciled"] is True
    assert receipt["tables"]["app_sessions"]["target_only_count"] >= 1
    assert receipt["tables"]["app_sessions"]["source_only_count"] == 0
    assert session["session_id"]


@requires_database
def test_backfill_is_idempotent(source):
    session = source.session()
    source.event(session["session_id"])
    first = RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    assert first["inserted_total"] == 2
    second = RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    assert second["inserted_total"] == 0
    assert second["target_digest"] == first["target_digest"]


@requires_database
def test_backfill_never_overwrites_a_disagreeing_target_row(source, target):
    """PostgreSQL may hold the newer value, so a conflict is reported, not fixed."""
    session = source.session(status="active")
    RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    with target.transaction() as conn:
        conn.execute(
            "UPDATE app_sessions SET status = 'closed', updated_at = 9999.0"
            " WHERE session_id = %s",
            (session["session_id"],),
        )
    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    assert receipt["reconciled"] is False
    assert receipt["tables"]["app_sessions"]["conflicting_count"] == 1
    assert receipt["inserted"]["app_sessions"] == 0
    survived = _rows(target, "app_sessions", session_id=session["session_id"])[0]
    assert survived["status"] == "closed"
    assert survived["updated_at"] == 9999.0

    parity = RECONCILE.reconcile(sqlite_path=source.path, mode="parity")
    assert parity["reconciled"] is False
    assert parity["tables"]["app_sessions"]["conflicting_count"] == 1


# --------------------------------------------------------------------------- #
# live: read-only proof
# --------------------------------------------------------------------------- #
@requires_database
def test_parity_is_read_only_on_both_stores(source, target):
    session = source.session()
    source.event(session["session_id"])
    RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    with RECONCILE._legacy_connection(source.path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def digests():
        return {
            suffix or ".db": hashlib.sha256(
                Path(str(source.path) + suffix).read_bytes()
            ).hexdigest()
            for suffix in ("", "-wal", "-shm")
            if Path(str(source.path) + suffix).exists()
        }

    before_files = digests()
    before_logical = RECONCILE.authority_digest(RECONCILE._sqlite_snapshot(source.path))
    before_target = RECONCILE.reconcile(
        sqlite_path=source.path, mode="parity"
    )["target_digest"]

    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="parity")

    # The WAL sidecars are transient shared-memory machinery that a READER
    # legitimately creates; the durable database file must not move.
    assert digests()[".db"] == before_files[".db"]
    assert RECONCILE.authority_digest(
        RECONCILE._sqlite_snapshot(source.path)
    ) == before_logical
    assert receipt["target_digest"] == before_target


@requires_database
def test_parity_read_only_is_enforced_not_merely_intended(source, target):
    """SQLite mode=ro and the PostgreSQL READ ONLY transaction both refuse writes."""
    source.session()
    resolved = source.path.resolve()
    connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly database"):
            connection.execute("DELETE FROM sessions")
    finally:
        connection.close()

    with pytest.raises(Exception) as excinfo:
        with target.transaction(isolation="repeatable read", read_only=True) as conn:
            conn.execute(
                "INSERT INTO app_sessions (session_id, tenant_id, drawing_id,"
                " status, created_at, updated_at, last_seq)"
                " VALUES (%s,%s,%s,'active',1.0,1.0,0)",
                (_unique("blocked"), source.tenant, _unique("drawing")),
            )
    assert "read-only" in str(excinfo.value).lower()


# --------------------------------------------------------------------------- #
# live: the seven traps
# --------------------------------------------------------------------------- #
@requires_database
def test_trap1_reordered_json_text_still_reconciles(source, target):
    """TRAP 1: JSONB does not preserve key order, so compare parsed values."""
    session = source.session()
    payload = {"zebra": 1, "alpha": {"inner": [3, 2, 1]}, "middle": "x"}
    source.event(session["session_id"], data_json=json.dumps(payload))
    RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    stored = _rows(target, "app_session_events", session_id=session["session_id"])[0]
    assert stored["data_json"] == payload
    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="parity")
    assert receipt["reconciled"] is True
    assert receipt["tables"]["app_session_events"]["conflicting_count"] == 0


@requires_database
def test_trap2_non_positive_seq_is_reported_never_inserted(source, target):
    """TRAP 2: app_session_events carries CHECK (seq > 0); SQLite does not."""
    session = source.session()
    source.event(session["session_id"], seq=0)
    source.event(session["session_id"], seq=1)
    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    events = receipt["tables"]["app_session_events"]
    assert events["blocked_reasons"] == {"non_positive_seq": 1}
    assert receipt["inserted"]["app_session_events"] == 1
    assert receipt["reconciled"] is False
    stored = _rows(target, "app_session_events", session_id=session["session_id"])
    assert [row["seq"] for row in stored] == [1]
    assert "CHECK (seq > 0)" in SESSIONS_MIGRATION


@requires_database
def test_trap3_orphan_event_is_reported_not_dropped_or_forced(source, target):
    """TRAP 3: the foreign key is mandatory, so a parentless event cannot insert."""
    session = source.session()
    source.event(session["session_id"])
    source.raw(
        "INSERT INTO session_events (session_id,seq,turn_id,type,data_json,created_at)"
        " VALUES (?,?,?,?,?,?)",
        ("no-such-session", 1, "turn-orphan", "message", json.dumps({}), 1.0),
    )
    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    events = receipt["tables"]["app_session_events"]
    assert events["blocked_reasons"] == {"orphan_event": 1}
    assert events["blocked_count"] == 1
    assert receipt["inserted"]["app_session_events"] == 1
    assert receipt["reconciled"] is False
    assert _rows(target, "app_session_events", session_id="no-such-session") == []


@requires_database
def test_trap4_a_float_difference_is_a_conflict_not_a_rounding_allowance(
    source, target
):
    """TRAP 4: REAL vs DOUBLE PRECISION must not become a tolerance."""
    session = source.session(created_at=1754500000.123456)
    RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    stored = _rows(target, "app_sessions", session_id=session["session_id"])[0]
    assert stored["created_at"] == 1754500000.123456

    with target.transaction() as conn:
        conn.execute(
            "UPDATE app_sessions SET created_at = %s WHERE session_id = %s",
            (1754500000.1234562, session["session_id"]),
        )
    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="parity")
    assert receipt["tables"]["app_sessions"]["conflicting_count"] == 1
    assert receipt["reconciled"] is False


@requires_database
def test_trap5_integer_flags_insert_into_boolean_columns(source, target):
    """TRAP 5: an uncoerced INTEGER cannot be inserted into a BOOLEAN column."""
    session = source.session()
    undecided = source.approval(
        session["session_id"], decided=0, approved=None, consumed=0,
    )
    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    assert receipt["inserted"]["app_approvals"] == 1
    stored = _rows(
        target, "app_approvals", confirmation_id=undecided["confirmation_id"]
    )[0]
    assert stored["decided"] is False
    assert stored["approved"] is None
    assert stored["consumed"] is False
    assert RECONCILE.reconcile(
        sqlite_path=source.path, mode="parity"
    )["reconciled"] is True


@requires_database
def test_trap6_duplicate_identity_is_reported_never_forced(source, target):
    """TRAP 6: UNIQUE (tenant_id, drawing_id) on both sides."""
    drawing = _unique("shared-drawing")
    source.session(drawing_id=drawing)
    source.drop_identity_index()
    source.raw(
        "INSERT INTO sessions (session_id,tenant_id,drawing_id,status,created_at,"
        "updated_at,last_seq) VALUES (?,?,?,?,?,?,?)",
        (_unique("session"), source.tenant, drawing, "active", 1.0, 1.0, 0),
    )
    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    sessions = receipt["tables"]["app_sessions"]
    assert sessions["blocked_reasons"] == {"duplicate_identity": 2}
    assert receipt["inserted"]["app_sessions"] == 0
    assert receipt["reconciled"] is False
    assert _rows(target, "app_sessions", tenant_id=source.tenant, drawing_id=drawing) == []
    assert "UNIQUE (tenant_id, drawing_id)" in SESSIONS_MIGRATION


@requires_database
def test_trap6_identity_already_held_by_another_session_is_reported(source, target):
    """TRAP 6: the collision can also be against a row PostgreSQL already holds."""
    drawing = _unique("contested-drawing")
    with target.transaction() as conn:
        conn.execute(
            "INSERT INTO app_sessions (session_id, tenant_id, drawing_id, status,"
            " created_at, updated_at, last_seq) VALUES (%s,%s,%s,'active',1.0,1.0,0)",
            (_unique("incumbent"), source.tenant, drawing),
        )
    source.session(drawing_id=drawing)
    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    sessions = receipt["tables"]["app_sessions"]
    assert sessions["blocked_reasons"] == {"identity_conflict": 1}
    assert receipt["inserted"]["app_sessions"] == 0
    assert receipt["reconciled"] is False


@requires_database
@pytest.mark.parametrize(
    "column,migration",
    [("model", MODEL_MIGRATION), ("active_turn_subject", SUBJECT_MIGRATION)],
)
def test_trap7_a_target_behind_its_migration_names_the_migration(
    source, target, column, migration
):
    """TRAP 7: fail with the migration to apply, not a driver error."""
    source.session()
    with target.transaction() as conn:
        conn.execute(f"ALTER TABLE app_sessions DROP COLUMN {column}")
    try:
        with pytest.raises(RuntimeError) as excinfo:
            RECONCILE.reconcile(sqlite_path=source.path, mode="parity")
        assert migration in str(excinfo.value)
        assert column in str(excinfo.value)
    finally:
        with target.transaction() as conn:
            conn.execute(f"ALTER TABLE app_sessions ADD COLUMN {column} TEXT")
    # The restore must leave the reconciler working again, which proves the
    # failure came from the missing column rather than a poisoned connection.
    assert RECONCILE.reconcile(sqlite_path=source.path, mode="parity")["tables"]


# --------------------------------------------------------------------------- #
# live: NOT NULL and CHECK constraints the legacy schema does not carry
# --------------------------------------------------------------------------- #
@requires_database
def test_null_in_a_target_not_null_column_is_reported(source, target):
    session = source.session()
    source.raw("UPDATE sessions SET created_at = NULL WHERE session_id = ?",
               (session["session_id"],))
    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    assert receipt["tables"]["app_sessions"]["blocked_reasons"] == {
        "null_violation:created_at": 1
    }
    assert receipt["inserted"]["app_sessions"] == 0
    assert receipt["reconciled"] is False


@requires_database
def test_consumed_without_a_decision_is_reported(source, target):
    """app_approvals carries CHECK (NOT consumed OR decided); SQLite does not."""
    session = source.session()
    source.approval(session["session_id"], decided=0, approved=None, consumed=1)
    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    assert receipt["tables"]["app_approvals"]["blocked_reasons"] == {
        "consumed_without_decision": 1
    }
    assert receipt["inserted"]["app_approvals"] == 0
    assert "CHECK (NOT consumed OR decided)" in SESSIONS_MIGRATION


# --------------------------------------------------------------------------- #
# live: command line contract
# --------------------------------------------------------------------------- #
@requires_database
def test_cli_exit_codes_distinguish_pass_failure_and_error(source, tmp_path):
    session = source.session()
    source.event(session["session_id"])

    def run(mode: str, path: Path):
        return subprocess.run(
            [sys.executable, str(RECONCILE_PATH), "--mode", mode, "--sqlite", str(path)],
            capture_output=True, text=True, env=dict(os.environ),
        )

    failed = run("parity", source.path)
    assert failed.returncode == 1
    receipt = json.loads(failed.stdout)
    assert receipt["reconciled"] is False
    assert receipt["tables"]["app_sessions"]["source_only_count"] == 1

    assert run("backfill", source.path).returncode == 0
    passed = run("parity", source.path)
    assert passed.returncode == 0
    assert json.loads(passed.stdout)["reconciled"] is True

    unreadable = run("parity", tmp_path / "absent.db")
    assert unreadable.returncode == 2
    assert "sessions authority reconciliation failed" in unreadable.stderr


@requires_database
def test_receipt_never_discloses_tenant_or_payload_values(source):
    session = source.session()
    source.event(session["session_id"], seq=0, data_json=json.dumps({"secret": "leak"}))
    source.approval(session["session_id"], params_json=json.dumps({"token": "leak"}))
    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    encoded = json.dumps(receipt)
    assert "leak" not in encoded
    assert source.tenant not in encoded
    assert session["drawing_id"] not in encoded
