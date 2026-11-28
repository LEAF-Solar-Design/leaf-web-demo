"""Strict tenant run receipts, bounded in the customization authority."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import sqlite3

from branch_tools.sync_receipts import ReceiptLimiter

MAX_BODY_BYTES = 4096
KEEP_PER_VERSION = 200
RETENTION_DAYS = 90
subject_limiter = ReceiptLimiter(capacity=30, refill_per_second=0.5)
tenant_limiter = ReceiptLimiter(capacity=120, refill_per_second=1.0)


class RunReceiptError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


def _matches(pattern, value):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _constant(value):
    raise ValueError("invalid constant")


def _iso(value):
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _stored_time(value):
    # Fixed precision keeps SQLite's indexed text age comparisons chronological.
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse(raw: bytes) -> dict:
    try:
        if not isinstance(raw, bytes) or len(raw) > MAX_BODY_BYTES:
            raise ValueError("body size")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
        required = {"receipt_id", "kind", "package_digest", "plugin_version", "at"}
        optional = {"outcome", "duration_ms", "drawing", "error"}
        if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
            raise ValueError("fields")
        if not (_matches(r"[0-9a-f]{32}", value["receipt_id"])
                and _matches(r"[0-9a-fA-F]{64}", value["package_digest"])
                and _matches(r"[0-9]+(\.[0-9]+){1,3}", value["plugin_version"])
                and _matches(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                             r"(\.[0-9]+)?(Z|[+-]([01][0-9]|2[0-3]):[0-5][0-9])", value["at"])):
            raise ValueError("identity or time")
        value["at"] = _iso(value["at"])
        value["package_digest"] = value["package_digest"].lower()
        kind = value["kind"]
        if not isinstance(kind, str) or kind not in ("arrived", "verify_failed", "ran"):
            raise ValueError("kind")
        run_fields = {"outcome", "duration_ms", "drawing"}
        if kind == "arrived" and value.keys() & optional:
            raise ValueError("arrival fields")
        if kind == "verify_failed" and ("error" not in value or value.keys() & run_fields):
            raise ValueError("verification fields")
        if kind == "ran":
            outcome = value.get("outcome")
            if not isinstance(outcome, str) or outcome not in ("ok", "failed", "cancelled"):
                raise ValueError("outcome")
            if (outcome == "failed" and "error" not in value) or (outcome == "ok" and "error" in value):
                raise ValueError("run error")
        if "duration_ms" in value and (type(value["duration_ms"]) is not int
                                       or not 0 <= value["duration_ms"] <= 600000):
            raise ValueError("duration")
        if "drawing" in value:
            line = value["drawing"]
            if not isinstance(line, str) or len(line) > 1024 or not line.isprintable():
                raise ValueError("drawing")
            parts = line.split(" | ")
            if (len(parts) != 4 or not parts[0] or any(c in parts[0] for c in "\\/:")
                    or not _matches(r"sent copy [0-9a-f]{8}", parts[1])
                    or not _matches(r"[0-9]+ objects? in model space", parts[3])):
                raise ValueError("drawing shape")
        if "error" in value:
            error = value["error"]
            if (not isinstance(error, dict) or set(error) != {"code", "message"}
                    or not _matches(r"[a-z][a-z0-9_]{0,63}", error["code"])
                    or not isinstance(error["message"], str) or len(error["message"]) > 300
                    or (error["message"] and not error["message"].isprintable())):
                raise ValueError("error")
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise RunReceiptError(422, "invalid_receipt") from None


def request_digest(receipt: dict) -> str:
    return hashlib.sha256(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


_SQLITE_TABLE = """
CREATE TABLE IF NOT EXISTS branch_tool_run_receipts (
    receipt_id INTEGER PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) = 32 AND idempotency_key NOT GLOB '*[^0-9a-f]*'),
    request_sha256 TEXT NOT NULL,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    package_digest TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('arrived', 'verify_failed', 'ran')),
    outcome TEXT CHECK (outcome IN ('ok', 'failed', 'cancelled')),
    plugin_version TEXT NOT NULL,
    duration_ms INTEGER CHECK (duration_ms BETWEEN 0 AND 600000),
    drawing_line TEXT CHECK (length(drawing_line) <= 1024),
    error_code TEXT CHECK (length(error_code) <= 64),
    error_message TEXT CHECK (length(error_message) <= 300),
    occurred_at TEXT,
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (tenant_id, idempotency_key)
)
"""


def append(store, tenant_id, subject, name, version, receipt, *, now=None) -> bool:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    occurred = datetime.fromisoformat(receipt["at"].replace("Z", "+00:00"))
    occurred = occurred if now - timedelta(days=7) <= occurred <= now + timedelta(minutes=5) else None
    digest = request_digest(receipt)
    error = receipt.get("error", {})
    key = (tenant_id, name, version)
    with store._transaction() as conn:
        sqlite = isinstance(conn, sqlite3.Connection)
        if sqlite:
            conn.execute(_SQLITE_TABLE)
            conn.execute("CREATE INDEX IF NOT EXISTS branch_tool_run_receipts_package_idx "
                         "ON branch_tool_run_receipts (tenant_id, name, version, receipt_id DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS branch_tool_run_receipts_age_idx "
                         "ON branch_tool_run_receipts (tenant_id, received_at)")
        inserted = conn.execute(
            "INSERT INTO branch_tool_run_receipts (tenant_id, subject, idempotency_key, request_sha256, "
            "name, version, package_digest, kind, outcome, plugin_version, duration_ms, drawing_line, "
            "error_code, error_message, occurred_at, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (tenant_id, idempotency_key) DO NOTHING",
            (tenant_id, subject, receipt["receipt_id"], digest, name, version, receipt["package_digest"],
             receipt["kind"], receipt.get("outcome"), receipt["plugin_version"], receipt.get("duration_ms"),
             receipt.get("drawing"), error.get("code"), error.get("message"),
             _iso(occurred) if sqlite and occurred else occurred, _stored_time(now) if sqlite else now),
        )
        if inserted.rowcount == 0:
            row = conn.execute("SELECT request_sha256 FROM branch_tool_run_receipts "
                               "WHERE tenant_id=? AND idempotency_key=?",
                               (tenant_id, receipt["receipt_id"])).fetchone()
            if row is None or row["request_sha256"] != digest:
                raise RunReceiptError(409, "idempotency_conflict")
            return False
        conn.execute(
            "DELETE FROM branch_tool_run_receipts WHERE tenant_id=? AND name=? AND version=? "
            "AND receipt_id < (SELECT receipt_id FROM branch_tool_run_receipts "
            "WHERE tenant_id=? AND name=? AND version=? ORDER BY receipt_id DESC LIMIT 1 OFFSET ?)",
            (*key, *key, KEEP_PER_VERSION - 1),
        )
        cutoff = now - timedelta(days=RETENTION_DAYS)
        conn.execute("DELETE FROM branch_tool_run_receipts WHERE tenant_id=? AND received_at < ?",
                     (tenant_id, _stored_time(cutoff) if sqlite else cutoff))
    return True


def _missing(conn):
    return isinstance(conn, sqlite3.Connection) and conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='branch_tool_run_receipts'"
    ).fetchone() is None


def _at(row):
    return _iso(row["occurred_at"] or row["received_at"])


def _error(row):
    return {"code": row["error_code"], "message": row["error_message"]} if row["error_code"] is not None else None


def stage_view(store, tenant_id, name, version, digest) -> dict | None:
    with store._connection() as conn:
        if _missing(conn):
            return None
        rows = conn.execute(
            "SELECT * FROM branch_tool_run_receipts WHERE tenant_id=? AND name=? AND version=? "
            "AND package_digest=? ORDER BY receipt_id DESC LIMIT ?",
            (tenant_id, name, version, digest, KEEP_PER_VERSION),
        ).fetchall()
    if not rows:
        return None
    newest = rows[0]
    event = {"arrived": "synced", "verify_failed": "verify_failed", "ran": "ran"}[newest["kind"]]
    if newest["kind"] == "ran" and newest["outcome"] != "ok":
        event = "run_failed"
    result = {"last_event": event, "at": _at(newest), "plugin_version": newest["plugin_version"]}
    arrived = next((row for row in rows if row["kind"] == "arrived"), None)
    failed = next((row for row in rows if row["kind"] == "verify_failed"), None)
    run = next((row for row in rows if row["kind"] == "ran"), None)
    if arrived is not None:
        result["arrived_at"] = _at(arrived)
    if failed is not None and (arrived is None or failed["receipt_id"] > arrived["receipt_id"]):
        result["verify_failed"] = {"at": _at(failed), "error": _error(failed)}
    if run is not None:
        result["last_run"] = {"at": _at(run), "outcome": run["outcome"]}
        for source, target in (("duration_ms", "duration_ms"), ("drawing_line", "drawing")):
            if run[source] is not None:
                result["last_run"][target] = run[source]
        if _error(run) is not None:
            result["last_run"]["error"] = _error(run)
    return result


def latest_run_failure(store, tenant_id, name) -> dict | None:
    with store._connection() as conn:
        if _missing(conn):
            return None
        row = conn.execute(
            "SELECT * FROM branch_tool_run_receipts WHERE tenant_id=? AND name=? "
            "AND kind='ran' AND outcome='failed' ORDER BY receipt_id DESC LIMIT 1", (tenant_id, name),
        ).fetchone()
    return {"version": row["version"], "at": _at(row), "error": _error(row)} if row is not None else None


def support_rows(store, tenant_id, name=None, limit=50) -> list[dict]:
    limit = max(1, min(100, int(limit)))
    with store._connection() as conn:
        if _missing(conn):
            return []
        rows = conn.execute(
            "SELECT * FROM branch_tool_run_receipts WHERE tenant_id=? "
            + ("AND name=? " if name is not None else "") + "ORDER BY receipt_id DESC LIMIT ?",
            (tenant_id, name, limit) if name is not None else (tenant_id, limit),
        ).fetchall()
    result = []
    for row in rows:
        item = {key: row[key] for key in ("name", "version", "package_digest", "kind", "outcome",
                                        "plugin_version", "duration_ms", "subject")}
        item.update(drawing=row["drawing_line"], error=_error(row),
                    occurred_at=_iso(row["occurred_at"]) if row["occurred_at"] else None,
                    received_at=_iso(row["received_at"]))
        result.append(item)
    return result
