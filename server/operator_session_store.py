"""PG-only operator session store (contract/OPERATOR.md section 2).

No SQLite or file fallback exists in this module by design: PostgreSQL
unavailability must surface as an error the router maps to 503 on every
write surface. Session identity is UNIQUE(subject, profile, environment);
events carry a per-session monotonic seq allocated inside the insert
transaction.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from psycopg.types.json import Jsonb

from operator_principals import _db


def create_or_get_session(subject: str, profile: str,
                          environment: str) -> Dict[str, Any]:
    session_id = f"opsess-{uuid.uuid4()}"
    db = _db()
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO operator_sessions
              (session_id, subject, profile, environment)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (subject, profile, environment) DO UPDATE
              SET updated_at = now()
            RETURNING session_id, subject, profile, environment, status,
                      created_at
            """,
            (session_id, subject, profile, environment))
        row = cur.fetchone()
        conn.commit()
    return {"session_id": row["session_id"], "subject": row["subject"],
            "profile": row["profile"], "environment": row["environment"],
            "status": row["status"], "created_at": str(row["created_at"])}


def get_session(session_id: str, subject: str) -> Optional[Dict[str, Any]]:
    """Own-subject only: another principal's session reads as absent."""
    db = _db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT session_id, subject, profile, environment, status,"
            " active_turn_id, created_at FROM operator_sessions"
            " WHERE session_id = %s AND subject = %s",
            (session_id, subject))
        row = cur.fetchone()
    if row is None:
        return None
    return {"session_id": row["session_id"], "subject": row["subject"],
            "profile": row["profile"], "environment": row["environment"],
            "status": row["status"], "active_turn_id": row["active_turn_id"],
            "created_at": str(row["created_at"])}


def list_sessions(subject: str) -> List[Dict[str, Any]]:
    db = _db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT session_id, profile, environment, status, created_at"
            " FROM operator_sessions WHERE subject = %s ORDER BY created_at",
            (subject,))
        return [{"session_id": r["session_id"], "profile": r["profile"],
                 "environment": r["environment"], "status": r["status"],
                 "created_at": str(r["created_at"])}
                for r in cur.fetchall()]


def begin_turn(session_id: str, subject: str, role_revision: int,
               profiles: tuple, environment: str) -> Optional[str]:
    """Take the turn lock and snapshot the authority tuple on the row.

    Returns the new turn id, or None when a turn is already active
    (reject-not-queue, the tenant idiom).
    """
    turn_id = f"opturn-{uuid.uuid4()}"
    db = _db()
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE operator_sessions SET
              active_turn_id = %s, turn_started_at = now(),
              turn_subject = %s, turn_role_revision = %s,
              turn_profiles = %s, turn_environment = %s,
              status = 'active', updated_at = now()
            WHERE session_id = %s AND subject = %s
              AND active_turn_id IS NULL
            """,
            (turn_id, subject, role_revision, list(profiles), environment,
             session_id, subject))
        updated = cur.rowcount
        conn.commit()
    return turn_id if updated == 1 else None


def end_turn(session_id: str, turn_id: str) -> None:
    db = _db()
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE operator_sessions SET active_turn_id = NULL,"
            " turn_started_at = NULL, status = 'idle', updated_at = now()"
            " WHERE session_id = %s AND active_turn_id = %s",
            (session_id, turn_id))
        conn.commit()


def append_event(session_id: str, turn_id: Optional[str], event_type: str,
                 data: Dict[str, Any]) -> int:
    db = _db()
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO operator_events (session_id, seq, turn_id, type, data)
            SELECT %s, COALESCE(MAX(seq), 0) + 1, %s, %s, %s
              FROM operator_events WHERE session_id = %s
            RETURNING seq
            """,
            (session_id, turn_id, event_type, Jsonb(data), session_id))
        seq = cur.fetchone()["seq"]
        conn.commit()
    return int(seq)


def read_events(session_id: str, subject: str,
                after_seq: int = 0, limit: int = 200) -> List[Dict[str, Any]]:
    if get_session(session_id, subject) is None:
        return []
    db = _db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT seq, turn_id, type, data, created_at FROM operator_events"
            " WHERE session_id = %s AND seq > %s ORDER BY seq LIMIT %s",
            (session_id, after_seq, limit))
        return [{"seq": r["seq"], "turn_id": r["turn_id"],
                 "type": r["type"], "data": r["data"],
                 "created_at": str(r["created_at"])} for r in cur.fetchall()]
