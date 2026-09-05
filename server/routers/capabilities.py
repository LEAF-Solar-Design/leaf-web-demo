"""GET /api/capabilities — capability catalog (CONTRACT-ADDENDUM section 9).

Internal/QA tools are filtered SERVER-SIDE by default. X-Internal-Role: qa
requests the projection, but the existing X-Ops-Secret credential authorizes it.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse

import catalog
import converse_registry
import customization_service
import deps
import mcp_tool_projection
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
        provenance_resolver = getattr(deps, "effective_tools_with_provenance", None)
        operator_owned_engine_source = getattr(
            deps, "TOOL_SOURCE_OPERATOR_OWNED_ENGINE", None
        )
        effective_rows = None
        if (
            callable(provenance_resolver)
            and operator_owned_engine_source == "operator_owned_engine"
        ):
            try:
                effective_rows = provenance_resolver(str(tenant))
            except (deps.ToolCatalogProvenanceError,
                    deps.ToolCatalogCollisionError) as exc:
                # Provenance could not be established, so live APS FAILS CLOSED
                # (None sources below) while the catalog itself stays available
                # through the same forgiving read this route has always served.
                print(
                    f"[leaf-catalog] tool provenance unavailable "
                    f"({type(exc).__name__}: {exc}); live APS fails closed to "
                    f"batch for this response",
                    file=sys.stderr, flush=True,
                )
        if effective_rows is not None:
            raw_tools = [tool for tool, _source in effective_rows]
            tool_sources = [source for _tool, source in effective_rows]
        else:
            # Compatibility with a base that predates provenance, and the
            # fail-closed degrade above, both read the ordinary catalog once;
            # None sources disable live APS.
            raw_tools = deps.all_tools(str(tenant))
            tool_sources = [None] * len(raw_tools)
        # Standardization slice 8c: tools projected from this tenant's OWN
        # connected MCP servers (server/mcp_tool_projection.py), behind the
        # same tenant binding every other folded row above uses. Always empty
        # today (projecting an upstream tool list is a later slice), so this
        # fans in nothing and changes no response byte for any tenant.
        mcp_tools = mcp_tool_projection.projected_tools(str(tenant))
        raw_tools = raw_tools + mcp_tools
        tool_sources = tool_sources + [None] * len(mcp_tools)
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
    trusted_live_catalog_digests = {
        deps.catalog_tool_digest(tool)
        for tool in deps.load_engine_registry_tools()
        if tool.get("aps_live") is True
    }
    tools = catalog.apply_live_aps_runtime_authority(
        tools,
        aps_live_enabled=deps.APS_LIVE,
        trusted_live_catalog_digests=trusted_live_catalog_digests,
        tool_sources=tool_sources,
        operator_owned_engine_source=operator_owned_engine_source,
    )
    families = catalog.build_catalog(
        tools,
        include_internal=include_internal,
    )
    return with_envelope_fields({
        "families": families,
        "cad_engine": cad_engine_selector(),
    })


# ---------------------------------------------------------------------------
# Card F-4: the TRUTHFUL CAD-engine selector. The program shipped for months
# with the terminal receipt "no CAD engine is enabled and the contract
# exposes no selector" — this block is that receipt's supersession point.
# Truthful BOTH ways: flag off => enabled:false and NO engine named; flag on
# => the engine, its exact rev pin, and its license posture, plus the NOTICE
# line the license review's binding condition 1 requires on the product's
# attributions surface (the web renders this text from here AT RUNTIME —
# the client tree may not name the engine, per the license fence).
# ---------------------------------------------------------------------------

FLAG_CAD_EDIT = "LEAF_CAD_EDIT_ENABLED"

# The exact consumed revision. MUST match vendor/acadrust-worker/Cargo.toml's
# rev pin; test_engine_selector.py locks the two together so a re-pin cannot
# leave this selector lying.
CAD_ENGINE_REVISION = "18500466e7e4392ef830fdc59cede75fa3794f2b"

CAD_ENGINE_NOTICE = (
    "This product includes acadrust (https://github.com/hakanaktt/acadrust), "
    "licensed under the Mozilla Public License 2.0. Source for the exact "
    "revision used is available at the upstream repository."
)


def cad_edit_enabled() -> bool:
    """Server-side cad_edit config rail, same truthy set as cad_upload's."""
    return os.environ.get(FLAG_CAD_EDIT, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def cad_engine_selector() -> Dict[str, Any]:
    if not cad_edit_enabled():
        return {"enabled": False, "engine": None}
    return {
        "enabled": True,
        "engine": "acadrust",
        "revision": CAD_ENGINE_REVISION,
        "license": "MPL-2.0",
        "isolation": "wasm worker behind the license-fenced boundary",
        "notice": CAD_ENGINE_NOTICE,
        # W4g-1: the route the browser engine opens the console's own head
        # from. Named here so a client can say "engine reach is off on this
        # deployment" honestly instead of probing for a 404.
        "reach": {"route": "/api/drawings/{drawing_id}/dxf", "version_query": "version"},
    }


@router.get("/api/surface-config")
def surface_config(tenant=Depends(deps.require_tenant)) -> Any:
    """GET /api/surface-config — the tenant's surface-config overlay
    (standardization slice 7b), through the SAME per-tenant fold
    `/api/capabilities` uses.

    Returns `{surfaces: <overlay>}` where `<overlay>` is EXACTLY what
    `deps.effective_surface_config` returns: the validated overlay only,
    never a server-side copy of the productSurfaces.js defaults (the web
    owns that merge). `{surfaces: {}}` covers both "no file" and "file
    failed to validate" — the fold fails closed either way, never a 500.
    `source` is present only when the tenant's repo resolves and its file
    exists, so the web's provenance chip has no digest/timestamp to show
    for a tenant with no overlay at all.
    """
    overlay = deps.effective_surface_config(str(tenant))
    payload: Dict[str, Any] = {"surfaces": overlay}
    source = deps.surface_config_source(str(tenant))
    if source is not None:
        payload["source"] = source
    return with_envelope_fields(payload)


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
