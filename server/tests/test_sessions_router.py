"""
Binary acceptance for the app<->harness integration on the UNIFIED §2.1 wire
(census #12 chip-spine-sessions-routers rewrite — the §18-era proxy suite this
file replaced monkeypatched internals that no longer exist, e.g.
routers.sessions._SESSIONS).

What makes this suite distinct from its two live siblings:

  * tests/test_sessions_routes.py pins the CLIENT-facing byte shapes with
    turn_runner.start_turn monkeypatched — it never crosses the app->harness
    hop.
  * tests/test_turn_runner.py pins the engine alone — no router in front.
  * THIS suite drives the real router -> real turn_runner -> a stub harness
    speaking actual HTTP/1.1 chunked `application/x-ndjson` on POST /turn, so
    the assertions that need BOTH layers live here:

      - env preference order: `LEAF_CONVERSE_HARNESS_URL` wins,
        `LEAF_AUTHOR_HARNESS_URL` is the fallback (both exported by
        scripts/start-leaf.py; single-var deploys keep working);
      - the frozen ConverseTurnInput body on the wire: {tenant_id, session_id,
        turn_id, drawing_id, messages, text|confirm} and NOTHING else — no
        ContextPacket/packet field (deliberate contract decision, census #12:
        ContextPacket has no live caller; chip 5 freezes this), no
        classifier_hint (durable-log-only);
      - the confirm resume carries the DURABLE approval row's proposal and
        stored `approved`, never the client's claim;
      - the NDJSON relay lands events durably (transcript/stream serve them),
        creates decidable approval rows from `proposed_run`, and releases the
        one-turn-per-session CAS on every terminal path;
      - immediate harness rejections (401/429/5xx/refused) map to the §18.4
        ErrorCode values, which are themselves pinned here verbatim.

Run:  cd server && python -m pytest tests/test_sessions_router.py -q
"""
from __future__ import annotations

# Cache the STDLIB `platform` module BEFORE SERVER_DIR lands on sys.path
# (repo-root platform/ package shadows it; mirrors test_wave4/5).
import platform as _stdlib_platform  # noqa: E402
_stdlib_platform.python_implementation()

import http.server  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import socket  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Dict, List, Optional, Tuple  # noqa: E402

import pytest  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

# route the sessions SQLite DB to a throwaway dir BEFORE session_store is
# imported anywhere (module reads SESSIONS_DB once, at import time — same
# posture as tests/test_turn_runner.py / tests/test_sessions_routes.py).
os.environ.setdefault(
    "SESSIONS_DB",
    str(Path(tempfile.mkdtemp(prefix="sessrouter-sessions-")) / "sessions.db"),
)

import entitlements  # noqa: E402
import session_store  # noqa: E402
from envelopes import DEFAULT_HTTP_STATUS, ErrorCode  # noqa: E402
from routers import agent as agent_router  # noqa: E402
from routers import sessions as sessions_router  # noqa: E402


# =========================================================================== #
# stub harness — real HTTP/1.1 chunked application/x-ndjson on POST /turn.
# Per-SERVER state (not class-level like test_turn_runner's stub) so the env-
# preference tests can run TWO independent stubs in one test.
# =========================================================================== #
class _StubState:
    def __init__(self) -> None:
        self.bodies: List[Dict[str, Any]] = []   # every POST /turn body, in order
        self.hits = 0
        self.immediate: Optional[Tuple[int, Dict[str, Any]]] = None
        self.scripts: List[List[Dict[str, Any]]] = []  # per-call event scripts (FIFO)
        self.release = threading.Event()         # stream is gated on this
        self.release.set()                       # default: stream immediately


DEFAULT_SCRIPT = [{"type": "turn_complete", "data": {"stop_reason": "end_turn"}}]


class _StubServer(http.server.ThreadingHTTPServer):
    def __init__(self, state: _StubState):
        super().__init__(("127.0.0.1", 0), _StubHandler)
        self.state = state


class _StubHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle(self):
        # the app side aborts keep-alive sockets freely (immediate-rejection
        # paths call resp.close()); that's expected, not per-test stderr noise.
        try:
            super().handle()
        except (ConnectionError, OSError):
            pass

    def do_POST(self):  # noqa: N802
        state: _StubState = self.server.state  # type: ignore[attr-defined]
        state.hits += 1
        length = int(self.headers.get("content-length", 0) or 0)
        try:
            state.bodies.append(json.loads(self.rfile.read(length) or b"{}"))
        except Exception:  # noqa: BLE001
            state.bodies.append({})

        if state.immediate is not None:
            status, body = state.immediate
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        state.release.wait(timeout=30)
        script = state.scripts.pop(0) if state.scripts else list(DEFAULT_SCRIPT)
        for ev in script:
            raw = (json.dumps(ev) + "\n").encode("utf-8")
            self.wfile.write(f"{len(raw):x}\r\n".encode("ascii") + raw + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def log_message(self, *a):  # silence
        return


def _spawn_stub() -> Tuple[str, _StubState, _StubServer]:
    state = _StubState()
    srv = _StubServer(state)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", state, srv


@pytest.fixture
def stub():
    url, state, srv = _spawn_stub()
    try:
        yield url, state
    finally:
        state.release.set()  # never leave a handler blocked on teardown
        srv.shutdown()


@pytest.fixture
def second_stub():
    url, state, srv = _spawn_stub()
    try:
        yield url, state
    finally:
        state.release.set()
        srv.shutdown()


def _free_closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# =========================================================================== #
# fixtures — hermetic agent state + a local mount of the two routers under test
# =========================================================================== #
@pytest.fixture(autouse=True)
def agent_env(tmp_path, monkeypatch):
    """Hermetic agent/gate state (the approvals endpoint's gate-bridge probe
    reads LEAF_AGENT_APPROVALS_DIR) + a clean harness-env slate so ambient
    dev-machine vars can never leak into the preference-order tests."""
    monkeypatch.delenv("LEAF_CONVERSE_HARNESS_URL", raising=False)
    monkeypatch.delenv("LEAF_AUTHOR_HARNESS_URL", raising=False)
    monkeypatch.delenv("LEAF_AGENT_POLICY_FILE", raising=False)
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.delenv("SESSIONS_APPROVAL_TTL_S", raising=False)
    monkeypatch.setenv("LEAF_AGENT_KILL_FILE", str(tmp_path / "agent.disabled"))
    monkeypatch.setenv("LEAF_AGENT_APPROVALS_DIR", str(tmp_path / "approvals"))
    monkeypatch.setenv("LEAF_AGENT_GRANTS_FILE", str(tmp_path / "grants.json"))
    monkeypatch.setenv("LEAF_AGENT_RATE_FILE", str(tmp_path / "rate.json"))
    monkeypatch.setenv("LEAF_AGENT_AUDIT", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LEAF_AGENT_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("LEAF_AGENT_TENANTS_FILE", str(tmp_path / "agent_tenants.json"))
    monkeypatch.setenv("TURN_MAX_S", "20")  # keep watchdog threads bounded
    yield tmp_path


@pytest.fixture
def wired(stub, monkeypatch):
    """The standard config: LEAF_CONVERSE_HARNESS_URL -> the stub, author var
    unset — the exact env start-leaf.py exports for the app."""
    url, state = stub
    monkeypatch.setenv("LEAF_CONVERSE_HARNESS_URL", url)
    return url, state


@pytest.fixture
def client():
    """Local mount of the two routers this suite owns assertions for
    (sessions wire + the approvals sibling). The PRODUCTION mount is proven by
    tests/test_agent_e2e.py."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from envelopes import install_error_handlers

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(sessions_router.router)
    app.include_router(agent_router.router)
    return TestClient(app, raise_server_exceptions=False)


def _h(tenant: str) -> dict:
    return {"X-Tenant-Id": tenant}


_counter = [0]


def _new_session(tenant_id: str = "tenant-a") -> Dict[str, Any]:
    _counter[0] += 1
    return session_store.get_or_create_session(tenant_id, f"dwg-router-{_counter[0]}")


def _post_text(client, sess, text="hi", hint=None):
    body: Dict[str, Any] = {"text": text}
    if hint is not None:
        body["classifier_hint"] = hint
    return client.post(f"/api/sessions/{sess['session_id']}/messages",
                       json=body, headers=_h(sess["tenant_id"]))


def _wait_until(predicate, timeout_s: float = 5.0, poll_s: float = 0.02):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return predicate()


def _terminal_count(session_id: str) -> int:
    events = session_store.recent_events(session_id, 200)
    return sum(1 for ev in events if ev["type"] in ("turn_complete", "error"))


def _wait_terminal(session_id: str, n: int = 1) -> None:
    assert _wait_until(lambda: _terminal_count(session_id) >= n), (
        f"relay never landed terminal event #{n} for {session_id}"
    )


# =========================================================================== #
# §18.4 ErrorCode values (CONTRACT-ADDENDUM §18.4) — shipped verbatim
# =========================================================================== #
def test_section_18_4_error_codes_shipped_verbatim():
    """The four §18.4 codes ship with their exact lowercase wire values (the
    frozen client-side errorCode strings converse.js switches on) and are
    members of the frozen enum's ALL tuple."""
    assert ErrorCode.TURN_IN_PROGRESS == "turn_in_progress"
    assert ErrorCode.SESSION_NOT_FOUND == "session_not_found"
    assert ErrorCode.LLM_QUOTA_EXHAUSTED == "llm_quota_exhausted"
    assert ErrorCode.LLM_RATE_LIMITED == "llm_rate_limited"
    for code in (ErrorCode.TURN_IN_PROGRESS, ErrorCode.SESSION_NOT_FOUND,
                 ErrorCode.LLM_QUOTA_EXHAUSTED, ErrorCode.LLM_RATE_LIMITED):
        assert code in ErrorCode.ALL


def test_section_18_4_default_http_statuses():
    """§18.4's HTTP column, plus the additive sessions-wire sibling
    confirmation_expired (410) that shipped in the same block."""
    assert DEFAULT_HTTP_STATUS[ErrorCode.TURN_IN_PROGRESS] == 409
    assert DEFAULT_HTTP_STATUS[ErrorCode.SESSION_NOT_FOUND] == 404
    assert DEFAULT_HTTP_STATUS[ErrorCode.LLM_QUOTA_EXHAUSTED] == 429
    assert DEFAULT_HTTP_STATUS[ErrorCode.LLM_RATE_LIMITED] == 429
    assert ErrorCode.CONFIRMATION_EXPIRED == "confirmation_expired"
    assert DEFAULT_HTTP_STATUS[ErrorCode.CONFIRMATION_EXPIRED] == 410


# =========================================================================== #
# env preference order — LEAF_CONVERSE_HARNESS_URL wins, author is fallback
# =========================================================================== #
def test_harness_url_prefers_leaf_converse_harness_url(client, stub, second_stub, monkeypatch):
    """Both vars set -> the turn goes to LEAF_CONVERSE_HARNESS_URL and the
    author sidecar sees ZERO traffic (start-leaf.py exports both; the turn
    engine must pick the converse one)."""
    converse_url, converse_state = stub
    author_url, author_state = second_stub
    monkeypatch.setenv("LEAF_CONVERSE_HARNESS_URL", converse_url)
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", author_url)

    sess = _new_session()
    r = _post_text(client, sess)
    assert r.status_code == 202, r.text
    _wait_terminal(sess["session_id"])
    assert converse_state.hits == 1
    assert author_state.hits == 0


def test_harness_url_falls_back_to_leaf_author_harness_url(client, stub, monkeypatch):
    """Only the author var set (single-sidecar deploys, docker-compose today)
    -> the turn still dispatches there. The fallback must survive."""
    url, state = stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)

    sess = _new_session()
    r = _post_text(client, sess)
    assert r.status_code == 202, r.text
    _wait_terminal(sess["session_id"])
    assert state.hits == 1


def test_neither_env_configured_502_names_the_env_pair(client, stub, monkeypatch):
    """Both vars unset (the autouse fixture's clean slate) -> immediate 502
    BROKER_UNREACHABLE whose message names BOTH env vars (operator-actionable),
    and the turn CAS is released — the next message, once wired, dispatches
    instead of 409ing."""
    url, state = stub
    sess = _new_session()
    r = _post_text(client, sess)
    assert r.status_code == 502, r.text
    body = r.json()
    assert body["error"]["error_code"] == "BROKER_UNREACHABLE"
    assert "LEAF_CONVERSE_HARNESS_URL" in body["error"]["message"]
    assert "LEAF_AUTHOR_HARNESS_URL" in body["error"]["message"]
    assert body["error"]["retryable"] is True

    monkeypatch.setenv("LEAF_CONVERSE_HARNESS_URL", url)
    r2 = _post_text(client, sess)
    assert r2.status_code == 202, r2.text  # CAS was released, not leaked
    _wait_terminal(sess["session_id"])


# =========================================================================== #
# the frozen ConverseTurnInput body on the wire
# =========================================================================== #
def test_forwarded_turn_body_is_frozen_shape_no_packet(client, wired):
    """POST /turn carries EXACTLY the frozen ConverseTurnInput fields for a
    text turn. Pins the deliberate census #12 contract decision: NO
    ContextPacket rides this wire (ContextPacket has no live caller; the
    frozen shape has no packet field — chip 5 freezes this)."""
    url, state = wired
    sess = _new_session()
    r = _post_text(client, sess, text="add a panel")
    assert r.status_code == 202, r.text
    _wait_terminal(sess["session_id"])

    body = state.bodies[0]
    assert set(body) == {"tenant_id", "session_id", "turn_id", "drawing_id",
                        "messages", "text"}
    assert body["tenant_id"] == sess["tenant_id"]
    assert body["session_id"] == sess["session_id"]
    assert body["turn_id"] == r.json()["turn_id"]
    assert body["drawing_id"] == sess["drawing_id"]
    assert body["text"] == "add a panel"
    assert body["messages"] == []  # first turn: no prior context


def test_classifier_hint_durable_in_turn_started_but_never_on_the_wire(client, wired):
    """classifier_hint is recorded on the durable turn_started event (the
    transcript source) but is NOT part of the frozen ConverseTurnInput — it
    must never reach the harness body."""
    url, state = wired
    sess = _new_session()
    hint = {"lane": "run", "tool": "count-by-layer", "confidence": 0.62}
    r = _post_text(client, sess, text="how many panels?", hint=hint)
    assert r.status_code == 202, r.text
    _wait_terminal(sess["session_id"])

    assert "classifier_hint" not in state.bodies[0]
    assert "classifierHint" not in state.bodies[0]
    started = [ev for ev in session_store.recent_events(sess["session_id"], 50)
               if ev["type"] == "turn_started"]
    assert started and started[0]["data"]["classifier_hint"] == hint
    assert started[0]["data"]["text"] == "how many panels?"


def _seed_decided_approval(sess, *, approved: bool, tool="write_home_run",
                           params=None, capability="drawing.write") -> str:
    _counter[0] += 1
    cid = f"confirm-router-{_counter[0]}"
    session_store.create_approval(
        cid, sess["session_id"], sess["tenant_id"], turn_id=f"turn-{_counter[0]}",
        tool=tool, params=params if params is not None else {"length_ft": 12},
        capability=capability, rationale="adds a home-run", kind="run_capability",
        payload=None, ttl_s=300,
    )
    session_store.decide_approval(cid, approved, by=sess["tenant_id"])
    return cid


def test_confirm_forwards_durable_proposal_not_client_shape(client, wired):
    """The confirm resume body carries the frozen snake_case confirm built from
    the DURABLE approval row ({confirmation_id, approved, proposal{tool,
    params, capability}}) — never the client's camelCase shape, and no text."""
    url, state = wired
    sess = _new_session()
    cid = _seed_decided_approval(sess, approved=True)

    r = client.post(f"/api/sessions/{sess['session_id']}/messages",
                    json={"confirm": {"confirmationId": cid, "approved": True}},
                    headers=_h(sess["tenant_id"]))
    assert r.status_code == 202, r.text
    _wait_terminal(sess["session_id"])

    body = state.bodies[0]
    assert "text" not in body
    assert body["confirm"] == {
        "confirmation_id": cid,
        "approved": True,
        "proposal": {"tool": "write_home_run", "params": {"length_ft": 12},
                     "capability": "drawing.write"},
    }
    assert "confirmationId" not in json.dumps(body)  # client shape never leaks


def test_confirm_forwarded_approved_is_stored_value_not_clients(client, wired):
    """Row decided approved=False; the client lies with approved=True on the
    confirm message — the WIRE must carry the stored False."""
    url, state = wired
    sess = _new_session()
    cid = _seed_decided_approval(sess, approved=False)

    r = client.post(f"/api/sessions/{sess['session_id']}/messages",
                    json={"confirm": {"confirmationId": cid, "approved": True}},
                    headers=_h(sess["tenant_id"]))
    assert r.status_code == 202, r.text
    _wait_terminal(sess["session_id"])
    assert state.bodies[0]["confirm"]["approved"] is False


def test_prior_turn_context_folded_into_messages(client, wired):
    """Turn 2's body.messages folds turn 1 into a bounded {user, assistant}
    pair (assistant = concatenated text_delta of the COMPLETED turn); the
    current turn's own text is excluded from its prior context."""
    url, state = wired
    state.scripts.append([
        {"type": "text_delta", "data": {"text": "Twelve "}},
        {"type": "text_delta", "data": {"text": "panels."}},
        {"type": "turn_complete", "data": {"stop_reason": "end_turn"}},
    ])
    sess = _new_session()
    r1 = _post_text(client, sess, text="how many panels?")
    assert r1.status_code == 202, r1.text
    _wait_terminal(sess["session_id"], 1)

    r2 = _post_text(client, sess, text="add one more")
    assert r2.status_code == 202, r2.text
    _wait_terminal(sess["session_id"], 2)

    body2 = state.bodies[1]
    assert body2["text"] == "add one more"
    assert body2["messages"] == [
        {"role": "user", "text": "how many panels?"},
        {"role": "assistant", "text": "Twelve panels."},
    ]


# =========================================================================== #
# NDJSON relay -> durable store -> transcript/stream, CAS lifecycle
# =========================================================================== #
def test_relay_lands_stream_events_durably_ascending(client, wired):
    url, state = wired
    state.scripts.append([
        {"type": "text_delta", "data": {"text": "On it."}},
        {"type": "tool_call", "data": {"tool": "count-by-layer", "args_summary": "layer=PV"}},
        {"type": "turn_complete", "data": {"stop_reason": "end_turn"}},
    ])
    sess = _new_session()
    r = _post_text(client, sess)
    assert r.status_code == 202, r.text
    _wait_terminal(sess["session_id"])

    tr = client.get(f"/api/sessions/{sess['session_id']}/transcript?limit=50",
                    headers=_h(sess["tenant_id"]))
    assert tr.status_code == 200, tr.text
    events = tr.json()["events"]
    assert [ev["type"] for ev in events] == \
        ["turn_started", "text_delta", "tool_call", "turn_complete"]
    seqs = [ev["seq"] for ev in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert events[1]["data"] == {"text": "On it."}  # relayed verbatim
    assert events[2]["data"] == {"tool": "count-by-layer", "args_summary": "layer=PV"}


def test_stream_route_replays_relayed_events_after_seq(client, wired, monkeypatch):
    monkeypatch.setattr(sessions_router, "STREAM_POLL_S", 0.01)
    monkeypatch.setattr(sessions_router, "STREAM_DEADLINE_S", 5.0)
    url, state = wired
    state.scripts.append([
        {"type": "text_delta", "data": {"text": "hello"}},
        {"type": "turn_complete", "data": {"stop_reason": "end_turn"}},
    ])
    sess = _new_session()
    sid = sess["session_id"]
    assert _post_text(client, sess).status_code == 202
    _wait_terminal(sid)

    started_seq = next(ev["seq"] for ev in session_store.recent_events(sid, 50)
                       if ev["type"] == "turn_started")
    seen = []
    with client.stream("GET", f"/api/sessions/{sid}/stream?after_seq={started_seq}",
                       headers=_h(sess["tenant_id"])) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line.startswith("data: "):
                seen.append(json.loads(line[len("data: "):]))
                if len(seen) >= 2:
                    break
    assert [ev["type"] for ev in seen] == ["text_delta", "turn_complete"]
    assert all(ev["seq"] > started_seq for ev in seen)


def test_second_turn_allowed_after_terminal(client, wired):
    """turn_complete releases the CAS — the next message dispatches (202)."""
    url, state = wired
    sess = _new_session()
    assert _post_text(client, sess).status_code == 202
    _wait_terminal(sess["session_id"], 1)
    assert _post_text(client, sess).status_code == 202
    _wait_terminal(sess["session_id"], 2)
    assert state.hits == 2


def test_concurrent_message_409_turn_in_progress(client, wired):
    """While a turn's stream is open, a second message is rejected-not-queued
    with 409 turn_in_progress (retryable); after the turn completes, the
    session accepts messages again."""
    url, state = wired
    state.release.clear()  # gate the stream open
    sess = _new_session()
    r1 = _post_text(client, sess, text="first")
    assert r1.status_code == 202, r1.text

    r2 = _post_text(client, sess, text="second while busy")
    assert r2.status_code == 409, r2.text
    body = r2.json()
    assert body["error"]["error_code"] == "turn_in_progress"
    assert body["error"]["retryable"] is True

    state.release.set()
    _wait_terminal(sess["session_id"], 1)
    r3 = _post_text(client, sess, text="third after done")
    assert r3.status_code == 202, r3.text
    _wait_terminal(sess["session_id"], 2)


def test_proposed_run_relay_creates_decidable_approval_row(client, wired, monkeypatch):
    """A spine turn emits proposed_run ALONE (no confirmation_required pair) —
    the relay must create the approvals row with the proposal metadata BEFORE
    the chip event is published, so the visible chip structurally implies a
    decidable row: POST /api/agent/approvals/{cid} works immediately.

    The BEFORE is pinned deterministically: append_event is wrapped so that AT
    the moment the proposed_run event is published, the row's existence is
    captured — reversing the relay's create-row/publish order fails here, not
    probabilistically."""
    url, state = wired
    cid = "confirm-spine-chip-1"
    state.scripts.append([
        {"type": "proposed_run", "data": {
            "confirmation_id": cid, "tool": "add-panel",
            "params": {"roof": "south"}, "capability": "drawing.write",
            "rationale": "user asked for a panel"}},
        {"type": "turn_complete", "data": {"stop_reason": "awaiting_approval"}},
    ])
    row_at_publication = {}
    real_append = session_store.append_event

    def _spy(session_id, turn_id, ev_type, data):
        if ev_type == "proposed_run":
            row_at_publication["exists"] = session_store.get_approval(cid) is not None
        return real_append(session_id, turn_id, ev_type, data)

    monkeypatch.setattr(session_store, "append_event", _spy)
    sess = _new_session()
    assert _post_text(client, sess, text="add a panel").status_code == 202
    _wait_terminal(sess["session_id"])
    assert row_at_publication.get("exists") is True, (
        "approvals row must exist BEFORE the proposed_run event is published")

    row = session_store.get_approval(cid)
    assert row is not None, "relay must materialize the approvals row"
    assert row["decided"] is False and row["consumed"] is False
    assert row["tool"] == "add-panel" and row["params"] == {"roof": "south"}
    assert row["capability"] == "drawing.write" and row["kind"] == "run_capability"

    r = client.post(f"/api/agent/approvals/{cid}", json={"approved": True},
                    headers=_h(sess["tenant_id"]))
    assert r.status_code == 200, r.text
    assert r.json()["resolved"] is True and r.json()["approved"] is True
    assert session_store.get_approval(cid)["decided"] is True

    resolved = [ev for ev in session_store.recent_events(sess["session_id"], 50)
                if ev["type"] == "confirmation_resolved"]
    assert resolved and resolved[0]["data"]["confirmation_id"] == cid


def test_approval_row_ttl_follows_sessions_approval_ttl_s(client, wired, monkeypatch):
    monkeypatch.setenv("SESSIONS_APPROVAL_TTL_S", "123")
    url, state = wired
    cid = "confirm-ttl-probe"
    state.scripts.append([
        {"type": "proposed_run", "data": {"confirmation_id": cid, "tool": "t",
                                          "params": {}, "capability": "drawing.write",
                                          "rationale": "r"}},
        {"type": "turn_complete", "data": {"stop_reason": "awaiting_approval"}},
    ])
    sess = _new_session()
    assert _post_text(client, sess).status_code == 202
    _wait_terminal(sess["session_id"])

    row = session_store.get_approval(cid)
    assert row is not None
    ttl = row["expires_at"] - row["created_at"]
    assert 122.0 <= ttl <= 125.0


# =========================================================================== #
# immediate harness rejections -> §18.4 mappings through the router
# =========================================================================== #
def test_immediate_401_maps_grant_required_with_top_level_flag(client, wired):
    url, state = wired
    state.immediate = (401, {"message": "tenant has no linked Claude grant"})
    sess = _new_session()
    r = _post_text(client, sess)
    assert r.status_code == 401, r.text
    body = r.json()
    assert body["error"]["error_code"] == "GRANT_REQUIRED"
    assert body["grant_required"] is True
    assert "linked Claude grant" in body["error"]["message"]

    state.immediate = None  # CAS released: the next message dispatches
    assert _post_text(client, sess).status_code == 202
    _wait_terminal(sess["session_id"])


def test_immediate_429_quota_exhausted_code_passes_through(client, wired):
    url, state = wired
    state.immediate = (429, {"errorCode": "llm_quota_exhausted", "message": "hard cap"})
    sess = _new_session()
    r = _post_text(client, sess)
    assert r.status_code == 429, r.text
    assert r.json()["error"]["error_code"] == "llm_quota_exhausted"


def test_immediate_429_rate_limited_code_passes_through(client, wired):
    url, state = wired
    state.immediate = (429, {"errorCode": "llm_rate_limited", "message": "slow down"})
    sess = _new_session()
    r = _post_text(client, sess)
    assert r.status_code == 429, r.text
    assert r.json()["error"]["error_code"] == "llm_rate_limited"


def test_immediate_429_unknown_code_defaults_to_rate_limited(client, wired):
    """A 429 whose body carries no recognizable errorCode must default to the
    SHORT horizon (llm_rate_limited), never quota-exhausted."""
    url, state = wired
    state.immediate = (429, {"error": "too_many_requests"})
    sess = _new_session()
    r = _post_text(client, sess)
    assert r.status_code == 429, r.text
    assert r.json()["error"]["error_code"] == "llm_rate_limited"


def test_connection_refused_maps_broker_unreachable(client, monkeypatch):
    monkeypatch.setenv("LEAF_CONVERSE_HARNESS_URL",
                       f"http://127.0.0.1:{_free_closed_port()}")
    sess = _new_session()
    r = _post_text(client, sess)
    assert r.status_code == 502, r.text
    body = r.json()
    assert body["error"]["error_code"] == "BROKER_UNREACHABLE"
    assert body["error"]["retryable"] is True


def test_unexpected_harness_5xx_maps_broker_unreachable_502(client, wired):
    url, state = wired
    state.immediate = (500, {"message": "harness exploded"})
    sess = _new_session()
    r = _post_text(client, sess)
    assert r.status_code == 502, r.text
    body = r.json()
    assert body["error"]["error_code"] == "BROKER_UNREACHABLE"
    assert "harness exploded" in body["error"]["message"]


# =========================================================================== #
# router guards fire BEFORE any harness traffic
# =========================================================================== #
def test_unknown_and_cross_tenant_404_identical_no_stub_traffic(client, wired):
    url, state = wired
    sess = _new_session(tenant_id="tenant-owner")
    r_unknown = client.post("/api/sessions/no-such-session/messages",
                            json={"text": "hi"}, headers=_h("tenant-owner"))
    r_foreign = client.post(f"/api/sessions/{sess['session_id']}/messages",
                            json={"text": "hi"}, headers=_h("tenant-intruder"))
    for r in (r_unknown, r_foreign):
        assert r.status_code == 404, r.text
        assert r.json()["error"]["error_code"] == "session_not_found"
        assert r.json()["error"]["retryable"] is False
    assert state.hits == 0  # guard fired app-side; nothing forwarded


def test_exactly_one_of_text_confirm_409_no_stub_traffic(client, wired):
    url, state = wired
    sess = _new_session()
    both = client.post(
        f"/api/sessions/{sess['session_id']}/messages",
        json={"text": "hi", "confirm": {"confirmationId": "x", "approved": True}},
        headers=_h(sess["tenant_id"]))
    neither = client.post(f"/api/sessions/{sess['session_id']}/messages",
                          json={}, headers=_h(sess["tenant_id"]))
    assert both.status_code == 409 and neither.status_code == 409
    assert both.json()["error"]["error_code"] == "BAD_PARAMS"
    assert state.hits == 0


def test_entitlement_denied_403_before_any_harness_traffic_or_events(client, wired, monkeypatch):
    """The converse entitlement gate fires before dispatch: standard 403 shape,
    zero harness traffic, and NO turn_started event ever recorded (the denied
    message must not pollute the durable transcript)."""
    url, state = wired
    sess = _new_session(tenant_id="tenant-restricted")
    monkeypatch.setattr(entitlements, "resolve_tier", lambda tenant: "restricted")
    r = _post_text(client, sess)
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["entitlement_required"] is True
    assert body["required"] == "converse"
    assert body["error"]["error_code"] == "ENTITLEMENT_REQUIRED"
    assert state.hits == 0
    assert session_store.recent_events(sess["session_id"], 10) == []


# =========================================================================== #
# "Mount your LLM" — per-session model + bring-your-own credential on the wire
# =========================================================================== #
def _create_session_via_client(client, tenant: str, drawing: str, model=None):
    body = {"drawing_id": drawing}
    if model is not None:
        body["model"] = model
    return client.post("/api/sessions", json=body, headers=_h(tenant))


def test_create_session_persists_and_returns_model(client):
    r = _create_session_via_client(client, "tenant-model", "dwg-model-1", "claude-opus-4-8")
    assert r.status_code == 200, r.text
    assert r.json()["model"] == "claude-opus-4-8"
    # Re-attach (idempotent) reflects the stored model.
    r2 = _create_session_via_client(client, "tenant-model", "dwg-model-1")
    assert r2.json()["session_id"] == r.json()["session_id"]
    assert r2.json()["model"] == "claude-opus-4-8"


def test_create_session_rejects_unknown_model(client):
    r = _create_session_via_client(client, "tenant-model", "dwg-bad-model", "gpt-4o")
    assert r.status_code == 400, r.text
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"


def test_stored_session_model_reaches_the_turn_wire(client, wired):
    """A session created with a model forwards THAT model on POST /turn even when
    the message itself carries none — the env default is thus overridden."""
    url, state = wired
    r = _create_session_via_client(client, "tenant-model", "dwg-wire-1", "claude-haiku-4-5")
    sess = {"session_id": r.json()["session_id"], "tenant_id": "tenant-model",
            "drawing_id": "dwg-wire-1"}
    assert _post_text(client, sess, text="hi").status_code == 202
    _wait_terminal(sess["session_id"])
    assert state.bodies[0]["model"] == "claude-haiku-4-5"


def test_per_turn_model_override_wins_over_stored(client, wired):
    url, state = wired
    r = _create_session_via_client(client, "tenant-model", "dwg-wire-2", "claude-sonnet-5")
    sid = r.json()["session_id"]
    m = client.post(f"/api/sessions/{sid}/messages",
                    json={"text": "hi", "model": "claude-opus-4-8"},
                    headers=_h("tenant-model"))
    assert m.status_code == 202, m.text
    _wait_terminal(sid)
    assert state.bodies[0]["model"] == "claude-opus-4-8"


def test_no_model_anywhere_omits_model_on_the_wire(client, wired):
    """A session + message with no model leaves the wire body model-free, so the
    frozen no-model shape (and the harness env default) is preserved."""
    url, state = wired
    sess = _new_session()
    assert _post_text(client, sess, text="hi").status_code == 202
    _wait_terminal(sess["session_id"])
    assert "model" not in state.bodies[0]


def test_message_rejects_unknown_model(client, wired):
    url, state = wired
    sess = _new_session()
    r = client.post(f"/api/sessions/{sess['session_id']}/messages",
                    json={"text": "hi", "model": "llama-3"}, headers=_h(sess["tenant_id"]))
    assert r.status_code == 400, r.text
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"
    assert state.hits == 0  # rejected before any harness traffic


_BYO_SECRET = "sk-ant-api-BYO-DO-NOT-LEAK-4417"


@pytest.fixture
def behind_trusted_proxy(monkeypatch):
    """Deployment that genuinely sits behind a proxy which OVERWRITES
    X-Forwarded-Proto. Only then may that header stand in for the transport."""
    monkeypatch.setenv("LEAF_TRUST_FORWARDED_PROTO", "1")


def test_byo_credential_over_tls_reaches_wire_but_not_session_state(
    client, wired, behind_trusted_proxy,
):
    """A BYO credential on an https-forwarded request rides the POST /turn body
    (so the runner can use it) but is NEVER written into the durable transcript."""
    url, state = wired
    sess = _new_session()
    r = client.post(
        f"/api/sessions/{sess['session_id']}/messages",
        json={"text": "hi", "credential_grant": {"kind": "api_key", "api_key": _BYO_SECRET}},
        headers={**_h(sess["tenant_id"]), "X-Forwarded-Proto": "https"},
    )
    assert r.status_code == 202, r.text
    _wait_terminal(sess["session_id"])
    # Flowed to the harness…
    assert state.bodies[0]["credential_grant"] == {"kind": "api_key", "api_key": _BYO_SECRET}
    # …but never into serialized session state (transcript events).
    events = session_store.recent_events(sess["session_id"], 200)
    assert _BYO_SECRET not in json.dumps(events)


def test_byo_credential_over_plain_http_is_rejected(client, wired):
    url, state = wired
    sess = _new_session()
    r = client.post(
        f"/api/sessions/{sess['session_id']}/messages",
        json={"text": "hi", "credential_grant": {"kind": "oauth", "oauth_token": _BYO_SECRET}},
        headers=_h(sess["tenant_id"]),  # no X-Forwarded-Proto: https
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"
    assert "TLS" in r.json()["error"]["message"]
    assert state.hits == 0


def test_byo_credential_malformed_is_rejected(client, wired, behind_trusted_proxy):
    url, state = wired
    sess = _new_session()
    r = client.post(
        f"/api/sessions/{sess['session_id']}/messages",
        json={"text": "hi", "credential_grant": {"kind": "api_key"}},  # no token
        headers={**_h(sess["tenant_id"]), "X-Forwarded-Proto": "https"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"
    assert state.hits == 0


def test_forwarded_proto_header_alone_cannot_bypass_the_tls_gate(client, wired):
    """sol-critic review of PR #117, blocker 1. docker-compose publishes this app
    straight to :8130 with no proxy in front, so X-Forwarded-Proto is attacker
    controlled. WITHOUT LEAF_TRUST_FORWARDED_PROTO the header must be ignored
    entirely: a plaintext request claiming https is still refused, and the
    credential never reaches the harness."""
    url, state = wired
    sess = _new_session()
    r = client.post(
        f"/api/sessions/{sess['session_id']}/messages",
        json={"text": "hi", "credential_grant": {"kind": "api_key", "api_key": _BYO_SECRET}},
        headers={**_h(sess["tenant_id"]), "X-Forwarded-Proto": "https"},  # the spoof
    )
    assert r.status_code == 400, r.text
    assert "TLS" in r.json()["error"]["message"]
    assert state.hits == 0
    assert _BYO_SECRET not in json.dumps(state.bodies)


def test_byo_credential_is_never_echoed_by_the_validation_handler(client, wired):
    """A grant of the WRONG TYPE fails pydantic PARSING, which runs before the
    router's own checks — so the TLS gate above cannot protect it. The 422 must
    name the offending field without mirroring the token back, or the secret rides
    into access logs and error monitoring (envelopes.py drops pydantic's `input`
    for exactly this reason)."""
    url, state = wired
    sess = _new_session()
    for bad in (_BYO_SECRET, [{"kind": "api_key", "api_key": _BYO_SECRET}]):
        r = client.post(
            f"/api/sessions/{sess['session_id']}/messages",
            json={"text": "hi", "credential_grant": bad},
            headers={**_h(sess["tenant_id"]), "X-Forwarded-Proto": "https"},
        )
        assert r.status_code == 422, r.text
        assert _BYO_SECRET not in r.text, "the 422 body echoed the caller's credential"
        assert "credential_grant" in r.text, "the 422 must still name the bad field"
    assert state.hits == 0


# --------------------------------------------------------------------------- #
# script runner
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
