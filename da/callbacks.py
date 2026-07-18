"""da/callbacks.py — signed APS Design Automation `onComplete` callback receiver.

The PRODUCTION upgrade path for completion: instead of the broker polling every
WorkItem to a terminal state (the demo's mechanism), DA can POST to an
`onComplete` callback URL when a WorkItem finishes, making completion
EVENT-DRIVEN. A WorkItem is submitted with an argument like:

    "onComplete": {"verb": "post", "url": "https://<public-host>/da/callback"}

and DA POSTs the terminal status there. This module is the receiver skeleton +
an HMAC signature-verify seam.

DEMO LIMITATION (documented): the demo has NO public callback URL (localhost is
not reachable by APS), so the LEASE SWEEPER in da/reaper.py is the PRIMARY
orphan-handling mechanism here. This receiver is the drop-in for when the platform
runs behind a public host: point DA's onComplete at `/da/callback`, set
LEAF_CALLBACK_SECRET, and completion stops depending on polling.

Nothing here makes an APS call; it only VERIFIES + PARSES an inbound callback.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any, Dict, Optional

CALLBACK_SIG_HEADER = "X-Leaf-Signature"  # "sha256=<hexdigest>"


def _secret() -> Optional[bytes]:
    s = os.environ.get("LEAF_CALLBACK_SECRET")
    return s.encode("utf-8") if s else None


def sign_payload(body: bytes, secret: Optional[bytes] = None) -> str:
    """Compute the `sha256=<hex>` signature for a raw request body (also used by
    tests to synthesize a valid signature)."""
    key = secret if secret is not None else _secret()
    if not key:
        raise RuntimeError("LEAF_CALLBACK_SECRET not configured")
    return "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, signature: Optional[str],
                     secret: Optional[bytes] = None) -> bool:
    """Constant-time verify of a callback signature. If no secret is configured,
    verification is treated as disabled and returns True (demo/dev), which is
    logged by callers so it is never silently trusted in production."""
    key = secret if secret is not None else _secret()
    if not key:
        return True  # signature checking disabled (no secret) — dev/demo only
    if not signature:
        return False
    try:
        expected = sign_payload(body, key)
    except RuntimeError:
        return False
    return hmac.compare_digest(expected, signature)


def parse_callback(body: bytes) -> Dict[str, Any]:
    """Parse a DA onComplete body into a normalized {workitem_id, status, ...}."""
    data = json.loads(body.decode("utf-8"))
    return {
        "workitem_id": data.get("id") or data.get("workitemId"),
        "status": data.get("status"),
        "report_url": data.get("reportUrl"),
        "stats": data.get("stats"),
        "raw": data,
    }


def handle_callback(body: bytes, signature: Optional[str] = None,
                    secret: Optional[bytes] = None) -> Dict[str, Any]:
    """Verify + parse an inbound onComplete callback.

    Returns {"ok": True, "callback": {...}, "signature_verified": bool} on
    success, or {"ok": False, "error_code": "bad_signature"|"bad_body", ...} on
    failure. Signature-verified completion can then advance the durable job
    record WITHOUT polling. This is a skeleton: wiring it to the job spine (mark
    the matching WorkItem's job complete) belongs to the session that gives the
    platform a public callback host.
    """
    key = secret if secret is not None else _secret()
    verified = verify_signature(body, signature, key)
    if not verified:
        return {"ok": False, "error_code": "bad_signature",
                "message": "callback signature verification failed"}
    try:
        cb = parse_callback(body)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error_code": "bad_body", "message": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "callback": cb, "signature_verified": bool(key)}
