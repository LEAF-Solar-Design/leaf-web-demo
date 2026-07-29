"""GET /api/capabilities — capability catalog (CONTRACT-ADDENDUM section 9).

Internal/QA tools are filtered SERVER-SIDE by default. X-Internal-Role: qa
requests the projection, but the existing X-Ops-Secret credential authorizes it.
"""
from __future__ import annotations

import sys
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse

import catalog
import converse_registry
import customization_service
import deps
from envelopes import ErrorCode, error_obj, with_envelope_fields
from routers import ops as ops_router

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


@router.get("/api/capabilities")
def capabilities(x_internal_role: Optional[str] = Header(default=None),
                 x_ops_secret: Optional[str] = Header(default=None),
                 tenant=Depends(deps.require_tenant)) -> Any:
    """Capability catalog, TENANT-SCOPED for the folded portion (wave 4): globals for
    everyone, only the requesting tenant's OWN repo tools folded in."""
    include_internal = (x_internal_role or "").strip().lower() == "qa"
    if include_internal:
        denial = ops_router._require_ops(x_ops_secret)
        if denial is not None:
            return denial
    try:
        raw_tools = deps.all_tools(str(tenant))
        pin = (
            customization_service.effective_catalog_pin(str(tenant))
            or deps.base_catalog_pin(raw_tools)
        )
    except customization_service.CustomizationServiceError as exc:
        return _catalog_error(exc)
    tools = []
    for tool in raw_tools:
        view = deps.catalog_tool_view(tool)
        view.update(pin)
        tools.append(view)
    families = catalog.build_catalog(
        tools,
        include_internal=include_internal,
    )
    return with_envelope_fields({"families": families})


@router.get("/api/converse/registry")
def converse_registry_route(tenant=Depends(deps.require_tenant)) -> Any:
    """The composer's `/` picker: commands + skills + tools in one catalog.

    Tenant-scoped through the SAME `deps.all_tools(tenant)` the capability
    catalog uses, so the picker can never offer a tool the tenant does not
    have. A catalog outage degrades to commands only rather than failing the
    whole menu — the composer stays usable, and `/stop` in particular must keep
    working when the catalog is down.
    """
    try:
        # SAME internal/QA filtering /api/capabilities applies. deps.all_tools
        # is unfiltered; without this the picker would offer internal and QA
        # tools to ordinary tenants, which the capability catalog has always
        # withheld server-side.
        tools = catalog.filter_internal(deps.all_tools(str(tenant)))
    except customization_service.CustomizationServiceError:
        tools = []
    except Exception as exc:  # noqa: BLE001 — the picker is not worth a 500
        # Degrade to commands-only, but never SILENTLY: an unexpected failure
        # here is a defect, and a quiet empty catalog looks identical to a
        # tenant who simply has no tools.
        print(f"[leaf-registry] tool catalog unavailable: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        tools = []
    # Skills stay empty until the curated per-tier bundle ships (build plan
    # Wave 0); the shape is already correct so the client needs no change then.
    registry = converse_registry.build_registry(tools=tools, skills=())
    return with_envelope_fields(registry)
