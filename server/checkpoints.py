"""Durable metadata snapshots for session checkpoints.

This store deliberately shares the sessions SQLite database path while keeping
its own connection and lock. Checkpoint metadata is independent of the turn
engine and never changes a drawing or transcript.
"""
from __future__ import annotations

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


def create_checkpoint(session_id: str, tenant_id: str, drawing_id: str,
                      drawing_version: Any, transcript_seq: int,
                      label: Optional[str]) -> Optional[Dict[str, Any]]:
    """Create one checkpoint, or return None when the session is at its cap."""
    checkpoint_id = str(uuid.uuid4())
    created_at = time.time()
    with _lock:
        conn = _db()
        conn.row_factory = sqlite3.Row
        count = conn.execute(
            "SELECT COUNT(*) FROM session_checkpoints WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        if count >= 50:
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


def list_checkpoints(session_id: str, tenant_id: str) -> List[Dict[str, Any]]:
    """Session AND tenant scoped. The router's ownership guard runs first, but
    the STORAGE boundary must hold on its own — a future caller that skips the
    guard, or a session id colliding across tenants, must still read nothing
    foreign (review round 1, the same defense-in-depth every session_store
    query keeps)."""
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


def get_checkpoint(session_id: str, tenant_id: str,
                   checkpoint_id: str) -> Optional[Dict[str, Any]]:
    """Return one checkpoint only when its session and tenant both match.

    The tenant predicate is deliberately part of the storage query, matching
    ``list_checkpoints``. Route ownership checks are useful, but callers that
    reach this function directly must not be able to resolve foreign metadata.
    """
    with _lock:
        conn = _db()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM session_checkpoints"
            " WHERE checkpoint_id = ? AND session_id = ? AND tenant_id = ?",
            (checkpoint_id, session_id, str(tenant_id)),
        ).fetchone()
    return _row_to_checkpoint(row) if row is not None else None
