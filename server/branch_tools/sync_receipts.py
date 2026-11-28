"""Bounded Branch receipts in the selected customization authority."""

from collections import OrderedDict
from datetime import datetime, timezone
import json
import sqlite3
from threading import Lock
import time
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, with_config
from typing_extensions import NotRequired, TypedDict


@with_config(ConfigDict(extra="forbid", strict=True))
class Diagnostic(TypedDict):
    code: Annotated[str, Field(pattern=r"^(CS[0-9]{4}|scan|unparsed|timeout|denied)$")]
    line: NotRequired[Annotated[int, Field(ge=0, le=1000000)]]


@with_config(ConfigDict(extra="forbid", strict=True))
class Receipt(TypedDict):
    event: Literal["synced", "compile_failed", "ran", "run_failed"]
    plugin_version: Annotated[str, Field(pattern=r"^[0-9]+(\.[0-9]+){1,3}$")]
    package_digest: Annotated[str, Field(pattern=r"^[0-9a-fA-F]{64}$")]
    duration_ms: NotRequired[Annotated[int, Field(ge=0, le=600000)]]
    diagnostics: NotRequired[Annotated[list[Diagnostic], Field(max_length=10)]]


class ReceiptLimiter:
    """Bounded token buckets with configurable burst and refill rates."""

    def __init__(self, clock=time.monotonic, capacity=60, refill_per_second=1.0):
        self._clock = clock
        self._capacity = capacity
        self._refill_per_second = refill_per_second
        self._lock = Lock()
        self._buckets = OrderedDict()

    def admit(self, subject: str) -> bool:
        with self._lock:
            now = self._clock()
            if subject not in self._buckets and len(self._buckets) >= 10000:
                # Evict only a fully replenished bucket. Churning identities
                # must not give an exhausted subject a fresh burst.
                oldest_tokens, oldest_at = next(iter(self._buckets.values()))
                if oldest_tokens + (now - oldest_at) * self._refill_per_second < self._capacity:
                    return False
                self._buckets.popitem(last=False)
            tokens, last = self._buckets.get(subject, (self._capacity, now))
            available = min(self._capacity, tokens + (now - last) * self._refill_per_second)
            if available < 1:
                return False
            self._buckets[subject] = (available - 1, now)
            self._buckets.move_to_end(subject)
            return True


limiter = ReceiptLimiter()

_SQLITE_TABLE = """
CREATE TABLE IF NOT EXISTS branch_tool_receipts (
    receipt_id INTEGER PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    package_digest TEXT NOT NULL,
    event TEXT NOT NULL CHECK (event IN ('synced', 'compile_failed', 'ran', 'run_failed')),
    plugin_version TEXT NOT NULL,
    duration_ms INTEGER CHECK (duration_ms BETWEEN 0 AND 600000),
    diagnostics_json TEXT,
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
)
"""


def append(store, tenant_id: str, subject: str, name: str, version: str, receipt: Receipt) -> None:
    key = (tenant_id, name, version)
    diagnostics = json.dumps(receipt["diagnostics"]) if "diagnostics" in receipt else None
    # Both customization backends serialize writers for the entire transaction.
    with store._transaction() as conn:
        if isinstance(conn, sqlite3.Connection):
            conn.execute(_SQLITE_TABLE)
            conn.execute("CREATE INDEX IF NOT EXISTS branch_tool_receipts_package_idx "
                         "ON branch_tool_receipts (tenant_id, name, version, receipt_id DESC)")
        conn.execute(
            "INSERT INTO branch_tool_receipts (tenant_id, name, version, subject, package_digest, "
            "event, plugin_version, duration_ms, diagnostics_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*key, subject, receipt["package_digest"].lower(), receipt["event"],
             receipt["plugin_version"], receipt.get("duration_ms"), diagnostics),
        )
        conn.execute(
            "DELETE FROM branch_tool_receipts WHERE tenant_id=? AND name=? AND version=? "
            "AND receipt_id < (SELECT receipt_id FROM branch_tool_receipts "
            "WHERE tenant_id=? AND name=? AND version=? ORDER BY receipt_id DESC LIMIT 1 OFFSET 999)",
            (*key, *key),
        )


def latest(store, tenant_id: str, name: str, version: str, digest: str) -> dict | None:
    with store._connection() as conn:
        # Older local databases acquire the mirror on their first receipt.
        if isinstance(conn, sqlite3.Connection) and conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='branch_tool_receipts'"
        ).fetchone() is None:
            return None
        row = conn.execute(
            "SELECT event, plugin_version, received_at, diagnostics_json FROM branch_tool_receipts "
            "WHERE tenant_id=? AND name=? AND version=? AND package_digest=? "
            "ORDER BY receipt_id DESC LIMIT 1", (tenant_id, name, version, digest),
        ).fetchone()
    if row is None:
        return None
    at = row["received_at"]
    if isinstance(at, datetime):
        at = at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    result = {"last_event": row["event"], "at": at, "plugin_version": row["plugin_version"]}
    if row["diagnostics_json"] is not None:
        result["diagnostics"] = json.loads(row["diagnostics_json"])
    return result
