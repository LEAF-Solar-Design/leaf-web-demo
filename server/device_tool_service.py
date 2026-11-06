"""Turn-bound device operations with a deliberately narrow result projection."""
from datetime import datetime, timezone
import json
import re
from uuid import uuid4

import deps
import device_catalog
import device_store as store
import entitlements
import platform_link
import session_store
import turn_runner
from device_auth import DeviceError
from integration_tool_service import ReadFailure, authority_headers, canonical_uuid, crypto, strict_json
from routers import devices

REQUEST_CAP = 16 * 1024
STATUS = {"invalid_input": 400, "unauthenticated": 401, "denied": 403,
          "not_found": 404, "conflict": 409, "unavailable": 503}
MESSAGES = {"invalid_input": "The request is invalid.", "unauthenticated": "Authentication is required.",
            "denied": "This operation is not allowed.", "not_found": "The device or command was not found.",
            "conflict": "The operation cannot run now.", "unavailable": "The device service is unavailable."}
WRITES = {"install_package", "ha_step", "light"}
ENTITY = re.compile(r"(?:light|group)\.[a-z0-9_]{1,64}")


def failure(code):
    return {"ok": False, "error": {"code": code, "message": MESSAGES[code]}}


def resolve_current(tenant_id, session_id, turn_id, *, write=False):
    if not deps.auth_live():
        raise ReadFailure("unauthenticated")
    authority_headers(tenant_id, session_id, turn_id)
    session = session_store.get_session(session_id)
    if (not session or session.get("session_id") != session_id
            or session.get("org_id") != tenant_id or not session.get("project_id")):
        raise ReadFailure("denied")
    project = canonical_uuid(session["project_id"])
    subject = session_store.active_turn_subject(
        session_id, turn_id, session["tenant_id"], turn_runner.turn_max_s())
    if not subject:
        raise ReadFailure("conflict")
    tenant, tier = deps.resolve_active_platform_tenant_authority(subject)
    if tenant != tenant_id:
        raise ReadFailure("denied")
    # Follow routers/agent.py's verified active-turn tier snapshot precedent.
    turn_tier = session_store.active_turn_tier(session_id, turn_id, session["tenant_id"])
    if isinstance(turn_tier, str) and turn_tier:
        tier = turn_tier
    context = deps.TenantContext(tenant_id, org_id=tenant_id, tier=tier,
                               subject=subject, authority_resolved=True)
    binding = platform_link.resolve_caller_binding(context)
    if (binding is None or binding.status != "active" or binding.external_authority != "auth0"
            or binding.external_subject != subject or str(binding.platform_tenant_id) != tenant_id):
        raise ReadFailure("denied")
    try:
        allowed = platform_link.require_project_session_access(session, context, write=write, binding=binding)
    except (platform_link.ProjectSessionForbidden, LookupError):
        raise ReadFailure("denied") from None
    if allowed is None:
        raise ReadFailure("denied")
    roles, elevated = entitlements.resolve_roles(context)
    caps = entitlements.entitlements_for(tier, roles, elevated)
    if caps.get("converse") is not True or (write and caps.get("link_service") is not True):
        raise ReadFailure("denied")
    return context, project


def normalize(operation, value):
    required = {"list": set(), "install_package": {"device_id", "package"},
                "ha_step": {"device_id", "op", "args"},
                "light": {"device_id", "action", "entity_id"},
                "light_states": {"device_id"}, "command_status": {"device_id", "command_id"}}
    if operation not in required or type(value) is not dict:
        raise ReadFailure("invalid_input")
    optional = {"color", "brightness_pct"} if operation == "light" else set()
    if not required[operation] <= value.keys() or value.keys() - required[operation] - optional:
        raise ReadFailure("invalid_input")
    for key in required[operation] - {"args"}:
        if type(value[key]) is not str or not 1 <= len(value[key]) <= 128:
            raise ReadFailure("invalid_input")
    if operation == "ha_step" and (value["op"] not in {
            "ha.onboard", "ha.flow.start", "ha.flow.step", "ha.flow.list"} or type(value["args"]) is not dict):
        raise ReadFailure("invalid_input")
    if operation == "light":
        if value["action"] not in {"turn_on", "turn_off"} or ENTITY.fullmatch(value["entity_id"]) is None:
            raise ReadFailure("invalid_input")
        if "color" in value and (type(value["color"]) is not str or re.fullmatch(r"#[0-9a-fA-F]{6}", value["color"]) is None):
            raise ReadFailure("invalid_input")
        if "brightness_pct" in value and (type(value["brightness_pct"]) is not int or not 0 <= value["brightness_pct"] <= 100):
            raise ReadFailure("invalid_input")
    return value


def _text(value):
    # Result strings are untrusted, including strings under permitted keys.
    if (type(value) is not str or len(value) > 128
            or re.search(r"eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", value)
            or re.search(r"[A-Za-z0-9_+/=-]{24,}|token|secret|bearer|authorization|password", value, re.I)
            or any(ord(c) < 32 for c in value)):
        return None
    return value


def _summary(command, result):
    data = result.get("data")
    data = data if type(data) is dict else {}
    if command["op"] == "http.proxy" and command["args"].get("method") == "GET" and command["args"].get("path") == "/api/states":
        lights = []
        body = data.get("body_json")
        for item in body if type(body) is list else []:
            if type(item) is not dict:
                continue
            entity = item.get("entity_id")
            if type(entity) is not str or not re.fullmatch(r"light\.[a-z0-9_]{1,64}", entity) or _text(entity) is None:
                continue
            attrs = item.get("attributes")
            attrs = attrs if type(attrs) is dict else {}
            light = {"entity_id": entity, "name": _text(attrs.get("friendly_name")) or entity}
            state = _text(item.get("state"))
            if state is not None:
                light["state"] = state
            rgb = attrs.get("rgb_color")
            if type(rgb) is list and len(rgb) == 3 and all(type(v) is int and 0 <= v <= 255 for v in rgb):
                light["rgb"] = rgb
            brightness = attrs.get("brightness")
            if type(brightness) is int and 0 <= brightness <= 255:
                light["brightness_pct"] = round(brightness * 100 / 255)
            lights.append(light)
            if len(lights) == 100:
                break
        return {"lights": lights}
    summary = {}
    for key in ("status", "flow_id", "step_id", "type", "title"):
        value = data.get(key, result.get(key))
        if key == "status" and type(value) is int and 100 <= value <= 599:
            summary[key] = value
        elif _text(value) is not None:
            summary[key] = value
    error = result.get("error")
    if type(error) is dict and _text(error.get("code")) is not None:
        summary["error"] = {"code": error["code"]}
    errors = data.get("errors")
    if type(errors) is dict:
        summary["errors"] = {k: v for k, v in list(errors.items())[:100]
                             if _text(k) is not None and _text(v) is not None}
    return summary


class DeviceToolService:
    def execute(self, operation, value, tenant_id, session_id, turn_id):
        try:
            value = normalize(operation, value)
            write = operation in WRITES
            tenant, project = resolve_current(tenant_id, session_id, turn_id, write=write)
            if operation == "list":
                org = devices._project_access(tenant, project, write=False)
                rows = []
                for row in store.list_devices(org, project):
                    seen = row["last_seen_at"]
                    age = (datetime.now(timezone.utc) - store._date(seen)).total_seconds() if seen else None
                    rows.append({"device_id": row["device_id"], "label": row["label"],
                                 "online": age is not None and 0 <= age < 90 and not row["revoked_at"],
                                 "last_seen_at": seen, "packages": [{
                                     "package": p["package"], "version": p["version"],
                                     "state": p["status"], "healthy": p["status"] == "running" and p.get("last_health_at") is not None
                                 } for p in row["packages"]]})
                result = {"devices": rows, "catalog": [{k: p[k] for k in ("package", "display_name", "version")}
                                                       for p in device_catalog.packages()]}
            else:
                device_id = value["device_id"]
                device = devices._device_access(tenant, device_id, write=False)
                if device["project_id"] != project:
                    raise ReadFailure("not_found")
                if write:
                    devices._project_access(tenant, project, write=True)
                if operation == "command_status":
                    row = store.get_command(str(tenant), device_id, value["command_id"])
                    if row is None:
                        raise ReadFailure("not_found")
                    state = row["state"]
                    if state not in {"queued", "leased", "done", "expired"}:
                        raise ReadFailure("unavailable")
                    if state in {"queued", "leased"} and store._date(row["expires_at"]) <= datetime.now(timezone.utc):
                        state = "expired"
                    result = {"state": state}
                    if state == "done" and type(row["result"]) is dict:
                        result.update(ok=row["result"].get("status") == "ok",
                                      summary=_summary(devices._command(row), row["result"]))
                else:
                    result = self._enqueue(operation, value, tenant, device)
            return {"ok": True, "result": result}
        except ReadFailure as exc:
            return failure(str(exc) if str(exc) in STATUS else "unavailable")
        except DeviceError as exc:
            return failure({400: "invalid_input", 401: "unauthenticated", 403: "denied",
                            404: "not_found", 409: "conflict", 422: "invalid_input"}.get(exc.status_code, "unavailable"))
        except (store.DeviceRevoked, store.QueueFull):
            return failure("conflict")
        except Exception:
            return failure("unavailable")

    def _enqueue(self, operation, value, tenant, device):
        if device["revoked_at"]:
            raise ReadFailure("conflict")
        if operation == "install_package":
            package = device_catalog.get_package(value["package"])
            if package is None:
                raise ReadFailure("invalid_input")
            op, args = "pkg.install", {"package": package["package"], "version": package["version"]}
        elif operation == "ha_step":
            op, args = value["op"], value["args"]
        else:
            op = "http.proxy"
            args = {"package": "homeassistant", "method": "GET", "path": "/api/states"}
            if operation == "light":
                body = {"entity_id": value["entity_id"]}
                if "color" in value:
                    body["rgb_color"] = [int(value["color"][i:i + 2], 16) for i in (1, 3, 5)]
                if "brightness_pct" in value:
                    body["brightness_pct"] = value["brightness_pct"]
                args.update(method="POST", path="/api/services/light/" + value["action"], body_json=body)
            package = device_catalog.get_package(args["package"])
            if package is None or not device_catalog.allows_proxy(package, args["method"], args["path"]):
                raise ReadFailure("invalid_input")
        devices._validate("Command", {"command_id": str(uuid4()), "op": op, "args": args,
                                     "issued_at": devices._now(), "expires_at": devices._now()},
                          status=400, code="command_invalid")
        actor = "user:" + tenant.subject + "#assistant"
        raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
        devices._audit(tenant, device["device_id"], actor, op, device["device_id"], "ok", raw)
        command_id = store.enqueue_command(str(tenant), device["device_id"], op, args, actor)
        if command_id is None:
            raise ReadFailure("not_found")
        return {"command_id": command_id}
