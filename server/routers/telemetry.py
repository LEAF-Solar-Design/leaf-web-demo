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

# Identity and envelope fields are SERVER-STAMPED; a client may not smuggle
# them (or lookalikes) in as labels. Stripped silently before merge.
RESERVED_LABEL_KEYS = frozenset({
    "tenant_id", "tenant_kind", "user_id", "user_email", "session_id",
    "environment", "app_version", "schema_version", "timestamp",
    "event_type", "event_name", "client_ts", "ingest",
})
MAX_LABELS_PER_EVENT = 40
MAX_LABEL_KEY_LEN = 64
MAX_LABEL_VALUE_LEN = 512

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


def _bucket_allows(key: str, cost: float) -> bool:
    """Token bucket keyed by caller (pre-auth: IP; guest: tenant id). COST is
    the event count, so one request with 50 events spends 50 tokens, not 1.
    Behind the ALB the pre-auth key is the ASGI peer (uvicorn's
    --proxy-headers governs whether that is the client or the proxy); guests
    key on their verified tenant id, which no proxy topology can merge."""
    try:
        cost = max(1.0, float(cost))
        now = time.monotonic()
        with _bucket_lock:
            tokens, last = _buckets.get(key, (_BUCKET_BURST, now))
            tokens = min(_BUCKET_BURST, tokens + (now - last) * _BUCKET_PER_S)
            if tokens < cost:
                _buckets[key] = [tokens, now]
                return False
            _buckets[key] = [tokens - cost, now]
            # Unbounded dict guard. Evicting a bucket resets it to FULL burst,
            # so only buckets whose CURRENT tokens (with refill projected to
            # now) already round to full may be evicted: for those, eviction
            # is semantically a no-op. A fresh-but-spent bucket (tokens just
            # under burst) must survive (review #420 round-2 warn 2).
            if len(_buckets) > 10_000:
                for k, (t, l) in list(_buckets.items()):
                    if t + (now - l) * _BUCKET_PER_S >= _BUCKET_BURST:
                        _buckets.pop(k, None)
                if len(_buckets) > 10_000:
                    # Memory still wins, but shed the LEAST-restricted buckets
                    # first; exhausted ones go last, never wholesale.
                    by_tokens = sorted(
                        _buckets.items(),
                        key=lambda kv: kv[1][0] + (now - kv[1][1]) * _BUCKET_PER_S,
                        reverse=True,
                    )
                    for k, _v in by_tokens[: len(_buckets) - 10_000]:
                        _buckets.pop(k, None)
            return True
    except Exception:  # noqa: BLE001 - never let accounting break ingest
        return False


async def _bounded_body(request: Request) -> Optional[bytes]:
    """Read at most MAX_BODY_BYTES without ever buffering an unbounded public
    payload: reject on the Content-Length header when present, and abort a
    chunked stream the moment it crosses the cap."""
    try:
        declared = request.headers.get("content-length")
        if declared is not None and int(declared) > MAX_BODY_BYTES:
            return None
    except Exception:  # noqa: BLE001 - a garbage header is an oversize body
        return None
    chunks: List[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


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
        raw = await _bounded_body(request)
        if raw is None:
            return {"accepted": 0}
        try:
            import json as _json

            body = _json.loads(raw or b"{}")
        except Exception:  # noqa: BLE001
            return {"accepted": 0}
        if not isinstance(body, dict):
            return {"accepted": 0}
        events = body.get("events")
        if not isinstance(events, list):
            return {"accepted": 0}

        principal = _resolve_principal(
            request, x_tenant_id, authorization, x_dispatch_secret, x_guest_session)

        n_cost = min(len(events), MAX_EVENTS)
        if principal is not None:
            tenant_id = str(principal)
            tenant_kind = "guest" if tenant_id.startswith("guest-") else "account"
            user_id = getattr(principal, "subject", None)
            # Guests are verified but self-mintable; they ride the same bucket
            # keyed by their tenant id. Accounts are not bucketed.
            if tenant_kind == "guest" and not _bucket_allows(f"g:{tenant_id}", n_cost):
                return {"accepted": 0}
        else:
            client_host = getattr(getattr(request, "client", None), "host", None) or "unknown"
            if not _bucket_allows(f"ip:{client_host}", n_cost):
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
            # Reserved keys are server-stamped envelope/identity fields: a
            # client may not smuggle them (or oversize junk) in as labels.
            merged: Dict[str, Any] = {}
            for k, v in list(labels.items())[: MAX_LABELS_PER_EVENT]:
                key = str(k)[:MAX_LABEL_KEY_LEN]
                if key in RESERVED_LABEL_KEYS:
                    continue
                # EVERY client value becomes a bounded string AT THE DOOR:
                # objects/arrays are serialized first so the cap applies to
                # the wire size, not just to values that arrived as strings
                # (review #420 round-2 blocker 1).
                if v is None:
                    continue
                try:
                    sval = v if isinstance(v, str) else _json.dumps(v, default=str) \
                        if isinstance(v, (dict, list)) else str(v)
                except Exception:  # noqa: BLE001 - skip the one bad value
                    continue
                merged[key] = sval[:MAX_LABEL_VALUE_LEN]
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
