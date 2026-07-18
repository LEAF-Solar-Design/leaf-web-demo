"""GET /api/tools — flat back-compat tool list.

Owned downstream by the `dynamic-tool-loader` session.

NOTE: POST /api/run moved to routers/jobs.py (async job spine, ADDENDUM
section 7) — it now returns 202 {job_id}; `?wait=1` keeps the old sync shape.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

import deps
import entitlements
from envelopes import with_envelope_fields

router = APIRouter()


@router.get("/api/tools")
def tools(tenant=Depends(deps.require_tenant)) -> Dict[str, Any]:
    """Flat tool list, TENANT-SCOPED for the folded portion (wave 4): the engine
    registry + write seed + authored globals are visible to everyone; only the
    requesting tenant's OWN repo tools are folded in (tenant A's authored tools are
    invisible to tenant B). Auth OFF -> tenant is the X-Tenant-Id stub (default
    demo-tenant); with no tenant repo configured this is byte-identical to before."""
    return with_envelope_fields({"tools": deps.all_tools(str(tenant))})


@router.get("/api/entitlements")
def get_entitlements(tenant=Depends(deps.require_tenant)) -> Dict[str, Any]:
    """The requesting tenant's tier-driven capability policy (§17):
    §10-enveloped ``{tier, entitlements: {run_read, run_write, build}, source: "policy"}``.

    Tier comes from ``require_tenant``: live auth -> the verified Auth0 claim; off-auth ->
    "demo" (full access). This is a READ of policy — the actual enforcement lives in the
    /api/run and /api/author execution chains and cannot be bypassed via this endpoint."""
    tier = entitlements.resolve_tier(tenant)
    return deps.tenant_echo(with_envelope_fields(entitlements.entitlements_view(tier)), tenant)
