"""Copy broker APS costs into the tenant ledger in the caller's transaction."""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from .tenant_cost_store import MONEY_QUANTUM, cost_event_key, record_cost_event


MAX_LIMIT = 5000
# broker.py reports success as ok and cost-bearing failed WorkItems as WORKITEM_FAILED.
ENGINE_TERMINAL_STATUSES = frozenset({"ok", "WORKITEM_FAILED"})


def sync_aps_cost_events(conn, *, since_ts: float, until_ts: float,
                         after_event_key=None, limit: int = MAX_LIMIT) -> dict:
    """Read a bounded half-open period; identical broker events are safe to replay.

    Pass next's since_ts and after_event_key back until more is false to page
    through timestamp ties without repeating rows.
    """
    for name, value in (("since_ts", since_ts), ("until_ts", until_ts)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite epoch timestamp")
    if since_ts >= until_ts:
        raise ValueError("until_ts must be after since_ts")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError("limit must be between 1 and 5000")

    result = dict(read=0, inserted=0, duplicate=0, skipped_not_live=0,
                  skipped_unresolved_tenant=0, skipped_incomplete=0, next=None, more=False)
    lower_bound = "l.ts >= %(since_ts)s"
    params = {"since_ts": since_ts, "until_ts": until_ts, "limit": limit}
    if after_event_key is not None:
        lower_bound = "(l.ts, l.event_key) > (%(since_ts)s, %(after_event_key)s)"
        params["after_event_key"] = after_event_key
    with conn.cursor() as cur:
        # Match the broker/org attribution used by unit_economics.fleet_report.
        # LEFT JOIN retains unresolved rows so they are counted, never billed.
        cur.execute(
            "SELECT l.event_key, l.ts, l.tenant_id, l.tool, l.engine_op, l.aps_live, "
            "l.status, l.engine_seconds, l.usd_est, o.org_id "
            "FROM broker_usage_ledger l LEFT JOIN orgs o ON o.org_id::text = l.tenant_id "
            f"WHERE {lower_bound} AND l.ts < %(until_ts)s "
            "ORDER BY l.ts, l.event_key LIMIT %(limit)s",
            params)
        rows = cur.fetchall()
    result["more"] = len(rows) == limit
    for row in rows:
        result["read"] += 1
        result["next"] = {"since_ts": row["ts"], "after_event_key": row["event_key"]}
        if row["aps_live"] is not True:
            result["skipped_not_live"] += 1
            continue
        if (row["engine_seconds"] is None or row["usd_est"] is None
                or row["status"] not in ENGINE_TERMINAL_STATUSES):
            result["skipped_incomplete"] += 1
            continue
        if row["org_id"] is None:
            result["skipped_unresolved_tenant"] += 1
            continue
        seconds, usd = Decimal(str(row["engine_seconds"])), Decimal(str(row["usd_est"]))
        # The destination uses NUMERIC(18,6); its validator disallows implicit rounding.
        cost = usd.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
        unit_cost = (usd / seconds if seconds else Decimal(0)).quantize(
            MONEY_QUANTUM, rounding=ROUND_HALF_UP)
        event = {
            "org_id": row["org_id"], "addon": "aps_solve", "mode": "managed",
            "vendor": "autodesk-aps", "unit": "engine_seconds", "quantity": seconds,
            "cost_usd": cost, "unit_cost_usd": unit_cost, "cost_basis": "estimated",
            "occurred_at": datetime.fromtimestamp(row["ts"], tz=timezone.utc),
            "event_key": cost_event_key(row["org_id"], "aps_solve", "broker_usage_ledger", row["event_key"]),
            "provenance": {"source": "broker_usage_ledger", "broker_event_key": row["event_key"],
                           "tool": row["tool"], "engine_op": row["engine_op"]},
        }
        result[record_cost_event(conn, event)] += 1
    return result


def _iso_epoch(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone is required")
        return parsed.timestamp()
    except (ValueError, OverflowError) as exc:
        raise argparse.ArgumentTypeError("expected an ISO timestamp with timezone") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True, type=_iso_epoch)
    parser.add_argument("--until", required=True, type=_iso_epoch)
    parser.add_argument("--after-key")
    args = parser.parse_args(argv)
    from .db import connection

    with connection() as conn:
        result = sync_aps_cost_events(conn, since_ts=args.since, until_ts=args.until,
                                     after_event_key=args.after_key)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
