"""
Turn engine (sessions wire spec, gap audit §2.1 / CONTRACT-ADDENDUM sessions
addendum) — Turn-engine API v1 (FROZEN):

    class TurnBusy(Exception)
    class TurnRejected(Exception): status_code, error_code, message, extra,
                                   pre_harness (additive, defaults False)
    start_turn(tenant_id, session_id, *, text, confirm, classifier_hint) -> turn_id

This module is the ONLY place that POSTs to the harness's `POST /turn`
(harness/src/ports/converse.ts, ConverseRunner / ConverseTurnInput). It owns
the whole turn lifecycle: session guard, the `active_turn_id` compare-and-swap
(session_store.try_begin_turn), durably recording the user's message as the
`turn_started` event (the durable transcript source for `{text}` /
`{confirm}` turns), relaying every NDJSON line the harness streams back into
`session_events` verbatim, materializing an `approvals` row the moment a
`confirmation_required` event arrives, and guaranteeing the turn's
`active_turn_id` lock is ALWAYS released — on an immediate harness rejection,
on a clean `turn_complete`/`error`, on an unexpected stream failure, or (the
backstop) a `TURN_MAX_S` watchdog.

Semantics (frozen order):

    session exists (+ tenant match) guard
      -> try_begin_turn CAS                              (raise TurnBusy on loss)
      -> append_event(..., 'turn_started', {text|confirm, classifier_hint})
      -> build bounded prior-context `messages[]` from the event log
      -> POST {LEAF_CONVERSE_HARNESS_URL|LEAF_AUTHOR_HARNESS_URL}/turn,
         stream=True, timeout=(5, TURN_MAX_S)   (converse var preferred)
           immediate 401              -> TurnRejected(401, GRANT_REQUIRED, extra={grant_required:True})
           immediate 429              -> TurnRejected(429, llm_quota_exhausted|llm_rate_limited)
           immediate connection error -> TurnRejected(502, BROKER_UNREACHABLE)
           (all of the above release the CAS via end_turn before raising)
      -> else return turn_id immediately; a detached daemon thread drains the
         NDJSON body, append_event-ing each relayed line, until a
         `turn_complete`/`error` event or the stream ends (either releases the
         CAS via end_turn); a SEPARATE `TURN_MAX_S` watchdog thread is the
         backstop — if the drain hasn't finished by then it appends a synthetic
         `turn_complete{stop_reason:'timeout'}` and releases the CAS itself.

Exactly one terminal path ever fires per turn (drain's real relay, drain's
synthetic error-on-exception, drain's plain "stream ended" cleanup, or the
watchdog's synthetic timeout) — guarded by a lock + one-shot flag so a race
between the drain thread finishing and the watchdog firing can never double-
append a terminal event or double-call end_turn (end_turn itself is also
inherently idempotent — see session_store.end_turn).

This module never emits harness-owned event types itself beyond relaying the
harness's own NDJSON lines verbatim (`text_delta`, `tool_call`, `tool_result`,
`job_linked`, `proposed_run`, `confirmation_required`, `turn_usage`,
`turn_complete`, `error`) — its own additions are exactly `turn_started` (the
user-message record) and, only as a last-resort backstop, a synthetic
`error`/`turn_complete{stop_reason:'timeout'}`.
"""
from __future__ import annotations

import json
import base64
import os
import sqlite3
import sys
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

import requests

import agent_gate
import agent_ledger
import broker_client
import emf_metrics
import entitlements
import instant_execution
import session_policy
import session_store
from envelopes import ErrorCode

# --------------------------------------------------------------------------- #
# env knobs (read at call time, never at import time, so tests can override
# via monkeypatch/env without needing to re-import this module — same posture
# as jobs.py's job_max_s()/heartbeat_stale_s()).
# --------------------------------------------------------------------------- #
# The per-session "mount your LLM" allowlist (the Claude family the Agent SDK
# runner supports). Keep in lockstep with harness/src/ports/modelAllowlist.ts.
# A true multi-provider (OpenAI/Gemini) adapter is an explicit, separate follow-up.
ALLOWED_MODELS = frozenset({
    "claude-sonnet-5",
    "claude-opus-4-8",
    "claude-haiku-4-5",
    "claude-fable-5",
})


def is_allowed_model(model: Optional[str]) -> bool:
    """True iff `model` is one of the allowed Claude-family ids."""
    return isinstance(model, str) and model in ALLOWED_MODELS


# Keep in step with harness/src/redact.ts MIN_REDACTABLE_SECRET_LEN and with
# routers/sessions.py _MIN_CREDENTIAL_LEN.
_MIN_REDACTABLE_SECRET_LEN = 24


def _grant_secret(grant: Optional[Dict[str, Any]]) -> Optional[str]:
    """The token value carried by a validated credential grant, if it is long and
    opaque enough to strip by literal match: at least _MIN_REDACTABLE_SECRET_LEN
    characters, all PRINTABLE ASCII (0x21-0x7E). Anything shorter, or carrying a
    space, a control character, or a non-ASCII codepoint, is never eligible —
    replacing such a value would rewrite ordinary prose instead of a secret.
    Keep this rule identical to routers/sessions.py _CREDENTIAL_CHARS and
    harness/src/redact.ts PRINTABLE_ASCII; validation upstream enforces the
    same one."""
    if not isinstance(grant, dict):
        return None
    tok = grant.get("api_key") if grant.get("kind") == "api_key" else grant.get("oauth_token")
    if (isinstance(tok, str) and len(tok) >= _MIN_REDACTABLE_SECRET_LEN
            and all(0x21 <= ord(c) <= 0x7E for c in tok)):
        return tok
    return None


def _scrub_tree(node: Any, secret: str) -> Any:
    """Strip `secret` from every STRING VALUE in a nested structure.

    `classifier_hint` is an unrestricted caller-controlled dict that this module
    persists verbatim into the durable `turn_started` event, and it is NOT part
    of the harness wire — so nothing downstream can repair it. A caller can put
    the same value in `credential_grant.api_key` and `classifier_hint.rationale`
    and have the live grant land in the transcript. (sol-critic PR #123 round 7,
    blocker 1.)

    VALUES only, never keys. Rewriting keys is what made the harness-side attempt
    drop fields on collision; a credential used as a dict KEY here is not a
    realistic shape, and leaving keys alone keeps this transformation total."""
    if isinstance(node, str):
        return node.replace(secret, "[REDACTED]")
    if isinstance(node, list):
        return [_scrub_tree(v, secret) for v in node]
    if isinstance(node, dict):
        return {k: _scrub_tree(v, secret) for k, v in node.items()}
    return node


def _scrub_secret(value: Optional[str], secret: Optional[str]) -> Optional[str]:
    """Remove a bring-your-own credential the user pasted into their own prompt.

    THIS is the boundary that matters: `append_event` below writes the user's
    text into the durable transcript BEFORE the harness is ever called, so a
    harness-side scrub cannot keep the value out of app storage (sol-critic PR
    #123 round 6). Scrubbing here covers the app transcript AND the wire body,
    and because the harness's prior-turn `messages` are read back out of this
    same transcript, earlier turns stay clean too.

    Only the BYO grant is strippable here — the tenant's linked grant is resolved
    inside the harness and this process never sees it, which is why the harness
    keeps its own equivalent pass."""
    if not value or not secret:
        return value
    return value.replace(secret, "[REDACTED]")


def turn_max_s() -> float:
    return float(os.environ.get("TURN_MAX_S", "300"))


def approval_ttl_s() -> float:
    return float(os.environ.get("SESSIONS_APPROVAL_TTL_S", "600"))


def _harness_url() -> str:
    """§2.1 env contract (census #12 chip 2): prefer LEAF_CONVERSE_HARNESS_URL
    (scripts/start-leaf.py exports both vars to the app), fall back to
    LEAF_AUTHOR_HARNESS_URL so single-var deploys (docker-compose today) keep
    working. An empty-string value falls through to the fallback."""
    return (os.environ.get("LEAF_CONVERSE_HARNESS_URL")
            or os.environ.get("LEAF_AUTHOR_HARNESS_URL") or "").rstrip("/")


# bounded prior-context window (§2.1.2 "messages ... bounded, built by the
# turn engine") — kept simple: fold the last PRIOR_EVENTS_WINDOW raw events
# into per-turn {user, assistant} pairs, then keep only the most recent
# MAX_PRIOR_MESSAGES of those.
PRIOR_EVENTS_WINDOW = 400
MAX_PRIOR_MESSAGES = 20


# --------------------------------------------------------------------------- #
# exceptions (FROZEN shape — routers/agent.py, S4, catches these)
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# in-flight turn cancellation
# --------------------------------------------------------------------------- #
# turn_id -> a callable that terminalizes THIS process's relay for that turn.
# Registered by _spawn_relay, dropped when the relay finishes. A turn whose
# relay lives in another process (or whose process restarted) is simply absent
# here, and request_cancel() falls back to terminalizing the durable row —
# so a cancel ALWAYS releases the session's CAS, never only sometimes.
_cancellers_lock = threading.Lock()
_cancellers: Dict[str, Any] = {}

# Serializes the ORPHAN cancel path's check-then-act. Two cancels arriving at
# once would otherwise both read `active_turn_id == turn_id` and both append a
# terminal event. Held across the re-read + append + release so the pair is
# atomic within this process.
#
# Within THIS process is the whole story by design: the app is single-writer
# (docker-compose.yml SESSIONS_DB: "SINGLE-WRITER: SQLite+WAL+threading.Lock
# assumes ONE app process — keep one replica/one uvicorn worker", and
# deploy/Dockerfile.app runs uvicorn with no --workers). The relay's own state
# — terminal_flag, proposals, turn_usage — is already per-process for the same
# reason, so a second app process would break far more than cancellation.
_orphan_cancel_lock = threading.Lock()

# A non-turn holder of the active-turn slot. routers/checkpoints.py reserves the
# slot for a restore so a turn cannot start mid-commit; it is NOT a turn, so the
# cancel route must refuse it (cancelling one would append a FALSE turn_complete,
# release the reservation, and let a real turn run while the restore is still
# writing — PR #310 review round 2, blocker 1).
RESERVATION_PREFIX = "restore-"


def is_reservation(turn_id: str) -> bool:
    return str(turn_id).startswith(RESERVATION_PREFIX)


def _register_canceller(turn_id: str, fn: Any) -> None:
    with _cancellers_lock:
        _cancellers[turn_id] = fn


def _drop_canceller(turn_id: str) -> None:
    with _cancellers_lock:
        _cancellers.pop(turn_id, None)


# --------------------------------------------------------------------------- #
# busy-turn queue (cap 1)
# --------------------------------------------------------------------------- #
# session_id -> the ONE pending prompt (text turns only — a confirm's approval
# lifecycle and a credential grant's never-persist rule both forbid queueing).
# In-process by the same single-writer argument as `_cancellers` above: the app
# runs one process, and the relay machinery this queue rides on is per-process
# already. The durable record is the `turn_queued` transcript event appended at
# enqueue time; the START is in-process best-effort, exactly like the relay it
# hands the prompt to. A process restart drops the start (the transcript still
# shows `turn_queued`), which matches how a restart orphans active relays.
#
# The slot has TWO states, and that is load-bearing (review round 1, findings
# 1 and 3): `payload["starting"]` is False while parked and True while a kicker
# is attempting the start. The slot is NOT emptied during that attempt — a
# concurrent enqueue still sees the key and answers "full" — so a second
# prompt can never be accepted into the gap where the first one's start might
# fail (which is how a 202'd prompt could otherwise be stranded on a free
# session, or an accepted prompt silently superseded). The slot empties only
# on a stable outcome: started, rejected (transcript closed), or dropped.
_queued_lock = threading.Lock()
_queued: Dict[str, Dict[str, Any]] = {}

# The kicker's TurnBusy-retry bound. Each retry requires a DISTINCT foreign
# turn to have acquired AND terminalized the session's CAS inside the window
# between one pop-check and one start attempt, with that turn's own terminal
# kick ALSO having lost the interleaving — each extra lap is another full
# foreign turn lifecycle squeezed into microseconds. Exhausting this bound is
# not a realistic schedule; if it ever happens the prompt stays parked and a
# LOUD stderr line says so (the next enqueue-time kick is the backstop).
_KICK_MAX_ATTEMPTS = 64

# Capabilities eligible for policy auto-approval. EXACT membership against the
# vocabulary the HARNESS actually mints - `Capability = "drawing.read" |
# "drawing.write"` (harness/src/ports/index.ts). `drawing.write` and a MISSING
# capability are ineligible: fail closed, unknown means confirm.
#
# The first cut of this set said `run_read`, which is an ENTITLEMENT key
# (entitlements.py: run_read/run_write/build), NOT a proposal capability - so
# it matched nothing in production while its tests fabricated the value and
# passed (review round 1, blocker 1). Widening this set is a money-safety
# decision, never a convenience edit, and any new member must be quoted from
# the harness Capability union.
READONLY_AUTO_CAPABILITIES = frozenset({"drawing.read"})

# The decided_by stamp for a policy decision. It is also the RESUME KEY: a
# decision carrying this actor and still unconsumed is one THIS policy made and
# may finish, which is what makes a lost CAS retryable instead of a permanent
# strand (review round 1, finding 3).
POLICY_DECIDER = "policy:auto_approve_reads"

# session_id -> confirmation_id of a policy decision that was recorded but
# whose confirm turn could not start (a lost CAS). Every terminal retries it.
#
# Without this the "resumable decision" was unreachable code: each relay owns a
# FRESH `proposals` map populated only by its own turn's events, so a later
# terminal had no way to rediscover the stranded id and the row stayed decided
# -by-policy forever - a human could not re-decide it, and under live auth
# could not consume it either (review round 2, finding 1).
#
# In-process, by the same single-writer argument as `_cancellers` and
# `_queued`. A process restart drops the retry, exactly as it drops active
# relays and queued prompts; the durable row remains and an operator can see it.
_pending_policy_lock = threading.Lock()
_pending_policy: Dict[str, List[str]] = {}
# Per session. A LIST, not one slot: terminals overlap (the CAS is released
# before auto-confirm runs), so two of them can each park a different id, and a
# single slot let the second overwrite the first — the displaced row stayed
# policy-decided and unconsumed with nothing able to rediscover it (review
# round 3).
#
# The cap is enforced BEFORE a row is decided, never by evicting one after:
# an eviction would strand whichever end it dropped. At the cap the policy
# simply stops deciding and the chips stay manual.
MAX_PENDING_POLICY = 16


class TurnBusy(Exception):
    """try_begin_turn lost the CAS — a turn is already active for this session."""


class TurnRejected(Exception):
    """The harness (or this engine, pre-flight) rejected the turn before it
    could stream. `extra` is merged top-level into the router's error body
    (e.g. {'grant_required': True}).

    `pre_harness` says the turn was rejected PROVABLY BEFORE the harness could
    have seen it, so nothing downstream can have redeemed a confirm's approval
    and the router may safely give that approval back (routers/sessions.py's
    APPROVAL GIVE-BACK note). It DEFAULTS TO FALSE — the fail-safe answer — so
    any leg that does not opt in is treated as "the harness may have acted",
    and an approval whose tool call might already be running is never
    un-spent. Only set it True where the request demonstrably never left this
    process: no POST was attempted, or the TCP connection was never
    established (ConnectTimeout).

    A plain ``requests.ConnectionError`` does NOT qualify: requests folds
    urllib3's ProtocolError into it, so it covers "the server accepted the
    request then dropped the connection before responding" as well as
    "connection refused", and those are indistinguishable here. Ambiguous
    means no rollback. (sol-critic round 2, blocker 1.)"""

    # The turn_id whose `turn_started` event was ALREADY appended when this
    # rejection was raised, else None (additive, same posture as pre_harness).
    # The queue's kicker uses it to close the failed turn's transcript with a
    # terminal `error` event — a queued start has no HTTP caller to answer, so
    # without this the prompt's turn_started would dangle forever.
    turn_id: Optional[str] = None

    def __init__(self, status_code: int, error_code: str, message: str,
                 extra: Optional[Dict[str, Any]] = None,
                 pre_harness: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.extra = extra or {}
        self.pre_harness = pre_harness


# --------------------------------------------------------------------------- #
# session guard
# --------------------------------------------------------------------------- #
def _require_session(tenant_id: str, session_id: str) -> Dict[str, Any]:
    """404-not-403 posture (matches routers/jobs.py's cross-tenant pattern):
    an unknown id and a real-but-foreign-tenant id look identical to the caller."""
    sess = session_store.get_session(session_id)
    if sess is None or str(sess.get("tenant_id")) != str(tenant_id):
        raise TurnRejected(404, ErrorCode.SESSION_NOT_FOUND,
                           f"unknown session_id {session_id!r}")
    return sess


# --------------------------------------------------------------------------- #
# bounded prior-context messages[]
# --------------------------------------------------------------------------- #
def _prior_messages(session_id: str, exclude_turn_id: str) -> List[Dict[str, str]]:
    """Fold the recent event log into `[{role, text}, ...]`: a `user` message
    per prior turn's `turn_started.data.text` (confirm-only turns contribute no
    user text) and an `assistant` message per prior turn's concatenated
    `text_delta.data.text`, but ONLY for turns that actually completed
    (`turn_complete`/`error` seen) — an in-flight turn never contributes a
    partial assistant message. `exclude_turn_id` keeps the CURRENT turn (whose
    `turn_started` was just appended) out of its own prior context."""
    events = session_store.recent_events(session_id, PRIOR_EVENTS_WINDOW)
    order: List[str] = []
    by_turn: Dict[str, Dict[str, Any]] = {}
    for ev in events:
        tid = ev.get("turn_id")
        if not tid or tid == exclude_turn_id:
            continue
        slot = by_turn.get(tid)
        if slot is None:
            slot = {"user": None, "parts": [], "terminal": False}
            by_turn[tid] = slot
            order.append(tid)
        etype = ev.get("type")
        data = ev.get("data") or {}
        if etype == "turn_started":
            text = data.get("text")
            if isinstance(text, str) and text:
                slot["user"] = text
        elif etype == "text_delta":
            piece = data.get("text")
            if isinstance(piece, str):
                slot["parts"].append(piece)
        elif etype in ("turn_complete", "error"):
            slot["terminal"] = True

    messages: List[Dict[str, str]] = []
    for tid in order:
        slot = by_turn[tid]
        if slot["user"]:
            messages.append({"role": "user", "text": slot["user"]})
        if slot["terminal"] and slot["parts"]:
            messages.append({"role": "assistant", "text": "".join(slot["parts"])})
    return messages[-MAX_PRIOR_MESSAGES:]


def _safe_json(resp: "requests.Response") -> Optional[Dict[str, Any]]:
    try:
        body = resp.json()
        return body if isinstance(body, dict) else None
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# start_turn
# --------------------------------------------------------------------------- #
def start_turn(tenant_id: str, session_id: str, *, text: Optional[str] = None,
               confirm: Optional[Dict[str, Any]] = None,
               classifier_hint: Optional[Dict[str, Any]] = None,
               model: Optional[str] = None,
               credential_grant: Optional[Dict[str, Any]] = None,
               tier: Optional[str] = None,
               subject: Optional[str] = None,
               queued_id: Optional[str] = None) -> str:
    # In live auth this is a deps.TenantContext, a str subclass carrying the
    # verified claim. Snapshot the claim before normalizing to the frozen
    # string tenant_id used on the harness wire. Off-auth callers are plain
    # strings and retain the existing broker/demo fallback. An explicit `tier`
    # (additive keyword) wins: the queue's kicker holds only the plain string
    # tenant_id, so it passes the tier it snapshotted at enqueue time.
    if tier is None:
        tier = getattr(tenant_id, "tier", None)
    # The verified subject that opened this turn, snapshotted with the tier.
    # The harness back-edge authenticates as a tenant and cannot assert a user,
    # so protected authoring resolves the author from this record instead.
    if subject is None:
        subject = getattr(tenant_id, "subject", None)
    # For the terminal-time policy auto-confirm (same posture as the queue
    # kicker's enqueue-time snapshot): resolve while the principal object is
    # still in hand, before it is flattened to a plain string.
    entitlement_tier = entitlements.resolve_tier(tenant_id)
    tenant_id = str(tenant_id)
    sess = _require_session(tenant_id, session_id)

    turn_id = str(uuid.uuid4())
    max_s = turn_max_s()
    if not session_store.try_begin_turn(session_id, turn_id, max_s, tier=tier,
                                        subject=subject):
        raise TurnBusy(f"session {session_id!r} already has an active turn")

    # The durable transcript source: whatever drove this turn (a fresh user
    # message OR the resume of a halted turn), plus the optional dispatcher
    # hint — classifier_hint is recorded here for the durable log ONLY, it is
    # NOT part of ConverseTurnInput (frozen shape has no such field).
    # Strip a pasted BYO credential BEFORE the transcript append below, so it is
    # never durable here and never rides the wire. See _scrub_secret.
    _secret = _grant_secret(credential_grant)
    if _secret:
        text = _scrub_secret(text, _secret)
        # classifier_hint is durable-log-only and never reaches the harness, so
        # this is the only chance to strip it (see _scrub_tree).
        if classifier_hint is not None:
            classifier_hint = _scrub_tree(classifier_hint, _secret)
        # `confirm` is deliberately NOT scrubbed. Its proposal is built
        # server-side from the STORED approval row (routers/sessions.py builds
        # {tool, params, capability} from `approval`, never from the client), so
        # a caller cannot inject a credential into it. When the SAME grant was
        # mounted on the propose turn, its params are clean already: this scrub
        # ran on that turn's prompt before the model ever saw it.
        #
        # That invariant is NOT absolute, and deliberately so. A proposal made
        # with NO grant, with a DIFFERENT grant, or before this code existed can
        # contain a value later mounted as the confirm turn's grant. Scrubbing it
        # here still would not help: that value was already user content, already
        # seen by the model, and already persisted in the propose turn's
        # transcript — so the approval row is a second copy of an existing leak,
        # not a new one. Proposal-time scrubbing cannot help either, since it
        # cannot know a future, different grant.
        #
        # Scrubbing it anyway is actively harmful: the app gate binds an approval
        # to the EXACT argument hash, so rewriting the approved args makes
        # redemption fail as `args_mismatch` on the documented store-loss
        # recovery path, where spineTurnAdapter rebuilds its confirmation mirror
        # from this proposal. That is the same self-inflicted break that the
        # sink-scrubbing attempt caused in rounds 3-5.
        # (sol-critic PR #123 round 8, blocker 1.)

    user_data: Dict[str, Any] = {}
    if text is not None:
        user_data["text"] = text
    if queued_id is not None:
        # The promoted-from-queue identity, TRANSCRIPT-ONLY (additive event
        # field; the frozen harness wire payload below never carries it). The
        # client's queued-note reconciliation keys on this id — text matching
        # was race-prone when two prompts shared identical text (PR #305
        # review round 2).
        user_data["queued_id"] = queued_id
    if confirm is not None:
        user_data["confirm"] = confirm
    if classifier_hint is not None:
        user_data["classifier_hint"] = classifier_hint
    session_store.append_event(session_id, turn_id, "turn_started", user_data)

    def _release_cas() -> None:
        # Synchronous rejections release the CAS HERE, not through the relay's
        # _finalize_terminal — so without this kick, a DIRECT turn that wins
        # the CAS while a kicker has the queue claimed and then rejects
        # synchronously leaves the parked prompt stranded on a free session
        # (review round 2, finding 1: the kicker saw "genuinely busy" and
        # trusted a terminal kick that never comes on this path). Re-entrancy
        # is safe: when THIS start_turn was itself invoked by a kicker, the
        # slot's `starting` claim is still held, so the nested kick returns
        # immediately.
        session_store.end_turn(session_id, turn_id)
        # BOTH follow-ups, like every other slot releaser (review round 9: the
        # queue kick alone left a policy decision parked against this exact
        # release with nothing to retry it). tier/entitlement_tier are the
        # snapshots this start_turn already took. Re-entrancy is bounded the
        # same way as the kick: a nested attempt on a cid that is mid-flight
        # sees its transient consumed/claimed state and stands down.
        _auto_confirm_reads(tenant_id, session_id, {}, tier, entitlement_tier)
        _kick_queued(session_id)

    def _rejected(*args: Any, **kwargs: Any) -> TurnRejected:
        # Every rejection BELOW this point happens after `turn_started` was
        # appended — tag the turn_id so a queued start's kicker can close the
        # transcript it opened (see TurnRejected.turn_id).
        exc = TurnRejected(*args, **kwargs)
        exc.turn_id = turn_id
        return exc

    harness_url = _harness_url()
    if not harness_url:
        _release_cas()
        # pre_harness: there is no URL, so no POST is even attempted — the
        # harness cannot have redeemed a confirm's approval.
        raise _rejected(502, ErrorCode.BROKER_UNREACHABLE,
                        "neither LEAF_CONVERSE_HARNESS_URL nor "
                        "LEAF_AUTHOR_HARNESS_URL is configured",
                        pre_harness=True)

    payload: Dict[str, Any] = {
        "tenant_id": tenant_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "drawing_id": sess["drawing_id"],
        "messages": _prior_messages(session_id, exclude_turn_id=turn_id),
    }
    if text is not None:
        payload["text"] = text
    if confirm is not None:
        payload["confirm"] = confirm

    # "Mount your LLM" (additive wire fields, only present when in play):
    #   - model: the per-turn override wins, else the session's stored model; an
    #     unknown id is dropped so the harness applies its env default rather than
    #     erroring (the app router already 400s unknown ids at the entry).
    #   - credential_grant: forwarded verbatim, NEVER logged, NEVER persisted.
    effective_model = model if model is not None else sess.get("model")
    if is_allowed_model(effective_model):
        payload["model"] = effective_model
    if credential_grant is not None:
        payload["credential_grant"] = credential_grant
    instant_assignment = instant_execution.assignment_for_session(tenant_id, session_id)
    harness_headers = broker_client.harness_headers()
    # Plan-first rides the SIDECAR, not the frozen turn body (the
    # instant-assignment precedent): consumed by the harness before the runner
    # starts, never in the transcript. Reading the policy can never sink a
    # turn — an unreadable policy simply omits the header, which the harness
    # treats as confirm_all: the SAFE direction (nothing widens).
    try:
        if session_policy.get_policy(session_id, tenant_id) == "plan_first":
            harness_headers["x-leaf-approval-policy"] = "plan_first"
    except Exception:  # noqa: BLE001
        pass
    if instant_assignment is not None:
        # Keep the frozen turn body exact. This authenticated sidecar header is
        # consumed before the runner starts and never enters the transcript.
        encoded = json.dumps(instant_assignment, separators=(",", ":")).encode("utf-8")
        harness_headers["x-leaf-instant-assignment"] = base64.urlsafe_b64encode(encoded).decode("ascii")

    try:
        resp = requests.post(f"{harness_url}/turn", json=payload,
                             headers=harness_headers,
                             stream=True, timeout=(5, max_s))
    except requests.exceptions.ConnectTimeout as exc:
        # The ONLY pre_harness leg here. A connect-phase timeout means the TCP
        # connection was never established, so no byte of this request reached
        # the harness.
        #
        # ConnectTimeout subclasses BOTH ConnectionError and Timeout, and
        # Python takes the FIRST matching handler — so this clause must stay
        # ABOVE both clauses below. Order is load-bearing.
        _release_cas()
        raise _rejected(502, ErrorCode.BROKER_UNREACHABLE,
                        f"harness at {harness_url} unreachable: {exc}",
                        pre_harness=True) from exc
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        # Deliberately NOT pre_harness — BOTH of these are ambiguous.
        #
        # ReadTimeout: the POST was sent and the harness may be acting on it.
        #
        # Plain ConnectionError: requests folds urllib3's ProtocolError into
        # this class, which covers "the server accepted the request then
        # dropped the connection before responding" (RemoteDisconnected) just
        # as much as "connection refused". We CANNOT tell those apart here, so
        # the fail-safe answer is to treat the harness as possibly-having-acted
        # and leave the approval spent. Un-spending an approval whose tool call
        # might already be running is exactly the double-execution consume-once
        # exists to prevent. (sol-critic round 2, blocker 1.)
        _release_cas()
        raise _rejected(502, ErrorCode.BROKER_UNREACHABLE,
                        f"harness at {harness_url} unreachable: {exc}") from exc
    except requests.exceptions.RequestException as exc:  # noqa: BLE001
        _release_cas()
        raise _rejected(502, ErrorCode.BROKER_UNREACHABLE,
                        f"harness request failed: {exc}") from exc

    if resp.status_code == 401:
        _release_cas()
        body = _safe_json(resp) or {}
        msg = (body.get("message") or (body.get("error") or {}).get("message")
              or f"tenant {tenant_id!r} has no linked Claude grant")
        resp.close()
        raise _rejected(401, ErrorCode.GRANT_REQUIRED, msg, extra={"grant_required": True})

    if resp.status_code == 429:
        _release_cas()
        body = _safe_json(resp) or {}
        code = body.get("errorCode") or body.get("error_code")
        if code not in (ErrorCode.LLM_QUOTA_EXHAUSTED, ErrorCode.LLM_RATE_LIMITED):
            code = ErrorCode.LLM_RATE_LIMITED
        msg = body.get("message") or "harness reported a rate/quota limit"
        resp.close()
        raise _rejected(429, code, msg)

    if resp.status_code >= 400:
        _release_cas()
        body = _safe_json(resp) or {}
        msg = body.get("message") or f"harness returned HTTP {resp.status_code}"
        resp.close()
        raise _rejected(502, ErrorCode.BROKER_UNREACHABLE, msg)

    _spawn_relay(tenant_id, session_id, turn_id, resp, max_s,
                 tier=tier, entitlement_tier=entitlement_tier)
    return turn_id


# --------------------------------------------------------------------------- #
# streaming relay (daemon thread) + watchdog (daemon thread)
# --------------------------------------------------------------------------- #
def _drain_terminal(deadline: float, exc: BaseException) -> tuple:
    """The terminal event for a drain-side stream failure, decided by the
    turn's own deadline rather than by which thread noticed first.

    The drain thread and the watchdog race for the same condition. The drain
    thread is started FIRST and its socket read timeout is also TURN_MAX_S, and
    urllib3 restarts that timeout on every chunk received — so when the harness
    goes silent (the canonical hang) BOTH deadlines land at the same instant
    and the winner is whichever thread the scheduler happens to run. Measured
    on an IDLE host, the drain won 11 of 12 zero-chunk hangs, and the caller
    saw `error{INTERNAL}` where this module documents
    `turn_complete{stop_reason:'timeout'}`.

    Widening one side's budget cannot fix that: any fixed margin is only a
    scheduling allowance, and the loser can still win under enough delay. So
    the two paths are not raced — they are made to AGREE. Past the turn's own
    deadline, a stream failure IS turn expiry, and both paths report expiry
    identically. Which thread got there first stops being observable.

    Note the deliberate consequence: a genuine transport fault that happens to
    land after the deadline is reported as a timeout. That is correct — the
    turn had already outlived TURN_MAX_S, so the watchdog was entitled to
    terminalize it as a timeout in the very next instant regardless.
    """
    if time.monotonic() >= deadline:
        return "turn_complete", {"stop_reason": "timeout"}
    return "error", {"error": {"error_code": ErrorCode.INTERNAL,
                               "message": f"{type(exc).__name__}: {exc}"}}


def _eof_terminal(deadline: float) -> tuple:
    """The terminal event for a stream that simply ENDED without one.

    Same arbitration as _drain_terminal: past the turn's deadline a stream
    that stopped producing is turn expiry, and must read exactly like the
    watchdog's terminal event. Before the deadline the historical behavior is
    preserved — release the CAS, append nothing — because that case is an
    unexpectedly short stream rather than an expired turn.
    """
    if time.monotonic() >= deadline:
        return "turn_complete", {"stop_reason": "timeout"}
    return None, None


def request_cancel(tenant_id: Any, session_id: str, turn_id: str) -> str:
    """Interrupt an in-flight turn. Returns "cancelled" or "not_active".

    Two paths, and the fallback is the point:

    * The relay is in THIS process -> its registered canceller terminalizes the
      turn as `turn_complete{stop_reason:"interrupted"}` and closes the harness
      response, which unblocks the drain thread at once.
    * No canceller is registered (the relay's process restarted, or the row
      outlived its thread) -> terminalize the durable row directly, so the
      session's CAS is released either way. Without this branch a cancel would
      silently no-op on an orphaned turn and the session would stay wedged
      until the stale-turn window expired.

    Ownership is enforced by `_require_session` (404-not-403, same posture as
    start_turn). Cancelling anything other than the session's CURRENT active
    turn is refused rather than silently accepted, so a stale client cannot
    terminalize the turn that replaced the one it was looking at.
    """
    # Snapshot BEFORE flattening (the router passes the live principal): the
    # orphan branch below runs the terminal follow-ups, and the policy retry's
    # entitlement check must see the caller's real tier — a flattened string
    # resolves to the demo tier, which under live auth would run the retry
    # with entitlements the tenant may not hold.
    tier = getattr(tenant_id, "tier", None)
    entitlement_tier = entitlements.resolve_tier(tenant_id)
    tenant_id = str(tenant_id)
    sess = _require_session(tenant_id, session_id)

    if str(sess.get("active_turn_id") or "") != str(turn_id):
        return "not_active"

    if is_reservation(turn_id):
        # Not a turn: a restore owns the slot and releases it itself.
        return "not_cancellable"

    with _cancellers_lock:
        canceller = _cancellers.get(turn_id)

    if canceller is not None:
        canceller()  # idempotent: _end_once is one-shot across every thread
        return "cancelled"

    # Orphaned row: no relay here to terminalize it, so do it directly — under
    # the orphan lock, with the active-turn check RE-READ inside it. The check
    # above happened outside any lock, so two simultaneous cancels could both
    # have passed it; re-reading here makes check-then-act atomic and keeps the
    # transcript to exactly one terminal event.
    with _orphan_cancel_lock:
        sess = session_store.get_session(session_id)
        if sess is None or str(sess.get("active_turn_id") or "") != str(turn_id):
            return "not_active"  # someone else terminalized it while we waited
        try:
            session_store.append_event(
                session_id, turn_id, "turn_complete", {"stop_reason": "interrupted"})
        except Exception:  # noqa: BLE001  best-effort; releasing the CAS is what matters
            pass
        session_store.end_turn(session_id, turn_id)
    # This path has no relay, so no _finalize_terminal will run for the turn —
    # BOTH follow-ups run here or a parked policy decision / queued prompt
    # waits forever (review rounds 3 and 9).
    _auto_confirm_reads(tenant_id, session_id, {}, tier, entitlement_tier)
    _kick_queued(session_id)
    return "cancelled"


# --------------------------------------------------------------------------- #
# busy-turn queue (cap 1): enqueue + the terminal-time kicker
# --------------------------------------------------------------------------- #
def queued_prompt(session_id: str) -> Optional[Dict[str, Any]]:
    """The session's pending prompt (a copy), or None. Read-only, for tests
    and status surfaces."""
    with _queued_lock:
        payload = _queued.get(session_id)
        if not payload:
            return None
        safe = dict(payload)
        safe.pop("subject", None)
        return safe


def try_enqueue_turn(tenant_id: str, session_id: str, *, text: str,
                     classifier_hint: Optional[Dict[str, Any]] = None,
                     model: Optional[str] = None) -> tuple:
    """Queue ONE text prompt to start when the active turn ends.

    Returns ("queued", queued_id) or ("full", None). Only plain text turns are
    eligible — the ROUTER enforces that (a confirm's approval lifecycle and a
    credential grant's never-persist rule both forbid queueing); this function
    trusts its caller on that and takes no confirm/grant parameters at all, so
    the ineligible shapes cannot even be expressed here.

    The durable record is a `turn_queued` transcript event (unknown event types
    are ignored by _prior_messages and by the SSE client, so it is additive).
    Ownership was checked by the router; the session vanishing between that
    check and the append here surfaces as TurnRejected(404).
    """
    tier = getattr(tenant_id, "tier", None)
    subject = getattr(tenant_id, "subject", None)
    # The entitlement tier is snapshotted too, so the KICKER can re-run the
    # router's converse-entitlement check at start time (review round 1,
    # finding 4): a revocation landing during the wait must gate the queued
    # start exactly as it would gate a direct request.
    entitlement_tier = entitlements.resolve_tier(tenant_id)
    tenant_id = str(tenant_id)
    queued_id = str(uuid.uuid4())
    payload = {
        "queued_id": queued_id,
        "tenant_id": tenant_id,
        "tier": tier,
        "subject": subject,
        "entitlement_tier": entitlement_tier,
        "text": text,
        "classifier_hint": classifier_hint,
        "model": model,
        "created_at": time.time(),
        "starting": False,
    }
    with _queued_lock:
        if session_id in _queued:
            return ("full", None)
        _queued[session_id] = payload
    try:
        session_store.append_event(session_id, None, "turn_queued",
                                   {"queued_id": queued_id, "text": text})
    except Exception as exc:  # noqa: BLE001  (KeyError: session deleted)
        with _queued_lock:
            if (_queued.get(session_id) or {}).get("queued_id") == queued_id:
                del _queued[session_id]
        raise TurnRejected(404, ErrorCode.SESSION_NOT_FOUND,
                           f"unknown session_id {session_id!r}") from exc
    # Close the enqueue/terminal handoff race: the active turn this prompt
    # queued BEHIND may have terminalized between the caller's CAS loss and the
    # registration above — in which case no future terminal event will kick.
    # One explicit check makes the pair airtight: either the terminal kicker
    # saw our registration, or we see its released CAS here.
    sess = session_store.get_session(session_id)
    if sess is not None and sess.get("active_turn_id") is None:
        _kick_queued(session_id)
    return ("queued", queued_id)


def drain_session_followups(tenant_id: Any, session_id: str) -> None:
    """The terminal-time follow-ups, for a slot released OUTSIDE a relay.

    A relay's _finalize_terminal runs the policy park drain/retry and then the
    queue kick. A NON-turn releaser — the checkpoint restore's reservation —
    used to call only _kick_queued, so a policy decision parked while the
    reservation held the slot had nothing to retry it until an unrelated later
    terminal or expiry (PR #311 round 8: the round-2 unreachable-resume,
    recreated by new main content). Every slot releaser runs BOTH follow-ups,
    in the relay's order. Accepts the live principal so tier and entitlement
    resolve exactly as start_turn would.
    """
    tier = getattr(tenant_id, "tier", None)
    entitlement_tier = entitlements.resolve_tier(tenant_id)
    _auto_confirm_reads(str(tenant_id), session_id, {}, tier, entitlement_tier)
    _kick_queued(session_id)


def _kick_queued(session_id: str) -> None:
    """Start the session's queued prompt, if any. Runs at every terminal site
    (relay finalization, orphan cancel) and at enqueue time when the session
    turned out to be free. NEVER raises — it runs on relay threads whose
    cleanup must complete.

    CLAIM, don't pop: the slot is marked `starting` and stays registered for
    the whole attempt, so a concurrent enqueue answers "full" and no second
    prompt can be accepted while this one's outcome is undecided (review
    round 1, findings 1 and 3 — popping opened a gap where a newly-202'd
    prompt could be stranded on a free session, and where an accepted prompt
    could be silently superseded). A concurrent kick that sees `starting`
    stands down; the claim-holder owns the outcome.

    Exit states, exhaustively: STARTED (slot emptied) · REJECTED (transcript
    closed with a terminal error for the turn_started it opened, slot
    emptied, then one more lap in case a prompt arrived mid-attempt — it
    cannot have, see above, but the lap is free) · ENTITLEMENT-DENIED (slot
    emptied, durable turn_queue_dropped) · BUSY (slot reverted to parked;
    the foreign turn's own terminal kick starts it — unless that turn ended
    inside our window, which the free-session re-check catches by retrying)
    · CRASH (slot emptied, loud stderr).
    """
    attempts = 0
    while attempts < _KICK_MAX_ATTEMPTS:
        attempts += 1
        with _queued_lock:
            payload = _queued.get(session_id)
            if payload is None or payload["starting"]:
                return  # nothing to do, or another kicker owns the attempt
            payload["starting"] = True

        def _release(*, keep: bool) -> None:
            """Drop the claim: keep=True reverts to parked, else empties."""
            with _queued_lock:
                current = _queued.get(session_id)
                if current is not None and current["queued_id"] == payload["queued_id"]:
                    if keep:
                        current["starting"] = False
                    else:
                        del _queued[session_id]

        # OUTER exception boundary: _kick_queued runs on relay threads and
        # promises never-raises / never-leaks-the-claim. The inner handlers
        # cover start_turn; this covers everything else after the claim
        # (the entitlement drop branch, the release bookkeeping, even a
        # failing stderr print) — an escape here previously left `starting`
        # True forever (review round 2, finding 2).
        try:
            # The router's entitlement gate, re-run at START time: a revocation
            # during the wait must not be laundered through the queue. Deny AND
            # policy-unavailable both fail closed — a paid turn never starts
            # without a current policy yes — with a durable drop record. The catch
            # is Exception, not just EntitlementsError: _kick_queued's contract is
            # never-raises and never-leaks-the-claim, and an unexpected evaluator
            # crash here previously escaped with `starting` still True (review
            # round 2, finding 2). An unevaluable policy is a NO either way.
            try:
                allowed = entitlements.entitlements_for(
                    payload["entitlement_tier"]).get("converse", False)
            except Exception:  # noqa: BLE001
                allowed = False
            if not allowed:
                _release(keep=False)
                try:
                    session_store.append_event(
                        session_id, None, "turn_queue_dropped",
                        {"queued_id": payload["queued_id"],
                         "reason": "entitlement_denied"})
                except Exception:  # noqa: BLE001
                    pass
                print(f"[leaf-agent] queued prompt {payload['queued_id']!r} dropped: "
                      f"converse entitlement no longer granted "
                      f"(session {session_id!r})", file=sys.stderr, flush=True)
                return

            try:
                start_turn(payload["tenant_id"], session_id,
                           text=payload["text"],
                           classifier_hint=payload["classifier_hint"],
                           model=payload["model"],
                           tier=payload["tier"],
                           subject=payload["subject"],
                           queued_id=payload["queued_id"])
                _release(keep=False)
                return
            except TurnBusy:
                _release(keep=True)
                sess = session_store.get_session(session_id)
                if sess is None or sess.get("active_turn_id"):
                    return  # genuinely busy — that turn's terminal will kick
                continue  # the busy turn ended inside the window; try again
            except TurnRejected as exc:
                # No HTTP caller to answer: close the transcript the failed start
                # opened (turn_started with no terminal would dangle forever), and
                # say so on stderr. The prompt is consumed — retrying a start the
                # harness just rejected would loop against a down harness.
                _release(keep=False)
                if exc.turn_id is not None:
                    try:
                        session_store.append_event(
                            session_id, exc.turn_id, "error",
                            {"error": {"error_code": exc.error_code,
                                       "message": exc.message},
                             "stop_reason": "error"})
                    except Exception:  # noqa: BLE001
                        pass
                print(f"[leaf-agent] queued turn start FAILED "
                      f"({exc.error_code}) session={session_id!r}: {exc.message}",
                      file=sys.stderr, flush=True)
                # One more lap: the claim held the slot for the whole attempt, so
                # nothing can have been accepted meanwhile — but the check is one
                # dict read, and belt-and-braces beats reasoning alone here.
                continue
            except Exception as exc:  # noqa: BLE001
                _release(keep=False)
                print(f"[leaf-agent] queued turn start CRASHED "
                      f"({type(exc).__name__}) session={session_id!r}",
                      file=sys.stderr, flush=True)
                return
        except Exception as exc:  # noqa: BLE001
            _release(keep=False)
            try:
                print(f"[leaf-agent] queue kick internals CRASHED "
                      f"({type(exc).__name__}) session={session_id!r}",
                      file=sys.stderr, flush=True)
            except Exception:  # noqa: BLE001
                pass
            return
    print(f"[leaf-agent] QUEUE KICK EXHAUSTED after {_KICK_MAX_ATTEMPTS} "
          f"busy-races on session {session_id!r} — the prompt stays parked; "
          f"the next enqueue-time kick is the backstop. This schedule should "
          f"be impossible; investigate if it appears.",
          file=sys.stderr, flush=True)


def _auto_confirm_reads(tenant_id: str, session_id: str,
                        proposals: Dict[str, Dict[str, Any]],
                        tier: Optional[str],
                        entitlement_tier: Optional[str]) -> None:
    """Policy auto-approval: under `auto_approve_reads`, decide and confirm the
    turn's FIRST `drawing.read` proposal at its terminal. NEVER raises - it
    runs on relay threads whose cleanup must complete.

    GATE-FIRST, exactly like the human path (routers/agent.py): the section 18
    gate record is the AUTHORITY the resume turn consults, and the session row
    is the mirror. Deciding only the mirror leaves the gate pending, so the
    resumed tool never dispatches (review round 1, blocker 2). So this grants
    the gate first, then the session row - same order, same failure posture: a
    gate that will not grant means no confirm turn and no session decision.

    RESUMABLE, so a lost race cannot strand the user's approval: a row already
    decided by THIS policy and not yet consumed is finished on a later
    terminal. Without that, a TurnBusy left the row decided-by-policy forever -
    a human could not re-decide it (already decided) and under live auth could
    not consume it either (consume binds decided_by to the acting subject).

    Every other gate the human path enforces is re-enforced from the STORED
    row, never the relay's in-memory copy: session+tenant match, unexpired,
    unconsumed, eligible capability, stored dwg binding.
    """
    try:
        # A decision parked by an earlier lost CAS outranks this turn's own
        # proposals: it is already decided and the user is waiting on it.
        with _pending_policy_lock:
            parked = list(_pending_policy.get(session_id) or ())

        try:
            allowed = (
                session_policy.get_policy(session_id, tenant_id) == "auto_approve_reads"
                and entitlements.entitlements_for(
                    entitlement_tier).get("converse", False))
        except Exception:  # noqa: BLE001
            allowed = False
        if not allowed:
            # Policy off (or entitlement lost) means STOP AUTO-RUNNING —
            # including decisions parked while it was on. The entries are
            # DROPPED, not finished (finishing one is still an auto-run), and
            # the rows themselves are time-bounded: an approval carries
            # expires_at (SESSIONS_APPROVAL_TTL_S), so a policy-decided row
            # nobody can consume simply expires. Without this drain a parked
            # entry was never inspected again while the new policy was active
            # (review round 6, finding 2).
            if parked:
                with _pending_policy_lock:
                    _pending_policy.pop(session_id, None)
                print(f"[leaf-agent] policy auto-confirm: policy off for "
                      f"{session_id!r}; dropping {len(parked)} parked "
                      f"decision(s) {parked!r} — the rows expire by TTL",
                      file=sys.stderr, flush=True)
            return

        candidates = parked + [
            cid for cid, data in proposals.items()
            if data.get("capability") in READONLY_AUTO_CAPABILITIES
            and cid not in parked]
        if not candidates:
            return

        for cid in candidates:
            if _try_one_policy_confirm(tenant_id, session_id, cid, tier):
                return  # one turn started; the rest keep for a later terminal
    except Exception as exc:  # noqa: BLE001
        try:
            print(f"[leaf-agent] policy auto-confirm CRASHED "
                  f"({type(exc).__name__}) session={session_id!r}",
                  file=sys.stderr, flush=True)
        except Exception:  # noqa: BLE001
            pass


def _try_one_policy_confirm(tenant_id: str, session_id: str, cid: str,
                            tier: Optional[str]) -> bool:
    """One candidate. True iff a confirm turn STARTED (the caller then stops:
    at most one auto-started turn per terminal). False means "not this one" —
    ineligible, raced, or the CAS was lost — and the caller tries the next, so
    a fresh proposal can never starve an already-decided strand and a strand
    can never displace one either (review round 3)."""
    try:

        def _forget_parked() -> None:
            with _pending_policy_lock:
                rest = [c for c in (_pending_policy.get(session_id) or ()) if c != cid]
                if rest:
                    _pending_policy[session_id] = rest
                else:
                    _pending_policy.pop(session_id, None)

        approval = session_store.get_approval(cid)
        if (approval is None
                or str(approval.get("session_id")) != str(session_id)
                or str(approval.get("tenant_id")) != str(tenant_id)
                or approval.get("expired")
                or approval.get("capability") not in READONLY_AUTO_CAPABILITIES):
            # Expired / vanished / foreign: a parked retry for it is dead.
            _forget_parked()
            return False
        if approval.get("consumed"):
            # CONSUMED IS TRANSIENT, not terminal: a winner sits between its
            # consume and its own outcome handling (success forgets the slot;
            # TurnBusy unconsumes and keeps it). A loser that released here
            # freed capacity while the row was decided — the round-6 leak: a
            # third row decided beyond the cap, and the winner's entry
            # orphaned. Only a row consumed under a HUMAN's decision is
            # terminally out of the policy's hands.
            if approval.get("decided_by") != POLICY_DECIDER:
                _forget_parked()
            return False
        if approval.get("decided") and approval.get("decided_by") != POLICY_DECIDER:
            # Only OUR OWN unfinished decision may be resumed; a human's
            # decision is theirs to redeem.
            return False
        # RESERVE THE SLOT before doing anything that can end in a park, and
        # for EVERY candidate — decided (a resume) or not. Two lock
        # acquisitions (read the length, append later) let N concurrent
        # terminals all observe room and all park, and a resume path that took
        # no slot at all appended unconditionally on TurnBusy; both blew the
        # cap (review round 5). Taking the slot in the SAME acquisition as the
        # check is what makes the bound real. Released on every exit that does
        # not leave work pending.
        with _pending_policy_lock:
            tracked = _pending_policy.setdefault(session_id, [])
            if cid not in tracked:
                if len(tracked) >= MAX_PENDING_POLICY:
                    if not tracked:
                        _pending_policy.pop(session_id, None)
                    print(f"[leaf-agent] policy auto-confirm: {session_id!r} "
                          f"already has {len(tracked)} unfinished policy "
                          f"decisions; leaving {cid!r} for a human",
                          file=sys.stderr, flush=True)
                    return False
                tracked.append(cid)

        stored_payload = approval.get("payload")
        stored_dwg = (stored_payload.get("dwg")
                      if isinstance(stored_payload, dict) else None)
        if not (isinstance(stored_dwg, str) and stored_dwg):
            _forget_parked()
            return False  # the router's rule: no dwg binding, no confirm

        if not approval.get("decided"):
            # CAPACITY FIRST, so an eviction can never strand a decision.
            # Deciding a row we have no room to track would recreate the exact
            # defect the park exists to fix: dropping the oldest loses a
            # decided-unconsumed row, and dropping the newest loses the one we
            # just decided (review round 4). An undecided row left alone is
            # simply a normal manual chip — the fail-closed outcome.
            # GATE FIRST (routers/agent.py's load-bearing ordering). A gate
            # record that is corrupt/unreadable, or that refuses the grant,
            # means this chip stays manual - never decide the mirror against a
            # gate we could not grant.
            _gate_rec, gate_status = agent_gate.read_pending_strict(cid)
            if gate_status == "ok":
                try:
                    granted, _rec, reason = agent_gate.grant_approval(
                        cid, by=POLICY_DECIDER)
                except Exception as exc:  # noqa: BLE001
                    _forget_parked()  # nothing decided: release the reservation
                    print(f"[leaf-agent] policy auto-confirm: gate write failed "
                          f"({type(exc).__name__}) session={session_id!r}",
                          file=sys.stderr, flush=True)
                    return False
                if not granted:
                    _forget_parked()
                    print(f"[leaf-agent] policy auto-confirm: gate refused "
                          f"({reason}) session={session_id!r}",
                          file=sys.stderr, flush=True)
                    return False
            elif gate_status != "absent":
                # corrupt / io_error: fail closed, leave it to a human.
                _forget_parked()
                print(f"[leaf-agent] policy auto-confirm: gate unreadable "
                      f"({gate_status}) session={session_id!r}",
                      file=sys.stderr, flush=True)
                return False
            # `absent` = the legacy pair flow with no gate record; the session
            # row is the whole story there, as on the human path.

            try:
                outcome = session_store.decide_approval(cid, True, by=POLICY_DECIDER)
            except Exception as exc:  # noqa: BLE001
                # The gate is granted and the mirror is not. This window is
                # IDENTICAL to the human path's (routers/agent.py grants the
                # gate, then writes the session row) - it is inherent to two
                # stores with no shared transaction, not new here. It is
                # RECOVERABLE in the safe direction: a human approving later
                # re-grants an already-granted gate (a no-op) and writes the
                # mirror. It is NOT recoverable by a human DENIAL, which cannot
                # ungrant the gate - so it is reported loudly rather than
                # swallowed.
                print(f"[leaf-agent] policy auto-confirm: GATE GRANTED BUT "
                      f"SESSION ROW NOT DECIDED ({type(exc).__name__}) "
                      f"confirmation_id={cid!r} session={session_id!r} - a "
                      f"human approval repairs this; a denial cannot",
                      file=sys.stderr, flush=True)
                # The GATE is granted, so this row is not "untouched" — keep the
                # reservation so a later terminal retries the mirror write.
                return False
            if outcome != "recorded":
                # A racer got here first — stand down. WHO owns the slot now
                # decides whether to release it: the slot belongs to the CID,
                # not to this thread. If the winner was OUR OWN policy (another
                # terminal racing the same cid), that winner is mid-flight and
                # still needs the reservation — a loser that released it here
                # freed capacity while the row was decided, letting a THIRD row
                # be decided beyond the cap (round 5's decided>tracked). Only a
                # row now owned by a HUMAN (or vanished) releases.
                current = session_store.get_approval(cid)
                if current is None or current.get("decided_by") != POLICY_DECIDER:
                    _forget_parked()
                return False

        try:
            consumed = session_store.consume_approval(cid, session_id, tenant_id)
        except session_store.ApprovalConsumeError:
            # The slot belongs to the cid: a consume race means a WINNER (our
            # own policy on another thread, or a human confirm) holds the row
            # mid-flight — leave the reservation to the winner's handlers.
            return False
        confirm_payload = {
            "confirmation_id": cid,
            "approved": bool(consumed.get("approved")),
            "proposal": {
                "tool": consumed.get("tool"),
                "params": consumed.get("params"),
                "capability": consumed.get("capability"),
                "dwg": stored_dwg,
            },
        }
        try:
            start_turn(tenant_id, session_id, confirm=confirm_payload, tier=tier)
            _forget_parked()
            return True
        except TurnBusy:
            # Provably unredeemed (the CAS is start_turn's first act): return
            # the consume so a LATER terminal can finish this same decision
            # (see RESUMABLE above). The decision itself stands - it is ours,
            # and re-granting an already-granted gate would be the divergence
            # the gate-first ordering exists to prevent.
            # The give-back gets THREE tries, and a persistent failure is
            # reported on the ALARMABLE channel (the router's precedent for a
            # destroyed approval): the row stays consumed-by-policy, unusable
            # by a human under live auth, until its TTL expires — at which
            # point the top check's `expired` branch releases the slot.
            # Bounded and visible, never silent (review round 6, finding 1).
            released = False
            for _ in range(3):
                try:
                    released = bool(session_store.unconsume_approval(
                        cid, session_id, tenant_id))
                except Exception:  # noqa: BLE001
                    released = False
                if released:
                    break
            if not released:
                try:
                    emf_metrics.emit_approval_give_back_failed(
                        "policy_auto_confirm", confirmation_id=cid,
                        session_id=session_id)
                except Exception:  # noqa: BLE001
                    pass
                print(f"[leaf-agent] policy auto-confirm give-back FAILED for "
                      f"{cid!r} on session {session_id!r} — the row stays "
                      f"consumed until its TTL expires",
                      file=sys.stderr, flush=True)
            # PARK it, so a later terminal actually retries (see
            # _pending_policy). Returning the consume alone left the decision
            # with nothing to rediscover it.
            # NOTHING to add: the slot was RESERVED above and is still held —
            # a lost CAS simply means we do not release it. The old
            # unconditional append here could re-add an id whose reservation
            # had already been released on another path, pushing the list past
            # its cap (review round 5); the bound only holds if the reservation
            # is the ONE place the list grows.
            print(f"[leaf-agent] policy auto-confirm lost the CAS on "
                  f"session {session_id!r}; approval {cid!r} parked for the "
                  f"next terminal", file=sys.stderr, flush=True)
        except TurnRejected as exc:
            if exc.pre_harness:
                try:
                    session_store.unconsume_approval(cid, session_id, tenant_id)
                except Exception:  # noqa: BLE001
                    pass
            if exc.turn_id is not None:
                # No HTTP caller to answer: close the transcript the failed
                # start opened, the queue kicker's rule.
                try:
                    session_store.append_event(
                        session_id, exc.turn_id, "error",
                        {"error": {"error_code": exc.error_code,
                                   "message": exc.message},
                         "stop_reason": "error"})
                except Exception:  # noqa: BLE001
                    pass
            _forget_parked()  # a harness rejection is not retried in a loop
            print(f"[leaf-agent] policy auto-confirm start FAILED "
                  f"({exc.error_code}) session={session_id!r}",
                  file=sys.stderr, flush=True)
        return False
    except Exception as exc:  # noqa: BLE001
        try:
            print(f"[leaf-agent] policy auto-confirm candidate CRASHED "
                  f"({type(exc).__name__}) session={session_id!r}",
                  file=sys.stderr, flush=True)
        except Exception:  # noqa: BLE001
            pass
        return False


def _spawn_relay(tenant_id: str, session_id: str, turn_id: str,
                 resp: "requests.Response", max_s: float,
                 tier: Optional[str] = None,
                 entitlement_tier: Optional[str] = None) -> None:
    # ONE deadline, shared by both terminal paths (see _drain_terminal). The
    # watchdog's own wait is anchored here too, so neither path can drift from
    # the other.
    deadline = time.monotonic() + max_s
    terminal_lock = threading.Lock()
    terminal_flag = threading.Event()
    finished = threading.Event()
    proposals: Dict[str, Dict[str, Any]] = {}
    turn_usage: Dict[str, Any] = {}
    tools_called: List[str] = []

    def _end_once(event_type: Optional[str] = None, data: Optional[Dict[str, Any]] = None,
                  *, resolve: Optional[Any] = None) -> None:
        """Append (at most once, across BOTH threads) the terminal event — if
        one is given AND nobody has already terminalized this turn — then
        release the CAS. Idempotent: a second call is a harmless no-op.

        `resolve`, when given, is a zero-arg callable evaluated INSIDE the
        one-shot critical section, supplying (event_type, data). Every
        deadline-sensitive caller must use it rather than deciding first and
        calling second: between an outside-the-lock decision and the claim,
        the deciding thread can be descheduled, the other thread can win, and
        the terminal event the caller observes goes back to depending on the
        scheduler — the exact property this arbitration exists to remove."""
        with terminal_lock:
            if terminal_flag.is_set():
                return
            if resolve is not None:
                event_type, data = resolve()
            terminal_flag.set()
        _finalize_terminal(event_type, data)

    def _finalize_terminal(event_type: Optional[str],
                           data: Optional[Dict[str, Any]]) -> None:
        """Post-claim work: append the terminal event (when the caller has not
        already published it), meter, release the CAS.

        Split out of `_end_once` so a caller that must claim the terminal
        ATOMICALLY WITH ITS OWN APPEND can do both under one acquisition of
        `terminal_lock` and then run this. `_drain` needs exactly that: it
        publishes the harness's own `turn_complete`/`error`, and if the claim
        happened afterwards a cancel could slip a SECOND terminal event into
        the gap. Pass `event_type=None` to skip the append.
        """
        terminal_data = data or {}
        if event_type is not None:
            try:
                session_store.append_event(
                    session_id, turn_id, event_type, terminal_data)
            except Exception:  # noqa: BLE001  best-effort; CAS release below matters
                pass
        stop_reason = terminal_data.get("stop_reason")
        if not isinstance(stop_reason, str) or not stop_reason:
            stop_reason = "error"
        usage = dict(turn_usage)
        record: Dict[str, Any] = {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "cost_tokens": usage.get("cost_tokens", 0),
            "usd_est": usage.get("total_cost_usd", usage.get("usd_est", 0.0)),
            "tools_called": list(tools_called),
            "stop_reason": stop_reason,
        }
        for source, target in (
            ("input_tokens", "tokens_in"),
            ("output_tokens", "tokens_out"),
            ("cache_creation_input_tokens", "cache_creation_tokens"),
            ("cache_read_input_tokens", "cache_read_tokens"),
            ("model", "model"),
            ("grant_kind", "grant_kind"),
            ("degraded_mode", "degraded_mode"),
        ):
            if source in usage:
                record[target] = usage[source]
        # Best-effort by the existing agent_ledger contract. PostgreSQL mode
        # logs failures, never falls back to JSONL, and deduplicates this stable
        # tenant/session/turn identity.
        try:
            agent_ledger.append(record)
        except Exception as exc:  # noqa: BLE001
            # Defense in depth for injected/custom ledger implementations.
            # Never strand the active-turn CAS because metering failed.
            print(
                f"[leaf-agent] terminal metering failed: {type(exc).__name__}",
                file=sys.stderr, flush=True,
            )
        try:
            session_store.end_turn(session_id, turn_id)
        except Exception:  # noqa: BLE001
            pass
        # Policy auto-confirm runs FIRST (it continues the interaction the
        # user is already in); if it starts a confirm turn, the queue kicker
        # below sees busy and parks correctly. Both never raise.
        _auto_confirm_reads(tenant_id, session_id, proposals,
                            tier, entitlement_tier)
        # The session is free — hand it to the queued prompt, if one is
        # waiting. Runs on the terminalizing thread (drain, watchdog, or a
        # cancel); _kick_queued never raises, and start_turn's blocking span
        # here is only the connect + response headers (the new relay streams
        # on its own threads).
        _kick_queued(session_id)

    def _cancel() -> None:
        """Terminalize this turn as user-interrupted, then unblock the drain.

        Same two moves the watchdog makes on timeout: `_end_once` appends the
        terminal event and releases the CAS (idempotent across threads), and
        closing the response unblocks `iter_lines()` immediately instead of
        leaving the thread parked on the socket for up to another TURN_MAX_S.
        Closing also drops the HTTP connection to the harness, which is the
        signal that the client is gone.
        """
        _end_once("turn_complete", {"stop_reason": "interrupted"})
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass

    _register_canceller(turn_id, _cancel)

    def _drain() -> None:
        try:
            for raw_line in resp.iter_lines(decode_unicode=True):
                if terminal_flag.is_set():
                    break
                if not raw_line:
                    continue
                try:
                    ev = json.loads(raw_line)
                except (ValueError, TypeError):
                    continue  # malformed line — skip, don't kill the whole relay
                ev_type = ev.get("type") if isinstance(ev, dict) else None
                data = (ev.get("data") if isinstance(ev, dict) else None) or {}
                if not ev_type:
                    continue
                if ev_type == "turn_usage" and isinstance(data, dict):
                    turn_usage.clear()
                    turn_usage.update(data)
                elif ev_type == "tool_call":
                    tool = data.get("tool") if isinstance(data, dict) else None
                    if isinstance(tool, str) and tool and tool not in tools_called:
                        tools_called.append(tool)

                # Approval rows are created BEFORE the event that renders their
                # chip is published (`append_event` below IS publication — the
                # SSE relay serves straight from the store), so a visible chip
                # STRUCTURALLY implies a decidable row, not probabilistically
                # (review round 2 finding). A duplicate insert is the benign
                # pair/replay case; any OTHER store failure propagates to the
                # outer handler, which terminalizes the turn with an in-band
                # error instead of publishing an undecidable chip. Spine turns
                # (census #12 chip 1) emit proposed_run alone, so its branch
                # creates the row with proposal metadata; the legacy pair's
                # confirmation_required keeps kind/payload on the transcript
                # EVENT, its duplicate insert a no-op.
                if ev_type == "proposed_run":
                    cid = data.get("confirmation_id")
                    if cid:
                        proposals[cid] = data
                        try:
                            session_store.create_approval(
                                confirmation_id=cid, session_id=session_id,
                                tenant_id=tenant_id, turn_id=turn_id,
                                tool=data.get("tool"), params=data.get("params"),
                                capability=data.get("capability"),
                                rationale=data.get("rationale"),
                                # Reuse payload_json for the additive drawing
                                # binding. This avoids a schema migration while
                                # keeping the server-authored confirmation wire
                                # argument-exact with the app gate.
                                kind="run_capability",
                                payload=(
                                    {"dwg": data.get("dwg")}
                                    if isinstance(data.get("dwg"), str)
                                    else None
                                ),
                                ttl_s=approval_ttl_s(),
                            )
                        except sqlite3.IntegrityError:
                            pass  # row already exists (duplicate line / replay)
                elif ev_type == "confirmation_required":
                    cid = data.get("confirmation_id")
                    if cid:
                        proposal = proposals.get(cid, {})
                        try:
                            session_store.create_approval(
                                confirmation_id=cid, session_id=session_id, tenant_id=tenant_id,
                                turn_id=turn_id, tool=proposal.get("tool"),
                                params=proposal.get("params"), capability=proposal.get("capability"),
                                rationale=proposal.get("rationale"), kind=data.get("kind"),
                                payload=data.get("payload"), ttl_s=approval_ttl_s(),
                            )
                        except sqlite3.IntegrityError:
                            pass  # row created at proposed_run (pair flow)

                # relay the harness's own event verbatim — the ONLY place besides
                # `turn_started` that this module appends to the transcript.
                #
                # Under `terminal_lock`, and re-checking the flag: publishing a
                # relayed event and CLAIMING the terminal must be mutually
                # exclusive, or an event lands AFTER the terminal one. The
                # loop-top check is not enough — another thread can terminalize
                # between that check and this append, which is exactly what an
                # arbitrary-moment cancel does (the watchdog shares the race,
                # it just needs the deadline to land in the same gap). The
                # client stops at `turn_complete`, so a later event would be
                # invisible in the UI but present in the durable transcript.
                # `_end_once` appends OUTSIDE this lock, so there is no nesting
                # and no deadlock; it only ever sets the flag while holding it.
                #
                # A harness terminal event is CLAIMED in the same acquisition
                # that publishes it. Publishing first and claiming after left a
                # gap in which a cancel (or the watchdog) could claim and append
                # a SECOND terminal event, so the transcript ended twice.
                terminal_data: Optional[Dict[str, Any]] = None
                with terminal_lock:
                    if terminal_flag.is_set():
                        break
                    session_store.append_event(session_id, turn_id, ev_type, data)
                    if ev_type in ("turn_complete", "error"):
                        terminal_data = (
                            data if ev_type == "turn_complete"
                            else dict(data, stop_reason="error")
                        )
                        terminal_flag.set()  # claimed atomically with the append above

                if terminal_data is not None:
                    # event_type=None: already published inside the lock.
                    _finalize_terminal(None, terminal_data)
                    break
        except Exception as exc:  # noqa: BLE001  network drop / decode failure mid-stream
            # Past the shared deadline this is turn EXPIRY, not a transport
            # fault, and must read identically to the watchdog's own terminal
            # event — otherwise which one the caller sees is a coin flip.
            # Decided INSIDE the one-shot lock (see `resolve`), never before it.
            _end_once(resolve=lambda: _drain_terminal(deadline, exc))
        finally:
            # A stream that simply ENDED without a terminal event gets the same
            # treatment: past the deadline it is expiry and must read like the
            # watchdog's, or a clean EOF landing just after the deadline would
            # terminalize the turn with NO event at all when the drain wins and
            # `turn_complete{timeout}` when the watchdog wins — leaving the CAS
            # released but the client still showing the turn in flight.
            _end_once(resolve=lambda: _eof_terminal(deadline))
            _drop_canceller(turn_id)
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
            finished.set()

    def _watchdog() -> None:
        # Wait to the SHARED deadline, not `max_s` from whenever this thread
        # happened to be scheduled — the drain thread is started first, so
        # anchoring here would let watchdog expiry drift later under load by
        # exactly the thread-start delay.
        if finished.wait(timeout=max(0.0, deadline - time.monotonic())):
            return  # the drain thread already terminalized this turn
        _end_once("turn_complete", {"stop_reason": "timeout"})
        # The drain thread is very likely still blocked inside
        # `resp.iter_lines()` waiting on the socket (the harness never sent a
        # terminal event, which is exactly why we're here) — its own
        # read-timeout wouldn't fire for up to another TURN_MAX_S, leaking a
        # thread + connection for that whole extra window even though the
        # turn has already been terminalized above. Closing the response here
        # unblocks `iter_lines()` immediately (it raises inside `_drain`,
        # which is swallowed by its own `except Exception` and is a no-op
        # against `_end_once` since `terminal_flag` is already set).
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass

    # If thread creation itself fails (RuntimeError: can't start new thread —
    # thread exhaustion), `_drain`'s `finally` never runs, so the canceller and
    # the CAS would both leak and the session would wedge until the stale-turn
    # window. Terminalize here instead: the turn genuinely cannot proceed.
    try:
        threading.Thread(target=_drain, daemon=True, name=f"turn-drain-{turn_id[:8]}").start()
        threading.Thread(target=_watchdog, daemon=True, name=f"turn-watchdog-{turn_id[:8]}").start()
    except Exception as exc:  # noqa: BLE001
        _drop_canceller(turn_id)
        _end_once("error", {"error": {"error_code": ErrorCode.INTERNAL,
                                      "message": f"relay start failed: {type(exc).__name__}"}})
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass
        raise
