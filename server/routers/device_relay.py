"""Tenant-managed relay credentials and the bounded device HTTP relay."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
import hashlib
import json
import threading
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

import deps
import device_catalog
import device_store as store
from device_auth import DeviceError
from routers import devices

router = APIRouter(route_class=devices.DeviceRoute)
MAX_BODY = 256 * 1024
WAIT_SECONDS = 8.0
POLL_SECONDS = 0.1
_BUCKETS = OrderedDict()
_BUCKET_LOCK = threading.Lock()
_TOKEN_FIELDS = ("token_id", "package", "label", "created_at", "revoked_at", "last_used_at")


def _tokens(tenant_id, device_id):
    # P1 exposes creation/resolution/revocation but no list operation. Keep this
    # read on its shared backend and explicitly scope it by tenant AND device.
    return store._run("id:" + device_id, lambda sql: [
        store._row(row) for row in sql.execute(
            "SELECT token_id,package,label,created_at,revoked_at,last_used_at"
            " FROM device_relay_tokens WHERE tenant_id=? AND device_id=?"
            " ORDER BY created_at,token_id", (str(tenant_id), device_id)).fetchall()])


@router.post("/api/devices/{device_id}/relay-tokens", status_code=201)
async def mint(device_id: str, request: Request, tenant=Depends(devices._require_link_service)):
    device = await run_in_threadpool(devices._device_access, tenant, device_id, write=True)
    raw = b""
    try:
        body = await devices._body(request)
        raw = request.state.device_body
        if (set(body) != {"package", "label"} or not isinstance(body["package"], str)
                or not isinstance(body["label"], str) or not 1 <= len(body["label"]) <= 128):
            raise DeviceError(422, "invalid_payload")
        if device["revoked_at"]:
            raise DeviceError(409, "device_revoked")
        package = device_catalog.get_package(body["package"])
        if package is None or not package["relay_allow"]:
            raise DeviceError(422, "package_unknown")
        if not any(item["package"] == body["package"] for item in device["packages"]):
            raise DeviceError(422, "package_not_installed")
        origin = devices._origin(request)
        try:
            created = await run_in_threadpool(store.create_relay_token, str(tenant), device_id,
                body["package"], body["label"], devices._actor(tenant))
        except store.RelayTokenLimit:
            raise DeviceError(429, "rate_limited") from None
        if created is None:
            raise DeviceError(404, "not_found")
        row, secret = created
    except (DeviceError, store.DeviceRevoked):
        await run_in_threadpool(devices._audit, str(tenant), device_id, devices._actor(tenant),
                                "relay.create", device_id, "refused", raw)
        raise
    await run_in_threadpool(devices._audit, str(tenant), device_id, devices._actor(tenant),
                            "relay.create", row["token_id"], "ok", raw)
    return JSONResponse(status_code=201, content={"token_id": row["token_id"], "relay_token": secret,
        "relay_origin": origin, "path_prefix": "/api/device-relay/" + row["package"]}, headers=devices._NO_STORE)


@router.get("/api/devices/{device_id}/relay-tokens")
async def list_tokens(device_id: str, tenant=Depends(deps.require_tenant)):
    await run_in_threadpool(devices._device_access, tenant, device_id)
    rows = await run_in_threadpool(_tokens, str(tenant), device_id)
    await run_in_threadpool(devices._audit, str(tenant), device_id, devices._actor(tenant),
                            "relay.list", device_id, "ok")
    return JSONResponse(content={"tokens": [{key: row[key] for key in _TOKEN_FIELDS} for row in rows]},
                        headers=devices._NO_STORE)


@router.delete("/api/devices/{device_id}/relay-tokens/{token_id}", status_code=204)
async def revoke(device_id: str, token_id: str, request: Request,
                 tenant=Depends(devices._require_link_service)):
    await run_in_threadpool(devices._device_access, tenant, device_id, write=True)
    rows = await run_in_threadpool(_tokens, str(tenant), device_id)
    if not any(row["token_id"] == token_id for row in rows):
        raise DeviceError(404, "not_found")
    if await devices.raw_body(request):
        raise DeviceError(400, "invalid_request")
    if not await run_in_threadpool(store.revoke_relay_token, str(tenant), token_id):
        raise DeviceError(404, "not_found")
    await run_in_threadpool(devices._audit, str(tenant), device_id, devices._actor(tenant),
                            "relay.revoke", token_id, "ok")
    return Response(status_code=204, headers=devices._NO_STORE)


def _openapi(origin):
    entity = {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]}
    transition = {"type": "number", "minimum": 0, "maximum": 60}
    states = {"description": "Home Assistant states", "content": {"application/json": {
        "schema": {"type": "array", "items": {"type": "object", "additionalProperties": True}}}}}

    def operation(operation_id, properties=None):
        result = {"operationId": operation_id, "security": [{"bearer": []}], "responses": {"200": states}}
        if properties is not None:
            result["requestBody"] = {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["entity_id"], "additionalProperties": False,
                "properties": {"entity_id": entity, **properties}}}}}
        return result

    prefix = "/api/device-relay/homeassistant/api/"
    return {"openapi": "3.0.3", "info": {"title": "Leaf Home Assistant relay", "version": "1.0.0"},
        "servers": [{"url": origin}], "components": {"securitySchemes": {
            "bearer": {"type": "http", "scheme": "bearer"}}}, "paths": {
            prefix + "states": {"get": operation("listStates")},
            prefix + "services/light/turn_on": {"post": operation("lightTurnOn", {
                "brightness_pct": {"type": "number", "minimum": 0, "maximum": 100},
                "rgb_color": {"type": "array", "minItems": 3, "maxItems": 3,
                              "items": {"type": "integer", "minimum": 0, "maximum": 255}},
                "color_temp_kelvin": {"type": "integer", "minimum": 2000, "maximum": 6500},
                "transition": transition})},
            prefix + "services/light/turn_off": {"post": operation("lightTurnOff", {"transition": transition})},
            prefix + "services/scene/turn_on": {"post": operation("sceneTurnOn", {})}}}


@router.get("/api/devices/relay-openapi/homeassistant.json")
def relay_openapi(request: Request):
    body = json.dumps(_openapi(devices._origin(request)), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return Response(body, media_type="application/json", headers={
        "X-Leaf-Document-Sha256": hashlib.sha256(body).hexdigest()})


def _take_token(token_id):
    with _BUCKET_LOCK:
        now = time.monotonic()
        available, previous = _BUCKETS.pop(token_id, (20.0, now))
        available = min(20.0, available + max(0.0, now - previous) * 10.0)
        allowed = available >= 1.0
        _BUCKETS[token_id] = (available - 1.0 if allowed else available, now)
        if len(_BUCKETS) > 10_000:
            _BUCKETS.popitem(last=False)
        return allowed


async def _relay_body(request, digest):
    body = bytearray()
    async for chunk in request.stream():
        digest.update(chunk)
        if len(body) + len(chunk) > MAX_BODY:
            raise DeviceError(413, "payload_too_large")
        body.extend(chunk)
    if not body:
        return None
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json" and not (media_type.startswith("application/") and media_type.endswith("+json")):
        raise DeviceError(415, "json_required")
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=devices._pairs, parse_constant=devices._nonfinite)
        json.dumps(value, allow_nan=False)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise DeviceError(400, "invalid_request") from None


def _proxy_response(result):
    if result["status"] != "ok":
        return JSONResponse(status_code=502, content={"error": result["error"]["code"]}, headers=devices._NO_STORE)
    data = result.get("data")
    if not isinstance(data, dict) or type(data.get("status")) is not int or not 100 <= data["status"] <= 599:
        raise DeviceError(502, "upstream_error")
    status = data["status"]
    if status in (204, 304):
        return Response(status_code=status, media_type="application/json", headers=devices._NO_STORE)
    # P4 agents emit body_json; P3 callers may supply the data.body envelope.
    field = "body" if "body" in data else "body_json"
    if field not in data:
        raise DeviceError(502, "upstream_error")
    try:
        body = json.dumps(data[field], ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        raise DeviceError(502, "upstream_error") from None
    if len(body) > 1_048_576:
        raise DeviceError(502, "upstream_error")
    return Response(body, status_code=status, media_type="application/json", headers=devices._NO_STORE)


@router.api_route("/api/device-relay/{package}/{path:path}",
                  methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"])
async def relay(package: str, path: str, request: Request):
    authorization = request.headers.get("authorization", "").split()
    if (len(request.headers.getlist("authorization")) != 1 or len(authorization) != 2
            or authorization[0].lower() != "bearer"):
        raise DeviceError(401, "relay_token_invalid")
    token = await run_in_threadpool(store.resolve_relay_token, authorization[1])
    if token is None:
        raise DeviceError(401, "relay_token_invalid")
    actor, digest, audit_status = "relay:" + token["token_id"], hashlib.sha256(), "error"
    path = "/" + path
    target = (request.method + " " + path)[:256]
    try:
        body = await _relay_body(request, digest)
        if token["package"] != package:
            raise DeviceError(403, "relay_token_mismatch")
        if not _take_token(token["token_id"]):
            raise DeviceError(429, "rate_limited")
        catalog = device_catalog.get_package(package)
        raw_path = request.scope.get("raw_path", b"").lower()
        if (catalog is None or b"%" in raw_path
                or not device_catalog.allows_proxy(catalog, request.method, path)):
            raise DeviceError(403, "relay_path_denied")
        items = list(request.query_params.multi_items())
        if any(len(key) > 128 for key, _ in items):
            raise DeviceError(403, "relay_path_denied")
        if any(not key for key, _ in items) or len({key for key, _ in items}) != len(items):
            raise DeviceError(400, "invalid_request")
        device = await run_in_threadpool(store.get_device, token["device_id"])
        if device is None or device["revoked_at"]:
            raise DeviceError(401, "relay_token_invalid")
        if (device["last_seen_at"] is None or
                (datetime.now(timezone.utc) - store._date(device["last_seen_at"])).total_seconds() >= 90):
            raise DeviceError(503, "device_offline")
        args = {"package": package, "method": request.method, "path": path, "query": dict(items), "body_json": body}
        command_id = await run_in_threadpool(store.enqueue_command, token["tenant_id"], token["device_id"],
                                            "http.proxy", args, actor, ttl_s=15)
        if command_id is None:
            raise DeviceError(401, "relay_token_invalid")
        deadline = time.monotonic() + WAIT_SECONDS
        while True:
            command = await run_in_threadpool(store.get_command, token["tenant_id"], token["device_id"], command_id)
            if command is not None and command["result"] is not None:
                response = _proxy_response(command["result"])
                audit_status = "ok" if response.status_code < 400 else "error"
                return response
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeviceError(504, "device_timeout")
            await asyncio.sleep(min(POLL_SECONDS, remaining))
    except DeviceError as exc:
        audit_status = "refused" if exc.status_code < 500 else "error"
        raise
    except store.DeviceRevoked:
        audit_status = "refused"
        raise DeviceError(401, "relay_token_invalid") from None
    except store.QueueFull:
        audit_status = "refused"
        raise DeviceError(429, "rate_limited") from None
    finally:
        if not await run_in_threadpool(store.append_audit, token["tenant_id"], token["device_id"], actor,
                                       "http.proxy", target, audit_status, digest.hexdigest()):
            raise DeviceError(503, "service_unavailable")
