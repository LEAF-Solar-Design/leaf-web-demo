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


def _checkout_view(co: Any) -> Any:
    """Manifest `checkout` lock → the read-only surface shape
    `{holder, acquired, expires}` or null. No acquire/release endpoints this wave
    (read-only): the frontend renders a calm "someone else is editing" chip."""
    if not co:
        return None
    return {"holder": co.get("holder"), "acquired": co.get("acquired"),
            "expires": co.get("expires")}


def _version_row(e: Dict[str, Any]) -> Dict[str, Any]:
    """One manifest `versions[]` entry → the version-history row shape
    (CONTRACT-ADDENDUM §11 manifest fields; missing optional fields → null)."""
    parent = e.get("parent")
    return {
        "v": int(e["v"]),
        "parent": (int(parent) if parent is not None else None),
        "created": e.get("created"),
        "bytes": e.get("bytes"),
        "sha256": e.get("sha256"),
        "tool": e.get("tool"),
        "workitem_id": e.get("workitem_id"),
        "note": e.get("note"),
    }


@router.get("/api/drawings/{drawing_id}/versions")
def get_versions(drawing_id: str, tenant_id: str = Depends(deps.require_tenant)) -> Dict[str, Any]:
    """Full version-history chain straight from the store manifest — the read the
    UI's version-chain popover needs (undo/redo only stepped head one hop).

    §10-enveloped `{drawing_id, head, latest, versions:[{v, parent, created, bytes,
    sha256, tool, workitem_id, note}], checkout: {holder, acquired, expires}|null}`
    (§14: `checkout` is the single-writer lock from the manifest, read-only — no
    acquire/release this wave). Same 404 pattern as the intake route for
    an unknown drawing (the well-known `demo` bootstraps on first read at
    APS_LIVE=0; any other unknown drawing → BAD_PARAMS 404)."""
    import store  # da/store.py; importable via write_loop's sys.path setup (imported above)

    backend = _backend()
    try:
        write_loop.ensure_demo_drawing(backend, str(tenant_id), drawing_id)
        m = store.load_manifest(backend, str(tenant_id), drawing_id)
    except (KeyError, ValueError) as exc:
        return error_response(ErrorCode.BAD_PARAMS, f"drawing unavailable: {exc}",
                              retryable=False, status_code=404)
    view = {
        "drawing_id": drawing_id,
        "head": int(m["head"]),
        "latest": int(m["latest"]),
        "versions": [_version_row(e) for e in m.get("versions", [])],
        "checkout": _checkout_view(m.get("checkout")),  # single-writer lock (read-only)
    }
    return with_envelope_fields(deps.tenant_echo(view, tenant_id))
