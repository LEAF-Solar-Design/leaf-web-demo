"""GET /api/usage — per-tenant spend / quota meter (UI wave 1).

Turns the broker's append-only attribution ledger (`server/broker_ledger.jsonl`,
ADDENDUM §8) into a calm, proactive spend surface for the frontend. §10-enveloped:

    { tenant_id,
      today: { runs, usd_est },        # UTC-date bucket
      total: { runs, usd_est },        # lifetime
      cap:   { usd_cap, remaining, enabled },
      updated_at,
      error, degraded_mode }

Source of truth is `da/usage.py`:
  * `aggregate_usage(tenant, ledger)` — runs + spend, today vs total (denied
    pre-flight lines never count; missing/empty ledger -> zeros, never an error);
  * `cap_for(tenant)` — the configured USD cap (None => caps OFF => enabled:false
    with nulls; the demo default).

Reading the ledger file app-side is deliberate and safe: the ledger holds NO
credential — it IS the metering surface (CONTRACT-ADDENDUM §13). The ledger path
is resolved at request time: `LEAF_USAGE_LEDGER` > `BROKER_LEDGER` (share the
broker's file) > default `server/broker_ledger.jsonl`.

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


@router.get("/api/usage")
def usage(tenant=Depends(deps.require_tenant)) -> Dict[str, Any]:
    tenant_id = str(tenant)
    um = _usage_mod()
    if um is None:  # da/usage.py somehow unavailable -> honest zeros, never a 500
        agg = {"today": {"runs": 0, "usd_est": 0.0}, "total": {"runs": 0, "usd_est": 0.0}}
        cap: Optional[float] = None
    else:
        agg = um.aggregate_usage(tenant_id, _ledger_path())
        cap = um.cap_for(tenant_id)

    enabled = cap is not None
    remaining = round(max(0.0, float(cap) - agg["total"]["usd_est"]), 6) if enabled else None

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
        "agent": agent_ledger.aggregate(tenant_id),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return with_envelope_fields(deps.tenant_echo(body, tenant))
