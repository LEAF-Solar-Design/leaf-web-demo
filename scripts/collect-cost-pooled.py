#!/usr/bin/env python3
"""Collect one month of pooled AWS usage split by the Environment tag as usage observations (TCM-08).

Transparency of Leaf's real cost, never billing. Prints JSON Lines to stdout.
Uses the default AWS credential chain and calls only ce:GetCostAndUsage. The
production share is split by tenant active days read from the agent and broker
ledgers through cost_meter.direct_usage's own loaders. It never writes the cost
ledger; a later publisher slice joins these to cost.

    python scripts/collect-cost-pooled.py [--period YYYY-MM] [--exclude RESOURCE_ID ...]

Exit codes: 0 written, 1 fetch or parse failure, 2 usage.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from cost_meter import direct_usage, pooled  # noqa: E402

CE_REGION = "us-east-1"  # Cost Explorer's only endpoint


def _ce_client():
    import boto3  # deferred: the default credential chain is resolved only when collecting

    return boto3.client("ce", region_name=CE_REGION)


def main(argv=None, *, client=None, stdout=None, now=None, agent_rows=None, broker_rows=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--period", help="calendar month YYYY-MM (default: the current UTC month)")
    parser.add_argument("--exclude", action="append", default=None, metavar="RESOURCE_ID",
                        help="resource id with a direct collector to skip (repeatable; default: "
                             + ", ".join(pooled.DIRECT_COLLECTOR_RESOURCE_IDS) + ")")
    args = parser.parse_args(argv)
    stdout = sys.stdout if stdout is None else stdout
    now = datetime.now(timezone.utc) if now is None else now
    period = args.period or now.strftime("%Y-%m")
    if not pooled.PERIOD_RE.fullmatch(period):
        print(f"collect-cost-pooled: --period must be YYYY-MM, got {period!r}", file=sys.stderr)
        return 2
    excludes = tuple(args.exclude) if args.exclude is not None else pooled.DIRECT_COLLECTOR_RESOURCE_IDS
    try:
        if agent_rows is None:
            agent_rows = direct_usage.load_agent_rows()
        if broker_rows is None:
            broker_rows = direct_usage.load_broker_rows(period=period)
        activity = pooled.tenant_activity_from_rows(period, agent_rows, broker_rows)
        responses = pooled.fetch_environment_split(client if client is not None else _ce_client(), period,
                                                   today=now.astimezone(timezone.utc).date())
        observations = pooled.pooled_usage_observations(period, responses, activity,
                                                        exclude_resource_ids=excludes)
    except Exception as exc:  # noqa: BLE001 - one named failure line, nothing partial written
        print(f"collect-cost-pooled: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    text = "".join(json.dumps(o, sort_keys=True, separators=(",", ":")) + "\n" for o in observations)
    stdout.write(text)
    stdout.flush()
    print(f"collect-cost-pooled: {len(observations)} usage observations for {period}, "
          f"{len(activity)} active tenants", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
