"""Authenticated browser-safe routes for the durable Glug Mushy job rail."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr

import deps
import glug_adoption
from glug_executor import GlugExecutor, GlugExecutorError
from glug_jobs import GlugJobService, GlugJobStore
from glug_live_adapters import SQLiteApprovalStore


router = APIRouter()
_EXECUTOR: Optional[GlugExecutor] = None
_SERVICE: Optional[GlugJobService] = None


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JobRequest(_ClosedModel):
    workspace_id: StrictStr
    requested_power: StrictStr
    idempotency_key: StrictStr = Field(min_length=1, max_length=200)
    instruction: StrictStr | None = Field(default=None, min_length=1, max_length=20_000)
    origin_job_id: StrictStr | None = None
    approval_id: StrictStr | None = None


class ApprovalRequest(_ClosedModel):
    workspace_id: StrictStr
    origin_job_id: StrictStr
    publication_power: StrictStr
    idempotency_key: StrictStr = Field(min_length=1, max_length=200)


def set_executor(executor: Optional[GlugExecutor]) -> None:
    global _EXECUTOR
    _EXECUTOR = executor


def set_job_service(service: Optional[GlugJobService]) -> None:
    global _SERVICE
    _SERVICE = service


def shutdown_services() -> None:
    global _SERVICE
    if _SERVICE is not None and _SERVICE.pool is not None:
        _SERVICE.pool.shutdown(wait=False, cancel_futures=False)
    _SERVICE = None


def initialize_services() -> None:
    """Validate every mutation mount and resume safe queued jobs."""
    _service()


def _executor() -> GlugExecutor:
    return _EXECUTOR or GlugExecutor.configured()


def _service() -> GlugJobService:
    global _SERVICE
    if _SERVICE is not None:
        return _SERVICE
    executor = _executor()
    if not isinstance(executor.approvals, SQLiteApprovalStore):
        raise GlugExecutorError("executor_unavailable", "Durable Glug approvals are not mounted", 503)
    database = os.environ.get("GLUG_MUSHY_JOB_DATABASE", "").strip()
    if not database:
        raise GlugExecutorError("executor_unavailable", "Durable Glug jobs are not configured", 503)
    _SERVICE = GlugJobService(
        store=GlugJobStore(Path(database)), executor=executor,
        approvals=executor.approvals,
    )
    return _SERVICE


def _control_actor(tenant: Any) -> str:
    expected_tenant = os.environ.get("GLUG_MUSHY_CONTROL_TENANT_ID", "").strip()
    allowed_subjects = frozenset(
        item.strip()
        for item in os.environ.get("GLUG_MUSHY_CONTROL_SUBJECTS", "").split(",")
        if item.strip()
    )
    if not expected_tenant or not allowed_subjects:
        raise HTTPException(status_code=503, detail={
            "code": "control_authority_unavailable",
            "message": "Glug control authority is not configured",
        })
    if not isinstance(tenant, deps.TenantContext):
        raise HTTPException(status_code=403, detail={
            "code": "control_authority_denied", "message": "Glug control authority is required",
        })
    subject = getattr(tenant, "subject", None)
    if (
        getattr(tenant, "authority_resolved", False) is not True
        or getattr(tenant, "tenant_id", None) != expected_tenant
        or not isinstance(subject, str) or not subject or subject not in allowed_subjects
    ):
        raise HTTPException(status_code=403, detail={
            "code": "control_authority_denied", "message": "Glug control authority is required",
        })
    return subject


async def require_control_actor(
    request: Request,
    tenant: Any = Depends(deps.require_tenant),
    board_actor: str | None = Header(default=None, alias="X-Glug-Board-Actor"),
    board_timestamp: str | None = Header(default=None, alias="X-Glug-Board-Timestamp"),
    board_signature: str | None = Header(default=None, alias="X-Glug-Board-Signature"),
) -> str:
    """Accept direct control actors or a signed server-to-server board identity.

    The Next.js proxy derives the board account and role from its authenticated
    session. The browser never supplies these headers or the signing secret.
    """
    control_subject = _control_actor(tenant)
    proxy_subject = os.environ.get("GLUG_MUSHY_PROXY_SUBJECT", "").strip()
    forwarded = (board_actor, board_timestamp, board_signature)
    if all(value is None for value in forwarded):
        if proxy_subject and control_subject == proxy_subject:
            raise HTTPException(status_code=403, detail={"code": "control_authority_denied"})
        return control_subject
    if any(value is None for value in forwarded):
        raise HTTPException(status_code=403, detail={"code": "control_authority_denied"})
    secret = os.environ.get("GLUG_MUSHY_PROXY_SIGNING_SECRET", "")
    if control_subject != proxy_subject or len(secret.encode("utf-8")) < 32:
        raise HTTPException(status_code=403, detail={"code": "control_authority_denied"})
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}", board_actor or ""):
        raise HTTPException(status_code=403, detail={"code": "control_authority_denied"})
    try:
        timestamp = int(board_timestamp or "")
    except ValueError as exc:
        raise HTTPException(status_code=403, detail={"code": "control_authority_denied"}) from exc
    if abs(int(time.time()) - timestamp) > 300:
        raise HTTPException(status_code=403, detail={"code": "control_authority_denied"})
    body_digest = hashlib.sha256(await request.body()).hexdigest()
    payload = (
        f"v1\n{board_actor}\n{timestamp}\n{request.method.upper()}\n"
        f"{request.url.path}\n{body_digest}"
    ).encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, board_signature or ""):
        raise HTTPException(status_code=403, detail={"code": "control_authority_denied"})
    return board_actor or control_subject


def _failure(error: Exception) -> JSONResponse:
    if isinstance(error, GlugExecutorError):
        return JSONResponse(status_code=error.status, content={"ok": False, "error": {
            "code": error.code, "message": "Glug request could not be completed",
        }})
    if isinstance(error, glug_adoption.GlugAdoptionError):
        return JSONResponse(status_code=409, content={"ok": False, "error": {
            "code": "adoption_refused", "message": "Glug adoption request was refused",
        }})
    return JSONResponse(status_code=503, content={"ok": False, "error": {
        "code": "executor_unavailable", "message": "Glug executor is unavailable",
    }})


@router.get("/api/glug/mushy/pin")
def pin(actor: str = Depends(require_control_actor)) -> JSONResponse:
    try:
        return JSONResponse(status_code=200, content={"ok": True, "pin": dict(_executor().pin_receipt())})
    except Exception as exc:
        return _failure(exc)


@router.post("/api/glug/mushy/jobs")
def create_job(request: JobRequest, actor: str = Depends(require_control_actor)) -> JSONResponse:
    try:
        if request.workspace_id != "glug":
            raise GlugExecutorError("workspace_unavailable", "Unknown workspace", 404)
        job, created = _service().create(
            actor_id=actor, requested_power=request.requested_power,
            instruction=request.instruction, origin_job_id=request.origin_job_id,
            approval_id=request.approval_id, idempotency_key=request.idempotency_key,
        )
        return JSONResponse(status_code=202, content={"ok": True, "created": created, "job": dict(job)})
    except Exception as exc:
        return _failure(exc)


@router.get("/api/glug/mushy/jobs/{job_id}")
def get_job(job_id: str, actor: str = Depends(require_control_actor)) -> JSONResponse:
    try:
        job = _service().store.get(job_id, actor_id=actor)
        if job is None:
            raise GlugExecutorError("job_unavailable", "Job is unavailable", 404)
        return JSONResponse(status_code=200, content={"ok": True, "job": dict(job)})
    except Exception as exc:
        return _failure(exc)


@router.post("/api/glug/mushy/approvals")
def issue_approval(
    request: ApprovalRequest, actor: str = Depends(require_control_actor),
) -> JSONResponse:
    try:
        if request.workspace_id != "glug":
            raise GlugExecutorError("workspace_unavailable", "Unknown workspace", 404)
        approval = _service().issue_approval(
            actor_id=actor, origin_job_id=request.origin_job_id,
            publication_power=request.publication_power,
            idempotency_key=request.idempotency_key,
        )
        return JSONResponse(status_code=201, content={"ok": True, "approval": dict(approval)})
    except Exception as exc:
        return _failure(exc)


def _migration_refusal() -> JSONResponse:
    return JSONResponse(status_code=410, content={"ok": False, "error": {
        "code": "durable_job_required",
        "message": "Use the actor-scoped Glug Mushy job rail.",
    }})


@router.post("/api/glug/mushy/claim")
def legacy_claim(actor: str = Depends(require_control_actor)) -> JSONResponse:
    return _migration_refusal()


@router.post("/api/glug/mushy/execute")
def legacy_execute(actor: str = Depends(require_control_actor)) -> JSONResponse:
    return _migration_refusal()


@router.post("/api/glug/mushy/publish")
def legacy_publish(actor: str = Depends(require_control_actor)) -> JSONResponse:
    return _migration_refusal()
