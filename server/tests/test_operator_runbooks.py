"""Lane F gate: tenant agent pause/resume runbook.

Two tiers:
  - No-DB tier: proves the atomic orchestration deterministically with a fake
    connection that mimics psycopg pool semantics (commit on clean exit,
    rollback on exception). Establishes that authority consume + tenant CAS +
    audit run on ONE connection and that ANY failure aborts before commit, so
    nothing is applied and the authority is not consumed (rollback).
  - needs_pg tier: the real single-transaction rollback proof against a live
    empty PostgreSQL (LEAF_OPERATOR_TEST_DATABASE_URL). Skips without a DB.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import operator_runbooks as rb  # noqa: E402
from route_flatten import leaf_paths

PG_URL = os.environ.get("LEAF_OPERATOR_TEST_DATABASE_URL")
needs_pg = pytest.mark.skipif(
    not PG_URL, reason="LEAF_OPERATOR_TEST_DATABASE_URL not set")


class _Op:
    subject = "auth0|op-1"
    role_revision = 3
    profile = "default"
    environment = "staging"


# --------------------------------------------------------------------------- #
# Fake psycopg-pool connection (no DB): commit on clean exit, rollback on error
# --------------------------------------------------------------------------- #
class _FakeCursor:
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
            self.state["applied"] = {"agent_disabled": params[1],
                                     "revision": params[2]}
            self._last = self.state["applied"]
        elif "INSERT INTO operator_security_audit" in s:
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


class _FakeConn:
    def __init__(self, state):
        self.state = state

    def cursor(self):  # replaced by monkeypatch in the fixture
        return self.state["cursor"]

    # psycopg pool: the connection context commits on clean exit, rolls back
    # on exception. We record which happened.
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.state["committed"] = True
        else:
            self.state["rolled_back"] = True
        return False


@pytest.fixture()
def atomic(monkeypatch):
    """Fake DB + injectable consume result. Tenant row defaults to
    enabled@revision 5."""
    state = {
        "row": {"agent_disabled": False, "revision": 5},
        "principal": {"status": "active", "role_revision": _Op.role_revision},
        "applied": None, "audited": False, "deny_audits": [],
        "committed": False, "rolled_back": False, "conn_opened": 0,
        "consume": {"raise": None},
    }
    cur = _FakeCursor(state)
    state["cursor"] = cur

    class _DB:
        def connection(self):
            state["conn_opened"] += 1
            return _FakeConn(state)
    # conn.cursor() must return a context manager yielding our recording cursor
    class _CM:
        def __enter__(self_):
            return cur
        def __exit__(self_, *a):
            return False
    monkeypatch.setattr(_FakeConn, "cursor", lambda self: _CM())

    monkeypatch.setattr(rb, "_db", lambda: _DB())
    monkeypatch.setattr(rb.operator_authority, "kill_switch_active",
                        lambda: state.get("kill", False))
    monkeypatch.setattr(rb.operator_principals, "revalidate",
                        lambda s, r: not state.get("principal_drift", False))

    def fake_consume(c, authority_id, subject, role_revision, environment,
                     action, args, target_revision=None):
        state["consume_target"] = target_revision
        if state["consume"]["raise"] is not None:
            raise state["consume"]["raise"]
        return {"authority_id": authority_id}
    monkeypatch.setattr(rb.operator_authority, "consume_in_tx", fake_consume)
    monkeypatch.setattr(rb, "_audit_deny",
                        lambda op, action, reason, aid: state["deny_audits"].append(reason))
    return state


# --- happy path -------------------------------------------------------------

def test_execute_atomic_happy_path(atomic):
    out = rb.execute(_Op(), rb.PAUSE, "acme", "opauth-x")
    stmts = [s for s, _ in atomic["cursor"].executed]
    sqls = " | ".join(stmts)
    # One connection, all statements on it, committed.
    assert atomic["conn_opened"] == 1
    # The tenant advisory lock is taken FIRST, before reading tenant state, so
    # the missing-row (revision 0) case is serialized too.
    adv = next(i for i, s in enumerate(stmts) if "pg_advisory_xact_lock" in s)
    ten = next(i for i, s in enumerate(stmts)
               if "agent_tenant_state" in s and "FOR UPDATE" in s)
    assert adv < ten
    assert "FOR UPDATE" in sqls
    assert "INSERT INTO agent_tenant_state" in sqls
    assert "INSERT INTO operator_security_audit" in sqls
    assert atomic["committed"] is True and atomic["rolled_back"] is False
    # Authority consume was bound to the locked current revision (5).
    assert atomic["consume_target"] == "5"
    assert out["before"] == {"agent_disabled": False, "revision": 5}
    assert out["after"]["agent_disabled"] is True


# --- rollback: consume failure rolls back the whole transaction -------------

def test_execute_consume_drift_rolls_back_all(atomic):
    atomic["consume"]["raise"] = rb.operator_authority.AuthorityDenied("target_drift")
    with pytest.raises(rb.operator_authority.AuthorityDenied):
        rb.execute(_Op(), rb.PAUSE, "acme", "opauth-x")
    sqls = " | ".join(s for s, _ in atomic["cursor"].executed)
    # The apply and audit were NEVER executed; the tx rolled back; and the
    # denial was recorded (separately, so it survives the rollback).
    assert "INSERT INTO agent_tenant_state" not in sqls
    assert atomic["audited"] is False
    assert atomic["committed"] is False and atomic["rolled_back"] is True
    assert atomic["deny_audits"] == ["target_drift"]


def test_execute_precondition_conflict_rolls_back(atomic):
    atomic["row"] = {"agent_disabled": True, "revision": 5}  # already paused
    with pytest.raises(rb.RunbookError) as e:
        rb.execute(_Op(), rb.PAUSE, "acme", "opauth-x")
    assert e.value.reason == "precondition_state_conflict"
    sqls = " | ".join(s for s, _ in atomic["cursor"].executed)
    assert "INSERT INTO agent_tenant_state" not in sqls
    assert atomic["audited"] is False
    assert atomic["rolled_back"] is True and atomic["committed"] is False
    assert atomic["deny_audits"] == ["precondition_state_conflict"]


def test_execute_revoked_principal_in_tx_rolls_back(atomic):
    """A principal revoked AFTER the preflight check (still passes preflight)
    is caught by the LOCKED in-transaction check -> whole tx rolls back."""
    atomic["principal"] = {"status": "revoked", "role_revision": _Op.role_revision}
    with pytest.raises(rb.RunbookError) as e:
        rb.execute(_Op(), rb.PAUSE, "acme", "opauth-x")
    assert e.value.reason == "principal_drift"
    sqls = " | ".join(s for s, _ in atomic["cursor"].executed)
    # Locked the principal row, then aborted: no consume, no apply, no audit.
    assert "operator_principals" in sqls and "FOR UPDATE" in sqls
    assert "INSERT INTO agent_tenant_state" not in sqls
    assert atomic["committed"] is False and atomic["rolled_back"] is True
    assert atomic["deny_audits"] == ["principal_drift"]


def test_execute_role_revision_bumped_in_tx_rolls_back(atomic):
    """A role_revision bump (what revoke/suspend does) is caught in-tx."""
    atomic["principal"] = {"status": "active",
                           "role_revision": _Op.role_revision + 1}
    with pytest.raises(rb.RunbookError) as e:
        rb.execute(_Op(), rb.PAUSE, "acme", "opauth-x")
    assert e.value.reason == "principal_drift"
    assert atomic["committed"] is False and atomic["rolled_back"] is True


# --- admission fails before any transaction is opened -----------------------

def test_execute_kill_switch_denies_before_tx(atomic):
    atomic["kill"] = True
    with pytest.raises(rb.RunbookError) as e:
        rb.execute(_Op(), rb.PAUSE, "acme", "opauth-x")
    assert e.value.reason == "kill_switch_active"
    assert atomic["conn_opened"] == 0  # no transaction opened


def test_execute_principal_drift_denies_before_tx(atomic):
    atomic["principal_drift"] = True
    with pytest.raises(rb.RunbookError) as e:
        rb.execute(_Op(), rb.PAUSE, "acme", "opauth-x")
    assert e.value.reason == "principal_drift"
    assert atomic["conn_opened"] == 0


def test_execute_rejects_bad_tenant_id(atomic):
    with pytest.raises(rb.RunbookError) as e:
        rb.execute(_Op(), rb.PAUSE, "Acme Corp!", "opauth-x")
    assert e.value.reason == "tenant_id_invalid"
    assert atomic["conn_opened"] == 0


# --- propose ----------------------------------------------------------------

@pytest.fixture()
def propose_fakes(monkeypatch):
    state = {"tenant": {"agent_disabled": False, "revision": 7}, "mint": []}
    monkeypatch.setattr(rb.agent_policy, "load_tenant_state",
                        lambda t: dict(state["tenant"]))

    def fake_mint(subject, role_revision, profile, environment, session_id,
                  turn_id, action, args, target_revision=None,
                  idempotency_key=None):
        state["mint"].append({"action": action, "args": args,
                              "target_revision": target_revision})
        return {"authority_id": "opauth-fake",
                "expires_at": "2026-01-01T00:00:00+00:00"}
    monkeypatch.setattr(rb.operator_authority, "mint", fake_mint)
    return state


def test_propose_mints_bound_to_current_revision(propose_fakes):
    out = rb.propose(_Op(), rb.PAUSE, "acme")
    m = propose_fakes["mint"][0]
    assert m["target_revision"] == "7"
    assert m["args"] == {"tenant_id": "acme", "expected_state": "enabled"}
    assert out["reversal_action"] == rb.RESUME


def test_propose_precondition_conflict(propose_fakes):
    propose_fakes["tenant"] = {"agent_disabled": True, "revision": 7}
    with pytest.raises(rb.RunbookError) as e:
        rb.propose(_Op(), rb.PAUSE, "acme")
    assert e.value.reason == "precondition_state_conflict"
    assert propose_fakes["mint"] == []


def test_propose_requires_revision(propose_fakes):
    propose_fakes["tenant"] = {"agent_disabled": False, "revision": None}
    with pytest.raises(rb.RunbookError) as e:
        rb.propose(_Op(), rb.PAUSE, "acme")
    assert e.value.reason == "tenant_state_not_revisioned"


def test_propose_rejects_bad_tenant_id(propose_fakes):
    with pytest.raises(rb.RunbookError) as e:
        rb.propose(_Op(), rb.PAUSE, "../etc/passwd")
    assert e.value.reason == "tenant_id_invalid"


# --- dark by default + route registration -----------------------------------

def test_runbook_actions_ship_dark():
    import operator_policy
    assert operator_policy.get_action("operator.tenant_agent_pause") is None
    assert operator_policy.get_action("operator.tenant_agent_resume") is None


def test_runbook_router_registers_expected_paths():
    from fastapi import FastAPI
    from routers import operator_runbooks as router_mod

    app = FastAPI()
    app.include_router(router_mod.router)
    paths = set(leaf_paths(app))
    assert "/api/operator/runbooks/tenant-agent/{tenant_id}/state" in paths
    assert "/api/operator/runbooks/tenant-agent/{verb}/propose" in paths
    assert "/api/operator/runbooks/tenant-agent/{verb}/execute" in paths


def test_default_app_registers_no_operator_route():
    if os.environ.get("LEAF_OPERATOR_ENABLED", "").strip() == "1":
        pytest.skip("operator plane explicitly enabled")
    from app import app
    all_paths = leaf_paths(app)
    assert len(all_paths) > 50, "route walk broke; a vacuous empty walk must not pass this gate"
    assert [p for p in all_paths if p.startswith("/api/operator")] == []


# --------------------------------------------------------------------------- #
# needs_pg: the REAL single-transaction rollback proof
# --------------------------------------------------------------------------- #
@pytest.fixture()
def pg(monkeypatch, tmp_path):
    if not PG_URL:
        pytest.skip("LEAF_OPERATOR_TEST_DATABASE_URL not set")
    monkeypatch.setenv("DATABASE_URL", PG_URL)
    monkeypatch.setenv("LEAF_AGENT_STORE", "postgres")
    # The shipped policy ships pause/resume DARK (enabled:false). Enable them
    # for the live test via a policy-file override so the flow is exercised;
    # the committed policy stays dark (asserted by test_runbook_actions_ship_dark).
    import json
    pol = json.loads((SERVER_DIR / "operator_policy.json").read_text(
        encoding="utf-8"))
    pol["actions"]["operator.tenant_agent_pause"]["enabled"] = True
    pol["actions"]["operator.tenant_agent_resume"]["enabled"] = True
    pol_file = tmp_path / "operator_policy_enabled.json"
    pol_file.write_text(json.dumps(pol), encoding="utf-8")
    monkeypatch.setenv("LEAF_OPERATOR_POLICY_FILE", str(pol_file))
    import operator_principals
    operator_principals._db().reset_pool()
    yield
    operator_principals._db().reset_pool()


def _grant(subject):
    import subprocess
    env = dict(os.environ, DATABASE_URL=PG_URL)
    subprocess.run(
        [sys.executable, str(SERVER_DIR.parent / "scripts" /
                             "operator_principal_admin.py"), "grant", subject, "--granted-by", "test-harness"],
        capture_output=True, text=True, env=env, check=True)


def _mint_authority(op, tenant_id, revision):
    import operator_authority
    return operator_authority.mint(
        op.subject, op.role_revision, op.profile, op.environment,
        session_id="t", turn_id=None, action=rb.PAUSE,
        args=rb._args(rb.PAUSE, tenant_id), target_revision=str(revision))


@needs_pg
def test_pg_atomic_rollback_when_target_drifts(pg):
    """Mint bound to revision R, drift the tenant to R+1, then execute: the
    consume fails and the ENTIRE transaction rolls back — the authority is
    still 'granted', no audit row was written, tenant state is unchanged."""
    import operator_authority
    from operator_principals import _db

    op = _Op()
    op = type("Op", (), {"subject": f"auth0|op-{uuid.uuid4().hex[:8]}",
                         "role_revision": 1, "profile": "default",
                         "environment": "staging"})
    _grant(op.subject)
    p = __import__("operator_principals").resolve_principal(op.subject)
    op.role_revision = p.role_revision

    tenant = f"t{uuid.uuid4().hex[:10]}"
    db = _db()
    # Seed the tenant at revision 5 (enabled).
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tenant_state (tenant_id, agent_disabled, overlay,"
            " revision, updated_at) VALUES (%s, false, '{}'::jsonb, 5, now())"
            " ON CONFLICT (tenant_id) DO UPDATE SET agent_disabled=false,"
            " revision=5", (tenant,))
        conn.commit()
    minted = _mint_authority(op, tenant, 5)
    # Drift the tenant to revision 6 AFTER minting.
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE agent_tenant_state SET revision=6 WHERE tenant_id=%s",
                    (tenant,))
        conn.commit()

    with pytest.raises(operator_authority.AuthorityDenied) as e:
        rb.execute(op, rb.PAUSE, tenant, minted["authority_id"])
    assert e.value.reason == "target_drift"

    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT status, used_count FROM operator_authorities"
                    " WHERE authority_id=%s", (minted["authority_id"],))
        a = cur.fetchone()
        assert a["status"] == "granted" and a["used_count"] == 0  # NOT consumed
        cur.execute("SELECT agent_disabled, revision FROM agent_tenant_state"
                    " WHERE tenant_id=%s", (tenant,))
        t = cur.fetchone()
        assert t["agent_disabled"] is False and t["revision"] == 6  # unchanged
        cur.execute("SELECT count(*) AS n FROM operator_security_audit"
                    " WHERE authority_id=%s AND decision='execute'",
                    (minted["authority_id"],))
        assert cur.fetchone()["n"] == 0  # no applied-audit row
        # ...but the denial WAS recorded (separate transaction, survives the
        # main rollback).
        cur.execute("SELECT reason FROM operator_security_audit"
                    " WHERE authority_id=%s AND decision='deny'",
                    (minted["authority_id"],))
        assert cur.fetchone()["reason"] == "target_drift"


@needs_pg
def test_pg_atomic_happy_path_commits_all(pg):
    import operator_authority
    from operator_principals import _db, resolve_principal

    op = type("Op", (), {"subject": f"auth0|op-{uuid.uuid4().hex[:8]}",
                         "role_revision": 1, "profile": "default",
                         "environment": "staging"})
    _grant(op.subject)
    op.role_revision = resolve_principal(op.subject).role_revision

    tenant = f"t{uuid.uuid4().hex[:10]}"
    db = _db()
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tenant_state (tenant_id, agent_disabled, overlay,"
            " revision, updated_at) VALUES (%s, false, '{}'::jsonb, 5, now())",
            (tenant,))
        conn.commit()
    minted = _mint_authority(op, tenant, 5)
    out = rb.execute(op, rb.PAUSE, tenant, minted["authority_id"])
    assert out["after"]["agent_disabled"] is True

    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM operator_authorities WHERE authority_id=%s",
                    (minted["authority_id"],))
        assert cur.fetchone()["status"] == "consumed"
        cur.execute("SELECT agent_disabled FROM agent_tenant_state WHERE tenant_id=%s",
                    (tenant,))
        assert cur.fetchone()["agent_disabled"] is True
        cur.execute("SELECT count(*) AS n FROM operator_security_audit"
                    " WHERE authority_id=%s AND decision='execute'",
                    (minted["authority_id"],))
        assert cur.fetchone()["n"] == 1
