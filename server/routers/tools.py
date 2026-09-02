"""GET /api/tools — flat back-compat tool list.

Owned downstream by the `dynamic-tool-loader` session.

NOTE: POST /api/run moved to routers/jobs.py (async job spine, ADDENDUM
section 7) — it now returns 202 {job_id}; `?wait=1` keeps the old sync shape.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

import customization_service
import deps
import entitlements
from customization_flags import enabled as customization_enabled
from envelopes import ErrorCode, error_obj, with_envelope_fields

router = APIRouter()


def _catalog_error(exc: customization_service.CustomizationServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=with_envelope_fields({
        "error": error_obj(
            ErrorCode.INTERNAL,
            "The catalog authority is temporarily unavailable.",
            retryable=exc.status_code >= 500,
        ),
        "reason_code": exc.code,
    }))


@router.get("/api/tools")
def tools(tenant=Depends(deps.require_tenant)) -> Any:
    """Flat tool list, TENANT-SCOPED for the folded portion (wave 4): the engine
    registry + write seed + authored globals are visible to everyone; only the
    requesting tenant's OWN repo tools are folded in (tenant A's authored tools are
    invisible to tenant B). Auth OFF -> tenant is the X-Tenant-Id stub (default
    demo-tenant); with no tenant repo configured this is byte-identical to before."""
    try:
        catalog_tools = deps.all_tools(str(tenant))
    except customization_service.CustomizationServiceError as exc:
        return _catalog_error(exc)
    return with_envelope_fields({
        "tools": [deps.catalog_tool_view(tool) for tool in catalog_tools]
    })


@router.get("/api/entitlements")
def get_entitlements(tenant=Depends(deps.require_tenant)) -> Dict[str, Any]:
    """The requesting tenant's tier-driven capability policy (§17):
    §10-enveloped ``{tier, entitlements: {run_read, run_write, build}, source: "policy"}``.

    Tier comes from ``require_tenant``: live auth -> the verified Auth0 claim; off-auth ->
    "demo" (full access). This is a READ of policy — the actual enforcement lives in the
    /api/run and /api/author execution chains and cannot be bypassed via this endpoint."""
    tier = entitlements.resolve_tier(tenant)
    roles, elevated = entitlements.resolve_roles(tenant)
    try:
        view = entitlements.entitlements_view(tier, roles, elevated)
    except entitlements.EntitlementsError:
        return entitlements.policy_unavailable_response("run_read", tier)
    # Rollout AVAILABILITY, distinct from entitlement POLICY: a tier may hold
    # `build` while the R5 authoring stage is still off (or internal-only) in
    # this deployment. The UI must require BOTH before showing an enabled
    # Generate affordance — same per-tenant predicate the /api/author gate uses.
    view["availability"] = {
        "author_stage": bool(customization_enabled(5, str(tenant).strip())),
    }
    return deps.tenant_echo(with_envelope_fields(view), tenant)
