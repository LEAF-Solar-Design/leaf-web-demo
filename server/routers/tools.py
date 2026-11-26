"""GET /api/tools — flat back-compat tool list.

Owned downstream by the `dynamic-tool-loader` session.

NOTE: POST /api/run moved to routers/jobs.py (async job spine, ADDENDUM
section 7) — it now returns 202 {job_id}; `?wait=1` keeps the old sync shape.
"""
from __future__ import annotations

from typing import Any, Dict
import os
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

import customization_service
import customization_flags
import deps
import entitlements
import investor_demo
import tool_envelopes
from customization_flags import enabled as customization_enabled
from envelopes import ErrorCode, error_obj, with_envelope_fields
from branch_tools import dev_catalog, sync_receipts, tenant_catalog, tenant_publish, online_signer
from text_match import text_matches

router = APIRouter()

_PUBLISHED_CACHE = {"Cache-Control": "private, max-age=300"}


@router.get("/api/tools/{name}/view-source")
def authored_view_source(name: str, project_id: str, session_id: str, catalog_commit: str,
                         effective_catalog_digest: str, tool_manifest_sha256: str,
                         catalog_digest: str, tenant=Depends(deps.require_tenant)) -> Response:
    from authored_view_source import AuthoredViewError, resolve_authored_view
    headers = {"Cache-Control": "no-store"}
    try:
        return JSONResponse(resolve_authored_view(
            tenant, project_id=project_id, session_id=session_id, name=name,
            catalog_commit=catalog_commit, effective_catalog_digest=effective_catalog_digest,
            tool_manifest_sha256=tool_manifest_sha256, catalog_digest=catalog_digest), headers=headers)
    except AuthoredViewError as exc:
        return JSONResponse({"detail": "Authored view unavailable", "reason_code": exc.code},
                            status_code=exc.status, headers=headers)


@router.get("/api/tool-envelopes")
def published_tools(tenant=Depends(deps.require_active_tenant)) -> Response:
    return JSONResponse({"tools": tool_envelopes.published_catalog().list(str(tenant))}, headers=_PUBLISHED_CACHE)


@router.get("/api/tool-envelopes/revocations")
def published_revocations(tenant=Depends(deps.require_active_tenant)) -> Response:
    raw = tool_envelopes.published_catalog().revocations_bytes()
    if raw is None:
        return JSONResponse({"detail": "no_revocation_list"}, status_code=404, headers=_PUBLISHED_CACHE)
    return Response(raw, media_type="application/json", headers=_PUBLISHED_CACHE)


_DEV_CACHE = {"Cache-Control": "no-store"}


def _dev_visible(tenant) -> bool:
    return (os.environ.get("LEAF_BRANCH_DEV_TOOLS") == "1"
            and str(tenant) in customization_flags.internal_tenants())


def _dev_missing() -> Response:
    return JSONResponse({"detail": "tool_not_published"}, status_code=404, headers=_DEV_CACHE)


@router.get("/api/tool-envelopes/dev")
def dev_tools(tenant=Depends(deps.require_active_tenant)) -> Response:
    if not _dev_visible(tenant):
        return _dev_missing()
    entries = dev_catalog.read_catalog()
    if entries is None:
        return _dev_missing()
    return JSONResponse({"tools": [entry.metadata for entry in entries]}, headers=_DEV_CACHE)


@router.get("/api/tool-envelopes/dev/{name}/{version}/package")
def dev_package(name: str, version: str, tenant=Depends(deps.require_active_tenant)) -> Response:
    if not _dev_visible(tenant) or not tool_envelopes.valid_identity(name, version):
        return _dev_missing()
    for entry in dev_catalog.read_catalog() or []:
        if entry.metadata["name"] == name and entry.metadata["version"] == version:
            return FileResponse(entry.package, media_type="application/zip", headers=_DEV_CACHE)
    return _dev_missing()


def _dev_receipt_tenant(name: str, version: str, tenant=Depends(deps.require_active_tenant)):
    if not _dev_visible(tenant) or not tool_envelopes.valid_identity(name, version):
        # Match the dev transport's plain 404 before validating receipt fields.
        raise StarletteHTTPException(404, detail="tool_not_published", headers=_DEV_CACHE)
    return tenant


@router.post("/api/tool-envelopes/dev/{name}/{version}/receipts", status_code=204)
def dev_receipt(name: str, version: str, receipt: sync_receipts.Receipt = Body(...),
                tenant=Depends(_dev_receipt_tenant)) -> Response:
    digest = receipt["package_digest"].lower()
    entries = (entry.metadata for entry in dev_catalog.read_catalog() or [])
    if not any(entry["name"] == name and entry["version"] == version and entry["package_digest"] == digest
               for entry in entries):
        return _dev_missing()
    subject = getattr(tenant, "subject", None) or f"tenant:{tenant}"
    if not sync_receipts.limiter.admit(subject):
        return JSONResponse({"detail": "rate_limited"}, status_code=429, headers=_DEV_CACHE)
    store = customization_service.CustomizationService.configured().store
    sync_receipts.append(store, str(tenant), subject, name, version, receipt)
    return Response(status_code=204, headers=_DEV_CACHE)


_TENANT_CACHE = {"Cache-Control": "no-store"}


def _plugin_builder_visible(tenant) -> bool:
    return customization_flags.plugin_builder_enabled(str(tenant))


def _tenant_missing(detail: str = "tool_not_published") -> Response:
    return JSONResponse({"detail": detail}, status_code=404, headers=_TENANT_CACHE)


@router.get("/api/tool-envelopes/tenant")
def tenant_tools(tenant=Depends(deps.require_active_tenant)) -> Response:
    if not _plugin_builder_visible(tenant):
        return _tenant_missing()
    entries = tenant_catalog.list_versions(os.environ["LEAF_TENANT_TOOLS_DIR"], str(tenant))
    return JSONResponse({"tools": entries}, headers=_TENANT_CACHE)


@router.get("/api/tool-envelopes/tenant/revocations")
def tenant_revocations(tenant=Depends(deps.require_active_tenant)) -> Response:
    if not _plugin_builder_visible(tenant):
        return _tenant_missing()
    raw = tenant_catalog.revocations_bytes(os.environ["LEAF_TENANT_TOOLS_DIR"], str(tenant))
    if raw is None:
        return _tenant_missing("no_revocation_list")
    now = datetime.now(timezone.utc)
    try:
        issued_at = datetime.fromisoformat(json.loads(raw)["issued_at"].replace("Z", "+00:00"))
        if now - issued_at > timedelta(days=3):
            raw = tenant_publish.refresh_revocations(
                os.environ["LEAF_TENANT_TOOLS_DIR"], str(tenant), online_signer.configured_signer(), now)
    except (online_signer.SignerUnavailable, tenant_publish.TenantPublishError,
            tenant_catalog.TenantCatalogError, OSError, ValueError, KeyError, TypeError, AttributeError):
        pass  # Serve the last signed list if refresh is unavailable.
    return Response(raw, media_type="application/json", headers=_TENANT_CACHE)


@router.get("/api/tool-envelopes/tenant/{name:path}/{version}/envelope")
def tenant_envelope(name: str, version: str, tenant=Depends(deps.require_active_tenant)) -> Response:
    if not _plugin_builder_visible(tenant) or not tool_envelopes.valid_identity(name, version):
        return _tenant_missing()
    raw = tenant_catalog.envelope_bytes(os.environ["LEAF_TENANT_TOOLS_DIR"], str(tenant), name, version)
    if raw is None:
        return _tenant_missing()
    return Response(raw, media_type="application/json", headers=_TENANT_CACHE)


@router.get("/api/tool-envelopes/tenant/{name:path}/{version}/package")
def tenant_package(name: str, version: str, tenant=Depends(deps.require_active_tenant)) -> Response:
    if not _plugin_builder_visible(tenant) or not tool_envelopes.valid_identity(name, version):
        return _tenant_missing()
    path = tenant_catalog.package_path(os.environ["LEAF_TENANT_TOOLS_DIR"], str(tenant), name, version)
    if path is None:
        return _tenant_missing()
    return FileResponse(path, media_type="application/zip", headers=_TENANT_CACHE)


@router.get("/api/tool-envelopes/{name:path}/{version}/envelope")
def published_envelope(name: str, version: str, tenant=Depends(deps.require_active_tenant)) -> Response:
    if not tool_envelopes.valid_identity(name, version):
        return JSONResponse({"detail": "invalid_tool_identity"}, status_code=400, headers=_PUBLISHED_CACHE)
    raw = tool_envelopes.published_catalog().envelope_bytes(name, version, str(tenant))
    if raw is None:
        return JSONResponse({"detail": "tool_not_published"}, status_code=404, headers=_PUBLISHED_CACHE)
    return Response(raw, media_type="application/json", headers=_PUBLISHED_CACHE)


@router.get("/api/tool-envelopes/{name:path}/{version}/package")
def published_package(name: str, version: str, tenant=Depends(deps.require_active_tenant)) -> Response:
    if not tool_envelopes.valid_identity(name, version):
        return JSONResponse({"detail": "invalid_tool_identity"}, status_code=400, headers=_PUBLISHED_CACHE)
    path = tool_envelopes.published_catalog().package_path(name, version, str(tenant))
    if path is None:
        return JSONResponse({"detail": "tool_not_published"}, status_code=404, headers=_PUBLISHED_CACHE)
    return FileResponse(path, media_type="application/zip", headers=_PUBLISHED_CACHE)


def _catalog_error(exc: customization_service.CustomizationServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=with_envelope_fields({
        "error": error_obj(
            ErrorCode.INTERNAL,
            "The catalog authority is temporarily unavailable.",
            retryable=exc.status_code >= 500,
        ),
        "reason_code": exc.code,
    }))


def _catalog_tools(tenant, investor: bool):
    return deps.shared_tools() if investor else deps.all_tools(str(tenant))


@router.get("/api/tools/{name}/executable-identity")
def executable_identity(name: str, catalog_digest: str,
                        tenant=Depends(deps.require_tenant)) -> Response:
    """Resolve one catalog-confirmed read tool without running it."""
    from tool_loader import published_tool_source_sha256, resolve_instant_batch_artifact

    headers = {"Cache-Control": "no-store"}
    investor = investor_demo.is_investor_context(tenant)
    try:
        selected = next((tool for tool in _catalog_tools(tenant, investor)
                         if tool.get("name") == name), None)
    except customization_service.CustomizationServiceError as exc:
        return _catalog_error(exc)
    if selected is None:
        return JSONResponse({"detail": "Tool unavailable"}, status_code=404, headers=headers)
    if not text_matches(catalog_digest, deps.catalog_tool_digest(selected)):
        return JSONResponse({"detail": "Tool changed. Refresh the catalog."},
                            status_code=409, headers=headers)

    identity = None
    if selected.get("capabilities") == ["drawing.read"]:
        if selected.get("execution_class") == "instant":
            try:
                artifact = resolve_instant_batch_artifact(selected)
            except (ValueError, OSError):
                pass
            else:
                identity = {"kind": "instant-artifact", "digest": artifact.code_digest}
        elif not investor:
            source_digest = published_tool_source_sha256(selected, str(tenant))
            if source_digest:
                identity = {"kind": "published-source", "digest": source_digest}
    return JSONResponse({"executable_identity": identity}, headers=headers)


@router.get("/api/tools")
def tools(tenant=Depends(deps.require_tenant)) -> Any:
    """Flat tool list, TENANT-SCOPED for the folded portion (wave 4): the engine
    registry + write seed + authored globals are visible to everyone; only the
    requesting tenant's OWN repo tools are folded in (tenant A's authored tools are
    invisible to tenant B). Auth OFF -> tenant is the X-Tenant-Id stub (default
    demo-tenant); with no tenant repo configured this is byte-identical to before."""
    try:
        catalog_tools = _catalog_tools(tenant, investor_demo.is_investor_context(tenant))
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
