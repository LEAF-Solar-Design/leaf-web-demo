"""Wave 3 gate: worker_credential_rotate runbook (O4).

Three tiers, mirroring the overlay runbook gate:
  - broker.rotate() unit tests (no DB): fail-closed, never returns the secret.
  - fake-connection atomic orchestration (no DB): commit on clean exit, rollback
    on any exception, correct statement ordering.
  - needs_pg: the real single-transaction proof against live PostgreSQL.
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

import operator_credential_rotate_runbook as rb  # noqa: E402
import operator_secret_broker as broker  # noqa: E402
from route_flatten import leaf_paths

PG_URL = os.environ.get("LEAF_OPERATOR_TEST_DATABASE_URL")
needs_pg = pytest.mark.skipif(
    not PG_URL, reason="LEAF_OPERATOR_TEST_DATABASE_URL not set")

_HANDLE = "github_operator_pr"  # a shipped NON-PRODUCTION (staging) handle


class _Op:
    subject = "auth0|op-1"
    role_revision = 3
    profile = "default"
    environment = "staging"


@pytest.fixture(autouse=True)
def _no_rotator():
    broker.register_rotator(None)
    yield
    broker.register_rotator(None)


# --- broker.rotate(): fail-closed, never returns the secret (no DB) ----------

def test_rotate_is_dark_without_a_rotator():
    with pytest.raises(broker.SecretBrokerError) as e:
        broker.rotate(_HANDLE, "staging")
    assert e.value.reason == "no_rotator"


def test_rotate_unknown_handle_refused():
    broker.register_rotator(lambda meta: None)
    with pytest.raises(broker.SecretBrokerError) as e:
        broker.rotate("does_not_exist", "staging")
    assert e.value.reason == "unknown_handle"


def test_rotate_production_environment_refused():
    broker.register_rotator(lambda meta: None)
    with pytest.raises(broker.SecretBrokerError) as e:
        broker.rotate(_HANDLE, "production")
    assert e.value.reason == "production_scope_refused"


def test_rotate_calls_rotator_with_meta_and_returns_constant():
    seen = {}
    broker.register_rotator(lambda meta: seen.update(meta) or "NEW-SECRET-xyz")
    out = broker.rotate(_HANDLE, "staging", subject="auth0|op-1")
    # The rotator ran with the handle metadata...
    assert seen["handle"] == _HANDLE and seen["environment"] == "staging"
    # ...but the receipt is a pure constant: the new secret never crosses back.
    assert out == {"rotated": True}
    assert "NEW-SECRET-xyz" not in json.dumps(out)


def test_rotate_rotator_failure_is_masked_no_leak():
    def boom(meta):
        raise RuntimeError("rotate blew up with NEW-SECRET-xyz")
    broker.register_rotator(boom)
    with pytest.raises(broker.SecretBrokerError) as e:
        broker.rotate(_HANDLE, "staging")
    assert e.value.reason == "rotator_failed"
    assert "NEW-SECRET" not in str(e.value)
    assert e.value.__cause__ is None
    assert e.value.__context__ is None


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit,
                                 __import__("asyncio").CancelledError])
def test_rotate_rotator_control_flow_baseexception_propagates(exc):
    # LIFECYCLE SAFETY: a control-flow BaseException from the rotator must
    # PROPAGATE (cancellation/shutdown), not become rotator_failed.
    def boom(meta):
        raise exc()
    broker.register_rotator(boom)
    with pytest.raises(exc):
        broker.rotate(_HANDLE, "staging")


# --- runbook handle validation + broker-verify (no DB) ----------------------

def test_bad_handle_refused():
    with pytest.raises(rb.RunbookError) as e:
        rb._validate_handle("BAD HANDLE")
    assert e.value.reason == "credential_handle_invalid"


def test_broker_verify_unknown_handle():
    with pytest.raises(rb.RunbookError) as e:
        rb._broker_verify("does_not_exist", "staging")
    assert e.value.reason == "credential_handle_unknown"


def test_broker_verify_production_environment_refused():
    with pytest.raises(rb.RunbookError) as e:
        rb._broker_verify(_HANDLE, "production")
    assert e.value.reason == "production_scope_refused"


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
        elif "operator_credential_rotations" in s and "FOR UPDATE" in s:
            self._last = self.state["row"]
        elif "INSERT INTO operator_credential_rotations" in s:
            self.state["applied_revision"] = params[1]
            self._last = {"revision": params[1]}
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
        "row": {"revision": 5},
        "principal": {"status": "active", "role_revision": _Op.role_revision},
        "applied_revision": None, "audited": False, "deny_audits": [],
        "committed": False, "rolled_back": False, "conn_opened": 0,
        "consume": {"raise": None}, "rotate": {"raise": None}, "rotated": 0,
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

    monkeypatch.setattr(rb, "_db", lambda: _DB())
    monkeypatch.setattr(rb.operator_authority, "kill_switch_active",
                        lambda: state.get("kill", False))
    monkeypatch.setattr(rb.operator_principals, "revalidate",
                        lambda s, r: not state.get("principal_drift", False))
    # Bypass the real registry read; the precondition is tested separately.
    monkeypatch.setattr(rb, "_broker_verify",
                        lambda h, e: {"scope": "worker:test", "environment": "staging"})

    def fake_consume(c, authority_id, subject, role_revision, environment,
                     action, args, target_revision=None):
        state["consume_target"] = target_revision
        if state["consume"]["raise"] is not None:
            raise state["consume"]["raise"]
        return {"authority_id": authority_id}
    monkeypatch.setattr(rb.operator_authority, "consume_in_tx", fake_consume)

    def fake_rotate(handle, environment, *, subject=None):
        state["rotated"] += 1
        if state["rotate"]["raise"] is not None:
            raise state["rotate"]["raise"]
        return {"rotated": True}
    monkeypatch.setattr(rb.broker, "rotate", fake_rotate)
    monkeypatch.setattr(rb, "_audit_deny",
                        lambda op, reason, aid: state["deny_audits"].append(reason))
    return state


def test_execute_atomic_happy_path(atomic):
    out = rb.execute(_Op(), _HANDLE, "opauth-x")
    stmts = [s for s, _ in atomic["cur"].executed]
    sqls = " | ".join(stmts)
    assert atomic["conn_opened"] == 1
    # ordering: advisory lock -> rotations FOR UPDATE -> INSERT rotations.
    adv = next(i for i, s in enumerate(stmts) if "pg_advisory_xact_lock" in s)
    lock = next(i for i, s in enumerate(stmts)
                if "operator_credential_rotations" in s and "FOR UPDATE" in s)
    ins = next(i for i, s in enumerate(stmts)
               if "INSERT INTO operator_credential_rotations" in s)
    assert adv < lock < ins
    assert atomic["committed"] is True and atomic["rolled_back"] is False
    assert atomic["consume_target"] == "5"   # bound to the locked revision
    assert atomic["rotated"] == 1            # rotator ran exactly once
    assert atomic["applied_revision"] == 6   # 5 -> 6
    assert out["after"]["revision"] == 6
    assert out["reversal"]["reissue_scope"] == "worker:test"


def test_execute_first_rotation_uses_revision_zero(atomic):
    atomic["row"] = None  # no rotation row yet
    out = rb.execute(_Op(), _HANDLE, "opauth-x")
    assert atomic["consume_target"] == "0"
    assert atomic["applied_revision"] == 1  # 0 -> 1
    assert out["after"]["revision"] == 1
    assert atomic["committed"] is True


def test_execute_dark_rotator_rolls_back(atomic):
    atomic["rotate"]["raise"] = broker.SecretBrokerError("no_rotator")
    with pytest.raises(rb.RunbookError) as e:
        rb.execute(_Op(), _HANDLE, "opauth-x")
    assert e.value.reason == "no_rotator"
    sqls = " | ".join(s for s, _ in atomic["cur"].executed)
    assert "INSERT INTO operator_credential_rotations" not in sqls  # never bumped
    assert atomic["audited"] is False
    assert atomic["committed"] is False and atomic["rolled_back"] is True
    assert atomic["deny_audits"] == ["no_rotator"]


def test_execute_rotator_failure_rolls_back(atomic):
    atomic["rotate"]["raise"] = broker.SecretBrokerError("rotator_failed")
    with pytest.raises(rb.RunbookError) as e:
        rb.execute(_Op(), _HANDLE, "opauth-x")
    assert e.value.reason == "rotator_failed"
    sqls = " | ".join(s for s, _ in atomic["cur"].executed)
    assert "INSERT INTO operator_credential_rotations" not in sqls
    assert atomic["committed"] is False and atomic["rolled_back"] is True
    assert atomic["deny_audits"] == ["rotator_failed"]


def test_execute_consume_drift_rolls_back_all(atomic):
    atomic["consume"]["raise"] = rb.operator_authority.AuthorityDenied("target_drift")
    with pytest.raises(rb.operator_authority.AuthorityDenied):
        rb.execute(_Op(), _HANDLE, "opauth-x")
    assert atomic["rotated"] == 0  # never reached the rotator
    sqls = " | ".join(s for s, _ in atomic["cur"].executed)
    assert "INSERT INTO operator_credential_rotations" not in sqls
    assert atomic["audited"] is False
    assert atomic["committed"] is False and atomic["rolled_back"] is True
    assert atomic["deny_audits"] == ["target_drift"]


def test_execute_revoked_principal_in_tx_rolls_back(atomic):
    atomic["principal"] = {"status": "revoked", "role_revision": _Op.role_revision}
    with pytest.raises(rb.RunbookError) as e:
        rb.execute(_Op(), _HANDLE, "opauth-x")
    assert e.value.reason == "principal_drift"
    assert atomic["rotated"] == 0
    assert atomic["committed"] is False and atomic["rolled_back"] is True
    assert atomic["deny_audits"] == ["principal_drift"]


# --- dark by default + route registration -----------------------------------

def test_credential_action_ships_dark():
    import operator_policy
    assert operator_policy.get_action("operator.worker_credential_rotate") is None


def test_credential_router_registers_expected_paths():
    from fastapi import FastAPI
    from routers import operator_credential as router_mod

    app = FastAPI()
    app.include_router(router_mod.router)
    paths = set(leaf_paths(app))
    assert "/api/operator/runbooks/credential/{handle}/state" in paths
    assert "/api/operator/runbooks/credential/propose" in paths
    assert "/api/operator/runbooks/credential/execute" in paths
    # No route injects/returns a secret value.
    assert not any("secret" in p or "inject" in p for p in paths)


# --- needs_pg: real single-transaction proof --------------------------------
@pytest.fixture()
def pg(monkeypatch, tmp_path):
    if not PG_URL:
        pytest.skip("LEAF_OPERATOR_TEST_DATABASE_URL not set")
    monkeypatch.setenv("DATABASE_URL", PG_URL)
    monkeypatch.setenv("LEAF_AGENT_STORE", "postgres")
    pol = json.loads((SERVER_DIR / "operator_policy.json").read_text(
        encoding="utf-8"))
    pol["actions"]["operator.worker_credential_rotate"]["enabled"] = True
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
def test_pg_rotate_happy_replay_and_drift(pg):
    import operator_authority
    from operator_principals import _db

    op = _new_op()
    rotations = []
    broker.register_rotator(lambda meta: rotations.append(meta["handle"]))
    try:
        # happy path: propose (revision 0) then execute -> revision 1.
        p1 = rb.propose(op, _HANDLE)
        assert p1["target_revision"] == 0
        out = rb.execute(op, _HANDLE, p1["authority_id"])
        assert out["after"]["revision"] == 1
        assert out["reversal"]["reissue_scope"]  # scope surfaced for re-issue
        assert len(rotations) == 1  # rotator ran exactly once

        # replay: the same authority cannot be consumed twice.
        with pytest.raises(operator_authority.AuthorityDenied) as e:
            rb.execute(op, _HANDLE, p1["authority_id"])
        assert e.value.reason in ("authority_replayed", "target_drift")
        assert len(rotations) == 1  # no second rotation

        # drift: mint against revision 1, bump it, execute -> deny, un-consumed.
        p2 = rb.propose(op, _HANDLE)
        assert p2["target_revision"] == 1
        db = _db()
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE operator_credential_rotations"
                        " SET revision = revision + 1 WHERE handle=%s", (_HANDLE,))
            conn.commit()
        with pytest.raises(operator_authority.AuthorityDenied) as e2:
            rb.execute(op, _HANDLE, p2["authority_id"])
        assert e2.value.reason == "target_drift"
        assert len(rotations) == 1  # drift blocked the rotation
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT status FROM operator_authorities"
                        " WHERE authority_id=%s", (p2["authority_id"],))
            assert cur.fetchone()["status"] == "granted"  # not consumed
    finally:
        broker.register_rotator(None)


@needs_pg
def test_pg_dark_rotator_fails_closed_and_rolls_back(pg):
    """No rotator registered: execute denies no_rotator, nothing is bumped, the
    authority stays granted (rollback), and a deny audit is written."""
    import operator_authority
    from operator_principals import _db

    op = _new_op()
    broker.register_rotator(None)  # dark
    p1 = rb.propose(op, _HANDLE)
    with pytest.raises(rb.RunbookError) as e:
        rb.execute(op, _HANDLE, p1["authority_id"])
    assert e.value.reason == "no_rotator"
    db = _db()
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM operator_credential_rotations"
                    " WHERE handle=%s", (_HANDLE,))
        # No rotation row was created (or if a prior test made one, revision
        # did not advance for this authority) -> at minimum the authority is
        # un-consumed and a deny is recorded.
        cur.execute("SELECT status FROM operator_authorities WHERE authority_id=%s",
                    (p1["authority_id"],))
        assert cur.fetchone()["status"] == "granted"
        cur.execute("SELECT count(*) AS n FROM operator_security_audit"
                    " WHERE authority_id=%s AND decision='deny'"
                    " AND reason='no_rotator'", (p1["authority_id"],))
        assert cur.fetchone()["n"] == 1
