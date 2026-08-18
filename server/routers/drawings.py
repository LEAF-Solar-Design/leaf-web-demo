"""Versioned drawing endpoints (M2 write loop) — this session's router.

    GET    /api/drawings/{drawing_id}/intake?version=head|<n>
           -> {intake, version, head, latest}   (envelope-wrapped per §10)
    POST   /api/drawings/{drawing_id}/undo   -> {version, head, latest, intake}
    POST   /api/drawings/{drawing_id}/redo   -> {version, head, latest, intake}
    GET    /api/drawings/{drawing_id}/versions -> full version chain + checkout
    POST   /api/drawings/{drawing_id}/checkout {holder?, ttl_s?}
           -> take the single-writer lock (409 not-acquired if held by another)
    DELETE /api/drawings/{drawing_id}/checkout?holder=<h>
           -> release the lock (403 if the caller is not the holder)

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

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import deps
import write_loop
from envelopes import ErrorCode, error_obj, error_response, with_envelope_fields

router = APIRouter()
SUMMARY_LAYER_CAP = 50
CACHED_DEMO_DRAWING_ID = "rooftop_demo"


def _mutation_gate() -> Optional[JSONResponse]:
    if write_loop.drawing_mutations_enabled():
        return None
    return error_response(
        ErrorCode.INTERNAL,
        "drawing mutations are temporarily disabled for a storage cutover",
        retryable=True,
        status_code=503,
    )


def _backend(tenant_id: str = ""):
    """Per-tenant store selection (§19): guest tenants read/write the isolated
    guest store; every other tenant keeps the exact pre-§19 default backend."""
    return write_loop.backend_for_tenant(
        str(tenant_id), aps_live=False, da=None,
    )


@router.get("/api/drawings/{drawing_id}/intake")
def get_intake(drawing_id: str, version: str = "head",
               tenant_id: str = Depends(deps.require_tenant)) -> Dict[str, Any]:
    ver: Any = version
    if isinstance(version, str) and version not in ("head", "latest") and version.lstrip("-").isdigit():
        ver = int(version)
    try:
        view = write_loop.intake_view(str(tenant_id), drawing_id, ver, backend=_backend(str(tenant_id)))
    except (KeyError, ValueError) as exc:
        return error_response(ErrorCode.BAD_PARAMS, f"drawing/version unavailable: {exc}",
                              retryable=False, status_code=404)
    return with_envelope_fields(deps.tenant_echo(view, tenant_id))


@router.get("/api/drawings/{drawing_id}/summary")
def get_summary(drawing_id: str, version: str = "head",
                tenant_id: str = Depends(deps.require_tenant)) -> Dict[str, Any]:
    """Return a bounded drawing digest for assistant context.

    The intake endpoint remains the lossless geometry surface. This route keeps
    model-facing reads small by returning only version facts, an entity total,
    and the most populated layers.
    """
    ver: Any = version
    if isinstance(version, str) and version not in ("head", "latest") and version.lstrip("-").isdigit():
        ver = int(version)
    if deps.APS_LIVE and drawing_id == CACHED_DEMO_DRAWING_ID:
        # The public demo is the bundled, immutable intake used by the UI and
        # deterministic tools. Keep this read inside the app process so the
        # app never needs the broker-only APS credential just to summarize it.
        if ver not in ("head", "latest", 1):
            return error_response(
                ErrorCode.BAD_PARAMS,
                f"drawing/version unavailable: version {ver!r} does not exist",
                retryable=False,
                status_code=404,
            )
        view = {
            "intake": deps.load_cached_intake(),
            "version": 1,
            "head": 1,
            "latest": 1,
        }
    else:
        try:
            view = write_loop.intake_view(
                str(tenant_id), drawing_id, ver, backend=_backend(str(tenant_id))
            )
        except (KeyError, ValueError) as exc:
            return error_response(
                ErrorCode.BAD_PARAMS,
                f"drawing/version unavailable: {exc}",
                retryable=False,
                status_code=404,
            )

    layers: Dict[str, int] = {}
    total = 0
    intake = view.get("intake") or {}
    for key in ("polylines", "inserts", "faces3d"):
        for entity in intake.get(key) or []:
            layer = str(entity.get("layer", "0"))
            layers[layer] = layers.get(layer, 0) + 1
            total += 1
    ranked = sorted(layers.items(), key=lambda item: (-item[1], item[0]))
    layer_rows: list[Dict[str, Any]] = [
        {"name": name, "count": count}
        for name, count in ranked[:SUMMARY_LAYER_CAP]
    ]
    if len(ranked) > SUMMARY_LAYER_CAP:
        layer_rows.append({"more": len(ranked) - SUMMARY_LAYER_CAP})

    summary = {
        "drawing_id": drawing_id,
        "version": view.get("version"),
        "head": view.get("head"),
        "latest": view.get("latest"),
        "entity_total": total,
        "layers": layer_rows,
    }
    return with_envelope_fields(deps.tenant_echo(summary, tenant_id))


@router.post("/api/drawings/{drawing_id}/undo")
def undo(drawing_id: str, tenant_id: str = Depends(deps.require_tenant)) -> Dict[str, Any]:
    blocked = _mutation_gate()
    if blocked is not None:
        return blocked
    try:
        view = write_loop.undo_view(str(tenant_id), drawing_id, backend=_backend(str(tenant_id)))
    except (KeyError, ValueError) as exc:
        return error_response(ErrorCode.BAD_PARAMS, str(exc), retryable=False, status_code=400)
    return with_envelope_fields(deps.tenant_echo(view, tenant_id))


@router.post("/api/drawings/{drawing_id}/redo")
def redo(drawing_id: str, tenant_id: str = Depends(deps.require_tenant)) -> Dict[str, Any]:
    blocked = _mutation_gate()
    if blocked is not None:
        return blocked
    try:
        view = write_loop.redo_view(str(tenant_id), drawing_id, backend=_backend(str(tenant_id)))
    except (KeyError, ValueError) as exc:
        return error_response(ErrorCode.BAD_PARAMS, str(exc), retryable=False, status_code=400)
    return with_envelope_fields(deps.tenant_echo(view, tenant_id))


def _checkout_view(co: Any) -> Any:
    """Manifest `checkout` lock → the surface shape `{holder, acquired, expires}`
    or null. GET /versions renders this read-only ("someone else is editing" chip);
    the POST/DELETE .../checkout routes below let the holder TAKE and RELEASE it."""
    if not co:
        return None
    view = {"holder": co.get("holder"), "acquired": co.get("acquired"),
            "expires": co.get("expires")}
    if co.get("fence") is not None:
        view["fence"] = int(co["fence"])
    return view


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

    backend = _backend(str(tenant_id))
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


# --------------------------------------------------------------------------- #
# Single-writer checkout lock — the MUTATING half (§14)
#
# GET /versions surfaces the lock read-only (the "someone else is editing" chip).
# These two routes let a holder TAKE and RELEASE it, reusing the da/store.py
# primitives (acquire_checkout / release_checkout) unchanged — the lock lives in
# the SAME per-tenant manifest the versions/intake/undo routes read, and the
# demo drawing bootstraps identically (ensure_demo_drawing).
#
# SEMANTICS (from da/store.py, not reimplemented here):
#   * The lock is keyed per (tenant, drawing); `holder` is the session identity
#     (defaults to the requesting tenant/session). A DIFFERENT holder on the same
#     tenant+drawing conflicts — that is the single-writer guarantee. Two distinct
#     TENANTS have separate manifests and never collide (correct isolation).
#   * The same holder re-acquiring REFRESHES the lease (200, not a conflict).
#   * An EXPIRED lock (expires <= now) is free — re-acquirable by anyone.
#   * A read-tool run never consults this lock, so a held lock cannot block reads.
# --------------------------------------------------------------------------- #
DEFAULT_CHECKOUT_TTL_S = 3600            # 1h working lease (sane default)
MAX_CHECKOUT_TTL_S = 86_400             # 24h hard cap so a forgotten lock always expires


class CheckoutRequest(BaseModel):
    """Optional POST body for taking the lock. Both fields default: `holder` to the
    requesting tenant/session id, `ttl_s` to DEFAULT_CHECKOUT_TTL_S."""
    holder: Optional[str] = Field(default=None, max_length=200)
    ttl_s: Optional[float] = Field(default=None, gt=0, le=MAX_CHECKOUT_TTL_S)


@router.post("/api/drawings/{drawing_id}/checkout")
def acquire_checkout_route(drawing_id: str, req: Optional[CheckoutRequest] = None,
                           tenant_id: str = Depends(deps.require_tenant)) -> Any:
    """Take the single-writer lock for the requesting tenant/holder.

    §10-enveloped `{drawing_id, acquired: true, holder, checkout:{holder, acquired,
    expires}}` on success. When the lock is already held by ANOTHER active holder →
    HTTP 409 with a clear not-acquired body `{acquired: false, locked_by, checkout,
    error}` so the UI can render "locked by X". The same holder re-acquiring
    refreshes the lease (200). Same 404 pattern as the other routes for an unknown
    drawing (the well-known `demo` bootstraps on first use at APS_LIVE=0)."""
    blocked = _mutation_gate()
    if blocked is not None:
        return blocked
    import store  # da/store.py; importable via write_loop's sys.path setup

    backend = _backend(str(tenant_id))
    try:
        write_loop.ensure_demo_drawing(backend, str(tenant_id), drawing_id)
    except (KeyError, ValueError) as exc:
        return error_response(ErrorCode.BAD_PARAMS, f"drawing unavailable: {exc}",
                              retryable=False, status_code=404)

    holder = (req.holder if req else None) or str(tenant_id)
    ttl_s = (req.ttl_s if req and req.ttl_s is not None else None) or DEFAULT_CHECKOUT_TTL_S

    with write_loop.drawing_mutation_commit_guard() as commit_enabled:
        if not commit_enabled:
            return _mutation_gate() or error_response(
                ErrorCode.INTERNAL,
                "drawing mutations are temporarily disabled for a storage cutover",
                retryable=True,
                status_code=503,
            )
        acquired = store.acquire_checkout(
            backend, str(tenant_id), drawing_id, holder, ttl_s)
    m = store.load_manifest(backend, str(tenant_id), drawing_id)
    co = m.get("checkout")

    if not acquired:
        # Held by another active holder (store only refuses in that case; an expired
        # or same-holder lock would have returned True). Return the CURRENT lock so
        # the frontend can show who holds it, with a §10 error object for strict
        # consumers (BAD_PARAMS is the frozen-enum stand-in for a retryable conflict).
        cur = co or {}
        body = with_envelope_fields(deps.tenant_echo({
            "drawing_id": drawing_id,
            "acquired": False,
            "locked_by": cur.get("holder"),
            "checkout": _checkout_view(co),
            "error": error_obj(
                ErrorCode.BAD_PARAMS,
                f"checkout locked by {cur.get('holder')!r} until {cur.get('expires')}",
                retryable=True),
        }, tenant_id))
        return JSONResponse(status_code=409, content=body)

    view = deps.tenant_echo({
        "drawing_id": drawing_id,
        "acquired": True,
        "holder": holder,
        "checkout": _checkout_view(co),
    }, tenant_id)
    return with_envelope_fields(view)


@router.delete("/api/drawings/{drawing_id}/checkout")
def release_checkout_route(drawing_id: str, holder: Optional[str] = None,
                           tenant_id: str = Depends(deps.require_tenant)) -> Any:
    """Release the single-writer lock. Only the holder may release an ACTIVE lock
    (`holder` query param defaults to the requesting tenant/session id). A non-holder
    trying to release an active lock → HTTP 403 (the lock is left intact). Releasing
    when nothing is held (or the lock already expired) is an idempotent 200 with the
    cleared state. §10-enveloped `{drawing_id, released, checkout: null}`."""
    blocked = _mutation_gate()
    if blocked is not None:
        return blocked
    import store  # da/store.py; importable via write_loop's sys.path setup

    backend = _backend(str(tenant_id))
    try:
        write_loop.ensure_demo_drawing(backend, str(tenant_id), drawing_id)
        m = store.load_manifest(backend, str(tenant_id), drawing_id)
    except (KeyError, ValueError) as exc:
        return error_response(ErrorCode.BAD_PARAMS, f"drawing unavailable: {exc}",
                              retryable=False, status_code=404)

    co = m.get("checkout")
    who = holder or str(tenant_id)
    with write_loop.drawing_mutation_commit_guard() as commit_enabled:
        if not commit_enabled:
            return _mutation_gate() or error_response(
                ErrorCode.INTERNAL,
                "drawing mutations are temporarily disabled for a storage cutover",
                retryable=True,
                status_code=503,
            )
        released = store.release_checkout(
            backend, str(tenant_id), drawing_id, holder=who)

    if not released and co:
        # release_checkout returns False-with-a-lock-present ONLY when that lock is
        # ACTIVE and held by a DIFFERENT holder (an expired lock is releasable by
        # anyone → True). So this is exactly the not-the-holder case → 403, untouched.
        return error_response(
            ErrorCode.BAD_PARAMS,
            f"checkout held by {co.get('holder')!r}; only the holder can release it",
            retryable=False, status_code=403)

    m2 = store.load_manifest(backend, str(tenant_id), drawing_id)
    view = deps.tenant_echo({
        "drawing_id": drawing_id,
        "released": bool(released),
        "checkout": _checkout_view(m2.get("checkout")),
    }, tenant_id)
    return with_envelope_fields(view)
