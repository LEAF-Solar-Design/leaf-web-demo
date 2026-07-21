"""
Binary acceptance for the agent spine HTTP surface (server/routers/agent.py,
plus the additive /api/usage agent block and the /api/ops/agent/* routes).

Contracts under test:
  * POST /internal/agent/gate: X-Dispatch-Secret constant-time gate — 401 when
    the env secret is UNSET (back-edge disabled) or wrong; gate result +
    envelope when correct;
  * POST /api/agent/approvals/{id}: tenant-bound — another tenant's id is 404
    (no existence oracle); grant enables the args-exact resume through the gate;
  * GET /api/agent/audit: own records only, audit_extra-projected (raw params
    never appear);
  * GET /api/agent/killswitch: read-only file-presence truth;
  * GET /api/usage: additive `agent` block aggregated from the agent ledger;
  * /api/ops/agent/*: tenants roll-up, per-session audit tail, per-tenant
    disable/enable that the gate enforces;
  * agent_ledger/agent_audit NEVER raise on write failure.

The routers are mounted on a LOCAL FastAPI app (server/app.py untouched —
another lane owns the production mount). Hermetic: all agent state in
tmp_path; auth off (X-Tenant-Id stub tenancy).

Run:  cd server && python -m pytest tests/test_agent_router.py -q
"""
from __future__ import annotations

# Cache the STDLIB `platform` module BEFORE PROJECT_ROOT lands on sys.path
# (repo-root platform/ package shadows it; mirrors test_wave4/5).
import platform as _stdlib_platform  # noqa: E402
_stdlib_platform.python_implementation()

import json  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import agent_gate  # noqa: E402
import agent_ledger  # noqa: E402
import agent_audit  # noqa: E402

DISPATCH_SECRET = "test-dispatch-secret-1234"


@pytest.fixture(autouse=True)
def agent_env(tmp_path, monkeypatch):
    monkeypatch.delenv("LEAF_AGENT_POLICY_FILE", raising=False)
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.delenv("LEAF_OPS_SECRET", raising=False)
    monkeypatch.setenv("LEAF_AGENT_KILL_FILE", str(tmp_path / "agent.disabled"))
    monkeypatch.setenv("LEAF_AGENT_APPROVALS_DIR", str(tmp_path / "approvals"))
    monkeypatch.setenv("LEAF_AGENT_GRANTS_FILE", str(tmp_path / "grants.json"))
    monkeypatch.setenv("LEAF_AGENT_RATE_FILE", str(tmp_path / "rate.json"))
    monkeypatch.setenv("LEAF_AGENT_AUDIT", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LEAF_AGENT_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("LEAF_AGENT_TENANTS_FILE", str(tmp_path / "agent_tenants.json"))
    # keep the /api/usage broker-ledger read hermetic too
    monkeypatch.setenv("LEAF_USAGE_LEDGER", str(tmp_path / "broker_ledger.jsonl"))
    yield tmp_path


@pytest.fixture
def client():
    """Local mount — server/app.py is another lane's file and stays untouched."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import agent as agent_router
    from routers import ops as ops_router
    from routers import usage as usage_router

    app = FastAPI()
    app.include_router(agent_router.router)
    app.include_router(usage_router.router)
    app.include_router(ops_router.router)
    return TestClient(app, raise_server_exceptions=False)


def _gate_call(client, action, args=None, *, tenant="demo-tenant", session="s-1",
               turn="turn-1", secret=DISPATCH_SECRET):
    headers = {}
    if secret is not None:
        headers["X-Dispatch-Secret"] = secret
    return client.post("/internal/agent/gate", headers=headers, json={
        "tenant_id": tenant, "session_id": session, "turn_id": turn,
        "action": action, "args": args or {},
    })


# --------------------------------------------------------------------------- #
# back-edge auth
# --------------------------------------------------------------------------- #
def test_gate_401_when_secret_unset(client, monkeypatch):
    monkeypatch.delenv("LEAF_APP_DISPATCH_SECRET", raising=False)
    resp = _gate_call(client, "read_platform_state")
    assert resp.status_code == 401
    assert resp.json()["error"]["error_code"] == "BAD_PARAMS"


def test_gate_401_wrong_or_missing_secret(client, monkeypatch):
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", DISPATCH_SECRET)
    assert _gate_call(client, "read_platform_state", secret="wrong").status_code == 401
    assert _gate_call(client, "read_platform_state", secret=None).status_code == 401


def test_gate_allows_with_correct_secret(client, monkeypatch):
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", DISPATCH_SECRET)
    resp = _gate_call(client, "read_platform_state")
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "allow"
    assert body["policy"] == "auto"
    assert body["rung"] == 1
    assert body["error"] is None and body["degraded_mode"] is False


def test_gate_deny_shape(client, monkeypatch):
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", DISPATCH_SECRET)
    body = _gate_call(client, "no_such_action").json()
    assert body["decision"] == "deny"
    assert body["reason"] == "unknown_action"


# --------------------------------------------------------------------------- #
# back-edge trust on app reads/dispatch (wire contract §0, deps.require_tenant)
# --------------------------------------------------------------------------- #
@pytest.fixture
def backedge_client():
    """Local mount of a §0 back-edge route (GET /api/tools) plus a NON-back-edge
    route (GET /api/entitlements) from the same router."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import tools as tools_router

    app = FastAPI()
    app.include_router(tools_router.router)
    return TestClient(app, raise_server_exceptions=False)


def test_backedge_route_matcher_covers_exactly_the_contract_routes():
    import deps
    assert deps._dispatch_backedge_route("POST", "/api/run") is True
    assert deps._dispatch_backedge_route("GET", "/api/jobs/job-123") is True
    assert deps._dispatch_backedge_route("GET", "/api/capabilities") is True
    assert deps._dispatch_backedge_route("GET", "/api/tools") is True
    assert deps._dispatch_backedge_route("GET", "/api/drawings/demo/versions") is True
    # NOT back-edge: the SSE stream, the jobs list, and everything else
    assert deps._dispatch_backedge_route("GET", "/api/jobs/job-123/stream") is False
    assert deps._dispatch_backedge_route("GET", "/api/jobs") is False
    assert deps._dispatch_backedge_route("GET", "/api/entitlements") is False
    assert deps._dispatch_backedge_route("POST", "/api/author") is False
    assert deps._dispatch_backedge_route("DELETE", "/api/sessions/x") is False


def test_corrupt_primary_tier_source_never_falls_back_to_a_stale_env_map(tmp_path, monkeypatch):
    """ABSENT primary means the operator did not provision it, so the env map is
    a legitimate fallback. PRESENT-but-corrupt is a different fact: falling
    through there let a stale LEAF_BROKER_TENANT_TIERS promote a DOWNGRADED
    tenant back to a paid tier. An unreadable primary means the tier is UNKNOWN,
    and unknown must resolve restricted, never inherit the secondary."""
    import deps
    p = tmp_path / "broker_tenants.json"
    monkeypatch.setenv("BROKER_TENANTS", str(p))
    monkeypatch.setenv("LEAF_BROKER_TENANT_TIERS", '{"t-r": "hosted_pro"}')

    # absent primary -> env fallback is legitimate
    assert deps.backedge_tier("t-r") == "hosted_pro"

    # present but corrupt -> unknown, NOT the stale env value
    for corrupt in ('{truncated', '["not","a","mapping"]', '{"t-r": null}', '{"t-r": "oops"}'):
        p.write_text(corrupt, encoding="utf-8")
        assert deps.backedge_tier("t-r") is None, corrupt

    # invalid UTF-8 is just another unreadable primary. UnicodeDecodeError
    # subclasses ValueError, NOT OSError, so it escaped as a 500 instead of
    # resolving unknown -> restricted -> an honest 403.
    p.write_bytes(b'{"t-r": {"tier": "\xff\xfe not utf8"}}')
    assert deps.backedge_tier("t-r") is None

    # a VALID primary that simply lacks the tenant still falls through
    p.write_text('{"someone-else": {"tier": "hosted_starter"}}', encoding="utf-8")
    assert deps.backedge_tier("t-r") == "hosted_pro"

    # ...and a valid record wins over the env map
    p.write_text('{"t-r": {"tier": "restricted"}}', encoding="utf-8")
    assert deps.backedge_tier("t-r") == "restricted"


def test_backedge_secret_trusts_tenant_header_in_live_auth(backedge_client, monkeypatch):
    """LEAF_AUTH_LIVE=1 + valid X-Dispatch-Secret on a §0 route: X-Tenant-Id is
    trusted as the resolved tenant (no JWT) — the spine can fetch the catalog."""
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", DISPATCH_SECRET)
    r = backedge_client.get("/api/tools", headers={
        "X-Dispatch-Secret": DISPATCH_SECRET, "X-Tenant-Id": "t-spine"})
    assert r.status_code == 200, r.text
    assert "tools" in r.json()


def test_backedge_wrong_secret_still_401(backedge_client, monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", DISPATCH_SECRET)
    r = backedge_client.get("/api/tools", headers={
        "X-Dispatch-Secret": "wrong", "X-Tenant-Id": "t-spine"})
    assert r.status_code == 401


def test_backedge_secret_unset_means_disabled(backedge_client, monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.delenv("LEAF_APP_DISPATCH_SECRET", raising=False)
    r = backedge_client.get("/api/tools", headers={
        "X-Dispatch-Secret": DISPATCH_SECRET, "X-Tenant-Id": "t-spine"})
    assert r.status_code == 401


def test_backedge_secret_ignored_on_non_contract_route(backedge_client, monkeypatch):
    """GET /api/entitlements is NOT a §0 back-edge route — the secret must not
    widen the trusted surface beyond the contract list."""
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", DISPATCH_SECRET)
    r = backedge_client.get("/api/entitlements", headers={
        "X-Dispatch-Secret": DISPATCH_SECRET, "X-Tenant-Id": "t-spine"})
    assert r.status_code == 401


# --------------------------------------------------------------------------- #
# gate tier resolution (trusted source, fail closed)
# --------------------------------------------------------------------------- #
def test_gate_tier_comes_from_the_broker_trusted_source(client, monkeypatch, tmp_path):
    """A tenant provisioned as `restricted` must be gated as restricted — resolving
    the bare tenant_id would default it to `demo` and allow the write."""
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", DISPATCH_SECRET)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    tenants = tmp_path / "broker_tenants.json"
    tenants.write_text(json.dumps({"t-down": {"tier": "restricted"}}), encoding="utf-8")
    monkeypatch.setenv("BROKER_TENANTS", str(tenants))
    body = _gate_call(client, "run_write_tool", {"tool": "add-panel"},
                      tenant="t-down").json()
    assert body["decision"] == "deny"
    assert body["reason"].startswith("entitlement_required")


def test_gate_tier_env_source_and_unprovisioned_fails_closed(client, monkeypatch, tmp_path):
    """LEAF_BROKER_TENANT_TIERS is the second trusted source; a tenant absent from
    both, with auth LIVE, resolves to `restricted` (never `demo`)."""
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", DISPATCH_SECRET)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("BROKER_TENANTS", str(tmp_path / "absent.json"))
    monkeypatch.setenv("LEAF_BROKER_TENANT_TIERS", json.dumps({"t-pro": "hosted_pro"}))
    assert _gate_call(client, "run_write_tool", {"tool": "add-panel"},
                      tenant="t-pro").json()["decision"] != "deny"
    unprovisioned = _gate_call(client, "run_write_tool", {"tool": "add-panel"},
                               tenant="t-unknown").json()
    assert unprovisioned["decision"] == "deny"
    assert unprovisioned["reason"].startswith("entitlement_required")


def test_gate_offauth_tier_is_unchanged_demo(client, monkeypatch, tmp_path):
    """Auth OFF (the open demo): the gate keeps resolving `demo` regardless of any
    provisioning file — byte-identical to the pre-fix behaviour."""
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", DISPATCH_SECRET)
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.setenv("BROKER_TENANTS", str(tmp_path / "absent.json"))
    assert _gate_call(client, "run_write_tool", {"tool": "add-panel"},
                      tenant="t-unknown").json()["decision"] != "deny"


def test_backedge_tenant_carries_the_trusted_tier(monkeypatch, tmp_path):
    import deps
    import entitlements as ent
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    tenants = tmp_path / "broker_tenants.json"
    tenants.write_text(json.dumps({"t-starter": {"tier": "hosted_starter"}}), encoding="utf-8")
    monkeypatch.setenv("BROKER_TENANTS", str(tenants))
    monkeypatch.delenv("LEAF_BROKER_TENANT_TIERS", raising=False)
    ctx = deps.backedge_tenant("t-starter")
    assert isinstance(ctx, deps.TenantContext) and str(ctx) == "t-starter"
    assert ent.resolve_tier(ctx) == "hosted_starter"
    # unprovisioned under live auth -> restricted, and a plain str when auth is off
    assert ent.resolve_tier(deps.backedge_tenant("t-nobody")) == ent.RESTRICTED_TIER
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    assert deps.backedge_tenant("t-nobody") == "t-nobody"
    assert ent.resolve_tier(deps.backedge_tenant("t-nobody")) == ent.DEFAULT_TIER


# --------------------------------------------------------------------------- #
# approvals — tenant binding + resume
# --------------------------------------------------------------------------- #
def _propose_write(client, tenant="tenant-a"):
    resp = _gate_call(client, "run_write_tool", {"tool": "add-panel"}, tenant=tenant)
    body = resp.json()
    assert body["decision"] == "awaiting_approval"
    return body["confirmation_id"]


def test_approval_cross_tenant_is_404(client, monkeypatch):
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", DISPATCH_SECRET)
    cid = _propose_write(client, tenant="tenant-a")
    resp = client.post(f"/api/agent/approvals/{cid}",
                       headers={"X-Tenant-Id": "tenant-b"}, json={"approved": True})
    assert resp.status_code == 404
    # untouched: the owner can still resolve it
    resp2 = client.post(f"/api/agent/approvals/{cid}",
                        headers={"X-Tenant-Id": "tenant-a"}, json={"approved": True})
    assert resp2.status_code == 200
    assert resp2.json() == {"resolved": True, "approved": True,
                            "error": None, "degraded_mode": False}


def test_unknown_approval_is_404(client):
    resp = client.post("/api/agent/approvals/deadbeef",
                       headers={"X-Tenant-Id": "tenant-a"}, json={"approved": True})
    assert resp.status_code == 404


def test_approve_then_resume_through_gate(client, monkeypatch):
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", DISPATCH_SECRET)
    cid = _propose_write(client, tenant="tenant-a")
    assert client.post(f"/api/agent/approvals/{cid}",
                       headers={"X-Tenant-Id": "tenant-a"},
                       json={"approved": True}).status_code == 200
    resumed = _gate_call(client, "run_write_tool",
                         {"tool": "add-panel", "confirmation_id": cid},
                         tenant="tenant-a").json()
    assert resumed["decision"] == "allow"
    assert resumed["reason"] == "allow_via_approval"


def test_deny_then_resume_is_denied(client, monkeypatch):
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", DISPATCH_SECRET)
    cid = _propose_write(client, tenant="tenant-a")
    resp = client.post(f"/api/agent/approvals/{cid}",
                       headers={"X-Tenant-Id": "tenant-a"}, json={"approved": False})
    assert resp.status_code == 200
    assert resp.json()["approved"] is False
    resumed = _gate_call(client, "run_write_tool",
                         {"tool": "add-panel", "confirmation_id": cid},
                         tenant="tenant-a").json()
    assert resumed["decision"] == "deny"
    assert resumed["reason"] == "approval_denied"


def test_double_decide_is_409(client, monkeypatch):
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", DISPATCH_SECRET)
    cid = _propose_write(client, tenant="tenant-a")
    headers = {"X-Tenant-Id": "tenant-a"}
    assert client.post(f"/api/agent/approvals/{cid}", headers=headers,
                       json={"approved": True}).status_code == 200
    assert client.post(f"/api/agent/approvals/{cid}", headers=headers,
                       json={"approved": True}).status_code == 409


# --------------------------------------------------------------------------- #
# tenant audit read — own records, projected
# --------------------------------------------------------------------------- #
def test_audit_is_tenant_scoped_and_projected(client, monkeypatch):
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", DISPATCH_SECRET)
    _gate_call(client, "run_read_tool",
               {"tool": "layer-report", "dwg": "rooftop_demo",
                "params": {"secret_payload": "never-log-me"}},
               tenant="tenant-a")
    _gate_call(client, "read_platform_state", tenant="tenant-b")

    body = client.get("/api/agent/audit", headers={"X-Tenant-Id": "tenant-a"}).json()
    assert body["count"] >= 1
    assert all(r["tenant_id"] == "tenant-a" for r in body["records"])
    allowed = [r for r in body["records"] if r.get("kind") == "allowed"]
    assert allowed, "the allow decision must be audited"
    # audit_extra allowlist ONLY: tool + dwg pass, params NEVER appear
    args = allowed[-1]["args"]
    assert args == {"tool": "layer-report", "dwg": "rooftop_demo"}
    assert "never-log-me" not in json.dumps(body)

    body_b = client.get("/api/agent/audit", headers={"X-Tenant-Id": "tenant-b"}).json()
    assert all(r["tenant_id"] == "tenant-b" for r in body_b["records"])


def test_free_text_is_hashed_in_audit():
    long_text = "build me a tool that " + "x" * 200
    projected = agent_audit.project_args({"description": long_text}, ("description",))
    assert projected["description"].startswith("sha256:")
    assert "xxx" not in projected["description"]


# --------------------------------------------------------------------------- #
# killswitch read
# --------------------------------------------------------------------------- #
def test_killswitch_read_only_truth(client, agent_env):
    assert client.get("/api/agent/killswitch").json()["active"] is False
    agent_gate.kill_file().write_text("", encoding="utf-8")
    assert client.get("/api/agent/killswitch").json()["active"] is True


# --------------------------------------------------------------------------- #
# usage — additive agent block
# --------------------------------------------------------------------------- #
def test_usage_gains_agent_block(client):
    for turn in (1, 2):
        agent_ledger.append({"tenant_id": "demo-tenant", "session_id": "s-1",
                             "turn_id": f"t{turn}", "turn": turn, "cost_tokens": 1000,
                             "usd_est": 0.02, "stop_reason": "end_turn"})
    agent_ledger.append({"tenant_id": "other", "session_id": "s-2", "turn_id": "t1",
                         "turn": 1, "cost_tokens": 5, "usd_est": 0.1,
                         "stop_reason": "end_turn"})
    body = client.get("/api/usage").json()  # default demo-tenant
    agent = body["agent"]
    assert agent["estimate_basis"] == "self_metered"
    assert agent["today"] == {"turns": 2, "cost_tokens": 2000, "usd_est": 0.04}
    assert agent["cycle"]["turns"] == 2
    # pre-existing fields untouched (additive)
    assert "today" in body and "cap" in body


# --------------------------------------------------------------------------- #
# ops surface
# --------------------------------------------------------------------------- #
def test_ops_agent_tenants_and_disable_enable(client, monkeypatch):
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", DISPATCH_SECRET)
    agent_ledger.append({"tenant_id": "t-ops", "session_id": "s", "turn_id": "t",
                         "turn": 1, "cost_tokens": 7, "usd_est": 0.01,
                         "stop_reason": "end_turn"})
    assert client.post("/api/ops/agent/tenants/t-ops/disable").json()["agent_disabled"] is True
    rows = client.get("/api/ops/agent/tenants").json()["tenants"]
    row = next(r for r in rows if r["tenant_id"] == "t-ops")
    assert row["turns"] == 1 and row["agent_disabled"] is True
    # the gate ENFORCES the flag
    denied = _gate_call(client, "read_platform_state", tenant="t-ops").json()
    assert denied["decision"] == "deny" and denied["reason"] == "tenant_agent_disabled"
    assert client.post("/api/ops/agent/tenants/t-ops/enable").json()["agent_disabled"] is False
    assert _gate_call(client, "read_platform_state", tenant="t-ops").json()["decision"] == "allow"


def test_ops_agent_session_audit_tail(client, monkeypatch):
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", DISPATCH_SECRET)
    _gate_call(client, "read_platform_state", tenant="tenant-a", session="s-ops-1")
    _gate_call(client, "read_platform_state", tenant="tenant-a", session="s-ops-2")
    body = client.get("/api/ops/agent/sessions/s-ops-1").json()
    assert body["session_id"] == "s-ops-1"
    assert body["count"] >= 1
    assert all(r["session_id"] == "s-ops-1" for r in body["records"])


def test_ops_agent_routes_fail_closed_in_live_auth(client, monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")  # secret unset + live auth => 503
    assert client.get("/api/ops/agent/tenants").status_code == 503
    assert client.post("/api/ops/agent/tenants/t/disable").status_code == 503


# --------------------------------------------------------------------------- #
# never-raise write discipline
# --------------------------------------------------------------------------- #
def test_ledger_and_audit_never_raise_on_write_failure(tmp_path, monkeypatch):
    # point both files at a path that IS a directory -> open() fails
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    monkeypatch.setenv("LEAF_AGENT_LEDGER", str(blocked))
    monkeypatch.setenv("LEAF_AGENT_AUDIT", str(blocked))
    agent_ledger.append({"tenant_id": "t", "turn": 1})   # must not raise
    agent_audit.append({"kind": "denied", "tenant_id": "t"})  # must not raise
    # reads on the broken paths degrade to empty, never an error
    assert agent_audit.for_tenant("t") == []
    assert agent_ledger.aggregate("t")["today"]["turns"] == 0


def test_gate_survives_unwritable_audit(client, monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", DISPATCH_SECRET)
    blocked = tmp_path / "blocked-audit"
    blocked.mkdir()
    monkeypatch.setenv("LEAF_AGENT_AUDIT", str(blocked))
    resp = _gate_call(client, "read_platform_state")
    assert resp.status_code == 200
    assert resp.json()["decision"] == "allow"
