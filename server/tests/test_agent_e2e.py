"""
End-to-end agent-spine journey at the APP boundary on the UNIFIED §2.1 wire
(census #12 chip-spine-sessions-routers rewrite), against the PRODUCTION mount
(`app.py` — this suite is what proves the sessions + agent routers are actually
wired in, unlike the local-mount suites).

The harness is a stub HTTP server speaking real chunked `application/x-ndjson`
on POST /turn (no network egress beyond localhost, zero Anthropic). The
split-turn journey under test — the sequencing the spine mount (census #12
chip 1) unified:

    create session (converse tier)     POST /api/sessions
    -> post text message               POST .../messages -> 202, frozen
                                       ConverseTurnInput on the wire (no packet)
    -> mid-turn gate consult           POST /internal/agent/gate, run_write_tool
                                       -> awaiting_approval + durable GATE pending
    -> harness emits proposed_run      relay materializes the decidable
       {confirmation_id} + ends turn   session-store approvals row
    -> cross-tenant approval           other tenant -> 404, BOTH stores untouched
    -> owner approves                  POST /api/agent/approvals/{id} -> the
                                       decision BRIDGES into the gate store
                                       (grant_approval) — chip-1 unification
    -> confirm resume message          POST .../messages {confirm} -> consume,
                                       wire carries the DURABLE proposal
    -> gate re-check                   confirmation_id in args -> allow, args-exact
    -> audit trail                     audit_extra projection ONLY — raw params never

Plus the two hard denies: kill-switch file present, and rate-limit category
exhaustion (denied reason names the failing gate).

Run:  cd server && python -m pytest tests/test_agent_e2e.py -q
"""
from __future__ import annotations

# Cache the STDLIB `platform` module BEFORE PROJECT_ROOT lands on sys.path
# (repo-root platform/ package shadows it; mirrors test_wave4/5).
import platform as _stdlib_platform  # noqa: E402
_stdlib_platform.python_implementation()

import http.server  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
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

# route BOTH SQLite DBs to throwaway dirs BEFORE `jobs`/`session_store`/`app`
# import anywhere (each module reads its env once, at import time).
os.environ.setdefault("JOBS_DB", str(Path(tempfile.mkdtemp(prefix="e2e-jobs-")) / "jobs.db"))
os.environ.setdefault(
    "SESSIONS_DB", str(Path(tempfile.mkdtemp(prefix="e2e-sessions-")) / "sessions.db"))

import agent_gate  # noqa: E402
import session_store  # noqa: E402

DISPATCH_SECRET = "e2e-dispatch-secret-1234"


def _client():
    from fastapi.testclient import TestClient

    from app import app
    return TestClient(app, raise_server_exceptions=False)


def _h(tenant: str) -> dict:
    return {"X-Tenant-Id": tenant}


# --------------------------------------------------------------------------- #
# stub harness — real HTTP/1.1 chunked application/x-ndjson on POST /turn.
# The stream is GATED on `release` so the test can consult the gate MID-TURN
# (the real sequencing: the loop calls the gate while the turn is open, then
# emits proposed_run with the gate-minted confirmation_id).
# --------------------------------------------------------------------------- #
class _StubState:
    def __init__(self) -> None:
        self.bodies: List[Dict[str, Any]] = []
        self.hits = 0
        self.scripts: List[List[Dict[str, Any]]] = []  # per-call scripts (FIFO)
        self.release = threading.Event()
        self.release.set()


DEFAULT_SCRIPT = [{"type": "turn_complete", "data": {"stop_reason": "end_turn"}}]


class _StubServer(http.server.ThreadingHTTPServer):
    def __init__(self, state: _StubState):
        super().__init__(("127.0.0.1", 0), _StubHandler)
        self.state = state


class _StubHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle(self):
        # the app side aborts keep-alive sockets freely; expected, not noise.
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


@pytest.fixture(autouse=True)
def agent_env(tmp_path, monkeypatch):
    """Hermetic agent state: every durable file under tmp_path; back-edge secret
    set; auth off (X-Tenant-Id stub tenancy -> the full-access demo tier)."""
    monkeypatch.delenv("LEAF_AGENT_POLICY_FILE", raising=False)
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", DISPATCH_SECRET)
    monkeypatch.setenv("LEAF_AGENT_KILL_FILE", str(tmp_path / "agent.disabled"))
    monkeypatch.setenv("LEAF_AGENT_APPROVALS_DIR", str(tmp_path / "approvals"))
    monkeypatch.setenv("LEAF_AGENT_GRANTS_FILE", str(tmp_path / "grants.json"))
    monkeypatch.setenv("LEAF_AGENT_RATE_FILE", str(tmp_path / "rate.json"))
    monkeypatch.setenv("LEAF_AGENT_AUDIT", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LEAF_AGENT_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("LEAF_AGENT_TENANTS_FILE", str(tmp_path / "agent_tenants.json"))
    monkeypatch.setenv("TURN_MAX_S", "20")  # keep relay watchdogs bounded
    yield tmp_path


@pytest.fixture
def fake_harness(monkeypatch, tmp_path):
    """Converse harness env -> the stub (the §2.1 preference var; author var
    cleared to prove the preferred path), throwaway drawing store, and
    requests.get refused so nothing in the journey can leave the process."""
    state = _StubState()
    srv = _StubServer(state)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("LEAF_CONVERSE_HARNESS_URL",
                       f"http://127.0.0.1:{srv.server_address[1]}")
    monkeypatch.delenv("LEAF_AUTHOR_HARNESS_URL", raising=False)
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "store"))

    def _refuse_get(*args, **kwargs):
        import requests
        raise requests.ConnectionError("no network in tests")
    monkeypatch.setattr("requests.get", _refuse_get)
    try:
        yield state
    finally:
        state.release.set()
        srv.shutdown()


def _gate_call(client, action, args=None, *, tenant="t-alpha", session="sess-demo",
               turn="turn-1", secret=DISPATCH_SECRET):
    return client.post("/internal/agent/gate",
                       headers={"X-Dispatch-Secret": secret},
                       json={"tenant_id": tenant, "session_id": session,
                             "turn_id": turn, "action": action, "args": args or {}})


def _custom_policy(tmp_path, monkeypatch, mutate):
    raw = json.loads((SERVER_DIR / "agent_policy.json").read_text(encoding="utf-8"))
    mutate(raw)
    p = tmp_path / "custom_policy.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv("LEAF_AGENT_POLICY_FILE", str(p))


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


# --------------------------------------------------------------------------- #
# production mount — the routers this lane wired into app.py are actually there
# --------------------------------------------------------------------------- #
def test_agent_and_sessions_routers_mounted_on_production_app(fake_harness):
    """/api/agent/killswitch AND /api/sessions answer on the REAL app (a
    local-mount suite can never prove this — regression guard for the app.py
    includes of both spine-facing routers)."""
    c = _client()
    r = c.get("/api/agent/killswitch", headers=_h("t-alpha"))
    assert r.status_code == 200, r.text
    assert r.json()["active"] is False

    r = c.post("/api/sessions", json={"drawing_id": "mount-probe"}, headers=_h("t-alpha"))
    assert r.status_code == 200, r.text
    assert r.json()["session_id"]


# --------------------------------------------------------------------------- #
# the full split-turn journey
# --------------------------------------------------------------------------- #
def test_full_split_turn_journey(fake_harness):
    state = fake_harness
    c = _client()
    write_args = {"tool": "add-panel", "dwg": "demo",
                  "params": {"secret_payload": "never-log-me"}}

    # 1. create session — demo tier carries `converse`, so this passes the
    #    entitlement gate. Idempotent per (tenant, drawing).
    r = c.post("/api/sessions", json={"drawing_id": "demo"}, headers=_h("t-alpha"))
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    assert c.post("/api/sessions", json={"drawing_id": "demo"},
                  headers=_h("t-alpha")).json()["session_id"] == sid

    # 2. post the user text with the stream GATED open — the frozen
    #    ConverseTurnInput rides the wire (no ContextPacket: deliberate
    #    census #12 contract decision — the frozen shape has no packet field).
    state.release.clear()
    hint = {"lane": "run", "tool": "add-panel", "confidence": 0.61, "rationale": "match"}
    r = c.post(f"/api/sessions/{sid}/messages",
               json={"text": "add a panel on the roof", "classifier_hint": hint},
               headers=_h("t-alpha"))
    assert r.status_code == 202, r.text
    turn_id = r.json()["turn_id"]
    assert _wait_until(lambda: state.bodies), "harness never saw POST /turn"
    body = state.bodies[0]
    assert set(body) == {"tenant_id", "session_id", "turn_id", "drawing_id",
                        "messages", "text"}
    assert body["tenant_id"] == "t-alpha" and body["session_id"] == sid
    assert body["turn_id"] == turn_id and body["drawing_id"] == "demo"
    assert body["text"] == "add a panel on the roof"

    # 3. MID-TURN: the loop's canUseTool consults the gate for run_write_tool
    #    (confirm-once policy) -> awaiting_approval + durable GATE pending.
    g = _gate_call(c, "run_write_tool", write_args, session=sid, turn=turn_id)
    assert g.status_code == 200, g.text
    gb = g.json()
    assert gb["decision"] == "awaiting_approval"
    assert gb["policy"] == "confirm-once" and gb["rung"] == 3
    cid = gb["confirmation_id"]
    pending = agent_gate.read_pending(cid)
    assert pending is not None, "awaiting_approval must create a durable pending record"
    assert pending["tenant_id"] == "t-alpha"
    assert pending["session_id"] == sid and pending["turn_id"] == turn_id
    assert pending["action"] == "run_write_tool"
    assert pending["granted"] is False and pending["denied"] is False

    # 4. the harness emits proposed_run with the GATE-MINTED confirmation_id and
    #    ends the split turn; the relay materializes the decidable session row.
    state.scripts.append([
        {"type": "proposed_run", "data": {
            "confirmation_id": cid, "tool": "add-panel",
            "params": {"secret_payload": "never-log-me"},
            "dwg": "demo",
            "capability": "run_write", "rationale": "user asked for a panel"}},
        {"type": "turn_complete", "data": {"stop_reason": "awaiting_approval"}},
    ])
    state.release.set()
    assert _wait_until(lambda: session_store.get_approval(cid) is not None), (
        "relay never materialized the approvals row")
    assert _wait_until(lambda: _terminal_count(sid) >= 1)
    row = session_store.get_approval(cid)
    assert row["decided"] is False and row["consumed"] is False
    assert row["tool"] == "add-panel" and row["kind"] == "run_capability"

    # 5. approvals are tenant-isolated: another tenant sees 404 (no oracle)
    #    and BOTH stores stay undecided.
    r = c.post(f"/api/agent/approvals/{cid}", json={"approved": True},
               headers=_h("t-beta"))
    assert r.status_code == 404, r.text
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"
    assert agent_gate.read_pending(cid)["granted"] is False
    assert session_store.get_approval(cid)["decided"] is False

    # 6. the owner approves — ONE call lands the decision in BOTH stores:
    #    session row decided AND the gate record granted (the chip-1 bridge the
    #    resume consult redeems).
    r = c.post(f"/api/agent/approvals/{cid}", json={"approved": True},
               headers=_h("t-alpha"))
    assert r.status_code == 200, r.text
    assert r.json()["resolved"] is True and r.json()["approved"] is True
    assert agent_gate.read_pending(cid)["granted"] is True   # bridged
    assert session_store.get_approval(cid)["decided"] is True
    resolved = [ev for ev in session_store.recent_events(sid, 100)
                if ev["type"] == "confirmation_resolved"]
    assert resolved and resolved[-1]["data"]["approved"] is True

    # 7. the client posts the confirm resume message (split-turn step b) — the
    #    wire carries the DURABLE proposal from the consumed row, never text.
    r = c.post(f"/api/sessions/{sid}/messages",
               json={"confirm": {"confirmationId": cid, "approved": True}},
               headers=_h("t-alpha"))
    assert r.status_code == 202, r.text
    assert _wait_until(lambda: len(state.bodies) >= 2)
    resume_body = state.bodies[1]
    assert "text" not in resume_body
    assert resume_body["confirm"] == {
        "confirmation_id": cid, "approved": True,
        "proposal": {"tool": "add-panel",
                     "params": {"secret_payload": "never-log-me"},
                     "dwg": "demo",
                     "capability": "run_write"},
    }
    assert _wait_until(lambda: _terminal_count(sid) >= 2)

    # 8. the re-invoked tool call carries confirmation_id in args — the gate
    #    re-check is args-exact against the approval binding and now allows.
    resume_args = dict(write_args, confirmation_id=cid)
    gb = _gate_call(c, "run_write_tool", resume_args, session=sid,
                    turn="turn-resume").json()
    assert gb["decision"] == "allow"
    assert gb["reason"] == "allow_via_approval"
    assert gb["confirmation_id"] == cid

    # 8b. args drift after approval denies (the approved call IS the call).
    drifted = dict(resume_args, dwg="OTHER")
    gb = _gate_call(c, "run_write_tool", drifted, session=sid,
                    turn="turn-resume").json()
    assert gb["decision"] == "deny" and gb["reason"] == "args_mismatch"

    # 9. audit trail: request/grant/allow all recorded, args projected through
    #    the action's audit_extra allowlist ONLY — raw params never appear.
    body = c.get("/api/agent/audit", headers=_h("t-alpha")).json()
    kinds = [rec["kind"] for rec in body["records"]]
    assert "approval_requested" in kinds
    assert "approval_granted" in kinds
    assert "allowed" in kinds
    allowed = [rec for rec in body["records"] if rec["kind"] == "allowed"]
    assert allowed[-1]["args"] == {"tool": "add-panel", "dwg": "demo"}
    assert "never-log-me" not in json.dumps(body)
    assert all(rec["tenant_id"] == "t-alpha" for rec in body["records"])
    # tenant-b's audit view holds none of tenant-a's records
    body_b = c.get("/api/agent/audit", headers=_h("t-beta")).json()
    assert all(rec["tenant_id"] == "t-beta" for rec in body_b["records"])


# --------------------------------------------------------------------------- #
# hard denies: kill switch + rate-limit exhaustion
# --------------------------------------------------------------------------- #
def test_gate_denies_when_kill_switch_file_present(fake_harness):
    c = _client()
    agent_gate.kill_file().write_text("e2e drill\n", encoding="utf-8")
    assert c.get("/api/agent/killswitch", headers=_h("t-alpha")).json()["active"] is True
    gb = _gate_call(c, "run_write_tool", {"tool": "add-panel"}).json()
    assert gb["decision"] == "deny"
    assert gb["reason"].startswith("kill_switch_active")
    assert "e2e drill" in gb["reason"]


def test_rate_limit_exhaustion_denies_with_named_gate(fake_harness, tmp_path, monkeypatch):
    _custom_policy(tmp_path, monkeypatch,
                   lambda raw: raw["rate_limits"].update({"medium_per_hour": 2}))
    c = _client()
    for _ in range(2):
        gb = _gate_call(c, "run_read_tool", {"tool": "layer-report"}).json()
        assert gb["decision"] == "allow"
    gb = _gate_call(c, "run_read_tool", {"tool": "layer-report"}).json()
    assert gb["decision"] == "deny"
    assert gb["reason"] == "rate_limit_exceeded: medium (2/2)"
    # the audit record names the failing gate
    body = c.get("/api/agent/audit", headers=_h("t-alpha")).json()
    denied = [rec for rec in body["records"] if rec["kind"] == "denied"]
    assert denied and denied[-1]["gate"] == "rate_limit"


# --------------------------------------------------------------------------- #
# script runner
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
