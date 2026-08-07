"""Durable metadata snapshots for session checkpoints.

The SQLite store deliberately shares the sessions database path while keeping
its own connection and lock. Checkpoint metadata is independent of the turn
engine and never changes a drawing or transcript.

Since 0029 the table also has a PostgreSQL home, selected by
``LEAF_SESSION_ANNEX_STORE`` rather than by ``LEAF_SESSIONS_STORE``. The reasons
for a separate selector, and the mode vocabulary, are in ``session_annex``.
Under the default ``legacy`` this module behaves exactly as it did before.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import session_annex

SERVER_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("SESSIONS_DB", str(SERVER_DIR / "sessions.db")))

CHECKPOINT_CAP = 50

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_checkpoints (
  checkpoint_id TEXT PRIMARY KEY,
  session_id TEXT,
  tenant_id TEXT,
  drawing_id TEXT,
  drawing_version TEXT,
  transcript_seq INTEGER,
  label TEXT,
  created_at REAL
);
"""


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


def _row_to_checkpoint(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "checkpoint_id": row["checkpoint_id"],
        "session_id": row["session_id"],
        "tenant_id": row["tenant_id"],
        "drawing_id": row["drawing_id"],
        "drawing_version": row["drawing_version"],
        "transcript_seq": row["transcript_seq"],
        "label": row["label"],
        "created_at": row["created_at"],
    }


def _legacy_create_checkpoint(
    session_id: str, tenant_id: str, drawing_id: str, drawing_version: Any,
    transcript_seq: int, label: Optional[str], *,
    checkpoint_id: Optional[str] = None, created_at: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    checkpoint_id = checkpoint_id or str(uuid.uuid4())
    created_at = time.time() if created_at is None else created_at
    with _lock:
        conn = _db()
        conn.row_factory = sqlite3.Row
        count = conn.execute(
            "SELECT COUNT(*) FROM session_checkpoints WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        if count >= CHECKPOINT_CAP:
            return None
        conn.execute(
            "INSERT INTO session_checkpoints"
            " (checkpoint_id, session_id, tenant_id, drawing_id, drawing_version,"
            " transcript_seq, label, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (checkpoint_id, session_id, tenant_id, drawing_id, str(drawing_version),
             int(transcript_seq), label, created_at),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM session_checkpoints WHERE checkpoint_id = ?",
            (checkpoint_id,),
        ).fetchone()
    return _row_to_checkpoint(row)


def _legacy_list_checkpoints(session_id: str, tenant_id: str) -> List[Dict[str, Any]]:
    with _lock:
        conn = _db()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM session_checkpoints"
            " WHERE session_id = ? AND tenant_id = ?"
            " ORDER BY created_at ASC, checkpoint_id ASC",
            (session_id, str(tenant_id)),
        ).fetchall()
    return [_row_to_checkpoint(row) for row in rows]


def _legacy_get_checkpoint(session_id: str, tenant_id: str,
                           checkpoint_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        conn = _db()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM session_checkpoints"
            " WHERE checkpoint_id = ? AND session_id = ? AND tenant_id = ?",
            (checkpoint_id, session_id, str(tenant_id)),
        ).fetchone()
    return _row_to_checkpoint(row) if row is not None else None


def _pg_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Project a PostgreSQL row into the SAME shape and types SQLite returns.

    ``transcript_seq`` is BIGINT and arrives as int, matching SQLite's INTEGER;
    ``drawing_version`` is TEXT on both sides because the legacy writer casts
    with ``str()``. Keeping the projection identical is what lets shadow mode
    compare whole dicts instead of a hand-listed subset of fields.
    """
    return {
        "checkpoint_id": row["checkpoint_id"],
        "session_id": row["session_id"],
        "tenant_id": row["tenant_id"],
        "drawing_id": row["drawing_id"],
        "drawing_version": row["drawing_version"],
        "transcript_seq": row["transcript_seq"],
        "label": row["label"],
        "created_at": row["created_at"],
    }


def _pg_create_checkpoint(
    session_id: str, tenant_id: str, drawing_id: str, drawing_version: Any,
    transcript_seq: int, label: Optional[str], *,
    checkpoint_id: Optional[str] = None, created_at: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Count and insert inside ONE transaction.

    The legacy path reads the count and inserts under a process-local
    ``threading.Lock``, which bounds nothing across tasks. Here the cap is
    enforced with a row-level lock the whole ECS fleet observes, so two
    concurrent creates on a session at 49 cannot both see 49 and both insert.
    """
    checkpoint_id = checkpoint_id or str(uuid.uuid4())
    created_at = time.time() if created_at is None else created_at
    db = session_annex.platform_db()
    with db.transaction() as conn:
        # A plain COUNT sees a snapshot, so two concurrent transactions can both
        # read 49. Locking the session's existing rows serializes them: the
        # second waits for the first to commit and then counts 50.
        conn.execute(
            f"SELECT checkpoint_id FROM {session_annex.PG_CHECKPOINTS_TABLE}"
            " WHERE session_id = %s FOR UPDATE",
            (session_id,),
        ).fetchall()
        count = conn.execute(
            f"SELECT COUNT(*) AS n FROM {session_annex.PG_CHECKPOINTS_TABLE}"
            " WHERE session_id = %s",
            (session_id,),
        ).fetchone()["n"]
        if count >= CHECKPOINT_CAP:
            return None
        conn.execute(
            f"INSERT INTO {session_annex.PG_CHECKPOINTS_TABLE}"
            " (checkpoint_id, session_id, tenant_id, drawing_id, drawing_version,"
            " transcript_seq, label, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (checkpoint_id, session_id, str(tenant_id), drawing_id,
             str(drawing_version), int(transcript_seq), label, created_at),
        )
        row = conn.execute(
            f"SELECT * FROM {session_annex.PG_CHECKPOINTS_TABLE}"
            " WHERE checkpoint_id = %s",
            (checkpoint_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("PostgreSQL checkpoint insert did not return a row")
    return _pg_row(row)


def _pg_list_checkpoints(session_id: str, tenant_id: str) -> List[Dict[str, Any]]:
    db = session_annex.platform_db()
    with db.cursor() as cur:
        cur.execute(
            f"SELECT * FROM {session_annex.PG_CHECKPOINTS_TABLE}"
            " WHERE session_id = %s AND tenant_id = %s"
            " ORDER BY created_at ASC, checkpoint_id ASC",
            (session_id, str(tenant_id)),
        )
        rows = cur.fetchall()
    return [_pg_row(row) for row in rows]


def _pg_get_checkpoint(session_id: str, tenant_id: str,
                       checkpoint_id: str) -> Optional[Dict[str, Any]]:
    db = session_annex.platform_db()
    with db.cursor() as cur:
        cur.execute(
            f"SELECT * FROM {session_annex.PG_CHECKPOINTS_TABLE}"
            " WHERE checkpoint_id = %s AND session_id = %s AND tenant_id = %s",
            (checkpoint_id, session_id, str(tenant_id)),
        )
        row = cur.fetchone()
    return _pg_row(row) if row is not None else None


def create_checkpoint(session_id: str, tenant_id: str, drawing_id: str,
                      drawing_version: Any, transcript_seq: int,
                      label: Optional[str]) -> Optional[Dict[str, Any]]:
    """Create one checkpoint, or return None when the session is at its cap."""
    mode = session_annex.store_mode()
    if mode == "postgres":
        return _pg_create_checkpoint(
            session_id, tenant_id, drawing_id, drawing_version,
            transcript_seq, label)
    if mode in session_annex.DUAL_WRITE_MODES:
        # Pre-flight, not a fail-closed write: it narrows the window in which a
        # committed legacy row can outrun its mirror. The two stores share no
        # atomic transaction, exactly as session_store records for its own dual
        # write, so a source-only row remains possible.
        session_annex.ensure_started()
    legacy = _legacy_create_checkpoint(
        session_id, tenant_id, drawing_id, drawing_version, transcript_seq, label)
    if legacy is None:
        # At the cap. Nothing was written, so there is nothing to mirror.
        return None
    if mode in session_annex.DUAL_WRITE_MODES:
        postgres = _pg_create_checkpoint(
            session_id, tenant_id, drawing_id, drawing_version,
            transcript_seq, label,
            checkpoint_id=legacy["checkpoint_id"], created_at=legacy["created_at"])
        if postgres is None:
            # The mirror hit its own cap while the legacy store did not, so the
            # two disagree about how full this session is. Fail loudly: a silent
            # return would leave a legacy row the mirror will never hold.
            raise RuntimeError("checkpoint mirror is at its cap while legacy is not")
        session_annex.shadow_equal(
            "checkpoint identity", legacy["checkpoint_id"], postgres["checkpoint_id"])
        if mode in session_annex.SHADOW_READ_MODES:
            session_annex.shadow_equal("checkpoint", legacy, postgres)
    # `shadow` deliberately does not compare here. It never wrote PostgreSQL, so
    # a post-write compare would test a target it chose not to update -- the
    # same rule session_store.append_event follows.
    return legacy


def list_checkpoints(session_id: str, tenant_id: str) -> List[Dict[str, Any]]:
    """Session AND tenant scoped. The router's ownership guard runs first, but
    the STORAGE boundary must hold on its own — a future caller that skips the
    guard, or a session id colliding across tenants, must still read nothing
    foreign (review round 1, the same defense-in-depth every session_store
    query keeps). Both backends carry the tenant predicate for that reason."""
    mode = session_annex.store_mode()
    if mode == "postgres":
        return _pg_list_checkpoints(session_id, tenant_id)
    legacy = _legacy_list_checkpoints(session_id, tenant_id)
    if mode in session_annex.SHADOW_READ_MODES:
        session_annex.shadow_equal(
            "checkpoints", legacy, _pg_list_checkpoints(session_id, tenant_id))
    return legacy


def get_checkpoint(session_id: str, tenant_id: str,
                   checkpoint_id: str) -> Optional[Dict[str, Any]]:
    """Return one checkpoint only when its session and tenant both match.

    The tenant predicate is deliberately part of the storage query, matching
    ``list_checkpoints``. Route ownership checks are useful, but callers that
    reach this function directly must not be able to resolve foreign metadata.
    """
    mode = session_annex.store_mode()
    if mode == "postgres":
        return _pg_get_checkpoint(session_id, tenant_id, checkpoint_id)
    legacy = _legacy_get_checkpoint(session_id, tenant_id, checkpoint_id)
    if mode in session_annex.SHADOW_READ_MODES:
        session_annex.shadow_equal(
            "checkpoint", legacy,
            _pg_get_checkpoint(session_id, tenant_id, checkpoint_id))
    return legacy
