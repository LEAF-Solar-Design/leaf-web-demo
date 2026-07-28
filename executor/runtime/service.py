"""Persistent HTTP/1.1 facade for a warm executor supervisor."""
from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
from pathlib import Path
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .supervisor import ExecutorError, WarmExecutorSupervisor


class ExecutorHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    supervisor: WarmExecutorSupervisor
    control_secret: str | None = None

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > 2 * 1024 * 1024:
            raise ValueError("request body is missing or too large")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("request body must be an object")
        return value

    def _send(self, status: int, body: Any, content_type: str = "application/json") -> None:
        encoded = body if isinstance(body, bytes) else json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send(HTTPStatus.OK, self.supervisor.health())
        elif self.path == "/metrics":
            self._send(HTTPStatus.OK, self.supervisor.metrics().encode("utf-8"), "text/plain; version=0.0.4")
        else:
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            body = self._json()
            if self.path == "/v1/control/assign":
                if not self._control_authorized():
                    self._send(HTTPStatus.UNAUTHORIZED, {"error": "control authentication required"})
                    return
                self._send(HTTPStatus.OK, self.supervisor.assign(body))
                return
            if self.path == "/v1/control/release":
                if not self._control_authorized():
                    self._send(HTTPStatus.UNAUTHORIZED, {"error": "control authentication required"})
                    return
                self._send(HTTPStatus.OK, self.supervisor.release(body["assignment_id"]))
                return
            if self.path == "/v1/invoke":
                response = self.supervisor.invoke(body, self.headers.get("Authorization"))
                status = HTTPStatus.OK if response["status"] == "succeeded" else HTTPStatus.FORBIDDEN
                self._send(status, response)
                return
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except (ValueError, ExecutorError) as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _control_authorized(self) -> bool:
        if self.control_secret is None:
            return True
        supplied = self.headers.get("X-Instant-Runtime-Control-Secret", "")
        return bool(supplied) and hmac.compare_digest(supplied, self.control_secret)


def make_server(address: tuple[str, int], supervisor: WarmExecutorSupervisor,
                control_secret: str | None = None) -> ThreadingHTTPServer:
    """Create a keep-alive HTTP/1.1 server. The caller owns its shutdown."""
    handler = type("BoundExecutorHandler", (ExecutorHandler,), {"supervisor": supervisor,
                                                                "control_secret": control_secret})

    class QuietServer(ThreadingHTTPServer):
        def handle_error(self, _request: Any, _client_address: Any) -> None:
            # A client closing an idle keep-alive connection is not an executor error.
            return

    return QuietServer(address, handler)


def load_trust_bundle() -> dict[str, bytes]:
    inline = os.environ.get("LEAF_INSTANT_CONTROL_JWKS_JSON", "").strip()
    filename = os.environ.get("LEAF_INSTANT_CONTROL_JWKS_FILE", "").strip()
    if inline and filename:
        raise ValueError("configure one control JWKS source, not both")
    if filename:
        inline = Path(filename).read_text(encoding="utf-8")
    if not inline:
        return {}
    value = json.loads(inline)
    keys: dict[str, bytes] = {}
    for item in value.get("keys", []):
        if item.get("kty") != "OKP" or item.get("crv") != "Ed25519" or item.get("alg") != "EdDSA":
            continue
        kid, encoded = item.get("kid"), item.get("x")
        if not isinstance(kid, str) or not isinstance(encoded, str):
            continue
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if len(raw) != 32 or base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != encoded:
            raise ValueError("invalid Ed25519 JWKS key")
        keys[kid] = raw
    if not keys:
        raise ValueError("control JWKS contains no usable Ed25519 signing keys")
    return keys


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--executor-id", default="executor-local-001")
    parser.add_argument("--pool-size", type=int, default=2)
    args = parser.parse_args()
    control_secret = os.environ.get("LEAF_INSTANT_RUNTIME_CONTROL_SECRET", "").strip()
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not control_secret:
        raise SystemExit("LEAF_INSTANT_RUNTIME_CONTROL_SECRET is required for a non-loopback listener")
    trusted_keys = load_trust_bundle()
    if not trusted_keys and os.environ.get("LEAF_INSTANT_ALLOW_EMPTY_TRUST") != "1":
        raise SystemExit("a control-plane JWKS trust bundle is required")
    supervisor = WarmExecutorSupervisor(args.executor_id, trusted_keys, args.pool_size)
    server = make_server((args.host, args.port), supervisor, control_secret or None)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        supervisor.close()


if __name__ == "__main__":
    main()
