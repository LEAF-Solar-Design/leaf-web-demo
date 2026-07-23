"""Signed Design Automation completion callbacks for the broker.

The broker exposes ``POST /da/callback`` for a public Design Automation
``onComplete`` URL. Set ``LEAF_CALLBACK_SECRET`` to provision the HMAC key,
``LEAF_CALLBACK_URL`` to that public URL, and ``LEAF_CALLBACK_PRIMARY=1`` to
ask the broker to use callback-primary completion.

The signature covers the timestamp, nonce, and body. Consumed nonces live in
the durable jobs SQLite database for the timestamp window, so a process restart
or another broker worker cannot accept a captured callback again.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional


CALLBACK_SECRET_ENV = "LEAF_CALLBACK_SECRET"
CALLBACK_URL_ENV = "LEAF_CALLBACK_URL"
CALLBACK_PRIMARY_ENV = "LEAF_CALLBACK_PRIMARY"
CALLBACK_MAX_AGE_ENV = "LEAF_CALLBACK_MAX_AGE_S"
SIGNATURE_HEADER = "X-Leaf-Signature"
TIMESTAMP_HEADER = "X-Leaf-Timestamp"
NONCE_HEADER = "X-Leaf-Nonce"
DEFAULT_MAX_AGE_S = 300.0
SERVER_DIR = Path(__file__).resolve().parent.parent


class CallbackReplayStore:
    """Durably consume callback nonces in the same SQLite database as jobs."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or Path(os.environ.get("JOBS_DB", str(SERVER_DIR / "jobs.db")))

    def consume(self, job_id: str, nonce: str, expires_at: float, now: float) -> bool:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS callback_consumed_nonces ("
                "job_id TEXT NOT NULL, nonce TEXT NOT NULL, expires_at REAL NOT NULL, "
                "PRIMARY KEY (job_id, nonce))"
            )
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM callback_consumed_nonces WHERE expires_at < ?", (now,))
            try:
                conn.execute(
                    "INSERT INTO callback_consumed_nonces (job_id, nonce, expires_at) VALUES (?,?,?)",
                    (job_id, nonce, expires_at),
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                return False
            conn.commit()
            return True
        finally:
            conn.close()


def _secret() -> Optional[bytes]:
    value = os.environ.get(CALLBACK_SECRET_ENV)
    return value.encode("utf-8") if value else None


def callback_url() -> Optional[str]:
    value = os.environ.get(CALLBACK_URL_ENV)
    return value.strip() if value and value.strip() else None


def _primary_requested() -> bool:
    return os.environ.get(CALLBACK_PRIMARY_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def callback_primary_configuration_error() -> Optional[str]:
    """Return a loud operator error when callback-primary is only partly configured."""
    if not _primary_requested():
        return None
    missing = []
    if not _secret():
        missing.append(CALLBACK_SECRET_ENV)
    if not callback_url():
        missing.append(CALLBACK_URL_ENV)
    if missing:
        return "callback-primary requires " + ", ".join(missing)
    return None


def callback_primary_enabled() -> bool:
    return _primary_requested() and callback_primary_configuration_error() is None


def _max_age_s() -> float:
    try:
        configured = float(os.environ.get(CALLBACK_MAX_AGE_ENV, DEFAULT_MAX_AGE_S))
    except (TypeError, ValueError):
        return DEFAULT_MAX_AGE_S
    return configured if configured > 0 else DEFAULT_MAX_AGE_S


def _canonical_payload(body: bytes, timestamp: str, nonce: str) -> bytes:
    """Length-prefix every signed field so distinct fields cannot concatenate alike."""
    fields = (timestamp.encode("utf-8"), nonce.encode("utf-8"), body)
    return b"".join(str(len(field)).encode("ascii") + b":" + field for field in fields)


def sign_payload(body: bytes, timestamp: str, nonce: str,
                 secret: Optional[bytes] = None) -> str:
    """Return the HMAC wire value over the timestamp, nonce, and raw body."""
    key = secret if secret is not None else _secret()
    if not key:
        raise RuntimeError(f"{CALLBACK_SECRET_ENV} is not configured")
    canonical = _canonical_payload(body, timestamp, nonce)
    return "sha256=" + hmac.new(key, canonical, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, timestamp: Optional[str], nonce: Optional[str],
                     signature: Optional[str], secret: Optional[bytes] = None) -> bool:
    """Verify the complete callback request in constant time, failing closed."""
    key = secret if secret is not None else _secret()
    if not key or not signature or not isinstance(timestamp, str) or not isinstance(nonce, str):
        return False
    expected = sign_payload(body, timestamp, nonce, key)
    return hmac.compare_digest(expected, signature)


def _callback_job_id(payload: Dict[str, Any]) -> Optional[str]:
    value = payload.get("job_id") or payload.get("workitem_id") or payload.get("id")
    return value if isinstance(value, str) and value else None


def consume_callback(body: bytes, signature: Optional[str], timestamp: Optional[str],
                     nonce: Optional[str], now: Optional[float] = None,
                     replay_store: Optional[CallbackReplayStore] = None) -> Dict[str, Any]:
    """Verify and durably consume one completion event exactly once."""
    key = _secret()
    if not key:
        return {"ok": False, "reason": "not_configured"}
    if not verify_signature(body, timestamp, nonce, signature, key):
        return {"ok": False, "reason": "bad_signature"}
    if not isinstance(nonce, str) or not nonce.strip():
        return {"ok": False, "reason": "bad_nonce"}
    try:
        sent_at = float(timestamp) if timestamp is not None else None
    except (TypeError, ValueError):
        sent_at = None
    current = time.time() if now is None else now
    max_age = _max_age_s()
    if sent_at is None or not math.isfinite(sent_at) or abs(current - sent_at) > max_age:
        return {"ok": False, "reason": "stale_callback"}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"ok": False, "reason": "bad_body"}
    if not isinstance(payload, dict):
        return {"ok": False, "reason": "bad_body"}
    job_id = _callback_job_id(payload)
    if not job_id:
        return {"ok": False, "reason": "bad_body"}

    store = replay_store or CallbackReplayStore()
    if not store.consume(job_id, nonce, sent_at + max_age, current):
        return {"ok": False, "reason": "replay"}
    return {"ok": True, "job_id": job_id, "callback": payload}
