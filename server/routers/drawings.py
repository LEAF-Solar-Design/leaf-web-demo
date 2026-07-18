"""Versioned drawing endpoints (M2 write loop) — this session's router.

    GET  /api/drawings/{drawing_id}/intake?version=head|<n>
         -> {intake, version, head, latest}   (envelope-wrapped per §10)
    POST /api/drawings/{drawing_id}/undo   -> {version, head, latest, intake}
    POST /api/drawings/{drawing_id}/redo   -> {version, head, latest, intake}

undo repoints head to its parent (objects are never deleted); redo re-advances
head toward `latest` along the parent chain. A successful `drawing.write` tool run
(via POST /api/run) creates the versions these endpoints serve, and stamps
`result.new_version = {drawing_id, version, parent}` into its §3 envelope.

The store backend is chosen by APS_LIVE (deps.APS_LIVE): local FilesystemBackend
offline (no credential), OSSBackend live. Serving the reads directly here (v1)
keeps the credential OUT of the app process at APS_LIVE=0 — the tested condition;
promoting the live reads through the broker for strict isolation at APS_LIVE=1 is
a documented follow-up (CONTRACT-ADDENDUM §11).
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

import deps
import write_loop
from envelopes import ErrorCode, error_response, with_envelope_fields

router = APIRouter()


def _backend():
    return write_loop.default_backend(
        aps_live=deps.APS_LIVE,
        da=deps.get_da_client() if deps.APS_LIVE else None,
    )


@router.get("/api/drawings/{drawing_id}/intake")
def get_intake(drawing_id: str, version: str = "head",
               tenant_id: str = Depends(deps.require_tenant)) -> Dict[str, Any]:
    ver: Any = version
    if isinstance(version, str) and version not in ("head", "latest") and version.lstrip("-").isdigit():
        ver = int(version)
    try:
        view = write_loop.intake_view(str(tenant_id), drawing_id, ver, backend=_backend())
    except (KeyError, ValueError) as exc:
        return error_response(ErrorCode.BAD_PARAMS, f"drawing/version unavailable: {exc}",
                              retryable=False, status_code=404)
    return with_envelope_fields(deps.tenant_echo(view, tenant_id))


@router.post("/api/drawings/{drawing_id}/undo")
def undo(drawing_id: str, tenant_id: str = Depends(deps.require_tenant)) -> Dict[str, Any]:
    try:
        view = write_loop.undo_view(str(tenant_id), drawing_id, backend=_backend())
    except (KeyError, ValueError) as exc:
        return error_response(ErrorCode.BAD_PARAMS, str(exc), retryable=False, status_code=400)
    return with_envelope_fields(deps.tenant_echo(view, tenant_id))


@router.post("/api/drawings/{drawing_id}/redo")
def redo(drawing_id: str, tenant_id: str = Depends(deps.require_tenant)) -> Dict[str, Any]:
    try:
        view = write_loop.redo_view(str(tenant_id), drawing_id, backend=_backend())
    except (KeyError, ValueError) as exc:
        return error_response(ErrorCode.BAD_PARAMS, str(exc), retryable=False, status_code=400)
    return with_envelope_fields(deps.tenant_echo(view, tenant_id))
