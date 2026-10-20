"""Explicit authenticated host endpoints; no general back-edge allowlist change."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

import deps
import integration_tool_service as service

router = APIRouter()


def _response(value):
    code = value.get("error", {}).get("code")
    return JSONResponse(value, status_code=service.STATUS.get(code, 200),
                        headers={"Cache-Control": "no-store, private", "Pragma": "no-cache"})


async def _request(request: Request):
    category = "invalid_input"
    try:
        if not deps.auth_live() or not deps._dispatch_secret_ok(request.headers.get("x-dispatch-secret")):
            raise service.ReadFailure("unauthenticated")
        # Reject duplicates as well as missing authority headers. No JWT,
        # body value or legacy auth-off tenant can select an alternative path.
        names = ("x-tenant-id", "x-dispatch-secret", "x-authority-session-id", "x-authority-turn-id")
        if any(len(request.headers.getlist(name)) != 1 for name in names):
            raise service.ReadFailure("invalid_input")
        authority = service.authority_headers(*(request.headers.get(name) for name in
            ("x-tenant-id", "x-authority-session-id", "x-authority-turn-id")))
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise service.ReadFailure("invalid_input")
        raw = bytearray()
        try:
            async for chunk in request.stream():
                if len(raw) + len(chunk) > service.REQUEST_CAP:
                    raise service.ReadFailure("invalid_input")
                raw.extend(chunk)
            value = service.strict_json(raw)
        finally:
            service.crypto._scrub(raw)
        return authority, value
    except service.ReadFailure as exc:
        category = str(exc) if str(exc) in service.STATUS else "unavailable"
    except Exception:
        pass
    return _response(service.failure(category))


def _execute(operation, parsed):
    if isinstance(parsed, JSONResponse):
        return parsed
    authority, value = parsed
    return _response(service.IntegrationToolService().execute(operation, value, *authority))


# FastAPI invokes these synchronous handlers in its worker pool. The custody
# callback owns asyncio.run, which cancels and closes HTTP work before returning.
@router.post("/internal/integrations/datasets")
def datasets(parsed=Depends(_request)):
    return _execute("datasets", parsed)


@router.post("/internal/integrations/query")
def query(parsed=Depends(_request)):
    return _execute("query", parsed)
