"""Bounded urllib transport. Exceptions and logs never contain response bodies."""
from __future__ import annotations

import hashlib
from http.client import HTTPException
import json
import logging
import platform
import secrets
import socket
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler, ProxyHandler
from uuid import UUID

from . import __version__, sign
from .config import Config, atomic_write, validate_origin

LOG = logging.getLogger("leaf_agent")


class ClientError(Exception):
    pass


class Revoked(ClientError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
        return None


def secure_opener():
    return build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()), NoRedirect())


class Backoff:
    def __init__(self, device_id: str, sleep=time.sleep):
        self.sleep = sleep
        self.failures = 0
        self.jitter = int.from_bytes(hashlib.sha256(device_id.encode()).digest()[:4], "big") / 2**32

    def reset(self):
        self.failures = 0

    def wait(self):
        delay = min(60.0, 2 ** min(self.failures, 6) * (1 + self.jitter * .25))
        self.failures += 1
        self.sleep(delay)
        return delay


class Client:
    def __init__(self, config: Config, server: str | None = None, *, opener=None, sleep=time.sleep):
        self.config = config
        identity = config.load() if server is None else {"server": server, "device_id": ""}
        self.server = validate_origin(identity["server"])
        self.device_id = identity["device_id"]
        self.opener = opener or secure_opener()
        self.backoff = Backoff(self.device_id or self.server, sleep)

    def _request(self, method, target, payload=None, *, signed=True, timeout=15):
        body = b"" if payload is None else json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
        request_headers = {"Accept": "application/json", "User-Agent": f"leaf-device-agent/{__version__}"}
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        if signed:
            seed = (self.config.home / "device.key").read_bytes()
            request_headers.update(sign.headers(seed, self.device_id, method, target, body))
        req = Request(self.server + target, data=body if payload is not None else None,
                      headers=request_headers, method=method)
        try:
            with self.opener.open(req, timeout=timeout) as response:
                if response.status == 204:
                    result = None
                else:
                    # A poll may contain eight separately capped proxy bodies.
                    raw = response.read(9 * 1048576 + 1)
                    if len(raw) > 9 * 1048576:
                        raise ValueError("Response too large")
                    result = json.loads(raw)
            self.backoff.reset()
            return result
        except HTTPError as exc:
            with exc:
                try:
                    code = json.loads(exc.read(65536)).get("error", {}).get("code")
                except (ValueError, AttributeError, OSError, HTTPException):
                    code = None
            if signed and exc.code == 401 and code == "device_revoked":
                raise Revoked("Device revoked") from None
            LOG.warning("Studio request failed (HTTP %d)", exc.code)
            self.backoff.wait()
            raise ClientError("Studio rejected request") from None
        except (URLError, OSError, ValueError, HTTPException):
            LOG.warning("Studio connection or response failed")
            self.backoff.wait()
            raise ClientError("Studio connection or response failed") from None

    def register(self, token: str) -> str:
        if (self.config.home / "config.json").exists():
            identity = self.config.load()
            if identity["server"] != self.server:
                raise ClientError("Uninstall before changing Studio origin")
            if len((self.config.home / "device.key").read_bytes()) != 32:
                raise ClientError("Invalid local device key")
            self.config.pin_catalog()
            self.device_id = identity["device_id"]
            return self.device_id
        seed = secrets.token_bytes(32)
        # Keep a fresh seed private before making the consuming registration call.
        atomic_write(self.config.home / "device.key", seed)
        response = self._request("POST", "/api/devices/agent/register", {
            "token": token, "public_key": sign.public_key(seed), "agent_version": __version__,
            "platform": {"os": platform.system().lower(), "os_version": platform.release()[:128],
                         "arch": platform.machine()[:64]}, "hostname": socket.gethostname()[:253],
        }, signed=False)
        if not isinstance(response, dict) or set(response) != {"device_id", "server_time"}:
            raise ClientError("Invalid registration response")
        try:
            self.device_id = str(UUID(response["device_id"]))
        except (ValueError, TypeError, AttributeError):
            raise ClientError("Invalid device identity") from None
        self.config.pin_catalog()
        self.config.save(self.server, self.device_id)
        self.backoff = Backoff(self.device_id, self.backoff.sleep)
        LOG.info("Device enrolled")
        return self.device_id

    def poll(self, wait=25):
        if type(wait) is not int or not 0 <= wait <= 25:
            raise ValueError("Poll wait must be 0..25")
        value = self._request("GET", f"/api/devices/agent/poll?wait={wait}", timeout=wait + 10)
        if value is None:
            return []
        if not isinstance(value, dict) or set(value) != {"commands"} or not isinstance(value["commands"], list) or len(value["commands"]) > 8:
            self.backoff.wait()
            raise ClientError("Invalid poll response")
        try:
            for command in value["commands"]:
                if not isinstance(command, dict) or not isinstance(command.get("command_id"), str):
                    raise ValueError("Invalid command identity")
                UUID(command["command_id"])
        except (ValueError, TypeError, AttributeError):
            self.backoff.wait()
            raise ClientError("Invalid poll response") from None
        return value["commands"]

    def post_results(self, results):
        return self._request("POST", "/api/devices/agent/results", {"results": results})

    def heartbeat(self, packages, free_mem_mb, uptime_s):
        return self._request("POST", "/api/devices/agent/heartbeat", {
            "agent_version": __version__, "packages": packages,
            "free_mem_mb": free_mem_mb, "uptime_s": uptime_s,
        })
