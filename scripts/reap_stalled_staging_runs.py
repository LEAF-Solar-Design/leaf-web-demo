#!/usr/bin/env python3
"""Cancel staging runs stuck acquiring runners; retry cancelled main-head builds."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import urllib.error
import urllib.parse
import urllib.request

STALL_AFTER_S = 1200
MAX_ATTEMPTS = 3
MAX_WRITES = 4
MAX_JOB_PAGES = 3
BUILD_WORKFLOW = "build-platform-images.yml"
RELAY_WORKFLOW = "dispatch-staging-deploys.yml"
WORKFLOWS = (BUILD_WORKFLOW, RELAY_WORKFLOW)
ACTIVE_STATUSES = {"queued", "in_progress", "pending"}
RUN_STATUSES = ACTIVE_STATUSES | {"completed", "waiting", "requested"}
JOB_STATUSES = RUN_STATUSES


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp is not a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return parsed


def is_stalled(run, jobs, now, stall_after_s=STALL_AFTER_S):
    """Pure fail-closed decision: queue age alone is insufficient if work runs."""
    try:
        if not isinstance(run, dict) or run.get("status") not in RUN_STATUSES:
            raise ValueError("run shape")
        if not isinstance(jobs, list) or not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("jobs or clock shape")
        if stall_after_s < 0:
            raise ValueError("negative threshold")
        aged = False
        running = False
        for job in jobs:
            if not isinstance(job, dict) or job.get("status") not in JOB_STATUSES:
                raise ValueError("job shape")
            runner = job.get("runner_name")
            if "runner_name" not in job or (runner is not None and not isinstance(runner, str)):
                raise ValueError("runner_name shape")
            created = _timestamp(job.get("created_at"))
            running |= job["status"] == "in_progress"
            if job["status"] == "queued" and not runner:
                aged |= (now - created).total_seconds() >= stall_after_s
        if run["status"] not in {"queued", "in_progress"}:
            return False, "run is not active"
        if running:
            return False, "a job is in_progress"
        if aged:
            return True, f"queued without a runner for at least {stall_after_s}s; no job in_progress"
        return False, "no aged queued job without a runner"
    except (ValueError, TypeError, OverflowError) as exc:
        return False, f"unreadable {exc}"


def _eligible(workflow, run):
    return (run.get("event") == "push" and run.get("head_branch") == "main"
            if workflow == BUILD_WORKFLOW else run.get("event") == "workflow_run")


def _validate_run(run):
    if not isinstance(run, dict) or type(run.get("id")) is not int or run["id"] <= 0:
        raise ValueError("unreadable run id")
    if run.get("status") not in RUN_STATUSES:
        raise ValueError("unreadable run status")
    for key in ("event", "head_branch", "head_sha"):
        if not isinstance(run.get(key), str) or not run[key]:
            raise ValueError(f"unreadable run {key}")
    _timestamp(run.get("created_at"))
    if type(run.get("run_attempt")) is not int or run["run_attempt"] < 1:
        raise ValueError("unreadable run_attempt")


def union_runs(first, second):
    """Keep ids present in either bounded listing; the second observation wins."""
    merged = {}
    for listing in (first, second):
        if not isinstance(listing, list):
            raise ValueError("unreadable runs listing")
        for run in listing:
            _validate_run(run)
            merged[run["id"]] = run
    return sorted(merged.values(), key=lambda r: (_timestamp(r["created_at"]), r["id"]), reverse=True)


def plan_actions(runs_by_workflow, jobs_by_run, main_head_sha, now):
    """Compute cancellations and at most one retry, without any I/O."""
    actions = []
    try:
        if not isinstance(main_head_sha, str) or not main_head_sha:
            return []
        runs_by_workflow = {w: union_runs(runs_by_workflow.get(w, []), []) for w in WORKFLOWS}
        for workflow, runs in runs_by_workflow.items():
            for run in runs:
                if not _eligible(workflow, run):
                    continue
                stalled, reason = is_stalled(run, jobs_by_run.get(run["id"], []), now)
                if reason.startswith("unreadable"):
                    return []
                if stalled:
                    actions.append({"action": "cancel", "workflow": workflow,
                                    "run_id": run["id"], "reason": reason})
        builds = runs_by_workflow[BUILD_WORKFLOW]
        if not any(r["status"] in ACTIVE_STATUSES for r in builds):
            newest = next((r for r in builds if _eligible(BUILD_WORKFLOW, r)
                           and r["head_sha"] == main_head_sha), None)
            if (newest and newest["status"] == "completed" and newest.get("conclusion") == "cancelled"
                    and newest["run_attempt"] < MAX_ATTEMPTS):
                actions.append({"action": "rerun", "workflow": BUILD_WORKFLOW,
                                "run_id": newest["id"], "reason": "cancelled main-head push; nothing active"})
        return actions
    except (ValueError, TypeError, AttributeError):
        return []


class GitHubClient:
    """Small REST adapter; all reads finish before any mutation begins."""

    def __init__(self, repo, token):
        if not repo or not token:
            raise ValueError("GITHUB_REPOSITORY and GITHUB_TOKEN are required")
        self.base = f"https://api.github.com/repos/{repo}"
        self.token = token

    def request(self, path, method="GET"):
        request = urllib.request.Request(self.base + path, method=method,
                                         data=b"" if method == "POST" else None,
                                         headers={"Authorization": f"Bearer {self.token}",
                                                  "Accept": "application/vnd.github+json",
                                                  "X-GitHub-Api-Version": "2022-11-28"})
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read()
        return json.loads(body) if body else None

    def list_runs(self, workflow):
        query = {"branch": "main", "per_page": 20, "page": 1}
        if workflow == BUILD_WORKFLOW:
            query["event"] = "push"
        payload = self.request(f"/actions/workflows/{workflow}/runs?{urllib.parse.urlencode(query)}")
        rows = payload["workflow_runs"]
        if not isinstance(rows, list) or len(rows) > 20:
            raise ValueError("unreadable runs listing")
        return union_runs(rows, [])

    def list_jobs(self, run_id):
        jobs = []
        for page in range(1, MAX_JOB_PAGES + 1):
            payload = self.request(f"/actions/runs/{run_id}/jobs?per_page=100&page={page}&filter=latest")
            rows = payload["jobs"]
            total = payload["total_count"]
            if (not isinstance(rows, list) or len(rows) > 100 or type(total) is not int
                    or total < 0 or total > MAX_JOB_PAGES * 100):
                raise ValueError("unreadable jobs listing")
            jobs.extend(rows)
            if len(jobs) == total:
                return jobs
            if not rows or len(jobs) > total:
                break
        raise ValueError("unreadable incomplete jobs listing")

    def main_head(self):
        sha = self.request("/branches/main")["commit"]["sha"]
        if not isinstance(sha, str) or not sha:
            raise ValueError("unreadable main head")
        return sha

    def write(self, action):
        self.request(f"/actions/runs/{action['run_id']}/{action['action']}", method="POST")


def _annotation(level, message):
    escaped = str(message).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{level}::{escaped}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    summary = {"dry_run": args.dry_run, "actions": [], "writes": 0, "deferred": 0}
    try:
        client = GitHubClient(args.repo, os.environ.get("GITHUB_TOKEN"))
        runs = {w: union_runs(client.list_runs(w), client.list_runs(w)) for w in WORKFLOWS}
        head = client.main_head()
        now = datetime.now(timezone.utc)
        jobs = {}
        for workflow, rows in runs.items():
            for run in rows:
                if _eligible(workflow, run) and run["status"] in {"queued", "in_progress"}:
                    jobs[run["id"]] = client.list_jobs(run["id"])
                    _, reason = is_stalled(run, jobs[run["id"]], now)
                    if reason.startswith("unreadable"):
                        raise ValueError(reason)
        plan = plan_actions(runs, jobs, head, now)
        summary["actions"] = plan
        summary["deferred"] = max(0, len(plan) - MAX_WRITES)
    except Exception as exc:  # Read failure must never turn into a partial mutation.
        _annotation("warning", f"staging reaper read failed; no-op: {exc}")
        summary["read_error"] = str(exc)
        print(json.dumps(summary, sort_keys=True))
        return 0
    by_id = {r["id"]: r for rows in runs.values() for r in rows}
    for action in plan[:MAX_WRITES]:
        _annotation("notice", f"{action['action']} run {action['run_id']} {action['workflow']} "
                    f"head {by_id[action['run_id']]['head_sha']}: {action['reason']}"
                    + (" (dry-run)" if args.dry_run else ""))
        if args.dry_run:
            continue
        try:
            client.write(action)
            summary["writes"] += 1
        except Exception as exc:
            _annotation("error", f"staging reaper write refused: {exc}")
            summary["write_error"] = str(exc)
            print(json.dumps(summary, sort_keys=True))
            return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
