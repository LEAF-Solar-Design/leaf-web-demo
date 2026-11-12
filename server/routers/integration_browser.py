"""Finite same-origin transport for verified service-browser dataset views.

The session middleware owns cookie custody. Canonical upstream handlers own
project authority. No caller can select a destination or a provider operation.
"""
from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

import deps
import service_browser_sessions as sessions
from integration_config import IntegrationConfigError, load_integration_config
from routers.service_browser_auth import joint_session

DEADLINE_SECONDS = 15
RESPONSE_BYTES = 1024 * 1024
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_SLUG = r"[a-z0-9]+(?:[-_][a-z0-9]+)*"
_VERSION = r"[1-9][0-9]{0,9}"
_JSON = re.compile(r'application/json(?:\s*;\s*charset\s*=\s*(?:utf-8|"utf-8"))?\s*\Z', re.I)
_ERRORS = {
    "WORKSPACE_AUTH_REQUIRED", "WORKSPACE_TOKEN_REQUIRED", "WORKSPACE_SCOPE_DENIED",
    "WORKSPACE_INVALID_INPUT", "WORKSPACE_CONFIG_REQUIRED", "WORKSPACE_UNAVAILABLE",
    "WORKSPACE_CONFLICT", "WORKSPACE_INVALID_RESPONSE", "WORKSPACE_DATABASE_UNAVAILABLE",
    "WORKSPACE_ORIGIN_DENIED", "WORKSPACE_BODY_TOO_LARGE", "WORKSPACE_JSON_INVALID",
    "WORKSPACE_JSON_REQUIRED", "WORKSPACE_NOT_FOUND", "WORKSPACE_METHOD_NOT_ALLOWED",
    "INTEGRATION_INVALID_INPUT", "INTEGRATION_NOT_FOUND", "INTEGRATION_FORBIDDEN",
    "INTEGRATION_CONFLICT", "INTEGRATION_UNSUPPORTED_OPERATION", "INTEGRATION_UNAVAILABLE",
}


def _error(status, code, correlation=None):
    correlation = correlation or str(uuid4())
    return JSONResponse({"error": {"code": code, "message": "Integration request could not complete.",
                                  "correlationId": correlation}},
                        status_code=status, headers={"Cache-Control": "private, no-store",
                                                    "X-Correlation-ID": correlation})


class _Failure(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


class _BridgeRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def guarded(request):
            try:
                return await handler(request)
            except HTTPException as exc:
                return _error(exc.status_code, "WORKSPACE_AUTH_REQUIRED" if exc.status_code == 401
                              else "WORKSPACE_SCOPE_DENIED")
        return guarded


router = APIRouter(route_class=_BridgeRoute)


def _one(request, name, required=False):
    values = request.headers.getlist(name)
    if len(values) > 1 or (required and len(values) != 1):
        raise _Failure(403, "WORKSPACE_SCOPE_DENIED")
    return values[0] if values else None


def _positive(value):
    return bool(re.fullmatch(_VERSION, value or "")) and int(value) <= 2147483647


def _route(request, project_id, resource):
    if not re.fullmatch(_UUID, project_id) or b"%" in request.scope.get("raw_path", b""):
        raise _Failure(400, "WORKSPACE_INVALID_INPUT")
    if resource == "scope":
        methods, query = {"GET"}, set()
    elif resource == "integrations/dataset-imports":
        methods, query = {"POST"}, set()
    elif resource == "integrations/dataset-derivations/capabilities":
        methods, query = {"GET"}, set()
    elif resource == "integrations/dataset-derivations":
        methods, query = {"POST"}, set()
    elif resource in {"integrations/datasets", "integrations/elements"}:
        methods = {"GET"} if resource.endswith("datasets") else {"GET", "POST"}
        query = {"limit", "after"} if request.method == "GET" else set()
    elif re.fullmatch(rf"integrations/(?:dataset-imports|datasets)/({_SLUG})/versions/({_VERSION})", resource):
        methods, query = {"GET"}, {"revision"}
    elif re.fullmatch(rf"integrations/datasets/({_SLUG})/versions/({_VERSION})/query", resource):
        methods, query = {"POST"}, set()
    elif re.fullmatch(rf"integrations/elements/({_SLUG})", resource):
        methods, query = {"GET", "PUT"}, {"revision"} if request.method == "GET" else set()
    else:
        raise _Failure(404, "WORKSPACE_NOT_FOUND")
    if request.method not in methods:
        raise _Failure(405, "WORKSPACE_METHOD_NOT_ALLOWED")
    parts = resource.split("/")
    if any(len(part) > 128 for part in parts) or (
            "versions" in parts and not _positive(parts[parts.index("versions") + 1])):
        raise _Failure(400, "WORKSPACE_INVALID_INPUT")
    pairs = list(request.query_params.multi_items())
    if len(pairs) != len(dict(pairs)) or any(key not in query for key, _ in pairs):
        raise _Failure(400, "WORKSPACE_INVALID_INPUT")
    for key, value in pairs:
        if key in {"limit", "revision"} and (not _positive(value) or (key == "limit" and int(value) > 100)):
            raise _Failure(400, "WORKSPACE_INVALID_INPUT")
        if key == "after" and not re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", value):
            raise _Failure(400, "WORKSPACE_INVALID_INPUT")
    return f"/api/v1/projects/{project_id}/{resource}" + ("?" + urlencode(pairs) if pairs else "")


def _authority(request, tenant):
    # This trusted state can only be produced by the joint cookie/selector
    # middleware. A direct bearer, guest token or tenant hint is insufficient.
    joint_session(request, tenant)
    cfg = sessions.config()
    sessions.local_request(request, cfg, origin_required=request.method != "GET")
    for name in ("authorization", "cookie", "host", "origin", "x-leaf-integration-origin"):
        _one(request, name, required=name in {"authorization", "cookie", "host", "x-leaf-integration-origin"})
    if any(request.headers.getlist(name) for name in ("x-dispatch-secret", "x-guest-session", "x-demo-session")):
        raise _Failure(403, "WORKSPACE_SCOPE_DENIED")
    settings = load_integration_config()
    # This is a non-authoritative build-binding assertion, never a destination.
    # Only the server-owned origin below is used to construct an upstream URL.
    if request.headers["x-leaf-integration-origin"] != settings.api_origin:
        raise _Failure(503, "WORKSPACE_CONFIG_REQUIRED")
    return settings.api_origin, request.headers["authorization"]


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _json(raw):
    def invalid(_):
        raise ValueError()
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=invalid)
    json.dumps(value, allow_nan=False)
    return value


async def _body(request, resource):
    cap = (65536 if resource == "integrations/dataset-imports" else
           163840 if resource == "integrations/dataset-derivations" else 262144)
    if request.method == "GET":
        cap = 0
    elif not _JSON.fullmatch(_one(request, "content-type", required=True) or ""):
        raise _Failure(415, "WORKSPACE_JSON_REQUIRED")
    length = _one(request, "content-length")
    if length is not None and (not re.fullmatch(r"[0-9]+", length) or len(length) > 12 or int(length) > cap):
        raise _Failure(413, "WORKSPACE_BODY_TOO_LARGE")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > cap:
            raise _Failure(413, "WORKSPACE_BODY_TOO_LARGE")
        raw.extend(chunk)
    if request.method != "GET":
        try:
            _json(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise _Failure(400, "WORKSPACE_JSON_INVALID") from None
    return bytes(raw) if request.method != "GET" else None


def _client():
    return httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=DEADLINE_SECONDS)


def _scope(value, project_id, subject):
    fields = {"tenantId", "projectId", "platformUserId", "bindingId", "externalAuthority", "externalSubject", "projectRole"}
    if (type(value) is not dict or set(value) != fields or value.get("projectId") != project_id
            or value.get("externalSubject") != subject or value.get("externalAuthority") != "auth0"
            or value.get("projectRole") not in {"owner", "editor", "reviewer", "read_only"}):
        raise _Failure(502, "WORKSPACE_INVALID_RESPONSE")
    for key in ("tenantId", "projectId", "platformUserId", "bindingId"):
        if type(value[key]) is not str or not re.fullmatch(_UUID, value[key]):
            raise _Failure(502, "WORKSPACE_INVALID_RESPONSE")


async def _forward(request, project_id, resource, tenant):
    path = _route(request, project_id, resource)
    origin, authorization = _authority(request, tenant)
    body = await _body(request, resource)
    headers = {"Authorization": authorization, "Accept": "application/json", "Accept-Encoding": "identity"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    match = _one(request, "if-match")
    if request.method == "PUT":
        if not _positive(match):
            raise _Failure(400, "WORKSPACE_INVALID_INPUT")
        headers["If-Match"] = match
    elif match is not None:
        raise _Failure(400, "WORKSPACE_INVALID_INPUT")
    async with _client() as client:
        async with client.stream(request.method, origin + path, headers=headers, content=body) as upstream:
            if (300 <= upstream.status_code < 400 or upstream.headers.get("content-encoding", "identity") != "identity"
                    or not _JSON.fullmatch(upstream.headers.get("content-type", ""))):
                raise _Failure(502, "WORKSPACE_INVALID_RESPONSE")
            length = upstream.headers.get("content-length")
            if length is not None and (not re.fullmatch(r"[0-9]+", length) or len(length) > 12 or int(length) > RESPONSE_BYTES):
                raise _Failure(502, "WORKSPACE_INVALID_RESPONSE")
            raw = bytearray()
            async for chunk in upstream.aiter_bytes():
                if len(raw) + len(chunk) > RESPONSE_BYTES:
                    raise _Failure(502, "WORKSPACE_INVALID_RESPONSE")
                raw.extend(chunk)
            if length is not None and int(length) != len(raw):
                raise _Failure(502, "WORKSPACE_INVALID_RESPONSE")
            try:
                value = _json(raw)
            except (ValueError, UnicodeError, RecursionError):
                raise _Failure(502, "WORKSPACE_INVALID_RESPONSE") from None
            if not 200 <= upstream.status_code < 300:
                detail = value.get("error") if type(value) is dict else None
                code = detail.get("code") if type(detail) is dict else None
                correlation = detail.get("correlationId") if type(detail) is dict else None
                if type(correlation) is not str or not re.fullmatch(_UUID, correlation):
                    correlation = None
                return _error(upstream.status_code if 400 <= upstream.status_code <= 599 else 502,
                              code if type(code) is str and code in _ERRORS else "WORKSPACE_UNAVAILABLE", correlation)
            if resource == "scope":
                _scope(value, project_id, tenant.subject)
            return JSONResponse(value, status_code=upstream.status_code,
                                headers={"Cache-Control": "private, no-store"})


async def _handle(request, project_id, resource, tenant):
    try:
        async with asyncio.timeout(DEADLINE_SECONDS):
            return await _forward(request, project_id, resource, tenant)
    except _Failure as exc:
        return _error(exc.status, exc.code)
    except HTTPException:
        return _error(403, "WORKSPACE_SCOPE_DENIED")
    except IntegrationConfigError:
        return _error(503, "WORKSPACE_CONFIG_REQUIRED")
    except (TimeoutError, httpx.TimeoutException):
        return _error(504, "WORKSPACE_UNAVAILABLE")
    except (httpx.HTTPError, ValueError):
        return _error(502, "WORKSPACE_UNAVAILABLE")


@router.get("/api/v1/projects/{project_id}/scope")
async def scope(project_id: str, request: Request, tenant=Depends(deps.require_tenant)):
    return await _handle(request, project_id, "scope", tenant)


@router.api_route("/api/v1/projects/{project_id}/integrations/{resource:path}", methods=["GET", "POST", "PUT"])
async def integration(project_id: str, resource: str, request: Request, tenant=Depends(deps.require_tenant)):
    return await _handle(request, project_id, "integrations/" + resource, tenant)
