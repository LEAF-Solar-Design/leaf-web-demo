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

``seq`` is a durable, monotonic, session-scoped cursor. It is allocated only by
the event append primitives below, inside one locked read-modify-write
transaction (SELECT current last_seq -> seq = last_seq+1 -> INSERT the event
row -> UPDATE sessions.last_seq -> commit). No caller computes or assigns it.

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
      active_turn_tier TEXT,
      active_turn_subject TEXT,
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
      expires_at       REAL,
      consumed         INTEGER NOT NULL DEFAULT 0
    )

Store API v1 (signatures FROZEN — downstream lanes S3/S4 call these exactly):

    ensure_started() -> None
    get_or_create_session(tenant_id, drawing_id) -> dict
    get_session(session_id) -> Optional[dict]
    append_event(session_id, turn_id, type, data) -> int
    append_confirmation_resolved_once(session_id, turn_id, confirmation_id,
                                      approved, by) -> (seq, inserted)
    events_after(session_id, after_seq, limit=500) -> list
    recent_events(session_id, limit) -> list
    try_begin_turn(session_id, turn_id, stale_after_s) -> bool
    end_turn(session_id, turn_id) -> None
    create_approval(confirmation_id, session_id, tenant_id, turn_id, tool, params,
                    capability, rationale, kind, payload, ttl_s) -> None
    get_approval(confirmation_id) -> Optional[dict]
    decide_approval(confirmation_id, approved, by) -> str   # 'recorded'|'already_decided'|'not_found'
    consume_approval(confirmation_id, session_id, tenant_id) -> dict   # raises ApprovalConsumeError
    unconsume_approval(confirmation_id, session_id, tenant_id) -> bool # give back an unredeemed consume
"""
from __future__ import annotations

import json
import importlib.util
import os
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

SERVER_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("SESSIONS_DB", str(SERVER_DIR / "sessions.db")))

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

# A non-turn RESERVATION of the active-turn slot (a checkpoint restore). The
# stale takeover below judges the CURRENT HOLDER by the holder's own window,
# not the incoming caller's — try_begin_turn used to apply the caller's
# stale_after_s to whatever held the slot, so an ordinary 300s turn could
# steal a 301-second-old reservation regardless of the window the restore
# declared (PR #310 review round 4). Turns keep exactly today's behavior;
# only a reservation holder gets the wider window.
RESERVATION_PREFIX = "restore-"
RESERVATION_STALE_S = 3600.0


def is_reservation_holder(turn_id: Optional[str]) -> bool:
    return isinstance(turn_id, str) and turn_id.startswith(RESERVATION_PREFIX)

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
  active_turn_tier TEXT,
  active_turn_subject TEXT,
  model           TEXT,
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
  expires_at      REAL,
  consumed        INTEGER NOT NULL DEFAULT 0
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
        # CREATE TABLE IF NOT EXISTS never retrofits columns onto a database
        # created by an older schema. `consumed` landed after the first cut of
        # this table, and consume_approval() SELECTs it — migrate additively
        # and idempotently before any reader touches the connection.
        cols = {r[1] for r in _conn.execute("PRAGMA table_info(approvals)").fetchall()}
        if "consumed" not in cols:
            _conn.execute("ALTER TABLE approvals ADD COLUMN consumed INTEGER NOT NULL DEFAULT 0")
        session_cols = {
            r[1] for r in _conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "active_turn_tier" not in session_cols:
            _conn.execute("ALTER TABLE sessions ADD COLUMN active_turn_tier TEXT")
        # The authenticated subject that opened the active turn, written in the
        # same CAS as the tier. It exists so a back-edge call can be attributed
        # to the human who started the turn WITHOUT the harness asserting an
        # identity. Same additive, idempotent posture as active_turn_tier.
        if "active_turn_subject" not in session_cols:
            _conn.execute("ALTER TABLE sessions ADD COLUMN active_turn_subject TEXT")
        # `model` (per-session "mount your LLM" choice) landed after the first cut
        # of this table; CREATE TABLE IF NOT EXISTS never retrofits it. Migrate
        # additively and idempotently, same posture as active_turn_tier above.
        if "model" not in session_cols:
            _conn.execute("ALTER TABLE sessions ADD COLUMN model TEXT")
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
    their own lifecycle; this store is passive, called-into storage only.

    Held under `_lock` because `_db()` publishes the module-level `_conn` BEFORE
    running the additive column retrofits below it. Every other `_db()` caller
    already holds the lock, so this was the one unserialized entry: a racing
    thread could take the published connection and read a row whose `model`
    column had not been added yet, raising `IndexError: No item with that key`
    from _row_to_session. Locking here makes first-open atomic with respect to
    every reader. (sol-critic PR #117 round 2, blocker 2.)"""
    with _lock:
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
        "model": row["model"],
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
        "consumed": bool(row["consumed"]),
    }


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #
def get_or_create_session(
    tenant_id: str, drawing_id: str, model: Optional[str] = None,
) -> Dict[str, Any]:
    """Idempotent per (tenant_id, drawing_id): INSERT OR IGNORE a fresh candidate
    row under the lock, then SELECT the (possibly pre-existing) row by the
    UNIQUE(tenant_id, drawing_id) key. Concurrent racers all funnel through the
    same lock, so exactly one row is ever created for a given key and every
    caller — winner or loser of the race — reads back the same session.

    `model` is the per-session "mount your LLM" choice (validated upstream against
    the allowed Claude family). When supplied it is persisted on create AND, on a
    repeat attach with a different model, updated — so re-opening a session with a
    new model re-selects it. When None the stored model is left untouched (the
    turn engine falls back to the env default)."""
    ensure_started()
    candidate_id = str(uuid.uuid4())
    now = time.time()
    with _lock:
        conn = _db()
        conn.execute(
            "INSERT OR IGNORE INTO sessions"
            " (session_id, tenant_id, drawing_id, status, created_at, updated_at, last_seq, model)"
            " VALUES (?,?,?,?,?,?,0,?)",
            (candidate_id, tenant_id, drawing_id, "active", now, now, model),
        )
        if model is not None:
            # Reflect an explicit model onto the (possibly pre-existing) row.
            conn.execute(
                "UPDATE sessions SET model = ?, updated_at = ?"
                " WHERE tenant_id = ? AND drawing_id = ?",
                (model, now, tenant_id, drawing_id),
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
        if type in ("turn_complete", "error") and turn_id is not None:
            # Publish the terminal event and release its active-turn CAS in the
            # same transaction. A transcript reader that sees the terminal
            # event can therefore start the next turn without racing the relay's
            # later metering work.
            conn.execute(
                "UPDATE sessions SET active_turn_id = NULL,"
                " turn_started_at = NULL, active_turn_tier = NULL,"
                " active_turn_subject = NULL"
                " WHERE session_id = ? AND active_turn_id = ?",
                (session_id, turn_id),
            )
        conn.commit()
        return seq


def append_confirmation_resolved_once(
    session_id: str, turn_id: str, confirmation_id: str,
    approved: bool, by: str,
) -> tuple[int, bool]:
    """Atomically find or append one exact confirmation resolution event.

    Unlike transcript reads, this lookup is unbounded. It is the durable
    restart/replay uniqueness boundary for a policy decision projection.
    """
    expected = {
        "confirmation_id": confirmation_id,
        "approved": bool(approved),
        "by": by,
    }
    now = time.time()
    with _lock:
        conn = _db()
        conn.row_factory = sqlite3.Row
        session = conn.execute(
            "SELECT last_seq FROM sessions WHERE session_id = ?", (session_id,),
        ).fetchone()
        if session is None:
            raise KeyError(f"unknown session_id {session_id!r}")
        rows = conn.execute(
            "SELECT seq, data_json FROM session_events"
            " WHERE session_id = ? AND turn_id = ?"
            " AND type = 'confirmation_resolved' ORDER BY seq ASC",
            (session_id, turn_id),
        ).fetchall()
        exact = []
        for row in rows:
            try:
                data = json.loads(row["data_json"] or "{}")
            except (TypeError, ValueError):
                raise RuntimeError("malformed confirmation resolution projection")
            if not isinstance(data, dict):
                raise RuntimeError("malformed confirmation resolution projection")
            if data == expected:
                exact.append(int(row["seq"]))
            elif data.get("confirmation_id") == confirmation_id:
                raise RuntimeError("conflicting confirmation resolution projection")
        if len(exact) > 1:
            raise RuntimeError("duplicate confirmation resolution projection")
        if exact:
            return exact[0], False
        seq = int(session["last_seq"]) + 1
        conn.execute(
            "INSERT INTO session_events"
            " (session_id, seq, turn_id, type, data_json, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (session_id, seq, turn_id, "confirmation_resolved",
             json.dumps(expected), now),
        )
        conn.execute(
            "UPDATE sessions SET last_seq = ?, updated_at = ? WHERE session_id = ?",
            (seq, now, session_id),
        )
        conn.commit()
        return seq, True


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
def _turn_is_stale(started_at: Any, max_age_s: Optional[float]) -> bool:
    """Whether try_begin_turn would consider this turn stale enough to seize.

    Same rule as the CAS below (a missing start time is stale), so identity
    reads and turn acquisition can never disagree about which turn is live.
    """
    if max_age_s is None:
        return False
    if started_at is None:
        return True
    try:
        return (time.time() - float(started_at)) > float(max_age_s)
    except (TypeError, ValueError):
        return True


def try_begin_turn(session_id: str, turn_id: str, stale_after_s: float,
                   tier: Optional[str] = None,
                   subject: Optional[str] = None) -> bool:
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
        # The HOLDER's window, not the caller's (see RESERVATION_PREFIX above).
        holder_window = (RESERVATION_STALE_S if is_reservation_holder(active)
                         else stale_after_s)
        is_stale = (not is_free) and (started is None or (now - started) > holder_window)
        if not (is_free or is_stale):
            return False
        conn.execute(
            "UPDATE sessions SET active_turn_id = ?, turn_started_at = ?,"
            " active_turn_tier = ?, active_turn_subject = ?, updated_at = ?"
            " WHERE session_id = ?",
            (turn_id, now, tier, subject, now, session_id),
        )
        conn.commit()
        return True


def active_turn_tier(session_id: str, turn_id: str,
                     tenant_id: str) -> Optional[str]:
    """Return the tier snapshot bound to this exact authenticated turn.

    The session, active turn, and tenant must all match. The value is written
    atomically with the active-turn CAS before the harness can make a gate call.
    """
    rows = _query(
        "SELECT active_turn_tier FROM sessions"
        " WHERE session_id = ? AND active_turn_id = ? AND tenant_id = ?",
        (session_id, turn_id, tenant_id),
    )
    if not rows:
        return None
    value = rows[0]["active_turn_tier"]
    return str(value) if value is not None else None


def active_turn_subject(session_id: str, turn_id: str, tenant_id: str,
                        max_age_s: Optional[float] = None) -> Optional[str]:
    """Return the authenticated subject bound to this exact active turn.

    Session, active turn, and tenant must all match, so a completed or
    superseded turn yields None and the caller fails closed. The turn must also
    be younger than `max_age_s`: try_begin_turn treats an older turn as stale
    and lets a new one take it over, so a turn past that bound is one nothing is
    guarding. Without the age check, an app restart that skipped the watchdog
    would leave the last turn's author resolvable indefinitely.

    Deliberately NOT exposed through the session projections — this is identity
    data, not session state.
    """
    rows = _query(
        "SELECT active_turn_subject, turn_started_at FROM sessions"
        " WHERE session_id = ? AND active_turn_id = ? AND tenant_id = ?",
        (session_id, turn_id, tenant_id),
    )
    if not rows:
        return None
    if _turn_is_stale(rows[0]["turn_started_at"], max_age_s):
        return None
    value = rows[0]["active_turn_subject"]
    return str(value) if value is not None else None


def end_turn(session_id: str, turn_id: str) -> None:
    """Clear active_turn_id/turn_started_at iff they still match turn_id (a
    stale or already-superseded turn_id is a harmless no-op — it never clobbers
    a newer turn that has since taken over via try_begin_turn's stale path)."""
    _exec(
        "UPDATE sessions SET active_turn_id = NULL, turn_started_at = NULL,"
        " active_turn_tier = NULL, active_turn_subject = NULL, updated_at = ?"
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


def list_pending_approvals(
    tenant_id: str, session_id: str, decided_by: str, limit: int = 100, *,
    now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Return one session's live decision and resume inbox, newest first.

    The SQL predicate is the authority. It returns undecided rows plus a
    same-actor decision that still needs its confirm resume. Expired, consumed,
    and other-actor decided rows never become actionable client state. The
    decision and confirm routes still re-check tenant, session, expiry, actor,
    and exact-once consumption under their existing transaction fences.
    """
    bounded = max(1, min(int(limit), 100))
    observed_at = time.time() if now is None else float(now)
    rows = _query(
        "SELECT * FROM approvals"
        " WHERE tenant_id = ? AND session_id = ? AND consumed = 0"
        " AND (decided = 0 OR decided_by = ?)"
        " AND (expires_at IS NULL OR expires_at > ?)"
        " ORDER BY created_at DESC, confirmation_id DESC LIMIT ?",
        (str(tenant_id), str(session_id), str(decided_by), observed_at, bounded),
    )
    return [_row_to_approval(row) for row in rows]


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


class ApprovalConsumeError(Exception):
    """Raised by consume_approval() on any non-success outcome. ``reason`` is
    one of:

      'not_found'         -- no row for confirmation_id, OR the row's
                             session_id/tenant_id doesn't match the caller's.
                             These three cases are DELIBERATELY collapsed into
                             one indistinguishable outcome (mirrors get_approval
                             callers' existing no-existence-leak posture in
                             routers/agent.py and routers/sessions.py) -- a
                             cross-session or cross-tenant caller can never
                             learn "the confirmation_id is real, just not
                             yours" from the failure alone.
      'undecided'         -- no decision has been recorded yet (decided=0) --
                             i.e. the caller is trying to jump straight to the
                             confirm message WITHOUT ever POSTing
                             /api/agent/approvals first.
      'expired'           -- the approval's TTL has lapsed (time.time() >
                             expires_at).
      'already_consumed'  -- a prior consume_approval() call for this
                             confirmation_id already won (replay of the same
                             confirm message).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def consume_approval(
    confirmation_id: str, session_id: str, tenant_id: str, *,
    decided_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Verify-and-consume an approval for the messages{confirm} resume path,
    in ONE atomic locked transaction (SELECT -> validate -> UPDATE consumed=1
    -> commit) so two concurrent callers racing the SAME confirmation_id can
    never both win: exactly one sees the check-then-set succeed, the other
    raises ApprovalConsumeError('already_consumed') -- this is what makes a
    replayed confirm message safely rejected rather than a second resume
    firing off two turns from one approval.

    Checks, in order (see ApprovalConsumeError's own docstring for what each
    reason means): row exists AND session_id matches AND tenant_id matches
    (collapsed -> 'not_found') -> decided (-> 'undecided' if not) -> not
    already consumed (-> 'already_consumed' if it is) -> not expired (->
    'expired' if it is).

    On success, marks consumed=1 and returns the full approval dict --
    INCLUDING the durably STORED `approved` value. Callers MUST resume the
    turn using THIS stored value, never whatever boolean the client happened
    to send on the confirm message -- a client that sends {approved: true}
    against a row that was actually decided/stored as approved=false must
    still resume with approved=false (the client cannot reverse a rejection
    by lying on the confirm call). This is wire-compatible with every real
    client: console/converse.js's `approve()` ALWAYS POSTs
    /api/agent/approvals/{confirmationId} (which durably records the decision
    via decide_approval, above) BEFORE its caller ever sends the confirm
    message via postMessage({confirm}) -- see that file's own `approve()` /
    `postMessage()` comments -- so a real client's confirm.approved is always
    already reflected in the stored row by the time this runs, and requiring
    `decided` here rejects only a client that skips the approvals call
    entirely (attack case, not a real UX path)."""
    now = time.time()
    with _lock:
        conn = _db()
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT * FROM approvals WHERE confirmation_id = ?", (confirmation_id,)
        )
        row = cur.fetchone()
        if row is None:
            raise ApprovalConsumeError("not_found")
        if str(row["session_id"]) != str(session_id) or str(row["tenant_id"]) != str(tenant_id):
            # collapsed with the unknown-row case above -- no existence leak.
            raise ApprovalConsumeError("not_found")
        if not row["decided"]:
            raise ApprovalConsumeError("undecided")
        if decided_by is not None and row["decided_by"] != decided_by:
            # Collapse a different tenant member with the unknown-row case.
            # The member who resumes must be the member who clicked the chip.
            raise ApprovalConsumeError("not_found")
        if row["consumed"]:
            raise ApprovalConsumeError("already_consumed")
        expires_at = row["expires_at"]
        if expires_at is not None and now > expires_at:
            raise ApprovalConsumeError("expired")
        conn.execute(
            "UPDATE approvals SET consumed = 1 WHERE confirmation_id = ?",
            (confirmation_id,),
        )
        conn.commit()
        result = _row_to_approval(row)
        result["consumed"] = True
        return result


def unconsume_approval(confirmation_id: str, session_id: str, tenant_id: str) -> bool:
    """Give back an approval that was consumed but never actually redeemed.

    consume_approval() marks the row spent BEFORE its caller knows whether the
    turn it was consumed for will really start. When the caller can PROVE the
    turn did not start (routers/sessions.py's TurnBusy path -- the turn CAS was
    lost, so no `turn_started` event was appended and the harness was never
    called), the approval was not redeemed by anyone and must go back on the
    shelf; otherwise the user's single approval is destroyed by a concurrent
    turn and the retry the 409 invites can only ever fail 'already_consumed'.

    This does NOT weaken single-redeem. The row is consumed=1 for the whole
    window between the consume and this call, so no other caller can consume it
    meanwhile -- the ONLY writer that can reach a given consumed=1 row is the
    one that set it. Redemption therefore stays strictly one-shot: this flips
    1 -> 0 only for a redemption that provably never happened, and a replay of
    a confirm whose turn DID start still hits 'already_consumed'.

    `session_id`/`tenant_id` must match the stored row, same ownership guard
    consume_approval enforces -- a cross-session or cross-tenant caller can
    never un-spend someone else's approval.

    Returns True iff this call actually flipped a consumed=1 row back to
    consumed=0; False (a harmless no-op) for an unknown/foreign row or one that
    was not consumed to begin with."""
    with _lock:
        conn = _db()
        cur = conn.execute(
            "UPDATE approvals SET consumed = 0"
            " WHERE confirmation_id = ? AND session_id = ? AND tenant_id = ?"
            " AND consumed = 1",
            (confirmation_id, str(session_id), str(tenant_id)),
        )
        conn.commit()
        return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# PostgreSQL authority seam
# --------------------------------------------------------------------------- #
# Keep the SQLite implementation above intact. These aliases let the public
# wrappers below select a backend at call time, so operators can change the
# mode without rebuilding the image and existing tests can keep redirecting
# SESSIONS_DB exactly as before.
_legacy_ensure_started = ensure_started
_legacy_get_or_create_session = get_or_create_session
_legacy_get_session = get_session
_legacy_append_event = append_event
_legacy_append_confirmation_resolved_once = append_confirmation_resolved_once
_legacy_events_after = events_after
_legacy_recent_events = recent_events
_legacy_try_begin_turn = try_begin_turn
_legacy_active_turn_tier = active_turn_tier
_legacy_active_turn_subject = active_turn_subject
_legacy_end_turn = end_turn
_legacy_create_approval = create_approval
_legacy_get_approval = get_approval
_legacy_list_pending_approvals = list_pending_approvals
_legacy_decide_approval = decide_approval
_legacy_consume_approval = consume_approval
_legacy_unconsume_approval = unconsume_approval

_STORE_MODES = {
    "legacy", "dual_write", "dual_write_shadow", "shadow", "postgres",
}
_DUAL_WRITE_MODES = {"dual_write", "dual_write_shadow"}
_SHADOW_READ_MODES = {"shadow", "dual_write_shadow"}
_PROJECT_ROOT = SERVER_DIR.parent


def _store_mode() -> str:
    """Return the call-time session authority mode, rejecting unsafe typos.

    ``legacy`` uses SQLite only. ``dual_write`` mirrors writes while reads stay
    on SQLite. ``dual_write_shadow`` also compares reads. ``shadow`` compares
    reads but never writes PostgreSQL. ``postgres`` is PostgreSQL authority.
    Turn-fence dual writes are valid only during the single-task migration
    phase because SQLite and PostgreSQL cannot share one atomic transaction.
    """
    value = os.environ.get("LEAF_SESSIONS_STORE", "legacy").strip().lower()
    if value not in _STORE_MODES:
        supported = ", ".join(sorted(_STORE_MODES))
        raise RuntimeError(
            f"invalid LEAF_SESSIONS_STORE {value!r}; expected one of: {supported}"
        )
    return value


def _platform_db():
    """Load the local platform database package without shadowing stdlib platform."""
    loaded = sys.modules.get("leaf_platform")
    if loaded is None:
        pkg_dir = _PROJECT_ROOT / "platform"
        spec = importlib.util.spec_from_file_location(
            "leaf_platform", pkg_dir / "__init__.py",
            submodule_search_locations=[str(pkg_dir)],
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load the Leaf platform database package")
        loaded = importlib.util.module_from_spec(spec)
        sys.modules["leaf_platform"] = loaded
        spec.loader.exec_module(loaded)
    from leaf_platform import db
    return db


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list, int, float, bool)):
        return value
    return json.loads(value)


def _pg_session(row: Dict[str, Any]) -> Dict[str, Any]:
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
        "model": row["model"],
    }


def _pg_envelope(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "v": 1,
        "session_id": row["session_id"],
        "turn_id": row["turn_id"],
        "seq": row["seq"],
        "type": row["type"],
        "data": _json_value(row["data_json"]) or {},
    }


def _pg_approval(row: Dict[str, Any]) -> Dict[str, Any]:
    expires_at = row["expires_at"]
    return {
        "confirmation_id": row["confirmation_id"],
        "session_id": row["session_id"],
        "tenant_id": row["tenant_id"],
        "turn_id": row["turn_id"],
        "tool": row["tool"],
        "params": _json_value(row["params_json"]),
        "capability": row["capability"],
        "rationale": row["rationale"],
        "kind": row["kind"],
        "payload": _json_value(row["payload_json"]),
        "decided": bool(row["decided"]),
        "approved": (bool(row["approved"]) if row["approved"] is not None else None),
        "decided_by": row["decided_by"],
        "created_at": row["created_at"],
        "expires_at": expires_at,
        "expired": bool(expires_at is not None and time.time() > expires_at),
        "consumed": bool(row["consumed"]),
    }


def _pg_ensure_started() -> None:
    db = _platform_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT to_regclass('app_sessions') AS sessions,"
            " to_regclass('app_session_events') AS events,"
            " to_regclass('app_approvals') AS approvals"
        )
        row = cur.fetchone()
    if not row or not all(row.values()):
        raise RuntimeError(
            "PostgreSQL session schema is unavailable; apply 0012_sessions.sql"
        )


def _pg_get_or_create_session(
    tenant_id: str, drawing_id: str, *,
    session_id: Optional[str] = None, created_at: Optional[float] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    candidate_id = session_id or str(uuid.uuid4())
    now = created_at if created_at is not None else time.time()
    db = _platform_db()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO app_sessions"
            " (session_id, tenant_id, drawing_id, status, created_at, updated_at, last_seq, model)"
            " VALUES (%s,%s,%s,'active',%s,%s,0,%s)"
            " ON CONFLICT (tenant_id, drawing_id) DO NOTHING",
            (candidate_id, tenant_id, drawing_id, now, now, model),
        )
        if model is not None:
            conn.execute(
                "UPDATE app_sessions SET model = %s, updated_at = %s"
                " WHERE tenant_id = %s AND drawing_id = %s",
                (model, now, tenant_id, drawing_id),
            )
        row = conn.execute(
            "SELECT * FROM app_sessions WHERE tenant_id = %s AND drawing_id = %s",
            (tenant_id, drawing_id),
        ).fetchone()
    if row is None:
        raise RuntimeError("PostgreSQL session insert did not return a row")
    return _pg_session(row)


def _pg_get_session(session_id: str) -> Optional[Dict[str, Any]]:
    db = _platform_db()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM app_sessions WHERE session_id = %s", (session_id,))
        row = cur.fetchone()
    return _pg_session(row) if row else None


def _legacy_turn_fence(session_id: str) -> Optional[tuple]:
    rows = _query(
        "SELECT active_turn_id, turn_started_at, active_turn_tier,"
        " active_turn_subject FROM sessions WHERE session_id = ?",
        (session_id,),
    )
    if not rows:
        return None
    row = rows[0]
    return (
        row["active_turn_id"], row["turn_started_at"], row["active_turn_tier"],
        row["active_turn_subject"],
    )


def _pg_turn_fence(session_id: str) -> Optional[tuple]:
    db = _platform_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT active_turn_id, turn_started_at, active_turn_tier,"
            " active_turn_subject FROM app_sessions WHERE session_id = %s",
            (session_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return (
        row["active_turn_id"], row["turn_started_at"], row["active_turn_tier"],
        row["active_turn_subject"],
    )


def _pg_append_event(
    session_id: str, turn_id: Optional[str], type: str, data: Dict[str, Any],
    *, expected_seq: Optional[int] = None, updated_at: Optional[float] = None,
) -> int:
    now = updated_at if updated_at is not None else time.time()
    db = _platform_db()
    with db.transaction() as conn:
        row = conn.execute(
            "UPDATE app_sessions SET last_seq = last_seq + 1, updated_at = %s"
            " WHERE session_id = %s RETURNING last_seq",
            (now, session_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown session_id {session_id!r}")
        seq = int(row["last_seq"])
        if expected_seq is not None and seq != expected_seq:
            raise RuntimeError(
                f"session event sequence mismatch: legacy={expected_seq}, postgres={seq}"
            )
        conn.execute(
            "INSERT INTO app_session_events"
            " (session_id, seq, turn_id, type, data_json, created_at)"
            " VALUES (%s,%s,%s,%s,%s::jsonb,%s)",
            (session_id, seq, turn_id, type,
             json.dumps(data if data is not None else {}), now),
        )
        if type in ("turn_complete", "error") and turn_id is not None:
            conn.execute(
                "UPDATE app_sessions SET active_turn_id = NULL,"
                " turn_started_at = NULL, active_turn_tier = NULL,"
                " active_turn_subject = NULL"
                " WHERE session_id = %s AND active_turn_id = %s",
                (session_id, turn_id),
            )
    return seq


def _pg_append_confirmation_resolved_once(
    session_id: str, turn_id: str, confirmation_id: str,
    approved: bool, by: str, *, expected_seq: Optional[int] = None,
) -> tuple[int, bool]:
    expected = {
        "confirmation_id": confirmation_id,
        "approved": bool(approved),
        "by": by,
    }
    data_json = json.dumps(expected)
    now = time.time()
    db = _platform_db()
    with db.transaction() as conn:
        session = conn.execute(
            "SELECT last_seq FROM app_sessions WHERE session_id = %s FOR UPDATE",
            (session_id,),
        ).fetchone()
        if session is None:
            raise KeyError(f"unknown session_id {session_id!r}")
        rows = conn.execute(
            "SELECT seq, data_json FROM app_session_events"
            " WHERE session_id = %s AND turn_id = %s"
            " AND type = 'confirmation_resolved' ORDER BY seq ASC",
            (session_id, turn_id),
        ).fetchall()
        exact = []
        for row in rows:
            data = _json_value(row["data_json"])
            if not isinstance(data, dict):
                raise RuntimeError("malformed confirmation resolution projection")
            if data == expected:
                exact.append(int(row["seq"]))
            elif data.get("confirmation_id") == confirmation_id:
                raise RuntimeError("conflicting confirmation resolution projection")
        if len(exact) > 1:
            raise RuntimeError("duplicate confirmation resolution projection")
        if exact:
            seq = exact[0]
            if expected_seq is not None and seq != expected_seq:
                raise RuntimeError(
                    "confirmation resolution sequence mismatch: "
                    f"legacy={expected_seq}, postgres={seq}"
                )
            return seq, False
        seq = int(session["last_seq"]) + 1
        if expected_seq is not None and seq != expected_seq:
            raise RuntimeError(
                "confirmation resolution sequence mismatch: "
                f"legacy={expected_seq}, postgres={seq}"
            )
        conn.execute(
            "INSERT INTO app_session_events"
            " (session_id, seq, turn_id, type, data_json, created_at)"
            " VALUES (%s,%s,%s,'confirmation_resolved',%s::jsonb,%s)",
            (session_id, seq, turn_id, data_json, now),
        )
        conn.execute(
            "UPDATE app_sessions SET last_seq = %s, updated_at = %s"
            " WHERE session_id = %s",
            (seq, now, session_id),
        )
        return seq, True


def _pg_events_after(
    session_id: str, after_seq: int, limit: int = 500,
) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 10000))
    db = _platform_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM app_session_events WHERE session_id = %s AND seq > %s"
            " ORDER BY seq ASC LIMIT %s",
            (session_id, int(after_seq), limit),
        )
        rows = cur.fetchall()
    return [_pg_envelope(row) for row in rows]


def _pg_recent_events(session_id: str, limit: int) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 10000))
    db = _platform_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM app_session_events WHERE session_id = %s"
            " ORDER BY seq DESC LIMIT %s",
            (session_id, limit),
        )
        rows = cur.fetchall()
    return [_pg_envelope(row) for row in reversed(rows)]


def _pg_try_begin_turn(
    session_id: str, turn_id: str, stale_after_s: float,
    tier: Optional[str] = None, subject: Optional[str] = None,
    *, started_at: Optional[float] = None,
) -> bool:
    now = started_at if started_at is not None else time.time()
    db = _platform_db()
    with db.transaction() as conn:
        return pg_try_begin_turn_in_transaction(
            conn, session_id, turn_id, stale_after_s, tier=tier,
            subject=subject, started_at=now,
        )


def pg_try_begin_turn_in_transaction(
    conn: Any, session_id: str, turn_id: str, stale_after_s: float,
    tier: Optional[str] = None, subject: Optional[str] = None, *,
    started_at: Optional[float] = None,
) -> bool:
    """Lock and acquire a PostgreSQL session turn on the caller's transaction."""
    now = started_at if started_at is not None else time.time()
    row = conn.execute(
        "SELECT active_turn_id, turn_started_at FROM app_sessions"
        " WHERE session_id=%s FOR UPDATE",
        (session_id,),
    ).fetchone()
    if row is None:
        return False
    active = row["active_turn_id"]
    started = row["turn_started_at"]
    holder_window = RESERVATION_STALE_S if is_reservation_holder(active) else stale_after_s
    is_free = active is None
    is_stale = not is_free and (
        started is None or now - float(started) > float(holder_window)
    )
    if not (is_free or is_stale):
        return False
    updated = conn.execute(
        "UPDATE app_sessions SET active_turn_id=%s, turn_started_at=%s,"
        " active_turn_tier=%s, active_turn_subject=%s, updated_at=%s"
        " WHERE session_id=%s RETURNING session_id",
        (turn_id, now, tier, subject, now, session_id),
    ).fetchone()
    return updated is not None


def _pg_active_turn_tier(
    session_id: str, turn_id: str, tenant_id: str,
) -> Optional[str]:
    db = _platform_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT active_turn_tier FROM app_sessions"
            " WHERE session_id = %s AND active_turn_id = %s AND tenant_id = %s",
            (session_id, turn_id, tenant_id),
        )
        row = cur.fetchone()
    if not row:
        return None
    value = row["active_turn_tier"]
    return str(value) if value is not None else None


def _pg_active_turn_subject(
    session_id: str, turn_id: str, tenant_id: str,
    max_age_s: Optional[float] = None,
) -> Optional[str]:
    db = _platform_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT active_turn_subject, turn_started_at FROM app_sessions"
            " WHERE session_id = %s AND active_turn_id = %s AND tenant_id = %s",
            (session_id, turn_id, tenant_id),
        )
        row = cur.fetchone()
    if not row:
        return None
    if _turn_is_stale(row["turn_started_at"], max_age_s):
        return None
    value = row["active_turn_subject"]
    return str(value) if value is not None else None


def _pg_end_turn(
    session_id: str, turn_id: str, *, updated_at: Optional[float] = None,
) -> bool:
    db = _platform_db()
    with db.cursor() as cur:
        cur.execute(
            "UPDATE app_sessions SET active_turn_id = NULL, turn_started_at = NULL,"
            " active_turn_tier = NULL, active_turn_subject = NULL, updated_at = %s"
            " WHERE session_id = %s AND active_turn_id = %s"
            " RETURNING session_id",
            (updated_at if updated_at is not None else time.time(), session_id, turn_id),
        )
        row = cur.fetchone()
    return row is not None


def _pg_create_approval(
    confirmation_id: str, session_id: str, tenant_id: str,
    turn_id: Optional[str], tool: Optional[str],
    params: Optional[Dict[str, Any]], capability: Optional[str],
    rationale: Optional[str], kind: Optional[str],
    payload: Optional[Dict[str, Any]], ttl_s: float, *,
    created_at: Optional[float] = None,
) -> None:
    now = created_at if created_at is not None else time.time()
    db = _platform_db()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO app_approvals"
            " (confirmation_id, session_id, tenant_id, turn_id, tool, params_json,"
            " capability, rationale, kind, payload_json, decided, approved,"
            " decided_by, created_at, expires_at, consumed)"
            " VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb,"
            " FALSE,NULL,NULL,%s,%s,FALSE)",
            (confirmation_id, session_id, tenant_id, turn_id, tool,
             json.dumps(params) if params is not None else None,
             capability, rationale, kind,
             json.dumps(payload) if payload is not None else None,
             now, now + float(ttl_s)),
        )


def _pg_get_approval(confirmation_id: str) -> Optional[Dict[str, Any]]:
    db = _platform_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM app_approvals WHERE confirmation_id = %s",
            (confirmation_id,),
        )
        row = cur.fetchone()
    return _pg_approval(row) if row else None


def _pg_list_pending_approvals(
    tenant_id: str, session_id: str, decided_by: str, limit: int = 100, *,
    now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    bounded = max(1, min(int(limit), 100))
    observed_at = time.time() if now is None else float(now)
    db = _platform_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM app_approvals"
            " WHERE tenant_id = %s AND session_id = %s AND consumed = FALSE"
            " AND (decided = FALSE OR decided_by = %s)"
            " AND (expires_at IS NULL OR expires_at > %s)"
            " ORDER BY created_at DESC, confirmation_id DESC LIMIT %s",
            (str(tenant_id), str(session_id), str(decided_by), observed_at, bounded),
        )
        rows = cur.fetchall()
    return [_pg_approval(row) for row in rows]


def _pg_decide_approval(
    confirmation_id: str, approved: bool, by: Optional[str] = None,
) -> str:
    db = _platform_db()
    with db.transaction() as conn:
        row = conn.execute(
            "UPDATE app_approvals SET decided = TRUE, approved = %s, decided_by = %s"
            " WHERE confirmation_id = %s AND decided = FALSE"
            " RETURNING confirmation_id",
            (approved, by, confirmation_id),
        ).fetchone()
        if row is not None:
            return "recorded"
        exists = conn.execute(
            "SELECT 1 FROM app_approvals WHERE confirmation_id = %s",
            (confirmation_id,),
        ).fetchone()
    return "already_decided" if exists else "not_found"


def _pg_consume_approval(
    confirmation_id: str, session_id: str, tenant_id: str, *,
    decided_by: Optional[str] = None,
) -> Dict[str, Any]:
    now = time.time()
    db = _platform_db()
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM app_approvals WHERE confirmation_id = %s FOR UPDATE",
            (confirmation_id,),
        ).fetchone()
        if row is None:
            raise ApprovalConsumeError("not_found")
        if str(row["session_id"]) != str(session_id) or str(row["tenant_id"]) != str(tenant_id):
            raise ApprovalConsumeError("not_found")
        if not row["decided"]:
            raise ApprovalConsumeError("undecided")
        if decided_by is not None and row["decided_by"] != decided_by:
            raise ApprovalConsumeError("not_found")
        if row["consumed"]:
            raise ApprovalConsumeError("already_consumed")
        expires_at = row["expires_at"]
        if expires_at is not None and now > expires_at:
            raise ApprovalConsumeError("expired")
        conn.execute(
            "UPDATE app_approvals SET consumed = TRUE WHERE confirmation_id = %s",
            (confirmation_id,),
        )
    result = _pg_approval(row)
    result["consumed"] = True
    return result


def _pg_unconsume_approval(
    confirmation_id: str, session_id: str, tenant_id: str,
) -> bool:
    db = _platform_db()
    with db.transaction() as conn:
        row = conn.execute(
            "UPDATE app_approvals SET consumed = FALSE"
            " WHERE confirmation_id = %s AND session_id = %s AND tenant_id = %s"
            " AND consumed = TRUE RETURNING confirmation_id",
            (confirmation_id, str(session_id), str(tenant_id)),
        ).fetchone()
    return row is not None


def _shadow_equal(label: str, legacy: Any, postgres: Any) -> None:
    """Fail closed when a shadow read disagrees with the legacy authority."""
    if legacy != postgres:
        raise RuntimeError(f"{label} shadow mismatch")


def ensure_started() -> None:
    mode = _store_mode()
    if mode != "postgres":
        _legacy_ensure_started()
    if mode != "legacy":
        _pg_ensure_started()


def get_or_create_session(
    tenant_id: str, drawing_id: str, model: Optional[str] = None,
) -> Dict[str, Any]:
    mode = _store_mode()
    if mode == "postgres":
        return _pg_get_or_create_session(tenant_id, drawing_id, model=model)
    if mode in _DUAL_WRITE_MODES:
        # Do not mutate the legacy authority when its required mirror is
        # already known to be unavailable.
        _pg_ensure_started()
    legacy = _legacy_get_or_create_session(tenant_id, drawing_id, model)
    if mode in _DUAL_WRITE_MODES:
        postgres = _pg_get_or_create_session(
            tenant_id, drawing_id,
            session_id=legacy["session_id"], created_at=legacy["created_at"],
            model=legacy["model"],
        )
        _shadow_equal("session identity", legacy["session_id"], postgres["session_id"])
        if mode in _SHADOW_READ_MODES:
            _shadow_equal("session", legacy, postgres)
    elif mode == "shadow":
        _shadow_equal("session", legacy, _pg_get_session(legacy["session_id"]))
    return legacy


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    mode = _store_mode()
    if mode == "postgres":
        return _pg_get_session(session_id)
    legacy = _legacy_get_session(session_id)
    if mode in _SHADOW_READ_MODES:
        _shadow_equal("session", legacy, _pg_get_session(session_id))
    return legacy


def append_event(
    session_id: str, turn_id: Optional[str], type: str, data: Dict[str, Any],
) -> int:
    mode = _store_mode()
    if mode == "postgres":
        return _pg_append_event(session_id, turn_id, type, data)
    if mode in _DUAL_WRITE_MODES:
        postgres_before = _pg_get_session(session_id)
        if postgres_before is None:
            raise RuntimeError("PostgreSQL session mirror is missing")
    seq = _legacy_append_event(session_id, turn_id, type, data)
    if mode in _DUAL_WRITE_MODES:
        mirrored = _legacy_get_session(session_id)
        _pg_append_event(
            session_id, turn_id, type, data, expected_seq=seq,
            updated_at=mirrored["updated_at"] if mirrored else None,
        )
    return seq


def append_confirmation_resolved_once(
    session_id: str, turn_id: str, confirmation_id: str,
    approved: bool, by: str,
) -> tuple[int, bool]:
    mode = _store_mode()
    if mode == "postgres":
        return _pg_append_confirmation_resolved_once(
            session_id, turn_id, confirmation_id, approved, by,
        )
    if mode in _DUAL_WRITE_MODES and _pg_get_session(session_id) is None:
        raise RuntimeError("PostgreSQL session mirror is missing")
    legacy_seq, legacy_inserted = _legacy_append_confirmation_resolved_once(
        session_id, turn_id, confirmation_id, approved, by,
    )
    if mode in _DUAL_WRITE_MODES:
        pg_seq, pg_inserted = _pg_append_confirmation_resolved_once(
            session_id, turn_id, confirmation_id, approved, by,
            expected_seq=legacy_seq,
        )
        _shadow_equal("confirmation resolution sequence", legacy_seq, pg_seq)
        return legacy_seq, legacy_inserted or pg_inserted
    return legacy_seq, legacy_inserted


def events_after(
    session_id: str, after_seq: int, limit: int = 500,
) -> List[Dict[str, Any]]:
    mode = _store_mode()
    if mode == "postgres":
        return _pg_events_after(session_id, after_seq, limit)
    legacy = _legacy_events_after(session_id, after_seq, limit)
    if mode in _SHADOW_READ_MODES:
        _shadow_equal(
            "session events", legacy, _pg_events_after(session_id, after_seq, limit),
        )
    return legacy


def recent_events(session_id: str, limit: int) -> List[Dict[str, Any]]:
    mode = _store_mode()
    if mode == "postgres":
        return _pg_recent_events(session_id, limit)
    legacy = _legacy_recent_events(session_id, limit)
    if mode in _SHADOW_READ_MODES:
        _shadow_equal("recent session events", legacy, _pg_recent_events(session_id, limit))
    return legacy


def try_begin_turn(
    session_id: str, turn_id: str, stale_after_s: float,
    tier: Optional[str] = None, subject: Optional[str] = None,
) -> bool:
    mode = _store_mode()
    if mode == "postgres":
        return _pg_try_begin_turn(session_id, turn_id, stale_after_s, tier, subject)
    if mode in _DUAL_WRITE_MODES or mode == "shadow":
        # Cross-store turn mirroring is a migration aid for the single-task
        # phase only. No transaction can atomically fence SQLite and Postgres.
        legacy_before = _legacy_turn_fence(session_id)
        postgres_before = _pg_turn_fence(session_id)
        _shadow_equal("turn fence before acquisition", legacy_before, postgres_before)
    legacy = _legacy_try_begin_turn(session_id, turn_id, stale_after_s, tier, subject)
    if mode in _DUAL_WRITE_MODES:
        legacy_after = _legacy_turn_fence(session_id)
        if legacy:
            if legacy_after is None:
                raise RuntimeError("legacy turn fence disappeared after acquisition")
            postgres = _pg_try_begin_turn(
                session_id, turn_id, stale_after_s, tier, subject,
                started_at=legacy_after[1],
            )
            if not postgres:
                raise RuntimeError("PostgreSQL turn acquisition mirror failed")
        # A legacy no-op must never trigger a mutating PostgreSQL CAS.
        postgres_after = _pg_turn_fence(session_id)
        _shadow_equal("turn fence after acquisition", legacy_after, postgres_after)
    elif mode == "shadow":
        _shadow_equal(
            "turn fence after acquisition",
            _legacy_turn_fence(session_id), _pg_turn_fence(session_id),
        )
    return legacy


def active_turn_tier(
    session_id: str, turn_id: str, tenant_id: str,
) -> Optional[str]:
    mode = _store_mode()
    if mode == "postgres":
        return _pg_active_turn_tier(session_id, turn_id, tenant_id)
    legacy = _legacy_active_turn_tier(session_id, turn_id, tenant_id)
    if mode in _SHADOW_READ_MODES:
        _shadow_equal(
            "active turn tier", legacy,
            _pg_active_turn_tier(session_id, turn_id, tenant_id),
        )
    return legacy


def active_turn_subject(
    session_id: str, turn_id: str, tenant_id: str,
    max_age_s: Optional[float] = None,
) -> Optional[str]:
    mode = _store_mode()
    if mode == "postgres":
        return _pg_active_turn_subject(session_id, turn_id, tenant_id, max_age_s)
    legacy = _legacy_active_turn_subject(session_id, turn_id, tenant_id, max_age_s)
    if mode in _SHADOW_READ_MODES:
        _shadow_equal(
            "active turn subject", legacy,
            _pg_active_turn_subject(session_id, turn_id, tenant_id, max_age_s),
        )
    return legacy


def end_turn(session_id: str, turn_id: str) -> None:
    mode = _store_mode()
    if mode == "postgres":
        _pg_end_turn(session_id, turn_id)
        return
    if mode in _DUAL_WRITE_MODES or mode == "shadow":
        legacy_before = _legacy_turn_fence(session_id)
        postgres_before = _pg_turn_fence(session_id)
        _shadow_equal("turn fence before release", legacy_before, postgres_before)
    if mode in _DUAL_WRITE_MODES:
        should_clear = bool(legacy_before and legacy_before[0] == turn_id)
    _legacy_end_turn(session_id, turn_id)
    if mode in _DUAL_WRITE_MODES:
        mirrored = _legacy_get_session(session_id)
        if should_clear:
            cleared = _pg_end_turn(
                session_id, turn_id,
                updated_at=mirrored["updated_at"] if mirrored else None,
            )
            if not cleared:
                raise RuntimeError("PostgreSQL turn release mirror failed")
        # A legacy no-op must never clear a PostgreSQL turn.
        _shadow_equal(
            "turn fence after release",
            _legacy_turn_fence(session_id), _pg_turn_fence(session_id),
        )
    elif mode == "shadow":
        _shadow_equal(
            "turn fence after release",
            _legacy_turn_fence(session_id), _pg_turn_fence(session_id),
        )


def create_approval(
    confirmation_id: str, session_id: str, tenant_id: str,
    turn_id: Optional[str], tool: Optional[str],
    params: Optional[Dict[str, Any]], capability: Optional[str],
    rationale: Optional[str], kind: Optional[str],
    payload: Optional[Dict[str, Any]], ttl_s: float,
) -> None:
    mode = _store_mode()
    if mode == "postgres":
        _pg_create_approval(
            confirmation_id, session_id, tenant_id, turn_id, tool, params,
            capability, rationale, kind, payload, ttl_s,
        )
        return
    if mode in _DUAL_WRITE_MODES:
        _pg_ensure_started()
        if _pg_get_approval(confirmation_id) is not None:
            raise RuntimeError("PostgreSQL approval mirror already exists")
    _legacy_create_approval(
        confirmation_id, session_id, tenant_id, turn_id, tool, params,
        capability, rationale, kind, payload, ttl_s,
    )
    if mode in _DUAL_WRITE_MODES:
        legacy = _legacy_get_approval(confirmation_id)
        if legacy is None:
            raise RuntimeError("legacy approval disappeared before PostgreSQL mirror")
        _pg_create_approval(
            confirmation_id, session_id, tenant_id, turn_id, tool, params,
            capability, rationale, kind, payload, ttl_s,
            created_at=legacy["created_at"],
        )


def get_approval(confirmation_id: str) -> Optional[Dict[str, Any]]:
    mode = _store_mode()
    if mode == "postgres":
        return _pg_get_approval(confirmation_id)
    legacy = _legacy_get_approval(confirmation_id)
    if mode in _SHADOW_READ_MODES:
        postgres = _pg_get_approval(confirmation_id)
        # `expired` is computed from the wall clock independently. Exclude it
        # from parity because a row may cross its expiry boundary between reads.
        left = dict(legacy) if legacy else None
        right = dict(postgres) if postgres else None
        if left is not None:
            left.pop("expired", None)
        if right is not None:
            right.pop("expired", None)
        _shadow_equal("approval", left, right)
    return legacy


def list_pending_approvals(
    tenant_id: str, session_id: str, decided_by: str, limit: int = 100,
) -> List[Dict[str, Any]]:
    mode = _store_mode()
    observed_at = time.time()
    if mode == "postgres":
        return _pg_list_pending_approvals(
            tenant_id, session_id, decided_by, limit, now=observed_at)
    legacy = _legacy_list_pending_approvals(
        tenant_id, session_id, decided_by, limit, now=observed_at)
    if mode in _SHADOW_READ_MODES:
        postgres = _pg_list_pending_approvals(
            tenant_id, session_id, decided_by, limit, now=observed_at)
        left = [{k: v for k, v in row.items() if k != "expired"} for row in legacy]
        right = [{k: v for k, v in row.items() if k != "expired"} for row in postgres]
        _shadow_equal("pending approvals", left, right)
    return legacy


def decide_approval(
    confirmation_id: str, approved: bool, by: Optional[str] = None,
) -> str:
    mode = _store_mode()
    if mode == "postgres":
        return _pg_decide_approval(confirmation_id, approved, by)
    if mode in _DUAL_WRITE_MODES:
        postgres_before = _pg_get_approval(confirmation_id)
        legacy_before = _legacy_get_approval(confirmation_id)
        _shadow_equal(
            "approval presence",
            legacy_before is not None, postgres_before is not None,
        )
    legacy = _legacy_decide_approval(confirmation_id, approved, by)
    if mode in _DUAL_WRITE_MODES:
        postgres = _pg_decide_approval(confirmation_id, approved, by)
        _shadow_equal("approval decision", legacy, postgres)
    return legacy


def consume_approval(
    confirmation_id: str, session_id: str, tenant_id: str, *,
    decided_by: Optional[str] = None,
) -> Dict[str, Any]:
    mode = _store_mode()
    if mode == "postgres":
        return _pg_consume_approval(
            confirmation_id, session_id, tenant_id, decided_by=decided_by)
    if mode in _DUAL_WRITE_MODES:
        postgres_before = _pg_get_approval(confirmation_id)
        legacy_before = _legacy_get_approval(confirmation_id)
        _shadow_equal(
            "approval presence",
            legacy_before is not None, postgres_before is not None,
        )
    legacy = _legacy_consume_approval(
        confirmation_id, session_id, tenant_id, decided_by=decided_by)
    if mode in _DUAL_WRITE_MODES:
        postgres = _pg_consume_approval(
            confirmation_id, session_id, tenant_id, decided_by=decided_by)
        left, right = dict(legacy), dict(postgres)
        left.pop("expired", None)
        right.pop("expired", None)
        _shadow_equal("approval consumption", left, right)
    return legacy


def unconsume_approval(
    confirmation_id: str, session_id: str, tenant_id: str,
) -> bool:
    mode = _store_mode()
    if mode == "postgres":
        return _pg_unconsume_approval(confirmation_id, session_id, tenant_id)
    # ORDER MATTERS, and it is the REVERSE of consume_approval's.
    #
    # consume_approval gates on LEGACY first (it only calls _pg_consume after
    # _legacy_consume succeeds), so legacy is what actually blocks a second
    # consume. A release must therefore free legacy LAST: while the release is
    # half-applied, legacy is still consumed=1, so a concurrent consume fails
    # `already_consumed` at the legacy gate and never touches PostgreSQL.
    #
    # Releasing legacy first is unsafe and _shadow_equal cannot catch it: the
    # concurrent consume would re-take legacy (1), then raise against the
    # still-consumed PostgreSQL row, and this release would then clear
    # PostgreSQL — leaving legacy consumed and PostgreSQL free. BOTH release
    # calls return True in that interleaving, so the shadow comparison passes
    # while the two stores genuinely disagree.
    if mode in _DUAL_WRITE_MODES:
        postgres = _pg_unconsume_approval(confirmation_id, session_id, tenant_id)
        legacy = _legacy_unconsume_approval(confirmation_id, session_id, tenant_id)
        _shadow_equal("approval consumption release", legacy, postgres)
        return legacy
    return _legacy_unconsume_approval(confirmation_id, session_id, tenant_id)
