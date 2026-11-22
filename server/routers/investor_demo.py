"""Public staging-only investor session exchange."""
from datetime import datetime, timezone
import ipaddress
import json
import re
import sys
import time

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

import investor_demo as demo


router = APIRouter()
PATH = "/api/site/investor-demo/session"
CREDENTIALS = {b"authorization", b"x-demo-session", b"x-guest-session", b"x-dispatch-secret"}
_NO_STORE = {"Cache-Control": "no-store"}
_LIMIT = 1024
_BODY_TIMEOUT_S = 5
_guard = demo.ExchangeGuard()


def _error(code, detail):
    return JSONResponse({"detail": detail}, status_code=code, headers=_NO_STORE)


def credential_refusal():
    if not demo.enabled():
        return _error(404, "not found")
    return _error(400, "credentials are not accepted")


async def _bounded_body(request):
    body = bytearray()
    async for chunk in request.stream():
        if len(chunk) > _LIMIT - len(body):
            return None
        body.extend(chunk)
    return body


def _token_object(pairs):
    if len(pairs) != 1 or pairs[0][0] != "token" or not isinstance(pairs[0][1], str):
        raise ValueError
    return dict(pairs)


def _client_key(request):
    addresses = request.headers.getlist("cf-connecting-ip")
    if len(addresses) == 1 and len(addresses[0]) <= 45 and "%" not in addresses[0]:
        try:
            return str(ipaddress.ip_address(addresses[0]))
        except ValueError:
            pass
    return request.client.host if request.client and request.client.host else "unknown"


@router.post(PATH)
async def exchange(request: Request):
    if not demo.enabled():
        return _error(404, "not found")
    if any(k.lower() in CREDENTIALS for k, _ in request.scope["headers"]):
        return credential_refusal()
    if (request.headers.getlist("host") != [demo.HOST]
            or request.headers.getlist("origin") not in ([], [demo.ORIGIN])):
        return _error(403, "origin refused")

    sizes = request.headers.getlist("content-length")
    if sizes:
        if len(sizes) != 1 or not re.fullmatch(r"[0-9]+", sizes[0]):
            return _error(400, "invalid content length")
        size = sizes[0].lstrip("0") or "0"
        if len(size) > 4 or int(size) > _LIMIT:
            return _error(413, "body too large")
    if not _guard.allow_client(_client_key(request), time.monotonic()):
        return _error(429, "too many requests")
    try:
        with anyio.fail_after(_BODY_TIMEOUT_S):
            body = await _bounded_body(request)
        if body is None:
            return _error(413, "body too large")
        if sizes and int(size) != len(body):
            return _error(400, "invalid content length")
        obj = json.loads(body.decode("utf-8"), object_pairs_hook=_token_object)
        if not isinstance(obj, dict):
            raise ValueError
    except TimeoutError:
        return _error(408, "request body timed out")
    except (ValueError, RecursionError, ClientDisconnect):
        return _error(400, "invalid request body")

    now = int(time.time())
    try:
        link = demo.verify_link_token(obj["token"], now=now)
    except demo.InvalidLink:
        return _error(401, "invalid or expired link")
    if not _guard.allow_verified(time.monotonic()):
        return _error(429, "too many requests")
    if not await run_in_threadpool(demo.binding_ok):
        return _error(503, "demo workspace unavailable")
    try:
        token, exp, jti = demo.mint_session(link, now=now)
    except Exception:
        return _error(503, "demo workspace unavailable")
    expires_at = datetime.fromtimestamp(exp, timezone.utc).isoformat()
    try:
        await run_in_threadpool(sys.stderr.write,
                                f"[leaf-demo] event=investor_demo_session_issued session={jti[:8]} expires={expires_at}\n")
    except Exception:
        pass
    return JSONResponse({
        "session": token, "expires_at": expires_at,
        "org_id": str(demo.ORG_ID), "drawing": "rooftop_demo",
    }, headers=_NO_STORE)
