"""Canonical estimate save, reopen and immutable JSON export routes."""
import json
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response
import deps
import project_estimate_custody as custody
from envelopes import ErrorCode, err_envelope
from solar_design_graph import GraphValidationError

_ERRORS = frozenset(("ESTIMATE_REQUEST_INVALID", "ESTIMATE_MEDIA_TYPE_REFUSED",
    "ESTIMATE_REQUEST_TOO_LARGE", "ESTIMATE_NOT_SAVED", "ESTIMATE_SNAPSHOT_INVALID",
    "ESTIMATE_SOURCE_CHANGED", "ESTIMATE_SOURCE_MISMATCH", "ESTIMATE_INTERNAL",
    "ESTIMATE_LIMIT_EXCEEDED", "ESTIMATE_INPUT_INVALID", "ESTIMATE_SOURCE_INVALID",
    "ESTIMATE_TAKEOFF_INVALID", "ESTIMATE_RATE_CARD_INVALID", "ESTIMATE_SCHEMA_INVALID",
    "ESTIMATE_DIGEST_INVALID", "ESTIMATE_DIGEST_MISMATCH", "ESTIMATE_MATERIAL_MISMATCH",
    "ESTIMATE_DUPLICATE_KEY"))


def _failure(reason):
    reason = reason if reason in _ERRORS else "ESTIMATE_INTERNAL"
    status = {"ESTIMATE_MEDIA_TYPE_REFUSED": 415, "ESTIMATE_REQUEST_TOO_LARGE": 413,
              "ESTIMATE_SOURCE_CHANGED": 409, "ESTIMATE_NOT_SAVED": 404,
              "ESTIMATE_INTERNAL": 500}.get(reason, 400)
    body = err_envelope(ErrorCode.INTERNAL if status == 500 else ErrorCode.BAD_PARAMS,
                        "Project estimate operation unavailable.", False)
    body["error"]["reason_code"] = reason
    return JSONResponse(content=body, status_code=status, headers=parent._NO_STORE)


class EstimateRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def handle(request):
            try:
                response = await handler(request)
            except HTTPException as exc:
                response = JSONResponse(content=err_envelope(
                    ErrorCode.UNAUTHENTICATED if exc.status_code == 401 else ErrorCode.FORBIDDEN,
                    "Project estimate access refused.", False), status_code=exc.status_code,
                    headers={key: value for key, value in (exc.headers or {}).items()
                             if key.lower() == "www-authenticate"})
            except RequestValidationError:
                response = _failure("ESTIMATE_REQUEST_INVALID")
            except Exception:
                response = _failure("ESTIMATE_INTERNAL")
            response.headers.update(parent._NO_STORE)
            return response
        return handle


router = APIRouter(route_class=EstimateRoute)
_PATH = "/api/projects/{project_id}/drawings/{drawing_id}/versions/{version_id}/estimates"


def _dispatch(operation):
    try:
        result = operation()
        if isinstance(result, Response):
            result.headers.update(parent._NO_STORE)
            return result
        return JSONResponse(content=result, headers=parent._NO_STORE)
    except (parent.service.ProjectContextError, parent.drawing_editor.EditorError) as exc:
        def refused():
            raise exc
        return parent._editor_dispatch(refused)
    except GraphValidationError as exc:
        if exc.code in parent._OUTPUT_FAILURES:
            return parent._output_failure(exc.code)
        return _failure(exc.code)
    except Exception:
        return _failure("ESTIMATE_INTERNAL")


async def _body(request):
    if not custody.policy.project_runs_enabled():
        raise GraphValidationError("project_execution_disabled")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise GraphValidationError("ESTIMATE_MEDIA_TYPE_REFUSED")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 262144:
            raise GraphValidationError("ESTIMATE_REQUEST_TOO_LARGE")
        raw.extend(chunk)
    try:
        body = json.loads(bytes(raw).decode("utf-8"), object_pairs_hook=parent.service._object_pairs,
            parse_constant=parent.service._nonfinite, parse_float=parent.service._json_float)
        if (type(body) is not dict or set(body) != {"rate_card", "catalog_digest"}
                or type(body["rate_card"]) is not dict or type(body["catalog_digest"]) is not str
                or not body["catalog_digest"]):
            raise ValueError()
        return body
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise GraphValidationError("ESTIMATE_REQUEST_INVALID") from None


@router.post(_PATH)
async def save_route(project_id: str, drawing_id: str, version_id: str, request: Request,
        tenant=Depends(deps.require_active_tenant), x_org_id: Optional[str] = Header(default=None),
        x_project_id: Optional[str] = Header(default=None),
        x_checkout_capability: Optional[str] = Header(default=None),
        idempotency_key: Optional[str] = Header(default=None)):
    try:
        proj, drawing, version = parent._output_scope(tenant, project_id, drawing_id,
            x_org_id, x_project_id, version_id)
        parent.service._access(tenant, proj, write=True)
        request_id = parent.drawing_editor.request_uuid(idempotency_key)
        body = await _body(request)
    except (GraphValidationError, parent.service.ProjectContextError,
            parent.drawing_editor.EditorError) as exc:
        def refused():
            raise exc
        return _dispatch(refused)
    return await run_in_threadpool(_dispatch, lambda: custody.save_estimate(
        tenant, proj, drawing, version, body["rate_card"], catalog_digest=body["catalog_digest"],
        request_id=request_id, capability=x_checkout_capability))


def _read(tenant, project_id, drawing_id, version_id, org, project, *, export):
    proj, drawing, version = parent._output_scope(tenant, project_id, drawing_id, org, project, version_id)
    result = custody.read_estimate(tenant, proj, drawing, version)
    if isinstance(result, Response) or not export:
        return result
    result = result["result"]
    if not result["saved"]:
        raise GraphValidationError("ESTIMATE_NOT_SAVED")
    return Response(content=custody.material.encode_estimate(result["estimate"]),
        media_type="application/json", headers={**parent._NO_STORE,
            "Content-Disposition": 'attachment; filename="estimate.json"',
            "ETag": '"' + result["content_sha256"] + '"', "X-Content-Type-Options": "nosniff"})


@router.get(_PATH)
def read_route(project_id: str, drawing_id: str, version_id: str,
        tenant=Depends(deps.require_active_tenant), x_org_id: Optional[str] = Header(default=None),
        x_project_id: Optional[str] = Header(default=None)):
    return _dispatch(lambda: _read(tenant, project_id, drawing_id, version_id,
                                  x_org_id, x_project_id, export=False))


@router.get(_PATH + "/export")
def export_route(project_id: str, drawing_id: str, version_id: str,
        tenant=Depends(deps.require_active_tenant), x_org_id: Optional[str] = Header(default=None),
        x_project_id: Optional[str] = Header(default=None)):
    return _dispatch(lambda: _read(tenant, project_id, drawing_id, version_id,
                                  x_org_id, x_project_id, export=True))


from routers import project_drawings as parent
