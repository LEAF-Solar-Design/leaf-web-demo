"""
Durable session store (sessions wire spec, gap audit §2.1 / CONTRACT-ADDENDUM
sessions addendum). Backs the conversational multi-turn surface:

    POST /api/sessions                       -> get_or_create_session
    POST /api/sessions/{id}/messages          -> try_begin_turn / append_event / end_turn
    GET  /api/sessions/{id}/stream  (SSE)     -> events_after
    GET  /api/sessions/{id}/transcript        -> recent_events
    POST /api/agent/approvals/{confirmation_id} -> decide_approval

Clones server/jobs.py's SQLite idioms verbatim: one WAL-mode connection shared
across threads (check_same_thread=False), a single module-level threading.Lock
serializing every statement, ensure_started() as the idempotent bootstrap seam,
and an env-var DB path override (SESSIONS_DB, default server/sessions.db) read
ONCE at import time so tests can redirect storage before first import (mirrors
JOBS_DB — see tests/test_wave2.py etc. for the precedent).

Event envelope (every stored/streamed event, frozen shape):
    {v: 1, session_id, turn_id, seq, type, data}

``seq`` is a durable, monotonic, session-scoped cursor. It is allocated in
EXACTLY ONE place in the whole codebase: append_event() below, inside a single
locked read-modify-write transaction (SELECT current last_seq -> seq = last_seq+1
-> INSERT the event row -> UPDATE sessions.last_seq -> commit). No other code
path may compute or assign a seq value.

Concurrency model: every public function that touches the database acquires
the module-level ``_lock`` for its ENTIRE read-modify-write sequence (not just
each individual statement), so compound operations — append_event's seq
allocation, get_or_create_session's idempotent insert-then-select,
try_begin_turn's compare-and-swap, decide_approval's check-then-set — are each
atomic with respect to every other thread calling into this module. This is a
single global lock (identical posture to jobs.py), not a database-level
transaction isolation guarantee; it is correct because ALL access to the
shared connection funnels through this module.

DDL:

    sessions(
      session_id       TEXT PRIMARY KEY,
      tenant_id        TEXT NOT NULL,
      drawing_id       TEXT NOT NULL,
      status           TEXT NOT NULL,
      created_at       REAL,
      updated_at       REAL,
      last_seq         INTEGER DEFAULT 0,
      active_turn_id   TEXT,
      turn_started_at  REAL,
      UNIQUE(tenant_id, drawing_id)
    )

    session_events(
      session_id  TEXT,
      seq         INTEGER,
      turn_id     TEXT,
      type        TEXT,
      data_json   TEXT,
      created_at  REAL,
      PRIMARY KEY(session_id, seq)
    )

    approvals(
      confirmation_id  TEXT PRIMARY KEY,
      session_id       TEXT,
      tenant_id        TEXT,
      turn_id          TEXT,
      tool             TEXT,
      params_json      TEXT,
      capability       TEXT,
      rationale        TEXT,
      kind             TEXT,
      payload_json     TEXT,
      decided          INTEGER DEFAULT 0,
      approved         INTEGER,
      decided_by       TEXT,
      created_at       REAL,
      expires_at       REAL
    )

Store API v1 (signatures FROZEN — downstream lanes S3/S4 call these exactly):

    ensure_started() -> None
    get_or_create_session(tenant_id, drawing_id) -> dict
    get_session(session_id) -> Optional[dict]
    append_event(session_id, turn_id, type, data) -> int
    events_after(session_id, after_seq, limit=500) -> list
    recent_events(session_id, limit) -> list
    try_begin_turn(session_id, turn_id, stale_after_s) -> bool
    end_turn(session_id, turn_id) -> None
    create_approval(confirmation_id, session_id, tenant_id, turn_id, tool, params,
                    capability, rationale, kind, payload, ttl_s) -> None
    get_approval(confirmation_id) -> Optional[dict]
    decide_approval(confirmation_id, approved, by) -> str   # 'recorded'|'already_decided'|'not_found'
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

SERVER_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("SESSIONS_DB", str(SERVER_DIR / "sessions.db")))

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id      TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  drawing_id      TEXT NOT NULL,
  status          TEXT NOT NULL,
  created_at      REAL,
  updated_at      REAL,
  last_seq        INTEGER DEFAULT 0,
  active_turn_id  TEXT,
  turn_started_at REAL,
  UNIQUE(tenant_id, drawing_id)
);

CREATE TABLE IF NOT EXISTS session_events (
  session_id  TEXT,
  seq         INTEGER,
  turn_id     TEXT,
  type        TEXT,
  data_json   TEXT,
  created_at  REAL,
  PRIMARY KEY(session_id, seq)
);

CREATE TABLE IF NOT EXISTS approvals (
  confirmation_id TEXT PRIMARY KEY,
  session_id      TEXT,
  tenant_id       TEXT,
  turn_id         TEXT,
  tool            TEXT,
  params_json     TEXT,
  capability      TEXT,
  rationale       TEXT,
  kind            TEXT,
  payload_json    TEXT,
  decided         INTEGER DEFAULT 0,
  approved        INTEGER,
  decided_by      TEXT,
  created_at      REAL,
  expires_at      REAL
);
"""


# --------------------------------------------------------------------------- #
# connection / low-level helpers (cloned from jobs.py idioms)
# --------------------------------------------------------------------------- #
def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.execute("PRAGMA busy_timeout = 5000")
        _conn.execute("PRAGMA journal_mode = WAL")
        _conn.executescript(_SCHEMA)
        _conn.commit()
    return _conn


def _exec(sql: str, args: tuple = ()) -> None:
    with _lock:
        _db().execute(sql, args)
        _db().commit()


def _query(sql: str, args: tuple = ()) -> List[sqlite3.Row]:
    with _lock:
        conn = _db()
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql, args)
        return cur.fetchall()


def ensure_started() -> None:
    """Idempotent: open the DB (creates the schema if missing). No background
    threads live in this module — the turn engine (S3) and its watchdog own
    their own lifecycle; this store is passive, called-into storage only."""
    _db()


# --------------------------------------------------------------------------- #
# row -> dict projections
# --------------------------------------------------------------------------- #
def _row_to_session(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "session_id": row["session_id"],
        "tenant_id": row["tenant_id"],
        "drawing_id": row["drawing_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_seq": row["last_seq"],
        "active_turn_id": row["active_turn_id"],
        "turn_started_at": row["turn_started_at"],
    }


def _row_to_envelope(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "v": 1,
        "session_id": row["session_id"],
        "turn_id": row["turn_id"],
        "seq": row["seq"],
        "type": row["type"],
        "data": json.loads(row["data_json"]) if row["data_json"] else {},
    }


def _row_to_approval(row: sqlite3.Row) -> Dict[str, Any]:
    expires_at = row["expires_at"]
    return {
        "confirmation_id": row["confirmation_id"],
        "session_id": row["session_id"],
        "tenant_id": row["tenant_id"],
        "turn_id": row["turn_id"],
        "tool": row["tool"],
        "params": json.loads(row["params_json"]) if row["params_json"] else None,
        "capability": row["capability"],
        "rationale": row["rationale"],
        "kind": row["kind"],
        "payload": json.loads(row["payload_json"]) if row["payload_json"] else None,
        "decided": bool(row["decided"]),
        "approved": (bool(row["approved"]) if row["approved"] is not None else None),
        "decided_by": row["decided_by"],
        "created_at": row["created_at"],
        "expires_at": expires_at,
        "expired": bool(expires_at is not None and time.time() > expires_at),
    }


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #
def get_or_create_session(tenant_id: str, drawing_id: str) -> Dict[str, Any]:
    """Idempotent per (tenant_id, drawing_id): INSERT OR IGNORE a fresh candidate
    row under the lock, then SELECT the (possibly pre-existing) row by the
    UNIQUE(tenant_id, drawing_id) key. Concurrent racers all funnel through the
    same lock, so exactly one row is ever created for a given key and every
    caller — winner or loser of the race — reads back the same session."""
    ensure_started()
    candidate_id = str(uuid.uuid4())
    now = time.time()
    with _lock:
        conn = _db()
        conn.execute(
            "INSERT OR IGNORE INTO sessions"
            " (session_id, tenant_id, drawing_id, status, created_at, updated_at, last_seq)"
            " VALUES (?,?,?,?,?,?,0)",
            (candidate_id, tenant_id, drawing_id, "active", now, now),
        )
        conn.commit()
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT * FROM sessions WHERE tenant_id = ? AND drawing_id = ?",
            (tenant_id, drawing_id),
        )
        row = cur.fetchone()
    return _row_to_session(row)


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Look up a session by id. Callers enforce the tenant guard themselves
    (compare the returned row's tenant_id to the caller's resolved identity and
    respond 404-not-403 on mismatch, per the auth posture used elsewhere in this
    codebase — see deps.py/require_tenant)."""
    rows = _query("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
    return _row_to_session(rows[0]) if rows else None


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #
def append_event(session_id: str, turn_id: Optional[str], type: str,
                  data: Dict[str, Any]) -> int:
    """Allocate the NEXT seq for this session and durably store the event, in
    ONE locked transaction: SELECT sessions.last_seq -> seq = last_seq + 1 ->
    INSERT INTO session_events -> UPDATE sessions.last_seq/updated_at -> commit.
    This is the ONLY place in the codebase that allocates seq. Raises
    KeyError if session_id is unknown (callers should have already resolved
    the session via get_session/get_or_create_session)."""
    now = time.time()
    data_json = json.dumps(data if data is not None else {})
    with _lock:
        conn = _db()
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT last_seq FROM sessions WHERE session_id = ?", (session_id,))
        row = cur.fetchone()
        if row is None:
            raise KeyError(f"unknown session_id {session_id!r}")
        seq = int(row["last_seq"]) + 1
        conn.execute(
            "INSERT INTO session_events (session_id, seq, turn_id, type, data_json, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (session_id, seq, turn_id, type, data_json, now),
        )
        conn.execute(
            "UPDATE sessions SET last_seq = ?, updated_at = ? WHERE session_id = ?",
            (seq, now, session_id),
        )
        conn.commit()
        return seq


def events_after(session_id: str, after_seq: int, limit: int = 500) -> List[Dict[str, Any]]:
    """Full envelopes with seq > after_seq, ascending seq (SSE replay / resume)."""
    limit = max(1, min(int(limit), 10000))
    rows = _query(
        "SELECT * FROM session_events WHERE session_id = ? AND seq > ?"
        " ORDER BY seq ASC LIMIT ?",
        (session_id, int(after_seq), limit),
    )
    return [_row_to_envelope(r) for r in rows]


def recent_events(session_id: str, limit: int) -> List[Dict[str, Any]]:
    """Most recent N envelopes, returned ascending by seq (poll-transcript path)."""
    limit = max(1, min(int(limit), 10000))
    rows = _query(
        "SELECT * FROM session_events WHERE session_id = ? ORDER BY seq DESC LIMIT ?",
        (session_id, limit),
    )
    ascending = list(rows)[::-1]
    return [_row_to_envelope(r) for r in ascending]


# --------------------------------------------------------------------------- #
# turn CAS
# --------------------------------------------------------------------------- #
def try_begin_turn(session_id: str, turn_id: str, stale_after_s: float) -> bool:
    """Atomic compare-and-swap: sets active_turn_id/turn_started_at ONLY when
    the session is unknown-free of an active turn (active_turn_id IS NULL) OR
    the existing active turn is staler than stale_after_s. Returns True iff
    this call won the CAS (the caller may now proceed as the turn owner);
    False iff a different, still-live turn already holds the session (caller
    should raise TurnBusy). Returns False for an unknown session_id."""
    now = time.time()
    with _lock:
        conn = _db()
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT active_turn_id, turn_started_at FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        active = row["active_turn_id"]
        started = row["turn_started_at"]
        is_free = active is None
        is_stale = (not is_free) and (started is None or (now - started) > stale_after_s)
        if not (is_free or is_stale):
            return False
        conn.execute(
            "UPDATE sessions SET active_turn_id = ?, turn_started_at = ?, updated_at = ?"
            " WHERE session_id = ?",
            (turn_id, now, now, session_id),
        )
        conn.commit()
        return True


def end_turn(session_id: str, turn_id: str) -> None:
    """Clear active_turn_id/turn_started_at iff they still match turn_id (a
    stale or already-superseded turn_id is a harmless no-op — it never clobbers
    a newer turn that has since taken over via try_begin_turn's stale path)."""
    _exec(
        "UPDATE sessions SET active_turn_id = NULL, turn_started_at = NULL, updated_at = ?"
        " WHERE session_id = ? AND active_turn_id = ?",
        (time.time(), session_id, turn_id),
    )


# --------------------------------------------------------------------------- #
# approvals
# --------------------------------------------------------------------------- #
def create_approval(confirmation_id: str, session_id: str, tenant_id: str,
                     turn_id: Optional[str], tool: Optional[str],
                     params: Optional[Dict[str, Any]], capability: Optional[str],
                     rationale: Optional[str], kind: Optional[str],
                     payload: Optional[Dict[str, Any]], ttl_s: float) -> None:
    """Insert a fresh, undecided approval row with an absolute expiry
    (created_at + ttl_s). confirmation_id is caller-generated (turn engine) and
    must be globally unique — a collision raises sqlite3.IntegrityError."""
    now = time.time()
    _exec(
        "INSERT INTO approvals (confirmation_id, session_id, tenant_id, turn_id, tool,"
        " params_json, capability, rationale, kind, payload_json, decided, approved,"
        " decided_by, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,0,NULL,NULL,?,?)",
        (confirmation_id, session_id, tenant_id, turn_id, tool,
         json.dumps(params) if params is not None else None,
         capability, rationale, kind,
         json.dumps(payload) if payload is not None else None,
         now, now + float(ttl_s)),
    )


def get_approval(confirmation_id: str) -> Optional[Dict[str, Any]]:
    """Look up an approval, with a computed `expired: bool` (time.time() >
    expires_at) — expiry is never mutated into the row; it's derived on read so
    a late decide_approval() call still sees the correct, honest expired flag."""
    rows = _query("SELECT * FROM approvals WHERE confirmation_id = ?", (confirmation_id,))
    return _row_to_approval(rows[0]) if rows else None


def decide_approval(confirmation_id: str, approved: bool, by: Optional[str] = None) -> str:
    """Record a decision exactly once, atomically (check-then-set under the
    lock so two concurrent decide calls can never both win). Returns:
      'recorded'        — this call recorded the decision
      'already_decided' — a prior call already recorded a decision (no-op)
      'not_found'       — no approval row exists for confirmation_id
    Deliberately does NOT check expiry — an expired-but-undecided approval can
    still be decided (the 410 confirmation_expired path is enforced by the
    caller on the subsequent /messages{confirm} turn, not here, per the wire
    spec's §2.1.5 note)."""
    with _lock:
        conn = _db()
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT decided FROM approvals WHERE confirmation_id = ?", (confirmation_id,)
        )
        row = cur.fetchone()
        if row is None:
            return "not_found"
        if row["decided"]:
            return "already_decided"
        conn.execute(
            "UPDATE approvals SET decided = 1, approved = ?, decided_by = ?"
            " WHERE confirmation_id = ?",
            (1 if approved else 0, by, confirmation_id),
        )
        conn.commit()
        return "recorded"
