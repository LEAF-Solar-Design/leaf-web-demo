"""Small WSGI API surface. There is intentionally no /v1/invoke route."""
from __future__ import annotations

import json
import hmac
from datetime import datetime

from .accounting import AccountingError, AccountingService
from .service import ControlPlaneError


_HOST_LIFECYCLE_PATHS = {
    "/v1/hosts/register",
    "/v1/hosts/readiness",
    "/v1/hosts/heartbeat",
}

_BODY_FIELDS = {
    "session": {"tenant_id", "session_id", "effective_catalog_digest", "artifact", "drawing_context"},
    "renew": {"binding_epoch"},
    "release": {"reason"},
    "invalidate": {"reason", "binding_epoch"},
    "register": {"executor_id", "endpoint", "slot_ids"},
    "readiness": {"executor_id", "slot_id", "ready", "code_digest", "runtime_digest"},
    "heartbeat": {"executor_id"},
    "drain": {"deadline"},
    "accounting": {"invocation_id", "tenant_id", "session_id", "lease_id", "code_digest", "state", "occurred_at", "outcome", "cpu_ms", "wall_ms", "memory_peak_bytes", "input_bytes", "output_bytes"},
}

_REQUIRED_BODY_FIELDS = {
    "session": _BODY_FIELDS["session"],
    "renew": {"binding_epoch"},
    "release": set(),
    "invalidate": {"reason"},
    "register": _BODY_FIELDS["register"],
    "readiness": _BODY_FIELDS["readiness"],
    "heartbeat": _BODY_FIELDS["heartbeat"],
    "drain": {"deadline"},
    "accounting": {"invocation_id", "tenant_id", "session_id", "lease_id", "code_digest", "state", "occurred_at"},
}


def _route(method: str, path: str) -> tuple[str | None, str | None]:
    if method != "POST":
        return None, None
    if path == "/v1/sessions":
        return "session", None
    if path == "/v1/accounting":
        return "accounting", None
    if path in _HOST_LIFECYCLE_PATHS:
        return path.rsplit("/", 1)[-1], None
    parts = path.split("/")
    if len(parts) != 5 or parts[:3] not in (["", "v1", "sessions"], ["", "v1", "hosts"]) or not parts[3]:
        return None, None
    if parts[2] == "sessions" and parts[4] in {"renew", "release", "invalidate", "rebind"}:
        return parts[4], parts[3]
    if parts[2] == "hosts" and parts[4] == "drain":
        return "drain", parts[3]
    return None, None


def _invalid_request() -> ControlPlaneError:
    return ControlPlaneError("INVALID_REQUEST", "invalid request body", 400)


def _read_body(environ, body_type: str) -> dict:
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        if length < 0:
            raise ValueError
        body = json.loads(environ["wsgi.input"].read(length) or b"{}")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _invalid_request() from exc
    if (not isinstance(body, dict) or set(body) - _BODY_FIELDS[body_type]
            or not _REQUIRED_BODY_FIELDS[body_type] <= set(body)):
        raise _invalid_request()
    return body


def _authorized(supplied, secret: str | None) -> bool:
    return bool(secret) and hmac.compare_digest(str(supplied), secret)


def application(control_plane, shared_secret: str | None = None, *, app_control_secret: str | None = None,
                host_lifecycle_secret: str | None = None, accounting_secret: str | None = None,
                accounting=None,
                allow_unauthenticated: bool = False):
    """Create the API with separate app and executor-host credentials.

    ``shared_secret`` remains a compatibility alias for ``app_control_secret``.
    A host lifecycle secret must always be passed explicitly.
    """
    if app_control_secret is not None and shared_secret is not None and app_control_secret != shared_secret:
        raise ValueError("shared_secret and app_control_secret disagree")
    app_control_secret = app_control_secret if app_control_secret is not None else shared_secret
    if host_lifecycle_secret and app_control_secret and hmac.compare_digest(host_lifecycle_secret, app_control_secret):
        raise ValueError("host_lifecycle_secret must differ from app_control_secret")
    configured = [value for value in (app_control_secret, host_lifecycle_secret, accounting_secret) if value]
    if len(configured) != len(set(configured)):
        raise ValueError("app, host lifecycle, and accounting secrets must differ")
    accounting = accounting or AccountingService(control_plane.store)

    def app(environ, start_response):
        try:
            method, path = environ["REQUEST_METHOD"], environ["PATH_INFO"]
            if method == "GET" and path == "/.well-known/jwks.json":
                result, status = control_plane.signer.jwks(), "200 OK"
            else:
                route, path_id = _route(method, path)
                supplied = environ.get("HTTP_X_INSTANT_CONTROL_SECRET", "")
                secret = (
                    host_lifecycle_secret if path in _HOST_LIFECYCLE_PATHS
                    else accounting_secret if route == "accounting"
                    else app_control_secret
                )
                if not allow_unauthenticated and not _authorized(supplied, secret):
                    result, status = {"code": "UNAUTHORIZED"}, "401 Unauthorized"
                    payload = json.dumps(result).encode()
                    start_response(status, [("Content-Type", "application/json"), ("Content-Length", str(len(payload)))])
                    return [payload]
                if route is None:
                    result, status = {"code": "NOT_FOUND"}, "404 Not Found"
                else:
                    body = _read_body(environ, "session" if route == "rebind" else route)
                    if route == "session": result, status = control_plane.assign(body), "201 Created"
                    elif route == "renew": result, status = control_plane.renew(path_id, body["binding_epoch"]), "200 OK"
                    elif route == "release": control_plane.release(path_id, body.get("reason", "released")); result, status = {}, "204 No Content"
                    elif route == "invalidate": control_plane.invalidate(path_id, body["reason"], body.get("binding_epoch")); result, status = {}, "204 No Content"
                    elif route == "rebind": result, status = control_plane.rebind(body), "201 Created"
                    elif route == "register": result, status = control_plane.register_host(body["executor_id"], body["endpoint"], body["slot_ids"]), "201 Created"
                    elif route == "readiness": result, status = control_plane.readiness(**body), "200 OK"
                    elif route == "heartbeat": control_plane.heartbeat(body["executor_id"]); result, status = {}, "204 No Content"
                    elif route == "drain": result, status = {"sessions": control_plane.drain(path_id, datetime.fromisoformat(body["deadline"].replace("Z", "+00:00")))}, "200 OK"
                    elif route == "accounting": result, status = accounting.ingest(body), "202 Accepted"
        except (ControlPlaneError, AccountingError) as exc:
            result, status = {"code": exc.code, "message": str(exc)}, f"{exc.status} Error"
        except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            result, status = {"code": "INVALID_REQUEST", "message": "invalid request body"}, "400 Bad Request"
        payload = json.dumps(result).encode()
        start_response(status, [("Content-Type", "application/json"), ("Content-Length", str(len(payload)))])
        return [payload]
    return app
