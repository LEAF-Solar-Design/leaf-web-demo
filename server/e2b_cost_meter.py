"""Best-effort managed E2B metering from the broker's verified receipt.

V1 does not meter helper failures after boot (helper_error or refused receipts),
a known undercharge. Lifetime is helper startedAt to stoppedAt, including cold
boot because E2B bills it.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP, localcontext


logger = logging.getLogger(__name__)
skipped_unresolved = 0
skipped_invalid = 0
record_failed = 0
_logged_error_classes = set()
_RATE = re.compile(r"[0-9]{1,5}(\.[0-9]{1,6})?")
_UTC_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|\+00:00)"
)


def meter_config(env):
    if env.get("LEAF_COST_METERING") != "1":
        return {"enabled": False, "reason": "flag_not_enabled", "rate": None}
    text = env.get("LEAF_E2B_USD_PER_SECOND")
    if not isinstance(text, str) or not _RATE.fullmatch(text):
        return {"enabled": False, "reason": "invalid_rate", "rate": None}
    rate = Decimal(text)
    if rate <= 0:
        return {"enabled": False, "reason": "nonpositive_rate", "rate": None}
    return {"enabled": True, "reason": "enabled", "rate": rate}


def _platform():
    # Reuse the broker's package loader, including its leaf_platform alias.
    from agent_pg_store import _load_platform

    db, _ = _load_platform()
    from leaf_platform import tenant_cost_store

    return db, tenant_cost_store


def _event_and_reason(org_id, receipt, rate):
    if not isinstance(org_id, (str, uuid.UUID)):
        return None, "unresolved_org"
    try:
        org = uuid.UUID(str(org_id))
    except ValueError:
        return None, "unresolved_org"
    if not isinstance(receipt, dict):
        return None, "invalid_receipt"
    sandbox_id = receipt.get("sandboxId")
    if (not isinstance(sandbox_id, str) or not sandbox_id
            or len(sandbox_id) > 128 or not sandbox_id.isascii()):
        return None, "invalid_sandbox_id"
    timestamps = []
    for field in ("startedAt", "stoppedAt"):
        value = receipt.get(field)
        if not isinstance(value, str) or not _UTC_TIMESTAMP.fullmatch(value):
            return None, "invalid_timestamp"
        try:
            timestamps.append(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None, "invalid_timestamp"
    started, stopped = timestamps
    lifetime = stopped - started
    if lifetime < timedelta(0) or lifetime > timedelta(seconds=3600):
        return None, "invalid_lifetime"
    milliseconds = lifetime // timedelta(milliseconds=1)
    with localcontext() as context:
        context.prec = 32
        quantity = (Decimal(milliseconds) / 1000).quantize(Decimal("0.001"))
        cost = (quantity * rate).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    _, store = _platform()
    return {
        "org_id": org, "addon": "e2b_exec", "mode": "managed",
        "vendor": "e2b", "unit": "sandbox_seconds", "quantity": quantity,
        "unit_cost_usd": rate, "cost_usd": cost, "cost_basis": "estimated",
        "occurred_at": stopped,
        "provenance": {"source": "broker-e2b", "run_ref": sandbox_id},
        "event_key": store.cost_event_key(org, "e2b_exec", "broker-e2b", sandbox_id),
    }, "ok"


def cost_event_from_receipt(org_id, receipt, rate):
    """Return ledger facts or None; _event_and_reason also exposes skip reasons.

    This does no database I/O and does not mutate counters or the receipt.
    """
    event, _ = _event_and_reason(org_id, receipt, rate)
    return event


def record_e2b_cost(org_id, receipt):
    """Meter a verified sandbox without affecting its tool result or error."""
    global skipped_unresolved, skipped_invalid, record_failed
    try:
        config = meter_config(os.environ)
        if not config["enabled"]:
            return
        event, reason = _event_and_reason(org_id, receipt, config["rate"])
        if event is None:
            if reason == "unresolved_org":
                skipped_unresolved += 1
            else:
                skipped_invalid += 1
            return
        db, store = _platform()
        with db.transaction() as conn:
            store.record_cost_event(conn, event)
    except Exception as exc:
        record_failed += 1
        error_class = type(exc)
        if error_class not in _logged_error_classes:
            _logged_error_classes.add(error_class)
            # Log only the class: database exceptions can contain tenant facts.
            try:
                logger.warning("E2B cost recording failed: %s", error_class.__name__)
            except Exception:
                pass
