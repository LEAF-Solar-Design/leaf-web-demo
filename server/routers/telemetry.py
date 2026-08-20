"""POST /api/telemetry — the product-event ingest door (P2 events layer).

Design of record: docs/PLATFORM_TELEMETRY.md. Decisions that live here:

  - AUTH IS OPTIONAL at the route level, resolved manually (the uploads.py
    idiom): an authenticated principal (Auth0 bearer, guest HMAC, mock
    tenant) gets its events accepted with SERVER-STAMPED identity; an
    anonymous caller may send exactly the pre-auth allowlist behind a
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

from fastapi import APIRouter, Header, HTTPException, Request

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

PLUGIN_SEVERITIES = frozenset({
    "DEBUG", "INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL", "ALERT",
    "EMERGENCY",
})
PLUGIN_FALLBACK_EVENT_NAMES = {
    "custom_event": "plugin.event",
    "error": "plugin.error",
    "exception": "plugin.exception",
    "page_view": "plugin.page_view",
}

# Pre-auth allowlist: the top-of-funnel events that fire before any identity
# exists, plus auth.completed, whose failure branch NEVER has a bearer (that
# invisible loss is the event's whole point) and whose success branch races a
# reload the beacon cannot authenticate. Everything else requires a principal.
#
# client.exception joins them for the same reason auth.completed did: a JS
# exception on a marketing or sign-in page happens BEFORE any principal
# exists, so requiring one drops exactly the failures nobody else can see (the
# server answers a healthy 200 for a page that is dead in the browser). The
# exposure is the same shape gate.choice already carries -- an anonymous
# caller can post these -- and it is bounded by the same per-IP token bucket
# below (burst 30, 0.5/s), the 50-events-per-body cap, the 512-char label cap,
# and a client-side per-session cap on the global handlers in
# web/src/telemetry.js (the ErrorBoundary path is uncapped: React bounds it).
PREAUTH_EVENTS = frozenset({
    "gate.choice", "site.demo_viewed", "tour.started", "auth.completed",
    "client.exception",
})

# `client.exception` promises STRUCTURAL labels (docs/PLATFORM_TELEMETRY.md),
# and the browser half keeps that promise. The door did not: an anonymous
# caller could POST the event with `message_class: "AliceSmith"`, a
# `message_hash` of "customerSecret", or an invented `raw_secret` key, and the
# row landed. The token bucket bounds VOLUME, not CONTENT, so the guarantee
# was only ever true of rows the real client sent.
#
# So this event's labels are validated against a schema, for EVERY caller and
# not just anonymous ones -- authentication proves identity, not label
# provenance. Unknown keys are dropped and every value must match its rule.
# The enumerated labels are CLOSED SETS rather than shapes (a shape accepted
# "alicesmith/desktop"), and the class list mirrors web/src/telemetry.js's
# KNOWN_CLASSES. An unlisted class degrades to "Other" rather than being
# dropped, so the two lists drifting costs a label's precision and never its
# safety; server/tests/test_client_exception_vocab_freeze.py enforces the
# mirror.
#
# A digest is ALWAYS 16 digits (the client zero-pads it), with one dated
# exception below for the one label an older client already emits. The door
# cannot prove a value came from the hash function -- an opaque field holds
# whatever the caller writes -- but requiring the exact width means the
# field's capacity is one digest and nothing else. The previous "any decimal"
# rule accepted "5550142", which is a phone number.
# `\Z`, not `$`: Python's `$` ALSO matches before a trailing newline, so
# sixteen zeros followed by a newline validated as a digest. One newline of
# capacity is not free text, but a rule that says "exactly 16 digits" should
# mean exactly that.
_HASH_RE = re.compile(r"\A[0-9]{16}\Z")

# ONE label gets a compatibility window, and it has an expiry.
#
# `component_stack_hash` is the only label a client OLDER than this release
# already emits: the pre-#537 ErrorBoundary hashed the component stack with
# its own 32-bit shift-hash and sent `String(hash >>> 0)`, a decimal of 1 to
# 10 digits. A browser tab opened before the deploy, or a cached bundle, keeps
# sending that width for as long as it lives. A 16-digit-only rule accepts
# those rows and STRIPS their only stack fingerprint -- a silent loss, on
# exactly the event that reports crashes, for the whole rollout.
#
# So both widths pass here and nowhere else. `message_hash` and `stack_hash`
# are new in this release, so no stale client can emit them and they keep the
# strict rule. The cost is real and bounded: 1 to 10 digits also fits a phone
# number, which is why the strict rule exists at all, and this concession
# applies to one label on one event for one rollout.
#
# REMOVE THE LEGACY BRANCH once no pre-#537 bundle can still be running -- one
# browser session's lifetime after the release is deployed everywhere is the
# honest bar. `test_component_stack_hash_accepts_both_client_versions` fails
# loudly when it goes, so removing it is a deliberate act rather than a drift.
_COMPONENT_STACK_HASH_RE = re.compile(r"\A(?:[0-9]{16}|[0-9]{1,10})\Z")
_CLIENT_EXCEPTION_CLASSES = frozenset({
    "Error", "EvalError", "RangeError", "ReferenceError", "SyntaxError",
    "TypeError", "URIError", "AggregateError", "DOMException",
    "FetchTimeoutError", "ChunkLoadError", "UnhandledRejection", "Other",
    "AbortError", "ConstraintError", "DataCloneError", "DataError",
    "EncodingError", "HierarchyRequestError", "IndexSizeError",
    "InvalidCharacterError", "InvalidStateError", "NamespaceError",
    "NetworkError", "NotAllowedError", "NotFoundError", "NotReadableError",
    "NotSupportedError", "OperationError", "QuotaExceededError",
    "ReadOnlyError", "SecurityError", "TimeoutError",
    "TransactionInactiveError", "UnknownError", "VersionError",
    "WrongDocumentError",
})
_CLIENT_EXCEPTION_SCHEMA: Dict[str, Any] = {
    "source": frozenset({"window.onerror", "unhandledrejection"}),
    "route": frozenset({"site", "tool", "sheets", "app", "unknown"}),
    "message_class": _CLIENT_EXCEPTION_CLASSES,
    "message_hash": _HASH_RE,
    "stack_hash": _HASH_RE,
    # CLOSED SET, not a shape. `^[a-z]+/(mobile|desktop)$` accepted
    # "alicesmith/desktop": the same shape-not-provenance failure as an
    # identifier-shaped class. These twelve are every value uaClass() emits.
    "ua_class": frozenset({
        f"{family}/{form}"
        for family in ("edge", "opera", "firefox", "chrome", "safari", "other")
        for form in ("mobile", "desktop")
    }) | {"unknown"},
    # BOTH client versions, deliberately and temporarily: see
    # _COMPONENT_STACK_HASH_RE. Every other digest here keeps the strict rule.
    "component_stack_hash": _COMPONENT_STACK_HASH_RE,
    # `tour_step` is deliberately ABSENT. Closing it needs a mirror of two
    # product-owned step tables that change with ordinary feature work, so it
    # would drift by design; a shape rule would accept "customer_secret". It
    # is marginal on an exception row, which already carries route and both
    # digests, so it is dropped rather than guessed at.
}
PREAUTH_LABEL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "client.exception": _CLIENT_EXCEPTION_SCHEMA,
}


def _schema_filtered(name: str, labels: Dict[str, Any]) -> Dict[str, Any]:
    """Client labels for an event that declares a schema. Unknown keys are
    dropped; a value that does not match its rule is dropped, EXCEPT a
    message_class outside the allowlist, which degrades to "Other" so the row
    keeps its meaning. Events with no schema are unchanged (the generic
    additive behaviour every other event keeps).

    Applied to EVERY caller, not only anonymous ones. Authentication proves
    identity, not label provenance: a bearer holder or a self-mintable guest
    can POST `message_hash: "customerSecret"` exactly as a stranger can, and
    this event's contract says its labels are structural. The cost is that a
    new label on THIS event needs a server release -- the right trade for the
    one event whose whole point is that its labels cannot carry free text."""
    schema = PREAUTH_LABEL_SCHEMAS.get(name)
    if schema is None:
        return labels
    out: Dict[str, Any] = {}
    for key, rule in schema.items():
        val = labels.get(key)
        if not isinstance(val, str):
            continue
        if isinstance(rule, frozenset):
            if val in rule:
                out[key] = val
            elif key == "message_class":
                out[key] = "Other"
        elif rule.match(val):
            out[key] = val
    return out

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
                # The DENY path inserts too, so it must sweep too, or a flood
                # of always-denied keys grows the dict without bound
                # (review #420 round-3 warn 1).
                _sweep_locked(now)
                return False
            _buckets[key] = [tokens - cost, now]
            _sweep_locked(now)
            return True
    except Exception:  # noqa: BLE001 - never let accounting break ingest
        return False


def _sweep_locked(now: float) -> None:
    """Bound the bucket dict; CALLER HOLDS _bucket_lock. Evicting a bucket
    resets it to FULL burst, so only buckets whose refill-projected tokens
    already round to full may be evicted (a semantic no-op for them); a
    fresh-but-spent bucket must survive. If memory still wins, shed the
    LEAST-restricted buckets first; exhausted ones go last, never wholesale."""
    if len(_buckets) <= 10_000:
        return
    for k, (t, l) in list(_buckets.items()):
        if t + (now - l) * _BUCKET_PER_S >= _BUCKET_BURST:
            _buckets.pop(k, None)
    if len(_buckets) > 10_000:
        by_tokens = sorted(
            _buckets.items(),
            key=lambda kv: kv[1][0] + (now - kv[1][1]) * _BUCKET_PER_S,
            reverse=True,
        )
        for k, _v in by_tokens[: len(_buckets) - 10_000]:
            _buckets.pop(k, None)


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


def _bounded_plugin_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        if isinstance(value, str):
            rendered = value
        elif isinstance(value, (dict, list)):
            import json as _json

            rendered = _json.dumps(value, default=str)
        else:
            rendered = str(value)
    except Exception:  # noqa: BLE001 - one bad label must not break the event
        return None
    return rendered[:MAX_LABEL_VALUE_LEN]


def _plugin_identity(request: Request, authorization: Optional[str]) -> Any:
    """Resolve only a live Auth0 bearer identity for the desktop collector.

    The general product-event door deliberately supports auth-off development,
    guest sessions, and a small anonymous allowlist. Branch2025 telemetry is a
    different trust boundary: its legacy GCP collector required Auth0, so this
    replacement must not silently widen access when LEAF_AUTH_LIVE is off.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing authorization")
    if not deps.auth_live():
        raise HTTPException(
            status_code=503,
            detail="Plugin telemetry requires live authentication",
        )
    principal = deps.require_tenant(request, None, authorization, None, None)
    subject = getattr(principal, "subject", None)
    if not isinstance(subject, str) or not subject.strip():
        raise HTTPException(status_code=403, detail="Authenticated subject required")
    return principal


def _normalized_plugin_labels(
    body: Dict[str, Any], principal: Any
) -> tuple[str, str, str, Dict[str, str]]:
    """Map the Branch2025 envelope to the promoted platform row contract."""
    message = body.get("message")
    severity = body.get("severity")
    source_labels = body.get("labels")
    client_timestamp = body.get("timestamp")
    if (
        not isinstance(message, str)
        or not isinstance(severity, str)
        or not isinstance(source_labels, dict)
        or not isinstance(client_timestamp, str)
    ):
        raise HTTPException(status_code=400, detail="Invalid plugin telemetry envelope")

    severity = severity.strip().upper()
    if severity not in PLUGIN_SEVERITIES:
        raise HTTPException(status_code=400, detail="Invalid telemetry severity")

    raw_type = source_labels.get("event_type", "custom_event")
    event_type = raw_type if raw_type in telemetry_sink.EVENT_TYPES else (
        "error" if severity in {"ERROR", "CRITICAL", "ALERT", "EMERGENCY"}
        else "custom_event"
    )
    raw_name = source_labels.get("event_name")
    event_name = (
        raw_name if isinstance(raw_name, str) and _NAME_RE.match(raw_name)
        else PLUGIN_FALLBACK_EVENT_NAMES[event_type]
    )
    raw_session = source_labels.get("session_id")
    session_id = (
        raw_session[:64]
        if isinstance(raw_session, str) and raw_session
        else "none"
    )

    subject = str(getattr(principal, "subject"))
    labels: Dict[str, str] = {
        "source": "branch2025",
        "message": message[:MAX_LABEL_VALUE_LEN],
        "severity": severity,
        "client_timestamp": client_timestamp[:MAX_LABEL_VALUE_LEN],
        "user_id": subject[:MAX_LABEL_VALUE_LEN],
        "ingest": "plugin",
    }
    # Leave two slots for telemetry_sink's server-owned schema_version and
    # event_id labels.
    output_cap = MAX_LABELS_PER_EVENT - 2
    for key_in, value in source_labels.items():
        if len(labels) >= output_cap:
            break
        key = str(key_in)[:MAX_LABEL_KEY_LEN]
        if not key or key in RESERVED_LABEL_KEYS or key in labels:
            continue
        rendered = _bounded_plugin_value(value)
        if rendered is not None:
            labels[key] = rendered
    return event_name, str(event_type), session_id, labels


@router.post("/api/telemetry/plugin", status_code=202)
async def ingest_plugin(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> Any:
    """Authenticated Branch2025 envelope collector backed by the shared sink."""
    principal = _plugin_identity(request, authorization)
    raw = await _bounded_body(request)
    if raw is None:
        raise HTTPException(status_code=413, detail="Telemetry body exceeds 32 KB")
    try:
        import json as _json

        body = _json.loads(raw or b"{}")
    except Exception as exc:  # noqa: BLE001 - report a bounded client error
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid plugin telemetry envelope")

    event_name, event_type, session_id, labels = _normalized_plugin_labels(
        body, principal
    )
    accepted = telemetry_sink.emit(
        event_name,
        event_type=event_type,
        tenant_id=str(principal),
        tenant_kind="account",
        session_id=session_id,
        labels=labels,
    )
    return {"accepted": 1 if accepted else 0}


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
            # Where the event declares a label schema, hold EVERY caller to
            # it before the generic bounding below, so an event promising
            # structural labels cannot be polluted by any direct POST.
            labels = _schema_filtered(name, labels)
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
