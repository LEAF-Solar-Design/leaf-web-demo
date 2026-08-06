"""Executor-control client. Invocation traffic has no client in this package."""
from __future__ import annotations

import http.client
import ipaddress
import json
import re
import ssl
from typing import Protocol
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class RuntimeClient(Protocol):
    def assign(self, endpoint: str, command: dict) -> dict: ...
    def release(self, endpoint: str, command: dict) -> None: ...


class HttpRuntimeClient:
    """Calls only the two executor control endpoints mandated by the spec."""
    def __init__(self, control_secret: str, *, ca_file: str | None = None,
                 client_cert_file: str | None = None, client_key_file: str | None = None,
                 tls_server_name: str | None = None,
                 request_timeout_seconds: float = 5.0) -> None:
        if not control_secret:
            raise ValueError("executor control secret is required")
        if request_timeout_seconds <= 0:
            raise ValueError("executor control request timeout must be positive")
        self.control_secret = control_secret
        # The socket budget for one control call; it must cover the executor's
        # child boot + source load on assign, so tests on contended hosts pass
        # a generous value.
        self.request_timeout_seconds = request_timeout_seconds
        self.tls_server_name = tls_server_name.strip() if tls_server_name else None
        if self.tls_server_name and not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", self.tls_server_name):
            raise ValueError("invalid executor TLS server name")
        configured = (ca_file, client_cert_file, client_key_file)
        if any(configured) and not all(configured):
            raise ValueError("executor mTLS requires a CA file, client certificate, and client key")
        self._https_context: ssl.SSLContext | None = None
        if all(configured):
            self._https_context = ssl.create_default_context(
                ssl.Purpose.SERVER_AUTH, cafile=ca_file,
            )
            self._https_context.load_cert_chain(
                certfile=client_cert_file, keyfile=client_key_file,
            )

    def _post(self, url: str, body: dict) -> dict:
        context = self._ssl_context(url)
        parsed = urlsplit(url)
        if parsed.scheme == "https" and self.tls_server_name:
            return self._post_with_server_name(parsed, body, context)
        request = Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json",
                          "X-Instant-Runtime-Control-Secret": self.control_secret}, method="POST")
        with urlopen(request, timeout=self.request_timeout_seconds, context=context) as response:  # nosec B310: endpoint is registered control-plane data
            return json.loads(response.read())

    def _post_with_server_name(self, parsed, body: dict, context: ssl.SSLContext | None) -> dict:
        if context is None or not parsed.hostname:
            raise ValueError("HTTPS executor control requires a verified TLS context")
        connection = _ServerNameHTTPSConnection(
            parsed.hostname,
            parsed.port or 443,
            context=context,
            server_name=self.tls_server_name or parsed.hostname,
            timeout=self.request_timeout_seconds,
        )
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        encoded = json.dumps(body).encode()
        try:
            connection.request("POST", path, encoded, {
                "Content-Type": "application/json",
                "X-Instant-Runtime-Control-Secret": self.control_secret,
            })
            response = connection.getresponse()
            payload = response.read()
            if not 200 <= response.status < 300:
                raise OSError(f"executor control returned HTTP {response.status}")
            return json.loads(payload)
        finally:
            connection.close()

    def _ssl_context(self, url: str) -> ssl.SSLContext | None:
        parsed = urlsplit(url)
        if parsed.scheme == "http":
            if not _is_loopback_host(parsed.hostname):
                raise ValueError("insecure HTTP is allowed only for loopback executor endpoints")
            return None
        if parsed.scheme != "https":
            raise ValueError("executor endpoint must use http or https")
        if self._https_context is None:
            raise ValueError("HTTPS executor control requires a CA file, client certificate, and client key")
        return self._https_context

    def assign(self, endpoint: str, command: dict) -> dict:
        return self._post(endpoint.rstrip("/") + "/v1/control/assign", command)

    def release(self, endpoint: str, command: dict) -> None:
        self._post(endpoint.rstrip("/") + "/v1/control/release", command)


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _ServerNameHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a private IP while verifying one configured DNS identity."""

    def __init__(self, host: str, port: int, *, context: ssl.SSLContext,
                 server_name: str, timeout: float) -> None:
        super().__init__(host, port, context=context, timeout=timeout)
        self._leaf_server_name = server_name

    def connect(self) -> None:
        http.client.HTTPConnection.connect(self)
        if self.sock is None:
            raise OSError("executor TLS socket was not created")
        self.sock = self._context.wrap_socket(
            self.sock,
            server_hostname=self._leaf_server_name,
        )
