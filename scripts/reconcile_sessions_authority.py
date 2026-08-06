#!/usr/bin/env python3
"""Incorporate legacy SQLite session rows into the PostgreSQL authority.

Mirrors ``scripts/reconcile_customization_authority.py`` for the three session
table pairs (``sessions``/``app_sessions``, ``session_events``/
``app_session_events``, ``approvals``/``app_approvals``). It exists so a
``LEAF_SESSIONS_STORE`` promotion out of ``dual_write_shadow`` has a gate: a
backfill that fills the history predating dual-write, and a full-data parity
check that proves the two stores agree.

Semantics, all three load-bearing:

* **Target-only rows are expected, never a failure.** Staging dual-writes while
  this runs, so PostgreSQL legitimately holds rows that SQLite does not.
* **Source-only rows are the backfill's job.** They are what ``--mode backfill``
  inserts.
* **A row present in both but disagreeing is reported, never clobbered.**
  PostgreSQL may hold the newer value, so there is no update or delete path
  anywhere in this file.

Exit codes:

* ``0`` reconciled: every legacy row has a matching target row.
* ``1`` ran to completion but parity failed. The per-table breakdown is on
  stdout.
* ``2`` could not run (bad schema, missing migration, locked source, ...). The
  reason is on stderr.

The command prints counts, reason tallies, hashed primary keys, and aggregate
digests only. It never prints tenant identifiers, event payloads, approval
params, approval payloads, or the database URL.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]


def _platform_db() -> Any:
    package_dir = ROOT / "platform"
    if "leaf_platform" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "leaf_platform",
            package_dir / "__init__.py",
            submodule_search_locations=[str(package_dir)],
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load the platform package")
        module = importlib.util.module_from_spec(spec)
        sys.modules["leaf_platform"] = module
        spec.loader.exec_module(module)
    import leaf_platform.db as database

    return database


def _session_store() -> Any:
    """Import the app's own legacy session store.

    Reused rather than reimplemented for two reasons: ``_json_value`` is the
    exact JSON coercion the dual-write path applies, and ``_db()`` is the only
    place the legacy schema is created and additively migrated.
    """
    server_dir = str(ROOT / "server")
    if server_dir not in sys.path:
        sys.path.insert(0, server_dir)
    import session_store

    return session_store


TEXT = "text"
FLOAT = "float"
INT = "int"
BOOL = "bool"
JSON = "json"

# Keyed by TARGET table, in FOREIGN KEY ORDER. ``app_session_events.session_id``
# REFERENCES ``app_sessions(session_id) ON DELETE CASCADE``, so sessions must be
# inserted first or every event insert fails. ``_insert_snapshot`` iterates this
# mapping directly and ``test_backfill_inserts_sessions_before_events`` pins the
# order, so do not reorder these entries.
TABLE_COLUMNS: dict[str, dict[str, str]] = {
    "app_sessions": {
        "session_id": TEXT,
        "tenant_id": TEXT,
        "drawing_id": TEXT,
        "status": TEXT,
        "created_at": FLOAT,
        "updated_at": FLOAT,
        "last_seq": INT,
        "active_turn_id": TEXT,
        "turn_started_at": FLOAT,
        "active_turn_tier": TEXT,
        "active_turn_subject": TEXT,
        "model": TEXT,
    },
    "app_session_events": {
        "session_id": TEXT,
        "seq": INT,
        "turn_id": TEXT,
        "type": TEXT,
        "data_json": JSON,
        "created_at": FLOAT,
    },
    "app_approvals": {
        "confirmation_id": TEXT,
        "session_id": TEXT,
        "tenant_id": TEXT,
        "turn_id": TEXT,
        "tool": TEXT,
        "params_json": JSON,
        "capability": TEXT,
        "rationale": TEXT,
        "kind": TEXT,
        "payload_json": JSON,
        "decided": BOOL,
        "approved": BOOL,
        "decided_by": TEXT,
        "created_at": FLOAT,
        "expires_at": FLOAT,
        "consumed": BOOL,
    },
}

LEGACY_TABLES: dict[str, str] = {
    "app_sessions": "sessions",
    "app_session_events": "session_events",
    "app_approvals": "approvals",
}

_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "app_sessions": ("session_id",),
    "app_session_events": ("session_id", "seq"),
    "app_approvals": ("confirmation_id",),
}

# Columns that arrive after 0012_sessions.sql. A target database behind these
# migrations lacks the column, and a bare INSERT would surface a driver error
# naming an undefined column rather than the migration to apply.
_MIGRATION_COLUMNS: dict[tuple[str, str], str] = {
    ("app_sessions", "model"): "0019_sessions_model.sql",
    ("app_sessions", "active_turn_subject"): "0022_sessions_active_turn_subject.sql",
}

# NOT NULL on the PostgreSQL side only. Legacy SQLite declares none of these
# (``created_at REAL`` with no constraint, ``decided INTEGER DEFAULT 0``), so a
# legacy NULL is unbackfillable and must be reported rather than coerced to a
# value the app never wrote.
_TARGET_NOT_NULL: dict[str, tuple[str, ...]] = {
    "app_sessions": (
        "session_id", "tenant_id", "drawing_id", "status",
        "created_at", "updated_at", "last_seq",
    ),
    "app_session_events": ("session_id", "seq", "type", "data_json", "created_at"),
    "app_approvals": ("confirmation_id", "decided", "created_at", "consumed"),
}

_WRITE_FENCE = 0x4C454146534553

_ENSURE_ATTEMPTS = 6
_ENSURE_BACKOFF_SECONDS = 2.0

# How many primary keys to report per table per defect class. Enough to act on,
# bounded so a badly drifted store cannot produce an unbounded receipt.
_KEY_SAMPLE_LIMIT = 20


def authority_digest(snapshot: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def authority_counts(
    snapshot: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, int]:
    return {table: len(snapshot.get(table, ())) for table in TABLE_COLUMNS}


def _is_locked_error(error: sqlite3.OperationalError) -> bool:
    message = str(error).lower()
    return "locked" in message or "busy" in message


class _JsonNull:
    """The JSON literal ``null``, which is a value, not SQL NULL."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "JSON_NULL"


JSON_NULL = _JsonNull()


def _normalize(kind: str, value: Any) -> Any:
    """Coerce one column to the single form both stores can be compared in.

    Float columns (``created_at``, ``updated_at``, ``turn_started_at``,
    ``expires_at``) are compared EXACTLY, with no tolerance. SQLite ``REAL`` and
    PostgreSQL ``DOUBLE PRECISION`` are both IEEE-754 binary64 and both drivers
    hand back a Python ``float``, so a value written by the dual-write path
    round-trips bit-for-bit; a tolerance would only let a genuinely different
    timestamp pass as equal. This deliberately matches ``session_store``'s
    ``_shadow_equal``, which compares whole dicts with a bare ``!=``. The two
    definitions of "equal" must not disagree, or a shadow read would fail on a
    pair this command has already certified.

    The result feeds INSERT. It is NOT sufficient for comparison on its own --
    see ``_comparable_value``.
    """
    if value is None:
        return None
    if kind == JSON:
        # TEXT in SQLite, JSONB in PostgreSQL. A string comparison always fails
        # and JSONB does not preserve key order, so compare parsed values.
        #
        # The JSON literal `null` is NOT SQL NULL: `'null'::jsonb` satisfies
        # app_session_events.data_json's NOT NULL. Collapsing both to Python
        # None would wrongly report such a row as unbackfillable and would
        # insert SQL NULL in its place, so keep them distinct.
        parsed = _session_store()._json_value(value)
        return JSON_NULL if parsed is None else parsed
    if kind == FLOAT:
        return float(value)
    if kind == INT:
        return int(value)
    if kind == BOOL:
        # INTEGER 0/1 in SQLite, BOOLEAN in PostgreSQL.
        return bool(value)
    return value


def _normalize_row(table: str, row: Mapping[str, Any]) -> dict[str, Any]:
    columns = TABLE_COLUMNS[table]
    return {column: _normalize(kind, row[column]) for column, kind in columns.items()}


def _comparable_value(kind: str, value: Any) -> Any:
    """Tag a column so equality cannot erase a distinguishable database state.

    ``_normalize`` produces values to INSERT, and plain Python equality over
    those is too weak to compare with: ``1 == True`` and ``0 == False``, so a
    source ``data_json`` of ``{"flag": 1}`` would certify equal to a target
    ``{"flag": true}``; and SQL NULL and the JSON literal ``null`` are different
    states in the same column. Tagging the kind and distinguishing SQL NULL from
    JSON null makes the comparison exact. The customization template compares
    canonical JSON strings for the same reason.

    Callers must compare the ENCODED form (``_comparable_row``), not these
    structures: the parsed JSON payload still contains Python values, where
    ``1 == True`` holds.
    """
    if kind == JSON:
        if value is None:
            return ["sql_null"]
        return ["json", _session_store()._json_value(value)]
    if value is None:
        return None
    if kind == FLOAT:
        # repr round-trips a binary64 float exactly in Python 3.
        return ["f", repr(float(value))]
    if kind == INT:
        return ["i", int(value)]
    if kind == BOOL:
        return ["b", bool(value)]
    return ["t", value]


def _comparable_row(table: str, row: Mapping[str, Any]) -> str:
    columns = TABLE_COLUMNS[table]
    tagged = {
        column: _comparable_value(kind, row[column])
        for column, kind in columns.items()
    }
    return json.dumps(tagged, sort_keys=True, separators=(",", ":"))


@contextmanager
def _legacy_connection(path: Path) -> Iterator[sqlite3.Connection]:
    """Yield the app's own lazily bootstrapped SQLite connection at ``path``.

    ``session_store._db()`` is the only place the legacy schema is created and
    additively migrated (``_SCHEMA`` plus the ``consumed``, ``active_turn_tier``,
    ``active_turn_subject`` and ``model`` ALTERs). Re-declaring that DDL here
    would drift from the schema the app actually maintains, so borrow the
    module's own bootstrap against a redirected path and restore its module
    state afterwards.
    """
    store = _session_store()
    with store._lock:
        previous_path = store.DB_PATH
        previous_conn = store._conn
        store.DB_PATH = Path(path)
        store._conn = None
        try:
            yield store._db()
        finally:
            opened = store._conn
            if opened is not None and opened is not previous_conn:
                try:
                    opened.close()
                except sqlite3.Error:
                    pass
            store.DB_PATH = previous_path
            store._conn = previous_conn


def _ensure_source_schema(
    path: Path,
    *,
    attempts: int = _ENSURE_ATTEMPTS,
    backoff_seconds: float = _ENSURE_BACKOFF_SECONDS,
    sleep: Any = time.sleep,
) -> None:
    """Run the app's own idempotent schema bootstrap on the source.

    ``session_store`` creates its tables lazily, so a store that was never
    touched, or one written before ``model``/``active_turn_subject`` existed,
    legitimately lacks tables or columns.

    This runs against the SAME database the live app is serving from, so a write
    lock held by a concurrent request is expected and recoverable. SQLite's own
    five second ``busy_timeout`` is not enough on its own: a busy database would
    otherwise turn a transient condition into a failed backfill. Retry a bounded
    number of times and let every other error surface immediately.

    Called from ``--mode backfill`` only. ``--mode parity`` must not write to the
    source at all, which is what makes its read-only claim provable by digest.
    """
    last_error: sqlite3.OperationalError | None = None
    for attempt in range(1, attempts + 1):
        try:
            with _legacy_connection(path):
                return
        except sqlite3.OperationalError as error:
            if not _is_locked_error(error):
                raise
            last_error = error
            if attempt < attempts:
                sleep(backoff_seconds)
    raise RuntimeError(
        "SQLite sessions source stayed locked after "
        f"{attempts} initialization attempts: {last_error}"
    )


def _sqlite_snapshot(path: Path) -> dict[str, list[dict[str, Any]]]:
    resolved = path.resolve(strict=True)
    if resolved.is_symlink() or not resolved.is_file():
        raise RuntimeError("SQLite sessions source must be one regular file")
    connection = sqlite3.connect(
        f"file:{resolved.as_posix()}?mode=ro", uri=True, isolation_level=None
    )
    connection.row_factory = sqlite3.Row
    # ONE encompassing read transaction. The connection is autocommit
    # (isolation_level=None), so without this each table's SELECT would be its
    # own implicit transaction and a writer committing between them would yield
    # a source snapshot that never existed as a single state -- the digest would
    # then certify a mixture. That matters precisely because this runs against
    # the database the live app is serving from.
    connection.execute("BEGIN DEFERRED")
    try:
        found = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing = sorted(set(LEGACY_TABLES.values()) - found)
        if missing:
            raise RuntimeError(
                "SQLite sessions source is incomplete: "
                + ",".join(missing)
                + " (run --mode backfill to bootstrap the source schema, or"
                " check --sqlite)"
            )
        snapshot: dict[str, list[dict[str, Any]]] = {}
        for table, columns in TABLE_COLUMNS.items():
            legacy = LEGACY_TABLES[table]
            present = {
                row[1]
                for row in connection.execute(f"PRAGMA table_info({legacy})")
            }
            # A legacy database written before an additive ALTER landed lacks
            # the column entirely. Selecting it would raise; the app treats it
            # as NULL, so read it as NULL rather than refusing the whole run.
            selected = ", ".join(
                column if column in present else f"NULL AS {column}"
                for column in columns
            )
            snapshot[table] = [
                dict(row)
                for row in connection.execute(
                    f"SELECT {selected} FROM {legacy} "
                    f"ORDER BY {','.join(_PRIMARY_KEYS[table])}"
                ).fetchall()
            ]
        return snapshot
    finally:
        # Read-only: nothing to commit, and ROLLBACK releases the read lock
        # promptly rather than waiting for close().
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        connection.close()


def _assert_target_columns(connection: Any) -> None:
    """Fail with the migration name when the target is behind 0019/0022."""
    rows = connection.execute(
        "SELECT table_name, column_name FROM information_schema.columns"
        " WHERE table_schema = current_schema()"
        " AND table_name = ANY(%s)",
        (list(TABLE_COLUMNS),),
    ).fetchall()
    present: dict[str, set[str]] = {table: set() for table in TABLE_COLUMNS}
    for row in rows:
        present.setdefault(row["table_name"], set()).add(row["column_name"])
    for table, columns in TABLE_COLUMNS.items():
        if not present.get(table):
            raise RuntimeError(
                f"PostgreSQL session schema is unavailable: {table} is missing;"
                " apply 0012_sessions.sql"
            )
        for column in columns:
            if column in present[table]:
                continue
            migration = _MIGRATION_COLUMNS.get((table, column))
            if migration is None:
                raise RuntimeError(
                    f"PostgreSQL {table} is missing column {column};"
                    " apply 0012_sessions.sql"
                )
            raise RuntimeError(
                f"PostgreSQL {table} is missing column {column};"
                f" apply migration {migration}"
            )


_SQL_NULL_PROBE = "__sql_null__"


def _postgres_snapshot(connection: Any) -> dict[str, list[dict[str, Any]]]:
    """Snapshot the target, keeping JSONB ``null`` distinct from SQL NULL.

    psycopg decodes ``'null'::jsonb`` and SQL NULL to the SAME Python ``None``,
    so a plain SELECT cannot tell them apart, and treating them as equal would
    let a genuinely different column certify clean. Ask the database directly
    with ``IS NULL`` and re-render a JSONB null as the text ``"null"``, which is
    exactly how SQLite stores it, so both sides reach ``_normalize`` in one
    representation. The rendered row still contains only the declared columns
    and stays JSON-serializable for ``authority_digest``.
    """
    snapshot: dict[str, list[dict[str, Any]]] = {}
    for table, columns in TABLE_COLUMNS.items():
        json_columns = [name for name, kind in columns.items() if kind == JSON]
        selected = list(columns) + [
            f"({name} IS NULL) AS {_SQL_NULL_PROBE}{name}" for name in json_columns
        ]
        rows = []
        for raw in connection.execute(
            f"SELECT {','.join(selected)} FROM {table} "
            f"ORDER BY {','.join(_PRIMARY_KEYS[table])}"
        ).fetchall():
            row = {name: raw[name] for name in columns}
            for name in json_columns:
                if row[name] is None and not raw[f"{_SQL_NULL_PROBE}{name}"]:
                    row[name] = "null"
            rows.append(row)
        snapshot[table] = rows
    return snapshot


def _indexed_snapshot(
    snapshot: Mapping[str, Sequence[Mapping[str, Any]]], *, side: str
) -> dict[str, dict[tuple[Any, ...], dict[str, Any]]]:
    """Index by primary key, carrying both the insertable values and a
    type-exact comparison string. The two are separate on purpose: ``values``
    feeds INSERT, ``compare`` decides equality, and plain equality over
    ``values`` is too weak (see ``_comparable_value``)."""
    if set(snapshot) != set(TABLE_COLUMNS):
        raise RuntimeError(f"{side} sessions authority has an invalid table set")
    indexed: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {}
    for table, columns in TABLE_COLUMNS.items():
        rows: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in snapshot[table]:
            if set(row) != set(columns):
                raise RuntimeError(
                    f"{side} sessions authority has an invalid row shape in {table}"
                )
            key = tuple(row[column] for column in _PRIMARY_KEYS[table])
            if any(value is None for value in key):
                raise RuntimeError(
                    f"{side} sessions authority has an ambiguous primary key in {table}"
                )
            if key in rows:
                raise RuntimeError(
                    f"{side} sessions authority has a duplicate primary key in {table}"
                )
            rows[key] = {
                "values": _normalize_row(table, row),
                "compare": _comparable_row(table, row),
            }
        indexed[table] = rows
    return indexed


def _identity(row: Mapping[str, Any]) -> tuple[Any, Any]:
    return (row["tenant_id"], row["drawing_id"])


def _non_integral_defects(raw: Mapping[str, Any], columns: Sequence[str]) -> list[str]:
    """Flag a source value that ``int()`` would silently truncate.

    SQLite is dynamically typed, so an INTEGER column can hold 3.5 or "3".
    ``int(3.5)`` is 3, which would compare equal to a target BIGINT 3 and
    certify a genuine difference as clean. The app cannot write one, but this
    command exists to judge a store rather than to trust it.
    """
    defects = []
    for column in columns:
        value = raw[column]
        if value is None or (isinstance(value, int) and not isinstance(value, bool)):
            continue
        defects.append(f"non_integral:{column}")
    return defects


def _session_defects(
    key: tuple[Any, ...],
    row: Mapping[str, Any],
    raw: Mapping[str, Any],
    *,
    duplicated_identities: set[tuple[Any, Any]],
    target_identities: Mapping[tuple[Any, Any], Any],
) -> list[str]:
    defects = [
        f"null_violation:{column}"
        for column in _TARGET_NOT_NULL["app_sessions"]
        if row[column] is None
    ]
    defects.extend(_non_integral_defects(raw, ("last_seq",)))
    last_seq = row["last_seq"]
    if last_seq is not None and last_seq < 0:
        # app_sessions.last_seq carries CHECK (last_seq >= 0); SQLite has none.
        defects.append("negative_last_seq")
    identity = _identity(row)
    if None not in identity:
        if identity in duplicated_identities:
            # UNIQUE (tenant_id, drawing_id) on both sides. A legacy database
            # carrying duplicates from before that index existed collides on
            # insert. Report, do not force.
            defects.append("duplicate_identity")
        holder = target_identities.get(identity)
        if holder is not None and holder != key[0]:
            defects.append("identity_conflict")
    return defects


def _event_defects(
    row: Mapping[str, Any], raw: Mapping[str, Any], *, available_sessions: set[Any]
) -> list[str]:
    defects = [
        f"null_violation:{column}"
        for column in _TARGET_NOT_NULL["app_session_events"]
        if row[column] is None
    ]
    defects.extend(_non_integral_defects(raw, ("seq",)))
    seq = row["seq"]
    if seq is not None and seq <= 0:
        # app_session_events.seq carries CHECK (seq > 0); SQLite has no such
        # constraint and sessions.last_seq defaults to 0. Detect and report;
        # never silently skip.
        defects.append("non_positive_seq")
    if row["session_id"] is not None and row["session_id"] not in available_sessions:
        # The FK is ON DELETE CASCADE and mandatory. An event whose session row
        # exists in neither store is reported as an orphan rather than dropped
        # or force-inserted.
        defects.append("orphan_event")
    return defects


def _approval_defects(row: Mapping[str, Any], raw: Mapping[str, Any]) -> list[str]:
    defects = [
        f"null_violation:{column}"
        for column in _TARGET_NOT_NULL["app_approvals"]
        if row[column] is None
    ]
    if row["consumed"] and not row["decided"]:
        # app_approvals carries CHECK (NOT consumed OR decided); SQLite does not.
        defects.append("consumed_without_decision")
    for column in ("decided", "approved", "consumed"):
        value = raw[column]
        if value is None or (isinstance(value, int) and value in (0, 1)):
            continue
        # bool() would map any nonzero to TRUE, so a legacy 2 would silently
        # certify equal to a target TRUE forever. The target column is BOOLEAN
        # and cannot represent it, so report rather than coerce.
        defects.append(f"non_boolean_flag:{column}")
    return defects


def _analyze(
    source: Mapping[str, Sequence[Mapping[str, Any]]],
    target: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Classify every source row against the target.

    Returns per table: rows only in the source (the backfill's work), rows only
    in the target (expected, never a failure), rows present in both that
    disagree (reported, never clobbered), and source-only rows that cannot be
    inserted at all, with their reason codes.
    """
    source_index = _indexed_snapshot(source, side="SQLite")
    target_index = _indexed_snapshot(target, side="PostgreSQL")

    identity_counts: dict[tuple[Any, Any], int] = {}
    for entry in source_index["app_sessions"].values():
        identity = _identity(entry["values"])
        if None in identity:
            continue
        identity_counts[identity] = identity_counts.get(identity, 0) + 1
    duplicated_identities = {
        identity for identity, count in identity_counts.items() if count > 1
    }
    target_identities = {
        _identity(entry["values"]): key[0]
        for key, entry in target_index["app_sessions"].items()
        if None not in _identity(entry["values"])
    }

    # Sessions present in BOTH stores whose rows disagree. Their identity is in
    # dispute, so nothing may be written beneath them: if the source row is
    # tenant A's and the target row with that session_id is tenant B's, then
    # inserting the source's events would file tenant A's events under tenant
    # B's session. Computed before events are classified, and removed from the
    # availability set below.
    conflicting_session_ids = {
        key[0]
        for key, target_entry in target_index["app_sessions"].items()
        if key in source_index["app_sessions"]
        and source_index["app_sessions"][key]["compare"] != target_entry["compare"]
    }

    raw_by_table = {
        table: {
            tuple(row[column] for column in _PRIMARY_KEYS[table]): row
            for row in source[table]
        }
        for table in TABLE_COLUMNS
    }

    tables: dict[str, Any] = {}
    insertable: dict[str, list[dict[str, Any]]] = {}

    # Sessions first: which sessions will exist decides which events are orphans.
    session_only = {
        key: entry
        for key, entry in source_index["app_sessions"].items()
        if key not in target_index["app_sessions"]
    }

    # last_seq must dominate the session's own event stream. seq is allocated in
    # exactly one place, append_event's last_seq+1, so a source where an event
    # outruns last_seq is already corrupt. It matters here and not only in the
    # abstract: after the flip _pg_append_event sets last_seq = last_seq + 1 and
    # inserts (session_id, that seq), so a stale last_seq collides with the
    # backfilled event's primary key on the first turn after cutover. Both
    # stores would hold the same broken state, so parity alone would never see
    # it. Computed over the source's FULL event stream, not just source-only
    # events.
    highest_source_seq: dict[Any, int] = {}
    for key, entry in source_index["app_session_events"].items():
        seq = entry["values"]["seq"]
        if seq is None:
            continue
        session_id = key[0]
        if seq > highest_source_seq.get(session_id, 0):
            highest_source_seq[session_id] = seq

    session_blocked: dict[tuple[Any, ...], list[str]] = {}
    for key, entry in session_only.items():
        row = entry["values"]
        defects = _session_defects(
            key, row, raw_by_table["app_sessions"][key],
            duplicated_identities=duplicated_identities,
            target_identities=target_identities,
        )
        last_seq = row["last_seq"]
        if last_seq is not None and highest_source_seq.get(key[0], 0) > last_seq:
            defects.append("last_seq_behind_events")
        if defects:
            session_blocked[key] = defects
    insertable["app_sessions"] = [
        entry["values"] for key, entry in session_only.items()
        if key not in session_blocked
    ]
    # A session that will exist in the target AND is not in dispute. A
    # conflicting shared session is deliberately excluded: see
    # conflicting_session_ids above.
    available_session_ids = (
        {key[0] for key in target_index["app_sessions"]}
        | {row["session_id"] for row in insertable["app_sessions"]}
    ) - conflicting_session_ids

    blocked_by_table = {"app_sessions": session_blocked}

    event_only = {
        key: entry
        for key, entry in source_index["app_session_events"].items()
        if key not in target_index["app_session_events"]
    }
    event_blocked: dict[tuple[Any, ...], list[str]] = {}
    for key, entry in event_only.items():
        defects = _event_defects(
            entry["values"], raw_by_table["app_session_events"][key],
            available_sessions=available_session_ids,
        )
        if key[0] in conflicting_session_ids:
            defects.append("conflicting_parent_session")
        if defects:
            event_blocked[key] = defects
    insertable["app_session_events"] = [
        entry["values"] for key, entry in event_only.items()
        if key not in event_blocked
    ]
    blocked_by_table["app_session_events"] = event_blocked

    approval_only = {
        key: entry
        for key, entry in source_index["app_approvals"].items()
        if key not in target_index["app_approvals"]
    }
    approval_blocked: dict[tuple[Any, ...], list[str]] = {}
    for key, entry in approval_only.items():
        defects = _approval_defects(
            entry["values"], raw_by_table["app_approvals"][key]
        )
        if defects:
            approval_blocked[key] = defects
    insertable["app_approvals"] = [
        entry["values"] for key, entry in approval_only.items()
        if key not in approval_blocked
    ]
    blocked_by_table["app_approvals"] = approval_blocked

    source_only_by_table = {
        "app_sessions": session_only,
        "app_session_events": event_only,
        "app_approvals": approval_only,
    }

    for table in TABLE_COLUMNS:
        blocked = blocked_by_table[table]
        conflicts: list[tuple[Any, ...]] = []
        for key, target_entry in target_index[table].items():
            source_entry = source_index[table].get(key)
            if source_entry is None:
                continue
            # Type-exact string comparison, NOT Python equality over the
            # normalized values: see _comparable_value.
            if source_entry["compare"] != target_entry["compare"]:
                conflicts.append(key)
        reasons: dict[str, int] = {}
        for defects in blocked.values():
            for defect in defects:
                reasons[defect] = reasons.get(defect, 0) + 1
        tables[table] = {
            "source_count": len(source_index[table]),
            "target_count": len(target_index[table]),
            "source_only_count": len(source_only_by_table[table]),
            "target_only_count": len(
                target_index[table].keys() - source_index[table].keys()
            ),
            "conflicting_count": len(conflicts),
            "conflicting_keys": _key_sample(conflicts),
            "blocked_count": len(blocked),
            "blocked_reasons": dict(sorted(reasons.items())),
            "blocked_keys": _key_sample(sorted(blocked, key=repr)),
            "insertable_count": len(insertable[table]),
        }

    return {"tables": tables, "insertable": insertable}


def _key_sample(keys: Sequence[tuple[Any, ...]]) -> list[str]:
    """Bounded, HASHED primary keys.

    The raw key is deliberately not printed. ``session_id`` is a durable
    tenant-linked conversation identifier, and ``confirmation_id`` is
    caller-supplied with no UUID enforcement, so it can carry identifying text.
    A truncated SHA-256 still lets an operator locate a row (hash the candidate
    id and match) while the receipt itself discloses nothing.
    """
    sample = []
    for key in list(keys)[:_KEY_SAMPLE_LIMIT]:
        joined = ":".join(str(part) for part in key)
        sample.append(hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16])
    return sample


def _missing_source_rows(
    source: Mapping[str, Sequence[Mapping[str, Any]]],
    target: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, list[Mapping[str, Any]]]:
    """Return the source rows that are absent from the target AND insertable."""
    return _analyze(source, target)["insertable"]


def _target_only_counts(
    source: Mapping[str, Sequence[Mapping[str, Any]]],
    target: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, int]:
    source_index = _indexed_snapshot(source, side="SQLite")
    target_index = _indexed_snapshot(target, side="PostgreSQL")
    return {
        table: len(target_index[table].keys() - source_index[table].keys())
        for table in TABLE_COLUMNS
    }


def _insert_value(kind: str, value: Any) -> Any:
    """Render one normalized value for the parameterized INSERT.

    A JSON column holding SQL NULL is inserted as SQL NULL, exactly as
    session_store's own ``_pg_create_approval`` does (``json.dumps(params) if
    params is not None else None``); the two must not disagree. The JSON literal
    ``null`` is a value and is inserted as ``'null'::jsonb``.
    """
    if kind != JSON:
        return value
    if value is None:
        return None
    if value is JSON_NULL:
        return "null"
    return json.dumps(value)


def _insert_snapshot(
    connection: Any, snapshot: Mapping[str, Sequence[Mapping[str, Any]]]
) -> dict[str, int]:
    """Insert missing rows. No update path, no delete path.

    Iterates ``TABLE_COLUMNS`` in declaration order, which puts ``app_sessions``
    before ``app_session_events`` so the ON DELETE CASCADE foreign key is
    satisfied.

    ``ON CONFLICT DO NOTHING`` is what makes this safe against a live
    dual-writing service: a row that lands in PostgreSQL between the snapshot
    and this insert is left exactly as the app wrote it, never overwritten.
    Under SERIALIZABLE the transaction more often aborts loudly than swallows
    such a row, and where it does swallow one the post-insert re-analysis leaves
    it counted as source-only rather than reconciled. Either way the run does
    not report success; it does not necessarily report it as a *conflict*.
    """
    inserted: dict[str, int] = {}
    for table, columns in TABLE_COLUMNS.items():
        rows = snapshot.get(table) or ()
        placeholders = ",".join(
            "%s::jsonb" if kind == JSON else "%s" for kind in columns.values()
        )
        statement = (
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})"
            " ON CONFLICT DO NOTHING"
        )
        count = 0
        for row in rows:
            values = tuple(
                _insert_value(kind, row[column]) for column, kind in columns.items()
            )
            count += connection.execute(statement, values).rowcount
        inserted[table] = count
    return inserted


def reconcile(*, sqlite_path: Path, mode: str) -> dict[str, Any]:
    if mode not in ("backfill", "parity"):
        raise RuntimeError(f"unsupported mode {mode!r}")
    if mode == "backfill":
        _ensure_source_schema(sqlite_path)
    source = _sqlite_snapshot(sqlite_path)
    source_digest = authority_digest(source)
    database = _platform_db()
    database.assert_schema_current()
    read_only = mode == "parity"
    with database.transaction(
        isolation="repeatable read" if read_only else "serializable",
        read_only=read_only,
    ) as connection:
        _assert_target_columns(connection)
        if read_only:
            # Shared, so a concurrent backfill cannot interleave with this
            # snapshot while two parity runs remain free to overlap.
            connection.execute("SELECT pg_advisory_xact_lock_shared(%s)", (_WRITE_FENCE,))
        else:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_WRITE_FENCE,))
        target = _postgres_snapshot(connection)
        analysis = _analyze(source, target)
        inserted = {table: 0 for table in TABLE_COLUMNS}
        if mode == "backfill" and any(
            analysis["insertable"][table] for table in TABLE_COLUMNS
        ):
            inserted = _insert_snapshot(connection, analysis["insertable"])
            target = _postgres_snapshot(connection)
            analysis = _analyze(source, target)
        target_digest = authority_digest(target)
        # The two snapshots are NOT one atomic read: SQLite is snapshotted and
        # released before PostgreSQL is locked, and the live app holds no
        # advisory lock at all, so a legacy write that commits in between is
        # invisible to both halves. Left undetected that is a FALSE PASS -- the
        # app could commit a legacy approval and then fail before its PostgreSQL
        # mirror, and neither snapshot would contain it. Re-read the source
        # while still holding the target transaction: if its digest moved, the
        # source changed under the run and the comparison certifies a pair that
        # never coexisted. This narrows the window rather than closing it; only
        # writer quiescence closes it, and this command deliberately does not
        # quiesce a live service.
        source_after = _sqlite_snapshot(sqlite_path)
        source_stable = authority_digest(source_after) == source_digest
        tables = analysis["tables"]
        reconciled = source_stable and all(
            entry["source_only_count"] == 0 and entry["conflicting_count"] == 0
            for entry in tables.values()
        )
        return {
            "schema": "leaf.sessions-authority-reconciliation.v2",
            "mode": mode,
            "reconciled": reconciled,
            "source_stable": source_stable,
            "parity": reconciled,
            "exact_equal": target_digest == source_digest,
            "inserted": inserted,
            "inserted_total": sum(inserted.values()),
            "source_counts": authority_counts(source),
            "source_digest": source_digest,
            "target_counts": authority_counts(target),
            "target_digest": target_digest,
            "target_only_counts": {
                table: entry["target_only_count"] for table, entry in tables.items()
            },
            "tables": tables,
        }


def default_sqlite_path() -> Path:
    """Resolve the legacy database exactly as the running app resolves it.

    ``session_store.DB_PATH`` is ``SESSIONS_DB`` if set, else
    ``server/sessions.db``. Hardcoding a path here would be wrong in one of the
    two live configurations: the deploy README documents
    ``SESSIONS_DB=/data/state/sessions.db``, but staging task definition
    ``leaf-platform-app-alt:27`` does not set it at all, so the app is serving
    from the task-local default. Reconciling the wrong file would report a clean
    parity against a database nothing writes.
    """
    return Path(_session_store().DB_PATH)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("backfill", "parity"), required=True)
    parser.add_argument(
        "--sqlite",
        type=Path,
        default=None,
        help="legacy SQLite path; defaults to the app's own SESSIONS_DB resolution",
    )
    args = parser.parse_args(argv)
    try:
        sqlite_path = args.sqlite if args.sqlite is not None else default_sqlite_path()
        receipt = reconcile(sqlite_path=sqlite_path, mode=args.mode)
    except Exception as exc:  # noqa: BLE001
        # Deliberately broad. Exit 1 means "ran to completion, parity failed"
        # and a caller gates a data-authority flip on that distinction, so a
        # driver error must NOT surface as 1. psycopg's exceptions derive from
        # Exception and not from any of OSError/RuntimeError/sqlite3.Error, so
        # a narrow clause let a connection failure exit 1 with a traceback.
        print(f"sessions authority reconciliation failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    if not receipt["reconciled"]:
        if not receipt["source_stable"]:
            print(
                "sessions authority source changed during the run; re-run",
                file=sys.stderr,
            )
        else:
            print("sessions authority parity failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
