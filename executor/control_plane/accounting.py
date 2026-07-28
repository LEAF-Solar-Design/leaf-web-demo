"""Durable, payload-free accounting ingestion for instant execution.

The later asynchronous executor delivery lane calls ``AccountingService.ingest``
with the exact JSON object accepted by ``POST /v1/accounting``.  It must first
send ``accepted``, optionally ``started``, then one terminal record.  This
service never calls an executor and never reads or writes Redis.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
import uuid

from .store import StaleFence, StoreUnavailable


MAX_CPU_MS = 86_400_000
MAX_WALL_MS = 86_400_000
MAX_MEMORY_PEAK_BYTES = 64 * 1024 * 1024 * 1024
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CREDENTIAL = re.compile(
    r"(?:bearer\s+|-----BEGIN|(?:api[_-]?key|secret|password|credential|token)\s*[=:]|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{10,}\.)",
    re.IGNORECASE,
)

_IDENTITY_FIELDS = {"invocation_id", "tenant_id", "session_id", "lease_id", "code_digest"}
_BASE_FIELDS = _IDENTITY_FIELDS | {"state", "occurred_at"}
_USAGE_FIELDS = {"outcome", "cpu_ms", "wall_ms", "memory_peak_bytes", "input_bytes", "output_bytes"}


class AccountingError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def _parse_uuid(value, name: str) -> str:
    if not isinstance(value, str):
        raise AccountingError("INVALID_ACCOUNTING", f"{name} must be a UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise AccountingError("INVALID_ACCOUNTING", f"{name} must be a UUID") from exc
    if str(parsed) != value.lower():
        raise AccountingError("INVALID_ACCOUNTING", f"{name} must be a canonical UUID")
    return str(parsed)


def _parse_timestamp(value) -> datetime:
    if not isinstance(value, str) or len(value) > 64 or _CREDENTIAL.search(value):
        raise AccountingError("INVALID_ACCOUNTING", "occurred_at must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AccountingError("INVALID_ACCOUNTING", "occurred_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AccountingError("INVALID_ACCOUNTING", "occurred_at must include an offset")
    now = datetime.now(timezone.utc)
    if parsed < now - timedelta(days=31) or parsed > now + timedelta(minutes=5):
        raise AccountingError("INVALID_ACCOUNTING", "occurred_at is outside the accepted delivery window")
    return parsed.astimezone(timezone.utc)


def _bounded_integer(value, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise AccountingError("INVALID_ACCOUNTING", f"{name} is outside its allowed range")
    return value


def validate_record(payload: dict) -> dict:
    """Validate and normalize one payload-free accounting state transition."""
    if not isinstance(payload, dict):
        raise AccountingError("INVALID_ACCOUNTING", "accounting record must be an object")
    state = payload.get("state")
    expected = _BASE_FIELDS | (_USAGE_FIELDS if state == "terminal" else set())
    if state not in {"accepted", "started", "terminal"} or set(payload) != expected:
        raise AccountingError("INVALID_ACCOUNTING", "accounting record has unknown or missing fields")
    for key, value in payload.items():
        if _CREDENTIAL.search(key) or (isinstance(value, str) and _CREDENTIAL.search(value)):
            raise AccountingError("INVALID_ACCOUNTING", "credentials are not allowed in accounting records")
    tenant_id = payload["tenant_id"]
    if not isinstance(tenant_id, str) or not _IDENTIFIER.fullmatch(tenant_id):
        raise AccountingError("INVALID_ACCOUNTING", "tenant_id is invalid")
    code_digest = payload["code_digest"]
    if not isinstance(code_digest, str) or not _DIGEST.fullmatch(code_digest):
        raise AccountingError("INVALID_ACCOUNTING", "code_digest is invalid")
    record = {
        "invocation_id": _parse_uuid(payload["invocation_id"], "invocation_id"),
        "tenant_id": tenant_id,
        "session_id": _parse_uuid(payload["session_id"], "session_id"),
        "lease_id": _parse_uuid(payload["lease_id"], "lease_id"),
        "code_digest": code_digest,
        "state": state,
        "occurred_at": _parse_timestamp(payload["occurred_at"]),
    }
    if state == "terminal":
        outcome = payload["outcome"]
        if outcome not in {"succeeded", "failed"}:
            raise AccountingError("INVALID_ACCOUNTING", "outcome is invalid")
        record.update({
            "outcome": outcome,
            "cpu_ms": _bounded_integer(payload["cpu_ms"], "cpu_ms", MAX_CPU_MS),
            "wall_ms": _bounded_integer(payload["wall_ms"], "wall_ms", MAX_WALL_MS),
            "memory_peak_bytes": _bounded_integer(payload["memory_peak_bytes"], "memory_peak_bytes", MAX_MEMORY_PEAK_BYTES),
            "input_bytes": _bounded_integer(payload["input_bytes"], "input_bytes", MAX_INPUT_BYTES),
            "output_bytes": _bounded_integer(payload["output_bytes"], "output_bytes", MAX_OUTPUT_BYTES),
        })
    return record


class AccountingService:
    """Narrow durable-ingestion boundary, backed by PostgreSQL only."""
    def __init__(self, store) -> None:
        self._store = store

    def ingest(self, payload: dict) -> dict:
        record = validate_record(payload)
        writer = getattr(self._store, "record_invocation", None)
        if writer is None:
            raise AccountingError("POSTGRES_UNAVAILABLE", "durable accounting storage is unavailable", 503)
        try:
            result = writer(record)
        except StoreUnavailable as exc:
            raise AccountingError("POSTGRES_UNAVAILABLE", "durable accounting storage is unavailable", 503) from exc
        except StaleFence as exc:
            raise AccountingError("ACCOUNTING_CONFLICT", "conflicting durable accounting record", 409) from exc
        if not isinstance(result, dict):
            raise AccountingError("ACCOUNTING_WRITE_FAILED", "durable accounting write did not complete", 503)
        return result
