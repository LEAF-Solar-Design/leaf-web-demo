"""POST /api/telemetry — the product-event ingest door (P2 events layer).

Design of record: docs/PLATFORM_TELEMETRY.md. Decisions that live here:

  - AUTH IS OPTIONAL at the route level, resolved manually (the uploads.py
    idiom): an authenticated principal (Auth0 bearer, guest HMAC, mock
    tenant) gets its events accepted with SERVER-STAMPED identity; an
    anonymous caller may send exactly the pre-auth allowlist trio behind a
    per-IP token bucket. Identity fields in the payload are IGNORED and
    overwritten from the verified principal (the telemetry-proxy
    verified-identity lesson).
  - The client can NEVER observe a telemetry failure: every outcome is
    `202 {accepted: n}` — auth-less off-allowlist events, validation drops,
    oversize bodies, sink overflow, disabled sink. Never a 4xx/5xx.
  - Caps: 50 events per body, 32 KB per body, label values stringified.
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, Request

import deps
import telemetry_sink

router = APIRouter()

MAX_EVENTS = 50
MAX_BODY_BYTES = 32 * 1024
CLIENT_TS_CLAMP_S = 24 * 3600.0

_NAME_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")

# Pre-auth allowlist: exactly the three top-of-funnel events that fire before
# any identity exists. Everything else requires a principal.
PREAUTH_EVENTS = frozenset({"gate.choice", "site.demo_viewed", "tour.started"})

# Per-IP token bucket for the pre-auth lane (the guest-quota pattern, in
# miniature): burst 30, refill 30 per minute, in-process only. Telemetry is
# loss-tolerant; a bucket miss silently drops.
_BUCKET_BURST = 30.0
_BUCKET_PER_S = 0.5
_buckets: Dict[str, List[float]] = {}
_bucket_lock = threading.Lock()


def _preauth_bucket_allows(ip: str) -> bool:
    try:
        now = time.monotonic()
        with _bucket_lock:
            tokens, last = _buckets.get(ip, (_BUCKET_BURST, now))
            tokens = min(_BUCKET_BURST, tokens + (now - last) * _BUCKET_PER_S)
            if tokens < 1.0:
                _buckets[ip] = [tokens, now]
                return False
            _buckets[ip] = [tokens - 1.0, now]
            # Unbounded dict guard: telemetry may be hit by scanners.
            if len(_buckets) > 10_000:
                _buckets.clear()
            return True
    except Exception:  # noqa: BLE001 - never let accounting break ingest
        return False


def _resolve_principal(request: Request, x_tenant_id, authorization,
                       x_dispatch_secret, x_guest_session):
    """The verified principal or None. 401/403 from the dependency mean
    'anonymous caller' here, never an error the client can observe."""
    try:
        return deps.require_tenant(
            request, x_tenant_id, authorization, x_dispatch_secret, x_guest_session)
    except Exception:  # noqa: BLE001 - HTTPException et al -> anonymous
        return None


def _clamped_client_ts(value: Any) -> Optional[str]:
    try:
        ts = float(value)
    except Exception:  # noqa: BLE001
        return None
    now = time.time()
    if abs(now - ts) > CLIENT_TS_CLAMP_S:
        ts = max(min(ts, now + CLIENT_TS_CLAMP_S), now - CLIENT_TS_CLAMP_S)
    return f"{ts:.3f}"


@router.post("/api/telemetry", status_code=202)
async def ingest(request: Request,
                 x_tenant_id: Optional[str] = Header(default=None),
                 authorization: Optional[str] = Header(default=None),
                 x_dispatch_secret: Optional[str] = Header(default=None),
                 x_guest_session: Optional[str] = Header(default=None)) -> Any:
    accepted = 0
    try:
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            return {"accepted": 0}
        try:
            import json as _json

            body = _json.loads(raw or b"{}")
        except Exception:  # noqa: BLE001
            return {"accepted": 0}
        events = body.get("events")
        if not isinstance(events, list):
            return {"accepted": 0}

        principal = _resolve_principal(
            request, x_tenant_id, authorization, x_dispatch_secret, x_guest_session)

        if principal is not None:
            tenant_id = str(principal)
            tenant_kind = "guest" if tenant_id.startswith("guest-") else "account"
            user_id = getattr(principal, "subject", None)
        else:
            client_host = getattr(getattr(request, "client", None), "host", None) or "unknown"
            if not _preauth_bucket_allows(str(client_host)):
                return {"accepted": 0}
            tenant_id = "anon"
            tenant_kind = "anon"
            user_id = None

        # A browser session id is a correlation label, not identity: accept it
        # from the body envelope, bounded.
        session_id = body.get("session_id")
        session_id = str(session_id)[:64] if isinstance(session_id, str) and session_id else "none"

        for ev in events[:MAX_EVENTS]:
            if not isinstance(ev, dict):
                continue
            name = ev.get("event_name")
            etype = ev.get("event_type", "custom_event")
            if not isinstance(name, str) or not _NAME_RE.match(name):
                continue
            if etype not in telemetry_sink.EVENT_TYPES:
                continue
            if principal is None and name not in PREAUTH_EVENTS:
                continue
            labels = ev.get("labels") if isinstance(ev.get("labels"), dict) else {}
            merged: Dict[str, Any] = dict(labels)
            client_ts = _clamped_client_ts(ev.get("client_ts"))
            if client_ts is not None:
                merged["client_ts"] = client_ts
            if user_id:
                merged["user_id"] = str(user_id)
            merged["ingest"] = "client"
            ok = telemetry_sink.emit(
                name,
                event_type=str(etype),
                tenant_id=tenant_id,
                tenant_kind=tenant_kind,
                session_id=session_id,
                labels=merged,
            )
            if ok:
                accepted += 1
        return {"accepted": accepted}
    except Exception:  # noqa: BLE001 - the client can never observe a failure
        return {"accepted": accepted}


def telemetry_enabled_state() -> Dict[str, Any]:
    """Small helper for ops surfaces: the sink's honest state."""
    reason = telemetry_sink.disabled_reason()
    return {"enabled": reason is None, "reason": reason,
            "kill_switch": os.environ.get("LEAF_TELEMETRY_DISABLED", "") == "1",
            "stats": telemetry_sink.stats()}
