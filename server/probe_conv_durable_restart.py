"""P058 operator restart probe. Importing this module performs no I/O.

The token is supplied only through LEAF_P058_TOKEN. All effects are injected;
no application modules are imported and writes are never automatically retried.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time
import uuid

ENVIRONMENTS = {
    "staging": {"origin": "https://platform-staging.leafdesign.ai",
                "cluster": "leaf-automation-staging", "service": "leaf-platform-app",
                "ack": "p058-staging-restart"},
    "production": {"origin": "https://platform.leafdesign.ai",
                   "cluster": "leaf-automation-production", "service": "leaf-platform",
                   "ack": "p058-production-restart"},
}
ACCOUNT = "807034087062"
REGION = "us-east-1"
SCHEMA = "p058-restart.v1"
DELIVERY_PROJECT = "leaf-forge-delivery-runner"
RESTART_TIMEOUT_S = 900
HEALTH_TIMEOUT_S = 300
POLL_S = 10
HTTP_TIMEOUT_S = 15
CHECKS = ("preflight", "written", "tail_before", "restarted", "healthy",
          "tail_after", "replay_same")
EXITS = {"PASS": 0, "FAIL": 1, "REFUSED": 2, "UNKNOWN": 3}


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Stop(Exception):
    def __init__(self, outcome, reason):
        self.outcome, self.reason = outcome, reason


def require(ok, outcome, reason):
    if not ok:
        raise Stop(outcome, reason)


def steady(service):
    deployments = service.get("deployments", [])
    return (len(deployments) == 1
            and deployments[0].get("rolloutState") == "COMPLETED"
            and type(service.get("runningCount")) is int
            and type(service.get("desiredCount")) is int
            and service["runningCount"] == service["desiredCount"])


def identifier(value):
    # UUID parsing prevents arbitrary remote text entering paths or receipts.
    return str(uuid.UUID(value))


class Probe:
    def __init__(self, adapter):
        self.a = adapter
        self.receipt = None
        self.phase = "preflight_unreadable"

    def preflight(self, args, env):
        require(bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.run_id)),
                "REFUSED", "run_id_refused")
        require(self.a.out_dir_exists(args.out_dir), "REFUSED", "out_dir_refused")
        try:
            self.a.reserve(args.out_dir, args.run_id)
        except FileExistsError:
            raise Stop("REFUSED", "run_exists") from None
        self.receipt = {
            "schema": SCHEMA, "env": args.env, "origin": ENVIRONMENTS[args.env]["origin"],
            "run_id": args.run_id, "project_sha256": sha(args.project_id),
            "conversation_id": None, "message_id": None, "content_sha256": None,
            "source_sha_before": None, "source_sha_after": None,
            "tasks_before": [], "tasks_after": [], "task_definition": None,
            "checks": dict.fromkeys(CHECKS, False), "outcome": "UNKNOWN", "reason": None,
            "started_at": self.a.now(), "finished_at": None,
        }
        require(args.ack == ENVIRONMENTS[args.env]["ack"], "REFUSED", "ack_refused")
        try:
            self.project = identifier(args.project_id)
            valid = uuid.UUID(self.project).int != 0
        except (ValueError, TypeError, AttributeError):
            valid = False
        require(valid, "REFUSED", "project_refused")
        self.token = env.get("LEAF_P058_TOKEN")
        require(isinstance(self.token, str) and bool(self.token.strip()),
                "REFUSED", "token_required")
        require(self.a.caller_account() == ACCOUNT, "REFUSED", "account_refused")
        service = self.a.service(args.env)
        require(steady(service), "REFUSED", "service_unsteady")
        definition = service.get("taskDefinition")
        require(isinstance(definition, str) and bool(re.fullmatch(
            r"arn:aws:ecs:us-east-1:807034087062:task-definition/[A-Za-z0-9_-]+:[0-9]+",
            definition)), "REFUSED", "service_unsteady")
        self.receipt["task_definition"] = definition
        if args.env == "staging":
            require(self.a.delivery_builds_in_progress() == 0,
                    "REFUSED", "delivery_in_flight")
        self.receipt["checks"]["preflight"] = True

    def http(self, method, path, body=None, key=None):
        return self.a.http(method, path, self.token, body, key)

    def health(self):
        status, body = self.http("GET", "/api/health")
        source = body.get("source_sha") if isinstance(body, dict) else None
        if not isinstance(source, str) or not re.fullmatch(r"[0-9a-f]{7,64}", source):
            source = None
        return status, source

    def tasks(self, environment):
        tasks = self.a.running_tasks(environment)
        require(isinstance(tasks, list) and bool(tasks)
                and all(isinstance(t, str) and re.fullmatch(r"[0-9a-f]{32}", t)
                        for t in tasks), "UNKNOWN", "tasks_unreadable")
        return sorted(set(tasks))

    def before(self, args):
        self.phase = "before_unreadable"
        status, source = self.health()
        require(status == 200, "UNKNOWN", "health_unobserved")
        self.receipt["source_sha_before"] = source
        self.receipt["tasks_before"] = self.tasks(args.env)

    def write(self, args):
        self.phase = "write_unobserved"
        self.base = "/api/projects/" + self.project + "/conversations"
        status, body = self.http("POST", self.base, {})
        require(status == 201, "FAIL", "conversation_write_failed")
        self.receipt["conversation_id"] = identifier(body["conversation_id"])
        self.message_path = self.base + "/" + self.receipt["conversation_id"] + "/messages"
        nonce = self.a.nonce()
        require(isinstance(nonce, str) and bool(re.fullmatch(r"[0-9a-f]{16}", nonce)),
                "UNKNOWN", "nonce_unreadable")
        self.content = "p058 " + args.run_id + " " + nonce
        self.body = {"content": self.content,
                     "metadata": {"source": "p058-probe", "client_message_id": "p058-" + args.run_id}}
        self.key = "p058-" + args.run_id
        self.receipt["content_sha256"] = sha(self.content)
        status, body = self.http("POST", self.message_path, self.body, self.key)
        require(status == 201, "FAIL", "message_write_failed")
        self.receipt["message_id"] = identifier(body["message_id"])
        self.created_at = body["created_at"]
        require(isinstance(self.created_at, str) and bool(self.created_at),
                "UNKNOWN", "write_unobserved")
        self.receipt["checks"]["written"] = True

    def read_tail(self, after=False):
        self.phase = "tail_unreadable"
        status, body = self.http("GET", self.base + "/recovery/tail?limit=50")
        require(status == 200, "UNKNOWN", "tail_unreadable")
        items, gaps = body["items"], body["gaps"]
        require(isinstance(items, list) and isinstance(gaps, list), "UNKNOWN", "tail_unreadable")
        require(all(item.get("conversation_id") == self.receipt["conversation_id"]
                    for item in items), "UNKNOWN", "tail_superseded")
        if not items:
            status, body = self.http("GET", self.base)
            require(status == 200, "UNKNOWN", "tail_unreadable")
            conversations = body["conversations"]
            require(isinstance(conversations, list), "UNKNOWN", "tail_unreadable")
            require(bool(conversations)
                    and conversations[-1].get("conversation_id") == self.receipt["conversation_id"],
                    "UNKNOWN", "tail_superseded")
        matches = [item for item in items if item.get("message_id") == self.receipt["message_id"]]
        reason = "tail_missing_after" if after else "tail_missing_before"
        require(len(matches) == 1 and not gaps, "FAIL", reason)
        require(matches[0].get("content") == self.content
                and (not after or matches[0].get("created_at") == self.created_at),
                "FAIL", "tail_changed" if after else reason)
        self.receipt["checks"]["tail_after" if after else "tail_before"] = True

    def restart(self, args):
        self.phase = "restart_unobserved"
        service = self.a.service(args.env)
        task_definition = self.receipt["task_definition"]
        require(steady(service) and service.get("taskDefinition") == task_definition,
                "REFUSED", "service_drifted")
        self.a.force_new_deployment(args.env, task_definition)
        deadline = self.a.monotonic() + RESTART_TIMEOUT_S
        while True:
            try:
                service = self.a.service(args.env)
                tasks = self.tasks(args.env)
                if (steady(service) and service.get("taskDefinition") == self.receipt["task_definition"]
                        and set(tasks).isdisjoint(self.receipt["tasks_before"])):
                    self.receipt["tasks_after"] = tasks
                    self.receipt["checks"]["restarted"] = True
                    break
            except Exception:
                pass  # An unreadable sample cannot prove a restart.
            remaining = deadline - self.a.monotonic()
            require(remaining > 0, "UNKNOWN", "restart_unobserved")
            self.a.sleep(min(POLL_S, remaining))
        self.phase = "health_unobserved"
        deadline = self.a.monotonic() + HEALTH_TIMEOUT_S
        while True:
            try:
                status, source = self.health()
                if status == 200:
                    self.receipt["source_sha_after"] = source
                    self.receipt["checks"]["healthy"] = True
                    return
            except Exception:
                pass
            remaining = deadline - self.a.monotonic()
            require(remaining > 0, "UNKNOWN", "health_unobserved")
            self.a.sleep(min(POLL_S, remaining))

    def idempotency(self):
        self.phase = "replay_unobserved"
        status, body = self.http("POST", self.message_path, self.body, self.key)
        require(status == 200 and body.get("message_id") == self.receipt["message_id"],
                "FAIL", "replay_duplicated")
        self.receipt["checks"]["replay_same"] = True

    def run(self, args, env):
        outcome, reason = "PASS", None
        try:
            self.preflight(args, env)
            self.before(args)
            self.write(args)
            self.read_tail()
            self.restart(args)
            self.read_tail(after=True)
            self.idempotency()
        except Stop as exc:
            outcome, reason = exc.outcome, exc.reason
        except Exception:
            outcome, reason = "UNKNOWN", self.phase
        if self.receipt is not None:
            self.receipt.update(outcome=outcome, reason=reason, finished_at=self.a.now())
            try:
                self.a.save(self.receipt)
            except Exception:
                outcome, reason = "UNKNOWN", "receipt_unwritten"
        return outcome, reason


class LiveAdapters:
    def __init__(self, environment):
        self.environment = ENVIRONMENTS[environment]

    def now(self):
        return time.time()

    def monotonic(self):
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(seconds)

    def nonce(self):
        return secrets.token_hex(8)

    def out_dir_exists(self, directory):
        return Path(directory).is_dir()

    def reserve(self, directory, run_id):
        self.directory = Path(directory) / run_id
        self.directory.mkdir(mode=0o700)

    def save(self, receipt):
        temporary = self.directory / "receipt.json.tmp"
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(receipt, stream, sort_keys=True, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.directory / "receipt.json")

    def client(self, name):
        import boto3
        from botocore.config import Config
        return boto3.client(name, region_name=REGION, config=Config(
            retries={"max_attempts": 0}, connect_timeout=5, read_timeout=10))

    def caller_account(self):
        return self.client("sts").get_caller_identity()["Account"]

    def service(self, environment):
        config = ENVIRONMENTS[environment]
        result = self.client("ecs").describe_services(
            cluster=config["cluster"], services=[config["service"]])
        if result.get("failures") or len(result.get("services", [])) != 1:
            return {}
        return result["services"][0]

    def running_tasks(self, environment):
        config = ENVIRONMENTS[environment]
        client = self.client("ecs")
        tasks, token = [], None
        while True:
            kwargs = {"cluster": config["cluster"], "serviceName": config["service"],
                      "desiredStatus": "RUNNING"}
            if token:
                kwargs["nextToken"] = token
            page = client.list_tasks(**kwargs)
            tasks.extend(arn.rsplit("/", 1)[-1] for arn in page["taskArns"])
            token = page.get("nextToken")
            if not token:
                return tasks

    def delivery_builds_in_progress(self):
        client = self.client("codebuild")
        count, token = 0, None
        while True:
            kwargs = {"projectName": DELIVERY_PROJECT}
            if token:
                kwargs["nextToken"] = token
            page = client.list_builds_for_project(**kwargs)
            ids = page["ids"]
            for offset in range(0, len(ids), 100):
                builds = client.batch_get_builds(ids=ids[offset:offset + 100])
                if builds.get("buildsNotFound") or len(builds["builds"]) != len(ids[offset:offset + 100]):
                    raise ValueError("builds_unreadable")
                count += sum(b["buildStatus"] == "IN_PROGRESS" for b in builds["builds"])
            token = page.get("nextToken")
            if not token:
                return count

    def force_new_deployment(self, environment, task_definition):
        config = ENVIRONMENTS[environment]
        self.client("ecs").update_service(cluster=config["cluster"],
                                          service=config["service"], taskDefinition=task_definition,
                                          forceNewDeployment=True)

    def http(self, method, path, token, body=None, key=None):
        import urllib.error
        import urllib.request

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        if key is not None:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(self.environment["origin"] + path,
                                         data=None if body is None else json.dumps(body).encode(),
                                         headers=headers, method=method)
        opener = urllib.request.build_opener(NoRedirect())
        try:
            with opener.open(request, timeout=HTTP_TIMEOUT_S) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, {}  # Never retain error bodies or redirect locations.


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Stop("REFUSED", "arguments_refused")


def main(argv=None, env=None, adapters=None):
    parser = Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--env", choices=tuple(ENVIRONMENTS), required=True)
    for option in ("project-id", "out-dir", "run-id", "ack"):
        run.add_argument("--" + option, required=True)
    try:
        args = parser.parse_args(argv)
        outcome, reason = Probe(adapters if adapters is not None else LiveAdapters(args.env)).run(
            args, os.environ if env is None else env)
    except Stop as exc:
        outcome, reason = exc.outcome, exc.reason
    except Exception:
        outcome, reason = "UNKNOWN", "probe_unreadable"
    print(json.dumps({"outcome": outcome, "reason": reason}))
    return EXITS[outcome]


if __name__ == "__main__":
    raise SystemExit(main())
