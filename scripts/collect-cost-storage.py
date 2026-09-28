#!/usr/bin/env python3
"""Daily per-tenant storage snapshot for cost transparency (TCM-05).

Transparency of Leaf's real cost only, never billing. One run measures bytes
per tenant under the roots the server reads (LEAF_UPLOADS_DIR, LEAF_STORE_DIR,
LEAF_TENANT_GIT_DIR, LEAF_MARATHON_RUNS_DIR; an unset one is reported as
unset) and prints one snapshot JSON line. When LEAF_COST_STORAGE_SNAPSHOTS is
set the same line is appended to that JSONL file with one O_APPEND write;
unset means stdout only.

  python scripts/collect-cost-storage.py                    # snapshot now
  python scripts/collect-cost-storage.py --observe 2026-09  # the period's gb-month observation

It never writes the cost ledger. Exit 0 ok, 2 usage or input error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from cost_meter import storage  # noqa: E402


def _append_line(path: str, line: str) -> None:
    """One write of the whole line on an O_APPEND descriptor, then fsync."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = (line + "\n").encode("utf-8")
    fd = os.open(str(target), os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o640)
    try:
        written = os.write(fd, data)
        if written != len(data):
            raise OSError(f"short append to {target}: {written} of {len(data)} bytes")
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_lines(path: str) -> List[str]:
    """Snapshot lines, bounded: at most MAX_SNAPSHOT_LINES + 1 lines, each capped."""
    lines: List[str] = []
    with open(path, "rb") as fh:
        while len(lines) <= storage.MAX_SNAPSHOT_LINES:
            raw = fh.readline(storage.MAX_LINE_BYTES + 1)
            if not raw:
                break
            if len(raw) > storage.MAX_LINE_BYTES and not raw.endswith(b"\n"):
                while True:  # drop the rest of an oversized line in bounded chunks
                    rest = fh.readline(storage.MAX_LINE_BYTES)
                    if not rest or rest.endswith(b"\n"):
                        break
                lines.append("")  # counted as malformed by the observation
                continue
            text = raw.decode("utf-8", "replace").strip()
            if text:
                lines.append(text)
    return lines


def main(argv: Optional[List[str]] = None, *, now: Optional[datetime] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--observe", metavar="YYYY-MM",
                        help="print the period's storage usage observation from the snapshots file")
    parser.add_argument("--max-entries", type=int, default=storage.MAX_ENTRIES_PER_ROOT,
                        help="entries scanned per root before the scan stops (default %(default)s)")
    args = parser.parse_args(argv)
    now = now or datetime.now(timezone.utc)
    snapshots_path = os.environ.get(storage.SNAPSHOTS_ENV, "").strip()

    if args.observe:
        if not snapshots_path:
            print(f"{storage.SNAPSHOTS_ENV} is not set", file=sys.stderr)
            return 2
        try:
            lines = _read_lines(snapshots_path) if os.path.exists(snapshots_path) else []
            observation = storage.storage_usage_observation(args.observe, lines, now=now)
        except (OSError, ValueError) as exc:
            print(f"observe failed: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(observation, sort_keys=True))
        return 0

    try:
        snap = storage.snapshot(storage.roots_from_env(), now, max_entries=args.max_entries)
    except ValueError as exc:
        print(f"snapshot failed: {exc}", file=sys.stderr)
        return 2
    line = json.dumps(snap, sort_keys=True, separators=(",", ":"))
    if snapshots_path:
        _append_line(snapshots_path, line)
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
