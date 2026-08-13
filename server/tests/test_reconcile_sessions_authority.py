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
import re
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

    def event(self, session_id: str, *, advance_last_seq: bool = True, **overrides):
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
        if advance_last_seq and row["seq"] is not None:
            # append_event allocates seq as last_seq+1 and writes both in one
            # transaction, so a faithful source never has an event outrunning
            # its session's last_seq. Tests that want that corruption pass
            # advance_last_seq=False.
            self.raw(
                "UPDATE sessions SET last_seq = MAX(last_seq, ?) WHERE session_id = ?",
                (row["seq"], session_id),
            )
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
        "python /app/scripts/reconcile_sessions_authority.py --mode backfill"
    )
    assert entry["parity"]["command"] == (
        "python /app/scripts/reconcile_sessions_authority.py --mode parity"
    )
    assert entry["backfill"]["status"] != "new_writes_only"
    assert entry["parity"]["status"] != "runtime_shadow_available"


def test_inventory_records_the_measured_staging_selection():
    """The field this lane closed. It read 'is not recorded' before.

    WHAT CHANGED HERE, and why it is not a loosened gate. This test used to
    assert the literal value ``dual_write_shadow`` and the literal revision
    ``leaf-platform-app-alt:27``. Both were true when written and both are now
    false: the P4A typed cutover moved the selector to ``postgres`` AND moved
    the serving color, so a snapshot pin failed twice over. Worse, it made the
    stale record load-bearing, so the next person to correct the inventory reded
    a test for doing the right thing, which is how a false record survives.

    So the snapshot is gone and the PROPERTY it existed to protect stays: the
    staging selection is a real measurement, its value is one this authority's
    own selector declares, and its evidence cites BOTH layers -- a task
    definition revision and an image digest -- because a task definition alone
    cannot establish a selection when an image ENV survives an absent override.
    That last half is a strengthening: the old record cited only a revision, so
    a revert to the pre-branch evidence now reds where it used to pass.

    None of it decays on remeasurement. The status assertion admits ``verified``
    as well, because the vocabulary defines that as "measured AND reconciled",
    so pinning ``measured`` alone would red the one upgrade this record can
    legitimately receive -- which is the same snapshot trap, one field over.
    Blanking the evidence, dropping either layer's citation, downgrading the
    status, or recording a mode the selector does not allow all still red.
    """
    inventory = json.loads(INVENTORY)
    entry = next(
        item for item in inventory["authorities"]
        if item["id"] == "app_sessions_and_approvals"
    )
    staging = entry["current_selection"]["staging"]
    assert staging["status"] in {"measured", "verified"}
    selector = next(
        item for item in entry["selectors"]
        if item["name"] == "LEAF_SESSIONS_STORE"
    )
    assert staging["value"] in selector["allowed_modes"]
    # Both layers, or the record does not support its own status.
    assert re.search(r"leaf-platform-app(-alt)?:\d+", staging["evidence"])
    assert "@sha256:" in staging["evidence"]


def test_app_image_contains_the_reconciliation_command():
    dockerfile = APP_DOCKERFILE.read_text(encoding="utf-8")
    assert (
        "COPY scripts/reconcile_sessions_authority.py "
        "/app/scripts/reconcile_sessions_authority.py"
    ) in dockerfile


# The WORKDIR-vs-COPY guard that used to live here now covers EVERY authority
# rather than just this one, so it sits with the other image-wiring contracts:
# see test_documented_authority_commands_resolve_in_the_image in
# server/tests/test_postgres_container_wiring.py.


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
    """TRAP 1: data_json is TEXT in SQLite and JSONB in PostgreSQL.

    JSONB does not preserve key order, so the two stores hand the same value
    back as differently ordered text. The COMPARISON must parse; _normalize
    deliberately keeps the raw text, because the insert path writes it through
    verbatim rather than re-serializing it.
    """
    def compare(text):
        return RECONCILE._comparable_row("app_session_events", {
            "session_id": "s", "seq": 1, "turn_id": None, "type": "message",
            "data_json": text, "created_at": 1.0,
        })

    assert compare('{"b": 1, "a": 2}') == compare('{"a": 2, "b": 1}')
    assert compare('{"b": 1, "a": 2}') != compare('{"b": 1, "a": 3}')
    assert RECONCILE._normalize(RECONCILE.JSON, '{"b": 1, "a": 2}') == '{"b": 1, "a": 2}'


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


def test_key_sample_is_bounded_and_hashed_never_raw():
    """confirmation_id is caller-supplied and can carry identifying text."""
    keys = [(f"session-{index}", index) for index in range(100)]
    sample = RECONCILE._key_sample(keys)
    assert len(sample) == RECONCILE._KEY_SAMPLE_LIMIT
    assert "session-0" not in " ".join(sample)
    expected = hashlib.sha256(b"session-0:0").hexdigest()[:16]
    assert sample[0] == expected


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
    """A DUAL-WRITING source leads to target-only rows, so they are not a failure.

    Named by the condition rather than by the environment, deliberately. This
    said "Staging dual-writes while this runs", which was true when written and
    stopped being true when staging's LEAF_SESSIONS_STORE went to `postgres`:
    only `dual_write` and `dual_write_shadow` are in
    `session_store._DUAL_WRITE_MODES`. The tolerance under test is a property of
    the MODE, and reading it as a property of staging is what makes a frozen
    source against a growing target look like a clean parity run.
    """
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
# live: findings from the sol-critic review of PR #488
# --------------------------------------------------------------------------- #
@requires_database
def test_events_under_a_conflicting_session_are_never_inserted(source, target):
    """A disputed parent must not receive children.

    If the source session row and the target row sharing its session_id
    disagree about tenant_id, inserting the source's events would file one
    tenant's events under another tenant's session.
    """
    session = source.session()
    RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    other_tenant = _unique("other-tenant")
    with target.transaction() as conn:
        conn.execute(
            "UPDATE app_sessions SET tenant_id = %s, drawing_id = %s"
            " WHERE session_id = %s",
            (other_tenant, _unique("other-drawing"), session["session_id"]),
        )
    source.event(session["session_id"], seq=1)

    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    events = receipt["tables"]["app_session_events"]
    assert "conflicting_parent_session" in events["blocked_reasons"]
    assert receipt["inserted"]["app_session_events"] == 0
    assert receipt["reconciled"] is False
    assert _rows(target, "app_session_events", session_id=session["session_id"]) == []
    # The disputed session itself is still only reported, never rewritten.
    survived = _rows(target, "app_sessions", session_id=session["session_id"])[0]
    assert survived["tenant_id"] == other_tenant


@requires_database
def test_last_seq_out_of_step_with_its_event_stream_is_reported(source, target):
    """The eighth invariant: seq is allocated as last_seq+1, so last_seq must
    dominate the stream. Backfilling a stale last_seq makes both stores agree
    on a broken state that collides on the first turn after the flip."""
    session = source.session(last_seq=0)
    source.event(session["session_id"], seq=1, advance_last_seq=False)
    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    sessions = receipt["tables"]["app_sessions"]
    assert sessions["blocked_reasons"] == {"last_seq_mismatch": 1}
    assert receipt["inserted"]["app_sessions"] == 0
    assert receipt["reconciled"] is False
    assert _rows(target, "app_sessions", session_id=session["session_id"]) == []


@requires_database
def test_json_integer_is_not_certified_equal_to_json_true(source, target):
    """Python says 1 == True, so plain equality over parsed JSON would certify
    two distinguishable JSONB states as equal and exit 0."""
    session = source.session()
    source.event(session["session_id"], seq=1, data_json=json.dumps({"flag": 1}))
    RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    assert RECONCILE.reconcile(
        sqlite_path=source.path, mode="parity"
    )["reconciled"] is True

    with target.transaction() as conn:
        conn.execute(
            "UPDATE app_session_events SET data_json = %s::jsonb"
            " WHERE session_id = %s",
            (json.dumps({"flag": True}), session["session_id"]),
        )
    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="parity")
    assert receipt["tables"]["app_session_events"]["conflicting_count"] == 1
    assert receipt["reconciled"] is False


def _encode_json(text):
    return json.dumps(
        RECONCILE._comparable_value(RECONCILE.JSON, text), sort_keys=True
    )


@pytest.mark.parametrize(
    "document,marker",
    [
        ("1", '["num","1E0"]'),
        ("0", '["num","0"]'),
        ('"x"', '["str","x"]'),
        ("true", '["bool",true]'),
        ("null", '["null"]'),
        ('{"a":1}', '["obj",[["a",["num","1E0"]]]]'),
        ('[1]', '["arr",[["num","1E0"]]]'),
        ('{"a":1}', '{"a":["num","1E0"]}'),
    ],
)
def test_a_document_cannot_impersonate_the_type_tags(document, marker):
    """The canonical form's own markers are ordinary JSON.

    Tagging only numbers meant a document literally containing the array
    ["num","1E0"] encoded identically to the number 1. Every JSON type is
    tagged recursively so no payload can forge a tag.
    """
    assert _encode_json(document) != _encode_json(marker)


@pytest.mark.parametrize(
    "left,right",
    [
        ("0.10", "0.1"), ("1e2", "100"), ("100.0", "100"), ("1E+2", "100"),
        ("-0", "0"), ("-0.0", "0"), ("0.0", "0"), ("1.000", "1"),
        ('{"a":1e2}', '{"a":100}'),
        # Equivalence must still hold BEYOND the context precision, where the
        # canonical form is built by hand rather than by normalize().
        ("1.00000000000000000000000000001000",
         "1.00000000000000000000000000001"),
        ("0.00000000000000000000000000000000000000", "0"),
    ],
)
def test_equivalent_json_numbers_never_read_as_a_conflict(left, right):
    """A gate that cries wolf gets overridden.

    parse_float alone leaves JSON integers as Python int, so 100 and 1e2 -- the
    SAME jsonb number written two ways -- would encode differently and report a
    conflict that does not exist. Every number goes through Decimal and one
    canonical form; -0 is folded to 0 because normalize() keeps the sign.
    """
    assert _encode_json(left) == _encode_json(right)


@pytest.mark.parametrize(
    "left,right",
    [
        ("0.1", "0.10000000000000001"),
        ("1", "1.0000000000000000000000001"),
        ("100", "1000"),
        # More than 28 significant digits: Decimal.normalize() rounds to the
        # active context precision before stripping zeros, so both of these
        # come back as "1" and would certify equal.
        ("1.00000000000000000000000000001", "1.00000000000000000000000000002"),
        ('{"n":1.00000000000000000000000000001}',
         '{"n":1.00000000000000000000000000002}'),
        ("-1.00000000000000000000000000001", "1.00000000000000000000000000001"),
        ('{"a":0.1}', '{"a":0.10000000000000001}'),
        ("1", '"1"'),
        ("true", '"true"'),
        ("null", '"null"'),
    ],
)
def test_different_json_values_never_certify_equal(left, right):
    assert _encode_json(left) != _encode_json(right)


def test_nan_and_infinity_are_refused_as_invalid_json():
    """jsonb rejects them; Python's json.loads accepts them by default."""
    for text in ("NaN", "Infinity", "-Infinity", '{"a": NaN}'):
        with pytest.raises(ValueError):
            RECONCILE._json_scalars(text)
    assert RECONCILE._invalid_json_defects({"data_json": "NaN"}, ("data_json",)) == [
        "invalid_json:data_json"
    ]


def test_comparable_values_keep_distinguishable_states_apart():
    # Compare the ENCODED form, which is what _comparable_row actually uses:
    # the tagged structures still contain Python values, where 1 == True.
    def encode(kind, value):
        return json.dumps(
            RECONCILE._comparable_value(kind, value),
            sort_keys=True, default=RECONCILE._encode_exact,
        )

    assert encode(RECONCILE.JSON, None) != encode(RECONCILE.JSON, "null")
    assert encode(RECONCILE.JSON, '{"f": 1}') != encode(RECONCILE.JSON, '{"f": true}')
    assert encode(RECONCILE.BOOL, 1) != encode(RECONCILE.INT, 1)
    # JSON numbers are parsed losslessly: json.loads would make both 0.1.
    assert encode(RECONCILE.JSON, "0.1") != \
        encode(RECONCILE.JSON, "0.10000000000000001")
    # ... but trailing-zero noise is the SAME jsonb number, not a conflict.
    assert encode(RECONCILE.JSON, "0.10") == encode(RECONCILE.JSON, "0.1")
    # Nested, not only scalar.
    assert encode(RECONCILE.JSON, '{"a":[{"b":0.1}]}') != \
        encode(RECONCILE.JSON, '{"a":[{"b":0.10000000000000001}]}')
    # The JSON literal null is the text "null" and stays distinct from SQL NULL.
    assert RECONCILE._normalize(RECONCILE.JSON, "null") == "null"
    assert RECONCILE._normalize(RECONCILE.JSON, None) is None
    assert RECONCILE._insert_value(RECONCILE.JSON, "null") == "null"
    assert RECONCILE._insert_value(RECONCILE.JSON, None) is None
    # And the row-level encoder, which is the real comparison surface.
    left = RECONCILE._comparable_row("app_session_events", {
        "session_id": "s", "seq": 1, "turn_id": None, "type": "message",
        "data_json": '{"f": 1}', "created_at": 1.0,
    })
    right = RECONCILE._comparable_row("app_session_events", {
        "session_id": "s", "seq": 1, "turn_id": None, "type": "message",
        "data_json": '{"f": true}', "created_at": 1.0,
    })
    assert left != right
    assert left != right


@requires_database
def test_a_non_boolean_legacy_flag_is_reported_never_coerced(source, target):
    """bool(2) is True, so coercion alone would certify 2 equal to TRUE."""
    session = source.session()
    source.approval(session["session_id"], decided=2)
    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    assert receipt["tables"]["app_approvals"]["blocked_reasons"] == {
        "non_boolean_flag:decided": 1
    }
    assert receipt["inserted"]["app_approvals"] == 0


@requires_database
def test_json_null_data_is_backfillable_and_not_a_null_violation(source, target):
    """'null'::jsonb satisfies data_json NOT NULL; SQL NULL does not."""
    session = source.session()
    source.event(session["session_id"], seq=1, data_json="null")
    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    assert receipt["tables"]["app_session_events"]["blocked_reasons"] == {}
    assert receipt["inserted"]["app_session_events"] == 1
    assert RECONCILE.reconcile(
        sqlite_path=source.path, mode="parity"
    )["reconciled"] is True

    source.raw("UPDATE session_events SET data_json = NULL WHERE session_id = ?",
               (session["session_id"],))
    blocked = RECONCILE.reconcile(sqlite_path=source.path, mode="parity")
    assert blocked["tables"]["app_session_events"]["conflicting_count"] == 1


@requires_database
def test_a_source_that_moves_mid_run_is_detected_not_certified(source, monkeypatch):
    """The two snapshots are not one atomic read.

    The app can commit a legacy write after the SQLite snapshot and fail before
    its PostgreSQL mirror, leaving a source-only row that neither half of the
    comparison contains. Re-reading the source under the target transaction
    turns that silent false pass into a reported failure.
    """
    session = source.session()
    RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    assert RECONCILE.reconcile(
        sqlite_path=source.path, mode="parity"
    )["reconciled"] is True

    original = RECONCILE._postgres_snapshot
    fired = {"count": 0}

    def snapshot_then_write(connection):
        result = original(connection)
        if fired["count"] == 0:
            fired["count"] = 1
            # Stands in for the live app committing to SQLite mid-run.
            source.event(session["session_id"], seq=1)
        return result

    monkeypatch.setattr(RECONCILE, "_postgres_snapshot", snapshot_then_write)
    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="parity")
    assert receipt["source_stable"] is False
    assert receipt["reconciled"] is False


@requires_database
def test_an_event_whose_parent_exists_only_in_the_target_is_not_inserted(
    source, target
):
    """A matching opaque session_id is not proof of ownership.

    If SQLite holds an event for session `s` but no session row for `s`, and
    PostgreSQL holds a target-only session `s` belonging to another tenant,
    filing the event under it would invent a parent the source never claimed.
    """
    orphan_parent = _unique("target-only-session")
    other_tenant = _unique("other-tenant")
    with target.transaction() as conn:
        conn.execute(
            "INSERT INTO app_sessions (session_id, tenant_id, drawing_id, status,"
            " created_at, updated_at, last_seq) VALUES (%s,%s,%s,'active',1.0,1.0,0)",
            (orphan_parent, other_tenant, _unique("other-drawing")),
        )
    source.raw(
        "INSERT INTO session_events (session_id,seq,turn_id,type,data_json,created_at)"
        " VALUES (?,?,?,?,?,?)",
        (orphan_parent, 1, "turn-x", "message", json.dumps({"a": 1}), 1.0),
    )
    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    assert receipt["tables"]["app_session_events"]["blocked_reasons"] == {
        "orphan_event": 1
    }
    assert receipt["inserted"]["app_session_events"] == 0
    assert receipt["reconciled"] is False
    assert _rows(target, "app_session_events", session_id=orphan_parent) == []


@requires_database
def test_a_jsonb_string_is_not_certified_equal_to_a_jsonb_boolean(source, target):
    """psycopg decodes JSONB '"true"' to the Python str "true".

    Feeding that back through _json_value -- which exists to parse SQLite TEXT --
    re-parses it into the boolean True, collapsing two different JSONB values.
    """
    session = source.session()
    source.event(session["session_id"], seq=1, data_json=json.dumps(True))
    RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    assert RECONCILE.reconcile(
        sqlite_path=source.path, mode="parity"
    )["reconciled"] is True

    with target.transaction() as conn:
        # The JSON *string* "true", not the JSON boolean true.
        conn.execute(
            "UPDATE app_session_events SET data_json = %s::jsonb WHERE session_id = %s",
            (json.dumps("true"), session["session_id"]),
        )
    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="parity")
    assert receipt["tables"]["app_session_events"]["conflicting_count"] == 1
    assert receipt["reconciled"] is False


@requires_database
@pytest.mark.parametrize(
    "source_json,target_json",
    [
        ("0.10000000000000001", "0.1"),
        ('{"a": [{"b": 0.10000000000000001}]}', '{"a": [{"b": 0.1}]}'),
    ],
    ids=["scalar", "nested"],
)
def test_json_numeric_precision_is_not_collapsed(
    source, target, source_json, target_json
):
    """PostgreSQL stores JSONB numbers as arbitrary-precision numeric.

    json.loads decodes every JSON number to a binary float, so 0.1 and
    0.10000000000000001 both become Python 0.1 and would certify equal.
    """
    session = source.session()
    source.event(session["session_id"], seq=1, data_json=source_json)
    RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")

    # The backfill itself must not have rewritten the number.
    stored = _rows(target, "app_session_events", session_id=session["session_id"])[0]
    assert RECONCILE.reconcile(
        sqlite_path=source.path, mode="parity"
    )["reconciled"] is True, f"round-tripped away from {source_json}: {stored}"

    with target.transaction() as conn:
        conn.execute(
            "UPDATE app_session_events SET data_json = %s::jsonb WHERE session_id = %s",
            (target_json, session["session_id"]),
        )
    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="parity")
    assert receipt["tables"]["app_session_events"]["conflicting_count"] == 1
    assert receipt["reconciled"] is False


@requires_database
def test_unparseable_json_is_reported_not_thrown(source, target):
    """The insert passes JSON text through verbatim, so a non-JSON source value
    would fail at INSERT time. Report it as unbackfillable instead."""
    session = source.session()
    source.event(session["session_id"], seq=1, data_json="{not json at all")
    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    assert receipt["tables"]["app_session_events"]["blocked_reasons"] == {
        "invalid_json:data_json": 1
    }
    assert receipt["inserted"]["app_session_events"] == 0
    assert receipt["reconciled"] is False


@requires_database
def test_type_drift_on_a_row_present_in_both_stores_is_a_conflict(source, target):
    """The raw-type defect codes only run over source-only rows, so the
    COMPARISON itself must not coerce, or a shared row drifts undetected."""
    session = source.session()
    RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    assert RECONCILE.reconcile(
        sqlite_path=source.path, mode="parity"
    )["reconciled"] is True

    # SQLite is dynamically typed; the target holds BIGINT 0.
    source.raw("UPDATE sessions SET last_seq = 0.5 WHERE session_id = ?",
               (session["session_id"],))
    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="parity")
    assert receipt["tables"]["app_sessions"]["conflicting_count"] == 1
    assert receipt["reconciled"] is False


@requires_database
def test_a_shared_session_violating_the_last_seq_invariant_fails_parity(
    source, target
):
    """Both stores holding the SAME broken state is still broken.

    seq is allocated as last_seq+1 in one transaction, so a consistent store
    satisfies last_seq == max(seq, 0). A shared session that violates it
    reconciles happily and then collides on the first turn after the flip.
    """
    session = source.session()
    source.event(session["session_id"], seq=1)
    RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    assert RECONCILE.reconcile(
        sqlite_path=source.path, mode="parity"
    )["reconciled"] is True

    # Drift BOTH stores to the same wrong value, so nothing else can catch it.
    source.raw("UPDATE sessions SET last_seq = 0 WHERE session_id = ?",
               (session["session_id"],))
    with target.transaction() as conn:
        conn.execute("UPDATE app_sessions SET last_seq = 0 WHERE session_id = %s",
                     (session["session_id"],))
    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="parity")
    assert receipt["tables"]["app_sessions"]["conflicting_count"] == 0
    assert receipt["tables"]["app_sessions"]["invariant_reasons"] == {
        "last_seq_mismatch": 1
    }
    assert receipt["reconciled"] is False


@requires_database
def test_text_in_a_real_column_is_reported_never_coerced(source, target):
    """SQLite is dynamically typed: a REAL column can hold the TEXT "1_0".

    Python's float() accepts digit separators and returns 10.0, identical to a
    target DOUBLE PRECISION 10.0 -- while the legacy runtime still reads a
    string there and session_store._shadow_equal would reject the pair.
    """
    session = source.session()
    source.raw("UPDATE sessions SET created_at = '1_0' WHERE session_id = ?",
               (session["session_id"],))
    stored = None
    with RECONCILE._legacy_connection(source.path) as conn:
        stored = conn.execute(
            "SELECT typeof(created_at), created_at FROM sessions WHERE session_id = ?",
            (session["session_id"],),
        ).fetchone()
    assert stored[0] == "text" and stored[1] == "1_0", stored

    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    assert "non_numeric:created_at" in \
        receipt["tables"]["app_sessions"]["blocked_reasons"]
    assert receipt["inserted"]["app_sessions"] == 0
    assert receipt["reconciled"] is False
    assert _rows(target, "app_sessions", session_id=session["session_id"]) == []


def test_a_string_never_encodes_as_a_float():
    """The comparison half of the same defect, for a row present in both."""
    numeric = RECONCILE._comparable_value(RECONCILE.FLOAT, 10.0)
    assert RECONCILE._comparable_value(RECONCILE.FLOAT, "1_0") != numeric
    assert RECONCILE._comparable_value(RECONCILE.FLOAT, "10.0") != numeric
    assert RECONCILE._comparable_value(RECONCILE.FLOAT, True) != numeric
    # An integer in a REAL column IS the same number and must still match.
    assert RECONCILE._comparable_value(RECONCILE.FLOAT, 10) == numeric


@requires_database
def test_a_fractional_integer_is_reported_never_truncated(source, target):
    """SQLite is dynamically typed. int(3.5) is 3, which would compare equal to
    a target BIGINT 3 and certify a genuine difference as clean."""
    session = source.session()
    source.raw("UPDATE sessions SET last_seq = 3.5 WHERE session_id = ?",
               (session["session_id"],))
    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
    assert "non_integral:last_seq" in receipt["tables"]["app_sessions"]["blocked_reasons"]
    assert receipt["inserted"]["app_sessions"] == 0
    assert _rows(target, "app_sessions", session_id=session["session_id"]) == []


@requires_database
def test_a_racing_target_write_is_never_reported_as_reconciled(source, target):
    """A row that lands in PostgreSQL between the analysis and the INSERT.

    ON CONFLICT DO NOTHING must leave the app's row exactly as written, and the
    post-insert re-analysis must not then call the run clean when the two
    disagree.
    """
    session = source.session(status="active")
    original = RECONCILE._insert_snapshot
    fired = {"count": 0}

    def race_then_insert(connection, snapshot):
        if fired["count"] == 0:
            fired["count"] = 1
            # A separate transaction, committed before ours inserts.
            with target.transaction() as other:
                other.execute(
                    "INSERT INTO app_sessions (session_id, tenant_id, drawing_id,"
                    " status, created_at, updated_at, last_seq)"
                    " VALUES (%s,%s,%s,'closed',777.0,777.0,0)"
                    " ON CONFLICT DO NOTHING",
                    (session["session_id"], session["tenant_id"],
                     session["drawing_id"]),
                )
        return original(connection, snapshot)

    RECONCILE._insert_snapshot = race_then_insert
    try:
        try:
            receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="backfill")
        except Exception:
            # A serialization failure is an acceptable loud outcome; the
            # forbidden outcome is a quiet success over a disagreeing row.
            return
    finally:
        RECONCILE._insert_snapshot = original

    stored = _rows(target, "app_sessions", session_id=session["session_id"])[0]
    assert stored["status"] == "closed", "the racing writer's row was overwritten"
    assert stored["created_at"] == 777.0
    assert receipt["reconciled"] is False


@requires_database
def test_an_empty_source_against_a_populated_target_withholds_a_clean_exit(
    source, target
):
    """The shape a DESTROYED source takes reconciles trivially.

    Every target row is an expected target-only row, nothing is missing, and
    the command would exit 0 having proven only that the empty set is a subset.
    That is exactly the state a deploy leaves wherever the legacy store is
    task-local, which is where this command is most likely to be run first.
    """
    RECONCILE._ensure_source_schema(source.path)
    tenant = _unique("empty-src")
    with target.transaction() as conn:
        conn.execute(
            "INSERT INTO app_sessions (session_id, tenant_id, drawing_id, status,"
            " created_at, updated_at, last_seq) VALUES (%s,%s,%s,'active',1.0,1.0,0)",
            (_unique("survivor"), tenant, _unique("drawing")),
        )

    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="parity")
    # The set comparison itself is still clean; that is the point.
    assert receipt["reconciled"] is True
    assert receipt["empty_source_populated_target"] is True

    def run(*extra):
        return subprocess.run(
            [sys.executable, str(RECONCILE_PATH), "--mode", "parity",
             "--sqlite", str(source.path), *extra],
            capture_output=True, text=True, env=dict(os.environ),
        )

    refused = run()
    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert "NOT" in refused.stderr and "cutover receipt" in refused.stderr

    # An operator who knows the source is legitimately empty can say so.
    allowed = run("--allow-empty-source")
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert json.loads(allowed.stdout)["empty_source_populated_target"] is True


@requires_database
def test_an_empty_source_and_an_empty_target_still_pass(source, monkeypatch):
    """The flag must NOT be needed for a genuinely fresh pair.

    The guard is deliberately global rather than tenant-scoped -- on a real
    store the whole source matters -- so a shared test database always has rows
    from sibling tests. The target is emptied at the snapshot seam instead of by
    TRUNCATE, which would be destructive to those siblings.
    """
    RECONCILE._ensure_source_schema(source.path)
    empty = {table: [] for table in RECONCILE.TABLE_COLUMNS}
    monkeypatch.setattr(RECONCILE, "_postgres_snapshot", lambda _c: dict(empty))

    receipt = RECONCILE.reconcile(sqlite_path=source.path, mode="parity")
    assert receipt["reconciled"] is True
    assert receipt["empty_source_populated_target"] is False, (
        "a fresh pair must not be treated as a destroyed source"
    )
    assert receipt["source_counts"] == {t: 0 for t in RECONCILE.TABLE_COLUMNS}
    assert receipt["target_counts"] == {t: 0 for t in RECONCILE.TABLE_COLUMNS}

    # And the CLI layer, which is what an operator and a gate actually see.
    # Called IN-PROCESS rather than through subprocess on purpose: the guard
    # lives in main(), and only an in-process call inherits the stubbed empty
    # target above. Asserting the receipt alone would stay green through a
    # regression that refused every empty source at the exit-code layer.
    assert RECONCILE.main(
        ["--mode", "parity", "--sqlite", str(source.path)]
    ) == 0, "a genuinely fresh pair must exit 0 without --allow-empty-source"


@requires_database
def test_backfill_also_refuses_a_clean_exit_on_an_empty_source(source, target):
    """The guard must not be parity-only.

    A backfill against a destroyed source inserts nothing and would otherwise
    report success, which is the same manufactured receipt in the other mode.
    """
    RECONCILE._ensure_source_schema(source.path)
    with target.transaction() as conn:
        conn.execute(
            "INSERT INTO app_sessions (session_id, tenant_id, drawing_id, status,"
            " created_at, updated_at, last_seq) VALUES (%s,%s,%s,'active',1.0,1.0,0)",
            (_unique("survivor"), _unique("tenant"), _unique("drawing")),
        )

    def run(*extra):
        return subprocess.run(
            [sys.executable, str(RECONCILE_PATH), "--mode", "backfill",
             "--sqlite", str(source.path), *extra],
            capture_output=True, text=True, env=dict(os.environ),
        )

    refused = run()
    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert "cutover receipt" in refused.stderr
    receipt = json.loads(refused.stdout)
    assert receipt["inserted_total"] == 0
    assert receipt["empty_source_populated_target"] is True

    allowed = run("--allow-empty-source")
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr


@requires_database
def test_a_database_error_exits_two_not_one(source, tmp_path):
    """Exit 1 means 'parity failed' and gates a flip, so a driver failure must
    not borrow it. psycopg errors are not OSError/RuntimeError/sqlite3.Error."""
    source.session()
    environment = dict(os.environ)
    environment["DATABASE_URL"] = (
        "postgresql://postgres:postgres@127.0.0.1:59999/leaf_absent"
    )
    proc = subprocess.run(
        [sys.executable, str(RECONCILE_PATH), "--mode", "parity",
         "--sqlite", str(source.path)],
        capture_output=True, text=True, env=environment,
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "sessions authority reconciliation failed" in proc.stderr


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
