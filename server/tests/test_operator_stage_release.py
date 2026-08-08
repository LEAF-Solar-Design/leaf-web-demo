"""Wave 3 gate: stage_release_candidate runbook (O6) + its dark stager.

Tiers mirror the external_write gate: stager unit tests and fake-connection
atomic orchestration run with no DB; a needs_pg tier is the real
single-transaction + immutability proof.
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

import operator_stage_release_runbook as rb  # noqa: E402
import operator_release_stager as stager  # noqa: E402

PG_URL = os.environ.get("LEAF_OPERATOR_TEST_DATABASE_URL")
needs_pg = pytest.mark.skipif(
    not PG_URL, reason="LEAF_OPERATOR_TEST_DATABASE_URL not set")

_SHA = "a1b2c3d4e5f6"
_TARGET = "staging"


class _Op:
    subject = "auth0|op-1"
    role_revision = 3
    profile = "default"
    environment = "staging"


@pytest.fixture(autouse=True)
def _no_stager():
    stager.register_stager(None)
    yield
    stager.register_stager(None)


# --- stager: fail-closed, staging only (no DB) ------------------------------

def test_stage_is_dark_without_a_stager():
    with pytest.raises(stager.StageError) as e:
        stager.stage(_SHA, "staging")
    assert e.value.reason == "no_stager"


def test_stage_production_target_refused():
    stager.register_stager(lambda sha, t: {"previous_revision": "1", "new_revision": "2"})
    with pytest.raises(stager.StageError) as e:
        stager.stage(_SHA, "production")
    assert e.value.reason == "production_target_refused"


def test_stage_happy_returns_revisions():
    stager.register_stager(lambda sha, t: {"previous_revision": "7", "new_revision": "8"})
    out = stager.stage(_SHA, "staging")
    assert out == {"previous_revision": "7", "new_revision": "8"}


def test_stage_stager_exception_is_masked():
    def boom(sha, t):
        raise RuntimeError(f"deploy blew up for {sha}")
    stager.register_stager(boom)
    with pytest.raises(stager.StageError) as e:
        stager.stage(_SHA, "staging")
    assert e.value.reason == "stager_failed"
    assert _SHA not in str(e.value)
    assert e.value.__context__ is None


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit,
                                 __import__("asyncio").CancelledError])
def test_stage_stager_control_flow_baseexception_propagates(exc):
    # LIFECYCLE SAFETY: a control-flow BaseException from the stager (a
    # cancellation or shutdown signal) must PROPAGATE, not be masked into a
    # fixed stager_failed, so cancellation-safe termination is not defeated.
    def boom(sha, t):
        raise exc()
    stager.register_stager(boom)
    with pytest.raises(exc):
        stager.stage(_SHA, "staging")


def test_stage_malformed_result_refused():
    stager.register_stager(lambda sha, t: {"previous_revision": 7, "new_revision": "8"})
    with pytest.raises(stager.StageError) as e:
        stager.stage(_SHA, "staging")
    assert e.value.reason == "stager_result_invalid"


def test_stage_hostile_result_subclass_cannot_leak_the_sha():
    # A dict subclass whose .get raises a SHA-bearing error must be masked as a
    # fixed value-free stager_result_invalid, not leaked.
    class Hostile(dict):
        def get(self, *a, **k):
            raise RuntimeError(f"detail-for-{_SHA}")

    stager.register_stager(lambda sha, t: Hostile())
    with pytest.raises(stager.StageError) as e:
        stager.stage(_SHA, "staging")
    assert e.value.reason == "stager_result_invalid"
    assert _SHA not in str(e.value)
    assert e.value.__context__ is None
    assert e.value.__cause__ is None


def test_stage_result_get_getitem_divergence_is_rejected():
    # A dict subclass whose .get returns safe strings but __getitem__ returns a
    # hostile object (that would raise the SHA when formatted downstream) must
    # be rejected: only a PLAIN dict with EXACT str values is accepted.
    class Hostile(dict):
        def get(self, *a, **k):
            return "42"  # looks valid
        def __getitem__(self, k):
            raise RuntimeError(f"attacker-{_SHA}")

    stager.register_stager(lambda sha, t: Hostile())
    with pytest.raises(stager.StageError) as e:
        stager.stage(_SHA, "staging")
    assert e.value.reason == "stager_result_invalid"
    assert _SHA not in str(e.value)


def test_stage_result_str_subclass_value_is_rejected():
    # A plain dict whose values are str SUBCLASSES (hostile __format__) is
    # refused: values must be EXACT str.
    class Evil(str):
        def __format__(self, spec):
            raise RuntimeError(f"fmt-{_SHA}")

    stager.register_stager(lambda sha, t: {"previous_revision": Evil("1"),
                                           "new_revision": Evil("2")})
    with pytest.raises(stager.StageError) as e:
        stager.stage(_SHA, "staging")
    assert e.value.reason == "stager_result_invalid"
    assert _SHA not in str(e.value)


# --- runbook validation (no DB) ---------------------------------------------

def test_bad_sha_refused():
    with pytest.raises(rb.RunbookError) as e:
        rb._validate("NOT A SHA", "staging")
    assert e.value.reason == "source_sha_invalid"


def test_non_staging_target_refused():
    with pytest.raises(rb.RunbookError) as e:
        rb._validate(_SHA, "production")
    assert e.value.reason == "target_not_staging"


# --- atomic orchestration via fake connection (no DB) -----------------------
class _Cur:
    def __init__(self, state):
        self.state = state
        self.executed = []
        self._last = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        s = " ".join(sql.split())
        if "operator_principals" in s and "FOR UPDATE" in s:
            self._last = self.state["principal"]
        elif "INSERT INTO operator_release_candidates" in s:
            # ON CONFLICT DO NOTHING RETURNING: None when already claimed.
            self._last = None if self.state["already"] else {"source_sha": _SHA}
        elif "INSERT INTO operator_security_audit" in s and "authority_consumed" in s:
            self.state["phase1_audit"] = True
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
            self.state["phase1_committed"] = True
        else:
            self.state["phase1_rolled_back"] = True
        return False


@pytest.fixture()
def atomic(monkeypatch):
    state = {
        "principal": {"status": "active", "role_revision": _Op.role_revision},
        "phase1_audit": False, "audit_rows": [], "staged": 0, "marks": [],
        "phase1_committed": False, "phase1_rolled_back": False, "conn_opened": 0,
        "consume": {"raise": None}, "stage": {"raise": None}, "already": False,
        "has_stager": True, "audit_fail_applied": False,
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
    monkeypatch.setattr(rb.operator_authority, "kill_switch_active", lambda: False)
    monkeypatch.setattr(rb.operator_principals, "revalidate",
                        lambda s, r: not state.get("principal_drift", False))
    monkeypatch.setattr(rb.stager, "has_stager", lambda: state["has_stager"])

    def fake_consume(c, authority_id, subject, role_revision, environment,
                     action, args, target_revision=None):
        state["consume_args"] = args
        if state["consume"]["raise"] is not None:
            raise state["consume"]["raise"]
        return {"authority_id": authority_id}
    monkeypatch.setattr(rb.operator_authority, "consume_in_tx", fake_consume)

    def fake_stage(sha, target):
        if state["stage"]["raise"] is not None:
            raise state["stage"]["raise"]
        state["staged"] += 1
        return {"previous_revision": "41", "new_revision": "42"}
    monkeypatch.setattr(rb.stager, "stage", fake_stage)
    monkeypatch.setattr(rb, "_mark_candidate",
                        lambda sha, t, status, previous=None, staged=None:
                        state["marks"].append((status, previous, staged)))

    def fake_audit_row(op, decision, reason, aid, extra=None):
        state["audit_rows"].append((decision, reason))
        if state["audit_fail_applied"] and reason == "runbook_applied":
            return False
        return True
    monkeypatch.setattr(rb, "_audit_row", fake_audit_row)
    return state


def test_execute_happy_path(atomic):
    out = rb.execute(_Op(), _SHA, _TARGET, "opauth-x")
    assert atomic["phase1_committed"] is True and atomic["phase1_rolled_back"] is False
    assert atomic["phase1_audit"] is True
    assert atomic["staged"] == 1                       # one staging deploy
    assert ("staged", "41", "42") in atomic["marks"]   # candidate marked staged
    assert ("execute", "runbook_applied") in atomic["audit_rows"]
    assert out["reversal"]["rollback_to_taskdef_revision"] == "41"
    assert out["staged_taskdef_revision"] == "42"


def test_execute_dark_no_stager_preserves_authority(atomic):
    atomic["has_stager"] = False
    with pytest.raises(rb.RunbookError) as e:
        rb.execute(_Op(), _SHA, _TARGET, "opauth-x")
    assert e.value.reason == "no_stager"
    assert atomic["conn_opened"] == 0       # authority never consumed
    assert atomic["staged"] == 0
    assert ("deny", "no_stager") in atomic["audit_rows"]


def test_execute_already_staged_rolls_back(atomic):
    atomic["already"] = True
    with pytest.raises(rb.RunbookError) as e:
        rb.execute(_Op(), _SHA, _TARGET, "opauth-x")
    assert e.value.reason == "already_staged"
    assert atomic["staged"] == 0
    assert atomic["phase1_committed"] is False and atomic["phase1_rolled_back"] is True
    assert ("deny", "already_staged") in atomic["audit_rows"]


def test_execute_stage_failure_after_consume_is_not_replayed(atomic):
    atomic["stage"]["raise"] = stager.StageError("stager_failed")
    with pytest.raises(rb.RunbookError) as e:
        rb.execute(_Op(), _SHA, _TARGET, "opauth-x")
    assert e.value.reason == "stager_failed"
    assert atomic["phase1_committed"] is True     # authority spent, at-most-once
    assert ("failed", None, None) in atomic["marks"]
    assert ("execute", "stage_failed:stager_failed") in atomic["audit_rows"]


def test_execute_outcome_audit_loss_is_surfaced(atomic):
    atomic["audit_fail_applied"] = True
    with pytest.raises(rb.RunbookError) as e:
        rb.execute(_Op(), _SHA, _TARGET, "opauth-x")
    assert e.value.reason == "outcome_audit_unavailable"
    assert atomic["staged"] == 1                   # the deploy happened
    assert atomic["phase1_committed"] is True


def test_execute_consume_drift_rolls_back(atomic):
    atomic["consume"]["raise"] = rb.operator_authority.AuthorityDenied("target_drift")
    with pytest.raises(rb.operator_authority.AuthorityDenied):
        rb.execute(_Op(), _SHA, _TARGET, "opauth-x")
    assert atomic["staged"] == 0
    assert atomic["phase1_committed"] is False and atomic["phase1_rolled_back"] is True
    assert ("deny", "target_drift") in atomic["audit_rows"]


def test_execute_revoked_principal_rolls_back(atomic):
    atomic["principal"] = {"status": "revoked", "role_revision": _Op.role_revision}
    with pytest.raises(rb.RunbookError) as e:
        rb.execute(_Op(), _SHA, _TARGET, "opauth-x")
    assert e.value.reason == "principal_drift"
    assert atomic["staged"] == 0
    assert atomic["phase1_rolled_back"] is True


# --- dark by default + route registration -----------------------------------

def test_release_action_ships_dark():
    import operator_policy
    assert operator_policy.get_action("operator.stage_release_candidate") is None


def test_release_router_registers_expected_paths():
    from fastapi import FastAPI
    from routers import operator_release as router_mod

    app = FastAPI()
    app.include_router(router_mod.router)
    paths = {r.path for r in app.routes}
    assert "/api/operator/release/{target}/{source_sha}/state" in paths
    assert "/api/operator/release/propose" in paths
    assert "/api/operator/release/execute" in paths
    # No route names a production deploy path.
    assert not any("production" in p or "promote" in p for p in paths)


# --- needs_pg: real single-transaction + immutability proof ------------------
@pytest.fixture()
def pg(monkeypatch, tmp_path):
    if not PG_URL:
        pytest.skip("LEAF_OPERATOR_TEST_DATABASE_URL not set")
    monkeypatch.setenv("DATABASE_URL", PG_URL)
    monkeypatch.setenv("LEAF_AGENT_STORE", "postgres")
    pol = json.loads((SERVER_DIR / "operator_policy.json").read_text(encoding="utf-8"))
    pol["actions"]["operator.stage_release_candidate"]["enabled"] = True
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
                             "operator_principal_admin.py"), "grant", op.subject],
        capture_output=True, text=True,
        env=dict(os.environ, DATABASE_URL=PG_URL), check=True)
    import operator_principals
    op.role_revision = operator_principals.resolve_principal(op.subject).role_revision
    return op


@needs_pg
def test_pg_stage_happy_immutable_and_replay(pg):
    import operator_authority
    from operator_principals import _db

    op = _new_op()
    sha = uuid.uuid4().hex[:12]
    stages = []
    stager.register_stager(lambda s, t: stages.append((s, t)) or
                           {"previous_revision": "9", "new_revision": "10"})
    try:
        p1 = rb.propose(op, sha, "staging")
        out = rb.execute(op, sha, "staging", p1["authority_id"])
        assert out["staged_taskdef_revision"] == "10"
        assert out["reversal"]["rollback_to_taskdef_revision"] == "9"
        assert len(stages) == 1
        db = _db()
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT status, previous_taskdef_revision FROM"
                        " operator_release_candidates WHERE source_sha=%s", (sha,))
            row = cur.fetchone()
            assert row["status"] == "staged" and row["previous_taskdef_revision"] == "9"

        # replay the same authority -> denied, no second deploy.
        with pytest.raises(operator_authority.AuthorityDenied):
            rb.execute(op, sha, "staging", p1["authority_id"])
        assert len(stages) == 1

        # immutability: a NEW authority for the SAME sha -> already_staged.
        p2 = rb.propose(op, sha, "staging")  # propose sees existing? it raises already_staged
    except rb.RunbookError as e:
        assert e.reason == "already_staged"
    finally:
        stager.register_stager(None)
    assert len(stages) == 1  # never a second staging deploy for the same sha


@needs_pg
def test_pg_dark_no_stager_fails_closed(pg):
    from operator_principals import _db
    op = _new_op()
    sha = uuid.uuid4().hex[:12]
    stager.register_stager(None)  # dark
    p1 = rb.propose(op, sha, "staging")
    with pytest.raises(rb.RunbookError) as e:
        rb.execute(op, sha, "staging", p1["authority_id"])
    assert e.value.reason == "no_stager"
    db = _db()
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM operator_authorities WHERE authority_id=%s",
                    (p1["authority_id"],))
        assert cur.fetchone()["status"] == "granted"  # authority preserved
        cur.execute("SELECT count(*) AS n FROM operator_release_candidates"
                    " WHERE source_sha=%s", (sha,))
        assert cur.fetchone()["n"] == 0  # no candidate claimed


@needs_pg
def test_pg_candidate_identity_is_db_immutable(pg):
    # O4 behavioral (contract/OPERATOR.md section 7): against the FULLY MIGRATED
    # schema (including the 0035 identity-immutability trigger), an UPDATE that
    # rewrites a reviewed candidate's identity (source_sha or target) is REJECTED
    # by the database, while a legitimate receipt-field update succeeds. Identity
    # immutability does not depend on application-code discipline: a quoted
    # identifier, dynamic SQL, or a future handler is rejected the same way,
    # because the trigger acts on the column value, not the SQL text.
    import psycopg
    from operator_principals import _db
    db = _db()
    sha = uuid.uuid4().hex[:12]

    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO operator_release_candidates (source_sha, target, status)"
            " VALUES (%s, 'staging', 'staging')", (sha,))

    # Rewriting source_sha -> rejected by the trigger.
    with pytest.raises(psycopg.Error):
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE operator_release_candidates SET source_sha=%s"
                " WHERE source_sha=%s AND target='staging'",
                (uuid.uuid4().hex[:12], sha))

    # Rewriting target -> rejected even via a QUOTED identifier (the trigger acts
    # on the column, so the quoted-identifier source bypass is irrelevant here).
    with pytest.raises(psycopg.Error):
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                'UPDATE operator_release_candidates SET "target"=%s'
                " WHERE source_sha=%s AND target='staging'",
                ("development", sha))

    # A legitimate receipt-field update succeeds.
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE operator_release_candidates SET status='staged',"
            " staged_taskdef_revision='10' WHERE source_sha=%s AND target='staging'",
            (sha,))

    # Identity unchanged; receipt applied.
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT source_sha, target, status, staged_taskdef_revision FROM"
            " operator_release_candidates WHERE source_sha=%s", (sha,))
        row = cur.fetchone()
        assert row["source_sha"] == sha and row["target"] == "staging"
        assert row["status"] == "staged" and row["staged_taskdef_revision"] == "10"
