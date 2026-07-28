"""Public demand-capture endpoint with durable, idempotent, fail-closed storage."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Optional, Union

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError


router = APIRouter()

SERVER_DIR = Path(__file__).resolve().parent.parent
_LOCK = threading.Lock()
_CONN: Optional[sqlite3.Connection] = None

# Requests are tiny JSON; anything bigger is abuse. Enforced BEFORE parsing so
# an unauthenticated client cannot buffer arbitrarily large bodies (the
# byte-counting middleware in guest_uploads.py covers only the upload route).
_MAX_BODY_BYTES = 16_384

_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
# RFC 5321 unquoted local part: dot-separated atoms of atext only. This is
# what rejects `a,b@`, `a:b@`, `a(b)@`, `a<b>@`, `a[b]@`, `a\b@`, `a"b@` --
# printable ASCII, but not valid unquoted mailbox syntax.
_LOCAL_PART = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$"
)
_DOMAIN_LABEL = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$")
_TLD = re.compile(r"^[A-Za-z]{2,}$")

# The only mount the deployed task definitions back with durable storage.
# See docker-compose.yml JOBS_DB and the /data volume contract.
_DURABLE_ROOT = PurePosixPath("/data")

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

_GLOBAL_KEY = "__global__"


class DemandCaptureInput(BaseModel):
    email: str
    interest: str = ""
    org: Optional[str] = None


def _configured_db_path() -> Optional[Path]:
    """Durability contract: in a deployed environment the store location MUST
    be explicit (DEMAND_DB pointing at a mounted volume, e.g.
    /data/state/demand.db). Falling back to a container-local file there would
    silently lose every capture on task replacement, so we fail closed
    instead. Local dev and tests keep the JOBS_DB-style repo default."""
    configured = os.environ.get("DEMAND_DB", "").strip()
    runtime = os.environ.get("LEAF_RUNTIME_ENV", "").strip().lower()
    deployed = runtime in ("staging", "production")
    if configured:
        if deployed and not _durable_deployed_path(configured):
            # A nonempty but non-durable value (relative path, /tmp, a typo
            # like "demand.db") must fail closed too: returning 200 while
            # writing task-local storage is the exact silent-loss failure
            # this guard exists to prevent.
            return None
        return Path(configured)
    if deployed:
        return None
    return SERVER_DIR / "demand.db"


def _durable_deployed_path(configured: str) -> bool:
    """True only for an absolute POSIX path under the /data durable mount,
    with no `..` segments (PurePosixPath does not resolve them, so
    `/data/../tmp/x` would otherwise pass the parent check while the OS
    writes outside the mount)."""
    path = PurePosixPath(configured)
    if not path.is_absolute() or ".." in path.parts:
        return False
    return _DURABLE_ROOT in path.parents


def _db(db_path: Path) -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _CONN = sqlite3.connect(str(db_path), check_same_thread=False)
        _CONN.execute("PRAGMA busy_timeout = 5000")
        # DELETE, not WAL: the deployed path lives on EFS, where WAL is
        # explicitly unsafe (see the JOBS_DB note in docker-compose.yml).
        # Demand traffic is tiny; rollback-journal durability is fine.
        _CONN.execute("PRAGMA journal_mode = DELETE")
        _CONN.executescript(_SCHEMA)
        _CONN.commit()
    return _CONN


def _client_ip(request: Request) -> str:
    """Rate-limit key. Behind the trusted proxy (LEAF_TRUST_FORWARDED_FOR=1)
    we take the RIGHTMOST X-Forwarded-For entry: the ALB APPENDS the address
    it observed, so the last entry is proxy-attested while earlier entries are
    client-supplied and forgeable. (uploads.py:_client_ip still takes the
    first entry; flagged separately rather than changed here.) Without the
    flag, the socket peer is the truth."""
    if os.environ.get("LEAF_TRUST_FORWARDED_FOR") == "1":
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded.strip():
            return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _cap(env_name: str, default: int) -> int:
    try:
        cap = int(os.environ.get(env_name, str(default)))
    except ValueError:
        return default
    return cap if cap > 0 else default


def _per_ip_daily_cap() -> int:
    return _cap("LEAF_DEMAND_PER_IP_PER_DAY", 10)


def _global_daily_cap() -> int:
    return _cap("LEAF_DEMAND_GLOBAL_PER_DAY", 500)


def _day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _ip_digest(client_ip: str) -> str:
    return hashlib.sha256(str(client_ip or "unknown").encode("utf-8")).hexdigest()


def _valid_email(value: str) -> bool:
    """Fail-closed shape check: printable ASCII only, sane local part, and
    hostname-shaped domain labels. Rejects NUL bytes, leading/trailing/double
    dots, and hyphen-edged or empty labels that the loose regex alone accepts."""
    if len(value) > 254 or not _EMAIL.fullmatch(value):
        return False
    if any(not (33 <= ord(ch) <= 126) for ch in value):
        return False
    local, _, domain = value.rpartition("@")
    if not 1 <= len(local) <= 64:
        return False
    if not _LOCAL_PART.fullmatch(local):
        return False
    labels = domain.split(".")
    if len(labels) < 2 or any(len(label) > 63 for label in labels):
        return False
    if not all(_DOMAIN_LABEL.fullmatch(label) for label in labels):
        return False
    return bool(_TLD.fullmatch(labels[-1]))


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


async def _read_bounded_body(request: Request) -> Optional[bytes]:
    """Stream the body with a hard byte cap; None means too large. The declared
    Content-Length is checked first so the obvious case never buffers at all."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > _MAX_BODY_BYTES:
                return None
        except ValueError:
            return None
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > _MAX_BODY_BYTES:
            return None
    return bytes(received)


def _store_capture(
    db_path: Path, email: str, interest: str, org: Optional[str], client_ip: str
) -> Union[dict, JSONResponse]:
    """The whole blocking section: sqlite transaction under the module lock.
    Runs in the threadpool (run_in_threadpool below), never on the event
    loop: `BEGIN IMMEDIATE`, the 5s busy timeout, and the threading.Lock can
    all stall on a contended database, and none of them may block health
    checks or unrelated requests while they do."""
    try:
        with _LOCK:
            conn = _db(db_path)
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT email FROM demand_captures WHERE email = ?", (email,)
            ).fetchone()
            if existing is not None:
                conn.commit()
                return {"ok": True, "stored": False, "duplicate": True}

            day = _day()
            counters = (
                (_ip_digest(client_ip), _per_ip_daily_cap()),
                (_GLOBAL_KEY, _global_daily_cap()),
            )
            for key, cap in counters:
                row = conn.execute(
                    "SELECT count FROM demand_rate_limits WHERE day = ? AND ip_digest = ?",
                    (day, key),
                ).fetchone()
                if (int(row[0]) if row is not None else 0) >= cap:
                    conn.rollback()
                    return _error(429, "Please try again tomorrow.", retryable=True)
            for key, _cap_unused in counters:
                conn.execute(
                    "INSERT INTO demand_rate_limits (day, ip_digest, count) VALUES (?, ?, 1) "
                    "ON CONFLICT(day, ip_digest) DO UPDATE SET count = count + 1",
                    (day, key),
                )
            conn.execute(
                "INSERT INTO demand_captures (email, interest, org, created_at) VALUES (?, ?, ?, ?)",
                (email, interest, org, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
    except (sqlite3.DatabaseError, OSError):
        with _LOCK:
            if _CONN is not None and _CONN.in_transaction:
                _CONN.rollback()
        return _error(503, "We could not save your request. Please try again.", retryable=True)
    return {"ok": True, "stored": True, "duplicate": False}


@router.post("/api/demand")
async def capture_demand(request: Request) -> Any:
    """Store one public interest record, without charging an existing email twice."""
    body = await _read_bounded_body(request)
    if body is None:
        return _error(413, "Request is too large.")
    try:
        payload = DemandCaptureInput(**json.loads(body.decode("utf-8")))
    except (ValueError, TypeError, ValidationError, RecursionError):
        # RecursionError: a bounded body can still be thousands of nested JSON
        # arrays; the parser blowing the stack is malformed input (422), not a
        # server fault (500).
        return _error(422, "Send a JSON body with an email address.")

    try:
        email, interest, org = _clean_input(payload)
    except ValueError as exc:
        return _error(422, str(exc))

    db_path = _configured_db_path()
    if db_path is None:
        return _error(503, "We could not save your request. Please try again.", retryable=True)

    client_ip = _client_ip(request)
    return await run_in_threadpool(_store_capture, db_path, email, interest, org, client_ip)


def _reset_for_tests() -> None:
    """Close the import-time connection so a test can replace DEMAND_DB safely."""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            _CONN.close()
        _CONN = None
