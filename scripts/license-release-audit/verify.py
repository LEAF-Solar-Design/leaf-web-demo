#!/usr/bin/env python3
"""Verify the license release audit's report shape (R10 lane 10, LA1).

Runs audit.py and asserts the report is complete, not that it is clean: the
unscanned window is non-empty and bounded by real commits on the current
branch, every commit in it carries a disposition, and the P-062 block is fully
populated. A "finding" or a missing NOTICE is a valid result; this prints the
counts and ALL PASS when the report itself is sound.

Usage: python scripts/license-release-audit/verify.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
AUDIT_TIMEOUT_S = 900
GIT_TIMEOUT_S = 60
DISPOSITIONS = frozenset({"clean", "finding", "unresolved"})
HEX40_RE = re.compile(r"[0-9a-f]{40}")
# Bounds the review doc names for the window (docs/CAD-ENGINE-LICENSE-REVIEW.md,
# "Unscanned-window release audit"); compared for information, never asserted.
DOC_WINDOW_START = "80ca662b"
DOC_WINDOW_MOVE = "29051a82"


def _git_ok(args: list[str]) -> bool:
    try:
        proc = subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True,
                              timeout=GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _is_branch_commit(sha) -> bool:
    return (isinstance(sha, str) and HEX40_RE.fullmatch(sha) is not None
            and _git_ok(["cat-file", "-e", f"{sha}^{{commit}}"])
            and _git_ok(["merge-base", "--is-ancestor", sha, "HEAD"]))


def _nonempty_str(value) -> bool:
    return isinstance(value, str) and value.strip() != ""


def main() -> int:
    try:
        proc = subprocess.run([sys.executable, str(HERE / "audit.py")], cwd=str(REPO_ROOT),
                              capture_output=True, timeout=AUDIT_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        print(f"FAIL: audit.py did not finish within {AUDIT_TIMEOUT_S}s")
        return 1
    if proc.returncode != 0:
        print(f"FAIL: audit.py exit {proc.returncode}: "
              f"{proc.stderr.decode('utf-8', 'replace').strip()[:500]}")
        return 1
    try:
        report = json.loads(proc.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"FAIL: audit.py output is not JSON: {exc}")
        return 1

    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        if not ok:
            failures.append(what)

    window = report.get("window") or {}
    commits = report.get("commits") or []
    first, last = window.get("first_commit"), window.get("last_commit")
    dates = window.get("dates") or {}
    check(bool(commits), "window is non-empty (at least one commit)")
    check(_is_branch_commit(first), f"window.first_commit {first!r} is a real commit on HEAD")
    check(_is_branch_commit(last), f"window.last_commit {last!r} is a real commit on HEAD")
    check(_nonempty_str(dates.get("first")) and _nonempty_str(dates.get("last")),
          "window.dates.first and window.dates.last are populated")
    try:
        check(datetime.fromisoformat(dates["first"]) <= datetime.fromisoformat(dates["last"]),
              "window.dates.first is not after window.dates.last")
    except (KeyError, TypeError, ValueError):
        check(False, "window dates parse as ISO 8601")
    if commits:
        check(commits[0].get("sha") == first, "commits[0] is window.first_commit")
        check(commits[-1].get("sha") == last, "commits[-1] is window.last_commit")

    counts = {d: 0 for d in sorted(DISPOSITIONS)}
    for i, row in enumerate(commits):
        sha = row.get("sha")
        disposition = row.get("disposition")
        check(isinstance(sha, str) and HEX40_RE.fullmatch(sha) is not None,
              f"commits[{i}].sha is a 40-hex sha")
        check(disposition in DISPOSITIONS,
              f"commits[{i}] ({str(sha)[:12]}) disposition {disposition!r} is one of {sorted(DISPOSITIONS)}")
        check(_nonempty_str(row.get("date")), f"commits[{i}].date is populated")
        check(bool(row.get("paths_outside_scan_roots")),
              f"commits[{i}].paths_outside_scan_roots is non-empty")
        verdict = row.get("fence_verdict_with_root_added")
        check(isinstance(verdict, dict) and _nonempty_str(verdict.get("verdict")),
              f"commits[{i}].fence_verdict_with_root_added carries a verdict")
        check(_nonempty_str(row.get("note")), f"commits[{i}].note is populated")
        if disposition in counts:
            counts[disposition] += 1

    p062 = report.get("p062") or {}
    check(isinstance(p062.get("cargo_pin_is_bare_rev"), bool), "p062.cargo_pin_is_bare_rev is a bool")
    check(_nonempty_str(p062.get("cargo_pin_detail")), "p062.cargo_pin_detail is populated")
    check(isinstance(p062.get("notice_present"), bool), "p062.notice_present is a bool")
    check(_nonempty_str(p062.get("notice_location_or_missing")),
          "p062.notice_location_or_missing is populated")

    print(f"head: {report.get('head')}")
    print(f"window: {str(first)[:12]} ({dates.get('first')}) .. {str(last)[:12]} ({dates.get('last')}), "
          f"{len(commits)} commit(s), next newer {str(window.get('next_newer_commit_in_log_order'))[:12]}")
    print("dispositions: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    print(f"doc cross-check (informational): first starts {DOC_WINDOW_START}: "
          f"{str(first).startswith(DOC_WINDOW_START)}; next newer starts {DOC_WINDOW_MOVE}: "
          f"{str(window.get('next_newer_commit_in_log_order')).startswith(DOC_WINDOW_MOVE)}")
    print(f"p062: cargo_pin_is_bare_rev={p062.get('cargo_pin_is_bare_rev')} "
          f"notice_present={p062.get('notice_present')}")
    print(f"p062 notice: {p062.get('notice_location_or_missing')}")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        print(f"{len(failures)} check(s) failed")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
