#!/usr/bin/env python3
"""One-shot local recovery of PR mq-review publication, dry-run by default.

Uses the installed DPAPI GitHub App helper, never GH_TOKEN or a stored PAT.
This recovers a deferred queue-admission status, NOT source tests or CodeBuild
success. The original failed build is retained. No watcher or cloud change.
"""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sys

REPO = "LEAF-Solar-Design/leaf-web-demo"
PROJECT = "leaf-mq-leaf-web-demo"
PREFIX = "arn:aws:codebuild:us-east-1:807034087062:build/"
LOADER = "ab949693fbae5c4a540637a717be85ac7bf5d847"
FAILURE = "mq-review failed: GitHub request failed: statuses POST rc=22 http=404"
DESCRIPTION = "deferred: the real mq-review check runs on the merge group"


def admit(build, build_id, head, messages):
    if not re.fullmatch(PROJECT + r":[0-9a-f-]{36}", build_id):
        raise ValueError("unexpected build identity")
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError("invalid exact source")
    expected = {"id": build_id, "arn": PREFIX + build_id,
                "projectName": PROJECT, "resolvedSourceVersion": head,
                "buildComplete": True, "buildStatus": "FAILED",
                "serviceRole": "arn:aws:iam::807034087062:role/leaf-mq-codebuild"}
    if any(build.get(k) != v for k, v in expected.items()):
        raise ValueError("native producer identity or outcome mismatch")
    if build.get("source", {}).get("location") != f"https://github.com/{REPO}.git":
        raise ValueError("source repository mismatch")
    match = re.fullmatch(r"pr/([1-9][0-9]*)", build.get("sourceVersion", ""))
    if not match or not build.get("initiator", "").startswith("GitHub-Hookshot/"):
        raise ValueError("not a native pull-request event")
    failed = [p["phaseType"] for p in build.get("phases", []) if p.get("phaseStatus") == "FAILED"]
    if failed != ["BUILD"]:
        raise ValueError("failure is not isolated to the publisher build phase")
    lines = [line.strip() for message in messages for line in message.splitlines()]
    loader = f"loader: running .codebuild/mq.sh and scripts/ci/mq_review.py from {LOADER} (ab949693)"
    if lines.count(FAILURE) != 1 or lines.count(loader) != 1:
        raise ValueError("missing exact trusted-loader publication failure")
    return int(match[1])


def request_adapter(client, head, target, apply, writes):
    token = client.installation_token(REPO)

    def github(path, payload=None, expected_status=None, step="request"):
        write = path == f"repos/{REPO}/statuses/{head}" and payload is not None
        if payload is not None and not write and path != "graphql":
            raise ValueError("unexpected write endpoint")
        if not write and not (path == "graphql" or path.startswith(f"repos/{REPO}/")):
            raise ValueError("unexpected repository endpoint")
        if write:
            expected = {"context": "mq-review", "state": "success", "description": DESCRIPTION,
                        "target_url": target}
            if payload != expected or writes:
                raise ValueError("unexpected or duplicate status payload")
            writes.append(dict(payload))
            if not apply:
                return {"dry_run": True}
        status, data = client._req("POST" if payload is not None else "GET", "/" + path, token, payload)
        wanted = expected_status or (201 if write else 200)
        if status != wanted:
            raise RuntimeError(f"App request failed at {step}: HTTP {status}")
        if write:
            read_status, rows = client._req("GET", f"/repos/{REPO}/commits/{head}/statuses?per_page=100", token)
            if read_status != 200 or not any(
                row.get("id") == data.get("id") and all(row.get(k) == v for k, v in payload.items())
                for row in rows if isinstance(row, dict)
            ):
                raise RuntimeError("status submitted but readback unconfirmed; inspect before retry")
        return data
    return github


def recover(evaluator, client, build, head, pr, apply=False):
    target = f"https://us-east-1.console.aws.amazon.com/codebuild/home?region=us-east-1#/builds/{build['id']}/view/new"
    writes = []
    request = request_adapter(client, head, target, apply, writes)
    pull = request(f"repos/{REPO}/pulls/{pr}", step="current pull request")
    if (pull.get("state") != "open" or pull.get("head", {}).get("sha") != head
            or pull.get("base", {}).get("ref") != "main"
            or pull.get("base", {}).get("repo", {}).get("full_name") != REPO
            or pull.get("head", {}).get("repo", {}).get("full_name") != REPO):
        raise ValueError("pull request source or base changed")
    previous_request = evaluator.github
    previous_url = os.environ.get("CODEBUILD_BUILD_URL")
    try:
        evaluator.github = request
        os.environ["CODEBUILD_BUILD_URL"] = target
        # The reused evaluator prints success even for a dry-run transport.
        # Suppress it; only the truthful receipt below reaches the operator.
        with contextlib.redirect_stdout(io.StringIO()):
            rc = evaluator.main(["--deferred", "--head-sha", head])
        if rc or len(writes) != 1:
            raise ValueError("existing mq-review evaluator did not authorize the exact status")
    finally:
        evaluator.github = previous_request
        if previous_url is None:
            os.environ.pop("CODEBUILD_BUILD_URL", None)
        else:
            os.environ["CODEBUILD_BUILD_URL"] = previous_url
    return {"repo": REPO, "head": head, "native_build": build["id"],
            "native_build_outcome": "FAILED", "source_tests_passed": False,
            "publication_performed": apply, "status": writes[0]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        if os.environ.get("GATE_APP_API_BASE") or os.environ.get("GATE_APP_CONFIG_DIR"):
            raise ValueError("test helper overrides are not allowed")
        import boto3
        from botocore.config import Config
        session = boto3.Session(profile_name=args.profile, region_name="us-east-1")
        config = Config(connect_timeout=10, read_timeout=30, retries={"max_attempts": 2})
        cb, logs = session.client("codebuild", config=config), session.client("logs", config=config)
        builds = cb.batch_get_builds(ids=[args.build_id])["builds"]
        if len(builds) != 1:
            raise ValueError("native build not found")
        build = builds[0]
        if build.get("logs", {}).get("groupName") != "/codebuild/" + PROJECT:
            raise ValueError("unexpected native log group")
        messages, cursor = [], None
        for _ in range(20):
            kwargs = {"logGroupName": build["logs"]["groupName"],
                      "logStreamName": build["logs"]["streamName"], "startFromHead": True, "limit": 1000}
            if cursor:
                kwargs["nextToken"] = cursor
            page = logs.get_log_events(**kwargs)
            messages.extend(e["message"] for e in page["events"])
            if sum(map(len, messages)) > 2_000_000:
                raise ValueError("native log exceeds bound")
            next_cursor = page["nextForwardToken"]
            if next_cursor == cursor:
                break
            cursor = next_cursor
        else:
            raise ValueError("native log pagination exceeds bound")
        pr = admit(build, args.build_id, args.head, messages)
        sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))
        import gate_app_checkruns
        client = gate_app_checkruns.GateAppClient()
        if client.app_id != 4627432 or client.installations.get(REPO) != 154468652:
            raise ValueError("unexpected approved App installation")
        evaluator_path = Path(__file__).with_name("mq_review.py")
        source = evaluator_path.read_bytes().replace(b"\r\n", b"\n")
        blob = hashlib.sha1(b"blob " + str(len(source)).encode() + b"\0" + source).hexdigest()
        if blob != "46b73f34683cd0526a8bef4e270dc45712e6cde3":
            raise ValueError("frozen mq-review evaluator changed")
        spec = importlib.util.spec_from_file_location("mq_evaluator", evaluator_path)
        evaluator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(evaluator)
        print(json.dumps(recover(evaluator, client, build, args.head, pr, args.apply)))
        return 0
    except Exception:
        # Provider/helper exception bodies may contain sensitive context.
        print(json.dumps({"ok": False, "error": "native mq App recovery refused; no success receipt"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
