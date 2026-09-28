#!/usr/bin/env python3
"""Publish one month of Leaf's resource share ledger (TCM-09a).

Transparency of Leaf's real cost only, never billing. Runs the collectors for the
period, merges any --observations files (JSON Lines of usage or cost observations,
for example the pooled split from scripts/collect-cost-pooled.py), joins usage to
cost by resource_id and publishes into LEAF_COST_LEDGER_DIR. --dry-run prints the
would-be ledger as JSON and writes nothing.

Collectors:
  aws-cost-explorer   ce:GetCostAndUsage through the default AWS credential chain
  cost-vendors        cost_meter/data/cost-vendors.yaml
  internal-resources  cost_meter/data/cost-internal-resources.yaml
  storage-snapshots   LEAF_COST_STORAGE_SNAPSHOTS (skipped and recorded missing when unset)
  broker-ledger       the broker attribution ledger through direct_usage.load_broker_rows
A collector that fails is reported on stderr and skipped; the publication records
which sources were missing. A source whose observations do not validate is
dropped whole, never half-used.

    python -m cost_meter publish --period 2026-09 [--dry-run] [--observations FILE ...]

Exit codes: 0 published (or printed), 1 nothing to publish or the publish failed, 2 usage.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from cost_meter import aws_import, direct_usage, internal, publisher, storage, vendors  # noqa: E402
from cost_meter.ledger import PERIOD_RE  # noqa: E402
from cost_meter.store import ENV_DIR, CostLedgerStore  # noqa: E402

CE_REGION = "us-east-1"  # Cost Explorer's only endpoint
MAX_OBSERVATION_LINES = 100_000
MAX_OBSERVATION_LINE_BYTES = 1024 * 1024

Collector = Callable[[str, datetime], List[dict]]


class SourceMissing(RuntimeError):
    """The source is not configured or holds nothing readable for the period."""


def _collect_aws(period: str, now: datetime) -> List[dict]:
    import boto3  # deferred: the default credential chain is resolved only when collecting

    client = boto3.client("ce", region_name=CE_REGION)
    responses = aws_import.fetch(client, period, today=now.astimezone(timezone.utc).date())
    return aws_import.to_cost_observations(period, responses, now)


def _collect_vendors(period: str, now: datetime) -> List[dict]:
    return vendors.vendor_observations(period, vendors.load_vendor_config())


def _collect_internal(period: str, now: datetime) -> List[dict]:
    return internal.internal_usage_observations(period, internal.load_internal_config())


def _read_bounded_lines(path: str) -> List[str]:
    """At most MAX_OBSERVATION_LINES non-empty lines, each capped; an oversized line refuses the file."""
    lines: List[str] = []
    with open(path, "rb") as fh:
        while True:
            raw = fh.readline(MAX_OBSERVATION_LINE_BYTES + 1)
            if not raw:
                return lines
            if len(raw) > MAX_OBSERVATION_LINE_BYTES:
                raise ValueError(f"{path}: a line exceeds {MAX_OBSERVATION_LINE_BYTES} bytes")
            text = raw.decode("utf-8").strip()
            if text:
                if len(lines) >= MAX_OBSERVATION_LINES:
                    raise ValueError(f"{path}: more than {MAX_OBSERVATION_LINES} lines")
                lines.append(text)


def _collect_storage(period: str, now: datetime) -> List[dict]:
    path = os.environ.get(storage.SNAPSHOTS_ENV, "").strip()
    if not path:
        raise SourceMissing(f"{storage.SNAPSHOTS_ENV} is not set")
    lines = _read_bounded_lines(path) if os.path.exists(path) else []
    return [storage.storage_usage_observation(period, lines, now=now)]


def _collect_broker(period: str, now: datetime) -> List[dict]:
    rows = direct_usage.load_broker_rows(period=period)
    if rows is None:
        raise SourceMissing("broker ledger rows are unreadable")
    return [direct_usage.aps_usage_observation(period, rows)]


DEFAULT_COLLECTORS: Dict[str, Collector] = {
    "aws-cost-explorer": _collect_aws,
    "cost-vendors": _collect_vendors,
    "storage-snapshots": _collect_storage,
    "broker-ledger": _collect_broker,
    "internal-resources": _collect_internal,
}


def _read_observations(path: str, period: str) -> Tuple[List[dict], int]:
    """JSON Lines of observations; lines for another period are ignored and counted."""
    kept: List[dict] = []
    other = 0
    for number, text in enumerate(_read_bounded_lines(path), 1):
        obs = json.loads(text, parse_float=_refuse_float)
        if not isinstance(obs, dict):
            raise ValueError(f"{path}:{number}: an observation must be a JSON object")
        if obs.get("period") != period:
            other += 1
            continue
        kept.append(obs)
    return kept, other


def _refuse_float(text: str) -> Any:
    raise ValueError(f"amounts must be decimal strings, not the JSON number {text}")


def _gather(period: str, now: datetime, collectors: Mapping[str, Collector],
            observation_files: List[str], log) -> Tuple[List[dict], List[str], List[str]]:
    """Every usable source's observations, the sources used, and the sources missing."""
    merged: List[dict] = []
    used: List[str] = []
    missing: List[str] = []

    def take(name: str, observations: List[dict]) -> None:
        try:
            publisher.build_period(period, observations)  # a source that does not validate is dropped whole
        except Exception as exc:  # noqa: BLE001 - reported, skipped, recorded missing
            log(f"publish-cost-ledger: {name} skipped, invalid observations: {type(exc).__name__}: {exc}")
            missing.append(name)
            return
        merged.extend(observations)
        used.append(name)
        log(f"publish-cost-ledger: {name}: {len(observations)} observations")

    for name, collect in collectors.items():
        try:
            observations = collect(period, now)
        except SourceMissing as exc:
            log(f"publish-cost-ledger: {name} missing: {exc}")
            missing.append(name)
            continue
        except Exception as exc:  # noqa: BLE001 - one failed collector never blocks the rest
            log(f"publish-cost-ledger: {name} failed: {type(exc).__name__}: {exc}")
            missing.append(name)
            continue
        take(name, list(observations))

    for path in observation_files:
        name = f"observations:{Path(path).name}"
        try:
            observations, other = _read_observations(path, period)
        except Exception as exc:  # noqa: BLE001
            log(f"publish-cost-ledger: {name} failed: {type(exc).__name__}: {exc}")
            missing.append(name)
            continue
        if other:
            log(f"publish-cost-ledger: {name}: ignored {other} observations for other periods")
        take(name, observations)
    # MEASURED ties select the first observation. Keep declared internal usage
    # behind real collectors, including usage supplied by --observations files.
    merged.sort(key=lambda obs: obs.get("source") == internal.SOURCE)
    return merged, used, missing


def main(argv: Optional[List[str]] = None, *, collectors: Optional[Mapping[str, Collector]] = None,
         environ: Optional[Mapping[str, str]] = None, stdout=None, now: Optional[datetime] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--period", help="calendar month YYYY-MM (default: current UTC month)")
    parser.add_argument("--dry-run", action="store_true", help="print the would-be ledger, write nothing")
    parser.add_argument("--observations", action="append", default=[], metavar="FILE",
                        help="JSON Lines of usage or cost observations to merge (repeatable)")
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ
    stdout = sys.stdout if stdout is None else stdout
    now = datetime.now(timezone.utc) if now is None else now
    if args.period is None:
        args.period = now.astimezone(timezone.utc).strftime("%Y-%m")

    def log(line: str) -> None:
        print(line, file=sys.stderr)

    if not PERIOD_RE.fullmatch(args.period):
        log(f"publish-cost-ledger: --period must be YYYY-MM, got {args.period!r}")
        return 2
    store = None
    if not args.dry_run:
        store = CostLedgerStore(environ.get(ENV_DIR, ""))
        if not store.enabled:
            log(f"publish-cost-ledger: {ENV_DIR} is not set; use --dry-run to print instead")
            return 2

    selected = dict(DEFAULT_COLLECTORS if collectors is None else collectors)
    if environ.get("LEAF_COST_DISABLE_AWS", "") == "1" and "aws-cost-explorer" in selected:
        def disabled_aws(period: str, now: datetime) -> List[dict]:
            raise SourceMissing("disabled by LEAF_COST_DISABLE_AWS=1")
        selected["aws-cost-explorer"] = disabled_aws
    observations, used, missing = _gather(
        args.period, now, selected, args.observations, log)
    if not observations:
        log(f"publish-cost-ledger: nothing to publish for {args.period}; missing: {missing}")
        return 1

    try:
        pairs = publisher.build_period(args.period, observations)
    except Exception as exc:  # noqa: BLE001 - merged sources can conflict
        log(f"publish-cost-ledger: publish failed: {type(exc).__name__}: {exc}")
        return 1

    if args.dry_run:
        stdout.write(json.dumps({
            "period": args.period,
            "publication_id": None,
            "sources": sorted(used),
            "missing_sources": sorted(missing),
            "resources": [{"resource_period": rp.to_dict(), "shares": [s.to_dict() for s in shares]}
                          for rp, shares in pairs],
        }, sort_keys=True) + "\n")
        stdout.flush()
        return 0

    reason = (f"publish-cost-ledger {args.period} at {now.astimezone(timezone.utc).isoformat(timespec='seconds')}; "
              f"sources {', '.join(sorted(used)) or 'none'}; missing {', '.join(sorted(missing)) or 'none'}")
    try:
        publication_id = publisher.publish_period(store, args.period, observations, reason,
                                                  sources=used, missing_sources=missing)
    except Exception as exc:  # noqa: BLE001 - one named failure line
        log(f"publish-cost-ledger: publish failed: {type(exc).__name__}: {exc}")
        return 1
    stdout.write(json.dumps({
        "period": args.period,
        "publication_id": publication_id,
        "resources": len(pairs),
        "missing_sources": sorted(missing),
    }, sort_keys=True) + "\n")
    stdout.flush()
    log(f"publish-cost-ledger: published {publication_id}; missing sources: {sorted(missing) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
