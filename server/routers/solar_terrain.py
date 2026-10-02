"""Tenant-scoped terrain reads and head-bound Ground preview operations."""
import json
import re
from typing import Optional

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import checkout_capability
import deps
import entitlements
import solar_ground_terrain_adapter as adapter
import solar_physical_head as physical_head
import solar_physical_state
import write_loop
from envelopes import ErrorCode, err_envelope, with_envelope_fields
from routers.drawings import _backend, _lock_authorization

router = APIRouter()
MAX_TERRAIN_BODY_BYTES = 8192
VIEW_RESPONSE_SCHEMA = "leaf.solar-terrain-view-response.v1"
TERRAIN_ROUTE_REFUSALS = {
    "TERRAIN_CHECKOUT_DENIED": (403, ErrorCode.BAD_PARAMS, False),
    "TERRAIN_CHECKOUT_UNAVAILABLE": (503, ErrorCode.INTERNAL, True),
    "TERRAIN_PROJECT_ID_INVALID": (400, ErrorCode.BAD_PARAMS, False),
    "TERRAIN_EXPECTED_HEAD_INVALID": (400, ErrorCode.BAD_PARAMS, False),
    "TERRAIN_LIMITS_INVALID": (400, ErrorCode.BAD_PARAMS, False),
    "TERRAIN_DRAWING_NOT_FOUND": (404, ErrorCode.BAD_PARAMS, False),
    "TERRAIN_STATE_NOT_FOUND": (404, ErrorCode.BAD_PARAMS, False),
    "TERRAIN_GRAPH_REQUIRED": (409, ErrorCode.BAD_PARAMS, False),
    "TERRAIN_PROJECT_MISMATCH": (409, ErrorCode.BAD_PARAMS, False),
    "TERRAIN_FRAME_UNSUPPORTED": (409, ErrorCode.BAD_PARAMS, False),
    "TERRAIN_GRID_MISSING": (409, ErrorCode.BAD_PARAMS, False),
    "TERRAIN_NO_TRACKER_ROWS": (409, ErrorCode.BAD_PARAMS, False),
    "TERRAIN_HEAD_MOVED": (409, ErrorCode.BAD_PARAMS, True),
    "TERRAIN_GRID_INVALID": (422, ErrorCode.BAD_PARAMS, False),
    "TERRAIN_GRID_TOO_LARGE": (422, ErrorCode.BAD_PARAMS, False),
    "TERRAIN_FRAMES_INVALID": (422, ErrorCode.BAD_PARAMS, False),
    "TERRAIN_TOO_MANY_ROWS": (422, ErrorCode.BAD_PARAMS, False),
    "TERRAIN_WRITES_DRAINED": (503, ErrorCode.INTERNAL, True),
    "TERRAIN_STORE_UNAVAILABLE": (503, ErrorCode.INTERNAL, True),
    "PHYSICAL_HEAD_CONFLICT": (409, ErrorCode.BAD_PARAMS, True),
    "PHYSICAL_HEAD_LOG_FULL": (409, ErrorCode.BAD_PARAMS, False),
    "PHYSICAL_HEAD_PROJECT_MISMATCH": (409, ErrorCode.BAD_PARAMS, False),
    "PHYSICAL_STATE_PROJECT_MISMATCH": (409, ErrorCode.BAD_PARAMS, False),
    "PHYSICAL_HEAD_WRITES_DRAINED": (503, ErrorCode.INTERNAL, True),
    "PHYSICAL_STATE_WRITES_DRAINED": (503, ErrorCode.INTERNAL, True),
    "PHYSICAL_HEAD_STORE_UNAVAILABLE": (503, ErrorCode.INTERNAL, True),
    "PHYSICAL_STATE_STORE_UNAVAILABLE": (503, ErrorCode.INTERNAL, True),
    "PHYSICAL_HEAD_CORRUPT": (500, ErrorCode.INTERNAL, False),
    "PHYSICAL_HEAD_STORE_UNSAFE": (500, ErrorCode.INTERNAL, False),
    "PHYSICAL_STATE_CORRUPT": (500, ErrorCode.INTERNAL, False),
    "TERRAIN_DRAWING_ID_INVALID": (400, ErrorCode.BAD_PARAMS, False),
    "TERRAIN_OPERATION_INVALID": (400, ErrorCode.BAD_PARAMS, False),
    "TERRAIN_BODY_INVALID": (400, ErrorCode.BAD_PARAMS, False),
    "TERRAIN_BODY_TOO_LARGE": (413, ErrorCode.BAD_PARAMS, False),
    "TERRAIN_MEDIA_TYPE_REFUSED": (415, ErrorCode.BAD_PARAMS, False),
    "TERRAIN_OPERATION_FAILED": (500, ErrorCode.INTERNAL, False),
}


def _terrain_refused(reason):
    if reason not in TERRAIN_ROUTE_REFUSALS:
        reason = "TERRAIN_OPERATION_FAILED"
    status, code, retryable = TERRAIN_ROUTE_REFUSALS[reason]
    env = err_envelope(code, reason, retryable=retryable)
    env["error"]["reason_code"] = reason
    return JSONResponse(status_code=status, content=env)


def _scope_refusal(drawing_id, project_id, tenant, permission):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", drawing_id):
        return _terrain_refused("TERRAIN_DRAWING_ID_INVALID")
    if project_id is not None and not 1 <= len(project_id) <= 100:
        return _terrain_refused("TERRAIN_PROJECT_ID_INVALID")
    tier = entitlements.resolve_tier(tenant)
    try:
        roles, elevated = entitlements.resolve_roles(tenant)
        if not entitlements.entitlements_for(tier, roles, elevated).get(permission, False):
            return entitlements.entitlement_denied_response(permission, tier)
    except entitlements.EntitlementsError:
        return entitlements.policy_unavailable_response(permission, tier)
    return None


def _terrain_backend(tenant):
    try:
        return _backend(tenant)
    except (RuntimeError, OSError):
        raise adapter.TerrainAdapterError("TERRAIN_STORE_UNAVAILABLE") from None


def _get_terrain(tenant, drawing_id, project_id):
    backend = _terrain_backend(tenant)
    context = adapter._context(backend, tenant, drawing_id, project_id)
    head, document = physical_head.load_physical_head(
        backend, tenant, drawing_id, project_id=context["project_id"])
    terrain = None if head is None else dict(
        adapter.terrain_view(document), drawing_id=drawing_id, project_id=head["project_id"])
    return {"schema": VIEW_RESPONSE_SCHEMA, "stored": head is not None,
            "head": head, "terrain": terrain}


def _operate(tenant, drawing_id, project_id, body, capability):
    backend = _terrain_backend(str(tenant))
    try:
        _lock_authorization(drawing_id, tenant, backend, capability)
    except checkout_capability.CapabilityRejected:
        raise adapter.TerrainAdapterError("TERRAIN_CHECKOUT_DENIED") from None
    except checkout_capability.CapabilityUnavailable:
        raise adapter.TerrainAdapterError("TERRAIN_CHECKOUT_UNAVAILABLE") from None
    except KeyError:
        pass  # No manifest: the adapter answers TERRAIN_DRAWING_NOT_FOUND.
    except (ValueError, OSError):
        raise adapter.TerrainAdapterError("TERRAIN_STORE_UNAVAILABLE") from None
    call = {"mesh": adapter.render_mesh, "slope": adapter.check_tracker_slope,
            "slope-clear": adapter.clear_tracker_slope}[body["operation"]]
    kwargs = {"project_id": project_id, "expected_head": body["expected_head"]}
    if "limits" in body:
        kwargs["limits"] = body["limits"]
    return call(backend, str(tenant), drawing_id, **kwargs)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


@router.get("/api/drawings/{drawing_id}/terrain")
async def get_terrain(drawing_id: str, project_id: Optional[str] = None,
                      tenant=Depends(deps.require_active_tenant)):
    refusal = _scope_refusal(drawing_id, project_id, tenant, "run_read")
    if refusal is not None:
        return refusal
    try:
        result = await run_in_threadpool(_get_terrain, str(tenant), drawing_id, project_id)
    except solar_physical_state.PhysicalStateError as exc:
        return _terrain_refused(exc.code)
    return JSONResponse(content=with_envelope_fields(result))


@router.post("/api/drawings/{drawing_id}/terrain/operations")
async def terrain_operation(drawing_id: str, request: Request, project_id: Optional[str] = None,
                            tenant=Depends(deps.require_active_tenant),
                            x_checkout_capability: Optional[str] = Header(default=None)):
    refusal = _scope_refusal(drawing_id, project_id, tenant, "run_write")
    if refusal is not None:
        return refusal
    if write_loop.drawing_mutations_refusal() is not None:
        return _terrain_refused("TERRAIN_WRITES_DRAINED")
    media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        return _terrain_refused("TERRAIN_MEDIA_TYPE_REFUSED")
    length = request.headers.get("content-length", "")
    if re.fullmatch(r"[0-9]+", length):
        stripped = length.lstrip("0")
        if len(stripped) > 12 or int(stripped or "0") > MAX_TERRAIN_BODY_BYTES:
            return _terrain_refused("TERRAIN_BODY_TOO_LARGE")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_TERRAIN_BODY_BYTES:
            return _terrain_refused("TERRAIN_BODY_TOO_LARGE")
        data.extend(chunk)
    try:
        body = json.loads(bytes(data), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        return _terrain_refused("TERRAIN_BODY_INVALID")
    if type(body) is not dict or not set(body) <= {"operation", "expected_head", "limits"}:
        return _terrain_refused("TERRAIN_BODY_INVALID")
    operation = body.get("operation")
    if "limits" in body and operation != "slope":
        return _terrain_refused("TERRAIN_BODY_INVALID")
    if type(operation) is not str or operation not in ("mesh", "slope", "slope-clear"):
        return _terrain_refused("TERRAIN_OPERATION_INVALID")
    expected_head = body.get("expected_head")
    if type(expected_head) is not str or not re.fullmatch(r"[0-9a-f]{64}", expected_head):
        return _terrain_refused("TERRAIN_EXPECTED_HEAD_INVALID")
    try:
        result = await run_in_threadpool(_operate, tenant, drawing_id, project_id, body,
                                         x_checkout_capability)
    except solar_physical_state.PhysicalStateError as exc:
        return _terrain_refused(exc.code)
    return JSONResponse(content=with_envelope_fields(result))
