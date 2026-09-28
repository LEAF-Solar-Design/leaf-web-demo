#!/usr/bin/env python3
"""Collect one month of Leaf's AWS cost from Cost Explorer as cost observations (TCM-06).

Transparency of Leaf's real cost, never billing. Prints JSON Lines to stdout, or
appends them to the file named by LEAF_COST_OBSERVATIONS when set. Uses the
default AWS credential chain and calls only ce:GetCostAndUsage. It never writes
the cost ledger; a later publisher slice joins these to usage.

    python scripts/collect-cost-aws.py [--period YYYY-MM]

Exit codes: 0 written, 1 fetch or parse failure, 2 usage.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from cost_meter import aws_import  # noqa: E402

ENV_OUTPUT = "LEAF_COST_OBSERVATIONS"
CE_REGION = "us-east-1"  # Cost Explorer's only endpoint


def _ce_client():
    import boto3  # deferred: the default credential chain is resolved only when collecting

    return boto3.client("ce", region_name=CE_REGION)


def main(argv=None, *, client=None, environ=None, stdout=None, now=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--period", help="calendar month YYYY-MM (default: the current UTC month)")
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ
    stdout = sys.stdout if stdout is None else stdout
    now = datetime.now(timezone.utc) if now is None else now
    period = args.period or now.strftime("%Y-%m")
    if not aws_import.PERIOD_RE.fullmatch(period):
        print(f"collect-cost-aws: --period must be YYYY-MM, got {period!r}", file=sys.stderr)
        return 2
    try:
        responses = aws_import.fetch(client if client is not None else _ce_client(), period,
                                     today=now.astimezone(timezone.utc).date())
        observations = aws_import.to_cost_observations(period, responses, now)
    except Exception as exc:  # noqa: BLE001 - one named failure line, nothing partial written
        print(f"collect-cost-aws: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    text = "".join(json.dumps(o, sort_keys=True, separators=(",", ":")) + "\n" for o in observations)
    target = environ.get(ENV_OUTPUT)
    if target:
        with open(target, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
    else:
        stdout.write(text)
        stdout.flush()
    print(f"collect-cost-aws: {len(observations)} cost observations for {period}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
