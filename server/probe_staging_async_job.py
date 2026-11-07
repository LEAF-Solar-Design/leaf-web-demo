"""P-080 staging-only operator probe. Importing this module performs no I/O.

The approved runner supplies DATABASE_URL and P080_RUNTIME_RECEIPT (a fresh,
operator-reviewed JSON receipt). No application module is imported. The runtime
receipt names ECS tasks, source, database hash, shared mount, and operations role;
live preflight checks these against AWS and the runner before reading the fixed
acceptance secret. Identity receipts contain hashes, never credentials.

``run --out-dir`` names an existing evidence parent; the exclusive run directory
is ``<out-dir>/<run-id>``. Pass that directory to ``cleanup --receipt-dir``.
Both commands require the bare ``--ack`` flag.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import time
from types import SimpleNamespace
import uuid

ORIGIN = "https://platform-staging.leafdesign.ai"
SECRET_ID = "stg/leaf/acceptance-tenant-b"
ACCOUNT = "807034087062"
REGION = "us-east-1"
REVISION_LABEL = "org.opencontainers.image.revision"
COMPONENT_KEYS = {
    "app": ("LEAF_RUNTIME_ENV", "LEAF_JOBS_STORE", "LEAF_BROKER_STORE",
            "LEAF_DRAWING_STORE", "LEAF_BLOB_STORE", "LEAF_STORE_DIR"),
    "broker": ("LEAF_BROKER_STORE", "LEAF_DRAWING_STORE", "LEAF_BLOB_STORE",
               "LEAF_STORE_DIR"),
}
TOOL = "measure-panel-area"
ACK = "p080-r3-staging-only"
EXPECTED = {"layer": "p080_r3", "count": 2, "total_area": 300.0,
            "avg_area": 150.0, "units": "drawing"}
INTAKE = {"polylines": [
    {"layer": "p080_r3", "closed": True,
     "pts": [[0, 0], [10, 0], [10, 10], [0, 10]]},
    {"layer": "p080_r3", "closed": True,
     "pts": [[20, 0], [40, 0], [40, 10], [20, 10]]},
]}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


FIXTURE_BYTES = canonical(INTAKE)
FIXTURE_SHA = sha(FIXTURE_BYTES)


class Refusal(Exception):
    """Only locally authored reason codes may leave the process."""


def require(ok, reason):
    if not ok:
        raise Refusal(reason)


def check_component(component, task, definition, entry, source, env, receipt):
    require(task["lastStatus"] == "RUNNING"
            and task["taskArn"].split(":")[4] == ACCOUNT
            and task["taskArn"].split(":")[3] == REGION,
            "runtime_refused")
    containers = [c for c in definition["containerDefinitions"]
                  if c["name"] == entry["container"]]
    require(len(containers) == 1, "runtime_refused")
    container = containers[0]
    actual = [c for c in task["containers"] if c["name"] == entry["container"]]
    labels = container.get("dockerLabels", {})
    require(len(actual) == 1 and actual[0].get("imageDigest") == entry["image_digest"]
            and (REVISION_LABEL not in labels or labels[REVISION_LABEL] == source),
            "source_refused")
    effective = {v["name"]: v["value"] for v in container.get("environment", [])}
    for override in task.get("overrides", {}).get("containerOverrides", []):
        if override["name"] == container["name"]:
            effective.update({v["name"]: v["value"]
                              for v in override.get("environment", [])})
    for key in COMPONENT_KEYS[component]:
        require(effective.get(key) == env.get(key), "authority_refused")
    # The runner's protected URL must come from the same reviewed secret
    # reference as BOTH services, rather than a similarly named database.
    secrets = {v["name"]: v["valueFrom"] for v in container.get("secrets", [])}
    require(secrets.get("DATABASE_URL") == receipt["database_secret_arn"], "database_refused")
    volumes = {v["name"]: v for v in definition.get("volumes", [])}
    mounts = [m for m in container.get("mountPoints", [])
              if m["containerPath"] == "/data/drawings" and not m.get("readOnly")]
    require(len(mounts) == 1 and volumes[mounts[0]["sourceVolume"]].get(
        "efsVolumeConfiguration", {}).get("fileSystemId") == receipt["efs_id"],
        "mount_refused")


def served_source_ok(status, body, source) -> bool:
    return status == 200 and isinstance(body, dict) and body.get("source_sha") == source


def preflight(args, env):
    require(args.ack == ACK, "ack_required")
    require(bool(env.get("DATABASE_URL")), "database_required")
    require(env.get("LEAF_RUNTIME_ENV") == "staging", "environment_refused")
    if args.command == "run":
        require(args.base_url == ORIGIN, "origin_refused")
        require(bool(re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", args.run_id)), "run_id_refused")
        require(bool(re.fullmatch(r"[0-9a-f]{40}", args.expected_runtime_source)),
                "source_refused")


def validate_runtime_receipt(r, source, env, now):
    require(r.get("environment") == "staging" and r.get("source") == source
            and now - 900 < r.get("verified_at", 0) <= now
            and r.get("database_sha256") == sha(env["DATABASE_URL"]), "runtime_refused")
    for key in ("LEAF_JOBS_STORE", "LEAF_BROKER_STORE", "LEAF_DRAWING_STORE"):
        require(env.get(key) == "postgres", "authority_refused")
    require(env.get("LEAF_BLOB_STORE") == "filesystem"
            and env.get("LEAF_STORE_DIR") == "/data/drawings", "mount_refused")


def approved_tool(tools, approved):
    matches = [t for t in tools if t.get("name") == TOOL]
    require(len(matches) == 1, "catalog_refused")
    tool = matches[0]
    definition = {k: v for k, v in tool.items()
                  if k not in {"catalog_digest", "tool_manifest_sha256"}}
    digest = "sha256:" + sha(canonical(definition))
    require(tool.get("catalog_digest") == digest
            and tool.get("tool_manifest_sha256") == digest
            and sha(canonical(definition)) == approved
            and definition.get("engine_op") == "measure_panel_area"
            and definition.get("script") == "engine/tools/measure_panel_area.lsp"
            and definition.get("capabilities") == ["drawing.read"]
            and definition.get("version") == "1.0.0"
            and not definition.get("aps_live"), "catalog_refused")
    return digest


class Probe:
    """All effects cross the injected adapter; the submission is never retried."""

    def __init__(self, adapter):
        self.a = adapter
        self.receipt = None
        self.token = None
        self.tenant = None

    def save(self):
        # Receipt fields are constructed locally. No response/exception mapping
        # is ever copied here, including an apparently harmless arbitrary key.
        validate_receipt(self.receipt)
        self.a.save(self.receipt)

    def identity(self, identity_receipt):
        secret = self.a.secret(SECRET_ID)
        require(isinstance(secret, dict), "identity_refused")
        tenant, subject = secret.get("tenant_id"), secret.get("subject")
        require(isinstance(tenant, str) and isinstance(subject, str)
                and sha(tenant) == identity_receipt.get("tenant_sha256")
                and sha(subject) == identity_receipt.get("subject_sha256")
                and identity_receipt.get("principal") ==
                "Leaf staging acceptance tenant B (protected, 20260728)"
                and identity_receipt.get("synthetic_provisioning_verified") is True
                and isinstance(secret.get("jwt"), str)
                and len(secret["jwt"].split(".")) == 3
                and timestamp(secret.get("exp")) is not None
                and secret["exp"] >= self.a.now() + 660,
                "identity_refused")
        # These decoded fields are constraints, not signature verification.
        # The HTTPS identity endpoint performs the actual JWT verification.
        parts = secret["jwt"].split(".")
        header = json.loads(base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4)))
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        require(header.get("alg") == "RS256" and bool(parts[2])
                and claims.get("sub") == subject
                and timestamp(claims.get("exp")) is not None
                and claims["exp"] == secret["exp"], "identity_refused")
        # Canonical identity is a UUID; never derive it from a CLI argument.
        require(str(uuid.UUID(tenant)) == tenant, "identity_refused")
        require(self.a.binding(tenant, subject), "identity_refused")
        self.token, self.tenant = secret["jwt"], tenant
        identity = self.http("GET", "/api/session/identity")
        require(identity.get("tenant_id") == tenant, "identity_refused")

    def http(self, method, path, body=None, correlation=None):
        status, result = self.a.http(method, path, self.token, body, correlation)
        require(status < 300 and status >= 200, "http_refused")
        return result

    def run(self, args, env):
        preflight(args, env)
        outcome = "failed"
        try:
            self.a.check_origin()
            identity_receipt = self.a.read_json(args.identity_receipt)
            self.a.runtime(args.expected_runtime_source, identity_receipt)
            self.identity(identity_receipt)
            ent = self.http("GET", "/api/entitlements")
            require(ent.get("entitlements", {}).get("run_read") is True
                    and self.a.broker_entitled(self.tenant), "entitlement_refused")
            digest = approved_tool(self.http("GET", "/api/tools").get("tools", []),
                                   identity_receipt.get("tool_sha256"))
            # Exclusive directory creation prevents restart/resubmit, even if
            # the prior process died before saving its first receipt.
            self.a.reserve(args.out_dir, args.run_id)
            drawing = "p080-r3-" + self.a.new_uuid()
            require(re.fullmatch(r"p080-r3-[0-9a-f]{32}", drawing), "drawing_refused")
            self.receipt = {
                "schema": "p080-r3.v1", "drawing": drawing,
                "fixture_sha256": FIXTURE_SHA, "tenant_sha256": sha(self.tenant),
                "run_id": args.run_id, "source": args.expected_runtime_source,
                "submission_started": False, "setup_started": False,
                "job_id": None, "close_started": False,
                "samples": [], "acknowledged_202": False,
                "submit_http_status": None, "submit_body_status": None,
                "submit_error_code": None,
                "outcome": "failed", "cleanup_complete": False,
                "removal_started": False,
                "result_match": False, "database_http_agree": False,
                "broker_non_aps": False,
            }
            self.save()  # UUID and source hash precede ALL drawing writes.
            require(self.a.absent(self.tenant, drawing), "drawing_exists")
            self.receipt["setup_started"] = True
            self.save()
            self.a.ingest(self.tenant, drawing, FIXTURE_BYTES)
            require(self.a.fixture_valid(self.tenant, drawing, FIXTURE_SHA),
                    "fixture_refused")
            self.a.start_observer(self.tenant, drawing, "p080-r3:" + args.run_id)
            self.receipt["submission_started"] = True
            self.save()  # fsync BEFORE the one possible submission POST.
            try:
                status, body = self.a.http("POST", "/api/run", self.token, {
                    "tool": TOOL, "params": {"layer": "p080_r3"}, "dwg": drawing,
                    "dwg_version": 1, "catalog_digest": digest,
                }, "p080-r3:" + args.run_id)
                self.receipt["submit_http_status"] = (
                    status if type(status) is int and 100 <= status <= 599 else None)
                if isinstance(body, dict):
                    if "status" in body:
                        self.receipt["submit_body_status"] = (
                            body["status"] if body["status"] in
                            ("submitted", "queued", "running", "complete", "failed") else "other")
                    error = body.get("error")
                    for code in (error.get("error_code") if isinstance(error, dict) else None,
                                 body.get("error_code")):
                        if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
                            self.receipt["submit_error_code"] = code
                            break
                if status == 202 and body.get("status") == "submitted":
                    candidate = body.get("job_id")
                    if isinstance(candidate, str) and re.fullmatch(r"[0-9a-f-]{36}", candidate):
                        self.receipt["job_id"] = str(uuid.UUID(candidate))
                        self.receipt["acknowledged_202"] = True
            except Exception:
                pass  # Includes malformed JSON, redirects and transport failures.
            self.save()
            outcome = self.observe()
        except Refusal:
            outcome = "failed"
        except Exception:
            outcome = "failed"
        finally:
            if self.receipt is not None:
                self.receipt["outcome"] = outcome
                self.receipt["execution_outcome"] = outcome
                try:
                    clean = self.cleanup_resources()
                except Exception:
                    clean = False
                if not clean and outcome != "submission_unknown":
                    self.receipt["outcome"] = "cleanup_pending"
                self.save()
            self.a.stop_observer()
        return self.receipt["outcome"] if self.receipt else outcome

    def rows(self, current=False):
        read = self.a.read_jobs if current else self.a.jobs
        return read(self.tenant, self.receipt["drawing"],
                    "p080-r3:" + self.receipt["run_id"])

    def observe(self):
        deadline = self.a.monotonic() + 600
        seen = set()
        while self.a.monotonic() < deadline:
            rows = self.rows()
            if len(rows) > 1:
                self.receipt["reason"] = "duplicate_submission"
                return "failed"
            if not rows:
                self.a.sleep(0.25)
                continue
            row = rows[0]
            job_id = str(uuid.UUID(row["job_id"]))
            if self.receipt["job_id"] not in (None, job_id):
                return "failed"
            self.receipt["job_id"] = job_id
            if not self.owned(row):
                return "failed"
            status = row["status"]
            # A row timestamp is supplementary history, never an observation.
            if status not in seen:
                sample = {"status": status if status in
                          {"submitted", "running", "complete", "failed"} else "invalid",
                          "queued": row.get("progress") == "queued",
                          "attempt": int(row.get("attempt", 0)),
                          "started_at": timestamp(row.get("started_at")),
                          "finished_at": timestamp(row.get("finished_at")),
                          "lease_owner_sha256": sha(row["lease_owner"])
                          if row.get("lease_owner") else None,
                          "lease_expires_at": timestamp(row.get("lease_expires_at")),
                          "heartbeat_at": timestamp(row.get("heartbeat_at")),
                          "terminal_fingerprint_sha256": sha(row["terminal_fingerprint"])
                          if row.get("terminal_fingerprint") else None}
                self.receipt["samples"].append(sample)
                seen.add(status)
                self.save()
            if (row.get("attempt", 0) > 1 or row.get("conflicts")
                    or status == "failed" or row.get("retrying")
                    or (status == "submitted" and ("running" in seen
                                                    or row.get("progress") != "queued"))):
                return "failed"
            if status == "running":
                if not (type(row.get("attempt")) is int and row["attempt"] > 0
                        and isinstance(row.get("lease_owner"), str) and row["lease_owner"]
                        and all(timestamp(row.get(key)) is not None and row[key] > 0
                                for key in ("started_at", "lease_expires_at", "heartbeat_at"))):
                    return "failed"
            fetched = self.http("GET", "/api/jobs/" + job_id)
            if not (self.owned(fetched) and fetched.get("job_id") == job_id
                    and fetched.get("status") in {"submitted", "running", "complete", "failed"}):
                return "failed"
            if status == "complete":
                if not (row.get("attempt") == 1
                        and timestamp(row.get("finished_at")) is not None and row["finished_at"] > 0
                        and row.get("terminal_fingerprint") and not row.get("lease_owner")
                        and not row.get("lease_expires_at")
                        and good_result(row.get("result_json"))
                        and good_provenance(row)
                        and self.a.execution_valid(self.tenant, job_id)):
                    return "failed"
                self.receipt["result_match"] = True
                self.receipt["broker_non_aps"] = True
                if not (self.owned(fetched) and fetched.get("job_id") == job_id
                        and fetched.get("status") == "complete"
                        and fetched.get("result") == row.get("result_json")):
                    return "failed"
                self.receipt["database_http_agree"] = True
                return ("passed" if {"running", "complete"} <= seen
                        and ("submitted" in seen or self.receipt["acknowledged_202"] is True)
                        else "completed_transition_evidence_incomplete")
            self.a.sleep(0.25)
        if not seen:
            self.receipt["reason"] = "submission_unknown"
            return "submission_unknown"
        return "failed"

    def owned(self, row):
        return (row.get("tenant_id") == self.tenant and row.get("tool") == TOOL
                and row.get("dwg") == self.receipt["drawing"]
                and row.get("dwg_version") == 1
                and row.get("org_id") is None and row.get("project_id") is None)

    def cleanup_resources(self):
        r = self.receipt
        r["cleanup_complete"] = False
        if r["submission_started"]:
            rows = self.rows(current=True)
            if len(rows) != 1:
                return False
            row = rows[0]
            if r["job_id"] is None and self.owned(row):
                r["job_id"] = str(uuid.UUID(row["job_id"]))
                self.save()  # Resolve an earlier unknown POST by read only.
            if row["job_id"] != r["job_id"] or not self.owned(row):
                return False
            if row["status"] in {"submitted", "running"}:
                if not r["close_started"]:
                    r["close_started"] = True
                    self.save()
                    self.http("POST", "/api/jobs/" + r["job_id"] + "/close")
                deadline = self.a.monotonic() + 600
                while self.a.monotonic() < deadline:
                    if self.a.quiescent(self.tenant, r["job_id"]):
                        break
                    self.a.sleep(0.25)
                else:
                    return False
            if not self.a.quiescent(self.tenant, r["job_id"]):
                return False
        if r["setup_started"]:
            if not self.a.remove_fixture(self.tenant, r["drawing"], FIXTURE_SHA):
                return False
        r["cleanup_complete"] = True
        return True

    def cleanup(self, args, env):
        preflight(args, env)
        self.a.check_origin()
        self.a.open_receipt(args.receipt_dir)
        r = self.a.read_receipt()
        validate_receipt(r)
        require(r.get("schema") == "p080-r3.v1"
                and re.fullmatch(r"p080-r3-[0-9a-f]{32}", r.get("drawing", ""))
                and r.get("fixture_sha256") == FIXTURE_SHA
                and re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", r.get("run_id", "")),
                "receipt_refused")
        identity_receipt = self.a.cleanup_identity_receipt()
        self.a.runtime(r["source"], identity_receipt)
        self.identity(identity_receipt)
        require(sha(self.tenant) == r.get("tenant_sha256"), "identity_refused")
        self.receipt = r
        self.save()
        try:
            clean = self.cleanup_resources()
        except Exception:
            clean = False
        if not clean:
            r["outcome"] = "cleanup_pending"
        else:
            r["outcome"] = r.get("execution_outcome", "failed")
        self.save()
        return "cleanup_complete" if clean else "cleanup_pending"


def good_result(value):
    return (isinstance(value, dict) and value.get("ok") is True
            and value.get("result") == EXPECTED
            and not value.get("degraded_mode") and not value.get("fallback")
            and not value.get("error"))


def good_provenance(row):
    provenance = row.get("provenance_json")
    return (isinstance(provenance, dict) and type(provenance.get("attempt")) is int
            and provenance["attempt"] == 1 and provenance.get("execution_path") == "local"
            and not provenance.get("fallback")
            and row["result_json"].get("execution_provenance") == provenance)


def timestamp(value):
    import math
    return value if type(value) in (int, float) and math.isfinite(value) else None


def validate_receipt(r):
    keys = {"schema", "drawing", "fixture_sha256", "tenant_sha256", "run_id", "source",
            "submission_started", "setup_started", "job_id", "close_started", "samples",
            "acknowledged_202", "outcome", "cleanup_complete", "removal_started",
            "submit_http_status", "submit_body_status", "submit_error_code",
            "execution_outcome", "reason"}
    keys.update({"result_match", "database_http_agree", "broker_non_aps"})
    outcomes = {"passed", "failed", "submission_unknown", "cleanup_pending",
                "completed_transition_evidence_incomplete"}
    require(isinstance(r, dict) and not set(r) - keys, "receipt_refused")
    require(r.get("schema") == "p080-r3.v1"
            and r.get("fixture_sha256") == FIXTURE_SHA
            and re.fullmatch(r"p080-r3-[0-9a-f]{32}", r.get("drawing", ""))
            and re.fullmatch(r"[0-9a-f]{64}", r.get("tenant_sha256", ""))
            and re.fullmatch(r"[0-9a-f]{40}", r.get("source", ""))
            and re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", r.get("run_id", ""))
            and r.get("outcome") in outcomes
            and r.get("execution_outcome", "failed") in outcomes
            and r.get("reason") in (None, "duplicate_submission", "submission_unknown"),
            "receipt_refused")
    for key in ("submission_started", "setup_started", "close_started", "acknowledged_202",
                "cleanup_complete", "removal_started"):
        require(type(r.get(key)) is bool, "receipt_refused")
    for key in ("result_match", "database_http_agree", "broker_non_aps"):
        require(type(r.get(key)) is bool, "receipt_refused")
    status = r.get("submit_http_status")
    require(status is None or type(status) is int and 100 <= status <= 599,
            "receipt_refused")
    require(r.get("submit_body_status") in
            (None, "submitted", "queued", "running", "complete", "failed", "other"),
            "receipt_refused")
    code = r.get("submit_error_code")
    require(code is None or isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code),
            "receipt_refused")
    require(r.get("job_id") is None or str(uuid.UUID(r["job_id"])) == r["job_id"],
            "receipt_refused")
    require(isinstance(r.get("samples"), list) and len(r["samples"]) <= 8, "receipt_refused")
    for sample in r["samples"]:
        require(set(sample) == {"status", "queued", "attempt", "started_at", "finished_at",
                               "lease_owner_sha256", "lease_expires_at", "heartbeat_at",
                               "terminal_fingerprint_sha256"}
                and sample["status"] in {"submitted", "running", "complete", "failed", "invalid"}
                and type(sample["attempt"]) is int and 0 <= sample["attempt"] < 1000,
                "receipt_refused")
        require(type(sample["queued"]) is bool, "receipt_refused")
        for key in ("lease_owner_sha256", "terminal_fingerprint_sha256"):
            require(sample[key] is None or re.fullmatch(r"[0-9a-f]{64}", sample[key]),
                    "receipt_refused")
        for key in ("started_at", "finished_at", "lease_expires_at", "heartbeat_at"):
            require(sample[key] is None or timestamp(sample[key]) is not None, "receipt_refused")


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    for name in ("base-url", "expected-runtime-source", "run-id", "identity-receipt", "out-dir"):
        run.add_argument("--" + name, required=True)
    clean = sub.add_parser("cleanup")
    clean.add_argument("--receipt-dir", required=True)
    for child in (run, clean):
        child.add_argument("--ack", action="store_const", const=ACK)
    return p


class LiveAdapters:
    """Lazy operations-runner boundaries. Never import app/jobs/broker/write_loop.

    P080_RUNTIME_RECEIPT must be reviewed on the runner and less than 15 minutes
    old. It contains operations_role_arn, source, environment, database_sha256,
    database_name, mount_device, efs_id, cluster, app/broker task IDs and container
    names, and identity_receipt (for cleanup). Task definitions are inspected,
    not deployed. Missing or unreadable authority is a refusal.
    """

    def __init__(self, env):
        self.env = env
        self.directory = None
        self.observer = None
        self.observer_error = False
        self.pending = []
        self.runtime_receipt = None
        self.last_saved = None

    now = staticmethod(time.time)
    monotonic = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)

    @staticmethod
    def new_uuid():
        return uuid.uuid4().hex

    @staticmethod
    def read_json(path):
        return json.loads(Path(path).read_bytes())

    def reserve(self, out_dir, run_id):
        parent = Path(out_dir).resolve(strict=True)
        self.directory = parent / run_id
        require(not self.directory.exists() and not self.directory.is_symlink(), "run_exists")
        self.directory.mkdir(mode=0o700, exist_ok=False)
        self._sync_dir(parent)

    @staticmethod
    def _sync_dir(path):
        if hasattr(os, "O_DIRECTORY"):
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def save(self, receipt):
        # Exclusive temp file + rename + file AND directory fsync. Never use a
        # general response dump or a filesystem adapter supplied by fixture data.
        temporary = self.directory / ("receipt-" + uuid.uuid4().hex + ".next")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(receipt))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.directory / "receipt.json")
        self._sync_dir(self.directory)
        self.last_saved = receipt

    def open_receipt(self, directory):
        path = Path(directory)
        require(not path.is_symlink(), "receipt_refused")
        self.directory = path.resolve(strict=True)
        require(not (self.directory / "receipt.json").is_symlink(), "receipt_refused")

    def read_receipt(self):
        return self.read_json(self.directory / "receipt.json")

    def cleanup_identity_receipt(self):
        runtime = self.read_json(self.env["P080_RUNTIME_RECEIPT"])
        return self.read_json(runtime["identity_receipt"])

    def runtime(self, source, identity_receipt):
        r = self.read_json(self.env["P080_RUNTIME_RECEIPT"])
        validate_runtime_receipt(r, source, self.env, self.now())
        require(identity_receipt.get("runtime_source") == source, "runtime_refused")
        root = Path("/data/drawings")
        require(root.resolve(strict=True) == root and os.path.ismount(root)
                and root.stat().st_dev == r.get("mount_device"), "mount_refused")
        # Imports occur only after local origin, ack, environment and receipt
        # checks. Disable third-party diagnostic logging of request material.
        import logging
        logging.disable(logging.CRITICAL)
        import boto3
        from botocore.config import Config
        config = Config(retries={"max_attempts": 0}, connect_timeout=5, read_timeout=10)
        self.aws = boto3.session.Session(region_name=REGION)
        self.aws_config = config
        caller = self.aws.client("sts", config=config,
                                 endpoint_url=f"https://sts.{REGION}.amazonaws.com").get_caller_identity()
        # Receipt uses the actual assumed-role session ARN; no permissive suffix
        # matching of role names from another account.
        require(caller["Account"] == ACCOUNT
                and caller["Arn"] == r.get("operations_role_arn"), "role_refused")
        ecs = self.aws.client("ecs", config=config,
                              endpoint_url=f"https://ecs.{REGION}.amazonaws.com")
        for component in ("app", "broker"):
            entry = r[component]
            described = ecs.describe_tasks(cluster=r["cluster"], tasks=[entry["task"]])
            require(not described.get("failures") and len(described["tasks"]) == 1,
                    "runtime_refused")
            task = described["tasks"][0]
            definition = ecs.describe_task_definition(
                taskDefinition=task["taskDefinitionArn"])["taskDefinition"]
            check_component(component, task, definition, entry, source, self.env, r)
        status, body = self.http("GET", "/api/health", None)
        require(served_source_ok(status, body, source), "source_refused")
        self.runtime_receipt = r
        row = self.query("SELECT current_database() AS name")[0]
        require(row["name"] == r["database_name"], "database_refused")

    def connect(self):
        import psycopg
        from psycopg.rows import dict_row
        conn = psycopg.connect(self.env["DATABASE_URL"], autocommit=True,
                               connect_timeout=5, row_factory=dict_row)
        conn.execute("SELECT set_config('statement_timeout', '5000', false), "
                     "set_config('lock_timeout', '3000', false)")
        return conn

    def query(self, sql, params=(), connection=None):
        if connection is not None:
            return connection.execute(sql, params).fetchall()
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def secret(self, identifier):
        require(identifier == SECRET_ID, "secret_refused")
        client = self.aws.client("secretsmanager", config=self.aws_config,
                                 endpoint_url=f"https://secretsmanager.{REGION}.amazonaws.com")
        result = client.get_secret_value(SecretId=SECRET_ID)
        require(re.fullmatch(r"arn:aws:secretsmanager:" + REGION + ":" + ACCOUNT
                             + r":secret:stg/leaf/acceptance-tenant-b-[A-Za-z0-9]{6}",
                             result["ARN"]), "secret_refused")
        return json.loads(result["SecretString"])

    def binding(self, tenant, subject):
        rows = self.query(
            "SELECT o.status FROM identity_bindings b JOIN orgs o "
            "ON o.org_id=b.platform_tenant_id WHERE b.external_authority='auth0' "
            "AND b.external_subject=%s AND b.platform_tenant_id::text=%s "
            "AND b.status='active' AND b.revoked_at IS NULL", (subject, tenant))
        return len(rows) == 1 and rows[0]["status"] == "active"

    def broker_entitled(self, tenant):
        rows = self.query("SELECT tier, disabled FROM broker_tenants WHERE tenant_id=%s", (tenant,))
        # Pure policy module, no broker/application startup.
        import entitlements
        if len(rows) > 1 or (rows and rows[0]["disabled"]):
            return False
        tier = (entitlements.DEFAULT_TIER if not rows or rows[0]["tier"] is None
                else entitlements.resolve_tier(SimpleNamespace(tier=rows[0]["tier"])))
        return entitlements.entitlements_for(tier).get("run_read") is True

    def check_origin(self):
        # Resolve redirect/authentication failures before loading the bearer.
        status, _ = self.http("GET", "/api/session/identity", None)
        require(status == 401, "origin_refused")

    def http(self, method, path, token, body=None, correlation=None):
        import requests
        require(path in {"/api/run", "/api/tools", "/api/entitlements", "/api/session/identity",
                         "/api/health"}
                or re.fullmatch(r"/api/jobs/[0-9a-f-]{36}(/close)?", path), "destination_refused")
        headers = {"Authorization": "Bearer " + token} if token is not None else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if correlation is not None:
            headers["Idempotency-Key"] = correlation
        # No Session retries, proxy environment, redirect following or TLS override.
        with requests.Session() as session:
            session.trust_env = False
            response = session.request(method, ORIGIN + path, headers=headers, json=body,
                                       timeout=(5, 15), allow_redirects=False, verify=True)
            require(not 300 <= response.status_code < 400, "redirect_refused")
            require(len(response.content) <= 1024 * 1024, "response_refused")
            result = response.json()
            require(isinstance(result, dict), "response_refused")
            return response.status_code, result

    @staticmethod
    def paths(tenant, drawing):
        require(str(uuid.UUID(tenant)) == tenant
                and re.fullmatch(r"p080-r3-[0-9a-f]{32}", drawing), "path_refused")
        prefix = Path("/data/drawings") / "tenants" / tenant / "drawings" / drawing
        blob = prefix / "v" / "00000001.dwg"
        for path in [blob, *blob.parents]:
            require(not path.is_symlink(), "path_refused")
        require(blob.resolve().is_relative_to(prefix.resolve()), "path_refused")
        return prefix, blob

    def absent(self, tenant, drawing):
        prefix, _ = self.paths(tenant, drawing)
        return (not prefix.exists()
                and not self.query("SELECT 1 FROM drawing_store_manifests WHERE drawing_id=%s "
                                   "UNION ALL SELECT 1 FROM drawing_store_versions WHERE drawing_id=%s "
                                   "UNION ALL SELECT 1 FROM drawing_upload_attempts WHERE drawing_id=%s "
                                   "UNION ALL SELECT 1 FROM async_jobs WHERE dwg=%s",
                                   (drawing, drawing, drawing, drawing)))

    def ingest(self, tenant, drawing, data):
        require(data == FIXTURE_BYTES and self.absent(tenant, drawing), "fixture_refused")
        source = self.directory / "fixture.json"
        with source.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "da"))
        import store
        backend = store.FilesystemBackend("/data/drawings")
        result = store.ingest_drawing(backend, tenant, str(source), drawing_id=drawing)
        require(result == {"drawing_id": drawing, "version": 1}, "fixture_refused")

    def fixture_rows(self, tenant, drawing, conn=None):
        manifests = self.query("SELECT * FROM drawing_store_manifests "
                               "WHERE tenant_id=%s AND drawing_id=%s", (tenant, drawing), conn)
        versions = self.query("SELECT * FROM drawing_store_versions "
                             "WHERE tenant_id=%s AND drawing_id=%s"
                             + (" FOR UPDATE" if conn is not None else ""),
                             (tenant, drawing), conn)
        return manifests, versions

    def fixture_valid(self, tenant, drawing, digest, conn=None, missing_ok=False):
        _, blob = self.paths(tenant, drawing)
        manifests, versions = self.fixture_rows(tenant, drawing, conn)
        if len(manifests) != 1 or len(versions) != 1:
            return False
        m, v = manifests[0], versions[0]
        return (m["head"] == m["latest"] == v["version"] == 1
                and m["checkout_holder"] is None and m["checkout_expires_at"] is None
                and v["state"] == "ready" and v["content_sha256"] == digest
                and v.get("intake_ref") is None and v.get("intake_sha256") is None
                and v["byte_count"] == len(FIXTURE_BYTES)
                and v["object_key"] == str(blob.relative_to("/data/drawings"))
                and ((not blob.exists() and missing_ok)
                     or (blob.is_file() and blob.read_bytes() == FIXTURE_BYTES)))

    def read_jobs(self, tenant, drawing, correlation, conn=None):
        # Select by drawing first: a wrong tool/key or tenant must not hide an
        # extra job. The caller checks every returned row's ownership.
        rows = self.query("SELECT j.*, (SELECT count(*) FROM async_job_terminal_conflicts c "
                          "WHERE c.job_id=j.job_id) AS conflicts FROM async_jobs j "
                          "WHERE j.dwg=%s ORDER BY j.created_at", (drawing,), conn)
        require(all(r["tenant_id"] == tenant and r["tool"] == TOOL
                    and r["idempotency_key"] == correlation for r in rows), "job_refused")
        return rows

    def start_observer(self, tenant, drawing, correlation):
        import threading
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.lock = threading.Lock()

        def observe():
            try:
                with self.connect() as conn:
                    previous = None
                    deadline = self.monotonic() + 600
                    while not self.stop.is_set() and self.monotonic() < deadline:
                        rows = self.read_jobs(tenant, drawing, correlation, conn)
                        signature = [(r["job_id"], r["status"], r["attempt"])
                                     for r in rows]
                        if rows and signature != previous:
                            with self.lock:
                                self.pending.append(rows)
                            previous = signature
                        self.ready.set()
                        self.stop.wait(0.1)
            except Exception:
                self.observer_error = True
                self.ready.set()

        self.observer = threading.Thread(target=observe, daemon=True)
        self.observer.start()
        require(self.ready.wait(10) and not self.observer_error, "observer_refused")

    def jobs(self, tenant, drawing, correlation):
        require(not self.observer_error, "observer_refused")
        if self.observer is not None:
            with self.lock:
                if self.pending:
                    return self.pending.pop(0)
        return self.read_jobs(tenant, drawing, correlation)

    def stop_observer(self):
        if self.observer is not None:
            self.stop.set()
            self.observer.join(timeout=10)

    def execution_valid(self, tenant, job_id):
        event = job_id + ":broker-run"
        admissions = self.query("SELECT * FROM broker_run_admissions "
                                "WHERE tenant_id=%s AND event_key=%s", (tenant, event))
        ledger = self.query("SELECT * FROM broker_usage_ledger WHERE event_key=%s", (event,))
        return (len(admissions) == len(ledger) == 1
                and admissions[0]["state"] == "terminal"
                and admissions[0]["aps_live"] is False
                and admissions[0]["terminal_at"] is not None
                and admissions[0]["http_status"] == 200
                and admissions[0]["reserved_usd"] == 0
                and good_result(admissions[0]["result_json"])
                and ledger[0]["tenant_id"] == tenant and ledger[0]["job_id"] == job_id
                and ledger[0]["aps_live"] is False
                and ledger[0]["status"] == "ok"
                and ledger[0]["tool"] == TOOL and ledger[0]["engine_op"] == "measure_panel_area"
                and not self.query("SELECT 1 FROM jobs WHERE spine_ref=%s", (job_id,))
                and not self.query("SELECT 1 FROM aps_completion_receipts WHERE job_id=%s", (job_id,)))

    def quiescent(self, tenant, job_id):
        jobs = self.query("SELECT status FROM async_jobs WHERE tenant_id=%s AND job_id=%s",
                          (tenant, job_id))
        active = self.query("SELECT 1 FROM broker_run_admissions WHERE tenant_id=%s "
                            "AND event_key=%s AND state <> 'terminal' UNION ALL "
                            "SELECT 1 FROM broker_aps_slots WHERE tenant_id=%s "
                            "AND event_key=%s AND state='held'",
                            (tenant, job_id + ":broker-run", tenant, job_id + ":broker-run"))
        return len(jobs) == 1 and jobs[0]["status"] in {"complete", "failed"} and not active

    def foreign_references(self, tenant, drawing, conn):
        # Read the actual catalog, including tables added after this probe was
        # written. Unknown referencing resources block cleanup rather than being
        # silently treated as absent. Identifiers are quoted by psycopg.sql.
        from psycopg import sql
        columns = self.query("SELECT table_schema, table_name, column_name FROM "
                             "information_schema.columns WHERE table_schema='public' "
                             "AND column_name IN ('drawing_id', 'dwg', 'object_key', 'oss_object', 'intake_ref')",
                             connection=conn)
        own = {"drawing_store_manifests", "drawing_store_versions", "async_jobs"}
        prefix, blob = self.paths(tenant, drawing)
        for column in columns:
            if column["table_name"] in own:
                continue
            statement = sql.SQL("SELECT 1 FROM {}.{} WHERE {}::text = ANY(%s) LIMIT 1").format(
                sql.Identifier(column["table_schema"]), sql.Identifier(column["table_name"]),
                sql.Identifier(column["column_name"]))
            if self.query(statement, ([drawing, str(blob.relative_to("/data/drawings")),
                                       str(prefix.relative_to("/data/drawings"))],), conn):
                return True
        return False

    def remove_fixture(self, tenant, drawing, digest):
        prefix, blob = self.paths(tenant, drawing)
        source = self.directory / "fixture.json"
        require(not source.is_symlink(), "cleanup_refused")
        if source.exists():
            require(source.read_bytes() == FIXTURE_BYTES, "cleanup_refused")
        with self.connect() as conn:
            with conn.transaction():
                # Fence manifest changes and admissions while validating/removing.
                self.query("SELECT head FROM drawing_store_manifests "
                           "WHERE tenant_id=%s AND drawing_id=%s FOR UPDATE", (tenant, drawing), conn)
                rows = self.read_jobs(tenant, drawing, "p080-r3:" + self.last_saved["run_id"], conn)
                if any(r["status"] not in {"complete", "failed"} for r in rows):
                    return False
                if any(not self.quiescent(tenant, r["job_id"]) for r in rows):
                    return False
                if self.foreign_references(tenant, drawing, conn):
                    return False
                marker = prefix / "manifest.json"
                files = list(prefix.rglob("*")) if prefix.exists() else []
                if any(p.is_symlink() or p not in {prefix / "v", blob, marker} for p in files):
                    return False
                if marker.exists() and marker.read_bytes() != b'{"authority":"postgres"}':
                    return False
                manifests, versions = self.fixture_rows(tenant, drawing, conn)
                if not manifests and not versions:
                    if blob.exists():
                        return False
                else:
                    if not self.fixture_valid(tenant, drawing, digest, conn,
                                              missing_ok=self.last_saved.get("removal_started", False)):
                        return False
                    self.last_saved["removal_started"] = True
                    self.save(self.last_saved)
                    if blob.exists():
                        blob.unlink()
                        self._sync_dir(blob.parent)
                    if marker.exists():
                        marker.unlink()
                        self._sync_dir(prefix)
                    removed = self.query(
                        "DELETE FROM drawing_store_manifests WHERE tenant_id=%s AND drawing_id=%s "
                        "AND head=1 AND latest=1 AND checkout_holder IS NULL "
                        "AND checkout_expires_at IS NULL RETURNING drawing_id", (tenant, drawing), conn)
                    require(len(removed) == 1, "cleanup_refused")
        for directory in (prefix / "v", prefix):
            if directory.exists():
                directory.rmdir()  # Empty, exact directories only; never recursive delete.
        if source.exists():
            source.unlink()
        m, v = self.fixture_rows(tenant, drawing)
        return not m and not v and not prefix.exists() and not source.exists()


def main(argv=None):
    # Never send argparse's echo of an invalid argument to logs: a mistaken
    # operator might have put a token in an unsupported CLI option.
    import contextlib
    import io
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            args = parser().parse_args(argv)
        preflight(args, os.environ)
        class SilentSink:
            def write(self, value):
                return len(value)

            def flush(self):
                pass

        # Guard diagnostic prints in dependencies too. Only the fixed outcome
        # envelope below reaches stdout; no captured text is persisted.
        with contextlib.redirect_stdout(SilentSink()), contextlib.redirect_stderr(SilentSink()):
            adapter = LiveAdapters(os.environ)
            probe = Probe(adapter)
            outcome = getattr(probe, args.command)(args, os.environ)
        print(json.dumps({"outcome": outcome}))
        return 0 if outcome in {"passed", "cleanup_complete"} else 1
    except (Exception, SystemExit, KeyboardInterrupt):
        print('{"outcome":"failed","reason":"probe_refused"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
