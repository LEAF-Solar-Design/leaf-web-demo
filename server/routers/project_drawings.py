"""Authenticated canonical project drawing context and checkout endpoints."""
from __future__ import annotations

import math
import re
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

import deps
import solar_project_context as service
from envelopes import ErrorCode, err_envelope, with_envelope_fields

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}
_PROJECT_DRAWING_PATH = re.compile(
    r"^/api/projects/[^/]+/(?:drawing-versions/[^/]+/context|drawings/[^/]+/checkout)/?$")
_FAILURES = {
    "INVALID_BINDING": (400, False), "CONTEXT_NOT_FOUND": (404, False),
    "PROJECT_FORBIDDEN": (403, False), "CANONICAL_AUTHORITY_REQUIRED": (409, False),
    "INTAKE_PROOF_REQUIRED": (409, False), "INTAKE_REFERENCE_INVALID": (500, False),
    "INTAKE_DIGEST_MISMATCH": (500, False), "INTAKE_INVALID": (500, False),
    "INTAKE_UNAVAILABLE": (503, True), "STORE_UNAVAILABLE": (503, True),
    "CHECKOUT_PARAMS_INVALID": (400, False), "CHECKOUT_CONFLICT": (409, True),
    "CHECKOUT_REQUIRED": (409, False), "CHECKOUT_EXPIRED": (409, False),
    "CHECKOUT_DENIED": (403, False), "CHECKOUT_STALE": (409, False),
    "CHECKOUT_UNAVAILABLE": (503, True), "FENCE_EXHAUSTED": (409, False),
    "WRITES_DRAINED": (503, True), "STALE_VERSION": (409, False), "INTERNAL": (500, False),
}


async def no_store_responses(request: Request, call_next):
    """Keep authentication and validation responses on these paths uncached too."""
    response = await call_next(request)
    if _PROJECT_DRAWING_PATH.fullmatch(request.url.path):
        response.headers["Cache-Control"] = "no-store"
    return response


class CheckoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    holder: Optional[StrictStr] = Field(default=None, min_length=1, max_length=200)
    ttl_s: float = Field(default=3600, gt=0, le=86400, allow_inf_nan=False)

    @field_validator("ttl_s", mode="before")
    @classmethod
    def numeric_ttl(cls, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("TTL must be numeric")
        try:
            if not math.isfinite(value):
                raise ValueError("TTL must be finite")
        except OverflowError:
            raise ValueError("TTL must be finite") from None
        return value


def _headers(tenant, project_id, org_header, project_header):
    if (org_header is not None and str(org_header) != str(tenant)) or (
            project_header is not None and project_header != project_id):
        service.refuse("CONTEXT_NOT_FOUND")


def _identity(tenant, project_id, drawing_id):
    return {"organization_id": str(tenant), "project_id": str(project_id),
            "drawing_id": str(drawing_id)}


def _dispatch(operation):
    try:
        return JSONResponse(content=with_envelope_fields(operation()), headers=_NO_STORE)
    except service.ProjectContextError as exc:
        reason = exc.reason_code
        suffix = reason.removeprefix("SIP_R1_")
        if suffix not in _FAILURES:
            reason, suffix = "SIP_R1_INTERNAL", "INTERNAL"
        status, retry = _FAILURES[suffix]
        code = ErrorCode.FORBIDDEN if status == 403 else (
            ErrorCode.INTERNAL if status >= 500 else ErrorCode.BAD_PARAMS)
        body = err_envelope(code, "Project drawing operation unavailable.", retry)
        body["error"]["reason_code"] = reason
        if suffix == "CHECKOUT_CONFLICT":
            body.update(acquired=False, locked_by=exc.checkout.holder if exc.checkout else None,
                        checkout=service.public_checkout(exc.checkout))
        return JSONResponse(content=body, status_code=status, headers=_NO_STORE)
    except Exception:
        return _dispatch(lambda: service.refuse("INTERNAL"))


@router.get("/api/projects/{project_id}/drawing-versions/{input_version_id}/context")
def context_route(project_id: UUID, input_version_id: UUID, drawing_id: Optional[UUID] = None,
                  tenant=Depends(deps.require_active_tenant),
                  x_org_id: Optional[UUID] = Header(default=None),
                  x_project_id: Optional[UUID] = Header(default=None)):
    def operation():
        _headers(tenant, project_id, x_org_id, x_project_id)
        context = service.resolve_context(tenant, project_id, input_version_id, drawing_id=drawing_id)
        binding = context.binding
        identity = _identity(binding.organization_id, binding.project_id, binding.drawing_id)
        identity.update(input_version_id=str(binding.input_version_id),
                        head_version_id=str(binding.head_version_id), is_head=binding.is_head,
                        intake_sha256=context.intake_sha256)
        return {"context": identity, "intake": context.intake}
    return _dispatch(operation)


@router.get("/api/projects/{project_id}/drawings/{drawing_id}/checkout")
def status_route(project_id: UUID, drawing_id: UUID, tenant=Depends(deps.require_active_tenant),
                 x_org_id: Optional[UUID] = Header(default=None),
                 x_project_id: Optional[UUID] = Header(default=None)):
    def operation():
        _headers(tenant, project_id, x_org_id, x_project_id)
        state = service.checkout_status(tenant, project_id, drawing_id)
        lease = state.checkout
        if lease is not None and lease.expires_at <= state.observed_at:
            lease = None
        return {**_identity(state.organization_id, project_id, drawing_id),
                "checkout": service.public_checkout(lease)}
    return _dispatch(operation)


@router.post("/api/projects/{project_id}/drawings/{drawing_id}/checkout")
def acquire_route(project_id: UUID, drawing_id: UUID, req: Optional[CheckoutRequest] = None,
                  tenant=Depends(deps.require_active_tenant),
                  x_org_id: Optional[UUID] = Header(default=None),
                  x_project_id: Optional[UUID] = Header(default=None),
                  x_checkout_capability: Optional[str] = Header(default=None)):
    def operation():
        _headers(tenant, project_id, x_org_id, x_project_id)
        request = req or CheckoutRequest()
        lease, token = service.acquire_project_checkout(tenant, project_id, drawing_id,
            holder=request.holder, ttl_s=request.ttl_s, checkout_capability_token=x_checkout_capability)
        return {**_identity(lease.organization_id, project_id, drawing_id), "acquired": True,
                "checkout": service.public_checkout(lease), "checkout_capability": token}
    return _dispatch(operation)


async def _no_delete_body(request: Request):
    if await request.body():
        raise HTTPException(status_code=422, detail="Release accepts no body", headers=_NO_STORE)


@router.delete("/api/projects/{project_id}/drawings/{drawing_id}/checkout",
               dependencies=[Depends(_no_delete_body)])
def release_route(project_id: UUID, drawing_id: UUID, tenant=Depends(deps.require_active_tenant),
                  x_org_id: Optional[UUID] = Header(default=None),
                  x_project_id: Optional[UUID] = Header(default=None),
                  x_checkout_capability: Optional[str] = Header(default=None)):
    def operation():
        _headers(tenant, project_id, x_org_id, x_project_id)
        released = service.release_project_checkout(tenant, project_id, drawing_id,
            checkout_capability_token=x_checkout_capability)
        return {**_identity(tenant, project_id, drawing_id), "released": released, "checkout": None}
    return _dispatch(operation)
