"""Signed Design Automation completion callbacks for the broker.

The broker exposes ``POST /da/callback`` for a public Design Automation
``onComplete`` URL. Set ``LEAF_CALLBACK_SECRET`` to provision the HMAC key,
``LEAF_CALLBACK_URL`` to that public URL, and ``LEAF_CALLBACK_PRIMARY=1`` to
ask the broker to use callback-primary completion. Secret provisioning and
deployment wiring are deliberately outside this module.

Callback-primary is opt-in. Polling remains the default, and the broker's
existing ``/broker/reap`` endpoint remains the orphan fallback in either mode.
This module does not make APS calls or mutate job state. It verifies and
consumes one signed callback event exactly once, leaving job completion to the
broker completion adapter.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from typing import Any, Dict, Optional


CALLBACK_SECRET_ENV = "LEAF_CALLBACK_SECRET"
CALLBACK_URL_ENV = "LEAF_CALLBACK_URL"
CALLBACK_PRIMARY_ENV = "LEAF_CALLBACK_PRIMARY"
CALLBACK_MAX_AGE_ENV = "LEAF_CALLBACK_MAX_AGE_S"
SIGNATURE_HEADER = "X-Leaf-Signature"
TIMESTAMP_HEADER = "X-Leaf-Timestamp"
NONCE_HEADER = "X-Leaf-Nonce"
DEFAULT_MAX_AGE_S = 300.0

_consumed: Dict[tuple[str, str], float] = {}
_consumed_lock = threading.Lock()


def _secret() -> Optional[bytes]:
    value = os.environ.get(CALLBACK_SECRET_ENV)
    return value.encode("utf-8") if value else None


def callback_url() -> Optional[str]:
    value = os.environ.get(CALLBACK_URL_ENV)
    return value.strip() if value and value.strip() else None


def callback_primary_enabled() -> bool:
    """True only when the full callback-primary configuration is present."""
    return (os.environ.get(CALLBACK_PRIMARY_ENV, "").strip().lower()
            in {"1", "true", "yes", "on"}) and bool(_secret()) and bool(callback_url())


def _max_age_s() -> float:
    try:
        configured = float(os.environ.get(CALLBACK_MAX_AGE_ENV, DEFAULT_MAX_AGE_S))
    except (TypeError, ValueError):
        return DEFAULT_MAX_AGE_S
    return configured if configured > 0 else DEFAULT_MAX_AGE_S


def sign_payload(body: bytes, secret: Optional[bytes] = None) -> str:
    """Return the wire value for ``X-Leaf-Signature`` over the raw body."""
    key = secret if secret is not None else _secret()
    if not key:
        raise RuntimeError(f"{CALLBACK_SECRET_ENV} is not configured")
    return "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, signature: Optional[str],
                     secret: Optional[bytes] = None) -> bool:
    """Verify a raw callback body in constant time, failing closed by default."""
    key = secret if secret is not None else _secret()
    if not key or not signature:
        return False
    expected = sign_payload(body, key)
    return hmac.compare_digest(expected, signature)


def _callback_job_id(payload: Dict[str, Any]) -> Optional[str]:
    value = payload.get("job_id") or payload.get("workitem_id") or payload.get("id")
    return value if isinstance(value, str) and value else None


def _purge_expired(now: float) -> None:
    for key, expires_at in list(_consumed.items()):
        if expires_at < now:
            del _consumed[key]


def consume_callback(body: bytes, signature: Optional[str], timestamp: Optional[str],
                     nonce: Optional[str], now: Optional[float] = None) -> Dict[str, Any]:
    """Verify and consume one completion event.

    A callback must carry a signed raw JSON body plus a timestamp and nonce.
    The tuple ``(job_id, nonce)`` is retained for the timestamp window, so a
    replay cannot trigger completion twice. Failed verification returns before
    touching the consumed-event store.
    """
    key = _secret()
    if not key:
        return {"ok": False, "reason": "not_configured"}
    if not verify_signature(body, signature, key):
        return {"ok": False, "reason": "bad_signature"}
    if not isinstance(nonce, str) or not nonce.strip():
        return {"ok": False, "reason": "bad_nonce"}
    try:
        sent_at = float(timestamp) if timestamp is not None else None
    except (TypeError, ValueError):
        sent_at = None
    current = time.time() if now is None else now
    if sent_at is None or abs(current - sent_at) > _max_age_s():
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

    replay_key = (job_id, nonce)
    with _consumed_lock:
        _purge_expired(current)
        if replay_key in _consumed:
            return {"ok": False, "reason": "replay"}
        _consumed[replay_key] = current + _max_age_s()
    return {"ok": True, "job_id": job_id, "callback": payload}
