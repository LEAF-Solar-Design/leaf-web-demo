"""Persistent HTTP/1.1 facade for a warm executor supervisor."""
from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import ipaddress
import ssl
from dataclasses import dataclass
from pathlib import Path
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .supervisor import ExecutorError, WarmExecutorSupervisor
from .registration import HostRegistrar, HostRegistrationConfig, resolve_executor_id


@dataclass(frozen=True)
class RuntimeTlsConfig:
    """Mutual TLS material for the executor control listener."""

    cert_file: str
    key_file: str
    client_ca_file: str

    @classmethod
    def from_environment(cls) -> "RuntimeTlsConfig | None":
        values = {
            "cert_file": os.environ.get("LEAF_INSTANT_RUNTIME_TLS_CERT_FILE", "").strip(),
            "key_file": os.environ.get("LEAF_INSTANT_RUNTIME_TLS_KEY_FILE", "").strip(),
            "client_ca_file": os.environ.get("LEAF_INSTANT_RUNTIME_TLS_CLIENT_CA_FILE", "").strip(),
        }
        present = [name for name, value in values.items() if value]
        if not present:
            return None
        if len(present) != len(values):
            missing = ", ".join(name for name, value in values.items() if not value)
            raise ValueError(f"incomplete runtime TLS configuration, missing {missing}")
        return cls(**values)

    def server_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=self.cert_file, keyfile=self.key_file)
        context.load_verify_locations(cafile=self.client_ca_file)
        context.verify_mode = ssl.CERT_REQUIRED
        return context


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
                control_secret: str | None = None, *, tls_config: RuntimeTlsConfig | None = None) -> ThreadingHTTPServer:
    """Create a keep-alive HTTP/1.1 server. Non-loopback listeners require mTLS."""
    if not _is_loopback_host(address[0]):
        if tls_config is None:
            raise ValueError("non-loopback executor listeners require mutual TLS configuration")
        if not control_secret:
            raise ValueError("non-loopback executor listeners require a runtime control secret")
        if not supervisor.public_keys:
            raise ValueError("non-loopback executor listeners require a lease trust bundle")
    handler = type("BoundExecutorHandler", (ExecutorHandler,), {"supervisor": supervisor,
                                                                "control_secret": control_secret})

    class QuietServer(ThreadingHTTPServer):
        def handle_error(self, _request: Any, _client_address: Any) -> None:
            # A client closing an idle keep-alive connection is not an executor error.
            return

    server = QuietServer(address, handler)
    if tls_config is not None:
        server.socket = tls_config.server_context().wrap_socket(server.socket, server_side=True)
    return server


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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


def configure_host_registrar(
    supervisor: WarmExecutorSupervisor,
    *,
    environ: dict[str, str] | None = None,
    registrar_factory: Any = HostRegistrar,
    config: HostRegistrationConfig | None = None,
) -> HostRegistrar | None:
    """Build the host lifecycle client and reject configuration drift.

    Registration is optional only for local loopback development. A configured
    client must describe the exact supervisor and its prestarted slots before
    the host can advertise readiness.
    """
    source = os.environ if environ is None else environ
    if config is None and not source.get("LEAF_INSTANT_CONTROL_PLANE_URL", "").strip():
        return None
    config = config or HostRegistrationConfig.from_environment(source)
    if config.executor_id != supervisor.executor_id:
        raise ValueError("registration executor ID does not match the warm supervisor")
    actual_slots = tuple(supervisor.process_ids())
    if set(config.slot_ids) != set(actual_slots) or len(config.slot_ids) != len(actual_slots):
        raise ValueError("registration slot IDs do not match the prestarted warm pool")
    return registrar_factory(config)


def serve_registered(
    server: ThreadingHTTPServer,
    supervisor: WarmExecutorSupervisor,
    registrar: HostRegistrar | None,
) -> None:
    """Advertise readiness only after the listening socket and pool exist."""
    try:
        if registrar is not None:
            registrar.start()
        server.serve_forever()
    finally:
        if registrar is not None:
            registrar.close()
        server.server_close()
        supervisor.close()


def scrub_child_environment(environ: dict[str, str] | None = None) -> None:
    """Remove parent-only secret values before multiprocessing spawn."""
    target = os.environ if environ is None else environ
    for name in (
        "LEAF_INSTANT_RUNTIME_CONTROL_SECRET",
        "LEAF_INSTANT_HOST_LIFECYCLE_SECRET",
        "LEAF_INSTANT_CONTROL_API_SECRET",  # compatibility until host-secret migration completes
    ):
        target.pop(name, None)


def _secret_value(name: str) -> str:
    value = os.environ.get(name, "").strip()
    filename = os.environ.get(f"{name}_FILE", "").strip()
    if value and filename:
        raise ValueError(f"configure one {name} source")
    if filename:
        path = Path(filename)
        if not path.is_absolute():
            raise ValueError(f"{name}_FILE must be an absolute path")
        value = path.read_text(encoding="utf-8").strip()
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--executor-id", default=os.environ.get("LEAF_INSTANT_EXECUTOR_ID", ""))
    parser.add_argument("--pool-size", type=int, default=int(os.environ.get("LEAF_INSTANT_EXECUTOR_POOL_SIZE", "2")))
    args = parser.parse_args()
    if not args.executor_id:
        try:
            args.executor_id = (
                resolve_executor_id(os.environ)
                if os.environ.get("LEAF_INSTANT_CONTROL_PLANE_URL", "").strip()
                else "executor-local-001"
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    try:
        control_secret = _secret_value("LEAF_INSTANT_RUNTIME_CONTROL_SECRET")
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    is_loopback = _is_loopback_host(args.host)
    if not is_loopback and not control_secret:
        raise SystemExit("LEAF_INSTANT_RUNTIME_CONTROL_SECRET is required for a non-loopback listener")
    try:
        tls_config = RuntimeTlsConfig.from_environment()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not is_loopback and tls_config is None:
        raise SystemExit("server certificate, private key, and client CA are required for a non-loopback listener")
    trusted_keys = load_trust_bundle()
    if not trusted_keys and (not is_loopback or os.environ.get("LEAF_INSTANT_ALLOW_EMPTY_TRUST") != "1"):
        raise SystemExit("a control-plane JWKS trust bundle is required")
    try:
        registration_config = (
            HostRegistrationConfig.from_environment(
                executor_id=args.executor_id,
                slot_ids=tuple(f"slot-{index + 1}" for index in range(args.pool_size)),
            )
            if os.environ.get("LEAF_INSTANT_CONTROL_PLANE_URL", "").strip()
            else None
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    # Multiprocessing spawn receives the current environment. Keep supervisor
    # secrets in parent memory, but remove them before any restricted child is
    # created so they are absent from the child's environment and /proc image.
    scrub_child_environment()
    supervisor = WarmExecutorSupervisor(args.executor_id, trusted_keys, args.pool_size)
    try:
        registrar = configure_host_registrar(supervisor, config=registration_config)
        if not is_loopback and registrar is None:
            raise ValueError("non-loopback executor listeners require control-plane host registration")
        server = make_server((args.host, args.port), supervisor, control_secret or None, tls_config=tls_config)
    except (ValueError, OSError) as exc:
        supervisor.close()
        raise SystemExit(str(exc)) from exc
    serve_registered(server, supervisor, registrar)


if __name__ == "__main__":
    main()
