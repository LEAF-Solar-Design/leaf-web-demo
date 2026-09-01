"""Authenticated browser-safe routes for the trusted Glug Mushy boundary."""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr

import deps
import glug_adoption
from glug_executor import GlugExecutor, GlugExecutorError


router = APIRouter()
_EXECUTOR: Optional[GlugExecutor] = None


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Claim(_ClosedModel):
    contract: StrictStr
    id: StrictStr
    workspace: StrictStr
    actor_digest: StrictStr
    power: StrictStr
    base_commit: StrictStr
    issued_at: StrictStr
    expires_at: StrictStr
    signature: StrictStr


class ClaimRequest(_ClosedModel):
    workspace_id: StrictStr
    requested_power: StrictStr


class ExecuteRequest(_ClosedModel):
    workspace_id: StrictStr
    requested_power: StrictStr
    instruction: StrictStr = Field(min_length=1, max_length=20_000)
    claim: Claim


class PublishRequest(_ClosedModel):
    workspace_id: StrictStr
    requested_power: StrictStr
    approval_id: StrictStr
    stage_receipt: Dict[str, Any]


def set_executor(executor: Optional[GlugExecutor]) -> None:
    global _EXECUTOR
    _EXECUTOR = executor


def _executor() -> GlugExecutor:
    return _EXECUTOR or GlugExecutor.configured()


def _control_actor(tenant: Any) -> str:
    expected_tenant = os.environ.get("GLUG_MUSHY_CONTROL_TENANT_ID", "").strip()
    allowed_subjects = frozenset(
        item.strip()
        for item in os.environ.get("GLUG_MUSHY_CONTROL_SUBJECTS", "").split(",")
        if item.strip()
    )
    if not expected_tenant or not allowed_subjects:
        raise GlugExecutorError(
            "control_authority_unavailable",
            "Glug control authority is not configured",
            503,
        )
    if not isinstance(tenant, deps.TenantContext):
        raise GlugExecutorError(
            "control_authority_denied", "Glug control authority is required", 403)
    subject = getattr(tenant, "subject", None)
    if (
        getattr(tenant, "authority_resolved", False) is not True
        or getattr(tenant, "tenant_id", None) != expected_tenant
        or not isinstance(subject, str)
        or not subject
        or subject not in allowed_subjects
    ):
        raise GlugExecutorError(
            "control_authority_denied", "Glug control authority is required", 403)
    return subject


def _failure(error: Exception) -> JSONResponse:
    if isinstance(error, GlugExecutorError):
        return JSONResponse(status_code=error.status, content={
            "ok": False,
            "error": {"code": error.code, "message": str(error)},
        })
    if isinstance(error, glug_adoption.GlugAdoptionError):
        return JSONResponse(status_code=409, content={
            "ok": False,
            "error": {"code": "adoption_refused", "message": str(error)},
        })
    return JSONResponse(status_code=503, content={
        "ok": False,
        "error": {"code": "executor_unavailable", "message": "Glug executor is unavailable"},
    })


@router.get("/api/glug/mushy/pin")
def pin(tenant: Any = Depends(deps.require_tenant)) -> JSONResponse:
    try:
        _control_actor(tenant)
        return JSONResponse(status_code=200, content={
            "ok": True, "pin": dict(_executor().pin_receipt()),
        })
    except Exception as exc:
        return _failure(exc)


@router.post("/api/glug/mushy/claim")
def claim(
    request: ClaimRequest,
    tenant: Any = Depends(deps.require_tenant),
) -> JSONResponse:
    try:
        actor_id = _control_actor(tenant)
        issued = _executor().issue_claim(request.model_dump(), actor_id=actor_id)
        return JSONResponse(status_code=201, content={"ok": True, "claim": dict(issued)})
    except Exception as exc:
        return _failure(exc)


@router.post("/api/glug/mushy/execute")
def execute(
    request: ExecuteRequest,
    tenant: Any = Depends(deps.require_tenant),
) -> JSONResponse:
    try:
        actor_id = _control_actor(tenant)
        result = _executor().execute(request.model_dump(), actor_id=actor_id)
        return JSONResponse(status_code=200, content={"ok": True, **dict(result)})
    except Exception as exc:
        return _failure(exc)


@router.post("/api/glug/mushy/publish")
def publish(
    request: PublishRequest,
    tenant: Any = Depends(deps.require_tenant),
) -> JSONResponse:
    try:
        actor_id = _control_actor(tenant)
        result = _executor().publish(request.model_dump(), actor_id=actor_id)
        return JSONResponse(status_code=201, content={"ok": True, "publication": dict(result)})
    except Exception as exc:
        return _failure(exc)
