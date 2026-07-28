"""Executor-control client. Invocation traffic has no client in this package."""
from __future__ import annotations

import json
from typing import Protocol
from urllib.request import Request, urlopen


class RuntimeClient(Protocol):
    def assign(self, endpoint: str, command: dict) -> dict: ...
    def release(self, endpoint: str, command: dict) -> None: ...


class HttpRuntimeClient:
    """Calls only the two executor control endpoints mandated by the spec."""
    def __init__(self, control_secret: str) -> None:
        if not control_secret:
            raise ValueError("executor control secret is required")
        self.control_secret = control_secret

    def _post(self, url: str, body: dict) -> dict:
        request = Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json",
                          "X-Instant-Runtime-Control-Secret": self.control_secret}, method="POST")
        with urlopen(request, timeout=5) as response:  # nosec B310: endpoint is registered control-plane data
            return json.loads(response.read())

    def assign(self, endpoint: str, command: dict) -> dict:
        return self._post(endpoint.rstrip("/") + "/v1/control/assign", command)

    def release(self, endpoint: str, command: dict) -> None:
        self._post(endpoint.rstrip("/") + "/v1/control/release", command)
