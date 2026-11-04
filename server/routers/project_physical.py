"""Organization-authorized physical reads and fenced canonical writes."""
import json
import re
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

import deps
import entitlements
import solar_civil_operations as civil
import solar_ground_terrain_adapter as adapter
import solar_landxml_import as landxml
import solar_physical_state as ps
import solar_project_admission as policy
import solar_project_context as project
import solar_project_physical as physical
import solar_project_physical_read as reads
import write_loop
from envelopes import ErrorCode, err_envelope, with_envelope_fields
from routers import drawings, project_drawings, solar_terrain
from solar_design_graph import GraphValidationError

router = APIRouter()
PREFIX = "/api/projects/{project_id}/drawings/{drawing_id}/versions/{version_id}/physical"
VIEW_REFUSALS = solar_terrain.TERRAIN_ROUTE_REFUSALS | solar_terrain.CIVIL_ROUTE_REFUSALS
_NO_STORE = {"Cache-Control": "no-store"}


def _no_store(response):
    response.headers.update(_NO_STORE)
    return response


def _physical_failure(exc, refusals):
    if isinstance(exc, Response):
        return _no_store(exc)
    if isinstance(exc, project.ProjectContextError) and exc.reason_code in project_drawings._FAILURES:
        def refusal():
            raise exc
        return project_drawings._dispatch(refusal)
    if isinstance(exc, GraphValidationError):
        return project_drawings._output_failure(exc.code)
    if isinstance(exc, ps.PhysicalStateError) and exc.code in refusals:
        call = drawings._landxml_refused if refusals is drawings.LANDXML_IMPORT_REFUSALS else solar_terrain._terrain_refused
        return _no_store(call(exc.code))
    body = err_envelope(ErrorCode.INTERNAL, "Project physical operation unavailable.", retryable=False)
    body["error"]["reason_code"] = "PROJECT_PHYSICAL_INTERNAL"
    return JSONResponse(content=body, status_code=500, headers=_NO_STORE)


async def _prefix(tenant, project_id, drawing_id, version_id, org_header, project_header):
    try:
        proj, drawing, version = UUID(project_id), UUID(drawing_id), UUID(version_id)
    except (ValueError, TypeError, AttributeError):
        raise GraphValidationError("SIP_R6_INVALID_REQUEST") from None
    if not policy.project_runs_enabled():
        raise GraphValidationError("project_execution_disabled")
    try:
        org = None if org_header is None else UUID(org_header)
        header_project = None if project_header is None else UUID(project_header)
    except (ValueError, TypeError, AttributeError):
        raise GraphValidationError("SIP_R6_INVALID_REQUEST") from None
    await run_in_threadpool(project_drawings._headers, tenant, proj, org, header_project)
    return proj, drawing, version


def _entitlement(tenant, permission):
    tier = entitlements.resolve_tier(tenant)
    try:
        roles, elevated = entitlements.resolve_roles(tenant)
        if not entitlements.entitlements_for(tier, roles, elevated).get(permission, False):
            return entitlements.entitlement_denied_response(permission, tier)
    except entitlements.EntitlementsError:
        return entitlements.policy_unavailable_response(permission, tier)
    return None


def _context(verified):
    b = verified.binding
    return {"organization_id": str(b.organization_id), "project_id": str(b.project_id),
            "drawing_id": str(b.drawing_id), "version_id": str(b.input_version_id),
            "head_version_id": str(b.head_version_id), "is_head": b.is_head,
            "intake_sha256": verified.intake_sha256}


def _terrain_result(view, document):
    return {"schema": solar_terrain.VIEW_RESPONSE_SCHEMA, "stored": view is not None,
            "head": view, "terrain": None if view is None else dict(adapter.terrain_view(document),
                drawing_id=view["drawing_id"], project_id=view["project_id"])}


def _view(tenant, proj, drawing, version, view):
    verified = project.resolve_context(tenant, proj, version, drawing_id=drawing, write=False)
    head, document = physical.load_bound(verified)
    result = _terrain_result(head, document) if view == "terrain" else civil.civil_view(head, document)
    return {**result, "schema": "leaf.solar-project-physical-view.v1", "view": view,
            "context": _context(verified), "result": result}


@router.get(PREFIX)
async def get_physical(project_id: str, drawing_id: str, version_id: str, view: str = "terrain",
                       tenant=Depends(deps.require_active_tenant),
                       x_org_id: Optional[str] = Header(default=None),
                       x_project_id: Optional[str] = Header(default=None)):
    try:
        proj, drawing, version = await _prefix(tenant, project_id, drawing_id, version_id, x_org_id, x_project_id)
        if view not in ("terrain", "civil"):
            raise GraphValidationError("SIP_R6_INVALID_REQUEST")
        denial = await run_in_threadpool(_entitlement, tenant, "run_read")
        if denial is not None:
            return _no_store(denial)
        result = await run_in_threadpool(_view, tenant, proj, drawing, version, view)
        return JSONResponse(content=with_envelope_fields(result), headers=_NO_STORE)
    except Exception as exc:
        return _physical_failure(exc, VIEW_REFUSALS)


def _operation_body(data, declared_length):
    def fail(code):
        raise ps.PhysicalStateError(code)
    try:
        body = json.loads(bytes(data), object_pairs_hook=solar_terrain._unique_object)
    except (ValueError, UnicodeError, RecursionError):
        fail("TERRAIN_BODY_TOO_LARGE" if max(len(data), declared_length or 0) > 8192 else "TERRAIN_BODY_INVALID")
    operation = body.get("operation") if type(body) is dict else None
    if type(operation) is str and operation in civil.OPERATIONS:
        return civil.validate_body(body)
    if max(len(data), declared_length or 0) > 8192:
        fail("TERRAIN_BODY_TOO_LARGE")
    if type(body) is not dict or not set(body) <= {"operation", "expected_head", "limits"}:
        fail("TERRAIN_BODY_INVALID")
    if "limits" in body and operation != "slope":
        fail("TERRAIN_BODY_INVALID")
    if type(operation) is not str or operation not in ("mesh", "slope", "slope-clear"):
        fail("TERRAIN_OPERATION_INVALID")
    if type(body.get("expected_head")) is not str or not re.fullmatch(r"[0-9a-f]{64}", body["expected_head"]):
        fail("TERRAIN_EXPECTED_HEAD_INVALID")
    return body


def _declared_length(request, limit, code):
    length = request.headers.get("content-length", "")
    if re.fullmatch(r"[0-9]+", length):
        stripped = length.lstrip("0")
        if len(stripped) > 12 or int(stripped or "0") > limit:
            raise ps.PhysicalStateError(code)
        return int(stripped or "0")
    return None


async def _stream(request, limit, code, *, read=False):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > limit:
            if read:
                raise GraphValidationError(code)
            raise ps.PhysicalStateError(code)
        data.extend(chunk)
    return bytes(data)


def _write_result(admission, outcome, schema):
    context = _context(admission.context)
    context.update(input_version_id=str(admission.context.binding.input_version_id),
                   output_version_id=outcome["output_version_id"], created=outcome["created"])
    return JSONResponse(content=with_envelope_fields({"schema": schema, "context": context,
                                                      "result": outcome["result"]}), headers=_NO_STORE)


@router.post(PREFIX + "/operations")
async def physical_operations(project_id: str, drawing_id: str, version_id: str, request: Request,
                              tenant=Depends(deps.require_active_tenant),
                              x_org_id: Optional[str] = Header(default=None),
                              x_project_id: Optional[str] = Header(default=None),
                              x_checkout_capability: Optional[str] = Header(default=None)):
    try:
        proj, drawing, version = await _prefix(tenant, project_id, drawing_id, version_id, x_org_id, x_project_id)
        denial = await run_in_threadpool(_entitlement, tenant, "run_write")
        if denial is not None:
            return _no_store(denial)
        if write_loop.drawing_mutations_refusal() is not None:
            raise ps.PhysicalStateError("TERRAIN_WRITES_DRAINED")
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ps.PhysicalStateError("TERRAIN_MEDIA_TYPE_REFUSED")
        length = _declared_length(request, civil.MAX_CIVIL_BODY_BYTES, "TERRAIN_BODY_TOO_LARGE")
        admission = await run_in_threadpool(project.verify_at_admission, tenant, proj, drawing, version,
                                           checkout_capability=x_checkout_capability)
        data = await _stream(request, civil.MAX_CIVIL_BODY_BYTES, "TERRAIN_BODY_TOO_LARGE")
        body = _operation_body(data, length)
        outcome = await run_in_threadpool(physical.run_operation, admission, body)
        return _write_result(admission, outcome, "leaf.solar-project-physical-operation.v1")
    except Exception as exc:
        return _physical_failure(exc, VIEW_REFUSALS)


@router.post(PREFIX + "/imports/landxml")
async def physical_import(project_id: str, drawing_id: str, version_id: str, request: Request,
                          drawing_units: Optional[str] = None, crs: Optional[str] = None,
                          target_cells: Optional[str] = None,
                          tenant=Depends(deps.require_active_tenant),
                          x_org_id: Optional[str] = Header(default=None),
                          x_project_id: Optional[str] = Header(default=None),
                          x_checkout_capability: Optional[str] = Header(default=None)):
    try:
        proj, drawing, version = await _prefix(tenant, project_id, drawing_id, version_id, x_org_id, x_project_id)
        if target_cells is None:
            cells = landxml.DEFAULT_TARGET_CELLS
        elif re.fullmatch(r"[0-9]{1,3}", target_cells):
            cells = int(target_cells)
        else:
            raise ps.PhysicalStateError("LANDXML_TARGET_CELLS_INVALID")
        denial = await run_in_threadpool(_entitlement, tenant, "upload")
        if denial is not None:
            return _no_store(denial)
        if write_loop.drawing_mutations_refusal() is not None:
            raise ps.PhysicalStateError("LANDXML_WRITES_DRAINED")
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() not in drawings.LANDXML_MEDIA_TYPES:
            raise ps.PhysicalStateError("LANDXML_MEDIA_TYPE_REFUSED")
        _declared_length(request, landxml.MAX_LANDXML_BYTES, "LANDXML_TOO_LARGE")
        admission = await run_in_threadpool(project.verify_at_admission, tenant, proj, drawing, version,
                                           checkout_capability=x_checkout_capability)
        data = await _stream(request, landxml.MAX_LANDXML_BYTES, "LANDXML_TOO_LARGE")
        outcome = await run_in_threadpool(physical.run_landxml_import, admission, data,
                                         drawing_units=drawing_units, crs=crs, target_cells=cells)
        return _write_result(admission, outcome, "leaf.solar-project-physical-import.v1")
    except Exception as exc:
        return _physical_failure(exc, drawings.LANDXML_IMPORT_REFUSALS)


@router.post(PREFIX + "/reads")
async def physical_reads(project_id: str, drawing_id: str, version_id: str, request: Request,
                         tenant=Depends(deps.require_active_tenant),
                         x_org_id: Optional[str] = Header(default=None),
                         x_project_id: Optional[str] = Header(default=None)):
    try:
        proj, drawing, version = await _prefix(tenant, project_id, drawing_id, version_id, x_org_id, x_project_id)
        denial = await run_in_threadpool(_entitlement, tenant, "run_read")
        if denial is not None:
            return _no_store(denial)
        await run_in_threadpool(project._access, tenant, proj, write=False)
        # Resolve the path binding too: a foreign version must fail before body consumption.
        await run_in_threadpool(project.resolve_context, tenant, proj, version, drawing_id=drawing, write=False)
        data = await _stream(request, 262144, "SIP_R6_REQUEST_TOO_LARGE", read=True)
        try:
            body = json.loads(data.decode("utf-8"), object_pairs_hook=project._object_pairs,
                              parse_constant=project._nonfinite, parse_float=project._json_float)
            if (type(body) is not dict or set(body) - {"tool", "params", "catalog_digest", "current"}
                    or type(body.get("tool")) is not str or not body["tool"]
                    or type(body.get("catalog_digest")) is not str or not body["catalog_digest"]
                    or type(body.get("params", {})) is not dict
                    or type(body.get("current", False)) is not bool):
                raise ValueError()
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise GraphValidationError("SIP_R6_INVALID_REQUEST") from None
        result = await run_in_threadpool(reads.run_project_physical_read, tenant, proj, drawing, version,
            body["tool"], body.get("params", {}), catalog_digest=body["catalog_digest"], current=body.get("current", False))
        return _no_store(result) if isinstance(result, Response) else JSONResponse(content=result, headers=_NO_STORE)
    except Exception as exc:
        return _physical_failure(exc, VIEW_REFUSALS)
