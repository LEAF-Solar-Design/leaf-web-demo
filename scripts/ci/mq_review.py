#!/usr/bin/env python3
"""Produce leaf-web-demo's merge-queue review status from member reviews."""
import argparse
from datetime import datetime
import json
import os
import re
import subprocess

REPO = "LEAF-Solar-Design/leaf-web-demo"
CONTEXT = "mq-review"
REVIEW_CONTEXT = "kimi-critic-review"
SHA40 = re.compile(r"[0-9a-fA-F]{40}")


class NotQueued(Exception):
    """The requested group no longer belongs to the live queue."""


def members_for(entries, head_sha):
    members = []
    for entry in sorted(entries, key=lambda item: item["position"]):
        members.append(entry)
        if entry["headCommit"]["oid"] == head_sha:
            return members
    raise NotQueued(head_sha)


def verdict(members, statuses_by_head):
    for member in members:
        pr = member["pullRequest"]
        state = statuses_by_head.get(pr["headRefOid"], "absent")
        if state != "success":
            return False, f"PR #{pr['number']}: {state}"
    return True, "All queued members have successful kimi-critic-review"


def membership(entry):
    return (entry["pullRequest"]["number"], entry["pullRequest"]["headRefOid"],
            entry["headCommit"]["oid"], entry["position"])


def drift(before, after):
    for index in range(max(len(before), len(after))):
        old = membership(before[index]) if index < len(before) else None
        new = membership(after[index]) if index < len(after) else None
        if old != new:
            return f"Queue changed at member {index + 1}: {old} -> {new}"
    return None


def newest_review(statuses):
    reviews = [s for s in statuses if s["context"] == REVIEW_CONTEXT]
    if not reviews:
        return "absent"
    latest = max(reviews, key=lambda s: (
        datetime.fromisoformat(s["created_at"].replace("Z", "+00:00")),
        s.get("id", 0)))
    return latest["state"]


def github(path, payload=None, expected_status=None):
    token = os.environ.get("GH_TOKEN", "")
    if not token or any(c in token for c in '\r\n'):
        raise RuntimeError("GH_TOKEN is missing or invalid")
    # Pass the authorization header on stdin, never in argv or error output.
    escaped = token.replace('\\', '\\\\').replace('"', '\\"')
    config = f'header = "Authorization: Bearer {escaped}"\n'
    command = ["curl", "--disable", "--silent", "--show-error", "--fail",
               "--max-time", "60", "--config", "-",
               "--header", "Accept: application/vnd.github+json",
               "--header", "X-GitHub-Api-Version: 2022-11-28",
               "https://api.github.com/" + path]
    if payload is not None:
        command += ["--header", "Content-Type: application/json",
                    "--data-raw", json.dumps(payload)]
    if expected_status is not None:
        command += ["--write-out", "\n%{http_code}"]
    result = subprocess.run(command, input=config, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError("GitHub request failed")
    body = result.stdout
    if expected_status is not None:
        body, separator, status = body.rpartition("\n")
        if not separator or status != str(expected_status):
            raise RuntimeError("GitHub returned unexpected HTTP status")
    try:
        return json.loads(body)
    except ValueError:
        raise RuntimeError("GitHub returned invalid JSON") from None


def read_queue():
    query = """query($cursor: String) {
      repository(owner: "LEAF-Solar-Design", name: "leaf-web-demo") {
        mergeQueue(branch: "main") { entries(first: 100, after: $cursor) {
          nodes { position headCommit { oid } baseCommit { oid }
                  pullRequest { number headRefOid } }
          pageInfo { hasNextPage endCursor }
        } }
      }
    }"""
    entries, cursor = [], None
    seen = set()
    while True:
        response = github("graphql", {"query": query, "variables": {"cursor": cursor}})
        if response.get("errors"):
            raise RuntimeError("GitHub queue query failed")
        queue = response["data"]["repository"]["mergeQueue"]
        if queue is None:
            return []
        connection = queue["entries"]
        entries.extend(connection["nodes"])
        page = connection["pageInfo"]
        if not page["hasNextPage"]:
            return entries
        cursor = page["endCursor"]
        if not cursor or cursor in seen:
            raise RuntimeError("GitHub queue pagination did not advance")
        seen.add(cursor)


def read_review(head):
    if not SHA40.fullmatch(head):
        raise ValueError("headRefOid must be a full 40-hex commit sha")
    statuses, page = [], 1
    while True:
        batch = github(f"repos/{REPO}/commits/{head}/statuses?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise RuntimeError("GitHub statuses response is invalid")
        statuses.extend(batch)
        if len(batch) < 100:
            return newest_review(statuses)
        page += 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--deferred", action="store_true")
    args = parser.parse_args(argv)
    if not SHA40.fullmatch(args.head_sha):
        parser.error("--head-sha must be a full commit SHA")
    try:
        if args.deferred:
            payload = {"context": CONTEXT, "state": "success",
                       "description": "deferred: the real mq-review check runs on the merge group"}
            if os.environ.get("CODEBUILD_BUILD_URL"):
                payload["target_url"] = os.environ["CODEBUILD_BUILD_URL"]
            github(f"repos/{REPO}/statuses/{args.head_sha}", payload, expected_status=201)
            print(f"{CONTEXT}: success: {payload['description']}")
            return 0
        before = members_for(read_queue(), args.head_sha)
        states = {m["pullRequest"]["headRefOid"]: read_review(m["pullRequest"]["headRefOid"])
                  for m in before}
        ok, reason = verdict(before, states)
        after = members_for(read_queue(), args.head_sha)
        changed = drift(before, after)
        if changed:
            ok, reason = False, changed
        payload = {"context": CONTEXT, "state": "success" if ok else "failure",
                   "description": reason[:139]}
        if os.environ.get("CODEBUILD_BUILD_URL"):
            payload["target_url"] = os.environ["CODEBUILD_BUILD_URL"]
        github(f"repos/{REPO}/statuses/{args.head_sha}", payload)
        print(f"{CONTEXT}: {payload['state']}: {payload['description']}")
        return 0 if ok else 1
    except NotQueued:
        print("Not queued: group was destroyed or merged; no status posted")
        return 2
    except (RuntimeError, KeyError, TypeError, ValueError, OSError):
        # API bodies and subprocess errors can carry sensitive material.
        print("mq-review failed: unable to read or publish GitHub state")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
