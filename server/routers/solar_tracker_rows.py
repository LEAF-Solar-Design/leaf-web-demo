"""Tenant-scoped, bounded manual tracker row publication."""
import json
import re
from types import MappingProxyType
from typing import Optional

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import checkout_capability
import deps
import entitlements
import solar_tracker_rows as domain
import write_loop
from envelopes import ErrorCode, err_envelope, with_envelope_fields
from routers.drawings import _backend, _lock_authorization
from routers.solar_terrain import _unique_object

router = APIRouter()
TRACKER_ROWS_ROUTE_REFUSALS = MappingProxyType({
    "TRACKER_ROWS_OPERATION_UNSUPPORTED": (400, ErrorCode.BAD_PARAMS, False),
    "TRACKER_ROWS_REQUEST_INVALID": (400, ErrorCode.BAD_PARAMS, False),
    "TRACKER_ROWS_ROWS_INVALID": (400, ErrorCode.BAD_PARAMS, False),
    "TRACKER_ROWS_ROW_INVALID": (400, ErrorCode.BAD_PARAMS, False),
    "TRACKER_ROWS_POWER_INVALID": (400, ErrorCode.BAD_PARAMS, False),
    "TRACKER_ROWS_HEAD_INVALID": (400, ErrorCode.BAD_PARAMS, False),
    "TRACKER_ROWS_UNITS_UNSUPPORTED": (409, ErrorCode.BAD_PARAMS, False),
    "TRACKER_ROWS_UNITS_MISMATCH": (409, ErrorCode.BAD_PARAMS, False),
    "TRACKER_ROWS_FRAME_UNSUPPORTED": (409, ErrorCode.BAD_PARAMS, False),
    "TRACKER_ROWS_SLOTS_INVALID": (400, ErrorCode.BAD_PARAMS, False),
    "TRACKER_ROWS_AXIS_INVALID": (400, ErrorCode.BAD_PARAMS, False),
    "TRACKER_ROWS_WIDTH_INVALID": (400, ErrorCode.BAD_PARAMS, False),
    "TRACKER_ROWS_LIMIT_EXCEEDED": (413, ErrorCode.BAD_PARAMS, False),
    "TRACKER_ROWS_PROJECT_ID_INVALID": (400, ErrorCode.BAD_PARAMS, False),
    "TRACKER_ROWS_PROJECT_MISMATCH": (409, ErrorCode.BAD_PARAMS, False),
    "TRACKER_ROWS_DRAWING_NOT_FOUND": (404, ErrorCode.BAD_PARAMS, False),
    "TRACKER_ROWS_GRAPH_REQUIRED": (409, ErrorCode.BAD_PARAMS, False),
    "TRACKER_ROWS_GROUND_REQUIRED": (409, ErrorCode.BAD_PARAMS, False),
    "TRACKER_ROWS_GRAPH_CONVERTED": (409, ErrorCode.BAD_PARAMS, False),
    "TRACKER_ROWS_DEPENDENT_STATE": (409, ErrorCode.BAD_PARAMS, False),
    "TRACKER_ROWS_ALREADY_EXISTS": (409, ErrorCode.BAD_PARAMS, False),
    "TRACKER_ROWS_STALE_HEAD": (409, ErrorCode.BAD_PARAMS, True),
    "TRACKER_ROWS_STATE_INVALID": (500, ErrorCode.INTERNAL, False),
    "TRACKER_ROWS_WRITES_DRAINED": (503, ErrorCode.INTERNAL, True),
    "TRACKER_ROWS_STORE_UNAVAILABLE": (503, ErrorCode.INTERNAL, True),
    "TRACKER_ROWS_STORE_UNSAFE": (500, ErrorCode.INTERNAL, False),
    "TRACKER_ROWS_LOG_FULL": (409, ErrorCode.BAD_PARAMS, False),
    "TRACKER_ROWS_CHECKOUT_REQUIRED": (403, ErrorCode.BAD_PARAMS, False),
    "TRACKER_ROWS_CHECKOUT_UNAVAILABLE": (503, ErrorCode.INTERNAL, True),
    "TRACKER_ROWS_CONTENT_TYPE_UNSUPPORTED": (415, ErrorCode.BAD_PARAMS, False),
})


def _refused(reason):
    if reason not in TRACKER_ROWS_ROUTE_REFUSALS:
        reason = "TRACKER_ROWS_STATE_INVALID"
    status, error_code, retryable = TRACKER_ROWS_ROUTE_REFUSALS[reason]
    env = err_envelope(error_code, reason, retryable=retryable)
    env["error"]["reason_code"] = reason
    return JSONResponse(status_code=status, content=env)


def _scope_refusal(drawing_id, project_id, tenant):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", drawing_id):
        return _refused("TRACKER_ROWS_REQUEST_INVALID")
    if project_id is not None and not 1 <= len(project_id) <= 100:
        return _refused("TRACKER_ROWS_PROJECT_ID_INVALID")
    tier = entitlements.resolve_tier(tenant)
    try:
        roles, elevated = entitlements.resolve_roles(tenant)
        if not entitlements.entitlements_for(tier, roles, elevated).get("run_write", False):
            return entitlements.entitlement_denied_response("run_write", tier)
    except entitlements.EntitlementsError:
        return entitlements.policy_unavailable_response("run_write", tier)
    return None


def _publish(tenant, drawing_id, project_id, body, capability, request_bytes):
    try:
        backend = _backend(str(tenant))
    except (RuntimeError, OSError):
        raise domain.TrackerRowsError("TRACKER_ROWS_STORE_UNAVAILABLE") from None
    try:
        _lock_authorization(drawing_id, tenant, backend, capability)
    except checkout_capability.CapabilityRejected:
        raise domain.TrackerRowsError("TRACKER_ROWS_CHECKOUT_REQUIRED") from None
    except checkout_capability.CapabilityUnavailable:
        raise domain.TrackerRowsError("TRACKER_ROWS_CHECKOUT_UNAVAILABLE") from None
    except KeyError:
        raise domain.TrackerRowsError("TRACKER_ROWS_DRAWING_NOT_FOUND") from None
    except (ValueError, OSError):
        raise domain.TrackerRowsError("TRACKER_ROWS_STORE_UNAVAILABLE") from None
    return domain.publish_manual_create(
        backend, str(tenant), drawing_id, rows=body["rows"],
        module_power_watts=body["module_power_watts"], expected_head=body["expected_head"],
        project_id=project_id, request_bytes=request_bytes)


def _reject_constant(value):
    raise ValueError("nonstandard JSON constant")


@router.post("/api/drawings/{drawing_id}/tracker-rows")
async def tracker_rows(drawing_id: str, request: Request, project_id: Optional[str] = None,
                       tenant=Depends(deps.require_active_tenant),
                       x_checkout_capability: Optional[str] = Header(default=None)):
    refusal = _scope_refusal(drawing_id, project_id, tenant)
    if refusal is not None:
        return refusal
    if write_loop.drawing_mutations_refusal() is not None:
        return _refused("TRACKER_ROWS_WRITES_DRAINED")
    media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        return _refused("TRACKER_ROWS_CONTENT_TYPE_UNSUPPORTED")
    length = request.headers.get("content-length", "")
    if re.fullmatch(r"[0-9]+", length):
        stripped = length.lstrip("0")
        if len(stripped) > 12 or int(stripped or "0") > domain.MAX_REQUEST_BYTES:
            return _refused("TRACKER_ROWS_LIMIT_EXCEEDED")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > domain.MAX_REQUEST_BYTES:
            return _refused("TRACKER_ROWS_LIMIT_EXCEEDED")
        data.extend(chunk)
    try:
        # Strict UTF-8 text first: json.loads on bytes would detect UTF-16 and UTF-32 and decode
        # with surrogatepass, so a lone surrogate would reach authorization.
        body = json.loads(bytes(data).decode("utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        return _refused("TRACKER_ROWS_REQUEST_INVALID")
    if type(body) is not dict or set(body) != {
            "operation", "rows", "module_power_watts", "expected_head"}:
        return _refused("TRACKER_ROWS_REQUEST_INVALID")
    if type(body["operation"]) is not str or body["operation"] != "manual-create":
        return _refused("TRACKER_ROWS_OPERATION_UNSUPPORTED")
    try:
        result = await run_in_threadpool(_publish, tenant, drawing_id, project_id, body,
                                         x_checkout_capability, len(data))
        return JSONResponse(status_code=201 if result["created"] else 200,
                            content=with_envelope_fields(result))
    except domain.TrackerRowsError as exc:
        return _refused(exc.code)
    except Exception:
        return _refused("TRACKER_ROWS_STATE_INVALID")
