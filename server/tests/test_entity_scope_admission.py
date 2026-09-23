"""SSD1 B1 admits effects against the exact turn's stored entity binding."""
import inspect
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agent_audit
import agent_policy
import deps
import entity_scope
import session_store
from routers import agent as agent_router
from routers import jobs as jobs_router


BINDING = {"drawing_id": "drawing-a", "base_version": 1,
           "base_source_sha256": "a" * 64, "allowed_handles": ["AB"]}
MALFORMED = [None, {}, {**BINDING, "base_version": True},
             {**BINDING, "allowed_handles": ["AB", "CD"]}]


def broken(*args, **kwargs):
    raise RuntimeError("store unavailable")


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "sessions.db")
    monkeypatch.setattr(session_store, "_conn", None)
    monkeypatch.setattr(session_store, "_store_mode", lambda: "legacy")
    session_store.ensure_started()
    return session_store.get_or_create_session("tenant-a", "drawing-a")["session_id"]


def start(session, data, turn="turn-1"):
    session_store.append_event(session, turn, "turn_started", data)


def test_turn_started_data_reads_the_exact_turn(session):
    start(session, {"first": True}, "t1")
    start(session, {"second": True}, "t2")
    assert session_store.turn_started_data(session, "t1", "tenant-a") == {"first": True}
    assert session_store.turn_started_data(session, "t2", "tenant-a") == {"second": True}
    assert session_store.turn_started_data(session, "unknown", "tenant-a") is None
    assert session_store.turn_started_data(session, "t1", "tenant-b") is None
    assert session_store.turn_started_data("unknown", "t1", "tenant-a") is None


def test_turn_started_data_refuses_ambiguity_and_non_objects(session):
    start(session, {}, "ambiguous")
    start(session, {}, "ambiguous")
    with pytest.raises(ValueError, match="ambiguous turn_started events"):
        session_store.turn_started_data(session, "ambiguous", "tenant-a")
    start(session, [1], "array")
    with pytest.raises(ValueError):
        session_store.turn_started_data(session, "array", "tenant-a")


def test_pg_turn_started_data_uses_the_project_tenant_rule(monkeypatch):
    source = inspect.getsource(session_store._pg_turn_started_data)
    assert "type = 'turn_started'" in source
    assert "'project:' || s.org_id::text || ':' || s.project_id::text" in source
    assert "s.tenant_id" in source
    calls = []
    monkeypatch.setattr(session_store, "_store_mode", lambda: "postgres")
    monkeypatch.setattr(session_store, "_pg_turn_started_data",
                        lambda *args: calls.append(args) or {"postgres": True})
    assert session_store.turn_started_data("s", "t", "tenant") == {"postgres": True}
    assert calls == [("s", "t", "tenant")]


@pytest.mark.parametrize("data,status", [({}, None), ({"entity_scope": BINDING}, None),
    ("missing", 403), ("failure", 403),
    *[({"entity_scope": value}, 409) for value in MALFORMED]])
def test_resolve_turn_binding_states(session, monkeypatch, data, status):
    if data == "failure":
        monkeypatch.setattr(session_store, "turn_started_data", broken)
    elif data != "missing":
        start(session, data)
    if status:
        with pytest.raises(entity_scope.ScopeError) as exc:
            entity_scope.resolve_turn_binding(session, "turn-1", "tenant-a")
        assert exc.value.status_code == status
    else:
        assert entity_scope.resolve_turn_binding(session, "turn-1", "tenant-a") == data.get("entity_scope")


@pytest.fixture
def gate_client(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", "scope-test-secret")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    monkeypatch.delenv("LEAF_AGENT_POLICY_FILE", raising=False)
    for key, name in {
        "LEAF_AGENT_KILL_FILE": "disabled", "LEAF_AGENT_APPROVALS_DIR": "approvals",
        "LEAF_AGENT_GRANTS_FILE": "grants.json", "LEAF_AGENT_RATE_FILE": "rate.json",
        "LEAF_AGENT_AUDIT": "audit.jsonl", "LEAF_AGENT_LEDGER": "ledger.jsonl",
        "LEAF_AGENT_TENANTS_FILE": "tenants.json",
    }.items():
        monkeypatch.setenv(key, str(tmp_path / name))
    # Exercise scope admission for every named action, including actions the
    # shipped catalog disables before the scope gate is reached.
    effective_action = agent_policy.effective_action
    def enabled_action(*args, **kwargs):
        action = effective_action(*args, **kwargs)
        return replace(action, enabled=True) if action is not None else None
    monkeypatch.setattr(agent_policy, "effective_action", enabled_action)
    app = FastAPI()
    app.include_router(agent_router.router)
    return TestClient(app)


def gate(client, session, action="author_tool", turn="turn-1"):
    response = client.post("/internal/agent/gate", headers={
        "X-Dispatch-Secret": "scope-test-secret"}, json={
        "tenant_id": "tenant-a", "session_id": "harness-session", "turn_id": "harness-turn",
        "authority_session_id": session, "authority_turn_id": turn,
        "action": action, "args": {},
    })
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("action", sorted(entity_scope.SCOPE_REFUSED_ACTIONS))
def test_gate_refuses_each_mutating_action_for_a_scoped_turn(session, gate_client, action):
    start(session, {"entity_scope": BINDING})
    result = gate(gate_client, session, action)
    assert result["decision"] == "deny"
    assert result["reason"] == (
        "entity_scope_mutation_unsupported: Entity-scoped turns can only read or run supported drawing writes.")


@pytest.mark.parametrize("action", sorted(entity_scope.SCOPE_REFUSED_ACTIONS))
def test_gate_does_not_refuse_an_unscoped_turn(session, gate_client, action):
    start(session, {})
    assert not (gate(gate_client, session, action).get("reason") or "").startswith("entity_scope")


@pytest.mark.parametrize("action", ["read_platform_state", "run_read_tool",
                                    "run_write_tool", "submit_live_solve"])
def test_gate_never_looks_up_scope_for_other_actions(session, gate_client, monkeypatch, action):
    start(session, {"entity_scope": BINDING})
    calls = []
    def unexpected(*args):
        calls.append(args)
        raise RuntimeError("unexpected scope lookup")
    monkeypatch.setattr(session_store, "turn_started_data", unexpected)
    assert not (gate(gate_client, session, action).get("reason") or "").startswith("entity_scope")
    assert calls == []


@pytest.mark.parametrize("state,prefix", [
    ("missing", "entity_scope_unavailable:"), ("invalid", "entity_scope_invalid:"),
    ("failure", "entity_scope_unavailable:"), ("unknown", None)])
def test_gate_scope_lookup_failures_deny_by_name(session, gate_client, monkeypatch, state, prefix):
    if state == "invalid":
        start(session, {"entity_scope": None})
    elif state == "failure":
        monkeypatch.setattr(session_store, "turn_started_data", broken)
    elif state == "unknown":
        session = "not-stored"
    reason = gate(gate_client, session).get("reason") or ""
    assert reason.startswith(prefix) if prefix else not reason.startswith("entity_scope")


def test_gate_scope_denial_is_audited(session, gate_client):
    start(session, {"entity_scope": BINDING})
    result = gate(gate_client, session)
    rows = agent_audit.for_tenant("tenant-a", limit=100)
    assert any(row.get("kind") == "denied" and row.get("gate") == "entity_scope"
               and row.get("rung") == result["rung"] and row.get("policy") == result["policy"]
               for row in rows)


@pytest.fixture
def run_env(monkeypatch):
    submissions = {"job": [], "plan": [], "canonical": []}
    for name, key in [("submit_job", "job"), ("submit_plan_job", "plan")]:
        def record(*args, _key=key, **kwargs):
            submissions[_key].append((args, kwargs))
            return "scope-test-job"
        monkeypatch.setattr(jobs_router.jobs, name, record)
    def canonical(*args, **kwargs):
        submissions["canonical"].append((args, kwargs))
        return "scope-test-canonical"
    monkeypatch.setattr(jobs_router.jobs.platform_link, "submit_canonical_solve", canonical)
    monkeypatch.setattr(jobs_router.jobs.platform_link, "resolve_submission_context", lambda *args: None)
    monkeypatch.setattr(jobs_router, "_checkout_identity", lambda *args: ("anonymous", None))
    monkeypatch.setattr(jobs_router, "_store", lambda: SimpleNamespace(authority_mode=lambda: "legacy"))
    monkeypatch.setattr(deps, "auth_live", lambda: False)
    monkeypatch.setenv("LEAF_EXACT_WRITE_PINS_REQUIRED", "0")
    monkeypatch.setattr(deps, "effective_tools_with_provenance", lambda *args: [])
    monkeypatch.setattr(deps, "load_engine_registry_tools", lambda: [])
    monkeypatch.setattr(jobs_router.entitlements, "entitlements_for",
                        lambda *args: {"run_read": True, "run_write": True})
    monkeypatch.setattr(jobs_router.entitlements, "w1_tool_availability", lambda *args, **kwargs: None)
    caller = deps.TenantContext("tenant-a", tier="demo")
    app = FastAPI()
    app.include_router(jobs_router.router)
    app.dependency_overrides[deps.require_tenant] = lambda: caller
    client = TestClient(app)
    def run(tool=None, headers=None):
        tool = tool or {"name": "scope-read", "capabilities": []}
        monkeypatch.setattr(deps, "find_tool", lambda *args: tool)
        return client.post("/api/run", headers=headers or {}, json={
            "tool": tool["name"], "params": {}, "dwg": "drawing-a",
            "catalog_digest": deps.catalog_tool_digest(tool),
        })
    return SimpleNamespace(run=run, submissions=submissions, app=app)


def authority(session, turn="turn-1"):
    return {"X-Authority-Session-Id": session, "X-Authority-Turn-Id": turn}


def refused(env, response, status, reason=None):
    assert response.status_code == status, response.text
    if reason:
        assert response.json()["reason_code"] == reason
    if status == 403:
        assert response.json()["error"]["error_code"] == "FORBIDDEN"
    assert env.submissions == {"job": [], "plan": [], "canonical": []}


def test_run_direct_call_without_authority_headers_reads_as_unscoped(monkeypatch):
    submissions = []
    scope_calls = []

    def submit(*args, **kwargs):
        submissions.append((args, kwargs))
        return "direct-call-job"

    def unexpected(*args, **kwargs):
        scope_calls.append((args, kwargs))
        raise RuntimeError("unexpected scope lookup")

    monkeypatch.setattr(jobs_router.jobs, "submit_job", submit)
    monkeypatch.setattr(entity_scope, "resolve_turn_binding", unexpected)
    tool = deps.find_tool("count-by-layer")
    req = jobs_router.RunRequest(
        tool="count-by-layer", params={}, dwg="rooftop_demo",
        catalog_digest=deps.catalog_tool_digest(tool))
    response = jobs_router.run(
        req, wait=0, tenant_id="demo-tenant",
        x_org_id=None, x_project_id=None,
        idempotency_key=None, authorization=None)

    assert response.status_code == 202
    assert len(submissions) == 1
    assert scope_calls == []


def test_run_without_authority_headers_never_resolves_scope(run_env, monkeypatch):
    before = run_env.run()
    calls = []
    def unexpected(*args):
        calls.append(args)
        raise RuntimeError("unexpected scope lookup")
    monkeypatch.setattr(entity_scope, "resolve_turn_binding", unexpected)
    after = run_env.run()
    assert before.status_code == after.status_code == 202
    assert calls == []


@pytest.mark.parametrize("header", ["X-Authority-Session-Id", "X-Authority-Turn-Id"])
def test_run_refuses_one_authority_header(run_env, header):
    refused(run_env, run_env.run(headers={header: "only-one"}), 403)


@pytest.mark.parametrize("live", [False, True])
def test_run_refuses_a_turn_without_its_start_event(session, run_env, monkeypatch, live):
    if live:
        monkeypatch.setattr(deps, "auth_live", lambda: True)
        monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority",
                            lambda subject: ("tenant-a", "hosted_pro"))
        assert session_store.try_begin_turn(session, "turn-1", 60,
                                            tier="hosted_pro", subject="auth0|alice")
        run_env.app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(
            "tenant-a", tier="hosted_pro", backedge=True)
    refused(run_env, run_env.run(headers=authority(session)), 403)


@pytest.mark.parametrize("value", MALFORMED)
def test_run_refuses_a_malformed_stored_scope(session, run_env, value):
    start(session, {"entity_scope": value})
    refused(run_env, run_env.run(headers=authority(session)), 409, "entity_scope_invalid")


@pytest.mark.parametrize("name", ["solar-settings", "solar-correct-string", "solar-size-strings"])
def test_run_refuses_scoped_solar_mutations(session, run_env, name):
    start(session, {"entity_scope": BINDING})
    refused(run_env, run_env.run({"name": name, "capabilities": ["drawing.write"]},
                                authority(session)), 403, "entity_scope_mutation_unsupported")


@pytest.mark.parametrize("path", ["canonical_only", "X-Org-Id", "X-Project-Id"])
def test_run_refuses_scoped_canonical_paths(session, run_env, path):
    start(session, {"entity_scope": BINDING})
    tool = {"name": "scope-read", "capabilities": []}
    headers = authority(session)
    if path == "canonical_only":
        tool[path] = True
    else:
        headers[path] = "project-context"
    refused(run_env, run_env.run(tool, headers), 403, "entity_scope_mutation_unsupported")


@pytest.mark.parametrize("tool", [
    {"name": "scope-read", "capabilities": []},
    {"name": "scope-write", "capabilities": ["drawing.write"]},
    {"name": "solar-solve-proposal", "capabilities": []},
])
def test_run_scoped_supported_tools_behave_like_unscoped(session, run_env, tool):
    start(session, {}, "unscoped")
    start(session, {"entity_scope": BINDING}, "scoped")
    ordinary = run_env.run(tool, authority(session, "unscoped"))
    scoped = run_env.run(tool, authority(session, "scoped"))
    assert scoped.status_code == ordinary.status_code
    assert scoped.status_code < 500
    assert scoped.json().get("reason_code") == ordinary.json().get("reason_code")
    if tool["name"] != "solar-solve-proposal":
        assert scoped.status_code == 202
