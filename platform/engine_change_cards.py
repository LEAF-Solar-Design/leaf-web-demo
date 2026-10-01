"""Retained acceptance cards; controller lifecycle and operator receipts only."""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from typing import Any, Mapping

from psycopg.types.json import Jsonb

from . import db

JSON_LIMIT = 32 * 1024
TEXT_LIMITS = {
    "incident_fingerprint": 256, "feature_id": 256, "title": 200,
    "summary": 4000, "deployment_identity": 256,
}
JSON_FIELDS = ("change", "evidence", "acceptance")
STATES = {
    "accepted": {"landed", "reverted", "held"},
    "landed": {"live", "reverted", "held"},
    "live": {"reverted", "held"}, "reverted": set(), "held": set(),
}
_TOKEN = re.compile(
    r"\bbearer\s+\S+|(?<![\w-])(?:eyJ[\w-]*\.[\w-]+\.[\w-]+|"
    r"[\w-]{8,}\.[\w-]{8,}\.[\w-]+)(?![\w-])", re.IGNORECASE | re.ASCII,
)
_FIELDS = tuple(TEXT_LIMITS) + JSON_FIELDS + ("state",)
_IMMUTABLE = tuple(key for key in _FIELDS if key not in {"state", "deployment_identity"})


class CardConflict(ValueError):
    """An operation id cannot be reused to rewrite accepted facts."""


def _text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= limit or not value.strip():
        raise ValueError(f"{name} must be nonblank text of at most {limit} characters")
    if _TOKEN.search(value) or any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        raise ValueError(f"{name} contains forbidden content")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be UTF-8 text") from exc
    return value


def _json(value: Any, name: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    remaining = 4096

    def visit(item, depth):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 16:
            raise ValueError(f"{name} is too complex")
        if isinstance(item, str):
            # Empty JSON strings are allowed, but still scanned and bounded.
            _text(item or " ", name, 4096)
        elif isinstance(item, dict):
            for key, child in item.items():
                _text(key, name, 128)
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
        elif item is None or isinstance(item, (bool, int)):
            if isinstance(item, int) and item.bit_length() > 64:
                raise ValueError(f"{name} number is too large")
        elif isinstance(item, float) and math.isfinite(item):
            pass
        else:
            raise ValueError(f"{name} must contain JSON values")

    visit(value, 0)
    encoded = json.dumps(value, sort_keys=True, allow_nan=False)
    if len(encoded.encode("utf-8")) > JSON_LIMIT:
        raise ValueError(f"{name} exceeds 32 KiB")
    return json.loads(encoded)


def validate_card(operation_id: str, fields: Mapping[str, Any]) -> dict:
    """Normalize a complete publisher payload and compute its digest ourselves."""
    operation_id = _text(operation_id, "operation_id", 256)
    if not isinstance(fields, Mapping) or set(fields) - set(_FIELDS) - {"payload_sha256"}:
        raise ValueError("unknown card fields")
    normalized = {"operation_id": operation_id}
    for name, limit in TEXT_LIMITS.items():
        value = fields.get(name)
        normalized[name] = None if name == "deployment_identity" and value is None else _text(value, name, limit)
    for name in JSON_FIELDS:
        normalized[name] = _json(fields.get(name), name)
    state = fields.get("state", "accepted")
    if not isinstance(state, str) or state not in STATES:
        raise ValueError("invalid card state")
    normalized["state"] = state
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    if "payload_sha256" in fields and fields["payload_sha256"] != digest:
        raise ValueError("payload_sha256 does not match card fields")
    normalized["payload_sha256"] = digest
    return normalized


def _card_id(value) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("invalid card_id") from exc


def upsert_card(operation_id: str, fields: Mapping[str, Any]) -> dict:
    values = validate_card(operation_id, fields)
    columns = ("operation_id",) + _FIELDS + ("payload_sha256",)
    params = {key: Jsonb(value) if key in JSON_FIELDS else value for key, value in values.items()}
    params["card_id"] = uuid.uuid4()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO engine_change_cards (card_id, " + ", ".join(columns) + ") VALUES "
            "(%(card_id)s, " + ", ".join(f"%({key})s" for key in columns) + ") "
            "ON CONFLICT (operation_id) DO NOTHING", params,
        )
        cur.execute("SELECT * FROM engine_change_cards WHERE operation_id = %s FOR UPDATE", (operation_id,))
        row = cur.fetchone()
        if row["payload_sha256"] == values["payload_sha256"]:
            return dict(row)
        if (values["state"] not in STATES[row["state"]]
                or any(row[key] != values[key] for key in _IMMUTABLE)
                or (row["deployment_identity"] is not None
                    and row["deployment_identity"] != values["deployment_identity"])):
            raise CardConflict("operation_id conflicts with retained card or legal state advance")
        cur.execute(
            "UPDATE engine_change_cards SET state = %(state)s, "
            "deployment_identity = %(deployment_identity)s, payload_sha256 = %(payload_sha256)s, "
            "updated_at = clock_timestamp() WHERE operation_id = %(operation_id)s RETURNING *", params,
        )
        return dict(cur.fetchone())


def list_cards(subject: str, limit: int = 100, before: str | None = None) -> dict:
    subject = _text(subject, "subject", 256)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    cursor_id = _card_id(before) if before is not None else None
    # One snapshot keeps unread_count coherent with the returned flags.
    with db.transaction(isolation="repeatable read", read_only=True) as conn:
        with conn.cursor() as cur:
            boundary = ""
            params = {"subject": subject, "limit": limit}
            if cursor_id is not None:
                cur.execute("SELECT created_at, card_id FROM engine_change_cards WHERE card_id = %s", (cursor_id,))
                cursor = cur.fetchone()
                if cursor is None:
                    raise ValueError("before cursor is unknown")
                params.update(cursor)
                boundary = "WHERE (c.created_at, c.card_id) < (%(created_at)s, %(card_id)s) "
            cur.execute(
                "SELECT c.*, NOT EXISTS (SELECT 1 FROM engine_change_card_reads r "
                "WHERE r.card_id = c.card_id AND r.subject = %(subject)s) AS unread "
                "FROM engine_change_cards c " + boundary +
                "ORDER BY c.created_at DESC, c.card_id DESC LIMIT %(limit)s", params,
            )
            cards = [dict(row) for row in cur.fetchall()]
            cur.execute(
                "SELECT count(*) AS unread_count FROM engine_change_cards c WHERE NOT EXISTS "
                "(SELECT 1 FROM engine_change_card_reads r WHERE r.card_id = c.card_id AND r.subject = %s)",
                (subject,),
            )
            return {"cards": cards, "unread_count": cur.fetchone()["unread_count"],
                    "next_cursor": str(cards[-1]["card_id"]) if len(cards) == limit else None}


def get_card(card_id: str) -> dict | None:
    with db.cursor() as cur:
        cur.execute("SELECT * FROM engine_change_cards WHERE card_id = %s", (_card_id(card_id),))
        row = cur.fetchone()
        return dict(row) if row is not None else None


def mark_read(card_id: str, subject: str) -> dict | None:
    card_id = _card_id(card_id)
    subject = _text(subject, "subject", 256)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO engine_change_card_reads (card_id, subject) "
            "SELECT card_id, %s FROM engine_change_cards WHERE card_id = %s "
            "ON CONFLICT (card_id, subject) DO NOTHING", (subject, card_id),
        )
        cur.execute("SELECT * FROM engine_change_card_reads WHERE card_id = %s AND subject = %s", (card_id, subject))
        row = cur.fetchone()
        return dict(row) if row is not None else None


def request_hold(card_id: str, subject: str) -> dict | None:
    card_id = _card_id(card_id)
    subject = _text(subject, "subject", 256)
    with db.cursor() as cur:
        cur.execute(
            "UPDATE engine_change_cards SET hold_requested_at = clock_timestamp(), "
            "hold_requested_by = %s, updated_at = clock_timestamp() "
            "WHERE card_id = %s AND hold_requested_at IS NULL", (subject, card_id),
        )
        cur.execute("SELECT * FROM engine_change_cards WHERE card_id = %s", (card_id,))
        row = cur.fetchone()
        return dict(row) if row is not None else None
