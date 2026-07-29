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
      releases the CAS — and reports that SAME terminal event whether the
      watchdog or the drain thread got there first.

      The two race for one condition, and the race is not winnable by
      widening a budget: the drain thread is started first, its socket read
      timeout is also TURN_MAX_S, and urllib3 restarts that timeout on every
      chunk, so any fixed margin is only a scheduling allowance. They are
      therefore made to AGREE on a shared deadline rather than raced —
      `_drain_terminal`, asserted directly by
      `test_drain_failure_past_the_deadline_reports_timeout_not_error` and
      its before-the-deadline control, with no threads or wall clock in the
      loop. The behavioral cases below then assert the observable outcome:
      a stalled stream and a harness silent from the first byte both
      terminalize as turn_complete{stop_reason:'timeout'}, exactly once.

      An earlier revision of this file claimed the ordering was "proven by
      construction" from chunk arrival times. It was not: the separation was
      only ever `last_chunk_time - turn_start`, which the HARNESS controls,
      not this module. On a loaded host the stub's own server thread starves,
      chunks stop arriving, the margin collapses to zero and the terminal
      event became a scheduling coin flip.
  (g) the arbitration MECHANISM, not just its two decision functions:
      `resolve()` is evaluated INSIDE the one-shot lock, and `_eof_terminal`
      is actually WIRED into `_drain`'s `finally:` leg. Both properties are
      invisible to (f) — the helpers are pure functions and keep passing when
      the call moves out of the critical section or disappears entirely. These
      drive `_spawn_relay` directly with a fake response and a captured-thread
      `threading` stand-in, so the drain and the watchdog interleave in a
      FIXED order across a real barrier rather than by wall clock.

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
import deps  # noqa: E402
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
    assert approval["payload"] == {"dwg": "d-1"}
    assert approval["decided"] is False

    _wait_until(lambda: session_store.get_session(session_id)["active_turn_id"] is None)


def test_proposed_run_row_exists_before_publication(monkeypatch, turn_stub):
    """Round-2 review invariant: `append_event(proposed_run)` IS publication
    (the SSE relay serves straight from the store), so the approvals row must
    already exist at that moment — structurally, not eventually."""
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")

    stub.SCRIPT = [
        {"type": "proposed_run", "data": {"confirmation_id": "cid-order-1", "tool": "add-panel",
                                          "params": {}, "capability": "drawing.write",
                                          "rationale": "policy"}},
        {"type": "turn_complete", "data": {"stop_reason": "awaiting_approval"}},
    ]

    row_existed_at_publication = []
    real_append = session_store.append_event

    def spy_append(session_id, turn_id, ev_type, data):
        if ev_type == "proposed_run":
            row_existed_at_publication.append(
                session_store.get_approval(data.get("confirmation_id")) is not None)
        return real_append(session_id, turn_id, ev_type, data)

    monkeypatch.setattr(session_store, "append_event", spy_append)

    sess = _new_session("tenant-order")
    turn_runner.start_turn("tenant-order", sess["session_id"], text="add a panel")
    ok = _wait_until(lambda: session_store.get_session(sess["session_id"])["active_turn_id"] is None)
    assert ok
    assert row_existed_at_publication == [True]


def test_approval_store_failure_terminalizes_turn_instead_of_dead_chip(monkeypatch, turn_stub):
    """A non-duplicate approvals-store failure must NOT publish the chip and
    swallow the error (an undecidable chip): the relay's outer handler
    terminalizes the turn with an in-band error instead."""
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")

    stub.SCRIPT = [
        {"type": "proposed_run", "data": {"confirmation_id": "cid-dead-1", "tool": "add-panel",
                                          "params": {}, "capability": "drawing.write",
                                          "rationale": "policy"}},
        {"type": "turn_complete", "data": {"stop_reason": "awaiting_approval"}},
    ]

    def boom(**_kwargs):
        raise OSError("approvals store down")

    monkeypatch.setattr(session_store, "create_approval", boom)

    sess = _new_session("tenant-deadchip")
    session_id = sess["session_id"]
    turn_id = turn_runner.start_turn("tenant-deadchip", session_id, text="add a panel")

    ok = _wait_until(lambda: session_store.get_session(session_id)["active_turn_id"] is None)
    assert ok, "CAS never released after the store failure"

    events = session_store.recent_events(session_id, 100)
    types = [e["type"] for e in events if e["turn_id"] == turn_id]
    assert "proposed_run" not in types, "dead chip was published"
    assert "error" in types
    assert session_store.get_approval("cid-dead-1") is None


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


def test_turn_cas_snapshots_the_verified_tenant_tier(monkeypatch):
    sess = _new_session("tenant-tier-snapshot")
    captured = {}

    def reject_after_capture(session_id, turn_id, stale_after_s, tier=None):
        captured.update(
            session_id=session_id, turn_id=turn_id,
            stale_after_s=stale_after_s, tier=tier,
        )
        return False

    monkeypatch.setattr(session_store, "try_begin_turn", reject_after_capture)
    tenant = deps.TenantContext("tenant-tier-snapshot", tier="hosted_pro")
    with pytest.raises(turn_runner.TurnBusy):
        turn_runner.start_turn(tenant, sess["session_id"], text="hi")

    assert captured["session_id"] == sess["session_id"]
    assert captured["tier"] == "hosted_pro"


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

    # 4 chunks ~0.12s apart, then the stub hangs (never sends the terminating
    # 0-chunk).
    #
    # NOTE: the chunk spacing is scenery, NOT the guarantee. It used to be
    # load-bearing — the read timeout's deadline is last_chunk + TURN_MAX_S,
    # so the spacing bought ~0.36s of margin — and that is exactly why this
    # test flaked: a loaded host starves the stub's own server thread, chunks
    # stop arriving, and the margin collapses to zero. The terminal event no
    # longer depends on that margin, or on which thread wins: both paths
    # resolve against one shared deadline (_drain_terminal).
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

    # This budget is LIVENESS only, and deliberately generous. It is NOT what
    # decides the outcome — racing the two deadlines by wall clock is what
    # made this test flaky in the first place. The assertion below is on the
    # terminal event's SIGNATURE, and once either path's `_end_once` has won,
    # waiting longer cannot turn an `error` into a `turn_complete`.
    ok = _wait_until(lambda: session_store.get_session(session_id)["active_turn_id"] is None,
                     timeout_s=8.0)
    assert ok, "CAS was not released by the watchdog within its deadline"

    events = session_store.recent_events(session_id, 100)
    assert all(e["turn_id"] == turn_id for e in events)
    terminal = [e for e in events if e["type"] in ("turn_complete", "error")]
    assert len(terminal) == 1, terminal
    # THE control: an `error` here means the request-level read timeout
    # terminalized this turn instead of the watchdog — the exact regression
    # this test exists to catch, and the exact way it used to fail on a
    # loaded host.
    assert terminal[0]["type"] == "turn_complete", (
        "the read timeout beat the watchdog and terminalized the turn: "
        f"{terminal[0]}"
    )
    assert terminal[0]["data"] == {"stop_reason": "timeout"}

    # exactly one terminal event ever landed — no double-fire race with the
    # drain thread's own "stream ended" cleanup.
    types = [e["type"] for e in events]
    assert types.count("turn_complete") == 1
    assert types.count("error") == 0


def test_drain_failure_past_the_deadline_reports_timeout_not_error():
    """The arbitration invariant — asserted with no threads and no wall clock
    in the loop at all.

    The drain thread and the watchdog race for the same condition, and the
    race cannot be won by widening either budget: any fixed margin is only a
    scheduling allowance, and the drain thread is started first. So the two
    paths are made to AGREE instead. Past the turn's own deadline a stream
    failure IS turn expiry, and the drain must report exactly what the
    watchdog would.
    """
    already_past = time.monotonic() - 1.0
    ev_type, data = turn_runner._drain_terminal(
        already_past, RuntimeError("connection reset by peer"))
    assert ev_type == "turn_complete"
    assert data == {"stop_reason": "timeout"}


def test_drain_failure_before_the_deadline_still_reports_error():
    """The control: arbitration must NOT blanket-relabel stream failures as
    timeouts. A genuine transport fault well inside the turn's budget is still
    an `error`, with its cause preserved."""
    not_yet = time.monotonic() + 30.0
    ev_type, data = turn_runner._drain_terminal(
        not_yet, RuntimeError("connection reset by peer"))
    assert ev_type == "error"
    assert data["error"]["error_code"] == ErrorCode.INTERNAL
    assert "connection reset by peer" in data["error"]["message"]


def test_clean_eof_past_the_deadline_also_reports_timeout():
    """A stream that simply ENDS past the deadline must read like the
    watchdog's terminal event too.

    Otherwise the same expired turn yields NO terminal event when the drain
    thread notices the EOF first, and turn_complete{stop_reason:'timeout'}
    when the watchdog fires first — the CAS released either way, but the
    client left showing a turn still in flight in the first case.
    """
    already_past = time.monotonic() - 1.0
    ev_type, data = turn_runner._eof_terminal(already_past)
    assert ev_type == "turn_complete"
    assert data == {"stop_reason": "timeout"}


def test_clean_eof_before_the_deadline_appends_no_terminal_event():
    """The control for the EOF path: an unexpectedly SHORT stream is not an
    expired turn. Historical behavior is preserved — release the CAS, append
    nothing — so this arbitration cannot manufacture a timeout for a stream
    that ended early."""
    not_yet = time.monotonic() + 30.0
    ev_type, data = turn_runner._eof_terminal(not_yet)
    assert ev_type is None
    assert data is None


def test_watchdog_wins_even_when_the_harness_is_silent_from_the_first_byte(
        monkeypatch, turn_stub):
    """The canonical hang: the harness answers 200 + headers and then sends
    nothing at all, ever.

    This is the case with NO separation to borrow — not one chunk arrives to
    push the read timeout's deadline out — so both deadlines land on the same
    instant and the winner is pure scheduling. Measured against the pre-fix
    code on an IDLE host, the drain thread won 11 of 12 runs here and the turn
    terminalized as `error` instead of the documented timeout. The outcome is
    now the same either way.
    """
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "1")

    stub.SCRIPT = []              # not one byte of body
    stub.SEND_TERMINATOR = False  # and never the terminating 0-chunk

    sess = _new_session("tenant-watchdog-silent")
    session_id = sess["session_id"]
    turn_id = turn_runner.start_turn(
        "tenant-watchdog-silent", session_id, text="hello?")

    ok = _wait_until(lambda: session_store.get_session(session_id)["active_turn_id"] is None,
                     timeout_s=8.0)
    assert ok, "CAS was never released for a harness that went silent"

    events = session_store.recent_events(session_id, 100)
    assert all(e["turn_id"] == turn_id for e in events)
    terminal = [e for e in events if e["type"] in ("turn_complete", "error")]
    assert len(terminal) == 1, terminal
    assert terminal[0]["type"] == "turn_complete", (
        "the read timeout beat the watchdog and terminalized the turn: "
        f"{terminal[0]}"
    )
    assert terminal[0]["data"] == {"stop_reason": "timeout"}


# =========================================================================== #
# (g) the arbitration MECHANISM — the one-shot lock, and the EOF wiring
#
# The four helper tests above assert `_drain_terminal` / `_eof_terminal` as
# pure functions. Two things they structurally cannot see:
#
#   1. WHERE the decision is taken. `_end_once` evaluates `resolve()` inside
#      `terminal_lock`; move that call above the `with` and both helpers still
#      pass, while the terminal event goes back to depending on the scheduler.
#   2. WHETHER the EOF helper is called at all. Revert `_drain`'s `finally:`
#      leg to a bare `_end_once()` and, again, both helpers still pass.
#
# Both tests below therefore drive `_spawn_relay` directly with a fake
# response and a captured-thread `threading` stand-in, so the drain and the
# watchdog can be interleaved in a FIXED order across a real barrier. No sleep
# and no wall clock decides either outcome. (sol-critic PR #222, the two
# optional test-coverage gaps.)
# =========================================================================== #
class _ObservableLock:
    """A mutex that reports whether a SECOND thread ever had to wait for it.

    That is the entire observable difference between resolving the terminal
    event inside the critical section and resolving it outside: inside, the
    other thread blocks here; outside, it walks straight in."""

    def __init__(self, real: threading.Lock, contended: threading.Event) -> None:
        self._real = real
        self._contended = contended

    def __enter__(self):
        if not self._real.acquire(blocking=False):
            self._contended.set()
            self._real.acquire()
        return self

    def __exit__(self, *exc):
        self._real.release()
        return False


class _CapturedThread:
    """`start()` is a no-op — the test starts the target itself."""

    daemon = True

    def start(self) -> None:
        return None


class _RelayHarness:
    """The slice of the `threading` module `_spawn_relay` actually uses, with
    the one-shot lock instrumented and both relay threads captured rather than
    started.

    `_spawn_relay` starts the drain and the watchdog itself, so from outside
    there is no way to interleave them and the ordering these tests need could
    only be bought with a sleep. Capturing the targets makes the order exact."""

    def __init__(self) -> None:
        self.lock_contended = threading.Event()
        self.targets: Dict[str, Any] = {}
        self.Event = threading.Event  # noqa: N803  mirrors the real module

    def Lock(self):  # noqa: N802  mirrors threading.Lock
        return _ObservableLock(threading.Lock(), self.lock_contended)

    def Thread(self, *, target, daemon=None, name=None):  # noqa: N802
        self.targets[name] = target
        return _CapturedThread()

    def launch(self, name_prefix: str) -> threading.Thread:
        matches = [n for n in self.targets if n.startswith(name_prefix)]
        assert len(matches) == 1, f"{name_prefix!r} matched {matches}"
        t = threading.Thread(target=self.targets[matches[0]],
                             name=f"test-{matches[0]}", daemon=True)
        t.start()
        return t


class _FrozenClock:
    """A stand-in for the `time` module that never advances.

    `monotonic` is the ONLY attribute `turn_runner` reads from `time` — see
    `_drain_terminal`, `_eof_terminal`, `_spawn_relay`'s deadline, and the
    watchdog's `finished.wait(...)` — so replacing the whole module is total
    rather than a partial patch that leaves a second clock running."""

    _NOW = 1_000_000.0

    def monotonic(self) -> float:
        return self._NOW


class _FakeStream:
    """The two attributes `_drain` touches on a `requests.Response`."""

    def __init__(self, lines: Optional[List[str]] = None,
                 raise_at_end: Optional[BaseException] = None) -> None:
        self._lines = list(lines or [])
        self._raise = raise_at_end
        self.closed = 0

    def iter_lines(self, decode_unicode: bool = False):
        for line in self._lines:
            yield line
        if self._raise is not None:
            raise self._raise

    def close(self) -> None:
        self.closed += 1


def test_terminal_event_is_resolved_inside_the_one_shot_lock(monkeypatch):
    """`resolve()` must be evaluated INSIDE `terminal_lock`, never before it.

    Deciding first and claiming second reopens the window this whole
    arbitration exists to close: between the decision and the claim the
    deciding thread can be descheduled, the other thread can take the flag,
    and which terminal event the caller observes is a coin flip again.

    The distinguishing observation, with no sleep in it: park the drain thread
    inside its resolver, then start the watchdog. If the resolver runs inside
    the lock, the watchdog BLOCKS on it and the drain's own event is the one
    that lands. If it runs outside, the watchdog walks straight through, takes
    the flag, and its `turn_complete{timeout}` lands instead.
    """
    harness = _RelayHarness()
    monkeypatch.setattr(turn_runner, "threading", harness)

    inside_resolver = threading.Event()
    contention_seen: List[bool] = []
    _DRAIN_MARK = "resolved-by-the-drain-thread"

    def _parked_resolver(deadline, exc):
        # Stands in for `_drain_terminal`. Parks here holding exactly whatever
        # the caller holds, until the watchdog has PROVABLY queued on the
        # one-shot lock (or until it is clear it never will).
        inside_resolver.set()
        contention_seen.append(harness.lock_contended.wait(timeout=5.0))
        return "error", {"error": {"error_code": ErrorCode.INTERNAL,
                                   "message": _DRAIN_MARK}}

    monkeypatch.setattr(turn_runner, "_drain_terminal", _parked_resolver)

    sess = _new_session("tenant-arb-lock")
    session_id = sess["session_id"]
    turn_id = "turn-arb-lock"
    assert session_store.try_begin_turn(session_id, turn_id, 60.0)

    # max_s=0 puts the SHARED deadline in the past, so the watchdog's
    # `finished.wait()` returns at once and the only thing that can hold it
    # back is the one-shot lock itself.
    resp = _FakeStream(raise_at_end=RuntimeError("connection reset by peer"))
    turn_runner._spawn_relay("tenant-arb-lock", session_id, turn_id, resp, 0.0)

    drain = harness.launch("turn-drain")
    assert inside_resolver.wait(timeout=5.0), "the drain never reached its resolver"
    watchdog = harness.launch("turn-watchdog")

    drain.join(timeout=15.0)
    watchdog.join(timeout=15.0)
    assert not drain.is_alive() and not watchdog.is_alive()

    assert contention_seen == [True], (
        "the watchdog never had to wait for the one-shot lock while the drain "
        "thread sat inside its resolver, so `resolve()` is being evaluated "
        "OUTSIDE `with terminal_lock:` — which puts a descheduling window "
        "between the decision and the claim and makes the terminal event "
        "scheduler-dependent again"
    )

    terminal = [e for e in session_store.recent_events(session_id, 100)
                if e["type"] in ("turn_complete", "error")]
    assert len(terminal) == 1, terminal
    assert terminal[0]["data"].get("error", {}).get("message") == _DRAIN_MARK, (
        "the watchdog terminalized the turn while the drain thread was still "
        f"inside its resolver: {terminal[0]}"
    )
    assert session_store.get_session(session_id)["active_turn_id"] is None


def test_clean_eof_past_the_deadline_is_wired_into_the_drain_finally(monkeypatch):
    """`_drain`'s `finally:` leg must ASK `_eof_terminal`, not merely release
    the CAS.

    `_eof_terminal` is a pure function, so it passes whether or not anything
    calls it: revert that leg to a bare `_end_once()` and every direct
    assertion on it still holds. Only this test fails — the expired turn is
    terminalized with NO event at all, CAS released, client left showing a
    turn still in flight.

    The watchdog is captured and deliberately NEVER started. It appends the
    very same `turn_complete{stop_reason:'timeout'}`, so letting it run would
    mask a reverted `finally:` leg completely.
    """
    harness = _RelayHarness()
    monkeypatch.setattr(turn_runner, "threading", harness)

    sess = _new_session("tenant-eof-wiring")
    session_id = sess["session_id"]
    turn_id = "turn-eof-wired"
    assert session_store.try_begin_turn(session_id, turn_id, 60.0)

    # one ordinary relayed line, then a clean end of stream — the harness never
    # sends a terminal event — and max_s=0 puts the shared deadline in the past.
    resp = _FakeStream(lines=[json.dumps({"type": "text_delta", "data": {"text": "hi"}})])
    turn_runner._spawn_relay("tenant-eof-wiring", session_id, turn_id, resp, 0.0)

    assert any(n.startswith("turn-watchdog") for n in harness.targets), \
        "the watchdog must exist and stay unstarted for this test to mean anything"
    drain = harness.launch("turn-drain")
    drain.join(timeout=15.0)
    assert not drain.is_alive()

    events = session_store.recent_events(session_id, 100)
    terminal = [e for e in events if e["type"] in ("turn_complete", "error")]
    assert len(terminal) == 1, (
        "the drain's EOF cleanup appended no terminal event for a turn already "
        "past its deadline, so `_eof_terminal` is not wired into the `finally:` "
        f"leg of `_drain`. events={[e['type'] for e in events]}"
    )
    assert terminal[0]["type"] == "turn_complete"
    assert terminal[0]["data"] == {"stop_reason": "timeout"}
    assert session_store.get_session(session_id)["active_turn_id"] is None
    assert resp.closed >= 1


def test_clean_eof_before_the_deadline_is_wired_to_the_turns_own_deadline(monkeypatch):
    """The control for that wiring: the `finally:` leg must pass the turn's OWN
    deadline, not append a timeout unconditionally.

    Without this, `finally: _end_once('turn_complete', {'stop_reason':
    'timeout'})` would satisfy the test above while manufacturing a timeout for
    every stream that merely ended early.

    This is the ONE case here with a deadline in the future, so it is the one
    case real elapsed time could decide: `_eof_terminal` compares
    `time.monotonic() >= deadline`, so a stall longer than the budget between
    `_spawn_relay` and the drain's `finally:` leg would return a timeout and
    fail this test with no code regression at all. The other two pin the
    deadline in the PAST (`max_s=0`), which no delay can undo. Freezing the
    clock removes the asymmetry: `deadline` is `_FROZEN_NOW + 30.0` and every
    reading is `_FROZEN_NOW`, so "inside the budget" holds no matter how long
    the host stalls. `monotonic` is the only `time` attribute this module
    uses (`_drain_terminal`, `_eof_terminal`, `_spawn_relay`, watchdog), so the
    stand-in is total. (sol-critic PR #224 round 1, blocker 1.)"""
    harness = _RelayHarness()
    monkeypatch.setattr(turn_runner, "threading", harness)
    monkeypatch.setattr(turn_runner, "time", _FrozenClock())

    sess = _new_session("tenant-eof-early")
    session_id = sess["session_id"]
    turn_id = "turn-eof-early"
    assert session_store.try_begin_turn(session_id, turn_id, 60.0)

    resp = _FakeStream(lines=[json.dumps({"type": "text_delta", "data": {"text": "hi"}})])
    turn_runner._spawn_relay("tenant-eof-early", session_id, turn_id, resp, 30.0)

    drain = harness.launch("turn-drain")   # watchdog again never started
    drain.join(timeout=15.0)
    assert not drain.is_alive()

    events = session_store.recent_events(session_id, 100)
    terminal = [e for e in events if e["type"] in ("turn_complete", "error")]
    assert terminal == [], (
        "a stream that ended WELL INSIDE the turn's budget was terminalized as "
        f"an expired turn: {terminal}"
    )
    assert [e["type"] for e in events] == ["text_delta"]
    assert session_store.get_session(session_id)["active_turn_id"] is None


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


# =========================================================================== #
# pre_harness classification (routers/sessions.py's APPROVAL GIVE-BACK). The
# router un-spends a consumed approval IFF TurnRejected.pre_harness is set, so
# a leg wrongly marked pre_harness can un-spend an approval the harness is
# already acting on. Getting this boundary right IS the safety property.
# (sol-critic round 2, blocker 1.)
# =========================================================================== #
def _reject_from_post_error(monkeypatch, exc, url="http://127.0.0.1:9/"):
    """Drive start_turn to its POST and make requests.post raise `exc`."""
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    sess = _new_session("tenant-preharness")

    def _boom(*a, **kw):
        raise exc

    monkeypatch.setattr(turn_runner.requests, "post", _boom)
    with pytest.raises(turn_runner.TurnRejected) as ei:
        turn_runner.start_turn("tenant-preharness", sess["session_id"], text="hi")
    return ei.value


def test_connect_timeout_is_pre_harness(monkeypatch):
    """ConnectTimeout: the TCP connection was never established, so no byte
    reached the harness — the one POST-phase leg safe to roll back."""
    exc = _reject_from_post_error(
        monkeypatch, turn_runner.requests.exceptions.ConnectTimeout("no connect"))
    assert exc.error_code == ErrorCode.BROKER_UNREACHABLE
    assert exc.pre_harness is True


def test_read_timeout_is_not_pre_harness(monkeypatch):
    """ReadTimeout: the POST was sent; the harness may be running the tool
    call right now."""
    exc = _reject_from_post_error(
        monkeypatch, turn_runner.requests.exceptions.ReadTimeout("slow"))
    assert exc.pre_harness is False


def test_accept_then_disconnect_is_not_pre_harness(monkeypatch):
    """The interleaving that made a blanket ConnectionError unsafe: requests
    folds urllib3's ProtocolError (server accepted the POST, then dropped the
    connection before responding) into ConnectionError. Indistinguishable from
    'connection refused' here, so it MUST stay ambiguous."""
    exc = _reject_from_post_error(
        monkeypatch,
        turn_runner.requests.exceptions.ConnectionError(
            "('Connection aborted.', RemoteDisconnected(...))"),
    )
    assert exc.pre_harness is False, (
        "a connection dropped AFTER the harness accepted the request was "
        "classified as pre-contact — the router would un-spend an approval "
        "whose tool call may already be running"
    )


def test_missing_harness_url_is_pre_harness(monkeypatch):
    """No URL configured: no POST is attempted at all."""
    monkeypatch.delenv("LEAF_CONVERSE_HARNESS_URL", raising=False)
    monkeypatch.delenv("LEAF_AUTHOR_HARNESS_URL", raising=False)
    sess = _new_session("tenant-nourl")
    with pytest.raises(turn_runner.TurnRejected) as ei:
        turn_runner.start_turn("tenant-nourl", sess["session_id"], text="hi")
    assert ei.value.pre_harness is True


def test_turn_rejected_defaults_to_not_pre_harness():
    """The fail-safe default: any construction site that does not opt in is
    treated as 'the harness may have acted'."""
    exc = turn_runner.TurnRejected(502, ErrorCode.BROKER_UNREACHABLE, "x")
    assert exc.pre_harness is False


# =========================================================================== #
# (h) cancellation — POST /turns/{id}/cancel's engine half
#
# The user pressing Esc must end the turn PROMPTLY and release the CAS, so the
# session is immediately usable again. The two paths that matter are the live
# relay (a canceller is registered in this process) and the orphan (no relay
# here — a restarted process would otherwise leave the session wedged until
# the stale-turn window expired).
# =========================================================================== #
def test_cancel_terminalizes_a_live_turn_as_interrupted(monkeypatch, turn_stub):
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    # Deliberately far from the deadline: if this turn ends, the CANCEL ended
    # it — the watchdog cannot be the explanation.
    monkeypatch.setenv("TURN_MAX_S", "60")

    stub.SCRIPT = [
        {"type": "text_delta", "data": {"text": "thinking"}},
        {"type": "text_delta", "data": {"text": " hard"}},
    ]
    stub.CHUNK_DELAY_S = 0.02
    stub.SEND_TERMINATOR = False  # stream stays open until we cancel it

    sess = _new_session("tenant-cancel")
    session_id = sess["session_id"]
    turn_id = turn_runner.start_turn("tenant-cancel", session_id, text="think for a while")

    # Wait until the relay is actually streaming, so we cancel a LIVE turn
    # rather than racing the spawn.
    assert _wait_until(
        lambda: any(e["type"] == "text_delta"
                    for e in session_store.recent_events(session_id, 100)),
        timeout_s=5.0,
    ), "relay never started streaming"

    assert turn_runner.request_cancel("tenant-cancel", session_id, turn_id) == "cancelled"

    ok = _wait_until(
        lambda: session_store.get_session(session_id)["active_turn_id"] is None,
        timeout_s=5.0)
    assert ok, "cancel did not release the CAS"

    events = session_store.recent_events(session_id, 100)
    terminal = events[-1]
    assert terminal["type"] == "turn_complete", [e["type"] for e in events]
    assert terminal["data"].get("stop_reason") == "interrupted", terminal["data"]
    # Exactly one terminal event: _end_once is one-shot across every thread.
    assert sum(1 for e in events if e["type"] == "turn_complete") == 1


def test_cancel_of_an_orphaned_turn_still_releases_the_cas(monkeypatch):
    """No relay in this process (its thread/process died) — the durable row is
    terminalized directly, otherwise the session stays wedged."""
    sess = _new_session("tenant-orphan")
    session_id = sess["session_id"]
    turn_id = "orphan-turn-id"

    assert session_store.try_begin_turn(session_id, turn_id, 300.0)
    assert session_store.get_session(session_id)["active_turn_id"] == turn_id
    # Nothing registered a canceller for it.
    with turn_runner._cancellers_lock:
        assert turn_id not in turn_runner._cancellers

    assert turn_runner.request_cancel("tenant-orphan", session_id, turn_id) == "cancelled"

    assert session_store.get_session(session_id)["active_turn_id"] is None
    events = session_store.recent_events(session_id, 100)
    assert events[-1]["type"] == "turn_complete"
    assert events[-1]["data"].get("stop_reason") == "interrupted"


def test_cancel_refuses_a_turn_that_is_not_the_active_one(monkeypatch):
    """A client holding a STALE turn_id must not be able to terminalize the
    turn that replaced it."""
    sess = _new_session("tenant-stale")
    session_id = sess["session_id"]
    live_turn = "the-live-turn"
    assert session_store.try_begin_turn(session_id, live_turn, 300.0)

    before = len(session_store.recent_events(session_id, 100))
    assert turn_runner.request_cancel("tenant-stale", session_id, "some-older-turn") == "not_active"

    # The live turn is untouched and nothing was appended.
    assert session_store.get_session(session_id)["active_turn_id"] == live_turn
    assert len(session_store.recent_events(session_id, 100)) == before


def test_cancel_with_no_active_turn_is_not_active(monkeypatch):
    sess = _new_session("tenant-idle")
    session_id = sess["session_id"]
    assert session_store.get_session(session_id)["active_turn_id"] is None
    assert turn_runner.request_cancel("tenant-idle", session_id, "anything") == "not_active"


def test_cancel_is_idempotent(monkeypatch):
    """A double-tap on Esc must not append a second terminal event."""
    sess = _new_session("tenant-twice")
    session_id = sess["session_id"]
    turn_id = "twice-turn"
    assert session_store.try_begin_turn(session_id, turn_id, 300.0)

    assert turn_runner.request_cancel("tenant-twice", session_id, turn_id) == "cancelled"
    # The CAS is released, so the second call sees no active turn.
    assert turn_runner.request_cancel("tenant-twice", session_id, turn_id) == "not_active"
    events = session_store.recent_events(session_id, 100)
    assert sum(1 for e in events if e["type"] == "turn_complete") == 1


def test_cancel_unknown_session_maps_to_session_not_found(monkeypatch):
    with pytest.raises(turn_runner.TurnRejected) as ei:
        turn_runner.request_cancel("tenant-a", "no-such-session", "t1")
    assert ei.value.status_code == 404
    assert ei.value.error_code == ErrorCode.SESSION_NOT_FOUND


def test_cancel_foreign_tenant_session_also_maps_to_session_not_found(monkeypatch):
    """404-not-403: a real session owned by someone else is indistinguishable
    from one that does not exist."""
    sess = _new_session("tenant-owner")
    session_id = sess["session_id"]
    assert session_store.try_begin_turn(session_id, "t-owned", 300.0)

    with pytest.raises(turn_runner.TurnRejected) as ei:
        turn_runner.request_cancel("tenant-intruder", session_id, "t-owned")
    assert ei.value.status_code == 404
    assert ei.value.error_code == ErrorCode.SESSION_NOT_FOUND
    # and the owner's turn is untouched
    assert session_store.get_session(session_id)["active_turn_id"] == "t-owned"


def test_relayed_publish_holds_the_one_shot_lock(monkeypatch):
    """Publishing a relayed event and CLAIMING the terminal must be mutually
    exclusive, or an event lands after the terminal one.

    `_drain` checks `terminal_flag` at the top of its loop but publishes much
    later, so anything terminalizing in that gap — an arbitrary-moment cancel
    here, the watchdog when its deadline falls there — used to produce
    `[turn_started, turn_complete, text_delta]`. The client stops rendering at
    `turn_complete`, so the straggler was invisible in the UI and permanent in
    the durable transcript.

    The observable difference, with no sleep and no thread racing: while the
    drain is inside `append_event` for a relayed line, a concurrent cancel must
    BLOCK on the one-shot lock. `_ObservableLock` records exactly that. Publish
    outside the lock and the cancel walks straight through, `lock_contended`
    is never set, and this test fails — which is what makes it a guard rather
    than a description.

    The watchdog is captured and never started, so the cancel is the only other
    thread that can touch the lock.
    """
    harness = _RelayHarness()
    monkeypatch.setattr(turn_runner, "threading", harness)

    sess = _new_session("tenant-publish-lock")
    session_id = sess["session_id"]
    turn_id = "turn-publish-lock"
    assert session_store.try_begin_turn(session_id, turn_id, 60.0)

    resp = _FakeStream(lines=[
        json.dumps({"type": "text_delta", "data": {"text": "straggler"}}),
    ])
    turn_runner._spawn_relay("tenant-publish-lock", session_id, turn_id, resp, 60.0)

    real_append = session_store.append_event
    fired = threading.Event()
    cancel_thread: Dict[str, Any] = {}

    def _append_and_race(sid, tid, ev_type, data):
        # Only for the relayed line — not for `turn_started` or the terminal
        # event the cancel itself appends.
        if ev_type == "text_delta" and not fired.is_set():
            fired.set()
            t = threading.Thread(
                target=lambda: turn_runner.request_cancel(
                    "tenant-publish-lock", session_id, turn_id),
                name="test-canceller", daemon=True)
            cancel_thread["t"] = t
            t.start()
            # Wait for that thread to actually reach the lock. With the publish
            # inside the critical section it blocks and this resolves; without
            # it, the cancel sails past and this times out.
            harness.lock_contended.wait(timeout=10.0)
        return real_append(sid, tid, ev_type, data)

    monkeypatch.setattr(session_store, "append_event", _append_and_race)

    drain = harness.launch("turn-drain")
    drain.join(timeout=20.0)
    assert not drain.is_alive()
    if cancel_thread.get("t"):
        cancel_thread["t"].join(timeout=20.0)
        assert not cancel_thread["t"].is_alive()

    assert fired.is_set(), "the relayed line was never published"
    assert harness.lock_contended.is_set(), (
        "a concurrent cancel did NOT block while the drain was publishing a "
        "relayed event — publishing and claiming the terminal are not mutually "
        "exclusive, so an event can land after the terminal one"
    )

    monkeypatch.setattr(session_store, "append_event", real_append)
    events = session_store.recent_events(session_id, 100)
    types = [e["type"] for e in events]
    # At most one terminal event, and nothing after it. (A clean EOF before the
    # turn's deadline legitimately terminalizes with NO event at all — see
    # test_clean_eof_before_the_deadline_appends_no_terminal_event — so the
    # count is 0-or-1 here; the ORDERING is the invariant under test.)
    terminal_idx = [i for i, t in enumerate(types) if t in ("turn_complete", "error")]
    assert len(terminal_idx) <= 1, f"more than one terminal event: {types}"
    if terminal_idx:
        assert terminal_idx[0] == len(types) - 1, (
            f"an event was appended AFTER the terminal event. events={types}")
    assert session_store.get_session(session_id)["active_turn_id"] is None


def test_concurrent_orphan_cancels_append_exactly_one_terminal_event(monkeypatch):
    """Two cancels arriving at once must not both terminalize the same turn.

    The orphan path's active-turn check happens outside any lock, so both
    callers could read `active_turn_id == turn_id` and both append. The check
    is re-read under `_orphan_cancel_lock`, so exactly one wins and the other
    reports `not_active`.
    """
    sess = _new_session("tenant-double-cancel")
    session_id = sess["session_id"]
    turn_id = "turn-double-cancel"
    assert session_store.try_begin_turn(session_id, turn_id, 300.0)
    with turn_runner._cancellers_lock:
        assert turn_id not in turn_runner._cancellers  # orphan path

    start = threading.Barrier(2)
    outcomes: List[str] = []
    outcomes_lock = threading.Lock()

    def _cancel_once():
        start.wait(timeout=10)
        result = turn_runner.request_cancel("tenant-double-cancel", session_id, turn_id)
        with outcomes_lock:
            outcomes.append(result)

    threads = [threading.Thread(target=_cancel_once, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15.0)
        assert not t.is_alive()

    assert sorted(outcomes) == ["cancelled", "not_active"], outcomes
    events = session_store.recent_events(session_id, 100)
    assert sum(1 for e in events if e["type"] == "turn_complete") == 1, \
        [e["type"] for e in events]
    assert session_store.get_session(session_id)["active_turn_id"] is None
