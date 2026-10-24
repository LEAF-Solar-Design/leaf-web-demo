"""Authenticated graph-only ledger HTTP surface."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import deps
import entitlements
import lab_artifacts as ledger
from lab_diff import structural_diff
from lab_doc_validators import InvalidGraph, MAX_RAW_BYTES, TooLarge, parse_body

router = APIRouter(prefix="/api/lab/docs", tags=["lab_artifacts"])


def _admit(tenant, capability):
    tier = entitlements.resolve_tier(tenant)
    roles, elevated = entitlements.resolve_roles(tenant)
    try:
        allowed = entitlements.entitlements_for(tier, roles, elevated).get(capability, False)
    except entitlements.EntitlementsError:
        raise HTTPException(503, detail="lab_policy_unavailable") from None
    if not allowed:
        raise HTTPException(403, detail="lab_entitlement_required")


def _actor(tenant):
    return str(getattr(tenant, "subject", None) or "tenant-member")


def _call(function, *args, _document_errors=False, **kwargs):
    try:
        return function(*args, **kwargs)
    except ledger.NotFound:
        raise HTTPException(404, detail="lab_document_not_found") from None
    except ledger.Conflict:
        raise HTTPException(409, detail="stale_expected_head") from None
    except TooLarge:
        raise HTTPException(413, detail="lab_capacity_exceeded") from None
    except InvalidGraph as error:
        if _document_errors:
            return JSONResponse(status_code=422, content={
                "detail": error.code, "pointer": error.pointer, "block": error.block,
            })
        raise HTTPException(422, detail="invalid_lab_document") from None
    except Exception:
        # DB and filesystem errors must not reveal configuration or host values.
        raise HTTPException(503, detail="lab_store_unavailable") from None


async def _body(request, required, *, _document_errors=False):
    length = request.headers.get("content-length")
    if length is not None:
        try:
            size = int(length)
        except ValueError:
            raise HTTPException(422, detail="invalid_content_length") from None
        if size < 0:
            raise HTTPException(422, detail="invalid_content_length")
        if size > MAX_RAW_BYTES:
            raise HTTPException(413, detail="lab_body_too_large")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_RAW_BYTES:
            raise HTTPException(413, detail="lab_body_too_large")
        data.extend(chunk)
    body = _call(parse_body, bytes(data), _document_errors=_document_errors)
    if isinstance(body, JSONResponse):
        return body
    if set(body) != set(required):
        raise HTTPException(422, detail="invalid_lab_fields")
    return body


@router.post("/{doc_id}/save")
async def save(doc_id: str, request: Request, tenant=Depends(deps.require_tenant)):
    _admit(tenant, "build")
    body = await _body(request, ("graph", "expected_head"), _document_errors=True)
    if isinstance(body, JSONResponse):
        return body
    return await run_in_threadpool(_call, ledger.save, str(tenant), doc_id, body["graph"], body["expected_head"], _actor(tenant), _document_errors=True)


@router.get("/{doc_id}/head")
def head(doc_id: str, tenant=Depends(deps.require_tenant)):
    _admit(tenant, "build")
    return _call(ledger.get, str(tenant), doc_id)


@router.get("/{doc_id}/revisions")
def revisions(doc_id: str, limit: int = 100, before: int | None = None,
              tenant=Depends(deps.require_tenant)):
    _admit(tenant, "build")
    return {"revisions": _call(ledger.list_revisions, str(tenant), doc_id, limit=limit, before=before)}


@router.get("/{doc_id}/revisions/{revision}")
def revision(doc_id: str, revision: int, tenant=Depends(deps.require_tenant)):
    _admit(tenant, "build")
    return _call(ledger.get, str(tenant), doc_id, revision)


@router.get("/{doc_id}/diff")
def diff(doc_id: str, before: int, after: int, tenant=Depends(deps.require_tenant)):
    _admit(tenant, "build")
    left = _call(ledger.get, str(tenant), doc_id, before)
    right = _call(ledger.get, str(tenant), doc_id, after)
    return {"before": before, "after": after,
            "changes": structural_diff(left["graph"], right["graph"])}


@router.post("/{doc_id}/restore")
async def restore(doc_id: str, request: Request, tenant=Depends(deps.require_tenant)):
    _admit(tenant, "build")
    body = await _body(request, ("revision", "expected_head"), _document_errors=True)
    if isinstance(body, JSONResponse):
        return body
    return await run_in_threadpool(_call, ledger.restore, str(tenant), doc_id, body["revision"], body["expected_head"], _actor(tenant), _document_errors=True)


@router.post("/{doc_id}/publish")
async def publish(doc_id: str, request: Request, tenant=Depends(deps.require_tenant)):
    _admit(tenant, "build")
    _admit(tenant, "deploy")
    body = await _body(request, ("expected_head",))
    return await run_in_threadpool(_call, ledger.publish, str(tenant), doc_id, body["expected_head"], _actor(tenant))
