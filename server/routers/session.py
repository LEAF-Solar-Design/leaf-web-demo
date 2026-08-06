"""GET /api/session — Intake JSON (contract section 1)."""
from __future__ import annotations

import requests
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

import broker_client
import deps
import drawing_identity
from envelopes import ErrorCode, error_response, with_envelope_fields

router = APIRouter()


def _stored_drawing_intake(tenant: str, drawing_id: str):
    """Read an existing tenant drawing without crossing the APS boundary.

    Account uploads are already extracted into the credential-free drawing
    store before the browser opens them.  A missing manifest means this is a
    curated library drawing, so the live route may continue to the broker.
    """
    import store  # noqa: PLC0415 - lazy store boundary
    import write_loop  # noqa: PLC0415 - lazy store boundary

    backend = write_loop.upload_backend_for_tenant(str(tenant))
    try:
        store.load_manifest(backend, str(tenant), drawing_id)
    except KeyError:
        return None
    _, intake = write_loop.read_intake(
        backend, str(tenant), drawing_id, "head")
    return intake


@router.get("/api/session")
def session(dwg: str = "rooftop_demo", tenant=Depends(deps.require_active_tenant)):
    """Return the Intake JSON (contract section 1). APS_LIVE=0 -> cached sample.

    AUTH (LEAF_AUTH_LIVE): `require_active_tenant` is a no-op passthrough when off, so
    the body is byte-identical to today. When on, it enforces the Bearer token
    and resolves the current platform identity binding before any drawing read.
    The success body additively echoes that canonical `tenant_id`/`org_id`.

    APS_LIVE=1 extraction crosses the same internal broker boundary as tool
    runs. The app process never calls the APS client or reads its credential.
    """
    identity = drawing_identity.curated_identity(dwg)
    store_dwg = drawing_identity.store_id(dwg)
    source_dwg = drawing_identity.source_id(dwg)
    if deps.APS_LIVE:
        import write_loop  # noqa: PLC0415 - lazy, keeps module import surface

        try:
            intake = _stored_drawing_intake(str(tenant), store_dwg)
        except write_loop.ProofStateUnreadable as exc:
            return error_response(
                ErrorCode.INTERNAL, str(exc), retryable=True,
                status_code=503)
        except (KeyError, ValueError) as exc:
            return error_response(
                ErrorCode.INTERNAL,
                f"stored drawing is unreadable: {exc}",
                retryable=False,
                status_code=500,
            )
        if intake is not None:
            return deps.tenant_echo(
                with_envelope_fields({"intake": intake}), tenant)
        try:
            response = requests.post(
                f"{broker_client.broker_url()}/broker/extract",
                json={
                    "tenant_id": str(tenant),
                    "dwg": source_dwg,
                    "ledger_event_key": broker_client.extract_event_key(
                        str(tenant), source_dwg),
                },
                headers=broker_client.broker_headers(),
                timeout=600,
            )
            body = response.json()
        except (requests.ConnectionError, requests.Timeout, ValueError) as exc:
            return error_response(
                ErrorCode.BROKER_UNREACHABLE,
                f"APS broker extraction failed: {exc}",
                retryable=True,
                status_code=502,
            )
        if response.status_code >= 400:
            return JSONResponse(status_code=response.status_code, content=body)
        return deps.tenant_echo(body, tenant)
    if identity is None:
        # §19 (review round 1, BLOCKER): this offline branch used to IGNORE
        # `dwg` and serve the cached DEMO intake for ANY name — for an
        # uploaded drawing id that is fabricated data labeled as the user's
        # own. A non-default dwg now reads the tenant's own store (real
        # geometry for an extracted upload; ensure_demo_drawing's fail-closed
        # guards -> honest 404 for anything unextracted/unknown).
        import write_loop  # noqa: PLC0415 - lazy, keeps module import surface

        backend = write_loop.backend_for_tenant(str(tenant), aps_live=False)
        try:
            write_loop.ensure_demo_drawing(backend, str(tenant), dwg)
            _, intake = write_loop.read_intake(backend, str(tenant), dwg, "head")
        except write_loop.ProofStateUnreadable as exc:
            # Transport-unreadable proof state: the drawing exists; retryable.
            return error_response(ErrorCode.INTERNAL, str(exc),
                                  retryable=True, status_code=503)
        except (KeyError, ValueError) as exc:
            return error_response(ErrorCode.BAD_PARAMS, f"drawing unavailable: {exc}",
                                  retryable=False, status_code=404)
        return deps.tenant_echo(with_envelope_fields({"intake": intake}), tenant)
    if not deps.DATA_FILE.exists():
        return error_response(ErrorCode.INTERNAL, f"cached intake not found: {deps.DATA_FILE}",
                              retryable=False, status_code=404)
    return deps.tenant_echo(with_envelope_fields({"intake": deps.load_cached_intake()}), tenant)
