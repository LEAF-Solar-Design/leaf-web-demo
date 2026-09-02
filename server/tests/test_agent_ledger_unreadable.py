"""An UNREADABLE agent ledger is unknown usage, never zero usage.

`agent_ledger` is read by three surfaces: the operator scoreboard join (hardened
in #865), the ops per-tenant rollup, and the tenant's own `/api/usage` agent
block. All three used to render a ledger they could not open as a confident
"$0.000 spent" -- the one reading that makes the most expensive profile look
idle, at exactly the moment nobody can see what it spent.

The split these cases pin, in both directions:

  * ABSENT ledger  -> a REAL zero. No turn was ever recorded, so 0 is the truth
    and must survive; turning a first-run deployment into a wall of em dashes
    would be its own dishonesty.
  * PRESENT but unreadable -> UNKNOWN. Nulls to the wire, `degraded_mode` on the
    envelope, and `estimate_basis: "unavailable"` on the tenant block.

Run:  cd server && python -m pytest tests/test_agent_ledger_unreadable.py -q
"""
from __future__ import annotations

# Cache the STDLIB `platform` module BEFORE PROJECT_ROOT lands on sys.path
# (the repo-root platform/ package shadows it; mirrors test_agent_router.py).
import platform as _stdlib_platform  # noqa: E402
_stdlib_platform.python_implementation()

import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import agent_ledger  # noqa: E402

TURN_LINE = ('{"kind":"turn","ts":"2026-09-01T00:00:00.000Z","tenant_id":'
             '"tenant-a","cost_tokens":1250,"usd_est":0.03}\n')


@pytest.fixture(autouse=True)
def agent_env(tmp_path, monkeypatch):
    """Hermetic: legacy store, auth off, every path under tmp_path."""
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.delenv("LEAF_OPS_SECRET", raising=False)
    monkeypatch.setenv("LEAF_AGENT_STORE", "legacy")
    monkeypatch.setenv("LEAF_BROKER_STORE", "legacy")
    monkeypatch.setenv("LEAF_AGENT_TENANTS_FILE", str(tmp_path / "agent_tenants.json"))
    monkeypatch.setenv("LEAF_USAGE_LEDGER", str(tmp_path / "broker_ledger.jsonl"))
    yield tmp_path


@pytest.fixture
def client():
    """Local mount -- server/app.py is another lane's file and stays untouched."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import ops as ops_router
    from routers import usage as usage_router

    app = FastAPI()
    app.include_router(usage_router.router)
    app.include_router(ops_router.router)
    return TestClient(app, raise_server_exceptions=False)


def _ledger(tmp_path, monkeypatch, *, exists: bool) -> Path:
    target = tmp_path / "agent_ledger.jsonl"
    if exists:
        target.write_text(TURN_LINE, encoding="utf-8")
    monkeypatch.setenv("LEAF_AGENT_LEDGER", str(target))
    return target


def _make_unreadable(monkeypatch, target: Path) -> None:
    """Fail the read of exactly this file.

    Monkeypatching `Path.read_text` rather than chmod'ing: the permission bit
    that denies a read is not portable to Windows, and the defect is the OSError
    itself, not any one way of provoking it.
    """
    original_read_text = Path.read_text

    def _unreadable(path, *args, **kwargs):
        if path == target:
            raise OSError("ledger cannot be read")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _unreadable)


# --------------------------------------------------------------------------- #
# module contract -- aggregate() gains the strict read tenants_seen() already had
# --------------------------------------------------------------------------- #
def test_aggregate_strict_read_raises_instead_of_reporting_zeros(
    tmp_path, monkeypatch,
) -> None:
    target = _ledger(tmp_path, monkeypatch, exists=True)
    _make_unreadable(monkeypatch, target)

    # Lenient (the default) still never raises: metering must not fail a turn.
    lenient = agent_ledger.aggregate("tenant-a")
    assert lenient["today"]["turns"] == 0
    assert lenient["cycle"]["cost_tokens"] == 0

    with pytest.raises(OSError, match="ledger cannot be read"):
        agent_ledger.aggregate("tenant-a", raise_on_read_error=True)


def test_a_missing_ledger_is_a_real_zero_even_under_a_strict_read(
    tmp_path, monkeypatch,
) -> None:
    """The other half of the contract, and the reason this is not just `raise`.

    No file means no turn was ever recorded. That IS zero, and a strict caller
    must still get the zero rather than an unknown -- otherwise every fresh
    deployment reports its usage as unmeasurable.
    """
    _ledger(tmp_path, monkeypatch, exists=False)

    assert agent_ledger.aggregate(
        "tenant-a", raise_on_read_error=True)["today"]["turns"] == 0
    assert agent_ledger.tenants_seen(raise_on_read_error=True) == {}


# --------------------------------------------------------------------------- #
# GET /api/usage -- the tenant's own agent block
# --------------------------------------------------------------------------- #
def test_usage_agent_block_is_unknown_not_zero_when_the_ledger_is_unreadable(
    client, tmp_path, monkeypatch,
) -> None:
    target = _ledger(tmp_path, monkeypatch, exists=True)
    _make_unreadable(monkeypatch, target)

    res = client.get("/api/usage", headers={"X-Tenant-Id": "tenant-a"})
    assert res.status_code == 200, res.text
    body = res.json()

    agent = body["agent"]
    assert agent["today"] == {"turns": None, "cost_tokens": None, "usd_est": None}
    assert agent["cycle"] == {"turns": None, "cost_tokens": None, "usd_est": None}
    # The basis field carries the reason -- an additive VALUE on the field the
    # design doc reserves for it (§6.7), not a schema change.
    assert agent["estimate_basis"] == "unavailable"
    assert body["degraded_mode"] is True

    # The broker half comes from a different authority and stays KNOWN: a
    # degraded agent read must not blank out numbers it did not touch.
    assert body["today"] == {"runs": 0, "usd_est": 0.0}
    assert body["cap"]["enabled"] is False


def test_usage_agent_block_stays_zero_on_a_missing_ledger(
    client, tmp_path, monkeypatch,
) -> None:
    _ledger(tmp_path, monkeypatch, exists=False)

    body = client.get("/api/usage", headers={"X-Tenant-Id": "tenant-a"}).json()
    assert body["agent"]["today"] == {"turns": 0, "cost_tokens": 0, "usd_est": 0.0}
    assert body["agent"]["estimate_basis"] == "self_metered"
    assert body["degraded_mode"] is False


def test_usage_agent_block_reports_real_numbers_when_the_ledger_reads(
    client, tmp_path, monkeypatch,
) -> None:
    """Guards the obvious over-correction: never-unknown must stay never-unknown."""
    _ledger(tmp_path, monkeypatch, exists=True)

    agent = client.get("/api/usage", headers={"X-Tenant-Id": "tenant-a"}).json()["agent"]
    assert agent["cycle"]["turns"] == 1
    assert agent["cycle"]["cost_tokens"] == 1250
    assert agent["estimate_basis"] == "self_metered"


# --------------------------------------------------------------------------- #
# GET /api/ops/agent/tenants -- the ops rollup
# --------------------------------------------------------------------------- #
def test_ops_agent_tenants_reports_unknown_usage_when_the_ledger_is_unreadable(
    client, tmp_path, monkeypatch,
) -> None:
    """The rows an operator disables a tenant from.

    This route's numbers ARE the agent ledger, so an unreadable one costs the
    row SET as well: only tenants the state store already named can appear, and
    their usage is unknown. degraded_mode says the listing is partial.
    """
    target = _ledger(tmp_path, monkeypatch, exists=True)
    monkeypatch.setattr(ops_module(), "_agent_store_mode", lambda: "postgres")
    monkeypatch.setattr(
        ops_module(), "_agent_pg_store",
        lambda: _FakeStore({"tenant-a": {"agent_disabled": True, "revision": 4}}))
    _make_unreadable(monkeypatch, target)

    res = client.get("/api/ops/agent/tenants")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["degraded_mode"] is True

    row = next(r for r in body["tenants"] if r["tenant_id"] == "tenant-a")
    assert row["turns"] is None
    assert row["cost_tokens"] is None
    assert row["usd_est"] is None
    # State is a different authority and is still known.
    assert row["agent_disabled"] is True
    assert row["revision"] == 4


def test_ops_agent_tenants_is_a_truthful_empty_listing_on_a_missing_ledger(
    client, tmp_path, monkeypatch,
) -> None:
    _ledger(tmp_path, monkeypatch, exists=False)

    body = client.get("/api/ops/agent/tenants").json()
    assert body["tenants"] == []
    assert body["degraded_mode"] is False


def test_ops_agent_tenants_reports_real_numbers_when_the_ledger_reads(
    client, tmp_path, monkeypatch,
) -> None:
    _ledger(tmp_path, monkeypatch, exists=True)

    body = client.get("/api/ops/agent/tenants").json()
    row = next(r for r in body["tenants"] if r["tenant_id"] == "tenant-a")
    assert row["turns"] == 1
    assert row["cost_tokens"] == 1250
    assert row["usd_est"] == 0.03
    assert body["degraded_mode"] is False


def ops_module():
    from routers import ops
    return ops


class _FakeStore:
    """Only the one method this route calls on the agent postgres store."""

    def __init__(self, states):
        self._states = states

    def tenant_states(self):
        return dict(self._states)
