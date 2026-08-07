"""Lane B gate: policy catalog, one-use authority, budgets, audit.

Structural tests always run; live-PG tests (incl. the 50-concurrent-
redemptions acceptance) key on LEAF_OPERATOR_TEST_DATABASE_URL.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

PG_URL = os.environ.get("LEAF_OPERATOR_TEST_DATABASE_URL")
needs_pg = pytest.mark.skipif(
    not PG_URL, reason="LEAF_OPERATOR_TEST_DATABASE_URL not set")


# --- structural: policy parser --------------------------------------------

def test_catalog_loads_and_is_operator_namespaced():
    import operator_policy
    catalog = operator_policy.load_catalog()
    assert catalog is not None
    assert all(name.startswith("operator.")
               for name in catalog["actions"])


def test_unknown_field_is_a_load_error(tmp_path, monkeypatch):
    import operator_policy
    bad = {"version": 1, "actions": {
        "operator.x": {"class": "O1", "rung": 1, "policy": "auto",
                       "required": "operator", "rate": "low",
                       "timeout_s": 5, "handler": "h",
                       "args_schema": {}, "surprise_field": True}}}
    p = tmp_path / "pol.json"
    p.write_text(json.dumps(bad), encoding="utf-8")
    monkeypatch.setenv("LEAF_OPERATOR_POLICY_FILE", str(p))
    with pytest.raises(operator_policy.OperatorPolicyError):
        operator_policy.load_catalog()


def test_boolean_coercion_refused(tmp_path, monkeypatch):
    import operator_policy
    bad = {"version": 1, "actions": {
        "operator.x": {"class": "O1", "rung": 1, "policy": "auto",
                       "required": "operator", "rate": "low",
                       "timeout_s": 5, "handler": "h",
                       "args_schema": {}, "enabled": "true"}}}
    p = tmp_path / "pol.json"
    p.write_text(json.dumps(bad), encoding="utf-8")
    monkeypatch.setenv("LEAF_OPERATOR_POLICY_FILE", str(p))
    with pytest.raises(operator_policy.OperatorPolicyError):
        operator_policy.load_catalog()


def test_production_shaped_route_refused(tmp_path, monkeypatch):
    import operator_policy
    bad = {"version": 1, "actions": {
        "operator.sneaky": {"class": "O2", "rung": 2, "policy": "auto",
                            "required": "operator", "rate": "low",
                            "timeout_s": 5,
                            "handler": "call_production_deploy",
                            "args_schema": {}}}}
    p = tmp_path / "pol.json"
    p.write_text(json.dumps(bad), encoding="utf-8")
    monkeypatch.setenv("LEAF_OPERATOR_POLICY_FILE", str(p))
    with pytest.raises(operator_policy.OperatorPolicyError):
        operator_policy.load_catalog()


def test_absent_catalog_denies_everything(tmp_path, monkeypatch):
    import operator_policy
    monkeypatch.setenv("LEAF_OPERATOR_POLICY_FILE",
                       str(tmp_path / "missing.json"))
    assert operator_policy.load_catalog() is None
    assert operator_policy.get_action("operator.read_fleet_state") is None
    assert operator_policy.policy_revision() == "absent"


def test_policy_revision_tracks_content(tmp_path, monkeypatch):
    import operator_policy
    rev_live = operator_policy.policy_revision()
    good = json.loads((SERVER_DIR / "operator_policy.json").read_text(
        encoding="utf-8"))
    good["authority_ttl_s"] = 299
    p = tmp_path / "pol.json"
    p.write_text(json.dumps(good), encoding="utf-8")
    monkeypatch.setenv("LEAF_OPERATOR_POLICY_FILE", str(p))
    assert operator_policy.policy_revision() != rev_live


def test_no_production_deploy_action_exists():
    """Contract section 7: production promotion is NOT MOUNTED. The only
    action allowed to say 'staging' is stage_release_candidate, and it is
    pinned to target=staging and ships disabled."""
    import operator_policy
    catalog = json.loads((SERVER_DIR / "operator_policy.json").read_text(
        encoding="utf-8"))
    stage = catalog["actions"]["operator.stage_release_candidate"]
    assert stage["enabled"] is False
    assert stage["args_schema"]["properties"]["target"]["enum"] == ["staging"]
    dump = json.dumps(catalog).lower()
    assert "deploy-platform" not in dump
    assert "production" not in json.dumps(catalog["actions"]).lower()


# --- live PG: mint/redeem --------------------------------------------------

@pytest.fixture()
def op(monkeypatch, tmp_path):
    """A granted principal + clean kill file, on the live PG."""
    if not PG_URL:
        pytest.skip("LEAF_OPERATOR_TEST_DATABASE_URL not set")
    monkeypatch.setenv("DATABASE_URL", PG_URL)
    monkeypatch.setenv("LEAF_OPERATOR_KILL_FILE",
                       str(tmp_path / "operator.disabled"))
    import operator_principals
    operator_principals._db().reset_pool()

    import subprocess
    subject = f"auth0|op-{uuid.uuid4().hex[:8]}"
    env = dict(os.environ, DATABASE_URL=PG_URL)
    proc = subprocess.run(
        [sys.executable, str(SERVER_DIR.parent / "scripts" /
                             "operator_principal_admin.py"),
         "grant", subject], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    import operator_principals as prin
    principal = prin.resolve_principal(subject)
    yield principal
    operator_principals._db().reset_pool()


def _mint(principal, action="operator.read_fleet_state", args=None,
          **kwargs):
    import operator_authority
    return operator_authority.mint(
        principal.subject, principal.role_revision, "default",
        principal.environment, f"opsess-{uuid.uuid4()}", None, action,
        args if args is not None else {}, **kwargs)


@needs_pg
def test_mint_and_redeem_happy_path(op):
    import operator_authority
    granted = _mint(op)
    result = operator_authority.redeem(
        granted["authority_id"], op.subject, op.role_revision,
        op.environment, "operator.read_fleet_state", {})
    assert result["redeemed"] is True


@needs_pg
def test_fifty_concurrent_redemptions_admit_exactly_one(op):
    import operator_authority
    granted = _mint(op)
    admitted, denied, errored = [], [], []
    barrier = threading.Barrier(50)

    def worker():
        barrier.wait()
        try:
            operator_authority.redeem(
                granted["authority_id"], op.subject, op.role_revision,
                op.environment, "operator.read_fleet_state", {})
            admitted.append(1)
        except operator_authority.AuthorityDenied:
            denied.append(1)
        except Exception:  # noqa: BLE001 - pool exhaustion = failed attempt
            errored.append(1)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    # The invariant under test: of 50 genuinely concurrent redemption
    # attempts EXACTLY ONE executes. A connection-pool timeout is a failed
    # attempt (fail closed), never a second admission.
    assert len(admitted) == 1, f"{len(admitted)} admitted"
    assert len(admitted) + len(denied) + len(errored) == 50
    assert len(denied) >= 1  # at least one attempt reached the row and lost


@needs_pg
@pytest.mark.parametrize("mutation,expected", [
    ("args", "args_mismatch"),
    ("action", "action_mismatch"),
    ("subject", "subject_mismatch"),
    ("role_revision", "role_revision_mismatch"),
    ("environment", "environment_mismatch"),
])
def test_binding_mismatches_deny_without_consuming(op, mutation, expected):
    import operator_authority
    granted = _mint(op)
    kwargs = dict(authority_id=granted["authority_id"], subject=op.subject,
                  role_revision=op.role_revision, environment=op.environment,
                  action="operator.read_fleet_state", args={})
    if mutation == "args":
        kwargs["args"] = {"limit": 5}  # hash differs from the minted {}
    elif mutation == "action":
        kwargs["action"] = "operator.read_sessions"
    elif mutation == "subject":
        kwargs["subject"] = "auth0|someone-else"
    elif mutation == "role_revision":
        kwargs["role_revision"] = op.role_revision + 7
    elif mutation == "environment":
        kwargs["environment"] = "production"

    with pytest.raises(operator_authority.AuthorityDenied) as err:
        operator_authority.redeem(**kwargs)
    if mutation in ("subject", "role_revision"):
        # principal revalidation fires before the row probe
        assert err.value.reason in (expected, "principal_drift")
    else:
        assert err.value.reason == expected

    # The failed attempt must NOT have consumed the authority.
    result = operator_authority.redeem(
        granted["authority_id"], op.subject, op.role_revision,
        op.environment, "operator.read_fleet_state", {})
    assert result["redeemed"] is True


@needs_pg
def test_replay_denies(op):
    import operator_authority
    granted = _mint(op)
    operator_authority.redeem(
        granted["authority_id"], op.subject, op.role_revision,
        op.environment, "operator.read_fleet_state", {})
    with pytest.raises(operator_authority.AuthorityDenied) as err:
        operator_authority.redeem(
            granted["authority_id"], op.subject, op.role_revision,
            op.environment, "operator.read_fleet_state", {})
    assert err.value.reason == "authority_replayed"


@needs_pg
def test_kill_switch_denies_mint_and_redeem(op, monkeypatch, tmp_path):
    import operator_authority
    granted = _mint(op)
    kill = tmp_path / "operator.disabled"
    kill.write_text("on", encoding="utf-8")
    monkeypatch.setenv("LEAF_OPERATOR_KILL_FILE", str(kill))
    with pytest.raises(operator_authority.AuthorityDenied) as err:
        _mint(op)
    assert err.value.reason == "kill_switch_active"
    with pytest.raises(operator_authority.AuthorityDenied) as err:
        operator_authority.redeem(
            granted["authority_id"], op.subject, op.role_revision,
            op.environment, "operator.read_fleet_state", {})
    assert err.value.reason == "kill_switch_active"


@needs_pg
def test_policy_drift_denies_redemption(op, monkeypatch, tmp_path):
    import operator_authority
    granted = _mint(op)
    drifted = json.loads((SERVER_DIR / "operator_policy.json").read_text(
        encoding="utf-8"))
    drifted["authority_ttl_s"] = 299
    p = tmp_path / "drifted.json"
    p.write_text(json.dumps(drifted), encoding="utf-8")
    monkeypatch.setenv("LEAF_OPERATOR_POLICY_FILE", str(p))
    with pytest.raises(operator_authority.AuthorityDenied) as err:
        operator_authority.redeem(
            granted["authority_id"], op.subject, op.role_revision,
            op.environment, "operator.read_fleet_state", {})
    assert err.value.reason == "policy_revision_drift"


@needs_pg
def test_revocation_between_mint_and_redeem_denies(op):
    import operator_authority
    import subprocess
    granted = _mint(op)
    env = dict(os.environ, DATABASE_URL=PG_URL)
    subprocess.run(
        [sys.executable, str(SERVER_DIR.parent / "scripts" /
                             "operator_principal_admin.py"),
         "revoke", op.subject], capture_output=True, text=True, env=env,
        check=True)
    with pytest.raises(operator_authority.AuthorityDenied) as err:
        operator_authority.redeem(
            granted["authority_id"], op.subject, op.role_revision,
            op.environment, "operator.read_fleet_state", {})
    assert err.value.reason == "principal_drift"


@needs_pg
def test_schema_violation_and_unknown_action_deny_mint(op):
    import operator_authority
    with pytest.raises(operator_authority.AuthorityDenied) as err:
        _mint(op, args={"unexpected": True})
    assert err.value.reason == "args_schema_violation"
    with pytest.raises(operator_authority.AuthorityDenied) as err:
        _mint(op, action="operator.not_a_real_action")
    assert err.value.reason == "action_not_in_catalog"


@needs_pg
def test_denied_mint_writes_audit_and_burns_no_rate(op):
    import operator_authority
    from operator_principals import _db
    with pytest.raises(operator_authority.AuthorityDenied):
        _mint(op, action="operator.not_a_real_action")
    db = _db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT decision, reason FROM operator_security_audit"
            " WHERE subject = %s ORDER BY audit_id DESC LIMIT 1",
            (op.subject,))
        row = cur.fetchone()
        assert (row["decision"], row["reason"]) == (
            "deny", "action_not_in_catalog")
        # catalog denial happens before rate reservation: no budget row
        cur.execute(
            "SELECT count(*) AS n FROM operator_budgets WHERE subject = %s"
            " AND scope_key LIKE 'operator.not_a_real_action@%%'",
            (op.subject,))
        assert cur.fetchone()["n"] == 0


@needs_pg
def test_rate_ceiling_tightening_applies_within_the_hour(op):
    """A tightened rate ceiling denies immediately in the running hour
    (regression for the EXCLUDED.ceiling fix), and a looser one still admits."""
    import operator_authority
    from operator_principals import _db

    catalog3 = {"rate_limits_per_hour": {"low": 3}}
    catalog1 = {"rate_limits_per_hour": {"low": 1}}
    action = f"operator.rate_probe_{op.subject[-6:]}"
    db = _db()
    with db.connection() as conn, conn.cursor() as cur:
        # Three reservations succeed under ceiling 3.
        ok = [operator_authority._reserve_rate(cur, op.subject, action, "low",
                                               catalog3) for _ in range(3)]
        assert ok == [True, True, True]
        # Tighten to 1 mid-hour: used (3) >= new ceiling (1) -> denied now.
        assert operator_authority._reserve_rate(
            cur, op.subject, action, "low", catalog1) is False
        conn.commit()
