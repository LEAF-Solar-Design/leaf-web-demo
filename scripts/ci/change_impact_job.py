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
import urllib.error
import urllib.request


SHA40 = re.compile(r"[0-9a-fA-F]{40}")
QUEUE_REF = re.compile(r"refs/heads/gh-readonly-queue/(.+)/pr-(\d+)(?:-(.*))?")
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


def merge_base(repo, ref, head):
    """The fork point of the change, never the base ref's tip: commits that landed on
    the base branch after the branch point are not this change's work."""
    try:
        result = subprocess.run(
            ["git", "merge-base", ref, head], cwd=repo,
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = result.stdout.strip().lower()
    return sha if result.returncode == 0 and SHA40.fullmatch(sha) else None


def resolve_base(repo, base_ref, head_ref, head):
    if base_ref:
        ref = base_ref.removeprefix("refs/heads/")
        try:
            fetched = subprocess.run(
                ["git", "fetch", "origin", "--", ref], cwd=repo,
                capture_output=True, text=True, timeout=60,
            )
            if fetched.returncode == 0 and git_sha(repo, "origin/" + ref):
                base = merge_base(repo, "origin/" + ref, head)
                if base:
                    return base, "pr-base-ref"
        except (OSError, subprocess.SubprocessError):
            pass
        base = merge_base(repo, base_ref, head)
        return (base if base else git_sha(repo, base_ref)), "pr-base-ref"
    match = QUEUE_REF.fullmatch(head_ref)
    if match:
        target, number, embedded = match.groups()
        if embedded and SHA40.fullmatch(embedded):
            return embedded.lower(), "merge-group-ref"
        try:
            return queue_base(target, head), "merge-queue-entry"
        except Exception:
            # Network errors may carry request details. Do not print them.
            return None, "merge-queue-entry"
    # A plain push (a merge landing on the default branch, or a manual build) carries no
    # webhook base ref; its change is what the head added over its first parent.
    parent = git_sha(repo, head + "^")
    if parent and parent != head:
        return parent, "push-first-parent"
    return None, None


def render_comment(receipt, context):
    def cell(value):
        return " ".join(str(value).splitlines()).replace("&", "&amp;").replace(
            "<", "&lt;").replace(">", "&gt;").replace("|", "&#124;")

    summary = receipt.get("summary", {})
    counts = ", ".join(f"{key} {int(summary.get(key, 0))}" for key in (
        "required", "unresolved", "changed", "unchanged-compatible", "deferred",
        "not-applicable",
    ))
    verdict = "COMPLETE" if receipt.get("verdict") == "COMPLETE" else "INCOMPLETE"
    header = "\n".join([
        "<!-- change-impact -->",
        "### Change impact (advisory)",
        f"base {cell(context['base'][:12])} head {cell(context['head'][:12])} "
        f"rule {cell(context['base_rule'][:128])} "
        f"checker {cell(receipt['checker_version'][:128])} "
        f"manifest {cell(receipt['manifest_digest'][:12])}",
        f"verdict {verdict}, {counts}",
        "",
        "| concern | subject | outcome | evidence |",
        "| --- | --- | --- | --- |",
    ])
    footer = ("Dispositions: impact.py resolve|defer|dismiss --record <record> "
              "--row <id>; kill switch C:/tmp/gates/CHANGE_IMPACT_OFF.")
    required = [row for row in receipt.get("rows", []) if row.get("required")]
    rows = ["| " + " | ".join(cell(row.get(key) if row.get(key) is not None else "-")
                              for key in ("concern", "subject", "outcome", "evidence"))
            + " |" for row in required[:40]]
    while True:
        omitted = len(required) - len(rows)
        tail = [f"... and {omitted} more"] if omitted else []
        body = "\n".join([header, *rows, *tail, footer])
        if len(body) <= 60000:
            return body
        if not rows:
            raise ValueError("comment header exceeds limit")
        rows.pop()


def sticky_comment(pr_number, body):
    token = os.environ.get("GH_TOKEN", "")
    if not token or any(c in token for c in "\r\n"):
        print(f"change-impact: comment skipped pr={pr_number}")
        return "skipped"
    step = "list"
    try:
        root = "https://api.github.com/repos/LEAF-Solar-Design/leaf-web-demo"
        comments = f"{root}/issues/{pr_number}/comments"
        headers = {"Authorization": "Bearer " + token,
                   "Accept": "application/vnd.github+json",
                   "Content-Type": "application/json",
                   "User-Agent": "leaf-change-impact-ci",
                   "X-GitHub-Api-Version": "2022-11-28"}
        target = None
        for page in range(1, 4):
            url = comments + "?per_page=100" + (f"&page={page}" if page > 1 else "")
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=60) as response:
                entries = json.load(response)
            target = next((entry for entry in entries
                           if (entry.get("body") or "").startswith("<!-- change-impact -->")), None)
            if target is not None or len(entries) < 100:
                break
        step = "update" if target is not None else "create"
        url = f"{root}/issues/comments/{int(target['id'])}" if target is not None else comments
        request = urllib.request.Request(
            url, data=json.dumps({"body": body}).encode(), headers=headers,
            method="PATCH" if target is not None else "POST",
        )
        with urllib.request.urlopen(request, timeout=60):
            pass
    except Exception as exc:
        code = exc.code if isinstance(exc, urllib.error.HTTPError) else "unknown"
        code = code if isinstance(code, int) else "unknown"
        print(f"change-impact: comment skipped ({step} http={code})")
        return "skipped"
    result = "updated" if target is not None else "created"
    print(f"change-impact: comment {result} pr={pr_number}")
    return result


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
            match = QUEUE_REF.fullmatch(args.head_ref)
            receipt_path = receipt_dir / "receipt.json"
            if (args.event.startswith("PUSH") and match and receipt_path.is_file()
                    and os.environ.get("CHANGE_IMPACT_NO_COMMENT") != "1"):
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                sticky_comment(match.group(2), render_comment(receipt, context))
            else:
                print(f"change-impact: comment not applicable (event={args.event})")
        except Exception:
            # Receipt/render failures are advisory too, without exception diagnostics.
            print("change-impact: comment skipped (list http=unknown)")
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
