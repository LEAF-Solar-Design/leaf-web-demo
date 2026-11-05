"""Authenticated internal device tools; no enrollment or credential surface."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

import deps
import device_tool_service as service

router = APIRouter()


def _response(value):
    return JSONResponse(value, status_code=service.STATUS.get(value.get("error", {}).get("code"), 200),
                        headers={"Cache-Control": "no-store, private"})


async def _request(request: Request):
    try:
        if not deps.auth_live() or not deps._dispatch_secret_ok(request.headers.get("x-dispatch-secret")):
            raise service.ReadFailure("unauthenticated")
        names = ("x-tenant-id", "x-dispatch-secret", "x-authority-session-id", "x-authority-turn-id")
        if any(len(request.headers.getlist(name)) != 1 for name in names):
            raise service.ReadFailure("invalid_input")
        authority = service.authority_headers(*(request.headers.get(name) for name in
            ("x-tenant-id", "x-authority-session-id", "x-authority-turn-id")))
        if (len(request.headers.getlist("content-type")) != 1 or
                request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json"):
            raise service.ReadFailure("invalid_input")
        raw = bytearray()
        try:
            async for chunk in request.stream():
                if len(raw) + len(chunk) > service.REQUEST_CAP:
                    raise service.ReadFailure("invalid_input")
                raw.extend(chunk)
            try:
                value = service.strict_json(raw)
            except (ValueError, UnicodeError, RecursionError):
                raise service.ReadFailure("invalid_input") from None
        finally:
            service.crypto._scrub(raw)
        if type(value) is not dict:
            raise service.ReadFailure("invalid_input")
        return authority, value
    except service.ReadFailure as exc:
        return _response(service.failure(str(exc) if str(exc) in service.STATUS else "unavailable"))
    except Exception:
        return _response(service.failure("unavailable"))


@router.post("/internal/devices/tools/{operation}")
def execute(operation: str, parsed=Depends(_request)):
    if isinstance(parsed, JSONResponse):
        return parsed
    authority, value = parsed
    return _response(service.DeviceToolService().execute(operation, value, *authority))
