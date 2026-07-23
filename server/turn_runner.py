"""
Turn engine (sessions wire spec, gap audit §2.1 / CONTRACT-ADDENDUM sessions
addendum) — Turn-engine API v1 (FROZEN):

    class TurnBusy(Exception)
    class TurnRejected(Exception): status_code, error_code, message, extra
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
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

import requests

import broker_client
import session_store
from envelopes import ErrorCode

# --------------------------------------------------------------------------- #
# env knobs (read at call time, never at import time, so tests can override
# via monkeypatch/env without needing to re-import this module — same posture
# as jobs.py's job_max_s()/heartbeat_stale_s()).
# --------------------------------------------------------------------------- #
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
class TurnBusy(Exception):
    """try_begin_turn lost the CAS — a turn is already active for this session."""


class TurnRejected(Exception):
    """The harness (or this engine, pre-flight) rejected the turn before it
    could stream. `extra` is merged top-level into the router's error body
    (e.g. {'grant_required': True})."""

    def __init__(self, status_code: int, error_code: str, message: str,
                 extra: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.extra = extra or {}


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
               classifier_hint: Optional[Dict[str, Any]] = None) -> str:
    # In live auth this is a deps.TenantContext, a str subclass carrying the
    # verified claim. Snapshot the claim before normalizing to the frozen
    # string tenant_id used on the harness wire. Off-auth callers are plain
    # strings and retain the existing broker/demo fallback.
    tier = getattr(tenant_id, "tier", None)
    tenant_id = str(tenant_id)
    sess = _require_session(tenant_id, session_id)

    turn_id = str(uuid.uuid4())
    max_s = turn_max_s()
    if not session_store.try_begin_turn(session_id, turn_id, max_s, tier=tier):
        raise TurnBusy(f"session {session_id!r} already has an active turn")

    # The durable transcript source: whatever drove this turn (a fresh user
    # message OR the resume of a halted turn), plus the optional dispatcher
    # hint — classifier_hint is recorded here for the durable log ONLY, it is
    # NOT part of ConverseTurnInput (frozen shape has no such field).
    user_data: Dict[str, Any] = {}
    if text is not None:
        user_data["text"] = text
    if confirm is not None:
        user_data["confirm"] = confirm
    if classifier_hint is not None:
        user_data["classifier_hint"] = classifier_hint
    session_store.append_event(session_id, turn_id, "turn_started", user_data)

    harness_url = _harness_url()
    if not harness_url:
        session_store.end_turn(session_id, turn_id)
        raise TurnRejected(502, ErrorCode.BROKER_UNREACHABLE,
                           "neither LEAF_CONVERSE_HARNESS_URL nor "
                           "LEAF_AUTHOR_HARNESS_URL is configured")

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

    try:
        resp = requests.post(f"{harness_url}/turn", json=payload,
                             headers=broker_client.harness_headers(),
                             stream=True, timeout=(5, max_s))
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        session_store.end_turn(session_id, turn_id)
        raise TurnRejected(502, ErrorCode.BROKER_UNREACHABLE,
                           f"harness at {harness_url} unreachable: {exc}") from exc
    except requests.exceptions.RequestException as exc:  # noqa: BLE001
        session_store.end_turn(session_id, turn_id)
        raise TurnRejected(502, ErrorCode.BROKER_UNREACHABLE,
                           f"harness request failed: {exc}") from exc

    if resp.status_code == 401:
        session_store.end_turn(session_id, turn_id)
        body = _safe_json(resp) or {}
        msg = (body.get("message") or (body.get("error") or {}).get("message")
              or f"tenant {tenant_id!r} has no linked Claude grant")
        resp.close()
        raise TurnRejected(401, ErrorCode.GRANT_REQUIRED, msg, extra={"grant_required": True})

    if resp.status_code == 429:
        session_store.end_turn(session_id, turn_id)
        body = _safe_json(resp) or {}
        code = body.get("errorCode") or body.get("error_code")
        if code not in (ErrorCode.LLM_QUOTA_EXHAUSTED, ErrorCode.LLM_RATE_LIMITED):
            code = ErrorCode.LLM_RATE_LIMITED
        msg = body.get("message") or "harness reported a rate/quota limit"
        resp.close()
        raise TurnRejected(429, code, msg)

    if resp.status_code >= 400:
        session_store.end_turn(session_id, turn_id)
        body = _safe_json(resp) or {}
        msg = body.get("message") or f"harness returned HTTP {resp.status_code}"
        resp.close()
        raise TurnRejected(502, ErrorCode.BROKER_UNREACHABLE, msg)

    _spawn_relay(tenant_id, session_id, turn_id, resp, max_s)
    return turn_id


# --------------------------------------------------------------------------- #
# streaming relay (daemon thread) + watchdog (daemon thread)
# --------------------------------------------------------------------------- #
def _spawn_relay(tenant_id: str, session_id: str, turn_id: str,
                 resp: "requests.Response", max_s: float) -> None:
    terminal_lock = threading.Lock()
    terminal_flag = threading.Event()
    finished = threading.Event()
    proposals: Dict[str, Dict[str, Any]] = {}

    def _end_once(event_type: Optional[str] = None, data: Optional[Dict[str, Any]] = None) -> None:
        """Append (at most once, across BOTH threads) the terminal event — if
        one is given AND nobody has already terminalized this turn — then
        release the CAS. Idempotent: a second call is a harmless no-op."""
        with terminal_lock:
            if terminal_flag.is_set():
                return
            terminal_flag.set()
            if event_type is not None:
                try:
                    session_store.append_event(session_id, turn_id, event_type, data or {})
                except Exception:  # noqa: BLE001  best-effort — CAS release below is what matters
                    pass
            try:
                session_store.end_turn(session_id, turn_id)
            except Exception:  # noqa: BLE001
                pass

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
                                kind="run_capability", payload=None,
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
                session_store.append_event(session_id, turn_id, ev_type, data)

                if ev_type in ("turn_complete", "error"):
                    _end_once()  # already relayed above — just release the CAS
                    break
        except Exception as exc:  # noqa: BLE001  network drop / decode failure mid-stream
            _end_once("error", {"error": {"error_code": ErrorCode.INTERNAL,
                                          "message": f"{type(exc).__name__}: {exc}"}})
        finally:
            _end_once()  # stream ended with no terminal event seen -> still release the CAS
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
            finished.set()

    def _watchdog() -> None:
        if finished.wait(timeout=max_s):
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

    threading.Thread(target=_drain, daemon=True, name=f"turn-drain-{turn_id[:8]}").start()
    threading.Thread(target=_watchdog, daemon=True, name=f"turn-watchdog-{turn_id[:8]}").start()
