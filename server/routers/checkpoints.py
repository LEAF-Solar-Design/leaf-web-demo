"""Session checkpoint routes.

Creating records the current drawing head and transcript sequence. Restoring
copies the recorded drawing version forward and records a transcript boundary.
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


def _restore_drawing_version(tenant_id: str, drawing_id: str,
                             recorded_version: str) -> int:
    """Copy a recorded version forward as a new immutable drawing head.

    ``write_loop._put_bytes_version`` is the drawing store's supported append
    path. It preserves history while making the new head's bytes identical to
    the checkpointed version, rather than rewriting an existing manifest row.
    """
    import store

    backend = write_loop.backend_for_tenant(tenant_id, aps_live=False, da=None)
    manifest = store.load_manifest(backend, tenant_id, drawing_id)
    source_version, source_key = store.resolve_version(
        backend, tenant_id, drawing_id, int(recorded_version))
    source_bytes = backend.get(source_key)
    return int(write_loop._put_bytes_version(
        backend,
        tenant_id,
        drawing_id,
        source_bytes,
        parent_version=int(manifest["head"]),
        meta={
            "tool": "checkpoint_restore",
            "note": f"restore checkpoint version {source_version}",
        },
        require_parent_is_head=True,
    ))


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
    body = {"checkpoints": checkpoints.list_checkpoints(session_id, str(tenant))}
    return deps.tenant_echo(with_envelope_fields(body), tenant)


@router.post("/api/sessions/{session_id}/checkpoints/{checkpoint_id}/restore")
def restore_checkpoint(session_id: str, checkpoint_id: str,
                       tenant=Depends(deps.require_active_tenant)):
    """Restore a checkpointed drawing by copy-forward and record an audit event.

    The transcript remains append-only. ``checkpoint_restored`` records the
    boundary that a turn_runner.py follow-up must apply in its prior-context
    builder. This route does not change turn context itself.

    The stores are separate, so this is ordered rather than cross-store atomic:
    validate all state, copy the drawing forward, then append the audit event.
    If the final append fails, the committed drawing restore is still reported
    with ``event_recorded: false``.
    """
    session = _require_owned_session(session_id, tenant)
    if session is None:
        return _session_not_found(session_id)
    checkpoint = checkpoints.get_checkpoint(session_id, str(tenant), checkpoint_id)
    if checkpoint is None:
        return _session_not_found(session_id)
    if session.get("active_turn_id") is not None:
        return error_response(
            ErrorCode.TURN_IN_PROGRESS,
            f"cannot restore while turn {session['active_turn_id']!r} is in progress; cancel it first",
            retryable=False,
            status_code=409,
        )

    try:
        new_drawing_version = _restore_drawing_version(
            str(tenant), checkpoint["drawing_id"], checkpoint["drawing_version"])
    except (KeyError, ValueError, OSError) as exc:
        return error_response(
            ErrorCode.BAD_PARAMS,
            f"checkpoint drawing version is unavailable: {exc}",
            retryable=False,
            status_code=409,
        )

    event_recorded = True
    event_seq = None
    try:
        event_seq = session_store.append_event(
            session_id,
            None,
            "checkpoint_restored",
            {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "transcript_seq": checkpoint["transcript_seq"],
                "drawing_version": checkpoint["drawing_version"],
                "new_drawing_version": new_drawing_version,
            },
        )
    except Exception:  # committed drawing restore must remain visible
        event_recorded = False

    body = {
        **checkpoint,
        "new_drawing_version": new_drawing_version,
        "event_seq": event_seq,
        "event_recorded": event_recorded,
    }
    return deps.tenant_echo(with_envelope_fields(body), tenant)
