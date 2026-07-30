"""Payload-free accounting events for the executor log delivery plane."""
from __future__ import annotations

import json
import os
import threading
from typing import Any


class AccountingEmissionError(RuntimeError):
    """The runtime could not hand an accounting event to its log driver."""


class StructuredAccountingEmitter:
    """Write one bounded JSON line atomically to the container stdout pipe."""

    def __init__(self, file_descriptor: int = 1) -> None:
        self._file_descriptor = file_descriptor
        self._lock = threading.Lock()

    def emit(self, record: dict[str, Any]) -> None:
        envelope = {"event": "leaf.instant.accounting", "record": record}
        encoded = json.dumps(
            envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("ascii") + b"\n"
        if len(encoded) > 4096:
            raise AccountingEmissionError("accounting event exceeds the atomic log limit")
        try:
            with self._lock:
                written = os.write(self._file_descriptor, encoded)
        except OSError as exc:
            raise AccountingEmissionError("accounting event could not reach the log driver") from exc
        if written != len(encoded):
            raise AccountingEmissionError("accounting event was only partially written")
