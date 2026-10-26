"""Validated, connection-scoped tenant cost ledger and add-on settings.

Callers own the transaction. Credential references are names, never secrets.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext


ADDONS = (
    "aps_solve", "e2b_exec", "marathon", "forge_hosting", "aws_resources",
    "storage", "canonical_solve", "tool_ci", "gpu_inference", "ios_build", "app_auth",
    "kashbot_runner",
)
EVENT_FIELDS = (
    "org_id", "addon", "mode", "vendor", "unit", "quantity", "unit_cost_usd",
    "cost_usd", "cost_basis", "event_key", "occurred_at", "provenance",
)
# Provenance must be strictly below 16 KiB; queries return at most 10000 events.
MAX_PROVENANCE_BYTES = 16 * 1024
MAX_LIST_LIMIT = 10000
# NUMERIC(18,6) has 12 integral digits and six fractional digits.
MONEY_CEILING = Decimal("1000000000000")
MONEY_QUANTUM = Decimal("0.000001")


class CostEventConflict(RuntimeError):
    """An event key was replayed with different facts."""


def _org_id(value):
    if not isinstance(value, (str, uuid.UUID)):
        raise ValueError("org_id must be a UUID")
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise ValueError("org_id must be a UUID") from exc


def _enum(field, value, choices):
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{field} must be one of {choices}")
    return value


def _text(field, value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{field} must be nonempty text of at most {maximum} characters")
    return value


def _timestamp(field, value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be an aware datetime")
    try:
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be an aware datetime") from exc


def _number(field, value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{field} must be a non-negative finite number")
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            raise ValueError(f"{field} must be a non-negative finite number")
        if field != "quantity":
            if number >= MONEY_CEILING:
                raise ValueError(f"{field} exceeds numeric(18,6)")
            with localcontext() as context:
                context.prec = max(32, len(number.as_tuple().digits))
                if number != number.quantize(MONEY_QUANTUM):
                    raise ValueError(f"{field} exceeds six fractional digits")
        return number
    except (InvalidOperation, OverflowError, ValueError) as exc:
        raise ValueError(f"{field} must fit its numeric column") from exc


def _json_value(value):
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_value(item) for key, item in value.items())
    if isinstance(value, list):
        return all(_json_value(item) for item in value)
    return value is None or isinstance(value, (str, bool, int, float))


def validate_cost_event(event: dict) -> dict:
    """Return a normalized copy or a field-naming ValueError, without DB access."""
    if not isinstance(event, dict):
        raise ValueError("event must be a dict")
    for key in event:
        if key not in EVENT_FIELDS:
            raise ValueError(f"unknown field: {key}")
    for field in EVENT_FIELDS:
        if field not in event:
            raise ValueError(f"{field} is required")
    result = dict(event)
    result["org_id"] = _org_id(event["org_id"])
    result["addon"] = _enum("addon", event["addon"], ADDONS)
    result["mode"] = _enum("mode", event["mode"], ("managed", "byo"))
    result["cost_basis"] = _enum("cost_basis", event["cost_basis"], ("measured", "estimated", "allocated"))
    # Vendor names allow 100 characters; units allow 50 characters.
    result["vendor"] = _text("vendor", event["vendor"], 100)
    result["unit"] = _text("unit", event["unit"], 50)
    for field in ("quantity", "unit_cost_usd", "cost_usd"):
        result[field] = _number(field, event[field])
    # Event keys are SHA-256 digests: 64 lowercase hexadecimal characters.
    if not isinstance(event["event_key"], str) or not re.fullmatch(r"[0-9a-f]{64}", event["event_key"]):
        raise ValueError("event_key must be a lowercase SHA-256 digest")
    result["occurred_at"] = _timestamp("occurred_at", event["occurred_at"])
    try:
        provenance = event["provenance"]
        if not isinstance(provenance, dict) or not _json_value(provenance):
            raise ValueError("provenance must be a JSON object")
        encoded = json.dumps(provenance, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) >= MAX_PROVENANCE_BYTES:
            raise ValueError("provenance must be under 16 KiB")
        result["provenance"] = json.loads(encoded)
    except (TypeError, ValueError, RecursionError, OverflowError) as exc:
        raise ValueError("provenance must be a JSON object under 16 KiB") from exc
    if result["mode"] == "byo" and result["cost_usd"] != 0:
        raise ValueError("cost_usd must be zero for mode byo")
    return result


def cost_event_key(org_id, addon, source, source_ref) -> str:
    """Hash an unambiguous, domain-separated canonical tuple of source identity."""
    org = _org_id(org_id)
    _enum("addon", addon, ADDONS)
    for field, value in (("source", source), ("source_ref", source_ref)):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} must be nonempty text")
    canonical = json.dumps(["leaf-tenant-cost:v1", str(org), addon, source, source_ref],
                           ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def record_cost_event(conn, event):
    """Append or verify an identical replay, without committing the transaction."""
    facts = validate_cost_event(event)

    from psycopg.types.json import Jsonb

    params = {**facts, "provenance": Jsonb(facts["provenance"])}
    columns = ", ".join(EVENT_FIELDS)
    placeholders = ", ".join(f"%({field})s" for field in EVENT_FIELDS)
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO tenant_cost_events ({columns}) VALUES ({placeholders}) "
            "ON CONFLICT (event_key) DO NOTHING RETURNING id", params)
        if cur.fetchone() is not None:
            return "inserted"
        # A separate statement sees a concurrent committed insert under READ COMMITTED.
        matches = " AND ".join(f"{field} IS NOT DISTINCT FROM %({field})s" for field in EVENT_FIELDS)
        cur.execute(f"SELECT id FROM tenant_cost_events WHERE {matches}", params)
        if cur.fetchone() is None:
            raise CostEventConflict("event_key was replayed with different values")
    return "duplicate"


def list_cost_events(conn, org_id, start, end, *, addon=None, limit=MAX_LIST_LIMIT):
    """Read a bounded half-open period in stable timestamp, identity order."""
    org = _org_id(org_id)
    start, end = _timestamp("start", start), _timestamp("end", end)
    if start >= end:
        raise ValueError("end must be after start")
    # A request must return at least 1 and at most MAX_LIST_LIMIT events.
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIST_LIMIT:
        raise ValueError("limit must be between 1 and 10000")
    if addon is not None:
        _enum("addon", addon, ADDONS)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM tenant_cost_events WHERE org_id = %(org_id)s "
            "AND occurred_at >= %(start)s AND occurred_at < %(end)s "
            + ("AND addon = %(addon)s " if addon is not None else "")
            + "ORDER BY occurred_at, id LIMIT %(limit)s",
            {"org_id": org, "start": start, "end": end, "addon": addon, "limit": limit})
        return cur.fetchall()


def get_addon_settings(conn, org_id) -> dict:
    """Return every known add-on, defaulting absent rows to off."""
    org = _org_id(org_id)
    result = {addon: {"mode": "off", "byo_ref": None} for addon in ADDONS}
    with conn.cursor() as cur:
        cur.execute("SELECT addon, mode, byo_ref FROM tenant_addon_settings WHERE org_id = %s", (org,))
        for row in cur.fetchall():
            if row["addon"] not in ADDONS:
                continue
            result[row["addon"]] = {"mode": row["mode"], "byo_ref": row["byo_ref"]}
    return result


def validate_addon_mode(org_id, addon, mode, *, byo_ref=None, actor) -> dict:
    """Validate settings independently of a connection."""
    org = _org_id(org_id)
    _enum("addon", addon, ADDONS)
    if addon == "kashbot_runner" and mode not in ("byo", "off"):
        raise ValueError("mode must be byo or off for kashbot_runner")
    _enum("mode", mode, ("managed", "byo", "off"))
    # Credential names have 1 to 128 characters; actors have at most 200.
    if mode == "byo" and addon == "kashbot_runner":
        message = "byo_ref must be kashbot-runner:<runner_uuid>"
        if not isinstance(byo_ref, str) or not byo_ref.startswith("kashbot-runner:"):
            raise ValueError(message)
        runner_id = byo_ref[len("kashbot-runner:"):]
        try:
            canonical = str(uuid.UUID(runner_id))
        except ValueError as exc:
            raise ValueError(message) from exc
        if runner_id != canonical:
            raise ValueError(message)
    elif mode == "byo":
        if not isinstance(byo_ref, str) or not re.fullmatch(r"[A-Za-z0-9_./:-]{1,128}", byo_ref):
            raise ValueError("byo_ref must be a credential name of 1 to 128 characters")
    elif byo_ref is not None:
        raise ValueError("byo_ref must be null unless mode is byo")
    return {"org_id": org, "addon": addon, "mode": mode, "byo_ref": byo_ref,
            "actor": _text("actor", actor, 200)}


def set_addon_mode(conn, org_id, addon, mode, *, byo_ref=None, actor):
    """Set the current mode and audit actor in the caller's transaction."""
    params = validate_addon_mode(org_id, addon, mode, byo_ref=byo_ref, actor=actor)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenant_addon_settings (org_id, addon, mode, byo_ref, updated_at, updated_by) "
            "VALUES (%(org_id)s, %(addon)s, %(mode)s, %(byo_ref)s, NOW(), %(actor)s) "
            "ON CONFLICT (org_id, addon) DO UPDATE SET mode = EXCLUDED.mode, "
            "byo_ref = EXCLUDED.byo_ref, updated_at = EXCLUDED.updated_at, updated_by = EXCLUDED.updated_by",
            params)
