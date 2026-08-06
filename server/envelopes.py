"""
Extended result/error envelope (server CONTRACT-ADDENDUM section 10).

Every server response body carries at minimum:
    error: null | {error_code, message, retryable, retry_class, actor, next_action}
    degraded_mode: bool

The section-3 run envelope is EXTENDED (existing success fields unchanged):
    {ok, tool, version, result, overlay, timing_ms, cost, error, degraded_mode}

degraded_mode=True means: APS_LIVE was requested but the run fell back to the
pure-python path (so the UI can say "used the local fallback, not the cloud
solver").
"""
from __future__ import annotations

import logging
import secrets
from typing import Any, Dict, Optional

from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class ErrorCode:
    """Machine-readable error codes (frozen enum for the platform frontend)."""

    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    BAD_PARAMS = "BAD_PARAMS"
    APS_UNAVAILABLE = "APS_UNAVAILABLE"
    BROKER_UNREACHABLE = "BROKER_UNREACHABLE"
    WORKITEM_FAILED = "WORKITEM_FAILED"
    TIMEOUT = "TIMEOUT"
    TENANT_DISABLED = "TENANT_DISABLED"
    UNAUTHENTICATED = "UNAUTHENTICATED"    # identity absent or invalid (HTTP 401)
    FORBIDDEN = "FORBIDDEN"                # identity valid, authority insufficient (HTTP 403)
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
        UNAUTHENTICATED,
        FORBIDDEN,
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
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.GRANT_REQUIRED: 401,
    ErrorCode.ENTITLEMENT_REQUIRED: 403,
    ErrorCode.INTERNAL: 500,
    ErrorCode.TURN_IN_PROGRESS: 409,
    ErrorCode.SESSION_NOT_FOUND: 404,
    ErrorCode.LLM_QUOTA_EXHAUSTED: 429,
    ErrorCode.LLM_RATE_LIMITED: 429,
    ErrorCode.CONFIRMATION_EXPIRED: 410,
}


_NON_RETRY_GUIDANCE: Dict[str, tuple[str, str, str]] = {
    ErrorCode.UNKNOWN_TOOL: (
        "user", "after_action", "Choose an available tool and submit again."),
    ErrorCode.BAD_PARAMS: (
        "user", "after_action", "Review the inputs, correct them, and submit again."),
    ErrorCode.APS_UNAVAILABLE: (
        "operator", "never", "Contact support so the CAD service can be restored."),
    ErrorCode.BROKER_UNREACHABLE: (
        "operator", "never", "Contact support so the execution broker can be restored."),
    ErrorCode.WORKITEM_FAILED: (
        "operator", "never", "Contact support with the job details for review."),
    ErrorCode.TIMEOUT: (
        "operator", "never", "Contact support with the timed-out job details."),
    ErrorCode.TENANT_DISABLED: (
        "workspace_admin", "after_action", "Enable the workspace before trying again."),
    ErrorCode.UNAUTHENTICATED: (
        "user", "after_action", "Sign in, then repeat the request."),
    ErrorCode.FORBIDDEN: (
        "workspace_admin", "after_action", "Ask a workspace admin to grant the required access."),
    ErrorCode.GRANT_REQUIRED: (
        "user", "after_action", "Connect an approved assistant account, then retry."),
    ErrorCode.ENTITLEMENT_REQUIRED: (
        "workspace_admin", "after_action", "Enable this capability for the workspace, then retry."),
    ErrorCode.QUOTA_EXCEEDED: (
        "workspace_admin", "after_action", "Raise the workspace limit or wait for it to reset."),
    ErrorCode.INTERNAL: (
        "operator", "never", "Contact support with the displayed error identifier."),
    ErrorCode.TURN_IN_PROGRESS: (
        "user", "after_action", "Wait for the active turn to finish, then retry."),
    ErrorCode.SESSION_NOT_FOUND: (
        "user", "after_action", "Refresh the workspace to start a new session."),
    ErrorCode.LLM_QUOTA_EXHAUSTED: (
        "workspace_admin", "after_action", "Restore assistant quota for the workspace, then retry."),
    ErrorCode.LLM_RATE_LIMITED: (
        "service", "after_action", "Wait a short time, then retry the request."),
    ErrorCode.CONFIRMATION_EXPIRED: (
        "user", "after_action", "Request a new confirmation and approve it."),
}

_RETRY_GUIDANCE: Dict[str, tuple[str, str, str]] = {
    ErrorCode.QUOTA_EXCEEDED: (
        "user", "backoff", "Wait for the workspace limit to reset, then retry."),
    ErrorCode.TURN_IN_PROGRESS: (
        "user", "backoff", "Wait for the active turn to finish, then retry."),
    ErrorCode.LLM_QUOTA_EXHAUSTED: (
        "workspace_admin", "after_action", "Restore assistant quota for the workspace, then retry."),
    ErrorCode.CONFIRMATION_EXPIRED: (
        "user", "after_action", "Request a new confirmation and approve it."),
}


def error_obj(error_code: str, message: str, retryable: bool) -> Dict[str, Any]:
    assert error_code in ErrorCode.ALL, f"unknown error_code {error_code!r}"
    can_retry = bool(retryable)
    if can_retry:
        actor, retry_class, next_action = _RETRY_GUIDANCE.get(
            error_code,
            ("service", "backoff", "Wait a short time, then retry the request."),
        )
    else:
        actor, retry_class, next_action = _NON_RETRY_GUIDANCE[error_code]
    return {
        "error_code": error_code,
        "message": str(message),
        "retryable": can_retry,
        "retry_class": retry_class,
        "actor": actor,
        "next_action": next_action,
    }


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
        # Never echo the REJECTED VALUE. pydantic carries it under `input`, and a
        # bring-your-own credential (routers/sessions.py MessageRequest's
        # `credential_grant`) is a validated field — so a wrongly-shaped grant
        # would mirror the caller's token into this body, and from there into
        # access logs and error monitoring. Body parsing runs BEFORE that router's
        # TLS gate, so it would leak over plain http too.
        #
        # ALLOWLIST, not denylist: keep exactly loc/msg/type — which field and
        # why — and drop everything else. `input` is the known offender, but
        # pydantic also carries `ctx`, whose contents vary by validator and can
        # embed the rejected value; a denylist would silently start leaking the
        # day a new key appears. (sol-critic review of PR #117, non-blocking 3.)
        safe = [{k: err[k] for k in ("loc", "msg", "type") if k in err}
                for err in exc.errors()]
        return error_response(ErrorCode.BAD_PARAMS, str(safe), retryable=False, status_code=422)

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
        if status_code == 401:
            error_code, retryable = ErrorCode.UNAUTHENTICATED, False
        elif status_code in (400, 404, 422):
            # 400/404: bad or unresolvable caller input (missing/invalid header,
            # unknown resource id under 404-not-403 isolation). 422: pydantic/
            # explicit body-validation failure — same "fix your request" family.
            error_code, retryable = ErrorCode.BAD_PARAMS, False
        elif status_code == 403:
            error_code, retryable = ErrorCode.FORBIDDEN, False
        elif status_code == 503:
            # Fail-closed-when-unconfigured (e.g. offboard with no admin token
            # set): retryable once an operator fixes the deployment config.
            error_code, retryable = ErrorCode.INTERNAL, True
        else:
            error_code, retryable = ErrorCode.INTERNAL, False
        return error_response(
            error_code, str(exc.detail), retryable=retryable, status_code=status_code
        )

    # Registered LAST: the app-wide catch-all. Any route that raises something
    # neither guarded in-route nor covered above used to escape as a RAW
    # framework 500 -- unshaped (no {ok,error,degraded_mode} body the frontend
    # can parse) and inconsistently logged. Now every unhandled exception logs
    # with its traceback and answers in the section-10 envelope.
    #
    # Ordering note: registration order is NOT what keeps this from shadowing
    # the specific handlers above. Starlette dispatches Exception/500 to
    # ServerErrorMiddleware (the OUTERMOST layer) and everything else to the
    # inner ExceptionMiddleware, so HTTPException and RequestValidationError
    # are handled before this is ever consulted. Registering last simply keeps
    # the reading order honest.
    @app.exception_handler(Exception)
    async def _unhandled_handler(request, exc):  # noqa: ANN001
        # Correlation ID. Suppressing the detail below (correctly) leaves the
        # caller holding a bare "internal server error", and the log line carried
        # only method + path -- so an operator handed that string by a user had
        # no way to find WHICH traceback it came from among every other 500 on
        # the same route. This token is the join key: same value in the log line
        # and in the response.
        #
        # It leaks nothing. The ID is random and opaque -- it IDENTIFIES the log
        # line rather than describing the fault, and is generated here rather
        # than derived from the exception, so it cannot carry request or
        # exception content. Nothing else about what the client sees widens.
        #
        # Contract-legal WITHOUT a new field (contract/CONTRACT.md §10 +
        # server/envelope_schema.json): §10 REQUIRES {error_code, message,
        # retryable} and freezes the error_code ENUM, while `message` is declared
        # only as `{"type": "string"}` with no further constraint. Carrying the ID
        # inside it therefore leaves the envelope's key set byte-identical and
        # needs no additive field. (§10 permits additive extension -- the schema
        # has no `additionalProperties: false` -- so a new field would also have
        # been legal; it is simply not needed, and not widening the shape is the
        # cheaper change for every existing consumer.)
        #
        # 8 bytes, not 4. At 32 bits the collision probability passes 1% within
        # ~10k IDs, and CloudWatch pools logs across every task and lifetime, so a
        # 4-byte token could point an operator at two different tracebacks. 64
        # bits keeps the join key unique at any volume this service will see.
        error_id = secrets.token_hex(8)
        # Log the FULL detail (type, message, traceback) server-side...
        logger.exception(
            "unhandled exception [error_id=%s] serving %s %s: %s",
            error_id,
            getattr(request, "method", "?"),
            getattr(getattr(request, "url", None), "path", "?"),
            exc,
        )
        # ...but do NOT echo str(exc) to the caller. An arbitrary in-flight
        # exception can carry a filesystem path, a SQL fragment, or credential
        # material (same leak class the validation handler above refuses to
        # echo). The client gets the machine-readable code plus the opaque ID;
        # the operator gets the detail from the log.
        return error_response(
            ErrorCode.INTERNAL,
            f"internal server error (error_id: {error_id})",
            retryable=False,
            status_code=500,
        )
