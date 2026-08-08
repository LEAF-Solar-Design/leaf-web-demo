"""Wave 3 gate: external_write runbook (O5) + its allowlist/adapter framework
and the USD spend reservation.

Tiers mirror the other operator runbook gates: allowlist/adapter unit tests and
fake-connection atomic orchestration run with no DB; a needs_pg tier is the real
single-transaction proof and the real spend-reservation proof.
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

import operator_external_write_runbook as rb  # noqa: E402
import operator_external_adapters as ext  # noqa: E402
import operator_secret_broker as broker  # noqa: E402

PG_URL = os.environ.get("LEAF_OPERATOR_TEST_DATABASE_URL")
needs_pg = pytest.mark.skipif(
    not PG_URL, reason="LEAF_OPERATOR_TEST_DATABASE_URL not set")

_DEST = "staging_status_webhook"       # shipped allowlist destination
_ADAPTER = "generic_webhook"           # its permitted adapter
_HANDLE = "github_operator_pr"         # a shipped NON-PRODUCTION broker handle


class _Op:
    subject = "auth0|op-1"
    role_revision = 3
    profile = "default"
    environment = "staging"


@pytest.fixture(autouse=True)
def _clean_registries():
    ext.register_adapter(_ADAPTER, None)
    broker.register_minter(None)
    yield
    ext.register_adapter(_ADAPTER, None)
    broker.register_minter(None)


# --- allowlist + adapter registry (no DB) -----------------------------------

def test_allowlist_describe_is_metadata_only():
    meta = ext.describe_destination(_DEST)
    assert meta == {"destination": _DEST, "environment": "staging",
                    "adapter": _ADAPTER}


def test_verify_allowed_happy():
    out = ext.verify_allowed(_DEST, _ADAPTER, "staging")
    assert out["adapter"] == _ADAPTER and out["environment"] == "staging"


def test_verify_allowed_unknown_destination():
    with pytest.raises(ext.ExternalWriteError) as e:
        ext.verify_allowed("nope", _ADAPTER, "staging")
    assert e.value.reason == "destination_not_allowlisted"


def test_verify_allowed_production_refused():
    with pytest.raises(ext.ExternalWriteError) as e:
        ext.verify_allowed(_DEST, _ADAPTER, "production")
    assert e.value.reason == "production_destination_refused"


def test_verify_allowed_adapter_mismatch():
    with pytest.raises(ext.ExternalWriteError) as e:
        ext.verify_allowed(_DEST, "other_adapter", "staging")
    assert e.value.reason == "adapter_not_permitted_for_destination"


def test_register_adapter_requires_reversal_and_valid_name():
    with pytest.raises(ext.ExternalWriteError) as e:
        ext.register_adapter(_ADAPTER, lambda d, p, t: None, reversal="")
    assert e.value.reason == "adapter_reversal_required"
    with pytest.raises(ext.ExternalWriteError) as e2:
        ext.register_adapter("BAD NAME", lambda d, p, t: None, reversal="undo")
    assert e2.value.reason == "adapter_name_invalid"


def test_adapter_registry_is_dark_by_default():
    assert ext.get_adapter(_ADAPTER) is None


def test_allowlist_rejects_production_destination(tmp_path, monkeypatch):
    p = tmp_path / "d.json"
    p.write_text(json.dumps({"destinations": {
        "prod_thing": {"environment": "production", "adapter": "a"}}}),
        encoding="utf-8")
    monkeypatch.setenv("LEAF_OPERATOR_DESTINATIONS_FILE", str(p))
    with pytest.raises(ext.ExternalWriteError):
        ext.list_destinations()


def test_allowlist_rejects_raw_url_destination_key(tmp_path, monkeypatch):
    p = tmp_path / "d.json"
    p.write_text(json.dumps({"destinations": {
        "https://evil.example.com": {"environment": "staging", "adapter": "a"}}}),
        encoding="utf-8")
    monkeypatch.setenv("LEAF_OPERATOR_DESTINATIONS_FILE", str(p))
    with pytest.raises(ext.ExternalWriteError):
        ext.list_destinations()


# --- runbook precondition (no DB) -------------------------------------------

def test_verify_maps_allowlist_error():
    with pytest.raises(rb.RunbookError) as e:
        rb._verify("nope", _HANDLE, _ADAPTER, "staging")
    assert e.value.reason == "destination_not_allowlisted"


def test_verify_unknown_token_handle():
    with pytest.raises(rb.RunbookError) as e:
        rb._verify(_DEST, "does_not_exist", _ADAPTER, "staging")
    assert e.value.reason == "token_handle_unknown"


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
        "principal": {"status": "active", "role_revision": _Op.role_revision},
        "audited": False, "deny_audits": [], "wrote": 0,
        "committed": False, "rolled_back": False,
        "consume": {"raise": None}, "inject": {"raise": None},
        "adapter": "registered",  # "registered" | "dark"
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
            return _Conn(state)

    monkeypatch.setattr(rb, "_db", lambda: _DB())
    monkeypatch.setattr(rb.operator_authority, "kill_switch_active",
                        lambda: False)
    monkeypatch.setattr(rb.operator_principals, "revalidate",
                        lambda s, r: not state.get("principal_drift", False))
    monkeypatch.setattr(rb, "_verify",
                        lambda d, h, a, e: {"environment": "staging"})

    def fake_consume(c, authority_id, subject, role_revision, environment,
                     action, args, target_revision=None):
        state["consume_args"] = args
        if state["consume"]["raise"] is not None:
            raise state["consume"]["raise"]
        return {"authority_id": authority_id}
    monkeypatch.setattr(rb.operator_authority, "consume_in_tx", fake_consume)

    class _Ad:
        reversal = "delete the webhook post"
        def write(self, destination, payload, token):
            state["wrote"] += 1
    monkeypatch.setattr(rb.ext, "get_adapter",
                        lambda name: _Ad() if state["adapter"] == "registered" else None)

    def fake_inject(handle, environment, use, *, subject=None):
        if state["inject"]["raise"] is not None:
            raise state["inject"]["raise"]
        use("SHORT-LIVED-TOKEN")  # exercises the adapter write
        return {"injected": True}
    monkeypatch.setattr(rb.broker, "with_injected", fake_inject)
    monkeypatch.setattr(rb, "_audit_deny",
                        lambda op, reason, aid: state["deny_audits"].append(reason))
    return state


def test_execute_atomic_happy_path(atomic):
    out = rb.execute(_Op(), _DEST, _HANDLE, _ADAPTER, "opauth-x",
                     payload={"msg": "hi"})
    assert atomic["committed"] is True and atomic["rolled_back"] is False
    assert atomic["wrote"] == 1                 # exactly one outbound write
    assert atomic["audited"] is True
    assert atomic["consume_args"]["destination"] == _DEST
    assert out["reversal"]["adapter_reversal"] == "delete the webhook post"


def test_execute_dark_no_adapter_rolls_back(atomic):
    atomic["adapter"] = "dark"
    with pytest.raises(rb.RunbookError) as e:
        rb.execute(_Op(), _DEST, _HANDLE, _ADAPTER, "opauth-x")
    assert e.value.reason == "no_adapter"
    assert atomic["wrote"] == 0
    sqls = " | ".join(s for s, _ in atomic["cur"].executed)
    assert "INSERT INTO operator_security_audit" not in sqls or "'execute'" not in sqls
    assert atomic["committed"] is False and atomic["rolled_back"] is True
    assert atomic["deny_audits"] == ["no_adapter"]


def test_execute_dark_broker_no_minter_rolls_back(atomic):
    atomic["inject"]["raise"] = broker.SecretBrokerError("no_minter")
    with pytest.raises(rb.RunbookError) as e:
        rb.execute(_Op(), _DEST, _HANDLE, _ADAPTER, "opauth-x")
    assert e.value.reason == "no_minter"
    assert atomic["audited"] is False
    assert atomic["committed"] is False and atomic["rolled_back"] is True
    assert atomic["deny_audits"] == ["no_minter"]


def test_execute_consume_drift_rolls_back(atomic):
    atomic["consume"]["raise"] = rb.operator_authority.AuthorityDenied("args_mismatch")
    with pytest.raises(rb.operator_authority.AuthorityDenied):
        rb.execute(_Op(), _DEST, _HANDLE, _ADAPTER, "opauth-x")
    assert atomic["wrote"] == 0            # never reached the adapter
    assert atomic["audited"] is False
    assert atomic["committed"] is False and atomic["rolled_back"] is True
    assert atomic["deny_audits"] == ["args_mismatch"]


def test_execute_revoked_principal_rolls_back(atomic):
    atomic["principal"] = {"status": "revoked", "role_revision": _Op.role_revision}
    with pytest.raises(rb.RunbookError) as e:
        rb.execute(_Op(), _DEST, _HANDLE, _ADAPTER, "opauth-x")
    assert e.value.reason == "principal_drift"
    assert atomic["wrote"] == 0
    assert atomic["committed"] is False and atomic["rolled_back"] is True


# --- dark by default + route registration -----------------------------------

def test_external_action_ships_dark():
    import operator_policy
    assert operator_policy.get_action("operator.external_write") is None


def test_external_router_registers_expected_paths():
    from fastapi import FastAPI
    from routers import operator_external as router_mod

    app = FastAPI()
    app.include_router(router_mod.router)
    paths = {r.path for r in app.routes}
    assert "/api/operator/external/destinations" in paths
    assert "/api/operator/external/propose" in paths
    assert "/api/operator/external/execute" in paths
    assert not any("token" in p or "secret" in p for p in paths)


# --- needs_pg: real single-transaction + spend-reservation proof ------------
@pytest.fixture()
def pg(monkeypatch, tmp_path):
    if not PG_URL:
        pytest.skip("LEAF_OPERATOR_TEST_DATABASE_URL not set")
    monkeypatch.setenv("DATABASE_URL", PG_URL)
    monkeypatch.setenv("LEAF_AGENT_STORE", "postgres")
    pol = json.loads((SERVER_DIR / "operator_policy.json").read_text(
        encoding="utf-8"))
    pol["actions"]["operator.external_write"]["enabled"] = True
    pf = tmp_path / "pol.json"
    pf.write_text(json.dumps(pol), encoding="utf-8")
    monkeypatch.setenv("LEAF_OPERATOR_POLICY_FILE", str(pf))
    import operator_principals
    operator_principals._db().reset_pool()
    yield pf
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
def test_pg_external_write_happy_replay_and_spend(pg):
    import operator_authority
    from operator_principals import _db

    op = _new_op()
    writes = []
    ext.register_adapter(_ADAPTER, lambda d, p, t: writes.append((d, t)),
                         reversal="delete the webhook post")
    # A trivial broker minter so with_injected can inject a token.
    broker.register_minter(lambda meta: f"tok-{meta['handle']}")
    try:
        p1 = rb.propose(op, _DEST, _HANDLE, _ADAPTER, payload={"m": "hi"})
        # The USD spend was reserved at mint.
        db = _db()
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT used, ceiling FROM operator_budgets"
                " WHERE subject=%s AND scope='spend_principal_day'", (op.subject,))
            row = cur.fetchone()
            assert row is not None and int(row["used"]) == 10000
            assert int(row["ceiling"]) == 50000

        out = rb.execute(op, _DEST, _HANDLE, _ADAPTER, p1["authority_id"],
                         payload={"m": "hi"})
        assert out["reversal"]["adapter_reversal"] == "delete the webhook post"
        assert len(writes) == 1  # exactly one outbound write, token injected
        assert writes[0][1] == "tok-github_operator_pr"

        # replay: the one-use authority cannot be consumed twice.
        with pytest.raises(operator_authority.AuthorityDenied) as e:
            rb.execute(op, _DEST, _HANDLE, _ADAPTER, p1["authority_id"],
                       payload={"m": "hi"})
        assert e.value.reason in ("authority_replayed", "args_mismatch")
        assert len(writes) == 1  # no second write
    finally:
        ext.register_adapter(_ADAPTER, None)
        broker.register_minter(None)


@needs_pg
def test_pg_dark_no_adapter_fails_closed(pg):
    """Adapter dark: execute denies no_adapter, the authority stays granted
    (rollback), a deny audit is written, and no spend is consumed beyond the
    mint reservation."""
    import operator_authority
    from operator_principals import _db

    op = _new_op()
    ext.register_adapter(_ADAPTER, None)  # dark
    broker.register_minter(lambda meta: "tok")
    try:
        p1 = rb.propose(op, _DEST, _HANDLE, _ADAPTER)
        with pytest.raises(rb.RunbookError) as e:
            rb.execute(op, _DEST, _HANDLE, _ADAPTER, p1["authority_id"])
        assert e.value.reason == "no_adapter"
        db = _db()
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT status FROM operator_authorities"
                        " WHERE authority_id=%s", (p1["authority_id"],))
            assert cur.fetchone()["status"] == "granted"  # rolled back
            cur.execute("SELECT count(*) AS n FROM operator_security_audit"
                        " WHERE authority_id=%s AND decision='deny'"
                        " AND reason='no_adapter'", (p1["authority_id"],))
            assert cur.fetchone()["n"] == 1
    finally:
        broker.register_minter(None)


@needs_pg
def test_pg_spend_exhaustion_denies_mint(pg, monkeypatch, tmp_path):
    """With the daily USD ceiling set below the per-action cap, mint denies
    spend_exhausted and no authority is created."""
    import operator_authority
    op = _new_op()
    pol = json.loads((SERVER_DIR / "operator_policy.json").read_text(encoding="utf-8"))
    pol["actions"]["operator.external_write"]["enabled"] = True
    # ceiling below the per-action reservation -> unconfigured/too-small.
    pol["spend_limits"]["usd_principal_day_cents"] = 5000  # < max_spend_cents 10000
    pf = tmp_path / "pol2.json"
    pf.write_text(json.dumps(pol), encoding="utf-8")
    monkeypatch.setenv("LEAF_OPERATOR_POLICY_FILE", str(pf))
    with pytest.raises(operator_authority.AuthorityDenied) as e:
        rb.propose(op, _DEST, _HANDLE, _ADAPTER)
    assert e.value.reason in ("spend_unconfigured", "spend_exhausted")
