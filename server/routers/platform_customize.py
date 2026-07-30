"""W14 admin self-edit lane routes (R7).

Public routes require ALL of: live auth, a valid tenant identity, the tenant
tier carrying `platform_customize` (only the operator-granted `admin` tier
ships with it), and the R7 internal-mode rollout listing the tenant
(`customization_flags.enabled(7, ...)` — mode `all` deliberately reads as
off). The co-sign routes are internal: they authenticate with
`LEAF_CUSTOMIZATION_APPROVAL_SECRET`, the independent-approval credential the
authoring harness never holds, and they are NOT on the wire-contract §0
back-edge allowlist, so the harness back-edge cannot reach them either.
"""
from __future__ import annotations

import logging
import os
import traceback
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import deps
import entitlements
import platform_customize as lane
from customization_flags import enabled as customization_enabled
from envelopes import error_obj, with_envelope_fields
from tenant_id_validator import is_valid_tenant_id

router = APIRouter()

_LOG = logging.getLogger(__name__)

_ERROR_MESSAGES = {
    "platform_customize_disabled": "Platform self-edit is not enabled for this workspace.",
    "platform_repo_unavailable": "The platform repository is not available to this deployment.",
    "fundamental_manifest_unavailable": "The fundamental-path manifest cannot be read; the lane fails closed.",
    "protected_ref_refused": "Only admin-customize branches may be written; protected refs are refused.",
    "cosign_required": "This change touches fundamental paths and needs the out-of-band co-sign before landing.",
    "cosign_self_approval": "The proposing subject cannot co-sign their own change.",
}


class EditItem(BaseModel):
    path: str = Field(..., min_length=1, max_length=500)
    content: Optional[str] = None
    delete: bool = False

    class Config:
        extra = "forbid"


class ProposeRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=lane.MAX_TITLE)
    edits: List[EditItem] = Field(..., min_length=1, max_length=lane.MAX_EDITS)

    class Config:
        extra = "forbid"


class CosignRequest(BaseModel):
    change_id: str
    commit_sha: str

    class Config:
        extra = "forbid"


def _error(exc: lane.PlatformCustomizeError,
           cause: BaseException | None = None) -> JSONResponse:
    """Opaque reason code out, operator detail into the log (author.py idiom)."""
    if cause is not None:
        frames = " | ".join(
            f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}"
            for f in traceback.extract_tb(cause.__traceback__)
        )
        _LOG.error("platform_customize_refused: code=%s cause=%s at %s",
                   exc.code, type(cause).__name__, frames or "<no frames>")
    elif exc.status_code >= 500:
        _LOG.warning("platform_customize_refused: code=%s detail=%s",
                     exc.code, exc.detail or "-")
    else:
        _LOG.debug("platform_customize_refused: code=%s", exc.code)
    message = _ERROR_MESSAGES.get(
        exc.code, "Platform self-edit could not complete. Nothing was landed.")
    return JSONResponse(status_code=exc.status_code, content=with_envelope_fields({
        "error": error_obj("BAD_PARAMS", message, retryable=exc.status_code >= 500),
        "reason_code": exc.code,
    }))


def _gate(tenant: Any) -> tuple[str, str] | JSONResponse:
    """The R7 admission chain. Returns (tenant_id, tier) or the refusal."""
    if not deps.auth_live():
        return _error(lane.PlatformCustomizeError("platform_customize_auth_required", 503))
    tenant_id = str(tenant).strip()
    if not is_valid_tenant_id(tenant_id):
        return _error(lane.PlatformCustomizeError("tenant_identity_invalid", 403))
    tier = entitlements.resolve_tier(tenant)
    try:
        caps = entitlements.entitlements_for(tier)
    except entitlements.EntitlementsError:
        return entitlements.policy_unavailable_response("platform_customize", tier)
    if caps.get("platform_customize") is not True:
        return entitlements.entitlement_denied_response("platform_customize", tier)
    if not customization_enabled(7, tenant_id):
        return _error(lane.PlatformCustomizeError("platform_customize_disabled", 404))
    return tenant_id, tier


def _subject(tenant: Any, tenant_id: str) -> str:
    subject = getattr(tenant, "subject", None) or getattr(tenant, "sub", None)
    return str(subject) if subject else f"tenant:{tenant_id}"


@router.post("/api/platform/customize")
def propose(req: ProposeRequest, tenant=Depends(deps.require_tenant)) -> Dict[str, Any]:
    admitted = _gate(tenant)
    if isinstance(admitted, JSONResponse):
        return admitted
    tenant_id, _tier = admitted
    try:
        return lane.propose(
            tenant_id=tenant_id,
            subject=_subject(tenant, tenant_id),
            title=req.title,
            edits=[e.dict() for e in req.edits],
        )
    except lane.PlatformCustomizeError as exc:
        return _error(exc)
    except Exception as exc:  # noqa: BLE001
        return _error(lane.PlatformCustomizeError("platform_customize_failed", 503), cause=exc)


@router.get("/api/platform/customize/{change_id}")
def status(change_id: str, tenant=Depends(deps.require_tenant)) -> Dict[str, Any]:
    admitted = _gate(tenant)
    if isinstance(admitted, JSONResponse):
        return admitted
    tenant_id, _tier = admitted
    try:
        record = lane.load_record(change_id)
        if record.get("tenant_id") != tenant_id:
            # Same shape as absent: existence must not leak across tenants.
            return _error(lane.PlatformCustomizeError("change_not_found", 404))
        return lane.public_view(record)
    except lane.PlatformCustomizeError as exc:
        return _error(exc)


@router.post("/api/platform/customize/{change_id}/land")
def land(change_id: str, tenant=Depends(deps.require_tenant)) -> Dict[str, Any]:
    admitted = _gate(tenant)
    if isinstance(admitted, JSONResponse):
        return admitted
    tenant_id, _tier = admitted
    try:
        return lane.land(change_id=change_id, tenant_id=tenant_id)
    except lane.PlatformCustomizeError as exc:
        return _error(exc)
    except Exception as exc:  # noqa: BLE001
        return _error(lane.PlatformCustomizeError("platform_customize_failed", 503), cause=exc)


def _cosign_admitted(x_approval_secret: Optional[str],
                     x_approver_subject: Optional[str]) -> str | JSONResponse:
    if not lane.verify_approval_secret(x_approval_secret):
        return _error(lane.PlatformCustomizeError("approval_authority_denied", 403))
    approver = (x_approver_subject or "").strip()
    if not approver:
        return _error(lane.PlatformCustomizeError("cosign_approver_missing", 422))
    return approver


@router.post("/internal/platform-customize/cosign")
def cosign(req: CosignRequest,
           x_approval_secret: str | None = Header(default=None),
           x_approver_subject: str | None = Header(default=None)) -> Dict[str, Any]:
    """Independent co-sign. The harness never holds this credential."""
    admitted = _cosign_admitted(x_approval_secret, x_approver_subject)
    if isinstance(admitted, JSONResponse):
        return admitted
    try:
        return lane.cosign(change_id=req.change_id, approver_subject=admitted,
                           commit_sha=req.commit_sha, approve=True)
    except lane.PlatformCustomizeError as exc:
        return _error(exc)


@router.post("/internal/platform-customize/deny")
def deny(req: CosignRequest,
         x_approval_secret: str | None = Header(default=None),
         x_approver_subject: str | None = Header(default=None)) -> Dict[str, Any]:
    admitted = _cosign_admitted(x_approval_secret, x_approver_subject)
    if isinstance(admitted, JSONResponse):
        return admitted
    try:
        return lane.cosign(change_id=req.change_id, approver_subject=admitted,
                           commit_sha=req.commit_sha, approve=False)
    except lane.PlatformCustomizeError as exc:
        return _error(exc)
