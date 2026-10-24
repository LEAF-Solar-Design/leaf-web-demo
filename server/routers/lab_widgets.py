"""Build-entitled values reads behind the shared Lab admission rail."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import deps
import lab_artifacts as ledger
from lab_doc_validators import identifier
from lab_widget_values import resolve_values, validate_bindings
from routers.lab_artifacts import _admit, _body, _call

router = APIRouter(prefix="/api/lab/widgets", tags=["lab_widgets"])


def _resolve(tenant, doc_id, bindings):
    identifier(doc_id)
    validate_bindings(bindings)
    revision = ledger.get(tenant, doc_id)
    if revision["kind"] != "widget":
        return JSONResponse(status_code=409, content={"detail": "lab_document_not_widget"})
    return resolve_values(tenant, bindings, documents={doc_id: revision})


@router.post("/{doc_id}/values")
async def values(doc_id: str, request: Request, tenant=Depends(deps.require_tenant)):
    _admit(tenant, "build")
    body = await _body(request, ("bindings",), _document_errors=True)
    if isinstance(body, JSONResponse):
        return body
    return await run_in_threadpool(_call, _resolve, str(tenant), doc_id,
                                   body["bindings"], _document_errors=True)
