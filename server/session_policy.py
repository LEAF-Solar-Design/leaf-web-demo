"""Per-session approval policy (chip: "Read-only auto-runs only in
auto_approve_reads; paid/write always confirms").

Its own table, lock and connection — the checkpoints.py precedent — so
session_store's dual-mode (SQLite/PostgreSQL) shadow parity is untouched: the
frozen session projections never grow a field the PG mirror lacks. PostgreSQL
persistence for policies is a named follow-up alongside the store migration.

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


def set_policy(session_id: str, tenant_id: str, policy: str) -> None:
    """Upsert. Caller validates `policy` (the router 400s invalid values);
    this refuses anyway rather than storing garbage."""
    if not is_valid_policy(policy):
        raise ValueError(f"invalid policy {policy!r}")
    with _lock:
        conn = _db()
        conn.execute(
            "INSERT INTO session_policies (session_id, tenant_id, policy, updated_at)"
            " VALUES (?,?,?,?) ON CONFLICT(session_id)"
            " DO UPDATE SET policy = excluded.policy, updated_at = excluded.updated_at"
            " WHERE session_policies.tenant_id = excluded.tenant_id",
            (session_id, str(tenant_id), policy, time.time()),
        )
        conn.commit()


def get_policy(session_id: str, tenant_id: str) -> str:
    """Tenant-scoped at the STORAGE boundary (the checkpoint-chip lesson):
    a mismatched tenant reads the DEFAULT, exactly like an absent row."""
    with _lock:
        conn = _db()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT policy FROM session_policies"
            " WHERE session_id = ? AND tenant_id = ?",
            (session_id, str(tenant_id)),
        ).fetchone()
    value = row["policy"] if row else None
    return value if is_valid_policy(value) else DEFAULT_POLICY
