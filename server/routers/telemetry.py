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
    # The cross-emitter identity of ONE failure (web/src/telemetry.js
    # `dedupKeyFor`). New in this release, so it keeps the strict 16-digit
    # rule: no older bundle can emit it, and a variable-width value here would
    # be both a free-text hole and a merge key, which is the worst pairing on
    # the schema. A row whose key fails this rule simply loses the label and
    # is never merged with anything -- the safe direction.
    "dedup_key": _HASH_RE,
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

# --------------------------------------------------------------------------- #
# client.exception de-duplication
# --------------------------------------------------------------------------- #
# ONE React crash reaches TWO emitters. React 18's development build re-throws
# a render error through a synthetic DOM event, so the global window handler
# records it and then the ErrorBoundary records it again. Both rows are real
# and both describe the same failure; anything counting crashes counted it
# twice.
#
# THE CLIENT NO LONGER TRIES TO SOLVE THIS. Three rounds of browser-side
# timing de-duplication (emit-then-retract, then hold-pending-then-flush) each
# failed the same way: a batch flush landing between the two emits committed
# the first row before the second could suppress it, so the row count became a
# property of how busy the page was. Instead both emitters now attach the same
# `dedup_key` -- a digest of the failure's signature plus a coarse time bucket,
# derived independently from the same Error object -- and de-duplication moved
# here, where it is a property of the DATA and no ordering can break it.
#
# TOPOLOGY ASSUMED, AND WHY IT HOLDS:
#
# This door is an ECS service, NOT a singleton. `docs/aws-staging-deploy-
# receipt.json` records the deployment owner as "ECS CI/CD task-definition
# revisions; Terraform intentionally ignores task_definition and
# desired_count", so the instance count is not pinned by IaC and can be raised
# without a code change. Even at desired_count=1, every rolling deployment
# runs the outgoing and incoming tasks concurrently behind the same ALB, and
# nothing in this stack configures session affinity. The two POSTs carrying
# one crash's twin rows are separate HTTP requests, so they CAN be served by
# different processes.
#
# Therefore a per-process memo would de-duplicate on a quiet single-task day
# and SILENTLY stop de-duplicating during every deploy and every scale-out --
# the same class of "works until the environment changes" failure the client
# design just died of. It is not used here.
#
# There is also no shared store to reach for. The telemetry rail deliberately
# depends on nothing but BigQuery: `telemetry_sink` is a bounded in-memory
# queue with a guarded SDK import and a never-raise contract, and the platform
# Postgres is absent from both the broker image and the hermetic CI gate (see
# the `platform` suite note in scripts/run-all-gates.py). Putting a database
# round-trip on an anonymous, unauthenticated, loss-tolerant path to save a
# duplicate row would trade a real availability coupling for a cosmetic one.
#
# So de-duplication happens in the two places that need no coordination:
#
#   1. WITHIN A REQUEST BODY, here, deterministically. This is the common case
#      by a wide margin -- both emits normally ride the same 20-event batch --
#      and it needs no state at all, so it cannot drift between instances.
#   2. AT THE DURABLE LAYER, by handing BigQuery a stable streaming insertId of
#      `<session>:<dedup_key>` (see telemetry_sink.emit). BigQuery collapses
#      repeated insertIds inside its streaming window regardless of which task,
#      or which retry, sent them. That is documented as BEST EFFORT, which is
#      exactly why it is not the only layer.
#   3. AT READ TIME, by the rule in docs/PLATFORM_TELEMETRY.md: every consumer
#      of client.exception keeps one row per (session_id, dedup_key). That rule
#      is `_dedup_rank` below, expressed as SQL. It is the authoritative one:
#      it is a pure function of rows already stored, so it holds no matter how
#      many instances wrote them or how long apart.
#
# A row with no `dedup_key` is never merged with anything.


def _dedup_identity(name: str, session_id: str, labels: Dict[str, Any]) -> Optional[tuple]:
    """The (session, key) two rows must share to be the same failure, or None
    when this row is not de-duplicable at all."""
    if name != "client.exception":
        return None
    key = labels.get("dedup_key")
    if not isinstance(key, str) or not key:
        return None
    return (session_id, key)


def _dedup_rank(labels: Dict[str, Any]) -> int:
    """Which row SURVIVES when two share an identity. Lower wins.

    The boundary row wins, as it did under every previous design: it carries
    `component_stack_hash`, which the global handler cannot know, and it is the
    older contract. It is recognised by the ABSENCE of `source`, which is the
    one label the two emitters must differ on.

    The read-time rule in docs/PLATFORM_TELEMETRY.md is this function as SQL --
    `ORDER BY (source IS NOT NULL), timestamp` -- so the layers agree on WHICH
    row survives, not merely on how many."""
    return 1 if labels.get("source") else 0


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

        # Normalize first, emit second. The body is capped at MAX_EVENTS, so
        # holding the validated rows costs nothing, and it is what lets two
        # twin rows in ONE body collapse before either is queued.
        normalized: List[tuple] = []
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
            normalized.append((str(name), str(etype), merged))

        # Collapse twin rows that arrived together. `_dedup_rank` decides WHICH
        # survives, so the outcome does not depend on the order the browser
        # happened to queue them in; arrival order breaks a tie between equal
        # ranks. A merged-away row still counts as accepted: the door took the
        # event, it simply resolved to a row that is already there, and the
        # client must never read a drop into a successful merge.
        survivors: Dict[tuple, int] = {}
        keep: List[bool] = []
        merged_away = 0
        for idx, (name, _etype, merged) in enumerate(normalized):
            identity = _dedup_identity(name, session_id, merged)
            keep.append(True)
            if identity is None:
                continue
            prior = survivors.get(identity)
            if prior is None:
                survivors[identity] = idx
                continue
            merged_away += 1
            if _dedup_rank(merged) < _dedup_rank(normalized[prior][2]):
                keep[prior] = False
                survivors[identity] = idx
            else:
                keep[idx] = False

        for idx, (name, etype, merged) in enumerate(normalized):
            if not keep[idx]:
                continue
            identity = _dedup_identity(name, session_id, merged)
            ok = telemetry_sink.emit(
                name,
                event_type=etype,
                tenant_id=tenant_id,
                tenant_kind=tenant_kind,
                session_id=session_id,
                labels=merged,
                # The stable streaming id: the SAME string for a twin that
                # arrives in a later body, on a later retry, or through a
                # different task. None for everything else, which is every
                # event's existing behaviour.
                insert_id=f"{session_id}:{identity[1]}" if identity else None,
            )
            if ok:
                accepted += 1
        return {"accepted": accepted + merged_away}
    except Exception:  # noqa: BLE001 - the client can never observe a failure
        return {"accepted": accepted}


def telemetry_enabled_state() -> Dict[str, Any]:
    """Small helper for ops surfaces: the sink's honest state."""
    reason = telemetry_sink.disabled_reason()
    return {"enabled": reason is None, "reason": reason,
            "kill_switch": os.environ.get("LEAF_TELEMETRY_DISABLED", "") == "1",
            "stats": telemetry_sink.stats()}
