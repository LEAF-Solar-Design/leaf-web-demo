"""Private TLS listener for identity-bound iOS provider callbacks."""
from __future__ import annotations

import os
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI


@dataclass(frozen=True)
class CallbackTlsConfig:
    host: str
    port: int
    cert_file: Path
    key_file: Path

    @classmethod
    def from_environment(cls) -> "CallbackTlsConfig | None":
        names = {
            "cert": os.environ.get("LEAF_IOS_SHIP_CALLBACK_TLS_CERT_FILE", "").strip(),
            "key": os.environ.get("LEAF_IOS_SHIP_CALLBACK_TLS_KEY_FILE", "").strip(),
            "port": os.environ.get("LEAF_IOS_SHIP_CALLBACK_TLS_PORT", "").strip(),
        }
        if not any(names.values()):
            return None
        if not all(names.values()):
            raise RuntimeError("iOS callback TLS configuration is incomplete")
        try:
            port = int(names["port"])
        except ValueError as exc:
            raise RuntimeError("iOS callback TLS port is invalid") from exc
        if not 1 <= port <= 65535 or port == 8130:
            raise RuntimeError("iOS callback TLS port is invalid")
        cert_file, key_file = Path(names["cert"]), Path(names["key"])
        for path in (cert_file, key_file):
            if not path.is_absolute() or not path.is_file():
                raise RuntimeError("iOS callback TLS material is unavailable")
            # The deployed task is Linux. Windows chmod does not model POSIX
            # group/other bits, so local tests can prove presence only.
            if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
                raise RuntimeError("iOS callback TLS material is not private")
        return cls(
            host=os.environ.get("LEAF_IOS_SHIP_CALLBACK_TLS_HOST", "0.0.0.0").strip(),
            port=port,
            cert_file=cert_file,
            key_file=key_file,
        )


class CallbackTlsServer:
    def __init__(self, callback_app: FastAPI, config: CallbackTlsConfig):
        import uvicorn

        self._server: Any = uvicorn.Server(uvicorn.Config(
            callback_app,
            host=config.host,
            port=config.port,
            ssl_certfile=str(config.cert_file),
            ssl_keyfile=str(config.key_file),
            access_log=False,
            log_level="warning",
        ))
        self._thread = threading.Thread(
            target=self._server.run, name="ios-ship-callback-tls", daemon=True)

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 5.0
        while self._thread.is_alive() and not self._server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        if not self._server.started:
            self.stop()
            raise RuntimeError("iOS callback TLS listener did not start")

    def stop(self) -> None:
        self._server.should_exit = True
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)
