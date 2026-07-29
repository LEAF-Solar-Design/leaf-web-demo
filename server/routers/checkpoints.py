"""Session checkpoint metadata routes.

Creating a checkpoint records the current drawing head and transcript sequence.
It does not rewind, mutate, or otherwise couple the drawing and turn engines.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import checkpoints
import deps
import session_store
import write_loop
from envelopes import ErrorCode, error_response, with_envelope_fields

router = APIRouter()
MAX_LABEL_LENGTH = 200


class CreateCheckpointRequest(BaseModel):
    label: Optional[str] = None


def _session_not_found(session_id: str) -> JSONResponse:
    return error_response(
        ErrorCode.SESSION_NOT_FOUND,
        f"unknown session_id {session_id!r}",
        retryable=False,
    )


def _require_owned_session(session_id: str, tenant: Any):
    session = session_store.get_session(session_id)
    if session is None or str(session.get("tenant_id")) != str(tenant):
        return None
    return session


def _drawing_version(tenant_id: str, drawing_id: str) -> int:
    """Read the drawing store's current head version without mutating it."""
    import store

    backend = write_loop.backend_for_tenant(tenant_id, aps_live=False, da=None)
    manifest = store.load_manifest(backend, tenant_id, drawing_id)
    return int(manifest["head"])


@router.post("/api/sessions/{session_id}/checkpoints")
def create_checkpoint(session_id: str, req: CreateCheckpointRequest,
                      tenant=Depends(deps.require_active_tenant)):
    session = _require_owned_session(session_id, tenant)
    if session is None:
        return _session_not_found(session_id)
    if req.label is not None and len(req.label) > MAX_LABEL_LENGTH:
        return error_response(
            ErrorCode.BAD_PARAMS,
            f"checkpoint label must be at most {MAX_LABEL_LENGTH} characters",
            retryable=False,
            status_code=400,
        )

    try:
        drawing_version = _drawing_version(str(tenant), session["drawing_id"])
    except (KeyError, ValueError):
        return error_response(
            ErrorCode.BAD_PARAMS,
            f"drawing {session['drawing_id']!r} is unavailable",
            retryable=False,
            status_code=404,
        )

    checkpoint = checkpoints.create_checkpoint(
        session_id,
        str(tenant),
        session["drawing_id"],
        drawing_version,
        int(session["last_seq"]),
        req.label,
    )
    if checkpoint is None:
        return error_response(
            ErrorCode.BAD_PARAMS,
            "a session may have at most 50 checkpoints",
            retryable=False,
            status_code=409,
        )
    return JSONResponse(
        status_code=201,
        content=deps.tenant_echo(with_envelope_fields(checkpoint), tenant),
    )


@router.get("/api/sessions/{session_id}/checkpoints")
def list_checkpoints(session_id: str, tenant=Depends(deps.require_active_tenant)):
    if _require_owned_session(session_id, tenant) is None:
        return _session_not_found(session_id)
    body = {"checkpoints": checkpoints.list_checkpoints(session_id)}
    return deps.tenant_echo(with_envelope_fields(body), tenant)
