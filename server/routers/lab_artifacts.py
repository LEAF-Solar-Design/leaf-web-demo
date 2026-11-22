"""Authenticated graph-only ledger HTTP surface."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

import deps
import entitlements
import lab_artifacts as ledger
import lab_layouts
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
    except lab_layouts.LayoutRefused as error:
        return _layout_error(error)
    except ledger.NotFound:
        raise HTTPException(404, detail="lab_document_not_found") from None
    except ledger.ExportForbidden:
        raise HTTPException(403, detail="lab_export_forbidden") from None
    except ledger.KindFixed:
        raise HTTPException(409, detail="lab_document_kind_fixed") from None
    except ledger.GitConflict:
        raise HTTPException(409, detail="lab_artifact_conflict") from None
    except ledger.GitUnavailable as error:
        code = str(error)
        if code not in {'lab_store_migrating', 'lab_harness_capability_missing', 'lab_commit_pending'}:
            code = 'lab_store_unavailable'
        raise HTTPException(503, detail=code) from None
    except ledger.Conflict:
        raise HTTPException(409, detail="stale_expected_head") from None
    except ledger.StoreMigrating:
        raise HTTPException(409, detail="lab_store_migrating") from None
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


async def _body(request, required, *, _document_errors=False, optional=()):
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
    if not set(required) <= set(body) or set(body) - set(required) - set(optional):
        raise HTTPException(422, detail="invalid_lab_fields")
    return body


@router.post("/{doc_id}/save")
async def save(doc_id: str, request: Request, tenant=Depends(deps.require_tenant)):
    _admit(tenant, "build")
    body = await _body(request, ("graph", "expected_head"), _document_errors=True, optional=("kind",))
    if isinstance(body, JSONResponse):
        return body
    return await run_in_threadpool(_call, ledger.save, str(tenant), doc_id, body["graph"], body["expected_head"], _actor(tenant), kind=body.get("kind", "graph"), _document_errors=True)


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


def _authorize_export(tenant, graph, run):
    # Use the same live identity binding and project role boundary as run
    # admission, even when the revision has never been run.
    from routers.lab_runs import _authorize_corpus_run, _corpus_actor
    from lab_evaluate import GraphRefused

    try:
        if run is not None:
            _authorize_corpus_run(tenant, run)
        for block in graph["blocks"]:
            settings = block["settings"]
            drawing = settings.get("drawing")
            project = settings.get("project_id") or (drawing.get("project_id") if isinstance(drawing, dict) else None)
            if project is None:
                continue
            actor = _corpus_actor(tenant)
            from leaf_platform import project_lifecycle, project_reference_materials
            project_lifecycle.require_project_role(str(tenant), project, actor, write=False)
            if block["registry"]["id"] == "corpus-evaluator":
                for key in ("corpus", "suite"):
                    descriptor = settings[key]
                    material = project_reference_materials.get_material(
                        str(tenant), project, actor, descriptor["material_id"])
                    if (material["org_id"] != str(tenant) or material["project_id"] != project
                            or any(material[field] != descriptor[field]
                                   for field in ("material_id", "sha256", "byte_size"))):
                        raise ledger.ExportForbidden()
    except GraphRefused:
        raise ledger.ExportForbidden() from None
    except Exception as error:
        # Project metadata uses a fail-closed boundary, just as run admission.
        if isinstance(error, ledger.ExportForbidden):
            raise
        raise ledger.ExportForbidden() from None


@router.get("/{doc_id}/revisions/{revision}/export")
def export_revision(doc_id: str, revision: int, tenant=Depends(deps.require_tenant)):
    _admit(tenant, "build")
    bundle = _call(ledger.export_bundle, str(tenant), doc_id, revision,
                   authorize=lambda graph, run: _authorize_export(tenant, graph, run))
    _admit(tenant, "build")
    return JSONResponse(bundle, headers={
        "Content-Disposition": f'attachment; filename="lab-{doc_id}-r{revision}.json"',
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
    })


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


def _layout_error(error):
    return JSONResponse({"code": error.code}, status_code=413 if error.code == "lab_layout_too_large" else 400,
                        headers={"Cache-Control": "no-store"})


@router.get("/{doc_id}/layout/{sha256}")
def get_layout(doc_id: str, sha256: str, tenant=Depends(deps.require_tenant)):
    _admit(tenant, "build")
    layout = _call(lab_layouts.get_layout, str(tenant), doc_id, sha256)
    return layout if isinstance(layout, JSONResponse) else JSONResponse(
        {"layout": layout}, headers={"Cache-Control": "no-store"})


@router.put("/{doc_id}/layout/{sha256}")
async def put_layout(doc_id: str, sha256: str, request: Request, tenant=Depends(deps.require_tenant)):
    _admit(tenant, "build")
    try:
        length = request.headers.get("content-length")
        if length is not None:
            size = int(length)
            if size < 0:
                raise lab_layouts.LayoutRefused()
            if size > lab_layouts.MAX_LAYOUT_BYTES:
                raise lab_layouts.LayoutRefused("lab_layout_too_large")
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > lab_layouts.MAX_LAYOUT_BYTES:
                raise lab_layouts.LayoutRefused("lab_layout_too_large")
            data.extend(chunk)
        body = parse_body(bytes(data))
    except lab_layouts.LayoutRefused as error:
        return _layout_error(error)
    except (ValueError, UnicodeError):
        return _layout_error(lab_layouts.LayoutRefused())
    result = await run_in_threadpool(_call, lab_layouts.put_layout, str(tenant), doc_id, sha256, body)
    return result if isinstance(result, JSONResponse) else Response(status_code=204, headers={"Cache-Control": "no-store"})
