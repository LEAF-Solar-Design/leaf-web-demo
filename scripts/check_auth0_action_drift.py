"""Scheduled read-only drift check for the Leaf tenant-claim Auth0 Actions.

A thin wrapper over ``deploy_auth0_actions.py --check``: that script owns the
comparison. This one runs it in check mode only, classifies the outcome as
match, drift or unavailable, writes one receipt per run, and emits at most one
notice per unchanged drift (or unchanged unavailable error) using a small
dedupe state file. It never passes --deploy, so the underlying HTTPS transport
is built with writes disabled and no Action can change.

Credentials stay external: the check reads LEAF_AUTH0_ACTIONS_CLIENT_ID and
LEAF_AUTH0_ACTIONS_CLIENT_SECRET from the environment, and no credential value
reaches argv, a receipt, the state file or a log line.

Exit codes: 0 match, 1 drift, 2 unavailable, 3 receipt or state write failed.
"""

import argparse
import contextlib
from datetime import datetime, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sys

_SYNC_PATH = Path(__file__).with_name("deploy_auth0_actions.py")
_SPEC = importlib.util.spec_from_file_location("_leaf_auth0_actions_sync", _SYNC_PATH)
sync = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(sync)

RECEIPT_SCHEMA = "leaf.auth0-action-drift-check.v1"
NOTICE_SCHEMA = "leaf.auth0-action-drift-notice.v1"
STATE_SCHEMA = "leaf.auth0-action-drift-state.v1"
SYNC_SCHEMA = "leaf.auth0-actions-sync.v1"
NOTICE_PREFIX = "LEAF_AUTH0_DRIFT_NOTICE="
ERROR_PREFIX = "LEAF_AUTH0_DRIFT_ERROR="
EXIT_CODES = {"match": 0, "drift": 1, "unavailable": 2}
EXIT_WRITE_FAILED = 3
MAX_STATE_BYTES = 64 * 1024
HEX64 = re.compile(r"[0-9a-f]{64}")
ERROR_LINE = re.compile(r"LEAF_AUTH0_ACTIONS_ERROR=([a-z_]{1,64})")
TARGET_STATES = ("match", "drift", "missing")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default echoes user-supplied values; keep stderr fixed.
        raise ValueError("arguments_invalid")


def build_parser():
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--domain", required=True, metavar="HOST")
    parser.add_argument("--receipt-dir", required=True, type=Path)
    parser.add_argument("--state-file", required=True, type=Path)
    return parser


def run_check(domain, *, transport=None):
    """Run the existing check once; return (state, report or None, error code or None).

    Never raises. Any output the check produces that is not a well-formed
    check report is unavailable, never match.
    """
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = sync.main(["--check", "--domain", domain], transport=transport)
    except Exception:
        return "unavailable", None, "check_crashed"
    if status == 2:
        lines = err.getvalue().splitlines()
        match = ERROR_LINE.fullmatch(lines[0]) if lines else None
        return "unavailable", None, match.group(1) if match else "check_error_unrecognized"
    report = _parse_report(out.getvalue(), status)
    if report is None:
        return "unavailable", None, "report_invalid"
    return report["result"], report, None


def _parse_report(text, status):
    """Shape-check the check's own report; the comparison itself is not redone."""
    lines = text.splitlines()
    if len(lines) != 1:
        return None
    try:
        report = json.loads(lines[0])
    except ValueError:
        return None
    if (not isinstance(report, dict) or report.get("schema") != SYNC_SCHEMA
            or report.get("mode") != "check"
            or report.get("result") not in ("match", "drift")
            or EXIT_CODES[report["result"]] != status):
        return None
    targets = report.get("targets")
    if not isinstance(targets, list) or len(targets) != len(sync.TARGETS):
        return None
    clean = []
    for target in targets:
        if not isinstance(target, dict) or set(target) != set(sync.REPORT_KEYS):
            return None
        deployed = target["deployed_sha256"]
        if (target["state"] not in TARGET_STATES
                or not isinstance(target["repo_sha256"], str)
                or HEX64.fullmatch(target["repo_sha256"]) is None
                or not (deployed is None or (isinstance(deployed, str)
                                             and HEX64.fullmatch(deployed)))
                or not any(target["trigger"] == known["trigger"]
                           and target["action_name"] == known["action_name"]
                           for known in sync.TARGETS)):
            return None
        clean.append({key: target[key] for key in sync.REPORT_KEYS})
    digest = report.get("plan_digest")
    if report["result"] == "match":
        if digest is not None or any(target["state"] != "match" for target in clean):
            return None
    elif not isinstance(digest, str) or HEX64.fullmatch(digest) is None:
        return None
    return {"result": report["result"], "plan_digest": digest, "targets": clean}


def load_state(path, domain):
    """Read dedupe state; anything unreadable or foreign reads as empty (re-notify, never miss)."""
    empty = {"drift_digest": None, "unavailable_code": None}
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            return empty
        state = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(state, dict) or state.get("schema") != STATE_SCHEMA or state.get("domain") != domain:
        return empty
    digest, code = state.get("drift_digest"), state.get("unavailable_code")
    if digest is not None and (not isinstance(digest, str) or HEX64.fullmatch(digest) is None):
        return empty
    if code is not None and (not isinstance(code, str) or re.fullmatch(r"[a-z_]{1,64}", code) is None):
        return empty
    return {"drift_digest": digest, "unavailable_code": code}


def decide(state, previous):
    """Return (dedupe_key or None, next_state) for this run's outcome."""
    kind, value = state
    if kind == "match":
        return None, {"drift_digest": None, "unavailable_code": None}
    if kind == "drift":
        key = None if value == previous["drift_digest"] else "drift:" + value
        return key, {"drift_digest": value, "unavailable_code": None}
    # An unavailable read says nothing about drift, so the drift key survives it.
    key = None if value == previous["unavailable_code"] else "unavailable:" + value
    return key, {"drift_digest": previous["drift_digest"], "unavailable_code": value}


def _write_new(path, text):
    # Exclusive create: a receipt is never overwritten.
    with open(path, "x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _write_replace(path, text):
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(tmp, path)


def main(argv=None, *, transport=None, now=None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except ValueError:
        print(ERROR_PREFIX + "arguments_invalid", file=sys.stderr)
        return EXIT_CODES["unavailable"]
    except SystemExit as exc:
        return int(exc.code or 0)
    checked = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    # Record the domain only once it is a valid tenant host, so a misconfigured
    # value never lands in a receipt.
    domain = args.domain if sync.DOMAIN_RE.fullmatch(args.domain) else None
    state, report, error_code = run_check(args.domain, transport=transport)
    previous = load_state(args.state_file, domain)
    dedupe_key, next_state = decide(
        (state, report["plan_digest"] if state == "drift" else error_code), previous)
    checked_at = checked.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    notice = None
    if dedupe_key is not None:
        notice = {"schema": NOTICE_SCHEMA, "kind": state, "domain": domain,
                  "checked_at": checked_at, "dedupe_key": dedupe_key}
        if state == "drift":
            notice["plan_digest"] = report["plan_digest"]
            notice["drifted"] = [{"trigger": target["trigger"], "action_name": target["action_name"],
                                  "state": target["state"]}
                                 for target in report["targets"] if target["state"] != "match"]
        else:
            notice["error_code"] = error_code
    receipt = {"schema": RECEIPT_SCHEMA, "checked_at": checked_at, "domain": domain,
               "state": state, "plan_digest": report["plan_digest"] if report else None,
               "targets": report["targets"] if report else [], "error_code": error_code,
               "notice_emitted": notice is not None,
               "notice_suppressed": state != "match" and notice is None,
               "dedupe_key": dedupe_key, "action_writes": 0}
    try:
        args.receipt_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = args.receipt_dir / (
            "auth0-action-drift-" + checked.strftime("%Y%m%dT%H%M%S%fZ") + ".json")
        _write_new(receipt_path, json.dumps(receipt, sort_keys=True, indent=2) + "\n")
        # The receipt lands before the state, so a failed state write re-notifies
        # next run rather than silently swallowing a notice.
        args.state_file.parent.mkdir(parents=True, exist_ok=True)
        _write_replace(args.state_file, json.dumps(
            {"schema": STATE_SCHEMA, "domain": domain, "updated_at": checked_at, **next_state},
            sort_keys=True, indent=2) + "\n")
    except OSError:
        print(ERROR_PREFIX + "receipt_or_state_write_failed", file=sys.stderr)
        return EXIT_WRITE_FAILED
    if notice is not None:
        print(NOTICE_PREFIX + json.dumps(notice, sort_keys=True, separators=(",", ":")))
    print(json.dumps({"schema": RECEIPT_SCHEMA, "state": state, "receipt": receipt_path.name,
                      "notice_emitted": notice is not None}, sort_keys=True, separators=(",", ":")))
    return EXIT_CODES[state]


if __name__ == "__main__":
    raise SystemExit(main())
