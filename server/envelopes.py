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
    # sessions wire (added 2026-07-21, gap audit §2.1 / CONTRACT-ADDENDUM sessions spec) —
    # lowercase values to match the frozen client-side errorCode strings verbatim
    # (converse.js's classifyAgentError() switches on these exact tokens).
    TURN_IN_PROGRESS = "turn_in_progress"          # a turn already in flight (HTTP 409)
    SESSION_NOT_FOUND = "session_not_found"        # unknown/expired session_id (HTTP 404)
    LLM_QUOTA_EXHAUSTED = "llm_quota_exhausted"    # harness-reported hard quota (HTTP 429)
    LLM_RATE_LIMITED = "llm_rate_limited"          # harness-reported rate limit (HTTP 429)
    CONFIRMATION_EXPIRED = "confirmation_expired"  # approval TTL lapsed (HTTP 410)

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
        TURN_IN_PROGRESS,
        SESSION_NOT_FOUND,
        LLM_QUOTA_EXHAUSTED,
        LLM_RATE_LIMITED,
        CONFIRMATION_EXPIRED,
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
    ErrorCode.TURN_IN_PROGRESS: 409,
    ErrorCode.SESSION_NOT_FOUND: 404,
    ErrorCode.LLM_QUOTA_EXHAUSTED: 429,
    ErrorCode.LLM_RATE_LIMITED: 429,
    ErrorCode.CONFIRMATION_EXPIRED: 410,
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
        # Section-10: map the HTTPException's HTTP status to a sensible
        # machine-readable ErrorCode instead of hardcoding INTERNAL for every
        # straggler. Primarily exercised by platform/api.py + platform/deps.py,
        # which raise bare HTTPException and rely on this handler (mounted by
        # server/app.py) to serialize the section-10 {ok,error,degraded_mode}
        # shape. Other routers construct error_response(...) directly and never
        # reach here, so this mapping is additive and does not touch them.
        status_code = exc.status_code
        if status_code in (400, 404, 422):
            # 400/404: bad or unresolvable caller input (missing/invalid header,
            # unknown resource id under 404-not-403 isolation). 422: pydantic/
            # explicit body-validation failure — same "fix your request" family.
            error_code, retryable = ErrorCode.BAD_PARAMS, False
        elif status_code == 403:
            # No more specific existing code fits a bare 403 (admin-token gate,
            # verified-but-unprovisioned session); ENTITLEMENT_REQUIRED is the
            # closest machine-readable "you're not allowed to do this" code.
            error_code, retryable = ErrorCode.ENTITLEMENT_REQUIRED, False
        elif status_code == 503:
            # Fail-closed-when-unconfigured (e.g. offboard with no admin token
            # set): retryable once an operator fixes the deployment config.
            error_code, retryable = ErrorCode.INTERNAL, True
        else:
            error_code, retryable = ErrorCode.INTERNAL, False
        return error_response(
            error_code, str(exc.detail), retryable=retryable, status_code=status_code
        )
