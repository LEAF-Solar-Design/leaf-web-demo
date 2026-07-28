"""Small WSGI API surface. There is intentionally no /v1/invoke route."""
from __future__ import annotations

import json
import hmac
from datetime import datetime

from .service import ControlPlaneError


def application(control_plane, shared_secret: str | None = None, *, allow_unauthenticated: bool = False):
    def app(environ, start_response):
        try:
            method, path = environ["REQUEST_METHOD"], environ["PATH_INFO"]
            if method == "GET" and path == "/.well-known/jwks.json":
                result, status = control_plane.signer.jwks(), "200 OK"
            else:
                supplied = environ.get("HTTP_X_INSTANT_CONTROL_SECRET", "")
                if (not allow_unauthenticated and
                        (not shared_secret or not hmac.compare_digest(str(supplied), shared_secret))):
                    result, status = {"code": "UNAUTHORIZED"}, "401 Unauthorized"
                    payload = json.dumps(result).encode()
                    start_response(status, [("Content-Type", "application/json"), ("Content-Length", str(len(payload)))])
                    return [payload]
                length = int(environ.get("CONTENT_LENGTH") or 0)
                body = json.loads(environ["wsgi.input"].read(length) or b"{}")
                if method == "POST" and path == "/v1/sessions": result, status = control_plane.assign(body), "201 Created"
                elif method == "POST" and path.endswith("/renew"):
                    result, status = control_plane.renew(path.split("/")[3], body["binding_epoch"]), "200 OK"
                elif method == "POST" and path.endswith("/release"):
                    control_plane.release(path.split("/")[3], body.get("reason", "released")); result, status = {}, "204 No Content"
                elif method == "POST" and path.endswith("/invalidate"):
                    control_plane.invalidate(path.split("/")[3], body["reason"], body.get("binding_epoch")); result, status = {}, "204 No Content"
                elif method == "POST" and path.endswith("/rebind"):
                    result, status = control_plane.rebind(body), "201 Created"
                elif method == "POST" and path == "/v1/hosts/register": result, status = control_plane.register_host(body["executor_id"], body["endpoint"], body["slot_ids"]), "201 Created"
                elif method == "POST" and path == "/v1/hosts/readiness": result, status = control_plane.readiness(**body), "200 OK"
                elif method == "POST" and path == "/v1/hosts/heartbeat": control_plane.heartbeat(body["executor_id"]); result, status = {}, "204 No Content"
                elif method == "POST" and path.startswith("/v1/hosts/") and path.endswith("/drain"):
                    result, status = {"sessions": control_plane.drain(path.split("/")[3], datetime.fromisoformat(body["deadline"].replace("Z", "+00:00")))}, "200 OK"
                else: result, status = {"code": "NOT_FOUND"}, "404 Not Found"
        except ControlPlaneError as exc:
            result, status = {"code": exc.code, "message": str(exc)}, f"{exc.status} Error"
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            result, status = {"code": "INVALID_REQUEST", "message": str(exc)}, "400 Bad Request"
        payload = json.dumps(result).encode()
        start_response(status, [("Content-Type", "application/json"), ("Content-Length", str(len(payload)))])
        return [payload]
    return app
