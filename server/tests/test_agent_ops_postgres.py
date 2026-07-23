"""Shared PostgreSQL authority for agent ops, audit, and metering."""
from __future__ import annotations

import os
import platform as _stdlib_platform
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_stdlib_platform.python_implementation()

SERVER_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SERVER_DIR.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import agent_audit  # noqa: E402
import agent_ledger  # noqa: E402
import agent_pg_store  # noqa: E402
import agent_policy  # noqa: E402
from routers import ops as ops_router  # noqa: E402


def test_migration_covers_agent_ops_authorities():
    sql = (PROJECT_ROOT / "platform" / "migrations" /
           "0013_agent_state.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS agent_tenant_state" in sql
    assert "CREATE TABLE IF NOT EXISTS agent_usage_turns" in sql
    assert "idx_agent_gate_audit_session_created" in sql
    assert "token" not in agent_pg_store._USAGE_FIELDS
    assert "grant_secret" not in agent_pg_store._USAGE_FIELDS


def test_postgres_readers_never_touch_stale_files(monkeypatch):
    class Store:
        @staticmethod
        def audit_for_tenant(tenant_id, limit):
            return [{"tenant_id": tenant_id, "kind": "allowed", "limit": limit}]

        @staticmethod
        def audit_for_session(session_id, limit):
            return [{"session_id": session_id, "kind": "allowed", "limit": limit}]

        @staticmethod
        def aggregate_usage(_tenant_id):
            return {
                "today": {"turns": 1, "cost_tokens": 2, "usd_est": 0.3},
                "cycle": {"turns": 1, "cost_tokens": 2, "usd_est": 0.3},
                "estimate_basis": "self_metered",
            }

        @staticmethod
        def usage_tenants():
            return {"tenant-a": {"turns": 1, "cost_tokens": 2, "usd_est": 0.3}}

    monkeypatch.setenv("LEAF_AGENT_STORE", "postgres")
    monkeypatch.setattr(agent_audit, "_pg_store", lambda: Store)
    monkeypatch.setattr(agent_ledger, "_pg_store", lambda: Store)
    monkeypatch.setattr(
        Path, "exists",
        lambda _self: pytest.fail("PostgreSQL mode touched a stale agent file"),
    )
    assert agent_audit.for_tenant("tenant-a", 7)[0]["limit"] == 7
    assert agent_audit.for_session("session-a", 8)[0]["limit"] == 8
    assert agent_ledger.aggregate("tenant-a")["today"]["turns"] == 1
    assert "tenant-a" in agent_ledger.tenants_seen()


def test_policy_uses_injected_shared_store_and_validates_overlay(monkeypatch):
    class Store:
        @staticmethod
        def tenant_state(_tenant_id):
            return {
                "agent_disabled": True,
                "overlay": {"run_read_tool": {"policy": "always-confirm"}},
                "revision": 4,
            }

    monkeypatch.setenv("LEAF_AGENT_STORE", "postgres")
    state = agent_policy.load_tenant_state("tenant-a", pg_store=Store)
    assert state["agent_disabled"] is True
    assert state["revision"] == 4
    action = agent_policy.effective_action(
        agent_policy.load_policy(), "run_read_tool",
        tenant_overlay=state["overlay"],
    )
    assert action is not None and action.policy == "always-confirm"


def test_postgres_enable_requires_cas_revision(monkeypatch):
    class Store:
        @staticmethod
        def set_tenant_state(*_args, **_kwargs):
            pytest.fail("unsafe enable must not reach PostgreSQL")

    monkeypatch.setenv("LEAF_AGENT_STORE", "postgres")
    monkeypatch.setattr(agent_policy, "_pg_store", lambda: Store)
    with pytest.raises(agent_policy.PolicyError, match="requires its current revision"):
        agent_policy.set_tenant_agent_disabled("tenant-a", False)


def test_postgres_overlay_requires_revision_zero_on_first_write(monkeypatch):
    class Store:
        calls = []

        @classmethod
        def set_tenant_state(cls, *args, **kwargs):
            cls.calls.append((args, kwargs))
            return {
                "agent_disabled": False,
                "overlay": kwargs["overlay"],
                "revision": 1,
            }

    monkeypatch.setenv("LEAF_AGENT_STORE", "postgres")
    monkeypatch.setattr(agent_policy, "_pg_store", lambda: Store)
    overlay = {"run_read_tool": {"policy": "always-confirm"}}
    with pytest.raises(agent_policy.PolicyError, match="requires its current revision"):
        agent_policy.set_tenant_overlay("tenant-a", overlay)
    assert Store.calls == []
    result = agent_policy.set_tenant_overlay(
        "tenant-a", overlay, expected_revision=0,
        audit_event={"kind": "policy_overlay"},
    )
    assert result["revision"] == 1
    assert Store.calls[0][1]["expected_revision"] == 0
    assert Store.calls[0][1]["audit_event"]["kind"] == "policy_overlay"


def test_overlay_ops_surface_requires_secret_and_revision(monkeypatch):
    app = FastAPI()
    app.include_router(ops_router.router)
    client = TestClient(app)
    calls = []

    def replace(tid, overlay, **kwargs):
        calls.append((tid, overlay, kwargs))
        if kwargs["expected_revision"] is None:
            raise agent_policy.PolicyError(
                "replacing an agent tenant overlay requires its current revision")
        return {
            "agent_disabled": False,
            "overlay": overlay,
            "revision": kwargs["expected_revision"] + 1,
        }

    monkeypatch.setenv("LEAF_OPS_SECRET", "ops-secret")
    monkeypatch.setenv("LEAF_AGENT_STORE", "postgres")
    monkeypatch.setattr(agent_policy, "set_tenant_overlay", replace)
    body = {"overlay": {"run_read_tool": {"policy": "always-confirm"}}}
    assert client.put(
        "/api/ops/agent/tenants/tenant-a/overlay", json=body,
        headers={"X-Ops-Secret": "wrong"},
    ).status_code == 403
    assert calls == []
    assert client.put(
        "/api/ops/agent/tenants/tenant-a/overlay", json=body,
        headers={"X-Ops-Secret": "ops-secret"},
    ).status_code == 409
    response = client.put(
        "/api/ops/agent/tenants/tenant-a/overlay", json=body,
        headers={
            "X-Ops-Secret": "ops-secret",
            "X-Agent-State-Revision": "0",
        },
    )
    assert response.status_code == 200
    assert response.json()["revision"] == 1


@pytest.fixture
def postgres_agent_ops(monkeypatch):
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL is not set")
    agent_pg_store._platform_modules = None
    agent_pg_store._counter_store = None
    db, _counter_type = agent_pg_store._load_platform()
    db.apply_migration(
        PROJECT_ROOT / "platform" / "migrations" / "0013_agent_state.sql")
    prefix = f"agent-ops-{uuid.uuid4().hex}"
    monkeypatch.setenv("LEAF_AGENT_STORE", "postgres")
    yield db, prefix
    with db.cursor() as cur:
        cur.execute(
            "DELETE FROM agent_usage_turns WHERE tenant_id LIKE %(prefix)s",
            {"prefix": prefix + "%"},
        )
        cur.execute(
            "DELETE FROM agent_gate_audit_events WHERE tenant_id LIKE %(prefix)s",
            {"prefix": prefix + "%"},
        )
        cur.execute(
            "DELETE FROM agent_tenant_state WHERE tenant_id LIKE %(prefix)s",
            {"prefix": prefix + "%"},
        )


def test_two_connections_preserve_tenants_and_reject_stale_enable(
    postgres_agent_ops,
):
    _db, prefix = postgres_agent_ops
    tenants = [prefix + "-a", prefix + "-b"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda tenant: agent_pg_store.set_tenant_state(
                tenant, disabled=True, expected_revision=0),
            tenants,
        ))
    assert all(result and result["revision"] == 1 for result in results)
    assert all(agent_pg_store.tenant_state(t)["agent_disabled"] for t in tenants)

    stale_revision = agent_pg_store.tenant_state(tenants[0])["revision"]
    disabled = agent_pg_store.set_tenant_state(
        tenants[0], disabled=True, expected_revision=stale_revision)
    assert disabled and disabled["revision"] == stale_revision + 1
    stale_enable = agent_pg_store.set_tenant_state(
        tenants[0], disabled=False, expected_revision=stale_revision)
    assert stale_enable is None
    assert agent_pg_store.tenant_state(tenants[0])["agent_disabled"] is True


def test_tenant_toggle_audit_failure_rolls_back_state(
    postgres_agent_ops, monkeypatch,
):
    _db, prefix = postgres_agent_ops

    def fail_audit(_conn, _event):
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(
        agent_pg_store, "_append_audit_in_transaction", fail_audit)
    with pytest.raises(RuntimeError, match="injected audit failure"):
        agent_policy.set_tenant_agent_disabled(
            prefix, True, expected_revision=0,
            audit_event={"kind": "kill_switch", "via": "ops"},
        )
    assert agent_pg_store.tenant_state(prefix) == {
        "agent_disabled": False, "overlay": {}, "revision": 0}


def test_concurrent_overlay_replacements_cannot_erase_each_other(
    postgres_agent_ops,
):
    db, prefix = postgres_agent_ops
    overlays = [
        {"run_read_tool": {"policy": "confirm-once"}},
        {"run_write_tool": {"policy": "always-confirm"}},
    ]

    def replace(overlay):
        try:
            return agent_policy.set_tenant_overlay(
                prefix, overlay, expected_revision=0,
                audit_event={"kind": "policy_overlay", "via": "ops"},
            )
        except agent_policy.PolicyError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(replace, overlays))
    winners = [result for result in results if isinstance(result, dict)]
    losers = [result for result in results if isinstance(result, str)]
    assert len(winners) == 1
    assert losers == ["stale agent tenant state revision"]
    state = agent_pg_store.tenant_state(prefix)
    assert state["revision"] == 1
    assert state["overlay"] in overlays
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS count FROM agent_gate_audit_events
            WHERE tenant_id = %(tenant_id)s AND kind = 'policy_overlay'
            """,
            {"tenant_id": prefix},
        )
        assert int(cur.fetchone()["count"]) == 1


def test_postgres_audit_and_usage_are_shared_and_usage_is_idempotent(
    postgres_agent_ops,
):
    _db, prefix = postgres_agent_ops
    session_id = prefix + "-session"
    agent_pg_store.append_audit({
        "kind": "allowed", "tenant_id": prefix, "session_id": session_id,
    })
    assert agent_pg_store.audit_for_tenant(prefix)[-1]["kind"] == "allowed"
    assert agent_pg_store.audit_for_session(session_id)[-1]["tenant_id"] == prefix

    record = {
        "tenant_id": prefix,
        "session_id": session_id,
        "turn_id": "turn-1",
        "cost_tokens": 12,
        "usd_est": 0.125,
        "stop_reason": "end_turn",
    }
    assert agent_pg_store.append_usage(record) is True
    stored = agent_pg_store.audit_for_tenant(prefix)
    assert stored
    aggregate = agent_pg_store.aggregate_usage(prefix)
    assert aggregate["today"] == {
        "turns": 1, "cost_tokens": 12, "usd_est": 0.125}
    assert agent_pg_store.append_usage(record) is False
    with pytest.raises(RuntimeError, match="different content"):
        agent_pg_store.append_usage(dict(record, cost_tokens=99))

    stable = dict(record, turn_id="turn-2", ts="2026-07-23T12:00:00Z")
    assert agent_pg_store.append_usage(stable) is True
    assert agent_pg_store.append_usage(stable) is False
