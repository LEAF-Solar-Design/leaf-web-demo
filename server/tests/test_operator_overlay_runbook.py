"""Lane F gate: tenant_overlay_set runbook.

Validation is tested against the REAL agent policy (no DB). The atomic
orchestration is proven with a fake connection (commit on clean exit, rollback
on exception) through the shared operator_runbook_tx primitive. A needs_pg
tier is the real single-transaction proof.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import operator_overlay_runbook as rb  # noqa: E402
import operator_runbook_tx as tx  # noqa: E402

PG_URL = os.environ.get("LEAF_OPERATOR_TEST_DATABASE_URL")
needs_pg = pytest.mark.skipif(
    not PG_URL, reason="LEAF_OPERATOR_TEST_DATABASE_URL not set")


class _Op:
    subject = "auth0|op-1"
    role_revision = 3
    profile = "default"
    environment = "staging"


# --- validation against the real agent policy (no DB) -----------------------

def _first_action(policy_getter):
    import agent_policy
    pol = agent_policy.load_policy()
    auto = next((n for n, a in pol.actions.items() if a.policy == "auto"), None)
    confirm = next((n for n, a in pol.actions.items()
                    if a.policy == "always-confirm"), None)
    return auto, confirm


def test_tighten_overlay_accepted():
    auto, _ = _first_action(None)
    assert auto, "expected an auto-policy tenant action to exist"
    # Tighten an auto action up to always-confirm — valid.
    rb._validate_overlay({auto: {"policy": "always-confirm"}})


def test_loosening_overlay_refused():
    _, confirm = _first_action(None)
    assert confirm, "expected an always-confirm tenant action to exist"
    with pytest.raises(rb.RunbookError) as e:
        rb._validate_overlay({confirm: {"policy": "auto"}})
    assert e.value.reason.startswith("overlay_not_tightening")


def test_unknown_action_overlay_refused():
    with pytest.raises(rb.RunbookError) as e:
        rb._validate_overlay({"not_a_real_action": {"policy": "auto"}})
    assert e.value.reason == "overlay_unknown_action"


def test_empty_or_nonmapping_overlay_refused():
    with pytest.raises(rb.RunbookError):
        rb._validate_overlay({})
    with pytest.raises(rb.RunbookError):
        rb._validate_overlay("nope")


def test_bad_tenant_id_refused():
    with pytest.raises(rb.RunbookError) as e:
        rb.resolve_target("BAD ID")
    assert e.value.reason == "tenant_id_invalid"


# --- atomic orchestration via fake connection (no DB) -----------------------
class _Cur:
    def __init__(self, state):
        self.state = state
        self.executed = []
        self._last = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        s = " ".join(sql.split())
        if "pg_advisory_xact_lock" in s:
            self._last = None
        elif "operator_principals" in s and "FOR UPDATE" in s:
            self._last = self.state["principal"]
        elif "agent_tenant_state" in s and "FOR UPDATE" in s:
            self._last = self.state["row"]
        elif "INSERT INTO agent_tenant_state" in s:
            self.state["applied"] = {"overlay": params[1].obj if hasattr(params[1], "obj") else params[1],
                                     "revision": params[2]}
            self._last = {"overlay": self.state["overlay_after"],
                          "revision": params[2]}
        elif "INSERT INTO operator_security_audit" in s and "'execute'" in s:
            self.state["audited"] = True
            self._last = None
        else:
            self._last = None

    def fetchone(self):
        return self._last

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, state):
        self.state = state

    def cursor(self):
        return self.state["cm"]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *a):
        if exc_type is None:
            self.state["committed"] = True
        else:
            self.state["rolled_back"] = True
        return False


@pytest.fixture()
def atomic(monkeypatch):
    state = {
        "row": {"agent_disabled": False, "overlay": {}, "revision": 5},
        "principal": {"status": "active", "role_revision": _Op.role_revision},
        "overlay_after": {"run_read_tool": {"policy": "always-confirm"}},
        "applied": None, "audited": False, "deny_audits": [],
        "committed": False, "rolled_back": False, "conn_opened": 0,
        "consume": {"raise": None},
    }
    cur = _Cur(state)

    class _CM:
        def __enter__(self_):
            return cur
        def __exit__(self_, *a):
            return False
    state["cm"] = _CM()
    state["cur"] = cur

    class _DB:
        def connection(self):
            state["conn_opened"] += 1
            return _Conn(state)

    monkeypatch.setattr(tx, "_db", lambda: _DB())
    monkeypatch.setattr(tx.operator_authority, "kill_switch_active",
                        lambda: state.get("kill", False))
    monkeypatch.setattr(tx.operator_principals, "revalidate",
                        lambda s, r: not state.get("principal_drift", False))

    def fake_consume(c, authority_id, subject, role_revision, environment,
                     action, args, target_revision=None):
        state["consume_target"] = target_revision
        if state["consume"]["raise"] is not None:
            raise state["consume"]["raise"]
        return {"authority_id": authority_id}
    monkeypatch.setattr(tx.operator_authority, "consume_in_tx", fake_consume)
    monkeypatch.setattr(tx, "audit_deny",
                        lambda op, action, reason, aid: state["deny_audits"].append(reason))
    # execute() re-validates the overlay; bypass real policy in these tests.
    monkeypatch.setattr(rb, "_validate_overlay", lambda o: None)
    return state


_OVERLAY = {"run_read_tool": {"policy": "always-confirm"}}


def test_execute_atomic_happy_path(atomic):
    out = rb.execute(_Op(), "acme", _OVERLAY, "opauth-x")
    stmts = [s for s, _ in atomic["cur"].executed]
    sqls = " | ".join(stmts)
    assert atomic["conn_opened"] == 1
    adv = next(i for i, s in enumerate(stmts) if "pg_advisory_xact_lock" in s)
    ten = next(i for i, s in enumerate(stmts)
               if "agent_tenant_state" in s and "FOR UPDATE" in s)
    assert adv < ten
    assert "INSERT INTO agent_tenant_state" in sqls
    assert atomic["committed"] is True and atomic["rolled_back"] is False
    assert atomic["consume_target"] == "5"
    assert out["reversal"]["restore_overlay"] == {}  # prior overlay


def test_execute_relative_loosening_rolls_back(atomic):
    """The tenant currently tightens run_read_tool to always-confirm; a new
    overlay that loosens it (confirm-once) is refused UNDER THE LOCK, rolling
    the whole transaction back. This is the relative-loosening case: validation
    against the global base alone would have missed it."""
    atomic["row"] = {"agent_disabled": False,
                     "overlay": {"run_read_tool": {"policy": "always-confirm"}},
                     "revision": 5}
    with pytest.raises(tx.RunbookError) as e:
        rb.execute(_Op(), "acme", {"run_read_tool": {"policy": "confirm-once"}},
                   "opauth-x")
    assert e.value.reason == "overlay_would_loosen_policy"
    sqls = " | ".join(s for s, _ in atomic["cur"].executed)
    assert "INSERT INTO agent_tenant_state" not in sqls  # never applied
    assert atomic["rolled_back"] is True and atomic["committed"] is False
    assert atomic["deny_audits"] == ["overlay_would_loosen_policy"]


def test_execute_consume_drift_rolls_back_all(atomic):
    atomic["consume"]["raise"] = tx.operator_authority.AuthorityDenied("target_drift")
    with pytest.raises(tx.operator_authority.AuthorityDenied):
        rb.execute(_Op(), "acme", _OVERLAY, "opauth-x")
    sqls = " | ".join(s for s, _ in atomic["cur"].executed)
    assert "INSERT INTO agent_tenant_state" not in sqls
    assert atomic["audited"] is False
    assert atomic["committed"] is False and atomic["rolled_back"] is True
    assert atomic["deny_audits"] == ["target_drift"]


def test_execute_revoked_principal_in_tx_rolls_back(atomic):
    atomic["principal"] = {"status": "revoked", "role_revision": _Op.role_revision}
    with pytest.raises(tx.RunbookError) as e:
        rb.execute(_Op(), "acme", _OVERLAY, "opauth-x")
    assert e.value.reason == "principal_drift"
    assert atomic["committed"] is False and atomic["rolled_back"] is True
    assert atomic["deny_audits"] == ["principal_drift"]


# --- dark by default + route registration -----------------------------------

def test_overlay_action_ships_dark():
    import operator_policy
    assert operator_policy.get_action("operator.tenant_overlay_set") is None


def test_overlay_router_registers_expected_paths():
    from fastapi import FastAPI
    from routers import operator_overlay as router_mod

    app = FastAPI()
    app.include_router(router_mod.router)
    paths = {r.path for r in app.routes}
    assert "/api/operator/runbooks/tenant-overlay/{tenant_id}/state" in paths
    assert "/api/operator/runbooks/tenant-overlay/propose" in paths
    assert "/api/operator/runbooks/tenant-overlay/execute" in paths


# --- needs_pg: real single-transaction proof --------------------------------
@pytest.fixture()
def pg(monkeypatch, tmp_path):
    if not PG_URL:
        pytest.skip("LEAF_OPERATOR_TEST_DATABASE_URL not set")
    monkeypatch.setenv("DATABASE_URL", PG_URL)
    monkeypatch.setenv("LEAF_AGENT_STORE", "postgres")
    pol = json.loads((SERVER_DIR / "operator_policy.json").read_text(
        encoding="utf-8"))
    pol["actions"]["operator.tenant_overlay_set"]["enabled"] = True
    pf = tmp_path / "pol.json"
    pf.write_text(json.dumps(pol), encoding="utf-8")
    monkeypatch.setenv("LEAF_OPERATOR_POLICY_FILE", str(pf))
    import operator_principals
    operator_principals._db().reset_pool()
    yield
    operator_principals._db().reset_pool()


def _new_op():
    op = type("Op", (), {"subject": f"auth0|op-{uuid.uuid4().hex[:8]}",
                         "role_revision": 1, "profile": "default",
                         "environment": "staging"})
    import subprocess
    subprocess.run(
        [sys.executable, str(SERVER_DIR.parent / "scripts" /
                             "operator_principal_admin.py"), "grant", op.subject, "--granted-by", "test-harness"],
        capture_output=True, text=True,
        env=dict(os.environ, DATABASE_URL=PG_URL), check=True)
    import operator_principals
    op.role_revision = operator_principals.resolve_principal(op.subject).role_revision
    return op


@needs_pg
def test_pg_overlay_happy_and_drift(pg):
    import operator_authority
    from operator_principals import _db

    op = _new_op()
    tenant = f"t{uuid.uuid4().hex[:10]}"
    db = _db()
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tenant_state (tenant_id, agent_disabled, overlay,"
            " revision, updated_at) VALUES (%s, false, '{}'::jsonb, 5, now())",
            (tenant,))
        conn.commit()
    overlay = {"run_read_tool": {"policy": "always-confirm"}}
    # happy path: propose (mint) then execute (atomic apply).
    proposal = rb.propose(op, tenant, overlay)
    out = rb.execute(op, tenant, overlay, proposal["authority_id"])
    assert out["after"]["overlay"] == overlay

    # drift: mint bound to the new revision, then bump it, then execute -> deny,
    # authority stays granted, overlay unchanged, deny audit written.
    proposal2 = rb.propose(op, tenant, overlay)
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE agent_tenant_state SET revision = revision + 1"
                    " WHERE tenant_id=%s", (tenant,))
        conn.commit()
    with pytest.raises(operator_authority.AuthorityDenied) as e:
        rb.execute(op, tenant, overlay, proposal2["authority_id"])
    assert e.value.reason == "target_drift"
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM operator_authorities WHERE authority_id=%s",
                    (proposal2["authority_id"],))
        assert cur.fetchone()["status"] == "granted"
        cur.execute("SELECT count(*) AS n FROM operator_security_audit"
                    " WHERE authority_id=%s AND decision='deny'",
                    (proposal2["authority_id"],))
        assert cur.fetchone()["n"] == 1


@needs_pg
def test_pg_relative_loosening_refused(pg):
    """Live: tighten run_read_tool to always-confirm, then a new overlay that
    loosens it to confirm-once is refused and the whole tx rolls back."""
    import operator_authority
    from operator_principals import _db

    op = _new_op()
    tenant = f"t{uuid.uuid4().hex[:10]}"
    db = _db()
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tenant_state (tenant_id, agent_disabled, overlay,"
            " revision, updated_at) VALUES (%s, false, '{}'::jsonb, 5, now())",
            (tenant,))
        conn.commit()
    tight = {"run_read_tool": {"policy": "always-confirm"}}
    p1 = rb.propose(op, tenant, tight)
    rb.execute(op, tenant, tight, p1["authority_id"])  # now tightened

    loosen = {"run_read_tool": {"policy": "confirm-once"}}
    p2 = rb.propose(op, tenant, loosen)  # passes base validation (>= auto)
    with pytest.raises(rb.RunbookError) as e:
        rb.execute(op, tenant, loosen, p2["authority_id"])
    assert e.value.reason == "overlay_would_loosen_policy"
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM operator_authorities WHERE authority_id=%s",
                    (p2["authority_id"],))
        assert cur.fetchone()["status"] == "granted"  # rolled back, not consumed
        cur.execute("SELECT overlay FROM agent_tenant_state WHERE tenant_id=%s",
                    (tenant,))
        assert cur.fetchone()["overlay"] == tight  # unchanged
