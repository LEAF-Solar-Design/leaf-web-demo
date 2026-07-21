"""Conversational agent sessions (§18) — app-side proxy to the harness ConverseLoop.

    POST   /api/sessions                    -> create/attach (idempotent per tenant+drawing)
    GET    /api/sessions?drawing_id=        -> own-tenant session list
    POST   /api/sessions/{id}/messages      -> start a turn (202 {turn_id}); assembles the
                                               ContextPacket (server/context_packet.py)
    GET    /api/sessions/{id}/stream        -> SSE relay of the harness event stream
    GET    /api/sessions/{id}/transcript    -> durable transcript passthrough
    DELETE /api/sessions/{id}               -> archive

The app stays LLM-free: every route forwards to the harness /converse/* surface
(LEAF_CONVERSE_HARNESS_URL, X-Harness-Secret — the same F5 mechanism author.py
uses) and maps harness outcomes onto §10 envelopes:

    harness 409                -> TURN_IN_PROGRESS (409)
    harness 401 grant_required -> GRANT_REQUIRED (401)
    harness 404                -> SESSION_NOT_FOUND (404; also cross-tenant — no oracle)
    harness 429                -> retry-after horizon > QUOTA_HORIZON_S => LLM_QUOTA_EXHAUSTED
                                  (degraded_mode true), else LLM_RATE_LIMITED
    connection failure         -> BROKER_UNREACHABLE (502, degraded_mode true)

PHASE-1 SCOPE NOTES
  * The stream relay opens ONE upstream harness connection PER browser client.
    Fanning N clients out from a single upstream stream (multi-tab dedupe) is a
    documented Phase-2 optimization; correctness is identical because the harness
    replays from sessions.db via after_seq.
  * The (session -> drawing) map lives in-process, populated by the idempotent
    create. Clients re-attach (POST /api/sessions) after an app restart — the
    documented §18 attach flow — which re-warms the map; a message on a session
    this process has never seen still forwards, with an empty drawing digest in
    its ContextPacket.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set, Tuple

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

import agent_ledger
import context_packet
import deps
import entitlements
from envelopes import (
    DEFAULT_HTTP_STATUS,
    ErrorCode,
    err_envelope,
    error_response,
    with_envelope_fields,
)

router = APIRouter()

# 429 disambiguation (wire contract §2; research/agentsdk-usage-visibility.md):
# a reset horizon beyond this is subscription/quota exhaustion (degraded until it
# resets), anything shorter is an ordinary rate limit the client may retry through.
QUOTA_HORIZON_S = 300.0

MAX_MESSAGE_TEXT = 8_000  # same bound as the §12 prompt route / author description
# classifier_hint is a small §12 classifier projection ({lane, tool, confidence,
# rationale}); bounding its SERIALIZED size keeps a client-supplied dict from
# blowing the ContextPacket budget (context_packet.MAX_PACKET_CHARS) on its own.
MAX_HINT_CHARS = 1_024


class SessionCreateRequest(BaseModel):
    drawing_id: str = Field(..., min_length=1, max_length=200)
    project_id: Optional[str] = Field(default=None, max_length=200)


class MessageRequest(BaseModel):
    text: Optional[str] = Field(default=None, max_length=MAX_MESSAGE_TEXT)
    confirm: Optional[Dict[str, Any]] = None
    classifier_hint: Optional[Dict[str, Any]] = None


# In-process (session_id -> {tenant_id, drawing_id, status, created_at}) map.
# See the Phase-1 scope note above: re-warmed by the idempotent create/attach.
_SESSIONS: Dict[str, Dict[str, Any]] = {}


def _harness_url() -> str:
    return os.environ.get("LEAF_CONVERSE_HARNESS_URL", "").rstrip("/")


def _unreachable(detail: str) -> JSONResponse:
    """Connection-level failure -> BROKER_UNREACHABLE pattern (502), degraded_mode
    true: the deterministic §12 path is now the whole product for this tenant."""
    body = err_envelope(ErrorCode.BROKER_UNREACHABLE,
                        f"converse harness unreachable: {detail}", retryable=True)
    body["degraded_mode"] = True
    return JSONResponse(status_code=DEFAULT_HTTP_STATUS[ErrorCode.BROKER_UNREACHABLE],
                        content=body)


def _session_not_found(session_id: str) -> JSONResponse:
    # Same wording for unknown AND cross-tenant — no existence oracle (wire §1).
    return error_response(ErrorCode.SESSION_NOT_FOUND,
                          f"unknown session: {session_id}", retryable=False)


def _retry_after_s(resp: Any) -> float:
    """Best-effort reset horizon from a harness 429 (Retry-After header first,
    then common body keys). Unparseable -> 0 (short horizon => rate-limited)."""
    raw = None
    try:
        raw = (resp.headers or {}).get("Retry-After")
    except Exception:  # noqa: BLE001
        raw = None
    if raw is None:
        try:
            jb = resp.json()
            if isinstance(jb, dict):
                raw = jb.get("retry_after") or jb.get("retry_after_s") or jb.get("retryAfterS")
        except Exception:  # noqa: BLE001
            raw = None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _map_429(resp: Any) -> JSONResponse:
    horizon = _retry_after_s(resp)
    if horizon > QUOTA_HORIZON_S:
        body = err_envelope(
            ErrorCode.LLM_QUOTA_EXHAUSTED,
            f"LLM quota exhausted (resets in ~{int(horizon)}s) — "
            "the deterministic prompt router keeps working meanwhile.",
            retryable=True)
        body["degraded_mode"] = True  # long horizon => conversational lane is DOWN
        return JSONResponse(status_code=429, content=body)
    return error_response(ErrorCode.LLM_RATE_LIMITED,
                          "LLM rate-limited — retry shortly.", retryable=True,
                          status_code=429)


def _harness_request(method: str, path: str, **kwargs: Any):
    """One harness HTTP call (same env/secret mechanism as author.py:82-123).
    Returns the response, or raises the connection exception for the caller."""
    import broker_client
    import requests
    return requests.request(method, f"{_harness_url()}{path}",
                            headers=broker_client.harness_headers(),
                            timeout=kwargs.pop("timeout", 30), **kwargs)


# --------------------------------------------------------------------------- #
# create / list
# --------------------------------------------------------------------------- #
@router.post("/api/sessions")
def create_session(req: SessionCreateRequest, tenant=Depends(deps.require_tenant)):
    """Create-or-attach the tenant's session for one drawing (idempotent — the
    harness keys sessions by (tenant, drawing)). Requires the `converse`
    entitlement, enforced FIRST so a plan without it never reaches the harness."""
    tier = entitlements.resolve_tier(tenant)
    if not entitlements.entitlements_for(tier).get("converse", False):
        return entitlements.entitlement_denied_response("converse", tier)

    if not _harness_url():
        return _unreachable("LEAF_CONVERSE_HARNESS_URL not configured")
    try:
        resp = _harness_request("POST", "/converse/sessions",
                                json={"tenantId": str(tenant), "drawingId": req.drawing_id})
    except Exception as exc:  # noqa: BLE001 - connection/timeout/etc.
        return _unreachable(str(exc))
    # Create has exactly ONE success shape (wire §1: 200). Anything else — most
    # importantly a 401 from a harness-secret mismatch — is an upstream failure,
    # not a session: reporting it as 200 {session_id: null} is a silent dead end.
    if resp.status_code != 200:
        return _unreachable(f"harness returned HTTP {resp.status_code}")
    try:
        hj = resp.json()
    except ValueError:
        return _unreachable("harness returned non-JSON")

    session_id = hj.get("sessionId")
    row = {
        "session_id": session_id,
        "tenant_id": str(tenant),
        "drawing_id": req.drawing_id,
        "status": hj.get("status", "idle"),
        "created_at": hj.get("createdAt") or datetime.now(timezone.utc).isoformat(),
    }
    if session_id:
        _SESSIONS[str(session_id)] = row
    return deps.tenant_echo(with_envelope_fields({
        "session_id": session_id,
        "status": row["status"],
        "created_at": row["created_at"],
    }), tenant)


@router.get("/api/sessions")
def list_sessions(drawing_id: Optional[str] = None, tenant=Depends(deps.require_tenant)):
    """Own-tenant sessions this app process has attached (see the Phase-1 scope
    note in the module docstring — the durable list lives harness-side in
    sessions.db; the pinned /converse/* surface exposes no list route yet)."""
    rows = [r for r in _SESSIONS.values() if r["tenant_id"] == str(tenant)]
    if drawing_id:
        rows = [r for r in rows if r["drawing_id"] == drawing_id]
    view = [{"session_id": r["session_id"], "drawing_id": r["drawing_id"],
             "status": r["status"], "created_at": r["created_at"]} for r in rows]
    return deps.tenant_echo(with_envelope_fields({"sessions": view}), tenant)


# --------------------------------------------------------------------------- #
# messages (turn start)
# --------------------------------------------------------------------------- #
@router.post("/api/sessions/{session_id}/messages")
def post_message(session_id: str, req: MessageRequest, tenant=Depends(deps.require_tenant)):
    """Start a turn: assemble the ContextPacket and forward. Exactly one of
    `text` / `confirm` is required (wire contract §1)."""
    if (req.text is None) == (req.confirm is None):
        return error_response(ErrorCode.BAD_PARAMS,
                              "exactly one of 'text' or 'confirm' is required",
                              retryable=False, status_code=400)
    if req.classifier_hint is not None:
        hint_len = len(json.dumps(req.classifier_hint, separators=(",", ":"), default=str))
        if hint_len > MAX_HINT_CHARS:
            return error_response(
                ErrorCode.BAD_PARAMS,
                f"classifier_hint too large ({hint_len} chars; max {MAX_HINT_CHARS})",
                retryable=False, status_code=400)
    if not _harness_url():
        return _unreachable("LEAF_CONVERSE_HARNESS_URL not configured")

    known = _SESSIONS.get(session_id)
    if known is not None and known["tenant_id"] != str(tenant):
        return _session_not_found(session_id)  # cross-tenant: 404, no oracle
    drawing_id = known["drawing_id"] if known else ""

    try:
        packet = context_packet.build_packet(tenant, drawing_id,
                                             classifier_hint=req.classifier_hint)
    except ValueError as exc:  # oversized non-catalog field — loud 400, never a 500
        return error_response(ErrorCode.BAD_PARAMS, str(exc),
                              retryable=False, status_code=400)
    payload: Dict[str, Any] = {"tenantId": str(tenant), "contextPacket": packet}
    if req.text is not None:
        payload["text"] = req.text
    else:
        payload["confirm"] = req.confirm
    if req.classifier_hint is not None:
        payload["classifierHint"] = req.classifier_hint

    try:
        resp = _harness_request("POST", f"/converse/sessions/{session_id}/messages",
                                json=payload)
    except Exception as exc:  # noqa: BLE001
        return _unreachable(str(exc))

    if resp.status_code == 429:
        return _map_429(resp)
    if resp.status_code >= 500:
        return _unreachable(f"harness returned HTTP {resp.status_code}")
    # Harness CLIENT errors are the caller's problem, not an outage — mapping
    # them through _unreachable would flash the degraded banner over a bad
    # message or a stale confirmation chip.
    if resp.status_code == 400:  # BadMessageError (malformed text/confirm)
        detail = ""
        try:
            jb = resp.json()
            if isinstance(jb, dict):
                detail = str(jb.get("message") or jb.get("error") or "")
        except ValueError:
            pass
        return error_response(ErrorCode.BAD_PARAMS,
                              f"harness rejected the message: {detail or 'bad message'}",
                              retryable=False, status_code=400)
    if resp.status_code == 410:  # ConfirmationInvalidError (expired approval)
        return error_response(ErrorCode.BAD_PARAMS,
                              "confirmation expired — ask the assistant to re-propose",
                              retryable=False, status_code=410)
    try:
        hj = resp.json()
    except ValueError:
        return _unreachable("harness returned non-JSON")

    if resp.status_code == 409:
        body = err_envelope(ErrorCode.TURN_IN_PROGRESS,
                            "a turn is already in flight for this session",
                            retryable=True)
        body["turn_id"] = hj.get("turnId")  # additive: the blocking turn
        return JSONResponse(status_code=409, content=body)
    if resp.status_code == 401:
        # Missing per-tenant Claude grant — same §16 shape author.py returns.
        body = err_envelope(
            ErrorCode.GRANT_REQUIRED,
            f"tenant {str(tenant)!r} has no linked Claude grant — "
            "sign in with Claude to converse.",
            retryable=False)
        body["grant_required"] = True
        return JSONResponse(status_code=401, content=body)
    if resp.status_code == 404:
        return _session_not_found(session_id)
    if resp.status_code != 202:
        return _unreachable(f"harness returned HTTP {resp.status_code}")

    return JSONResponse(status_code=202, content=deps.tenant_echo(
        with_envelope_fields({"turn_id": hj.get("turnId"), "status": "started"}), tenant))


# --------------------------------------------------------------------------- #
# turn metering — app-side agent-ledger writes off the relayed event stream
# --------------------------------------------------------------------------- #
# One ledger line per (session, turn) even across reconnects/multi-tab: the
# harness replays history via after_seq, so every relay re-observes old
# turn_complete events — this process-wide set dedupes them. Best-effort by
# contract (a restart may re-append a replayed turn; agent_ledger tolerates it).
_METERED_TURNS: Set[Tuple[str, str]] = set()
_METER_LOCK = threading.Lock()
_METER_KEEP = 4096  # hard bound; wholesale clear beats unbounded growth


class _TurnMeter:
    """Observes the relayed SSE bytes (never modifies them) and appends one
    agent_ledger turn line per turn_complete, joined with the turn's preceding
    turn_usage event (§3). This is the app-side writer the /api/usage `agent`
    block and /api/ops/agent/tenants aggregate — without it the metering
    surface only ever reports zeros."""

    def __init__(self, tenant_id: str, session_id: str):
        self.tenant_id = tenant_id
        self.session_id = session_id
        self._buffer = b""
        self._usage_by_turn: Dict[str, Dict[str, Any]] = {}

    def feed(self, chunk: bytes) -> None:
        try:
            self._buffer += chunk
            while b"\n\n" in self._buffer:
                frame, self._buffer = self._buffer.split(b"\n\n", 1)
                self._frame(frame)
            if len(self._buffer) > 65536:
                self._buffer = b""  # runaway unterminated frame: drop, keep relaying
        except Exception:  # noqa: BLE001 - metering must never break the relay
            pass

    def _frame(self, frame: bytes) -> None:
        for line in frame.splitlines():
            if not line.startswith(b"data: "):
                continue
            try:
                ev = json.loads(line[len(b"data: "):].decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(ev, dict):
                continue
            turn_id = str(ev.get("turn_id") or "")
            data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
            if ev.get("type") == "turn_usage":
                self._usage_by_turn[turn_id] = data
            elif ev.get("type") == "turn_complete":
                self._complete(turn_id, data)

    def _complete(self, turn_id: str, data: Dict[str, Any]) -> None:
        key = (self.session_id, turn_id)
        with _METER_LOCK:
            if key in _METERED_TURNS:
                return
            if len(_METERED_TURNS) >= _METER_KEEP:
                _METERED_TURNS.clear()
            _METERED_TURNS.add(key)
        usage = self._usage_by_turn.pop(turn_id, {})
        agent_ledger.append({
            "tenant_id": self.tenant_id,
            "session_id": self.session_id,
            "turn_id": turn_id,
            "turn": usage.get("turns"),
            "tokens_in": usage.get("input_tokens"),
            "tokens_out": usage.get("output_tokens"),
            "cache_creation_tokens": usage.get("cache_creation_tokens"),
            "cache_read_tokens": usage.get("cache_read_tokens"),
            "cost_tokens": usage.get("cost_tokens") or 0,
            "usd_est": usage.get("total_cost_usd") or 0.0,
            "stop_reason": data.get("stop_reason"),
        })


# --------------------------------------------------------------------------- #
# stream (SSE relay)
# --------------------------------------------------------------------------- #
@router.get("/api/sessions/{session_id}/stream")
async def stream_session(session_id: str, after_seq: int = 0,
                         tenant=Depends(deps.require_tenant)):
    """SSE relay: verbatim byte passthrough of the harness event stream (§3
    envelopes are produced harness-side; the app adds nothing). `after_seq`
    passes through as `afterSeq`, so reconnecting clients replay from the
    durable transcript. One upstream connection per client (Phase-1; see
    module docstring)."""
    if not _harness_url():
        return _unreachable("LEAF_CONVERSE_HARNESS_URL not configured")
    known = _SESSIONS.get(session_id)
    if known is not None and known["tenant_id"] != str(tenant):
        return _session_not_found(session_id)

    def _open():
        return _harness_request(
            "GET", f"/converse/sessions/{session_id}/stream",
            params={"afterSeq": int(after_seq), "tenantId": str(tenant)},
            stream=True, timeout=(10, None))  # no read timeout: SSE is long-lived

    try:
        upstream = await asyncio.to_thread(_open)
    except Exception as exc:  # noqa: BLE001
        return _unreachable(str(exc))
    if upstream.status_code == 404:
        upstream.close()
        return _session_not_found(session_id)
    if upstream.status_code != 200:
        upstream.close()
        return _unreachable(f"harness returned HTTP {upstream.status_code}")

    # Dedicated pump thread -> asyncio queue: the blocking requests iterator
    # never occupies AnyIO's shared threadpool for the stream's lifetime (the
    # B1 lesson from routers/jobs.py). A dead client stops draining the queue;
    # the bounded put then times out and the pump closes the upstream socket.
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    loop = asyncio.get_running_loop()
    done = object()
    meter = _TurnMeter(str(tenant), session_id)

    def _pump() -> None:
        try:
            for chunk in upstream.iter_content(chunk_size=None):
                if not chunk:
                    continue
                meter.feed(chunk)  # metering observer; relay bytes untouched
                asyncio.run_coroutine_threadsafe(queue.put(chunk), loop).result(timeout=30)
        except Exception:  # noqa: BLE001 - upstream drop / dead client / loop closed
            pass
        finally:
            upstream.close()
            try:
                asyncio.run_coroutine_threadsafe(queue.put(done), loop).result(timeout=5)
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=_pump, daemon=True,
                     name=f"sse-relay-{session_id[:12]}").start()

    async def relay():
        while True:
            item = await queue.get()
            if item is done:
                return
            yield item

    return StreamingResponse(relay(), media_type="text/event-stream")


# --------------------------------------------------------------------------- #
# transcript / archive
# --------------------------------------------------------------------------- #
@router.get("/api/sessions/{session_id}/transcript")
def get_transcript(session_id: str, limit: int = 200,
                   tenant=Depends(deps.require_tenant)):
    if not _harness_url():
        return _unreachable("LEAF_CONVERSE_HARNESS_URL not configured")
    known = _SESSIONS.get(session_id)
    if known is not None and known["tenant_id"] != str(tenant):
        return _session_not_found(session_id)
    try:
        resp = _harness_request("GET", f"/converse/sessions/{session_id}/transcript",
                                params={"limit": int(limit), "tenantId": str(tenant)})
    except Exception as exc:  # noqa: BLE001
        return _unreachable(str(exc))
    if resp.status_code == 404:
        return _session_not_found(session_id)
    # 200/404 are the only expected outcomes (wire §1); an unexpected code (e.g.
    # 401 on a secret mismatch) must not surface as an empty-but-successful
    # transcript.
    if resp.status_code != 200:
        return _unreachable(f"harness returned HTTP {resp.status_code}")
    try:
        hj = resp.json()
    except ValueError:
        return _unreachable("harness returned non-JSON")
    return deps.tenant_echo(
        with_envelope_fields({"events": hj.get("events", [])}), tenant)


@router.delete("/api/sessions/{session_id}")
def archive_session(session_id: str, tenant=Depends(deps.require_tenant)):
    if not _harness_url():
        return _unreachable("LEAF_CONVERSE_HARNESS_URL not configured")
    known = _SESSIONS.get(session_id)
    if known is not None and known["tenant_id"] != str(tenant):
        return _session_not_found(session_id)
    try:
        resp = _harness_request("DELETE", f"/converse/sessions/{session_id}",
                                params={"tenantId": str(tenant)})
    except Exception as exc:  # noqa: BLE001
        return _unreachable(str(exc))
    if resp.status_code == 404:
        return _session_not_found(session_id)
    # Same rule as transcript: only 200/404 are expected, and claiming
    # {archived: true} on an unexpected 4xx would be a lie the client acts on.
    if resp.status_code != 200:
        return _unreachable(f"harness returned HTTP {resp.status_code}")
    _SESSIONS.pop(session_id, None)
    return deps.tenant_echo(with_envelope_fields({"archived": True}), tenant)
