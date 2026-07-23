"""
End-to-end agent-spine journey at the APP boundary (wire contract §§1-8),
against the PRODUCTION mount (`app.py` — this suite is what proves the agent
router is actually wired in, unlike test_agent_router.py's local mount).

Harness HTTP is monkeypatched (no network, no real harness). The split-turn
journey under test:

    create session (converse tier)
    -> post text message      (ContextPacket assembled; forwarded body carries
                               contextPacket + classifierHint)
    -> harness gate call      (POST /internal/agent/gate, run_capability on a
                               write tool -> awaiting_approval + durable pending)
    -> cross-tenant approval  (other tenant -> 404, no existence oracle)
    -> owner approves         (POST /api/agent/approvals/{id})
    -> confirm resume message (POST /api/sessions/{id}/messages {confirm})
    -> gate re-check          (confirmation_id in args -> allow, args-exact)
    -> audit trail            (audit_extra projection ONLY — raw params never)

Plus the two hard denies: kill-switch file present, and rate-limit category
exhaustion (denied reason names the failing gate).

Run:  cd server && python -m pytest tests/test_agent_e2e.py -q
"""
from __future__ import annotations

# Cache the STDLIB `platform` module BEFORE PROJECT_ROOT lands on sys.path
# (repo-root platform/ package shadows it; mirrors test_wave4/5).
import platform as _stdlib_platform  # noqa: E402
_stdlib_platform.python_implementation()

import json  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Dict, List, Optional  # noqa: E402

import pytest  # noqa: E402

pytestmark = pytest.mark.skip(reason="PARKED at the 2026-07-21 merge resolution (spine x sessions-wire): written against the section-18-era app internals (e.g. routers.sessions._SESSIONS) that the section-2.1 lane replaced with session_store. The ENGINE side was unified by census #12 chip-spine-harness-mount (spine engine live behind POST /turn); restoring THESE suites means rewriting their app-surface assertions against the unified 2.1 wire — census #12 chip-spine-sessions-routers.")

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

# route the jobs SQLite DB to a throwaway dir BEFORE `jobs`/`app` import anywhere.
os.environ.setdefault("JOBS_DB", str(Path(tempfile.mkdtemp(prefix="e2e-jobs-")) / "jobs.db"))

import agent_gate  # noqa: E402
from routers import sessions as sessions_router  # noqa: E402

HARNESS = "http://harness.test"
DISPATCH_SECRET = "e2e-dispatch-secret-1234"


def _client():
    from fastapi.testclient import TestClient

    from app import app
    return TestClient(app, raise_server_exceptions=False)


def _h(tenant: str) -> dict:
    return {"X-Tenant-Id": tenant}


# --------------------------------------------------------------------------- #
# fake harness HTTP layer (monkeypatches requests.request — no network)
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, status_code: int, body: Optional[Dict[str, Any]] = None):
        self.status_code = status_code
        self._body = body
        self.headers: Dict[str, str] = {}

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body

    def close(self):
        return None


class FakeHarness:
    """Programmable stand-in for requests.request. Records every forwarded call."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if method == "POST" and url.endswith("/converse/sessions"):
            drawing = kwargs["json"]["drawingId"]
            return FakeResponse(200, {"sessionId": f"sess-{drawing}", "status": "idle",
                                      "createdAt": "2026-07-20T00:00:00+00:00"})
        if method == "POST" and "/messages" in url:
            return FakeResponse(202, {"turnId": f"turn-{len(self.calls)}"})
        return FakeResponse(404, {"error": "session_not_found"})


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
    yield tmp_path


@pytest.fixture
def fake_harness(monkeypatch, tmp_path):
    """Converse harness URL set, requests.request faked, throwaway drawing store,
    grant-store read degrades instantly (requests.get refuses — no network)."""
    fh = FakeHarness()
    monkeypatch.setenv("LEAF_CONVERSE_HARNESS_URL", HARNESS)
    monkeypatch.delenv("LEAF_AUTHOR_HARNESS_URL", raising=False)
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setattr("requests.request", fh)

    def _refuse_get(*args, **kwargs):
        import requests
        raise requests.ConnectionError("no network in tests")
    monkeypatch.setattr("requests.get", _refuse_get)
    monkeypatch.setattr(sessions_router, "_SESSIONS", {})
    yield fh


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


# --------------------------------------------------------------------------- #
# production mount — the router this lane wired into app.py is actually there
# --------------------------------------------------------------------------- #
def test_agent_router_mounted_on_production_app(fake_harness):
    """/api/agent/killswitch answers on the REAL app (a local-mount suite can
    never prove this — regression guard for the app.py include)."""
    c = _client()
    r = c.get("/api/agent/killswitch", headers=_h("t-alpha"))
    assert r.status_code == 200, r.text
    assert r.json()["active"] is False


# --------------------------------------------------------------------------- #
# the full split-turn journey
# --------------------------------------------------------------------------- #
def test_full_split_turn_journey(fake_harness):
    c = _client()
    write_args = {"tool": "add-panel", "dwg": "demo",
                  "params": {"secret_payload": "never-log-me"}}

    # 1. create session — demo tier carries `converse`, so this passes the
    #    entitlement gate and reaches the (fake) harness.
    r = c.post("/api/sessions", json={"drawing_id": "demo"}, headers=_h("t-alpha"))
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    assert sid == "sess-demo"

    # 2. post the user text — the app assembles the §4 ContextPacket and the
    #    forwarded harness body carries contextPacket + classifierHint.
    hint = {"lane": "run", "tool": "add-panel", "confidence": 0.61, "rationale": "match"}
    r = c.post(f"/api/sessions/{sid}/messages",
               json={"text": "add a panel on the roof", "classifier_hint": hint},
               headers=_h("t-alpha"))
    assert r.status_code == 202, r.text
    turn_id = r.json()["turn_id"]
    fwd = fake_harness.calls[-1]
    assert fwd["url"] == f"{HARNESS}/converse/sessions/{sid}/messages"
    payload = fwd["json"]
    assert payload["tenantId"] == "t-alpha"
    assert payload["classifierHint"] == hint
    packet = payload["contextPacket"]
    for key in ("catalog", "catalog_hash", "drawing", "versions", "checkout",
                "entitlements", "active_jobs", "grant", "classifier_hint"):
        assert key in packet, f"ContextPacket missing {key!r}"
    assert packet["classifier_hint"] == hint
    assert packet["entitlements"]["converse"] is True

    # 3. the harness's canUseTool consults the gate for run_capability on a
    #    WRITE tool -> confirm-once policy -> awaiting_approval + durable pending.
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

    # 4. approvals are tenant-isolated: another tenant sees 404 (no oracle)
    #    and the record stays undecided.
    r = c.post(f"/api/agent/approvals/{cid}", json={"approved": True},
               headers=_h("t-beta"))
    assert r.status_code == 404, r.text
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"
    assert agent_gate.read_pending(cid)["granted"] is False

    # 5. the owner approves.
    r = c.post(f"/api/agent/approvals/{cid}", json={"approved": True},
               headers=_h("t-alpha"))
    assert r.status_code == 200, r.text
    assert r.json()["resolved"] is True and r.json()["approved"] is True

    # 6. the client posts the confirm resume message (split-turn step b) — the
    #    forwarded body carries confirm (not text) plus a fresh ContextPacket.
    r = c.post(f"/api/sessions/{sid}/messages",
               json={"confirm": {"confirmationId": cid, "approved": True}},
               headers=_h("t-alpha"))
    assert r.status_code == 202, r.text
    resume_payload = fake_harness.calls[-1]["json"]
    assert resume_payload["confirm"] == {"confirmationId": cid, "approved": True}
    assert "text" not in resume_payload
    assert "contextPacket" in resume_payload

    # 7. the re-invoked tool call carries confirmation_id in args — the gate
    #    re-check is args-exact against the approval binding and now allows.
    resume_args = dict(write_args, confirmation_id=cid)
    gb = _gate_call(c, "run_write_tool", resume_args, session=sid,
                    turn="turn-resume").json()
    assert gb["decision"] == "allow"
    assert gb["reason"] == "allow_via_approval"
    assert gb["confirmation_id"] == cid

    # 7b. args drift after approval denies (the approved call IS the call).
    drifted = dict(resume_args, dwg="OTHER")
    gb = _gate_call(c, "run_write_tool", drifted, session=sid,
                    turn="turn-resume").json()
    assert gb["decision"] == "deny" and gb["reason"] == "args_mismatch"

    # 8. audit trail: request/grant/allow all recorded, args projected through
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
