"""Demo authority matrix: deny before admission, replay, reads or APS in both modes."""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

SERVER = Path(__file__).resolve().parents[1]
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import broker
import broker_demo_guard as guard
import broker_pg_store


TENANT = "demo-session-123"
STATES = (
    "no_broker", "null_tier", "empty_tier", "demo_tier", "pro_tier",
    "disabled", "null_disabled", "no_session", "revoked", "expired",
    "null_expiry", "switch_off", "switch_true", "lookup_error", "no_database",
)
ACTIONS = (
    "run", "run_write", "plan", "extract", "upload", "admission",
    "replay", "source_read", "execute", "execute_plan", "extract_internal",
    "run_internal", "feasibility",
)


class Authority:
    """Connection seam with independently mutable broker/session records."""
    def __init__(self):
        self.now = datetime.now(timezone.utc)
        self.tenant = {"tier": "demo_live", "disabled": False}
        self.session = {"revoked_at": None, "expires_at": self.now + timedelta(hours=1)}
        self.error = None
        self.queries = []

    @contextmanager
    def connection(self):
        if self.error:
            raise self.error
        yield self

    def execute(self, sql, params):
        self.queries.append((sql, params))
        assert params == {"tenant_id": TENANT}
        # Pin all security predicates in the actual SQL sent to PostgreSQL.
        for clause in (
            "JOIN demo_sessions AS session ON session.tenant_id = tenant.tenant_id",
            "tenant.tenant_id = %(tenant_id)s", "tenant.tier = 'demo_live'",
            "tenant.disabled = FALSE", "session.revoked_at IS NULL",
            "session.expires_at > NOW()",
        ):
            assert clause in sql
        t, s = self.tenant, self.session
        allowed = bool(t is not None and t["tier"] == "demo_live"
                       and t["disabled"] is False and s is not None
                       and s["revoked_at"] is None and s["expires_at"] is not None
                       and s["expires_at"] > self.now)
        return SimpleNamespace(fetchone=lambda: {"authorized": allowed})


@pytest.fixture
def authority(monkeypatch):
    db = Authority()
    monkeypatch.setenv("LEAF_DEMO_LIVE_ENABLED", "1")
    monkeypatch.setenv("LEAF_BROKER_TENANT_TIERS", json.dumps({TENANT: "hosted_pro"}))
    monkeypatch.setattr(broker, "_tenants", {TENANT: {"tier": "demo", "disabled": False}})
    monkeypatch.setattr(broker_pg_store, "_load_db", lambda: SimpleNamespace(get_pool=lambda: db))
    return db


def deny_state(db, state, monkeypatch):
    if state == "no_broker":
        db.tenant = None
    elif state in {"null_tier", "empty_tier", "demo_tier", "pro_tier"}:
        db.tenant["tier"] = {"null_tier": None, "empty_tier": "",
                             "demo_tier": "demo", "pro_tier": "hosted_pro"}[state]
    elif state in {"disabled", "null_disabled"}:
        db.tenant["disabled"] = True if state == "disabled" else None
    elif state == "no_session":
        db.session = None
    elif state == "revoked":
        db.session["revoked_at"] = db.now
    elif state in {"expired", "null_expiry"}:
        db.session["expires_at"] = db.now if state == "expired" else None
    elif state in {"switch_off", "switch_true"}:
        monkeypatch.setenv("LEAF_DEMO_LIVE_ENABLED", "0" if state == "switch_off" else "true")
    elif state in {"lookup_error", "no_database"}:
        db.error = RuntimeError(state)


def plan_request():
    return broker.BrokerPlanRunRequest(
        tenant_id=TENANT, dwg="drawing", dwg_version=1, ledger_event_key="event",
        plan={"drawing_id": "drawing", "parent_version": 1, "mutations": {},
              "plan_sha256": "a" * 64, "source_sha256": "b" * 64})


def invoke(action):
    tool = {"name": "read-test", "engine_op": "read"}
    req = broker.BrokerRunRequest(tenant_id=TENANT, tool=tool, ledger_event_key="event")
    if action == "run_write":
        req.tool = {"name": "write-test", "engine_op": "write",
                    "capabilities": ["drawing.write"]}
    if action == "source_read":
        req.aps_live, req.dwg_version = True, 1
    if action in {"extract", "upload", "extract_internal"}:
        extract = broker.BrokerExtractRequest(
            tenant_id=TENANT, upload=action == "upload", ledger_event_key="event")
        fn = broker._broker_extract if action == "extract_internal" else broker.broker_extract
        return fn(extract)
    if action == "plan":
        return broker.broker_run_plan(plan_request())
    if action == "execute_plan":
        return broker._execute_plan(plan_request(), broker.PLAN_TOOL, "write", 0, {})
    if action == "execute":
        return broker._execute(req, tool, "read", 0, {})
    if action in {"admission", "replay"}:
        return broker._broker_run_request(req)
    if action == "run_internal":
        return broker._broker_run(req)
    if action == "feasibility":
        return broker.blank_dwg_feasibility(broker.BlankDwgFeasibilityRequest(
            tenant_id=TENANT, project_id="project", ledger_event_key="event",
            job_id="job", source_sha="a" * 40))
    return broker.broker_run(req)


def unpack(response):
    if isinstance(response, tuple):
        return response
    return json.loads(response.body), response.status_code


@pytest.mark.parametrize("mode", ["legacy", "postgres"])
@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("action", ACTIONS)
def test_denied_matrix(authority, monkeypatch, mode, state, action):
    monkeypatch.setenv("LEAF_BROKER_STORE", mode)
    deny_state(authority, state, monkeypatch)
    # Every protected boundary is a spy, including the read of a stored result.
    store = Mock()
    store.admit_run.return_value = {"status": "replay", "http_status": 200,
                                   "result": {"ok": True, "result": "stored success"}}
    spies = [store]
    for name in ("_get_da", "_get_usage", "_resolve_live_dwg", "_resolve_upload_dwg",
                 "_resolve_live_read_dwg", "_ledger_append", "tenant_disabled",
                 "_start_admitted_execution", "run_tool_dynamic", "_run_live_tool"):
        spy = Mock(side_effect=AssertionError("protected work: " + name))
        monkeypatch.setattr(broker, name, spy)
        spies.append(spy)
    monkeypatch.setattr(broker, "_postgres_store", lambda: store)
    env, status = unpack(invoke(action))
    assert status == 403
    assert env == broker.err_envelope(
        broker.ErrorCode.FORBIDDEN, "demo tenant is not authorized for broker work", False)
    for spy in spies:
        assert spy.mock_calls == []


@pytest.mark.parametrize("mode", ["legacy", "postgres"])
def test_authorized_tier_ignores_overrides(authority, monkeypatch, mode):
    monkeypatch.setenv("LEAF_BROKER_STORE", mode)
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "test")
    assert guard.demo_tenant_authorized(TENANT).authorized is True
    assert broker._tenant_tier(TENANT) == "demo_live"
    assert not any(broker.entitlements.entitlements_for("demo_live").values())
    monkeypatch.setattr(broker, "_tenants", {TENANT: {"tier": "hosted_pro", "disabled": True}})
    assert broker.tenant_disabled(TENANT) is False
    monkeypatch.setattr(broker, "tenant_disabled", lambda _: False)
    monkeypatch.setattr(broker, "_cap_preflight", lambda *args: None)
    monkeypatch.setattr(broker.write_loop, "drawing_mutations_refusal", lambda: None)
    for action in ("execute", "execute_plan"):
        env, status = unpack(invoke(action))
        assert status == 403
        assert env["error"]["error_code"] == broker.ErrorCode.ENTITLEMENT_REQUIRED


@pytest.mark.parametrize("state", ["disabled", "revoked"])
def test_previous_success_is_not_replayed_after_revocation(authority, monkeypatch, state):
    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    store = Mock()
    saved = {"ok": True, "result": "previous success"}
    store.admit_run.return_value = {"status": "replay", "http_status": 200, "result": saved}
    monkeypatch.setattr(broker, "_postgres_store", lambda: store)
    monkeypatch.setattr(broker, "_get_usage", lambda: None)
    assert unpack(invoke("replay")) == (saved, 200)
    store.reset_mock()
    deny_state(authority, state, monkeypatch)
    env, status = unpack(invoke("replay"))
    assert status == 403 and env["error"]["error_code"] == "FORBIDDEN"
    assert store.mock_calls == []


@pytest.mark.parametrize("mode", ["legacy", "postgres"])
def test_non_demo_unprovisioned_default_unchanged(authority, monkeypatch, mode):
    monkeypatch.setenv("LEAF_BROKER_STORE", mode)
    monkeypatch.setenv("LEAF_DEMO_LIVE_ENABLED", "0")
    store = Mock()
    store.tenant.return_value = None
    monkeypatch.setattr(broker, "_postgres_store", lambda: store)
    assert guard.demo_tenant_authorized("unprovisioned").authorized is True
    assert broker._demo_denial("unprovisioned") is None
    assert broker._tenant_tier("unprovisioned") == broker.entitlements.DEFAULT_TIER
    assert authority.queries == []


def test_operator_recovery_is_not_demo_guarded(authority, monkeypatch):
    monkeypatch.setenv("LEAF_DEMO_LIVE_ENABLED", "0")
    setter = Mock()
    monkeypatch.setattr(broker, "set_tenant_disabled", setter)
    broker.enable_tenant(TENANT)
    broker.disable_tenant(TENANT)
    assert setter.call_args_list == [call(TENANT, False), call(TENANT, True)]
    assert authority.queries == []


@pytest.mark.parametrize("mode", ["legacy", "postgres"])
def test_demo_operator_writes_postgres_even_with_switch_off(authority, monkeypatch, mode):
    monkeypatch.setenv("LEAF_BROKER_STORE", mode)
    monkeypatch.setenv("LEAF_DEMO_LIVE_ENABLED", "0")
    store = Mock()
    monkeypatch.setattr(broker, "_postgres_store", lambda: store)
    broker.enable_tenant(TENANT)
    broker.disable_tenant(TENANT)
    assert store.set_tenant_disabled.call_args_list == [call(TENANT, False), call(TENANT, True)]
    assert authority.queries == []


@pytest.mark.parametrize("mode", ["legacy", "postgres"])
@pytest.mark.parametrize("upload", [False, True])
def test_authorized_extraction_proceeds(authority, monkeypatch, mode, upload):
    monkeypatch.setenv("LEAF_BROKER_STORE", mode)
    monkeypatch.setattr(broker, "_get_usage", lambda: None)
    monkeypatch.setattr(broker, "_resolve_live_dwg", lambda *args: Path("source.dwg"))
    monkeypatch.setattr(broker, "_resolve_upload_dwg", lambda *args: Path("source.dwg"))
    monkeypatch.setattr(broker.write_loop, "fence_refusal", lambda: None)
    @contextmanager
    def open_fence():
        yield None
    monkeypatch.setattr(broker.write_loop, "upload_mutation_refusal_guard", open_fence)
    extract = Mock(return_value={"drawing": "intake"})
    monkeypatch.setattr(broker, "_get_da", lambda: SimpleNamespace(extract=extract))
    store = Mock()
    store.admit_run.return_value = {"status": "acquired", "lease_token": "token"}
    store.mark_execution_started.return_value = True
    monkeypatch.setattr(broker, "_postgres_store", lambda: store)
    monkeypatch.setattr(broker, "_emit_aps_metric", lambda *args: None)
    env, status = unpack(invoke("upload" if upload else "extract"))
    assert status == 200 and env["intake"] == {"drawing": "intake"}
    extract.assert_called_once_with("source.dwg")
    assert store.admit_run.call_count == (1 if mode == "postgres" else 0)


@pytest.mark.parametrize("mode", ["legacy", "postgres"])
@pytest.mark.parametrize("action", ["run", "run_write", "plan", "source_read"])
def test_authorized_requests_reach_execution(authority, monkeypatch, mode, action):
    monkeypatch.setenv("LEAF_BROKER_STORE", mode)
    monkeypatch.setattr(broker, "_get_usage", lambda: None)
    @contextmanager
    def open_fence():
        yield None
    monkeypatch.setattr(broker.write_loop, "drawing_mutation_refusal_guard", open_fence)
    # Execute spies mark the boundary; real capability refusal is covered above.
    result = broker.err_envelope(broker.ErrorCode.ENTITLEMENT_REQUIRED, "capability refused", False)
    execute = Mock(return_value=(result, 403))
    monkeypatch.setattr(broker, "_execute", execute)
    monkeypatch.setattr(broker, "_execute_plan", execute)
    monkeypatch.setattr(broker, "_ledger_append", Mock())
    monkeypatch.setattr(broker, "_emit_aps_metric", lambda *args: None)
    store = Mock()
    store.admit_run.return_value = {"status": "acquired", "lease_token": "token"}
    monkeypatch.setattr(broker, "_postgres_store", lambda: store)
    assert unpack(invoke(action)) == (result, 403)
    execute.assert_called_once()
    assert store.admit_run.call_count == (1 if mode == "postgres" else 0)


@pytest.mark.parametrize("mode", ["legacy", "postgres"])
def test_http_denial_envelope(authority, monkeypatch, mode):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("LEAF_BROKER_STORE", mode)
    monkeypatch.setenv("LEAF_DEMO_LIVE_ENABLED", "0")
    # Keep the HTTP auth boundary exercised with a valid broker secret.
    monkeypatch.setenv("LEAF_BROKER_SECRET", "test-demo-guard")
    client = TestClient(broker.app)
    bodies = (
        ("/broker/run", {"tenant_id": TENANT, "tool": {"name": "test"}}),
        ("/broker/run-plan", plan_request().model_dump()),
        ("/broker/extract", {"tenant_id": TENANT}),
    )
    for route, body in bodies:
        response = client.post(route, json=body, headers={"X-Broker-Secret": "test-demo-guard"})
        assert response.status_code == 403
        assert response.json() == broker.err_envelope(
            broker.ErrorCode.FORBIDDEN, "demo tenant is not authorized for broker work", False)
