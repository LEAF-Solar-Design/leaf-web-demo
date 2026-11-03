"""Authenticated canonical project drawing context and checkout endpoints."""
from __future__ import annotations

import math
import re
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

import deps
from demo_actor import DemoActorRefused, is_demo_context, resolve_demo_actor, require_demo_project
import solar_project_context as service
from envelopes import ErrorCode, err_envelope, with_envelope_fields

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}
_PROJECT_DRAWING_PATH = re.compile(
    r"^/api/projects/[^/]+/(?:solar-layout|drawing-versions/[^/]+/context|drawings/[^/]+/checkout|"
    r"drawings/[^/]+/versions/[^/]+/(?:solar-reads|imports/solaredge-pdf(?:/report)?|editor(?:/restore)?|dxf|solar-layout)|"
    r"drawings/[^/]+/solar-artifacts/[^/]+)/?$")
_FAILURES = {
    "demo_actor_refused": (403, False),
    "SIP_R1_INVALID_BINDING": (400, False), "SIP_R1_CONTEXT_NOT_FOUND": (404, False),
    "SIP_R1_PROJECT_FORBIDDEN": (403, False), "SIP_R1_CANONICAL_AUTHORITY_REQUIRED": (409, False),
    "SIP_R1_INTAKE_PROOF_REQUIRED": (409, False), "SIP_R1_INTAKE_REFERENCE_INVALID": (500, False),
    "SIP_R1_INTAKE_DIGEST_MISMATCH": (500, False), "SIP_R1_INTAKE_INVALID": (500, False),
    "SIP_R1_INTAKE_UNAVAILABLE": (503, True), "SIP_R1_STORE_UNAVAILABLE": (503, True),
    "SIP_R1_CHECKOUT_PARAMS_INVALID": (400, False), "SIP_R1_CHECKOUT_CONFLICT": (409, True),
    "SIP_R1_CHECKOUT_REQUIRED": (409, False), "SIP_R1_CHECKOUT_EXPIRED": (409, False),
    "SIP_R1_CHECKOUT_DENIED": (403, False), "SIP_R1_CHECKOUT_STALE": (409, False),
    "SIP_R1_CHECKOUT_UNAVAILABLE": (503, True), "SIP_R1_FENCE_EXHAUSTED": (409, False),
    "SIP_R1_WRITES_DRAINED": (503, True), "SIP_R1_STALE_VERSION": (409, False), "SIP_R1_INTERNAL": (500, False),
    "SIP_R2_PUBLICATION_PARAMS_INVALID": (400, False),
    "SIP_R2_IDEMPOTENCY_CONFLICT": (409, False),
    "SIP_R2_PUBLICATION_REQUIRED": (409, False),
    "PROJECT_LAYOUT_INITIAL_REQUIRES_EMPTY_PROJECT": (409, False),
}


async def no_store_responses(request: Request, call_next):
    """Keep authentication and validation responses on these paths uncached too."""
    response = await call_next(request)
    if _PROJECT_DRAWING_PATH.fullmatch(request.url.path):
        response.headers["Cache-Control"] = "no-store"
    return response


class CheckoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    holder: Optional[StrictStr] = Field(default=None, min_length=1, max_length=200)
    ttl_s: float = Field(default=3600, gt=0, le=86400, allow_inf_nan=False)

    @field_validator("ttl_s", mode="before")
    @classmethod
    def numeric_ttl(cls, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("TTL must be numeric")
        try:
            if not math.isfinite(value):
                raise ValueError("TTL must be finite")
        except OverflowError:
            raise ValueError("TTL must be finite") from None
        return value


def _headers(tenant, project_id, org_header, project_header):
    try:
        actor = resolve_demo_actor(tenant)
        if actor is not None:
            require_demo_project(actor, project_id)
            if ((org_header is not None and str(org_header) != str(actor.org_id))
                    or (project_header is not None and project_header != actor.project_id)):
                raise DemoActorRefused()
            return
    except DemoActorRefused:
        raise service.ProjectContextError("demo_actor_refused") from None
    except Exception:
        service.refuse("STORE_UNAVAILABLE")
    if (org_header is not None and str(org_header) != str(tenant)) or (
            project_header is not None and project_header != project_id):
        service.refuse("CONTEXT_NOT_FOUND")


def _identity(tenant, project_id, drawing_id):
    try:
        actor = resolve_demo_actor(tenant)
    except DemoActorRefused:
        raise service.ProjectContextError("demo_actor_refused") from None
    except Exception:
        if is_demo_context(tenant):
            service.refuse("STORE_UNAVAILABLE")
        raise
    return {"organization_id": str(actor.org_id if actor is not None else tenant), "project_id": str(project_id),
            "drawing_id": str(drawing_id)}


def _dispatch(operation):
    try:
        return JSONResponse(content=with_envelope_fields(operation()), headers=_NO_STORE)
    except service.ProjectContextError as exc:
        reason = exc.reason_code
        if reason not in _FAILURES:
            reason = "SIP_R1_INTERNAL"
        status, retry = _FAILURES[reason]
        code = ErrorCode.FORBIDDEN if status == 403 else (
            ErrorCode.INTERNAL if status >= 500 else ErrorCode.BAD_PARAMS)
        body = err_envelope(code, "Project drawing operation unavailable.", retry)
        body["error"]["reason_code"] = reason
        if reason == "SIP_R1_CHECKOUT_CONFLICT":
            body.update(acquired=False, locked_by=exc.checkout.holder if exc.checkout else None,
                        checkout=service.public_checkout(exc.checkout))
        return JSONResponse(content=body, status_code=status, headers=_NO_STORE)
    except Exception:
        return _dispatch(lambda: service.refuse("INTERNAL"))


@router.get("/api/projects/{project_id}/drawing-versions/{input_version_id}/context")
def context_route(project_id: UUID, input_version_id: UUID, drawing_id: Optional[UUID] = None,
                  tenant=Depends(deps.require_active_tenant),
                  x_org_id: Optional[UUID] = Header(default=None),
                  x_project_id: Optional[UUID] = Header(default=None)):
    def operation():
        _headers(tenant, project_id, x_org_id, x_project_id)
        context = service.resolve_context(tenant, project_id, input_version_id, drawing_id=drawing_id)
        binding = context.binding
        identity = _identity(binding.organization_id, binding.project_id, binding.drawing_id)
        identity.update(input_version_id=str(binding.input_version_id),
                        head_version_id=str(binding.head_version_id), is_head=binding.is_head,
                        intake_sha256=context.intake_sha256)
        return {"context": identity, "intake": context.intake}
    return _dispatch(operation)


@router.get("/api/projects/{project_id}/drawings/{drawing_id}/checkout")
def status_route(project_id: UUID, drawing_id: UUID, tenant=Depends(deps.require_active_tenant),
                 x_org_id: Optional[UUID] = Header(default=None),
                 x_project_id: Optional[UUID] = Header(default=None)):
    def operation():
        _headers(tenant, project_id, x_org_id, x_project_id)
        state = service.checkout_status(tenant, project_id, drawing_id)
        lease = state.checkout
        if lease is not None and lease.expires_at <= state.observed_at:
            lease = None
        return {**_identity(state.organization_id, project_id, drawing_id),
                "checkout": service.public_checkout(lease)}
    return _dispatch(operation)


@router.post("/api/projects/{project_id}/drawings/{drawing_id}/checkout")
def acquire_route(project_id: UUID, drawing_id: UUID, req: Optional[CheckoutRequest] = None,
                  tenant=Depends(deps.require_active_tenant),
                  x_org_id: Optional[UUID] = Header(default=None),
                  x_project_id: Optional[UUID] = Header(default=None),
                  x_checkout_capability: Optional[str] = Header(default=None)):
    def operation():
        _headers(tenant, project_id, x_org_id, x_project_id)
        request = req or CheckoutRequest()
        lease, token = service.acquire_project_checkout(tenant, project_id, drawing_id,
            holder=request.holder, ttl_s=request.ttl_s, checkout_capability_token=x_checkout_capability)
        return {**_identity(lease.organization_id, project_id, drawing_id), "acquired": True,
                "checkout": service.public_checkout(lease), "checkout_capability": token}
    return _dispatch(operation)


async def _no_delete_body(request: Request):
    if await request.body():
        raise HTTPException(status_code=422, detail="Release accepts no body", headers=_NO_STORE)


@router.delete("/api/projects/{project_id}/drawings/{drawing_id}/checkout",
               dependencies=[Depends(_no_delete_body)])
def release_route(project_id: UUID, drawing_id: UUID, tenant=Depends(deps.require_active_tenant),
                  x_org_id: Optional[UUID] = Header(default=None),
                  x_project_id: Optional[UUID] = Header(default=None),
                  x_checkout_capability: Optional[str] = Header(default=None)):
    def operation():
        _headers(tenant, project_id, x_org_id, x_project_id)
        released = service.release_project_checkout(tenant, project_id, drawing_id,
            checkout_capability_token=x_checkout_capability)
        return {**_identity(tenant, project_id, drawing_id), "released": released, "checkout": None}
    return _dispatch(operation)


# Browser edits publish both immutable DXF bytes and their derived intake in
# the canonical project chain. These never enter the legacy drawing store.
import project_drawing_editor as drawing_editor


def _editor_dispatch(operation):
    try:
        result = operation()
        return result if isinstance(result, Response) else JSONResponse(
            content=with_envelope_fields(result), headers=_NO_STORE)
    except drawing_editor.EditorError as exc:
        body = err_envelope(ErrorCode.INTERNAL if exc.status >= 500 else ErrorCode.BAD_PARAMS,
                            "Project drawing edit unavailable.", exc.retryable)
        body["error"]["reason_code"] = exc.code
        return JSONResponse(content=body, status_code=exc.status, headers=_NO_STORE)
    except service.ProjectContextError as exc:
        def refused():
            raise exc
        return _dispatch(refused)
    except Exception:
        return _dispatch(lambda: service.refuse("INTERNAL"))


async def _editor_request(request, *, restore=False, layout=False, initial=False):
    expected = "application/json" if restore or layout or initial else "application/dxf"
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != expected:
        raise drawing_editor.EditorError("PROJECT_EDITOR_MEDIA_TYPE_REFUSED", 415)
    maximum = 2 * 1024 * 1024 if initial else (4096 if restore or layout else drawing_editor.MAX_DXF_BYTES)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > maximum:
        raise drawing_editor.EditorError("PROJECT_EDITOR_TOO_LARGE", 413)
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > maximum:
            raise drawing_editor.EditorError("PROJECT_EDITOR_TOO_LARGE", 413)
        raw.extend(chunk)
    if not restore and not layout and not initial:
        return bytes(raw)
    try:
        import json
        body = json.loads(bytes(raw).decode("utf-8"), object_pairs_hook=service._object_pairs,
                          parse_constant=service._nonfinite)
        if layout or initial:
            import project_solar_materialization
            return (project_solar_materialization.initial_parameters(body) if initial else
                    project_solar_materialization.parameters(body))
        if type(body) is not dict or set(body) != {"version_id"}:
            raise ValueError()
        return drawing_editor.request_uuid(body["version_id"])
    except drawing_editor.EditorError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise drawing_editor.EditorError("PROJECT_EDITOR_REQUEST_INVALID") from None


@router.get("/api/projects/{project_id}/drawings/{drawing_id}/versions/{version_id}/dxf")
def project_editor_source_route(project_id: UUID, drawing_id: UUID, version_id: UUID,
        tenant=Depends(deps.require_active_tenant),
        x_org_id: Optional[UUID] = Header(default=None),
        x_project_id: Optional[UUID] = Header(default=None)):
    def operation():
        _headers(tenant, project_id, x_org_id, x_project_id)
        raw, digest = drawing_editor.read_dxf(tenant, project_id, drawing_id, version_id)
        return Response(content=raw, media_type="application/dxf", headers={**_NO_STORE,
            "X-Content-Type-Options": "nosniff", "X-Leaf-Source-Sha256": digest,
            "Content-Disposition": 'attachment; filename="drawing.dxf"'})
    return _editor_dispatch(operation)


async def _project_editor_write(request, tenant, project_id, drawing_id, parent_version_id,
                                org_header, project_header, capability, idempotency_key, *, restore):
    try:
        _headers(tenant, project_id, org_header, project_header)
        # Authorize before reading/parsing up to16MiB from an untrusted caller.
        service._access(tenant, project_id, write=True)
        request_id = drawing_editor.request_uuid(idempotency_key)
        body = await _editor_request(request, restore=restore)
    except (service.ProjectContextError, drawing_editor.EditorError) as exc:
        def refused():
            raise exc
        return _editor_dispatch(refused)
    operation = drawing_editor.restore_dxf if restore else drawing_editor.save_dxf
    return await run_in_threadpool(_editor_dispatch, lambda: operation(
        tenant, project_id, drawing_id, parent_version_id, body,
        request_id=request_id, capability=capability))


@router.get("/api/projects/{project_id}/solar-layout")
def project_initial_solar_layout_source_route(project_id: UUID,
        tenant=Depends(deps.require_active_tenant),
        x_org_id: Optional[UUID] = Header(default=None), x_project_id: Optional[UUID] = Header(default=None)):
    import project_solar_materialization as layout
    def operation():
        _headers(tenant, project_id, x_org_id, x_project_id)
        return layout.initial_layout_source(tenant, project_id)
    return _editor_dispatch(operation)


@router.post("/api/projects/{project_id}/solar-layout")
async def project_initial_solar_layout_route(project_id: UUID, request: Request,
        tenant=Depends(deps.require_active_tenant),
        x_org_id: Optional[UUID] = Header(default=None), x_project_id: Optional[UUID] = Header(default=None),
        idempotency_key: Optional[str] = Header(default=None)):
    import project_solar_materialization as layout
    try:
        _headers(tenant, project_id, x_org_id, x_project_id)
        service._access(tenant, project_id, write=True)
        request_id = drawing_editor.request_uuid(idempotency_key)
        body = await _editor_request(request, initial=True)
    except (service.ProjectContextError, drawing_editor.EditorError) as exc:
        def refused():
            raise exc
        return _editor_dispatch(refused)
    return await run_in_threadpool(_editor_dispatch, lambda: layout.publish_initial_layout(
        tenant, project_id, body, request_id=request_id))


@router.get("/api/projects/{project_id}/drawings/{drawing_id}/versions/{parent_version_id}/solar-layout")
def project_solar_layout_source_route(project_id: UUID, drawing_id: UUID, parent_version_id: UUID,
        tenant=Depends(deps.require_active_tenant),
        x_org_id: Optional[UUID] = Header(default=None), x_project_id: Optional[UUID] = Header(default=None)):
    import project_solar_materialization as layout
    def operation():
        _headers(tenant, project_id, x_org_id, x_project_id)
        return layout.current_layout_source(tenant, project_id, drawing_id, parent_version_id)
    return _editor_dispatch(operation)


@router.post("/api/projects/{project_id}/drawings/{drawing_id}/versions/{parent_version_id}/solar-layout")
async def project_solar_layout_route(project_id: UUID, drawing_id: UUID, parent_version_id: UUID,
        request: Request, tenant=Depends(deps.require_active_tenant),
        x_org_id: Optional[UUID] = Header(default=None), x_project_id: Optional[UUID] = Header(default=None),
        x_checkout_capability: Optional[str] = Header(default=None),
        idempotency_key: Optional[str] = Header(default=None)):
    import project_solar_materialization as layout
    try:
        _headers(tenant, project_id, x_org_id, x_project_id)
        service._access(tenant, project_id, write=True)
        request_id = drawing_editor.request_uuid(idempotency_key)
        body = await _editor_request(request, layout=True)
    except (service.ProjectContextError, drawing_editor.EditorError) as exc:
        def refused():
            raise exc
        return _editor_dispatch(refused)
    return await run_in_threadpool(_editor_dispatch, lambda: layout.publish_layout(
        tenant, project_id, drawing_id, parent_version_id, body,
        request_id=request_id, capability=x_checkout_capability))


@router.post("/api/projects/{project_id}/drawings/{drawing_id}/versions/{parent_version_id}/editor")
async def project_editor_save_route(project_id: UUID, drawing_id: UUID, parent_version_id: UUID,
        request: Request, tenant=Depends(deps.require_active_tenant),
        x_org_id: Optional[UUID] = Header(default=None),
        x_project_id: Optional[UUID] = Header(default=None),
        x_checkout_capability: Optional[str] = Header(default=None),
        idempotency_key: Optional[str] = Header(default=None)):
    return await _project_editor_write(request, tenant, project_id, drawing_id, parent_version_id,
        x_org_id, x_project_id, x_checkout_capability, idempotency_key, restore=False)


@router.post("/api/projects/{project_id}/drawings/{drawing_id}/versions/{parent_version_id}/editor/restore")
async def project_editor_restore_route(project_id: UUID, drawing_id: UUID, parent_version_id: UUID,
        request: Request, tenant=Depends(deps.require_active_tenant),
        x_org_id: Optional[UUID] = Header(default=None),
        x_project_id: Optional[UUID] = Header(default=None),
        x_checkout_capability: Optional[str] = Header(default=None),
        idempotency_key: Optional[str] = Header(default=None)):
    return await _project_editor_write(request, tenant, project_id, drawing_id, parent_version_id,
        x_org_id, x_project_id, x_checkout_capability, idempotency_key, restore=True)


# These output endpoints share only the existing context and response helpers.
import json
from starlette.responses import Response
from starlette.concurrency import run_in_threadpool
import solar_project_admission as output_policy
import solar_project_artifacts as output_artifacts
import solar_project_read as output_reads
import solar_project_solaredge as project_solaredge
from solar_design_graph import GraphValidationError

_OUTPUT_FAILURES = {
    "IMPORT_MEDIA_TYPE_REFUSED": (415, "Use a PDF source."),
    "REPORT_MEDIA_TYPE_REFUSED": (415, "Use a JSON report request."),
    "IMPORT_PDF_TOO_LARGE": (413, "The PDF exceeds the source size limit."),
    "REPORT_REQUEST_TOO_LARGE": (413, "The report request is too large."),
    "REPORT_REQUEST_INVALID": (400, "The report request is invalid."),
    "IMPORT_QUOTA_EXCEEDED": (429, "This version has reached its source limit."),
    "IMPORT_STORE_UNAVAILABLE": (503, "Project source storage is unavailable."),
    "IMPORT_WRITES_DRAINED": (503, "Project source creation is temporarily unavailable."),
    "REPORT_WRITES_DRAINED": (503, "Project report creation is temporarily unavailable."),
    "REPORT_BUSY": (503, "The PDF parser is busy. Try again shortly."),
    "REPORT_PARSE_TIMEOUT": (503, "The PDF could not be parsed within the time limit."),
    "REPORT_PARSE_UNAVAILABLE": (503, "The PDF parser is unavailable."),
    "project_execution_disabled": (409, "Project Solar outputs are not enabled on this server."),
    "SIP_R6_INVALID_REQUEST": (400, "Project Solar read request is invalid."),
    "SIP_R6_REQUEST_TOO_LARGE": (413, "Project Solar read request is too large."),
    "SIP_R6_TOOL_UNSUPPORTED": (409, "This Solar read has no connected project adapter."),
    "SIP_R6_TOOL_MANIFEST_MISMATCH": (409, "The Solar tool changed. Refresh the tools and try again."),
    "SIP_R6_STALE_CURRENT": (409, "The drawing changed. Select the current version and run the tool again."),
    "ARTIFACT_STALE": (409, "The drawing changed after this file was made. Select the current version and run the tool again."),
    "ARTIFACT_ID_INVALID": (400, "The project artifact reference is invalid."),
    "ARTIFACT_NOT_FOUND": (404, "The project artifact was not found."),
    "ARTIFACT_CORRUPT": (500, "The stored project artifact failed its integrity check."),
    "SIP_R6_ARTIFACT_SOURCE_MISMATCH": (500, "The project artifact does not match its source version."),
    "ARTIFACT_CONFLICT": (409, "The project artifact conflicts with an existing immutable file."),
    "ARTIFACT_STORE_UNAVAILABLE": (503, "Project artifact storage is unavailable."),
    "ARTIFACT_WRITES_DRAINED": (503, "Project artifact creation is temporarily unavailable."),
    "READ_OUTPUT_LIMIT_EXCEEDED": (500, "The Solar read result exceeds the response limit."),
    "READ_OUTPUT_INVALID": (500, "The Solar read produced an invalid result."),
    "ARTIFACT_REFERENCE_RESERVED": (500, "The Solar read produced an invalid result."),
    "SIP_R6_INTERNAL": (500, "Project Solar output is unavailable."),
}
_INVALID_ARTIFACT = frozenset(("ARTIFACT_TOO_LARGE", "ARTIFACT_FILENAME_INVALID",
    "ARTIFACT_CONTENT_MISMATCH", "ARTIFACT_INVALID", "ARTIFACT_MEDIA_TYPE_REFUSED"))
_INVALID_SOURCE = frozenset(("IMPORT_PDF_EMPTY", "IMPORT_NOT_A_PDF", "IMPORT_PDF_MALFORMED",
    "IMPORT_PDF_ENCRYPTED", "IMPORT_PDF_NO_PAGES", "IMPORT_PDF_TOO_MANY_PAGES"))


def _output_failure(reason):
    if reason in _INVALID_SOURCE:
        status, message = 400, "The source PDF is invalid."
    elif reason in _INVALID_ARTIFACT:
        status, message = 500, "The Solar read produced an invalid artifact."
    elif reason in _OUTPUT_FAILURES:
        status, message = _OUTPUT_FAILURES[reason]
    else:
        status, message = 409, "The Solar read was refused for this version."
    code = ErrorCode.INTERNAL if status >= 500 else ErrorCode.BAD_PARAMS
    retry = reason in ("ARTIFACT_STORE_UNAVAILABLE", "ARTIFACT_WRITES_DRAINED",
        "IMPORT_STORE_UNAVAILABLE", "IMPORT_WRITES_DRAINED", "REPORT_WRITES_DRAINED",
        "REPORT_BUSY", "REPORT_PARSE_TIMEOUT", "REPORT_PARSE_UNAVAILABLE")
    body = err_envelope(code, message, retry)
    body["error"]["reason_code"] = reason
    return JSONResponse(content=body, status_code=status, headers=_NO_STORE)


def _output_dispatch(operation):
    try:
        result = operation()
        return result if isinstance(result, Response) else JSONResponse(content=result, headers=_NO_STORE)
    except service.ProjectContextError as exc:
        if exc.reason_code in _FAILURES:
            def refusal():
                raise exc
            return _dispatch(refusal)
        return _output_failure("SIP_R6_INTERNAL")
    except GraphValidationError as exc:
        return _output_failure(exc.code)
    except Exception:
        return _output_failure("SIP_R6_INTERNAL")


def _output_scope(tenant, project_id, drawing_id, org_header, project_header, version_id=None):
    try:
        proj, drawing = UUID(project_id), UUID(drawing_id)
        version = None if version_id is None else UUID(version_id)
        org = None if org_header is None else UUID(org_header)
        header_project = None if project_header is None else UUID(project_header)
    except (ValueError, TypeError, AttributeError):
        raise GraphValidationError("SIP_R6_INVALID_REQUEST") from None
    _headers(tenant, proj, org, header_project)
    return proj, drawing, version


async def _solar_read_body(request: Request):
    if not output_policy.project_runs_enabled():
        return GraphValidationError("project_execution_disabled")
    raw = bytearray()
    try:
        async for chunk in request.stream():
            if len(raw) + len(chunk) > output_reads.MAX_REQUEST_BYTES:
                return GraphValidationError("SIP_R6_REQUEST_TOO_LARGE")
            raw.extend(chunk)
        body = json.loads(bytes(raw).decode("utf-8"), object_pairs_hook=service._object_pairs,
                          parse_constant=service._nonfinite, parse_float=service._json_float)
        if (type(body) is not dict or set(body) - {"tool", "params", "catalog_digest", "current"}
                or type(body.get("tool")) is not str or not body["tool"]
                or type(body.get("catalog_digest")) is not str or not body["catalog_digest"]
                or type(body.get("params", {})) is not dict
                or type(body.get("current", False)) is not bool):
            raise ValueError()
        return body
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return GraphValidationError("SIP_R6_INVALID_REQUEST")


@router.post("/api/projects/{project_id}/drawings/{drawing_id}/versions/{input_version_id}/solar-reads")
def solar_read_route(project_id: str, drawing_id: str, input_version_id: str,
                     body=Depends(_solar_read_body), tenant=Depends(deps.require_active_tenant),
                     x_org_id: Optional[str] = Header(default=None),
                     x_project_id: Optional[str] = Header(default=None)):
    def operation():
        if not output_policy.project_runs_enabled():
            raise GraphValidationError("project_execution_disabled")
        if isinstance(body, GraphValidationError):
            raise body
        proj, drawing, version = _output_scope(tenant, project_id, drawing_id,
            x_org_id, x_project_id, input_version_id)
        return output_reads.run_project_read(tenant, proj, drawing, version, body["tool"],
            body.get("params", {}), catalog_digest=body["catalog_digest"], current=body.get("current", False))
    return _output_dispatch(operation)


async def _project_solaredge_body(request, *, report):
    if not output_policy.project_runs_enabled():
        raise GraphValidationError("project_execution_disabled")
    media = "application/json" if report else "application/pdf"
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != media:
        raise GraphValidationError("REPORT_MEDIA_TYPE_REFUSED" if report else "IMPORT_MEDIA_TYPE_REFUSED")
    maximum = (project_solaredge.reports.MAX_REPORT_REQUEST_BYTES if report
               else project_solaredge.sources.MAX_IMPORT_PDF_BYTES)
    too_large = "REPORT_REQUEST_TOO_LARGE" if report else "IMPORT_PDF_TOO_LARGE"
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > maximum:
            raise GraphValidationError(too_large)
        raw.extend(chunk)
    if not report:
        return bytes(raw)
    try:
        return json.loads(bytes(raw).decode("utf-8"), object_pairs_hook=service._object_pairs,
            parse_constant=service._nonfinite, parse_float=service._json_float)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise GraphValidationError("REPORT_REQUEST_INVALID") from None


async def _project_solaredge_request(request, tenant, project_id, drawing_id,
                                    input_version_id, org_header, project_header, *, report):
    try:
        proj, drawing, version = _output_scope(tenant, project_id, drawing_id,
            org_header, project_header, input_version_id)
        body = await _project_solaredge_body(request, report=report)
    except (GraphValidationError, service.ProjectContextError) as exc:
        def refused():
            raise exc
        # The exception variable is cleared after the except suite.
        return _output_dispatch(refused)
    operation = project_solaredge.build_report if report else project_solaredge.import_source
    return await run_in_threadpool(_output_dispatch,
        lambda: operation(tenant, proj, drawing, version, body))


@router.post("/api/projects/{project_id}/drawings/{drawing_id}/versions/{input_version_id}/imports/solaredge-pdf")
async def project_solaredge_source_route(project_id: str, drawing_id: str, input_version_id: str,
        request: Request, tenant=Depends(deps.require_active_tenant),
        x_org_id: Optional[str] = Header(default=None), x_project_id: Optional[str] = Header(default=None)):
    return await _project_solaredge_request(request, tenant, project_id, drawing_id,
        input_version_id, x_org_id, x_project_id, report=False)


@router.post("/api/projects/{project_id}/drawings/{drawing_id}/versions/{input_version_id}/imports/solaredge-pdf/report")
async def project_solaredge_report_route(project_id: str, drawing_id: str, input_version_id: str,
        request: Request, tenant=Depends(deps.require_active_tenant),
        x_org_id: Optional[str] = Header(default=None), x_project_id: Optional[str] = Header(default=None)):
    return await _project_solaredge_request(request, tenant, project_id, drawing_id,
        input_version_id, x_org_id, x_project_id, report=True)


@router.get("/api/projects/{project_id}/drawings/{drawing_id}/solar-artifacts/{artifact_id}")
def solar_artifact_route(project_id: str, drawing_id: str, artifact_id: str, current: str = "false",
                         tenant=Depends(deps.require_active_tenant),
                         x_org_id: Optional[str] = Header(default=None),
                         x_project_id: Optional[str] = Header(default=None),
                         if_none_match: Optional[str] = Header(default=None)):
    def operation():
        if not output_policy.project_runs_enabled():
            raise GraphValidationError("project_execution_disabled")
        proj, drawing, _version = _output_scope(tenant, project_id, drawing_id, x_org_id, x_project_id)
        if current not in ("true", "false"):
            raise GraphValidationError("SIP_R6_INVALID_REQUEST")
        meta, content = output_artifacts.read_project_artifact(
            tenant, proj, drawing, artifact_id, current=current == "true")
        etag = '"' + meta["content_sha256"] + '"'
        headers = {**_NO_STORE, "ETag": etag, "X-Leaf-Artifact-Id": meta["artifact_id"],
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": 'attachment; filename="' + meta["filename"] + '"'}
        if if_none_match is not None and any(value.strip() in (etag, "W/" + etag, "*")
                                            for value in if_none_match.split(",")):
            return Response(content=b"", status_code=304, headers=headers)
        return Response(content=content, media_type=meta["media_type"], headers=headers)
    return _output_dispatch(operation)
