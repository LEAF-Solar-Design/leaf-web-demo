"""Start a real warm executor TLS listener for the harness acceptance test."""
from __future__ import annotations

import http.client
import importlib.util
import json
import platform as _stdlib_platform
import socket
import sys
import sysconfig
import threading
from pathlib import Path
from typing import Any
from urllib import request as urllib_request


ROOT = Path(__file__).resolve().parents[3]
# A repository-root PYTHONPATH can resolve Leaf's top-level ``platform``
# package before Python's standard-library module. Load the stdlib file by
# path and bind its canonical name before uuid imports it lazily.
if not callable(getattr(_stdlib_platform, "system", None)):
    platform_path = Path(sysconfig.get_path("stdlib")) / "platform.py"
    platform_spec = importlib.util.spec_from_file_location("_leaf_stdlib_platform", platform_path)
    if platform_spec is None or platform_spec.loader is None:
        raise RuntimeError(f"cannot load standard-library platform module from {platform_path}")
    _stdlib_platform = importlib.util.module_from_spec(platform_spec)
    platform_spec.loader.exec_module(_stdlib_platform)
    sys.modules["platform"] = _stdlib_platform
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from executor.runtime.service import RuntimeTlsConfig, make_server  # noqa: E402
from executor.runtime.supervisor import WarmExecutorSupervisor  # noqa: E402
from executor.runtime.tests.helpers import EXECUTOR_ID, documents, keys, lease  # noqa: E402


def emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, separators=(",", ":")), flush=True)


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit("usage: runtime-mtls-fixture.py CA_FILE SERVER_CERT_FILE SERVER_KEY_FILE")
    ca_file, server_cert_file, server_key_file = sys.argv[1:]
    supervisor = WarmExecutorSupervisor(
        EXECUTOR_ID,
        keys(),
        pool_size=1,
        trusted_development_fixtures=True,
    )
    server = make_server(
        ("127.0.0.1", 0),
        supervisor,
        tls_config=RuntimeTlsConfig(server_cert_file, server_key_file, ca_file),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    invocation_count = 0
    original_invoke = supervisor.invoke

    def counted_invoke(body: dict[str, Any], authorization: str | None) -> dict[str, Any]:
        nonlocal invocation_count
        invocation_count += 1
        return original_invoke(body, authorization)

    supervisor.invoke = counted_invoke  # type: ignore[method-assign]
    source = "def run(intake, params):\n return {'warm': True, 'layer': params['layer']}\n"
    docs = documents(source)
    docs["assignment"]["executor_endpoint"] = f"https://127.0.0.1:{server.server_port}"
    docs["assignment"]["lease_token"] = lease(docs["invocation"])
    assignment_request = {key: docs[key] for key in ("assignment", "code_load", "catalog", "source", "drawing_context")}
    supervisor.assign(assignment_request)
    warm_process_ids = supervisor.process_ids()

    attempts = {"dns": 0, "urlopen": 0, "http_client": 0}
    original_getaddrinfo = socket.getaddrinfo
    original_urlopen = urllib_request.urlopen
    original_http_connect = http.client.HTTPConnection.connect

    def counted_getaddrinfo(*args: Any, **kwargs: Any) -> Any:
        attempts["dns"] += 1
        return original_getaddrinfo(*args, **kwargs)

    def blocked_urlopen(*_args: Any, **_kwargs: Any) -> Any:
        attempts["urlopen"] += 1
        raise AssertionError("warm invocation attempted an artifact or external HTTP fetch")

    def blocked_http_connect(self: http.client.HTTPConnection) -> None:
        attempts["http_client"] += 1
        raise AssertionError("warm invocation attempted an outbound HTTP connection")

    socket.getaddrinfo = counted_getaddrinfo
    urllib_request.urlopen = blocked_urlopen
    http.client.HTTPConnection.connect = blocked_http_connect
    try:
        emit({"state": "ready", "assignment": docs["assignment"], "invocation": docs["invocation"]})
        for line in sys.stdin:
            command = json.loads(line)
            if command == {"command": "status"}:
                emit({
                    "state": "status",
                    "invocation_count": invocation_count,
                    "warm_process_ids": warm_process_ids,
                    "current_process_ids": supervisor.process_ids(),
                    "outbound_attempts": attempts,
                })
            elif command == {"command": "stop"}:
                break
    finally:
        socket.getaddrinfo = original_getaddrinfo
        urllib_request.urlopen = original_urlopen
        http.client.HTTPConnection.connect = original_http_connect
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        supervisor.close()


if __name__ == "__main__":
    main()
