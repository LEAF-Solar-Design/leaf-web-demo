"""
Binary acceptance for server/turn_runner.py (S3 lane — Turn-engine API v1).

Spins a stdlib http.server stub that speaks real HTTP/1.1 chunked
`application/x-ndjson` (so the streaming relay is exercised against an actual
socket, not a mock), and asserts:

  (a) a normal streamed turn lands every relayed event with monotonic seq,
      and the CAS is released (active_turn_id back to None) on turn_complete.
  (b) a `confirmation_required` event (paired with the preceding
      `proposed_run` by confirmation_id) creates an `approvals` row whose TTL
      matches SESSIONS_APPROVAL_TTL_S.
  (c) an immediate 401 (grant_required), 429 (llm_quota_exhausted /
      llm_rate_limited), and connection-refused all raise TurnRejected with
      the right (status_code, error_code) and release the CAS.
  (d) try_begin_turn loss -> TurnBusy, with NO HTTP call made.
  (e) an unknown session_id -> TurnRejected(404, session_not_found).
  (f) a stub that streams data but never sends turn_complete -> the
      TURN_MAX_S watchdog appends turn_complete{stop_reason:'timeout'} and
      releases the CAS — deterministically BEFORE the request-level read
      timeout could ever fire (proven by construction: the watchdog's
      deadline is anchored at turn-START, the read-timeout's deadline resets
      on every chunk received, so as long as at least one chunk arrives after
      t=0 the read-timeout deadline is strictly later).

Run:  cd server && python -m pytest tests/test_turn_runner.py -q
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

# route the sessions SQLite DB to a throwaway dir BEFORE session_store is
# imported anywhere (module reads SESSIONS_DB once, at import time — same
# posture as JOBS_DB; see tests/test_session_store.py).
os.environ.setdefault("SESSIONS_DB", str(Path(tempfile.mkdtemp(prefix="turnrunner-sessions-")) / "sessions.db"))

import session_store  # noqa: E402
import turn_runner  # noqa: E402
from envelopes import ErrorCode  # noqa: E402


# =========================================================================== #
# stub harness — real HTTP/1.1 chunked application/x-ndjson streaming
# =========================================================================== #
class _TurnStub(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # class-level script config, reset per-fixture
    IMMEDIATE_STATUS: Optional[int] = None     # non-stream short-circuit (401/429/etc.)
    IMMEDIATE_BODY: Dict[str, Any] = {}
    SCRIPT: List[Dict[str, Any]] = []          # list of {"type","data"} NDJSON events
    CHUNK_DELAY_S: float = 0.0                 # sleep between chunks (proves incremental streaming)
    SEND_TERMINATOR: bool = True               # False -> never send the final 0-chunk (hang)
    LAST_BODY: Optional[Dict[str, Any]] = None

    def _write_chunk(self, raw: bytes) -> None:
        self.wfile.write(f"{len(raw):x}\r\n".encode("ascii") + raw + b"\r\n")
        self.wfile.flush()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", 0) or 0)
        body = self.rfile.read(length)
        try:
            type(self).LAST_BODY = json.loads(body or b"{}")
        except Exception:
            type(self).LAST_BODY = None

        cls = type(self)
        if cls.IMMEDIATE_STATUS is not None:
            raw = json.dumps(cls.IMMEDIATE_BODY).encode("utf-8")
            self.send_response(cls.IMMEDIATE_STATUS)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for ev in cls.SCRIPT:
            line = (json.dumps(ev) + "\n").encode("utf-8")
            self._write_chunk(line)
            if cls.CHUNK_DELAY_S:
                time.sleep(cls.CHUNK_DELAY_S)
        if cls.SEND_TERMINATOR:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        # else: deliberately hang — never send the terminating 0-chunk.

    def log_message(self, *a):  # silence
        return


@pytest.fixture
def turn_stub():
    _TurnStub.IMMEDIATE_STATUS = None
    _TurnStub.IMMEDIATE_BODY = {}
    _TurnStub.SCRIPT = []
    _TurnStub.CHUNK_DELAY_S = 0.0
    _TurnStub.SEND_TERMINATOR = True
    _TurnStub.LAST_BODY = None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TurnStub)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}", _TurnStub
    finally:
        srv.shutdown()


def _free_closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _new_session(tenant_id: str = "tenant-a", drawing_id: Optional[str] = None) -> Dict[str, Any]:
    drawing_id = drawing_id or f"dwg-{threading.get_ident()}-{time.time()}"
    return session_store.get_or_create_session(tenant_id, drawing_id)


def _wait_until(predicate, timeout_s: float = 3.0, poll_s: float = 0.02):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return predicate()


# =========================================================================== #
# (a) happy path — monotonic seq, CAS released on turn_complete
# =========================================================================== #
def test_streamed_turn_lands_events_with_monotonic_seq_and_releases_cas(monkeypatch, turn_stub):
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")

    stub.SCRIPT = [
        {"type": "text_delta", "data": {"text": "Hello"}},
        {"type": "text_delta", "data": {"text": " world"}},
        {"type": "tool_call", "data": {"tool": "panel_count", "args_summary": "{}"}},
        {"type": "tool_result", "data": {"tool": "panel_count", "ok": True, "summary": "42 panels"}},
        {"type": "turn_usage", "data": {"cost_tokens": 123}},
        {"type": "turn_complete", "data": {"stop_reason": "end_turn"}},
    ]
    stub.CHUNK_DELAY_S = 0.02

    sess = _new_session("tenant-mono")
    session_id = sess["session_id"]

    turn_id = turn_runner.start_turn("tenant-mono", session_id, text="how many panels?")
    assert turn_id

    ok = _wait_until(lambda: session_store.get_session(session_id)["active_turn_id"] is None)
    assert ok, "CAS was never released after turn_complete"

    events = session_store.recent_events(session_id, 100)
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and seqs == list(range(1, len(seqs) + 1)), seqs

    types = [e["type"] for e in events]
    assert types[0] == "turn_started"
    assert types[-1] == "turn_complete"
    assert "text_delta" in types and "tool_call" in types and "tool_result" in types

    assert events[0]["data"] == {"text": "how many panels?"}
    assert all(e["turn_id"] == turn_id for e in events)

    # the exact ConverseTurnInput the harness received
    body = stub.LAST_BODY
    assert body["tenant_id"] == "tenant-mono"
    assert body["session_id"] == session_id
    assert body["turn_id"] == turn_id
    assert body["drawing_id"] == sess["drawing_id"]
    assert body["text"] == "how many panels?"
    assert body["messages"] == []
    assert "confirm" not in body


# =========================================================================== #
# (b) confirmation_required -> approvals row with the right TTL, sourced
#     from the preceding proposed_run
# =========================================================================== #
def test_confirmation_required_creates_approval_row_with_ttl(monkeypatch, turn_stub):
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")
    monkeypatch.setenv("SESSIONS_APPROVAL_TTL_S", "7")

    stub.SCRIPT = [
        {"type": "proposed_run", "data": {"confirmation_id": "cid-1", "tool": "drawing.write",
                                          "params": {"x": 1}, "capability": "drawing.write",
                                          "rationale": "move a panel"}},
        {"type": "confirmation_required", "data": {"confirmation_id": "cid-1", "kind": "run",
                                                    "payload": {"note": "confirm?"}}},
        {"type": "turn_complete", "data": {"stop_reason": "awaiting_approval"}},
    ]

    sess = _new_session("tenant-approve")
    session_id = sess["session_id"]
    turn_id = turn_runner.start_turn("tenant-approve", session_id, text="move panel A2")

    ok = _wait_until(lambda: session_store.get_approval("cid-1") is not None)
    assert ok, "approval row was never created"

    approval = session_store.get_approval("cid-1")
    assert approval["session_id"] == session_id
    assert approval["tenant_id"] == "tenant-approve"
    assert approval["turn_id"] == turn_id
    assert approval["tool"] == "drawing.write"
    assert approval["params"] == {"x": 1}
    assert approval["capability"] == "drawing.write"
    assert approval["rationale"] == "move a panel"
    # Since the spine unification (census #12 chip 1) the row is created at
    # proposed_run — race-free with the chip becoming client-visible — so its
    # metadata comes from the PROPOSAL; confirmation_required's kind/payload
    # stay on the transcript event (which is what UIs render), and its
    # duplicate create is a swallowed no-op. No production code reads the
    # row's kind/payload (consume uses tool/params/capability).
    assert approval["kind"] == "run_capability"
    assert approval["payload"] is None
    assert approval["decided"] is False

    ttl = approval["expires_at"] - approval["created_at"]
    assert abs(ttl - 7.0) < 0.5, ttl

    _wait_until(lambda: session_store.get_session(session_id)["active_turn_id"] is None)


def test_spine_proposed_run_creates_approval_row_at_awaiting_approval_end(monkeypatch, turn_stub):
    """Spine unification (census #12 chip 1): the mounted §18 engine proposes a
    write with proposed_run ONLY — no confirmation_required follows — so the
    relay must still create the tenant-facing approvals row (at the
    awaiting_approval turn end) or the chip is undeliverable."""
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")
    monkeypatch.setenv("SESSIONS_APPROVAL_TTL_S", "7")

    stub.SCRIPT = [
        {"type": "proposed_run", "data": {"confirmation_id": "cid-spine-1", "tool": "add-panel",
                                          "params": {"col": 2}, "dwg": "d-1",
                                          "capability": "drawing.write",
                                          "rationale": "approval required by policy"}},
        {"type": "turn_complete", "data": {"stop_reason": "awaiting_approval"}},
    ]

    sess = _new_session("tenant-spine")
    session_id = sess["session_id"]
    turn_id = turn_runner.start_turn("tenant-spine", session_id, text="add a panel at col 2")

    ok = _wait_until(lambda: session_store.get_approval("cid-spine-1") is not None)
    assert ok, "spine proposed_run never produced an approvals row"

    approval = session_store.get_approval("cid-spine-1")
    assert approval["session_id"] == session_id
    assert approval["tenant_id"] == "tenant-spine"
    assert approval["turn_id"] == turn_id
    assert approval["tool"] == "add-panel"
    assert approval["params"] == {"col": 2}
    assert approval["capability"] == "drawing.write"
    assert approval["rationale"] == "approval required by policy"
    assert approval["kind"] == "run_capability"
    assert approval["decided"] is False

    _wait_until(lambda: session_store.get_session(session_id)["active_turn_id"] is None)


# =========================================================================== #
# (c) immediate rejections release the CAS
# =========================================================================== #
def test_immediate_401_maps_to_grant_required_and_releases_cas(monkeypatch, turn_stub):
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")
    stub.IMMEDIATE_STATUS = 401
    stub.IMMEDIATE_BODY = {"grant_required": True, "message": "no linked grant"}

    sess = _new_session("tenant-401")
    session_id = sess["session_id"]

    with pytest.raises(turn_runner.TurnRejected) as ei:
        turn_runner.start_turn("tenant-401", session_id, text="hi")
    exc = ei.value
    assert exc.status_code == 401
    assert exc.error_code == ErrorCode.GRANT_REQUIRED
    assert exc.extra == {"grant_required": True}

    assert session_store.get_session(session_id)["active_turn_id"] is None
    # a second attempt must be able to acquire the CAS immediately
    ok = session_store.try_begin_turn(session_id, "probe-turn", 30)
    assert ok is True


@pytest.mark.parametrize("body_code,expected", [
    ("llm_quota_exhausted", ErrorCode.LLM_QUOTA_EXHAUSTED),
    ("llm_rate_limited", ErrorCode.LLM_RATE_LIMITED),
])
def test_immediate_429_maps_quota_vs_rate_limited(monkeypatch, turn_stub, body_code, expected):
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")
    stub.IMMEDIATE_STATUS = 429
    stub.IMMEDIATE_BODY = {"errorCode": body_code, "message": "slow down"}

    sess = _new_session(f"tenant-429-{body_code}")
    session_id = sess["session_id"]

    with pytest.raises(turn_runner.TurnRejected) as ei:
        turn_runner.start_turn(f"tenant-429-{body_code}", session_id, text="hi")
    exc = ei.value
    assert exc.status_code == 429
    assert exc.error_code == expected
    assert session_store.get_session(session_id)["active_turn_id"] is None


def test_connection_refused_maps_to_broker_unreachable_and_releases_cas(monkeypatch):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", f"http://127.0.0.1:{_free_closed_port()}")
    monkeypatch.setenv("TURN_MAX_S", "30")

    sess = _new_session("tenant-refused")
    session_id = sess["session_id"]

    with pytest.raises(turn_runner.TurnRejected) as ei:
        turn_runner.start_turn("tenant-refused", session_id, text="hi")
    exc = ei.value
    assert exc.status_code == 502
    assert exc.error_code == ErrorCode.BROKER_UNREACHABLE
    assert session_store.get_session(session_id)["active_turn_id"] is None


def test_harness_not_configured_maps_to_broker_unreachable(monkeypatch):
    monkeypatch.delenv("LEAF_AUTHOR_HARNESS_URL", raising=False)
    monkeypatch.setenv("TURN_MAX_S", "30")

    sess = _new_session("tenant-noharness")
    session_id = sess["session_id"]

    with pytest.raises(turn_runner.TurnRejected) as ei:
        turn_runner.start_turn("tenant-noharness", session_id, text="hi")
    assert ei.value.status_code == 502
    assert ei.value.error_code == ErrorCode.BROKER_UNREACHABLE
    # turn_started was still durably recorded before the pre-flight check
    events = session_store.recent_events(session_id, 10)
    assert [e["type"] for e in events] == ["turn_started"]


# =========================================================================== #
# (d) CAS loss -> TurnBusy, no HTTP call
# =========================================================================== #
def test_turn_busy_raised_without_any_http_call(monkeypatch):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://127.0.0.1:1")  # would refuse if ever hit
    monkeypatch.setenv("TURN_MAX_S", "30")

    sess = _new_session("tenant-busy")
    session_id = sess["session_id"]
    assert session_store.try_begin_turn(session_id, "existing-turn", 9999) is True

    with pytest.raises(turn_runner.TurnBusy):
        turn_runner.start_turn("tenant-busy", session_id, text="hi")

    # the existing turn's ownership is untouched
    assert session_store.get_session(session_id)["active_turn_id"] == "existing-turn"


# =========================================================================== #
# (e) unknown session_id
# =========================================================================== #
def test_unknown_session_id_maps_to_session_not_found(monkeypatch):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://127.0.0.1:1")
    with pytest.raises(turn_runner.TurnRejected) as ei:
        turn_runner.start_turn("tenant-x", "no-such-session-id", text="hi")
    assert ei.value.status_code == 404
    assert ei.value.error_code == ErrorCode.SESSION_NOT_FOUND


def test_foreign_tenant_session_id_also_maps_to_session_not_found(monkeypatch):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://127.0.0.1:1")
    sess = _new_session("tenant-owner")
    with pytest.raises(turn_runner.TurnRejected) as ei:
        turn_runner.start_turn("tenant-intruder", sess["session_id"], text="hi")
    assert ei.value.status_code == 404
    assert ei.value.error_code == ErrorCode.SESSION_NOT_FOUND


# =========================================================================== #
# (f) watchdog fires when the stream never completes
# =========================================================================== #
def test_watchdog_appends_timeout_turn_complete_when_stream_never_ends(monkeypatch, turn_stub):
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "1")  # watchdog deadline = turn-start + 1s

    # 4 chunks ~0.12s apart (last one lands ~t=0.45-0.6s) then the stub hangs
    # (never sends the terminating 0-chunk). The watchdog's deadline is
    # anchored at turn-start (t=1s); the request-level read-timeout resets on
    # every chunk received, so its deadline after the last chunk is
    # last_chunk_time + 1s > 1s — strictly later than the watchdog. The
    # watchdog is therefore guaranteed to fire first.
    stub.SCRIPT = [
        {"type": "text_delta", "data": {"text": "thinking"}},
        {"type": "text_delta", "data": {"text": "..."}},
        {"type": "tool_call", "data": {"tool": "slow_tool", "args_summary": "{}"}},
        {"type": "text_delta", "data": {"text": "still going"}},
    ]
    stub.CHUNK_DELAY_S = 0.12
    stub.SEND_TERMINATOR = False

    sess = _new_session("tenant-watchdog")
    session_id = sess["session_id"]
    turn_id = turn_runner.start_turn("tenant-watchdog", session_id, text="do something slow")

    # Give the watchdog its full TURN_MAX_S=1s plus slack, but assert well
    # before the read-timeout's earliest possible deadline (~last_chunk+1s
    # >= 1.45s here), so a pass here cannot be explained by the read-timeout
    # instead of the watchdog.
    ok = _wait_until(lambda: session_store.get_session(session_id)["active_turn_id"] is None,
                     timeout_s=1.35)
    assert ok, "CAS was not released by the watchdog within its deadline"

    events = session_store.recent_events(session_id, 100)
    assert all(e["turn_id"] == turn_id for e in events)
    terminal = [e for e in events if e["type"] in ("turn_complete", "error")]
    assert len(terminal) == 1, terminal
    assert terminal[0]["type"] == "turn_complete"
    assert terminal[0]["data"] == {"stop_reason": "timeout"}

    # exactly one terminal event ever landed — no double-fire race with the
    # drain thread's own "stream ended" cleanup.
    types = [e["type"] for e in events]
    assert types.count("turn_complete") == 1
    assert types.count("error") == 0


# =========================================================================== #
# prior-context messages[] — bounded fold of the event log
# =========================================================================== #
def test_second_turn_carries_prior_turn_as_messages(monkeypatch, turn_stub):
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")

    stub.SCRIPT = [
        {"type": "text_delta", "data": {"text": "42 panels."}},
        {"type": "turn_complete", "data": {"stop_reason": "end_turn"}},
    ]

    sess = _new_session("tenant-history")
    session_id = sess["session_id"]

    turn_runner.start_turn("tenant-history", session_id, text="how many panels?")
    ok = _wait_until(lambda: session_store.get_session(session_id)["active_turn_id"] is None)
    assert ok

    stub.SCRIPT = [{"type": "turn_complete", "data": {"stop_reason": "end_turn"}}]
    turn_runner.start_turn("tenant-history", session_id, text="thanks")
    _wait_until(lambda: stub.LAST_BODY is not None and stub.LAST_BODY.get("text") == "thanks")

    body = stub.LAST_BODY
    assert body["messages"] == [
        {"role": "user", "text": "how many panels?"},
        {"role": "assistant", "text": "42 panels."},
    ]
