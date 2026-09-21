#!/usr/bin/env python3
"""Advisory change-impact assessment for leaf-web-demo native CI (S1)."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request


SHA40 = re.compile(r"[0-9a-fA-F]{40}")
QUEUE_REF = re.compile(r"refs/heads/gh-readonly-queue/(.+)/pr-\d+(?:-(.*))?")
QUEUE_QUERY = """query($cursor: String, $branch: String!) {
  repository(owner: "LEAF-Solar-Design", name: "leaf-web-demo") {
    mergeQueue(branch: $branch) { entries(first: 100, after: $cursor) {
      nodes { position headCommit { oid } baseCommit { oid }
              pullRequest { number headRefOid } }
      pageInfo { hasNextPage endCursor }
    } }
  }
}"""


def git_sha(repo, ref):
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--end-of-options", ref + "^{commit}"],
        cwd=repo, capture_output=True, text=True, timeout=60,
    )
    value = result.stdout.strip()
    return value.lower() if result.returncode == 0 and SHA40.fullmatch(value) else None


def queue_base(target, head):
    token = os.environ.get("GH_TOKEN", "")
    if not token or any(c in token for c in "\r\n"):
        return None
    cursor, seen = None, set()
    while True:
        # Keep the token in-process, never in subprocess argv or diagnostics.
        request = urllib.request.Request(
            "https://api.github.com/graphql",
            data=json.dumps({"query": QUEUE_QUERY,
                             "variables": {"cursor": cursor, "branch": target}}).encode(),
            headers={"Authorization": "Bearer " + token,
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json",
                     "User-Agent": "leaf-change-impact-ci",
                     "X-GitHub-Api-Version": "2022-11-28"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)
        if payload.get("errors"):
            return None
        queue = payload["data"]["repository"]["mergeQueue"]
        if queue is None:
            return None
        connection = queue["entries"]
        for entry in connection["nodes"]:
            if entry["headCommit"]["oid"].lower() == head:
                base = entry["baseCommit"]["oid"]
                return base.lower() if SHA40.fullmatch(base) else None
        page = connection["pageInfo"]
        if not page["hasNextPage"]:
            return None
        cursor = page["endCursor"]
        if not cursor or cursor in seen:
            return None
        seen.add(cursor)


def resolve_base(repo, base_ref, head_ref, head):
    if base_ref:
        ref = base_ref.removeprefix("refs/heads/")
        try:
            fetched = subprocess.run(
                ["git", "fetch", "origin", "--", ref], cwd=repo,
                capture_output=True, text=True, timeout=60,
            )
            if fetched.returncode == 0:
                base = git_sha(repo, "origin/" + ref)
                if base:
                    return base, "pr-base-ref"
        except (OSError, subprocess.SubprocessError):
            pass
        return git_sha(repo, base_ref), "pr-base-ref"
    match = QUEUE_REF.fullmatch(head_ref)
    if match:
        target, embedded = match.groups()
        if embedded and SHA40.fullmatch(embedded):
            return embedded.lower(), "merge-group-ref"
        try:
            return queue_base(target, head), "merge-queue-entry"
        except Exception:
            # Network errors may carry request details. Do not print them.
            return None, "merge-queue-entry"
    return None, None


def print_output(value):
    if value:
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        print(value, end="" if value.endswith("\n") else "\n")


VENDORED_CHECKER = Path("scripts") / "ci" / "vendor" / "impact" / "impact.py"


def locate_checker(repo):
    """Lookup order: CHANGE_IMPACT_CHECKER, the copy vendored in this repo (pinned by
    scripts/ci/vendor/impact/VENDORED.json, so CI needs no network and no IAM change),
    then a host install under ~/.claude. Returns (path, source) or (None, None)."""
    override = os.environ.get("CHANGE_IMPACT_CHECKER")
    if override:
        path = Path(override).expanduser()
        return (path, "env") if path.is_file() else (None, None)
    vendored = Path(repo) / VENDORED_CHECKER
    if vendored.is_file():
        return vendored, "vendored"
    home = Path.home() / ".claude" / "scripts" / "impact" / "impact.py"
    if home.is_file():
        return home, "home"
    return None, None


def run(args):
    started = time.monotonic()
    repo = Path(args.repo).resolve()
    checker, checker_source = locate_checker(repo)
    if checker is None:
        print("change-impact: checker not installed on this runner, skipped")
        return 0
    print(f"change-impact: checker={checker_source} {checker}", flush=True)
    head = git_sha(repo, args.head)
    if not head:
        print("change-impact: SKIP head could not be resolved to 40 hex")
        return 0
    base, rule = resolve_base(repo, args.base_ref, args.head_ref, head)
    if not base:
        print(f"change-impact: SKIP no base for event={args.event} head={head}")
        return 0
    print(f"change-impact: base_rule={rule} base={base} head={head}", flush=True)
    receipt_dir = Path(args.receipt_dir).resolve()
    receipt_dir.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(checker.resolve()), "check", "--workdir", str(repo),
               "--change-id", "ci-" + head[:12], "--base", base, "--head", head,
               "--record", str(receipt_dir / "record.yaml"),
               "--receipt", str(receipt_dir / "receipt.json"), "--json"]
    incomplete = False
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=300)
        print_output(result.stdout)
        print_output(result.stderr)
        incomplete = bool(re.search(r"^IMPACT:\s*INCOMPLETE\b",
                                    result.stdout + "\n" + result.stderr, re.MULTILINE))
        if result.returncode:
            print(f"change-impact: checker exit {result.returncode} (advisory)")
    except subprocess.TimeoutExpired as exc:
        print_output(exc.stdout)
        print_output(exc.stderr)
        print("change-impact: checker timed out (advisory)")
    finally:
        context = {"event": args.event, "base_rule": rule, "base": base, "head": head,
                   "gate_result_present": bool(args.gate_result and
                                               Path(args.gate_result).is_file()),
                   "elapsed_s": round(time.monotonic() - started, 3)}
        try:
            (receipt_dir / "ci.json").write_text(
                json.dumps(context, indent=2) + "\n", encoding="utf-8",
            )
        except OSError:
            print("change-impact: unable to write CI context (advisory)")
    return 1 if args.strict and incomplete else 0


def main(argv=None):
    if os.environ.get("CHANGE_IMPACT_DISABLE") == "1":
        print("change-impact: disabled")
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--base-ref", default="")
    parser.add_argument("--head-ref", default="")
    parser.add_argument("--event", default="manual")
    parser.add_argument("--gate-result")
    parser.add_argument("--receipt-dir", default="/tmp/impact")
    parser.add_argument("--strict", action="store_true")
    try:
        return run(parser.parse_args(argv))
    except SystemExit:
        return 0
    except Exception:
        # S1 is advisory even for missing tools, malformed state or I/O failures.
        # Exception strings can contain credentials or API bodies.
        print("change-impact: unable to complete assessment (advisory)")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
