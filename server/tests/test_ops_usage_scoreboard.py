"""Usage-scoreboard join behind the operator-gated tenant listing.

The drawer's scoreboard shows AutoCAD-backend usage and LLM usage at two
scopes. Those live in two different authorities, and only one of them is
reachable with the browser's bearer, so the JOIN happens here, server-side.
These cases pin the three things that make the reading trustworthy: the
per-tenant columns, an unknown that stays unknown, and a platform total that is
not silently clipped to the rows the broker happens to know about.
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import operator_deps  # noqa: E402
import operator_principals  # noqa: E402
from operator_principals import OperatorPrincipal  # noqa: E402
import deps  # noqa: E402
from routers import ops  # noqa: E402


def _operator() -> OperatorPrincipal:
    return OperatorPrincipal(
        subject="auth0|operator",
        role="operator",
        role_revision=1,
        status="active",
        profiles=("default",),
        environment="staging",
    )


def _listing(monkeypatch, *, brokers, agent):
    """Drive _tenant_listing through the real operator route.

    `agent` is what agent_ledger.tenants_seen() does: a dict, or an exception
    instance to raise.
    """
    monkeypatch.setattr(operator_deps.tenant_deps, "auth_live", lambda: False)
    monkeypatch.setattr(
        operator_principals, "resolve_principal",
        lambda subject: _operator() if subject == "auth0|operator" else None)
    monkeypatch.setattr(ops, "_disabled_set", lambda: set())
    monkeypatch.setattr(ops, "_broker_store_mode", lambda: "legacy")
    monkeypatch.setattr(ops, "_distinct_tenants", lambda _path: set(brokers))
    monkeypatch.setattr(ops, "_usage_mod", lambda: None)

    calls = {"n": 0}

    def _seen():
        calls["n"] += 1
        if isinstance(agent, BaseException):
            raise agent
        return agent

    monkeypatch.setattr(ops.agent_ledger, "tenants_seen", _seen)

    app = FastAPI()
    app.include_router(ops.operator_router)
    app.dependency_overrides[deps.require_tenant] = lambda: "demo-tenant"
    client = TestClient(app, raise_server_exceptions=True)
    res = client.get("/api/operator/tenants",
                     headers={"X-Operator-Subject": "auth0|operator"})
    assert res.status_code == 200
    return res.json(), calls


def test_rows_carry_llm_columns_and_a_platform_block(monkeypatch) -> None:
    body, _ = _listing(
        monkeypatch,
        brokers={"tenant-a", "tenant-b"},
        agent={
            "tenant-a": {"turns": 3, "cost_tokens": 1250, "usd_est": 0.03},
            "tenant-b": {"turns": 1, "cost_tokens": 400, "usd_est": 0.01},
        },
    )

    rows = {r["tenant_id"]: r for r in body["tenants"]}
    assert rows["tenant-a"]["llm_turns"] == 3
    assert rows["tenant-a"]["llm_cost_tokens"] == 1250
    assert rows["tenant-b"]["llm_usd_est"] == 0.01

    platform = body["platform"]
    assert platform["profiles"] == 2
    assert platform["llm"] == {"turns": 4, "cost_tokens": 1650, "usd_est": 0.04}


def test_a_tenant_the_broker_never_saw_still_counts_toward_the_platform(monkeypatch) -> None:
    """A profile can spend LLM turns without ever reaching the broker.

    Clipping the platform LLM total to the listed rows would under-report the
    platform by exactly the tenants an operator is least likely to notice.
    """
    body, _ = _listing(
        monkeypatch,
        brokers={"tenant-a"},
        agent={
            "tenant-a": {"turns": 3, "cost_tokens": 1250, "usd_est": 0.03},
            "llm-only": {"turns": 9, "cost_tokens": 7750, "usd_est": 0.87},
        },
    )

    assert [r["tenant_id"] for r in body["tenants"]] == ["tenant-a"]
    assert body["platform"]["profiles"] == 2
    assert body["platform"]["llm"]["cost_tokens"] == 9000


def test_an_unreadable_agent_store_reports_unknown_not_zero(monkeypatch) -> None:
    """The reading that would invert an operator's judgement.

    A confident $0.000 of LLM spend over a store that could not be read says
    "this profile is idle" about a profile that may be the most expensive one.
    """
    body, _ = _listing(
        monkeypatch,
        brokers={"tenant-a"},
        agent=RuntimeError("agent store unreachable"),
    )

    row = body["tenants"][0]
    assert row["llm_turns"] is None
    assert row["llm_cost_tokens"] is None
    assert row["llm_usd_est"] is None
    # The AutoCAD half comes from a different authority and is still known.
    assert row["runs"] == 0
    assert body["platform"]["llm"] == {"turns": None, "cost_tokens": None, "usd_est": None}
    assert body["platform"]["autocad_backend"]["runs"] == 0


def test_the_agent_ledger_is_read_once_per_listing_not_once_per_tenant(monkeypatch) -> None:
    """Performance contract, not a style preference.

    tenants_seen() makes a full pass over the ledger. Called inside the row
    loop it would be an N+1 over the whole file, so an operator opening the
    drawer on a fleet of 200 profiles would pay 200 full ledger reads.
    """
    _, calls = _listing(
        monkeypatch,
        brokers={f"tenant-{i}" for i in range(25)},
        agent={f"tenant-{i}": {"turns": 1, "cost_tokens": 10, "usd_est": 0.001}
               for i in range(25)},
    )

    assert calls["n"] == 1
