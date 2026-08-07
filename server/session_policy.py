"""Per-session approval policy (chip: "Read-only auto-runs only in
auto_approve_reads; paid/write always confirms").

Its own table, lock and connection — the checkpoints.py precedent — so
session_store's dual-mode (SQLite/PostgreSQL) shadow parity is untouched: the
frozen session projections never grow a field the PG mirror lacks.

Since 0029 the table also has a PostgreSQL home, selected by
``LEAF_SESSION_ANNEX_STORE`` rather than by ``LEAF_SESSIONS_STORE``. The reasons
for a separate selector, and the mode vocabulary, are in ``session_annex``.
Under the default ``legacy`` this module behaves exactly as it did before.

Two values, and the DEFAULT is the whole safety story:

    confirm_all        (default) every proposal confirms — today's behavior,
                       byte-identical when nothing ever sets a policy.
    plan_first         EVERYTHING confirms, including the tool the harness
                       normally auto-approves via its allowlist: the server
                       sends the `x-leaf-approval-policy: plan_first` sidecar
                       header (the instant-assignment precedent — consumed
                       before the runner starts, never in the transcript) and
                       the harness empties its per-turn auto-approval so every
                       execution rides the proposal/confirmation lifecycle.
                       Until the harness half ships, the header is emitted and
                       ignored — behavior degrades to confirm_all, the SAFE
                       direction.
    auto_approve_reads proposals whose capability is EXACTLY `run_read` are
                       auto-decided and auto-confirmed at the turn's terminal
                       (turn_runner._auto_confirm_reads). Everything else —
                       run_write, drawing.write, deploy, build, a MISSING
                       capability — always confirms. Fail closed: unknown
                       means confirm.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

import session_annex

SERVER_DIR = Path(__file__).resolve().parent
# Same resolution rule as session_store.DB_PATH so tests' SESSIONS_DB redirect
# covers this table too (read at import time, matching that module's posture).
DB_PATH = Path(os.environ.get("SESSIONS_DB", str(SERVER_DIR / "sessions.db")))

POLICIES = frozenset({"confirm_all", "auto_approve_reads", "plan_first"})
DEFAULT_POLICY = "confirm_all"

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.execute("PRAGMA busy_timeout = 5000")
        _conn.execute("PRAGMA journal_mode = WAL")
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS session_policies ("
            " session_id TEXT PRIMARY KEY,"
            " tenant_id  TEXT NOT NULL,"
            " policy     TEXT NOT NULL,"
            " updated_at REAL)"
        )
        _conn.commit()
    return _conn


def is_valid_policy(value: object) -> bool:
    return isinstance(value, str) and value in POLICIES


def _legacy_set_policy(session_id: str, tenant_id: str, policy: str) -> float:
    """Write the legacy row and return the timestamp it stored.

    THE TIMESTAMP IS SAMPLED INSIDE THE LOCK, and returned rather than passed
    in, because those are the same requirement. Sampling before the lock lets
    two concurrent setters commit in the opposite order from their timestamps,
    so ``updated_at`` moves backwards -- a real change from the pre-dispatch
    behaviour, which sampled it under the lock immediately before the upsert
    (review round 1). Returning it is then what lets the mirror store the SAME
    value, without which every dual_write_shadow read would compare two rows
    differing only on ``updated_at`` and read as corruption.
    """
    with _lock:
        updated_at = time.time()
        conn = _db()
        conn.execute(
            "INSERT INTO session_policies (session_id, tenant_id, policy, updated_at)"
            " VALUES (?,?,?,?) ON CONFLICT(session_id)"
            " DO UPDATE SET policy = excluded.policy, updated_at = excluded.updated_at"
            " WHERE session_policies.tenant_id = excluded.tenant_id",
            (session_id, str(tenant_id), policy, updated_at),
        )
        conn.commit()
    return updated_at


def _legacy_get_policy(session_id: str, tenant_id: str) -> Optional[str]:
    with _lock:
        conn = _db()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT policy FROM session_policies"
            " WHERE session_id = ? AND tenant_id = ?",
            (session_id, str(tenant_id)),
        ).fetchone()
    return row["policy"] if row else None


def _pg_set_policy(session_id: str, tenant_id: str, policy: str,
                   updated_at: float) -> None:
    """Same tenant-guarded upsert as the legacy path, including its no-op.

    The trailing WHERE is not decoration. A session_id already held by another
    tenant must leave the stored policy alone rather than be overwritten, and
    dropping the clause here would make the PostgreSQL authority strictly more
    permissive than the SQLite one it replaces.
    """
    db = session_annex.platform_db()
    with db.transaction() as conn:
        conn.execute(
            f"INSERT INTO {session_annex.PG_POLICIES_TABLE}"
            " (session_id, tenant_id, policy, updated_at) VALUES (%s,%s,%s,%s)"
            " ON CONFLICT (session_id) DO UPDATE SET policy = excluded.policy,"
            " updated_at = excluded.updated_at"
            f" WHERE {session_annex.PG_POLICIES_TABLE}.tenant_id = excluded.tenant_id",
            (session_id, str(tenant_id), policy, updated_at),
        )


def _pg_get_policy(session_id: str, tenant_id: str) -> Optional[str]:
    db = session_annex.platform_db()
    with db.cursor() as cur:
        cur.execute(
            f"SELECT policy FROM {session_annex.PG_POLICIES_TABLE}"
            " WHERE session_id = %s AND tenant_id = %s",
            (session_id, str(tenant_id)),
        )
        row = cur.fetchone()
    return row["policy"] if row else None


def set_policy(session_id: str, tenant_id: str, policy: str) -> None:
    """Upsert. Caller validates `policy` (the router 400s invalid values);
    this refuses anyway rather than storing garbage."""
    if not is_valid_policy(policy):
        raise ValueError(f"invalid policy {policy!r}")
    mode = session_annex.store_mode()
    if mode == "postgres":
        _pg_set_policy(session_id, tenant_id, policy, time.time())
        return
    if mode in session_annex.DUAL_WRITE_MODES:
        # Pre-flight only; the two stores share no atomic transaction, so a
        # committed legacy row can still outrun its mirror.
        session_annex.ensure_started()
    # The legacy writer samples the timestamp under its own lock and hands it
    # back, so the mirror stores the SAME value without moving the sample
    # outside the lock. See _legacy_set_policy.
    updated_at = _legacy_set_policy(session_id, tenant_id, policy)
    if mode in session_annex.DUAL_WRITE_MODES:
        _pg_set_policy(session_id, tenant_id, policy, updated_at)
    # `shadow` deliberately does not compare here — see checkpoints.create_checkpoint.


def get_policy(session_id: str, tenant_id: str) -> str:
    """Tenant-scoped at the STORAGE boundary (the checkpoint-chip lesson):
    a mismatched tenant reads the DEFAULT, exactly like an absent row.

    The validity filter is applied to whichever backend answered, so a row that
    somehow holds an unknown value still degrades to ``confirm_all``. That is
    fail-closed in both stores, and in PostgreSQL the CHECK constraint in
    0029 makes such a row unwritable in the first place.
    """
    mode = session_annex.store_mode()
    if mode == "postgres":
        value = _pg_get_policy(session_id, tenant_id)
        return value if is_valid_policy(value) else DEFAULT_POLICY
    value = _legacy_get_policy(session_id, tenant_id)
    if mode in session_annex.SHADOW_READ_MODES:
        session_annex.shadow_equal(
            "session policy", value, _pg_get_policy(session_id, tenant_id))
    return value if is_valid_policy(value) else DEFAULT_POLICY
