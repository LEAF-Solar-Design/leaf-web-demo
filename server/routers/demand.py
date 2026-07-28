"""Public demand-capture endpoint with durable, idempotent storage."""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel


router = APIRouter()

SERVER_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("DEMAND_DB", str(SERVER_DIR / "demand.db")))
_LOCK = threading.Lock()
_CONN: Optional[sqlite3.Connection] = None
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS demand_captures (
  email TEXT PRIMARY KEY,
  interest TEXT NOT NULL,
  org TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS demand_rate_limits (
  day TEXT NOT NULL,
  ip_digest TEXT NOT NULL,
  count INTEGER NOT NULL,
  PRIMARY KEY (day, ip_digest)
);
"""


class DemandCaptureInput(BaseModel):
    email: str
    interest: str = ""
    org: Optional[str] = None


def _db() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CONN = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _CONN.execute("PRAGMA busy_timeout = 5000")
        _CONN.execute("PRAGMA journal_mode = WAL")
        _CONN.executescript(_SCHEMA)
        _CONN.commit()
    return _CONN


def _client_ip(request: Request) -> str:
    """Mirror the guest-upload trust boundary for forwarded client addresses."""
    if os.environ.get("LEAF_TRUST_FORWARDED_FOR") == "1":
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded.strip():
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _per_ip_daily_cap() -> int:
    try:
        cap = int(os.environ.get("LEAF_DEMAND_PER_IP_PER_DAY", "10"))
    except ValueError:
        return 10
    return cap if cap > 0 else 10


def _day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _ip_digest(client_ip: str) -> str:
    return hashlib.sha256(str(client_ip or "unknown").encode("utf-8")).hexdigest()


def _valid_email(value: str) -> bool:
    return bool(_EMAIL.fullmatch(value)) and len(value) <= 254


def _clean_input(payload: DemandCaptureInput) -> tuple[str, str, Optional[str]]:
    email = payload.email.strip().lower()
    interest = payload.interest.strip()
    org = payload.org.strip() if isinstance(payload.org, str) else None
    if not _valid_email(email):
        raise ValueError("Enter a valid email address.")
    if len(interest) > 1200:
        raise ValueError("Interest must be 1200 characters or fewer.")
    if org is not None and len(org) > 200:
        raise ValueError("Organization must be 200 characters or fewer.")
    return email, interest, org or None


def _error(status_code: int, message: str, *, retryable: bool = False) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"ok": False, "error": {"message": message, "retryable": retryable}},
    )


@router.post("/api/demand")
def capture_demand(payload: DemandCaptureInput, request: Request) -> Any:
    """Store one public interest record, without charging an existing email twice."""
    try:
        email, interest, org = _clean_input(payload)
    except ValueError as exc:
        return _error(422, str(exc))

    try:
        with _LOCK:
            conn = _db()
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT email FROM demand_captures WHERE email = ?", (email,)
            ).fetchone()
            if existing is not None:
                conn.commit()
                return {"ok": True, "stored": False, "duplicate": True}

            day = _day()
            ip_digest = _ip_digest(_client_ip(request))
            row = conn.execute(
                "SELECT count FROM demand_rate_limits WHERE day = ? AND ip_digest = ?",
                (day, ip_digest),
            ).fetchone()
            used = int(row[0]) if row is not None else 0
            if used >= _per_ip_daily_cap():
                conn.rollback()
                return _error(429, "Please try again tomorrow.", retryable=True)

            if row is None:
                conn.execute(
                    "INSERT INTO demand_rate_limits (day, ip_digest, count) VALUES (?, ?, 1)",
                    (day, ip_digest),
                )
            else:
                conn.execute(
                    "UPDATE demand_rate_limits SET count = count + 1 WHERE day = ? AND ip_digest = ?",
                    (day, ip_digest),
                )
            conn.execute(
                "INSERT INTO demand_captures (email, interest, org, created_at) VALUES (?, ?, ?, ?)",
                (email, interest, org, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
    except sqlite3.DatabaseError:
        with _LOCK:
            if _CONN is not None and _CONN.in_transaction:
                _CONN.rollback()
        return _error(503, "We could not save your request. Please try again.", retryable=True)
    return {"ok": True, "stored": True, "duplicate": False}


def _reset_for_tests() -> None:
    """Close the import-time connection so a test can replace DEMAND_DB safely."""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            _CONN.close()
        _CONN = None
