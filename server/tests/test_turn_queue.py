"""
Busy-turn queue (cap 1) — chip acceptance: "One pending prompt is persisted
and starts after the terminal event; a second is refused."

Engine level (real turn_runner against a scripted NDJSON harness stub, the
tests/test_turn_runner.py pattern):
  (a) a prompt queued behind a live turn STARTS at that turn's terminal event,
      and a second enqueue while one is parked answers "full";
  (b) enqueueing against a session that is actually free starts immediately
      (the enqueue/terminal handoff race is closed by the post-insert check);
  (c) the ORPHAN cancel path kicks the queue (no relay exists to do it);
  (d) a kick whose start_turn is REJECTED (harness unreachable) closes the
      transcript it opened — turn_started followed by a terminal `error` for
      the same turn_id — and empties the slot (no silent loss, no retry loop);
  (e) a kick that loses the CAS to a foreign turn RE-INSERTS the prompt — the
      structural property that makes "that turn's terminal will kick" true.
      Removing the re-insert makes this fail (falsification-checked).

Route level (real engine, orphan-held CAS, TestClient over ONLY the sessions
router — the tests/test_sessions_routes.py pattern):
  (f) busy + queue:true text -> 202 {status:"queued"}, and the durable
      `turn_queued` event is on the transcript;
  (g) a second queue:true while one is parked -> the byte-identical busy 409;
  (h) busy WITHOUT queue -> 409 exactly as before (opt-in regression pin);
  (i) queue:true + confirm -> 400 BEFORE the approval consume (the approval
      stays undecided-consumable); queue:true + credential_grant -> 400.

Run:  cd server && python -m pytest tests/test_turn_queue.py -q
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

# Redirect the sessions DB BEFORE session_store is imported (read once at
# import time — same posture as tests/test_turn_runner.py).
os.environ.setdefault(
    "SESSIONS_DB",
    str(Path(tempfile.mkdtemp(prefix="turnqueue-sessions-")) / "sessions.db"))
os.environ.setdefault("LEAF_AUTH_LIVE", "0")

import session_store  # noqa: E402
import turn_runner  # noqa: E402
from envelopes import ErrorCode  # noqa: E402
from routers import sessions as sessions_router  # noqa: E402


# --------------------------------------------------------------------------- #
# scripted harness stub (test_turn_runner.py pattern) + a GATE so the test —
# not the scheduler — decides when the first turn's terminal event lands.
# --------------------------------------------------------------------------- #
class _TurnStub(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    SCRIPT: List[Dict[str, Any]] = []
    GATE: Optional[threading.Event] = None   # held BEFORE the final scripted event
    BODIES: List[Dict[str, Any]] = []

    def _write_chunk(self, raw: bytes) -> None:
        self.wfile.write(f"{len(raw):x}\r\n".encode("ascii") + raw + b"\r\n")
        self.wfile.flush()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", 0) or 0)
        body = self.rfile.read(length)
        cls = type(self)
        try:
            cls.BODIES.append(json.loads(body or b"{}"))
        except Exception:
            cls.BODIES.append({})
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for i, ev in enumerate(cls.SCRIPT):
            if cls.GATE is not None and i == len(cls.SCRIPT) - 1:
                cls.GATE.wait(timeout=10)
            self._write_chunk((json.dumps(ev) + "\n").encode("utf-8"))
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def log_message(self, *a):  # silence
        return


@pytest.fixture
def turn_stub():
    _TurnStub.SCRIPT = [
        {"type": "text_delta", "data": {"text": "working"}},
        {"type": "turn_complete", "data": {"stop_reason": "end_turn"}},
    ]
    _TurnStub.GATE = None
    _TurnStub.BODIES = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TurnStub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", _TurnStub
    finally:
        srv.shutdown()


def _free_closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


_counter = [0]


def _new_session(tenant_id: str = "tenant-q") -> Dict[str, Any]:
    _counter[0] += 1
    return session_store.get_or_create_session(
        tenant_id, f"dwg-queue-{_counter[0]}-{time.time()}")


def _wait_until(predicate, timeout_s: float = 5.0, poll_s: float = 0.02):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return predicate()


def _types(session_id: str) -> List[str]:
    return [e["type"] for e in session_store.recent_events(session_id, 100)]


@pytest.fixture(autouse=True)
def _clean_queue():
    yield
    with turn_runner._queued_lock:
        turn_runner._queued.clear()


# =========================================================================== #
# (a) queued prompt starts at the terminal event; second enqueue refused
# =========================================================================== #
def test_queued_prompt_starts_after_terminal_and_second_is_refused(monkeypatch, turn_stub):
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")
    gate = threading.Event()
    stub.GATE = gate

    sess = _new_session()
    sid = sess["session_id"]
    turn_runner.start_turn("tenant-q", sid, text="first prompt")

    status, queued_id = turn_runner.try_enqueue_turn("tenant-q", sid, text="second prompt")
    assert (status, bool(queued_id)) == ("queued", True)
    # durable record, cap enforced
    assert "turn_queued" in _types(sid)
    assert turn_runner.try_enqueue_turn("tenant-q", sid, text="third") == ("full", None)
    # the queued prompt must NOT have started while turn 1 is live
    assert sum(1 for t in _types(sid) if t == "turn_started") == 1

    gate.set()  # let turn 1 emit its terminal event

    ok = _wait_until(lambda: sum(1 for t in _types(sid) if t == "turn_started") == 2)
    assert ok, f"queued prompt never started; events: {_types(sid)}"
    started = [e for e in session_store.recent_events(sid, 100) if e["type"] == "turn_started"]
    assert started[1]["data"].get("text") == "second prompt"
    assert turn_runner.queued_prompt(sid) is None
    # both turns end cleanly (stub's gate is already open for turn 2)
    assert _wait_until(lambda: session_store.get_session(sid)["active_turn_id"] is None)


# =========================================================================== #
# (b) the enqueue/terminal handoff race is closed
# =========================================================================== #
def test_enqueue_on_a_free_session_starts_immediately(monkeypatch, turn_stub):
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")

    sess = _new_session()
    sid = sess["session_id"]
    # No active turn: the post-insert fence check must start the prompt itself
    # (this is exactly the state a request lands in when the busy turn
    # terminalized between the CAS loss and the enqueue registration).
    status, _ = turn_runner.try_enqueue_turn("tenant-q", sid, text="raced prompt")
    assert status == "queued"
    ok = _wait_until(lambda: "turn_started" in _types(sid))
    assert ok, f"handoff race not closed; events: {_types(sid)}"
    assert turn_runner.queued_prompt(sid) is None


# =========================================================================== #
# (c) the orphan cancel path kicks the queue
# =========================================================================== #
def test_orphan_cancel_kicks_queued_prompt(monkeypatch, turn_stub):
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")

    sess = _new_session()
    sid = sess["session_id"]
    # An orphaned active turn: CAS held, but no relay in this process.
    assert session_store.try_begin_turn(sid, "orphan-turn-1", 300)
    status, _ = turn_runner.try_enqueue_turn("tenant-q", sid, text="after cancel")
    assert status == "queued"

    assert turn_runner.request_cancel("tenant-q", sid, "orphan-turn-1") == "cancelled"

    ok = _wait_until(
        lambda: any(e["type"] == "turn_started" and e["data"].get("text") == "after cancel"
                    for e in session_store.recent_events(sid, 100)))
    assert ok, f"orphan cancel did not kick; events: {_types(sid)}"
    assert turn_runner.queued_prompt(sid) is None


# =========================================================================== #
# (d) a rejected kick closes the transcript it opened and empties the slot
# =========================================================================== #
def test_rejected_kick_appends_error_terminal_and_drops_prompt(monkeypatch):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL",
                       f"http://127.0.0.1:{_free_closed_port()}")
    monkeypatch.setenv("TURN_MAX_S", "30")

    sess = _new_session()
    sid = sess["session_id"]
    assert session_store.try_begin_turn(sid, "orphan-turn-2", 300)
    status, _ = turn_runner.try_enqueue_turn("tenant-q", sid, text="doomed prompt")
    assert status == "queued"

    assert turn_runner.request_cancel("tenant-q", sid, "orphan-turn-2") == "cancelled"

    events = session_store.recent_events(sid, 100)
    started = [e for e in events if e["type"] == "turn_started"
               and e["data"].get("text") == "doomed prompt"]
    assert started, f"queued start never appended turn_started; events: {_types(sid)}"
    turn_id = started[0]["turn_id"]
    errors = [e for e in events if e["type"] == "error" and e["turn_id"] == turn_id]
    assert errors, ("rejected queued start left a dangling turn_started "
                    f"(no terminal error); events: {_types(sid)}")
    assert errors[0]["data"]["error"]["error_code"] == ErrorCode.BROKER_UNREACHABLE
    assert turn_runner.queued_prompt(sid) is None
    # and the failed start released the CAS — the session is not wedged
    assert session_store.get_session(sid)["active_turn_id"] is None


# =========================================================================== #
# (e) a kick that loses the CAS re-inserts the prompt
# =========================================================================== #
def test_busy_kick_reinserts_the_prompt(monkeypatch):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://127.0.0.1:9")  # never reached
    sess = _new_session()
    sid = sess["session_id"]
    assert session_store.try_begin_turn(sid, "orphan-turn-3", 300)
    status, queued_id = turn_runner.try_enqueue_turn("tenant-q", sid, text="parked")
    assert status == "queued"

    # A racing kick (e.g. a stale terminal thread) while the CAS is genuinely
    # held: start_turn raises TurnBusy, and the prompt must survive in the
    # slot — otherwise "that turn's terminal will kick" points at nothing.
    turn_runner._kick_queued(sid)

    parked = turn_runner.queued_prompt(sid)
    assert parked is not None and parked["queued_id"] == queued_id, (
        "TurnBusy kick dropped the queued prompt instead of re-inserting it")
    session_store.end_turn(sid, "orphan-turn-3")


# =========================================================================== #
# route level
# =========================================================================== #
@pytest.fixture()
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from envelopes import install_error_handlers

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(sessions_router.router)
    return TestClient(app, raise_server_exceptions=False)


def _h(tenant: str) -> dict:
    return {"X-Tenant-Id": tenant}


def _route_session(client, tenant: str) -> str:
    _counter[0] += 1
    r = client.post("/api/sessions", json={"drawing_id": f"dwg-rq-{_counter[0]}"},
                    headers=_h(tenant))
    assert r.status_code < 300, r.text
    return r.json()["session_id"]


def test_route_busy_queue_true_parks_then_second_bounces(client, monkeypatch):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://127.0.0.1:9")
    sid = _route_session(client, "tenant-rq")
    assert session_store.try_begin_turn(sid, "orphan-rq-1", 300)
    try:
        # (f) parked
        r = client.post(f"/api/sessions/{sid}/messages",
                        json={"text": "park me", "queue": True}, headers=_h("tenant-rq"))
        assert r.status_code == 202, r.text
        assert r.json()["status"] == "queued" and r.json()["queued_id"]
        assert "turn_queued" in _types(sid)
        # (g) second refused with the ordinary busy envelope
        r2 = client.post(f"/api/sessions/{sid}/messages",
                         json={"text": "again", "queue": True}, headers=_h("tenant-rq"))
        assert r2.status_code == 409, r2.text
        assert r2.json()["error"]["error_code"] == ErrorCode.TURN_IN_PROGRESS
        assert r2.json()["error"]["retryable"] is True
        # (h) opt-in pin: no queue flag -> plain busy 409, nothing parked extra
        r3 = client.post(f"/api/sessions/{sid}/messages",
                         json={"text": "no flag"}, headers=_h("tenant-rq"))
        assert r3.status_code == 409
        assert r3.json()["error"]["error_code"] == ErrorCode.TURN_IN_PROGRESS
    finally:
        with turn_runner._queued_lock:
            turn_runner._queued.pop(sid, None)
        session_store.end_turn(sid, "orphan-rq-1")


def test_route_queue_with_confirm_or_grant_is_400_before_consume(client):
    sid = _route_session(client, "tenant-rq2")
    # queue + confirm -> 400, and the approval was never consumed
    _counter[0] += 1
    cid = f"confirm-q-{_counter[0]}"
    sess = session_store.get_session(sid)
    session_store.create_approval(
        cid, sid, "tenant-rq2", turn_id="t-x", tool="write_home_run",
        params={"length_ft": 1}, capability="drawing.write", rationale="r",
        kind="proposed_run", payload={"dwg": sess["drawing_id"]}, ttl_s=300)
    session_store.decide_approval(cid, True)

    r = client.post(f"/api/sessions/{sid}/messages",
                    json={"confirm": {"confirmationId": cid, "approved": True},
                          "queue": True},
                    headers=_h("tenant-rq2"))
    assert r.status_code == 400, r.text
    assert "cannot be queued" in r.json()["error"]["message"]
    # the 400 fired BEFORE the consume: the approval is still consumable
    consumed = session_store.consume_approval(cid, sid, "tenant-rq2")
    assert consumed["approved"] is True

    r2 = client.post(f"/api/sessions/{sid}/messages",
                     json={"text": "hi", "queue": True,
                           "credential_grant": {"kind": "api_key",
                                                "api_key": "k" * 30}},
                     headers=_h("tenant-rq2"))
    assert r2.status_code == 400, r2.text
    assert "cannot be queued" in r2.json()["error"]["message"]
