"""Closed command dispatcher and independent loopback relay boundary."""
from __future__ import annotations

import json
from http.client import HTTPException
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

from .client import secure_opener
from .config import Config
from .packages import Registry

MAX_BODY = 1048576
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
ARGS = {
    "pkg.install": ({"package", "version"}, set()),
    "pkg.upgrade": ({"package", "version"}, set()),
    "pkg.uninstall": ({"package", "keep_data"}, set()),
    "pkg.health": ({"package"}, set()),
    "http.proxy": ({"package", "method", "path"}, {"query", "body_json"}),
    "ha.onboard": (set(), set()), "ha.flow.list": (set(), set()),
    "ha.flow.start": ({"handler"}, {"data"}), "ha.flow.step": ({"flow_id", "data"}, set()),
    "agent.pause": (set(), set()), "agent.update": ({"version", "sha256"}, set()),
}


def failure(code: str, *, status="refused") -> dict:
    return {"status": status, "error": {"code": code, "message": code.replace("_", " ")}}


def canonical_path(path) -> bool:
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or len(path) > 2048:
        return False
    if any(c in path for c in "%\\?#") or any(ord(c) < 33 or 127 <= ord(c) <= 159 or c.isspace() for c in path):
        return False
    parts = path[1:].split("/")
    return not any(p in {".", ".."} for p in parts) and not any(p == "" for p in parts[:-1])


def path_allowed(method, path, entry) -> bool:
    if not isinstance(method, str) or method not in METHODS or not canonical_path(path):
        return False
    for rule in entry["relay_allow"]:
        prefix = rule["path_prefix"]
        if rule["method"] == method and canonical_path(prefix):
            if prefix.endswith("/") and path.startswith(prefix):
                return True
            if path == prefix or path.startswith(prefix + "/"):
                return True
    return False


def strict_json(raw):
    def invalid_constant(value):
        raise ValueError("Non-JSON constant")
    return json.loads(raw, parse_constant=invalid_constant)


def redact(value, secrets):
    """Defense in depth for local credentials echoed by an upstream service."""
    if isinstance(value, dict):
        return {key: redact(item, secrets) for key, item in value.items()
                if key.lower() not in {"access_token", "refresh_token", "password", "secret", "authorization", "token"}}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
    return value


class Dispatcher:
    def __init__(self, config: Config, registry: Registry):
        self.config, self.registry = config, registry

    def dispatch(self, op: str, args: dict) -> dict:
        if self.config.disabled:
            return failure("paused")
        if not isinstance(op, str) or op not in ARGS or op == "agent.update":
            return failure("unsupported_op")
        required, optional = ARGS[op]
        if not isinstance(args, dict) or not required <= args.keys() or args.keys() - required - optional:
            return failure("invalid_args")
        if op == "agent.pause":
            self.config.pause()
            return {"status": "ok"}
        package = "homeassistant" if op.startswith("ha.") else args["package"]
        if not isinstance(package, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", package):
            return failure("invalid_args")
        catalog = self.config.catalog()
        entry = catalog.get(package)
        handler = self.registry.get(package)
        if entry is None or handler is None:
            return failure("package_unknown")
        if op in {"pkg.install", "pkg.upgrade"}:
            if not isinstance(args["version"], str) or not 1 <= len(args["version"]) <= 128:
                return failure("invalid_args")
            if args["version"] != entry["version"]:
                return failure("version_unknown")
        if op == "pkg.uninstall" and type(args["keep_data"]) is not bool:
            return failure("invalid_args")
        if op == "ha.flow.start" and args["handler"] not in ("hue", "wiz", "group"):
            return failure("invalid_args")
        if "data" in args and not isinstance(args["data"], dict):
            return failure("invalid_args")
        if op == "ha.flow.step" and (not isinstance(args["flow_id"], str) or not 1 <= len(args["flow_id"]) <= 128):
            return failure("invalid_args")
        try:
            if op == "http.proxy":
                auth = handler.auth_headers()
                secrets = [part for value in auth.values() for part in (value, value.removeprefix("Bearer "))]
                return self.proxy(args, entry, auth, secrets)
            data = handler.handle(op, args)
            # Preserve health metadata, including macOS lan_access, in result data.
            # Check JSON serialization locally; no exception data leaves the device.
            json.dumps(data, allow_nan=False)
            return {"status": "ok", "data": redact(data, [])}
        except (TimeoutError, socket.timeout):
            return failure("timeout", status="error")
        except Exception:
            return failure("install_failed" if op.startswith("pkg.") else "upstream_error", status="error")

    def proxy(self, args, entry, auth, secrets):
        if not path_allowed(args["method"], args["path"], entry):
            return failure("path_denied")
        query = args.get("query", {})
        if not isinstance(query, dict) or any(not isinstance(k, str) or not k or not isinstance(v, str) for k, v in query.items()):
            return failure("invalid_args")
        port = entry["port"]
        if type(port) is not int or not 1 <= port <= 65535:
            return failure("package_unknown")
        body = None
        if "body_json" in args:
            try:
                body = json.dumps(args["body_json"], separators=(",", ":"), allow_nan=False).encode("utf-8")
            except (ValueError, TypeError):
                return failure("invalid_args")
            if len(body) > MAX_BODY:
                return failure("invalid_args")
        target = f"http://127.0.0.1:{port}" + args["path"]
        if query:
            target += "?" + urlencode(query)
        headers = {"Accept": "application/json", **auth}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = Request(target, data=body, method=args["method"], headers=headers)
        try:
            try:
                response = secure_opener().open(req, timeout=15)
            except HTTPError as exc:
                response = exc
            with response:
                status = response.code
                if 300 <= status < 400 and status != 304:
                    return failure("upstream_error", status="error")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                raw = response.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY:
                    return failure("upstream_error", status="error")
                if status in {204, 304} and not raw:
                    return {"status": "ok", "data": {"status": status, "content_type": "application/json"}}
                if content_type != "application/json" and not (content_type.startswith("application/") and content_type.endswith("+json")):
                    return failure("upstream_error", status="error")
                data = strict_json(raw.decode("utf-8"))
                return {"status": "ok", "data": {"status": status, "content_type": content_type,
                                                   "body_json": redact(data, secrets)}}
        except (TimeoutError, socket.timeout):
            return failure("timeout", status="error")
        except (URLError, OSError, ValueError, HTTPException):
            return failure("upstream_error", status="error")
