#!/usr/bin/env python3
"""Validate the expiring Trivy ignore list used by the platform image gate."""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path


CVE = re.compile(r"^CVE-\d{4}-\d{4,}$")
EXPIRY = re.compile(r"^# expires: (\d{4}-\d{2}-\d{2})$")
JUSTIFICATION = re.compile(r"^# justification: (\S(?:.*\S)?)$")
FORBIDDEN = re.compile(r"starlette|fastapi", re.IGNORECASE)


def validate(path: Path, today: dt.date, latest: dt.date) -> list[str]:
    errors: list[str] = []
    entries: set[str] = set()
    pending_expiry: dt.date | None = None
    pending_justification = False

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [f"cannot read {path}: {exc}"]

    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        expiry = EXPIRY.match(line)
        if expiry:
            try:
                pending_expiry = dt.date.fromisoformat(expiry.group(1))
            except ValueError:
                errors.append(f"line {number}: invalid expiry date")
            continue
        justification = JUSTIFICATION.match(line)
        if justification:
            pending_justification = True
            if FORBIDDEN.search(justification.group(1)):
                errors.append(f"line {number}: forbidden Starlette/FastAPI justification")
            continue
        if line.startswith("#"):
            continue
        if not CVE.fullmatch(line):
            errors.append(f"line {number}: expected a CVE id, got {line!r}")
            pending_expiry = None
            pending_justification = False
            continue
        if line in entries:
            errors.append(f"line {number}: duplicate {line}")
        entries.add(line)
        if pending_expiry is None:
            errors.append(f"line {number}: {line} has no machine-checked expiry")
        elif pending_expiry < today:
            errors.append(f"line {number}: {line} expired on {pending_expiry}")
        elif pending_expiry > latest:
            errors.append(f"line {number}: {line} expires after {latest}")
        if not pending_justification:
            errors.append(f"line {number}: {line} has no one-line justification")
        pending_expiry = None
        pending_justification = False

    if pending_expiry is not None or pending_justification:
        errors.append("metadata without a following CVE entry")
    if not entries:
        errors.append("allowlist is empty")
    if errors:
        return errors
    print(f"CVE allowlist valid: {len(entries)} entries, expiry <= {latest.isoformat()}")
    return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    parser.add_argument("--latest", type=dt.date.fromisoformat, default=dt.date(2026, 11, 30))
    args = parser.parse_args()
    errors = validate(args.path, args.today, args.latest)
    for error in errors:
        print(f"::error::{error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
