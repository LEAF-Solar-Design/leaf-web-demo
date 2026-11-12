"""Non-relay device bridge endpoints on the shared device store."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

import deps
import device_catalog
import device_store as store
import platform_link
from device_auth import DeviceError, raw_body, require_device
from routers.tenant_mcp import _require_link_service
from runtime_posture import _production_runtime

_ROOT = Path(__file__).resolve().parents[2]
DEVICE_PUBLIC_ORIGINS = {
    "platform.leafdesign.ai": "https://platform.leafdesign.ai",
    "platform-staging.leafdesign.ai": "https://platform-staging.leafdesign.ai",
    # deploy/studio-door/{README.md,vercel.json}: the door proxies to production.
    # web/src/api.js uses same-origin API calls, so agents must use its platform origin.
    "studio.leafautomation.ai": "https://platform.leafdesign.ai",
}
_NO_STORE = {"Cache-Control": "no-store"}
_PACKAGE_LOCK = threading.Lock()
_PACKAGE = None
_LOG = logging.getLogger(__name__)


class DeviceRoute(APIRoute):
    """Keep bridge errors local, including dependency failures and dark routes."""
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request):
            try:
                if _production_runtime() and os.environ.get("LEAF_DEVICE_BRIDGE_ENABLED") != "1":
                    raise DeviceError(404, "not_found")
                return await handler(request)
            except DeviceError as exc:
                status, code = exc.status_code, exc.code
            except HTTPException as exc:
                status = exc.status_code
                code = {400: "invalid_request", 401: "unauthorized", 403: "forbidden",
                        404: "not_found", 422: "invalid_payload", 429: "rate_limited"}.get(status, "service_unavailable")
            except RequestValidationError:
                status, code = 400, "invalid_request"
            except store.EnrollmentInvalid:
                status, code = 401, "enrollment_invalid"
            except store.DeviceKeyInUse:
                status, code = 409, "device_key_in_use"
            except store.DeviceRevoked:
                status = 401 if "/agent/" in request.url.path else 409
                code = "device_revoked"
            except (store.EnrollmentLimit, store.DeviceLimit, store.QueueFull):
                status, code = 429, "rate_limited"
            except Exception as exc:
                # Exception text/tracebacks can contain credentials or raw JSON.
                _LOG.warning("device bridge unavailable exception_type=%s", type(exc).__name__)
                status, code = 503, "service_unavailable"
            return JSONResponse(status_code=status, content={"error": {
                "code": code, "message": "Device request rejected"}}, headers=_NO_STORE)

        return handle


router = APIRouter(prefix="/api/devices", route_class=DeviceRoute)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _actor(tenant):
    return "user:" + (getattr(tenant, "subject", None) or str(tenant))


def _project_owner(tenant, project_id):
    """Require a project owner or two-factor platform admin with project write access, mirroring #2248."""
    binding = platform_link.resolve_caller_binding(tenant)
    platform_link._ensure_platform_package()
    from leaf_platform import project_lifecycle

    try:
        role = project_lifecycle.require_project_role(UUID(str(tenant)), UUID(project_id),
                                                     binding.binding_id, write=True)
    except project_lifecycle.LifecycleForbidden:
        raise DeviceError(403, "forbidden") from None
    except project_lifecycle.LifecycleUnavailable:
        raise DeviceError(404, "not_found") from None
    if role != "owner" and not (
        role == "editor"
        and isinstance(tenant, deps.TenantContext)
        and getattr(tenant, "tier", None) == "admin"
        and isinstance(tenant.subject, str) and tenant.subject
        and tenant.subject in deps.admin_subjects()
    ):
        raise DeviceError(403, "forbidden")


def _project_access(tenant, project_id, *, write=False):
    if not isinstance(project_id, str) or not 1 <= len(project_id) <= 128:
        raise DeviceError(400, "invalid_request")
    try:
        org = platform_link.require_project_access(tenant, project_id, write=False)
    except (LookupError, platform_link.ProjectSessionForbidden):
        raise DeviceError(404, "not_found") from None
    if str(org) != str(tenant):
        raise DeviceError(404, "not_found")
    if write:
        try:
            _project_owner(tenant, project_id)
        except platform_link.ProjectSessionForbidden:
            raise DeviceError(403, "forbidden") from None
    return str(org)


def _device_access(tenant, device_id, *, write=False):
    device = store.get_device(device_id)
    if device is None or device["tenant_id"] != str(tenant):
        raise DeviceError(404, "not_found")
    _project_access(tenant, device["project_id"], write=write)
    return device


def _origin(request):
    if any(len(request.headers.getlist(name)) > 1 for name in ("host", "x-forwarded-host", "x-forwarded-proto")):
        raise DeviceError(400, "invalid_request")
    host = request.headers.get("x-forwarded-host", request.headers.get("host", "")).lower()
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).lower()
    if proto != "https":
        raise DeviceError(400, "invalid_request")
    origins = dict(DEVICE_PUBLIC_ORIGINS)
    if not _production_runtime():
        # Local/test extensions cannot replace a door's canonical platform origin.
        for value in os.environ.get("LEAF_DEVICE_PUBLIC_ORIGINS", "").split(","):
            origin = value.strip().lower()
            if not re.fullmatch(r"https://(?:[a-z0-9.-]+|\[[0-9a-f:]+\])(?::[0-9]{1,5})?", origin):
                continue
            try:
                parsed = urlsplit(origin)
                port = parsed.port
            except ValueError:
                continue
            if parsed.hostname and (port is None or 1 <= port <= 65535):
                origins.setdefault(parsed.netloc, origin)
    if host not in origins:
        raise DeviceError(400, "invalid_request")
    return origins[host]


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError("nonfinite JSON")


async def _body(request):
    try:
        body = json.loads((await raw_body(request)).decode("utf-8"),
                          object_pairs_hook=_pairs, parse_constant=_nonfinite)
    except (ValueError, UnicodeError, RecursionError):
        raise DeviceError(400, "invalid_request") from None
    if not isinstance(body, dict):
        raise DeviceError(400, "invalid_request")
    return body


def _validate(name, body, *, status=422, code="invalid_payload"):
    try:
        valid = device_catalog.VALIDATORS[name].is_valid(body)
        json.dumps(body, allow_nan=False)
    except (ValueError, RecursionError):
        valid = False
    if not valid:
        raise DeviceError(status, code)


def _query(request, allowed):
    items = list(request.query_params.multi_items())
    if any(key not in allowed for key, _ in items) or len({key for key, _ in items}) != len(items):
        raise DeviceError(400, "invalid_request")
    return dict(items)


def _integer(value, minimum, maximum):
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or len(value) > 10:
        raise DeviceError(400, "invalid_request")
    number = int(value)
    if not minimum <= number <= maximum:
        raise DeviceError(400, "invalid_request")
    return number


def _command(row):
    return {key: row[key] for key in ("command_id", "op", "args", "issued_at", "expires_at")}


def _audit(tenant, device_id, actor, op, target, status, body=None):
    digest = hashlib.sha256(body).hexdigest() if body is not None else None
    if not store.append_audit(str(tenant), device_id, actor, op, target, status, digest):
        raise DeviceError(503, "service_unavailable")


def _package():
    global _PACKAGE
    with _PACKAGE_LOCK:
        if _PACKAGE is None:
            source = _ROOT / "device-agent"
            spec = importlib.util.spec_from_file_location("leaf_device_package_builder", source / "build_package.py")
            builder = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(builder)
            with tempfile.TemporaryDirectory(prefix="leaf-device-release-") as output:
                digest = builder.build(source, Path(output))
                archive = (Path(output) / "package.tar.gz").read_bytes()
                installer = (Path(output) / "install.sh").read_text(encoding="utf-8")
            _PACKAGE = archive, installer, digest
        return _PACKAGE


@router.get("/agent/package.tar.gz")
def agent_package(request: Request):
    query = _query(request, {"sha256"})
    sha256 = query.get("sha256")
    if sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise DeviceError(400, "invalid_request")
    archive, _, digest = _package()
    if sha256 is not None and sha256 != digest:
        raise DeviceError(404, "not_found")
    return Response(archive, media_type="application/gzip", headers={
        "Cache-Control": "public, max-age=31536000, immutable" if sha256 is not None else "no-store",
        "ETag": '"' + digest + '"',
        "X-Leaf-Document-Sha256": digest})


@router.get("/agent/install.sh")
def installer(request: Request):
    origin = _origin(request)
    _, script, _ = _package()
    # The P4 template expects this variable. Embed the validated public origin
    # so the enrollment response needs only the one-use enrollment variable.
    script = script.replace("set -eu\n", "set -eu\nexport LEAF_SERVER_ORIGIN='" + origin + "'\n", 1)
    return Response(script, media_type="text/x-shellscript", headers=_NO_STORE)


@router.get("/packages")
def package_catalog(tenant=Depends(deps.require_tenant)):
    return {"packages": device_catalog.packages()}


@router.post("/enrollments", status_code=201)
async def enroll(request: Request, tenant=Depends(_require_link_service)):
    body = await _body(request)
    if (set(body) != {"project_id", "label"} or any(not isinstance(body[k], str)
            or not 1 <= len(body[k]) <= 128 for k in body)):
        raise DeviceError(422, "invalid_payload")
    origin = _origin(request)
    org = await run_in_threadpool(_project_access, tenant, body["project_id"], write=True)
    row, token = await run_in_threadpool(store.create_enrollment, org, body["project_id"], body["label"], _actor(tenant))
    return JSONResponse(status_code=201, content={"enrollment_id": row["enrollment_id"], "token": token,
        "expires_at": row["expires_at"], "install_command":
        f"curl -fsSL {origin}/api/devices/agent/install.sh | LEAF_ENROLL_TOKEN={token} sh"}, headers=_NO_STORE)


@router.post("/agent/register", status_code=201)
async def register(request: Request):
    body = await _body(request)
    _validate("RegisterRequest", body)
    device = await run_in_threadpool(store.consume_enrollment, body["token"], body["public_key"],
                                    body["agent_version"], body["platform"], body["hostname"])
    await run_in_threadpool(_audit, device["tenant_id"], device["device_id"], "device",
                            "device.register", device["device_id"], "ok")
    return JSONResponse(status_code=201, content={"device_id": device["device_id"], "server_time": _now()}, headers=_NO_STORE)


@router.get("/agent/poll")
async def poll(request: Request, device=Depends(require_device)):
    if await raw_body(request):
        raise DeviceError(400, "invalid_request")
    query = _query(request, {"wait"})
    wait = _integer(query.get("wait", "25"), 0, 25)
    if not await run_in_threadpool(store.touch_device, device["device_id"], device["agent_version"],
                                   device["packages"], device["free_mem_mb"] or 0):
        raise DeviceError(401, "device_revoked")
    deadline = time.monotonic() + min(wait, 25)
    while True:
        commands = await run_in_threadpool(store.lease_commands, device["device_id"])
        if commands:
            return JSONResponse(content={"commands": [_command(row) for row in commands]}, headers=_NO_STORE)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return Response(status_code=204, headers=_NO_STORE)
        await asyncio.sleep(min(0.25, remaining))


@router.post("/agent/results")
async def results(request: Request, device=Depends(require_device)):
    body = await _body(request)
    _validate("ResultsRequest", body, status=400)
    # Validate the entire batch before the first write; each completion remains
    # independently idempotent, including retries and foreign/unknown IDs.
    for result in body["results"]:
        try:
            store._validate("Result", result)
        except ValueError:
            raise DeviceError(400, "invalid_payload") from None
    accepted = 0
    for result in body["results"]:
        completed = await run_in_threadpool(store.complete_command, device["device_id"], result["command_id"], result)
        if completed:
            accepted += 1
            command = await run_in_threadpool(store.get_command, device["tenant_id"], device["device_id"], result["command_id"])
            op = command["op"]
            if result.get("error", {}).get("code") == "paused" or (op == "agent.pause" and result["status"] == "ok"):
                op = "device.paused"
            elif result["status"] == "ok":
                op = "device.active"
            await run_in_threadpool(_audit, device["tenant_id"], device["device_id"], "device", op,
                                    result["command_id"], result["status"])
    return {"accepted": accepted}


@router.post("/agent/heartbeat")
async def heartbeat(request: Request, device=Depends(require_device)):
    body = await _body(request)
    _validate("HeartbeatRequest", body)
    if len({item["package"] for item in body["packages"]}) != len(body["packages"]):
        raise DeviceError(422, "invalid_payload")
    if not await run_in_threadpool(store.touch_device, device["device_id"], body["agent_version"],
                                   body["packages"], body["free_mem_mb"]):
        raise DeviceError(401, "device_revoked")
    return {"server_time": _now()}


def _view(row):
    view = {key: row[key] for key in ("device_id", "project_id", "label", "hostname", "platform",
                                    "agent_version", "packages", "last_seen_at", "revoked_at")}
    status = "online"
    if row["last_seen_at"] is None or (datetime.now(timezone.utc) - store._date(row["last_seen_at"])).total_seconds() >= 90:
        status = "offline"
    else:
        for event in store.list_audit(row["tenant_id"], row["device_id"], 200):
            if event["op"] in ("device.paused", "device.active"):
                status = "paused" if event["op"] == "device.paused" else "online"
                break
    view.update(enrolled_at=row["created_at"], status="revoked" if row["revoked_at"] else status)
    return view


@router.get("")
def list_devices(request: Request, tenant=Depends(deps.require_tenant)):
    project = _query(request, {"project_id"}).get("project_id")
    org = _project_access(tenant, project)
    return {"devices": [_view(row) for row in store.list_devices(org, project)]}


@router.get("/{device_id}/audit")
def audit(device_id: str, request: Request, tenant=Depends(deps.require_tenant)):
    device = _device_access(tenant, device_id)
    limit = _integer(_query(request, {"limit"}).get("limit", "50"), 1, 200)
    keys = ("at", "actor", "op", "target", "status", "request_sha256")
    return {"events": [{key: row[key] for key in keys if row.get(key) is not None}
                       for row in store.list_audit(device["tenant_id"], device_id, limit)]}


@router.post("/{device_id}/commands", status_code=202)
async def enqueue(device_id: str, request: Request, tenant=Depends(_require_link_service)):
    device = await run_in_threadpool(_device_access, tenant, device_id, write=True)
    raw = await raw_body(request)
    op = None
    try:
        body = await _body(request)
        op = body.get("op")
        if device["revoked_at"]:
            raise DeviceError(409, "device_revoked")
        if set(body) != {"op", "args"}:
            raise DeviceError(400, "command_invalid")
        _validate("Command", {**body, "command_id": str(uuid4()), "issued_at": _now(),
                              "expires_at": _now()}, status=400, code="command_invalid")
        args = body["args"]
        if "package" in args:
            package = device_catalog.get_package(args["package"])
            if package is None:
                raise DeviceError(422, "package_unknown")
            if op in ("pkg.install", "pkg.upgrade") and args["version"] != package["version"]:
                raise DeviceError(422, "version_unknown")
            if op == "http.proxy" and not device_catalog.allows_proxy(package, args["method"], args["path"]):
                raise DeviceError(422, "path_denied")
        command_id = await run_in_threadpool(store.enqueue_command, device["tenant_id"], device_id, op, args, _actor(tenant))
        if command_id is None:
            raise DeviceError(404, "not_found")
    except (DeviceError, store.DeviceRevoked, store.QueueFull):
        await run_in_threadpool(_audit, device["tenant_id"], device_id, _actor(tenant),
                                op if op in store.OPS else "command.invalid", device_id, "refused", raw)
        raise
    await run_in_threadpool(_audit, device["tenant_id"], device_id, _actor(tenant), op, command_id, "ok", raw)
    return JSONResponse(status_code=202, content={"command_id": command_id}, headers=_NO_STORE)


@router.get("/{device_id}/commands/{command_id}")
def command(device_id: str, command_id: str, tenant=Depends(deps.require_tenant)):
    device = _device_access(tenant, device_id)
    row = store.get_command(device["tenant_id"], device_id, command_id)
    if row is None:
        raise DeviceError(404, "not_found")
    return {"command": _command(row), "result": row["result"]}


@router.post("/{device_id}/revoke")
async def revoke(device_id: str, request: Request, tenant=Depends(_require_link_service)):
    device = await run_in_threadpool(_device_access, tenant, device_id, write=True)
    if await raw_body(request):
        raise DeviceError(400, "invalid_request")
    await run_in_threadpool(store.revoke_device, device["tenant_id"], device_id, _actor(tenant))
    await run_in_threadpool(_audit, device["tenant_id"], device_id, _actor(tenant), "device.revoke", device_id, "ok")
    row = await run_in_threadpool(store.get_device, device_id)
    return {"revoked_at": row["revoked_at"]}
