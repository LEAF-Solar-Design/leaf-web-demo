"""
Extended result/error envelope (server CONTRACT-ADDENDUM section 10).

Every server response body carries at minimum:
    error: null | {error_code, message, retryable}
    degraded_mode: bool

The section-3 run envelope is EXTENDED (existing success fields unchanged):
    {ok, tool, version, result, overlay, timing_ms, cost, error, degraded_mode}

degraded_mode=True means: APS_LIVE was requested but the run fell back to the
pure-python path (so the UI can say "used the local fallback, not the cloud
solver").
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi.responses import JSONResponse


class ErrorCode:
    """Machine-readable error codes (frozen enum for the platform frontend)."""

    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    BAD_PARAMS = "BAD_PARAMS"
    APS_UNAVAILABLE = "APS_UNAVAILABLE"
    BROKER_UNREACHABLE = "BROKER_UNREACHABLE"
    WORKITEM_FAILED = "WORKITEM_FAILED"
    TIMEOUT = "TIMEOUT"
    TENANT_DISABLED = "TENANT_DISABLED"
    GRANT_REQUIRED = "GRANT_REQUIRED"      # per-tenant Claude grant absent (HTTP 401)
    ENTITLEMENT_REQUIRED = "ENTITLEMENT_REQUIRED"  # tier lacks the capability (HTTP 403)
    QUOTA_EXCEEDED = "quota_exceeded"  # promoted 2026-07-17 (broker hard cap, HTTP 402)
    INTERNAL = "INTERNAL"

    ALL = (
        UNKNOWN_TOOL,
        BAD_PARAMS,
        APS_UNAVAILABLE,
        BROKER_UNREACHABLE,
        WORKITEM_FAILED,
        TIMEOUT,
        TENANT_DISABLED,
        GRANT_REQUIRED,
        ENTITLEMENT_REQUIRED,
        QUOTA_EXCEEDED,
        INTERNAL,
    )


# sane default HTTP status per error code (body carries the machine-readable part)
DEFAULT_HTTP_STATUS: Dict[str, int] = {
    ErrorCode.QUOTA_EXCEEDED: 402,
    ErrorCode.UNKNOWN_TOOL: 404,
    ErrorCode.BAD_PARAMS: 400,
    ErrorCode.APS_UNAVAILABLE: 502,
    ErrorCode.BROKER_UNREACHABLE: 502,
    ErrorCode.WORKITEM_FAILED: 502,
    ErrorCode.TIMEOUT: 504,
    ErrorCode.TENANT_DISABLED: 403,
    ErrorCode.GRANT_REQUIRED: 401,
    ErrorCode.ENTITLEMENT_REQUIRED: 403,
    ErrorCode.INTERNAL: 500,
}


def error_obj(error_code: str, message: str, retryable: bool) -> Dict[str, Any]:
    assert error_code in ErrorCode.ALL, f"unknown error_code {error_code!r}"
    return {"error_code": error_code, "message": str(message), "retryable": bool(retryable)}


def ok_envelope(
    tool: str,
    version: str,
    result: Any,
    overlay: Any,
    timing_ms: int,
    cost: Any = None,
    degraded_mode: bool = False,
) -> Dict[str, Any]:
    """Section-3 run envelope, extended (success)."""
    return {
        "ok": True,
        "tool": tool,
        "version": version,
        "result": result,
        "overlay": overlay,
        "timing_ms": timing_ms,
        "cost": cost,
        "error": None,
        "degraded_mode": bool(degraded_mode),
    }


def err_envelope(
    error_code: str,
    message: str,
    retryable: bool,
    tool: Optional[str] = None,
    version: Optional[str] = None,
    timing_ms: int = 0,
) -> Dict[str, Any]:
    """Section-3 run envelope, extended (failure)."""
    return {
        "ok": False,
        "tool": tool,
        "version": version,
        "result": None,
        "overlay": None,
        "timing_ms": timing_ms,
        "cost": None,
        "error": error_obj(error_code, message, retryable),
        "degraded_mode": False,
    }


def with_envelope_fields(body: Dict[str, Any], degraded_mode: bool = False) -> Dict[str, Any]:
    """ADDITIVELY extend a non-run response body (session/tools/author/health/...)
    with the shared envelope fields. Existing keys are never touched."""
    out = dict(body)
    out.setdefault("error", None)
    out.setdefault("degraded_mode", bool(degraded_mode))
    return out


def error_response(
    error_code: str,
    message: str,
    retryable: bool,
    status_code: Optional[int] = None,
    tool: Optional[str] = None,
) -> JSONResponse:
    """A JSONResponse whose body is the extended err envelope."""
    return JSONResponse(
        status_code=status_code or DEFAULT_HTTP_STATUS.get(error_code, 500),
        content=err_envelope(error_code, message, retryable, tool=tool),
    )


def install_error_handlers(app) -> None:
    """App-wide stragglers: pydantic validation errors and any leftover
    HTTPException become structured err envelopes (machine-readable bodies)."""
    from fastapi import HTTPException
    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request, exc):  # noqa: ANN001
        return error_response(ErrorCode.BAD_PARAMS, str(exc.errors()), retryable=False, status_code=422)

    @app.exception_handler(HTTPException)
    async def _http_exc_handler(request, exc):  # noqa: ANN001
        return error_response(
            ErrorCode.INTERNAL, str(exc.detail), retryable=False, status_code=exc.status_code
        )
