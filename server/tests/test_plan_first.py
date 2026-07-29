"""
Plan-first, server half (chip: "In plan-first, execution starts only after the
approval lifecycle completes").

The server's whole contribution is ONE sidecar header: a session whose policy
is `plan_first` sends `x-leaf-approval-policy: plan_first` on the harness POST
(the instant-assignment precedent — consumed pre-runner, never in the
transcript, frozen turn body untouched). The harness half (emptying its
per-turn auto-approval) ships separately; until then the header is ignored and
behavior degrades to confirm_all — the SAFE direction.

Pinned here:
  (a) plan_first validates and persists through POST /api/sessions;
  (b) the header rides the wire exactly when the policy is plan_first, and
      NEVER otherwise (a stray header widening nothing is still a contract);
  (c) plan_first does NOT trigger the auto_approve_reads machinery (a
      drawing.read proposal still waits for a human);
  (d) the frozen turn BODY carries no policy field either way.

Run:  cd server && python -m pytest tests/test_plan_first.py -q
"""
from __future__ import annotations

import http.server
import json
import os
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

os.environ.setdefault(
    "SESSIONS_DB",
    str(Path(tempfile.mkdtemp(prefix="planfirst-sessions-")) / "sessions.db"))
os.environ.setdefault("LEAF_AUTH_LIVE", "0")

import session_policy  # noqa: E402
import session_store  # noqa: E402
import turn_runner  # noqa: E402
from routers import sessions as sessions_router  # noqa: E402


class _TurnStub(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    HEADERS: List[Dict[str, str]] = []
    BODIES: List[Dict[str, Any]] = []

    def do_POST(self):  # noqa: N802
        cls = type(self)
        cls.HEADERS.append({k.lower(): v for k, v in self.headers.items()})
        length = int(self.headers.get("content-length", 0) or 0)
        try:
            cls.BODIES.append(json.loads(self.rfile.read(length) or b"{}"))
        except Exception:
            cls.BODIES.append({})
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        raw = (json.dumps({"type": "turn_complete",
                           "data": {"stop_reason": "end_turn"}}) + "\n").encode()
        self.wfile.write(f"{len(raw):x}\r\n".encode() + raw + b"\r\n0\r\n\r\n")
        self.wfile.flush()

    def log_message(self, *a):
        return


@pytest.fixture
def turn_stub():
    _TurnStub.HEADERS = []
    _TurnStub.BODIES = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TurnStub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", _TurnStub
    finally:
        srv.shutdown()


_counter = [0]


def _new_session(tenant: str = "tenant-pf") -> Dict[str, Any]:
    _counter[0] += 1
    return session_store.get_or_create_session(
        tenant, f"dwg-pf-{_counter[0]}-{time.time()}")


def _wait_done(sid: str, timeout_s: float = 5.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if session_store.get_session(sid)["active_turn_id"] is None:
            return True
        time.sleep(0.02)
    return False


def test_plan_first_validates_and_persists():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from envelopes import install_error_handlers
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(sessions_router.router)
    client = TestClient(app, raise_server_exceptions=False)
    _counter[0] += 1
    r = client.post("/api/sessions",
                    json={"drawing_id": f"dwg-pfr-{_counter[0]}",
                          "policy": "plan_first"},
                    headers={"X-Tenant-Id": "tenant-pfr"})
    assert r.status_code < 300, r.text
    assert r.json()["policy"] == "plan_first"


def test_header_rides_exactly_when_plan_first(monkeypatch, turn_stub):
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")

    plain = _new_session()
    turn_runner.start_turn("tenant-pf", plain["session_id"], text="hi")
    assert _wait_done(plain["session_id"])

    planned = _new_session()
    session_policy.set_policy(planned["session_id"], "tenant-pf", "plan_first")
    turn_runner.start_turn("tenant-pf", planned["session_id"], text="hi")
    assert _wait_done(planned["session_id"])

    assert "x-leaf-approval-policy" not in stub.HEADERS[0], (
        "the sidecar header leaked onto a confirm_all session")
    assert stub.HEADERS[1].get("x-leaf-approval-policy") == "plan_first"
    # the frozen turn BODY carries no policy field either way
    assert "policy" not in stub.BODIES[0] and "policy" not in stub.BODIES[1]


def test_plan_first_never_triggers_read_auto_approval(monkeypatch, turn_stub):
    url, stub = turn_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "30")
    monkeypatch.setattr(turn_runner.agent_gate, "read_pending_strict",
                        lambda cid: (None, "absent"))
    sess = _new_session()
    sid = sess["session_id"]
    session_policy.set_policy(sid, "tenant-pf", "plan_first")
    session_store.create_approval(
        confirmation_id="cid-pf", session_id=sid, tenant_id="tenant-pf",
        turn_id="t-pf", tool="panel_count", params={},
        capability="drawing.read", rationale="r", kind="run_capability",
        payload={"dwg": sess["drawing_id"]}, ttl_s=600)

    turn_runner._auto_confirm_reads(
        "tenant-pf", sid, {"cid-pf": {"capability": "drawing.read"}}, None, "demo")

    time.sleep(0.15)
    assert session_store.get_approval("cid-pf")["decided"] is False, (
        "plan_first auto-decided a read — it must confirm EVERYTHING")
