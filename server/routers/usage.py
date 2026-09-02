"""GET /api/usage — per-tenant spend / quota meter (UI wave 1).

Turns the broker's append-only attribution ledger (`server/broker_ledger.jsonl`,
ADDENDUM §8) into a calm, proactive spend surface for the frontend. §10-enveloped:

    { tenant_id,
      today: { runs, usd_est },        # UTC-date bucket
      total: { runs, usd_est },        # lifetime
      cap:   { usd_cap, remaining, enabled },
      updated_at,
      error, degraded_mode }

The selected broker authority is the source of truth. Legacy mode aggregates
the JSONL ledger through `da/usage.py`. PostgreSQL mode reads the immutable
broker ledger table and never falls back to the stale file. `da/usage.py`
continues to resolve the configured cap in both modes. The legacy ledger path
resolves at request time: `LEAF_USAGE_LEDGER` > `BROKER_LEDGER` > default
`server/broker_ledger.jsonl`.

Tenant identity is the usual `X-Tenant-Id` stub (default `demo-tenant`), or the
verified JWT `TenantContext` when `LEAF_AUTH_LIVE=1` — `require_tenant` like the
other routers.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends

import agent_ledger
import deps
from envelopes import with_envelope_fields

router = APIRouter()


def _usage_mod():
    """da/usage.py loaded under a distinct name (pure module, no credential) —
    mirrors the broker's path-based load so it never depends on sys.path order."""
    return deps._load_module_from(deps.DA_DIR / "usage.py", "leaf_usage")


def _ledger_path() -> Path:
    """Resolve the broker attribution ledger this app-side read aggregates.
    Read at call time so subprocess env / test monkeypatch overrides apply."""
    override = os.environ.get("LEAF_USAGE_LEDGER") or os.environ.get("BROKER_LEDGER")
    return Path(override) if override else (deps.SERVER_DIR / "broker_ledger.jsonl")


def _postgres_store():
    from broker_pg_store import get_store

    return get_store()


def _broker_store_mode() -> str:
    mode = os.environ.get("LEAF_BROKER_STORE", "legacy").strip().lower()
    if mode not in {"legacy", "postgres"}:
        raise RuntimeError("LEAF_BROKER_STORE must be 'legacy' or 'postgres'")
    return mode


def _aggregate_usage(tenant_id: str, um: Any) -> Dict[str, Any]:
    """Select one authority. PostgreSQL mode never falls back to stale JSONL."""
    if _broker_store_mode() == "postgres":
        return _postgres_store().aggregate_usage(tenant_id)
    if um is None:
        return {
            "today": {"runs": 0, "usd_est": 0.0},
            "total": {"runs": 0, "usd_est": 0.0},
        }
    return um.aggregate_usage(tenant_id, _ledger_path())


# The agent block when the agent ledger is present but UNREADABLE. Every number
# is null rather than 0: a confident "$0.000 spent" over a failed read tells the
# tenant they are idle at exactly the moment their spend is unknown, and the
# frontend already renders null as an em dash (web/src/usage.js `finiteOrNull` /
# `orDash`). `estimate_basis` carries the reason on the field the design doc
# reserves for exactly this -- an additive VALUE, not a schema change
# (docs/AGENT-SPINE-DESIGN.md §6.7). A MISSING ledger stays a real zero.
_AGENT_UNKNOWN_BUCKET: Dict[str, Any] = {
    "turns": None, "cost_tokens": None, "usd_est": None}


def _agent_block(tenant_id: str) -> tuple[Dict[str, Any], bool]:
    """This tenant's agent-spine usage, and whether the reading is degraded."""
    try:
        return agent_ledger.aggregate(tenant_id, raise_on_read_error=True), False
    except OSError:
        return ({"today": dict(_AGENT_UNKNOWN_BUCKET),
                 "cycle": dict(_AGENT_UNKNOWN_BUCKET),
                 "estimate_basis": "unavailable"}, True)


@router.get("/api/usage")
def usage(tenant=Depends(deps.require_tenant)) -> Dict[str, Any]:
    tenant_id = str(tenant)
    um = _usage_mod()
    agg = _aggregate_usage(tenant_id, um)
    cap: Optional[float] = um.cap_for(tenant_id) if um is not None else None

    enabled = cap is not None
    remaining = round(max(0.0, float(cap) - agg["total"]["usd_est"]), 6) if enabled else None

    agent, agent_degraded = _agent_block(tenant_id)
    body = {
        "tenant_id": tenant_id,
        "today": agg["today"],
        "total": agg["total"],
        "cap": {"usd_cap": (float(cap) if enabled else None),
                "remaining": remaining, "enabled": enabled},
        # ADDITIVE agent block (agent spine §18): conversation-turn metering
        # from the agent's OWN ledger (data/agent_ledger.jsonl) — never the
        # broker attribution ledger, whose one-line-per-run invariant is
        # sacred. Self-metered estimates only (no balance API exists).
        "agent": agent,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return with_envelope_fields(deps.tenant_echo(body, tenant),
                                degraded_mode=agent_degraded)
