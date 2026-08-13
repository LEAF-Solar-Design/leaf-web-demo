#!/usr/bin/env python3
"""Build one provider-associated, read-only staging convergence receipt.

The production CLI accepts only GitHub run identifiers. All release authority is
discovered again from provider records and immutable artifacts. This module does
not dispatch workflows, call AWS, or accept a caller-built evidence bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Protocol
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from scripts.platform_release_manifest import (
    SCHEMA_V3,
    SERVICES,
    validate_manifest,
)


APP_REPOSITORY = "LEAF-Solar-Design/leaf-web-demo"
TF_REPOSITORY = "LEAF-Solar-Design/leaf-automation-aws-terraform"
BUILD_WORKFLOW = ".github/workflows/build-platform-images.yml"
RELAY_WORKFLOW = ".github/workflows/dispatch-staging-deploys.yml"
DEPLOY_WORKFLOW = ".github/workflows/deploy-leaf-platform-staging.yml"
OUTPUT_SCHEMA = "leaf.platform-staging-convergence.v1"
SERVICE_RECEIPT_SCHEMA = "leaf.platform-staging-service-run.v1"
ENVIRONMENT = "staging"
SERVICE_ORDER = tuple(SERVICES)
TERMINAL_CONCLUSIONS = {
    "success",
    "failure",
    "cancelled",
    "timed_out",
    "action_required",
    "neutral",
    "skipped",
    "stale",
    "startup_failure",
}
SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA64 = re.compile(r"^[0-9a-f]{64}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
DECIMAL = re.compile(r"^[1-9][0-9]*$")
ARTIFACT_FILE = "leaf-platform-staging-service-run.json"
SECRET_KEY = re.compile(
    r"(?i)(secret|token|password|authorization|credential|private.?key|key.?material)"
)
SECRET_VALUE = re.compile(
    r"(?i)(bearer\s|github_pat_|gh[pousr]_|AKIA[0-9A-Z]{16}|"
    r"ASIA[0-9A-Z]{16}|-----BEGIN[^-]*PRIVATE KEY-----|://[^/\s]+:[^/@\s]+@)"
)


class ContractError(RuntimeError):
    """A closed, sanitized provider-evidence failure."""

    def __init__(self, reason: str) -> None:
        if not re.fullmatch(r"[A-Z0-9_]+", reason):
            raise ValueError("contract reason must be a closed code")
        super().__init__(reason)
        self.reason = reason


class Provider(Protocol):
    def json(self, repository: str, endpoint: str) -> Any: ...

    def bytes(self, repository: str, endpoint: str) -> bytes: ...


class GitHubProvider:
    """Least-privilege GET-only GitHub evidence adapter."""

    def __init__(self, app_token: str, terraform_token: str) -> None:
        if not app_token or not terraform_token:
            raise ContractError("UNCONFIGURED_PROVIDER_CREDENTIAL")
        self._tokens = {
            APP_REPOSITORY: app_token,
            TF_REPOSITORY: terraform_token,
        }

    def _request(self, repository: str, endpoint: str) -> bytes:
        token = self._tokens.get(repository)
        if token is None or not endpoint.startswith("/") or "//" in endpoint:
            raise ContractError("PROVIDER_REQUEST_REJECTED")
        request = urllib.request.Request(
            f"https://api.github.com/repos/{repository}{endpoint}",
            method="GET",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "leaf-platform-convergence-finalizer/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ContractError("PROVIDER_READ_FAILED") from exc

    def json(self, repository: str, endpoint: str) -> Any:
        try:
            return _load_json(self._request(repository, endpoint))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ContractError("PROVIDER_JSON_INVALID") from exc

    def bytes(self, repository: str, endpoint: str) -> bytes:
        return self._request(repository, endpoint)


def _load_json(raw: bytes) -> Any:
    def pairs(value: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, item in value:
            if key in output:
                raise ValueError("duplicate JSON key")
            output[key] = item
        return output

    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _exact(value: Any, keys: set[str], reason: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ContractError(reason)
    return value


def _positive(value: Any, reason: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractError(reason)
    return value


def _sha40(value: Any, reason: str) -> str:
    if not isinstance(value, str) or not SHA40.fullmatch(value):
        raise ContractError(reason)
    return value


def _sha64(value: Any, reason: str) -> str:
    if not isinstance(value, str) or not SHA64.fullmatch(value):
        raise ContractError(reason)
    return value


def _provider_run(raw: Any, repository: str, workflow: str, event: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ContractError("PROVIDER_RUN_INVALID")
    required = {
        "id",
        "run_attempt",
        "event",
        "head_sha",
        "head_branch",
        "path",
        "status",
        "conclusion",
        "created_at",
        "updated_at",
    }
    if not required.issubset(raw):
        raise ContractError("PROVIDER_RUN_INVALID")
    run = {
        "repository": repository,
        "workflow_path": raw["path"],
        "run_id": _positive(raw["id"], "PROVIDER_RUN_INVALID"),
        "run_attempt": _positive(raw["run_attempt"], "PROVIDER_RUN_INVALID"),
        "event": raw["event"],
        "head_sha": _sha40(raw["head_sha"], "PROVIDER_RUN_INVALID"),
        "head_branch": raw["head_branch"],
        "status": raw["status"],
        "conclusion": raw["conclusion"],
        "created_at": raw["created_at"],
        "updated_at": raw["updated_at"],
    }
    if (
        run["workflow_path"] != workflow
        or run["event"] != event
        or run["status"] != "completed"
        or run["conclusion"] not in TERMINAL_CONCLUSIONS
        or not all(isinstance(run[key], str) for key in ("created_at", "updated_at"))
    ):
        raise ContractError("PROVIDER_RUN_MISMATCH")
    return run


def _workflow_blob(provider: Provider, repository: str, sha: str, path: str) -> tuple[str, str]:
    commit = provider.json(repository, f"/git/commits/{sha}")
    if not isinstance(commit, dict) or not isinstance(commit.get("tree"), dict):
        raise ContractError("PROVIDER_COMMIT_INVALID")
    tree_sha = _sha40(commit["tree"].get("sha"), "PROVIDER_COMMIT_INVALID")
    tree = provider.json(repository, f"/git/trees/{tree_sha}?recursive=1")
    rows = tree.get("tree") if isinstance(tree, dict) else None
    matches = [
        row
        for row in rows or []
        if isinstance(row, dict) and row.get("path") == path and row.get("type") == "blob"
    ]
    if len(matches) != 1:
        raise ContractError("PROVIDER_WORKFLOW_BLOB_INVALID")
    return tree_sha, _sha40(matches[0].get("sha"), "PROVIDER_WORKFLOW_BLOB_INVALID")


def _artifact_rows(provider: Provider, repository: str, run_id: int) -> list[dict[str, Any]]:
    raw = provider.json(repository, f"/actions/runs/{run_id}/artifacts?per_page=100")
    if not isinstance(raw, dict) or not isinstance(raw.get("artifacts"), list):
        raise ContractError("PROVIDER_ARTIFACT_LIST_INVALID")
    if raw.get("total_count") != len(raw["artifacts"]):
        raise ContractError("PROVIDER_ARTIFACT_PAGINATION_UNPROVEN")
    return [row for row in raw["artifacts"] if isinstance(row, dict)]


def _workflow_run_rows(
    provider: Provider,
    repository: str,
    workflow: str,
    parameters: dict[str, object],
) -> list[dict[str, Any]]:
    base = f"/actions/workflows/{workflow}/runs?" + urllib.parse.urlencode(parameters)
    page = 1
    output: list[dict[str, Any]] = []
    total: int | None = None
    seen: set[int] = set()
    while total is None or len(output) < total:
        endpoint = base if page == 1 else f"{base}&page={page}"
        raw = provider.json(repository, endpoint)
        if not isinstance(raw, dict) or not isinstance(raw.get("workflow_runs"), list):
            raise ContractError("PROVIDER_RUN_LIST_INVALID")
        if total is None:
            total = raw.get("total_count")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise ContractError("PROVIDER_RUN_LIST_INVALID")
        elif raw.get("total_count") != total:
            raise ContractError("PROVIDER_RUN_LIST_DRIFT")
        rows = raw["workflow_runs"]
        if not rows and len(output) < total:
            raise ContractError("PROVIDER_RUN_PAGINATION_UNPROVEN")
        for row in rows:
            if not isinstance(row, dict) or isinstance(row.get("id"), bool) or not isinstance(row.get("id"), int):
                raise ContractError("PROVIDER_RUN_LIST_INVALID")
            if row["id"] in seen:
                raise ContractError("PROVIDER_RUN_DUPLICATE")
            seen.add(row["id"])
            output.append(row)
        if len(output) > total:
            raise ContractError("PROVIDER_RUN_LIST_DRIFT")
        page += 1
        if page > 100:
            raise ContractError("PROVIDER_RUN_PAGINATION_UNPROVEN")
    if len(output) != total:
        raise ContractError("PROVIDER_RUN_PAGINATION_UNPROVEN")
    return output


def _one_artifact(
    provider: Provider,
    repository: str,
    run_id: int,
    name: str,
    filename: str,
) -> tuple[dict[str, Any], Any]:
    matches = [
        row
        for row in _artifact_rows(provider, repository, run_id)
        if row.get("name") == name and row.get("expired") is False
    ]
    if len(matches) != 1:
        raise ContractError("PROVIDER_ARTIFACT_CARDINALITY")
    row = matches[0]
    workflow_run = row.get("workflow_run")
    if (
        not isinstance(workflow_run, dict)
        or workflow_run.get("id") != run_id
        or not SHA40.fullmatch(str(workflow_run.get("head_sha", "")))
    ):
        raise ContractError("PROVIDER_ARTIFACT_ASSOCIATION")
    artifact_id = _positive(row.get("id"), "PROVIDER_ARTIFACT_INVALID")
    zip_raw = provider.bytes(repository, f"/actions/artifacts/{artifact_id}/zip")
    payload = _zip_member(zip_raw, filename)
    evidence = {
        "artifact_id": artifact_id,
        "artifact_name": name,
        "provider_zip_sha256": _sha(zip_raw),
        "file_sha256": _sha(payload),
    }
    try:
        return evidence, _load_json(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ContractError("PROVIDER_ARTIFACT_JSON_INVALID") from exc


def _zip_member(raw: bytes, filename: str) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = [info.filename for info in archive.infolist() if not info.is_dir()]
            if names != [filename] or filename.startswith(("/", "\\")) or ".." in Path(filename).parts:
                raise ContractError("PROVIDER_ARCHIVE_SHAPE_INVALID")
            return archive.read(filename)
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise ContractError("PROVIDER_ARCHIVE_INVALID") from exc


def _supply(manifest: Any, build: dict[str, Any], tree: str) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise ContractError("SUPPLY_MANIFEST_INVALID")
    try:
        validate_manifest(manifest)
    except Exception as exc:
        raise ContractError("SUPPLY_MANIFEST_INVALID") from exc
    if manifest["schema"] == SCHEMA_V3:
        source = manifest["release_source_revision"]
        source_tree = manifest["release_source_tree"]
        build_tag = "v3-" + source[:12]
        if manifest["build_run_id"] != build["run_id"] or manifest["build_run_attempt"] != build["run_attempt"]:
            raise ContractError("SUPPLY_BUILD_IDENTITY_MISMATCH")
    else:
        source = manifest["source_revision"]
        source_tree = manifest.get("source_tree", tree)
        build_tag = manifest["build_tag"]
    if source != build["head_sha"] or source_tree != tree:
        raise ContractError("SUPPLY_SOURCE_LINEAGE_MISMATCH")
    return {
        "schema": manifest["schema"],
        "source_revision": source,
        "source_tree": source_tree,
        "build_tag": build_tag,
        "manifest_sha256": _sha(_canonical(manifest)),
        "service_digests": {
            service: manifest["services"][service]["image_digest"] for service in SERVICE_ORDER
        },
    }


def _relay_receipt(value: Any, build: dict[str, Any], supply: dict[str, Any], run_id: int) -> None:
    if not isinstance(value, dict):
        raise ContractError("RELAY_RECEIPT_INVALID")
    if value.get("schema") == "leaf.staging-converged.v1":
        _exact(
            value,
            {"schema", "source_revision", "build_run_attempt", "build_tag", "relay_run_id", "services"},
            "RELAY_RECEIPT_INVALID",
        )
        attempt = value["build_run_attempt"]
        if isinstance(attempt, str) and DECIMAL.fullmatch(attempt):
            attempt = int(attempt)
        if (
            value["source_revision"] != supply["source_revision"]
            or attempt != build["run_attempt"]
            or value["build_tag"] != supply["build_tag"]
            or value["relay_run_id"] != run_id
            or value["services"] != ["web", "app"]
        ):
            raise ContractError("RELAY_RECEIPT_LINEAGE_MISMATCH")
        return
    if value.get("schema") == "leaf.staging-converged.v2":
        required = {
            "schema", "release_source_revision", "build_run_attempt", "relay_run_id",
            "supply_set_sha256", "candidate_supply_set", "automatic_surfaces",
            "surface_results", "non_relay_services", "full_fleet_identity_stamped",
        }
        _exact(value, required, "RELAY_RECEIPT_INVALID")
        if (
            value["release_source_revision"] != supply["source_revision"]
            or value["build_run_attempt"] != build["run_attempt"]
            or value["relay_run_id"] != run_id
            or value["supply_set_sha256"] != supply["manifest_sha256"]
            or _sha(_canonical(value["candidate_supply_set"])) != supply["manifest_sha256"]
            or value["automatic_surfaces"] != ["web", "app"]
            or not isinstance(value["surface_results"], dict)
            or set(value["surface_results"]) != {"web", "app"}
            or value["non_relay_services"]
            != {
                "broker": "not_automatically_reconciled",
                "harness": "not_automatically_reconciled",
                "canonical-worker": "not_automatically_reconciled",
            }
            or value["full_fleet_identity_stamped"] is not False
        ):
            raise ContractError("RELAY_RECEIPT_LINEAGE_MISMATCH")
        return
    raise ContractError("RELAY_RECEIPT_INVALID")


def _relay_children(provider: Provider, relay: dict[str, Any]) -> tuple[dict[str, int], str]:
    raw = provider.bytes(APP_REPOSITORY, f"/actions/runs/{relay['run_id']}/logs")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            text = "\n".join(
                archive.read(info).decode("utf-8")
                for info in archive.infolist()
                if not info.is_dir()
            )
    except (zipfile.BadZipFile, UnicodeDecodeError, OSError) as exc:
        raise ContractError("RELAY_LOGS_INVALID") from exc
    pairs = re.findall(r"Watching (web|app) deploy run ([1-9][0-9]+)\.", text)
    children: dict[str, int] = {}
    for service, run_id in pairs:
        if service in children:
            raise ContractError("RELAY_CHILD_CARDINALITY")
        children[service] = int(run_id)
    if set(children) != {"web", "app"} or len(set(children.values())) != 2:
        raise ContractError("RELAY_CHILD_CARDINALITY")
    return children, _sha(raw)


def _strict_slot(value: Any, reason: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("status") not in {"produced", "not_produced"}:
        raise ContractError(reason)
    expected = {"status", "value"} if value["status"] == "produced" else {"status"}
    return _exact(value, expected, reason)


def _reject_secrets(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if SECRET_KEY.search(str(key)):
                raise ContractError("SECRET_SHAPED_EVIDENCE")
            _reject_secrets(item)
    elif isinstance(value, list):
        for item in value:
            _reject_secrets(item)
    elif isinstance(value, str) and SECRET_VALUE.search(value):
        raise ContractError("SECRET_SHAPED_EVIDENCE")


def _service_receipt(value: Any) -> dict[str, Any]:
    top = {
        "schema", "environment", "provider", "requested", "path", "preflight_result",
        "deploy_result", "terminal_result", "failed_stage", "facts", "receipt_sha256",
    }
    receipt = _exact(value, top, "SERVICE_RECEIPT_INVALID")
    if receipt["schema"] != SERVICE_RECEIPT_SCHEMA or receipt["environment"] != ENVIRONMENT:
        raise ContractError("SERVICE_RECEIPT_INVALID")
    provider = _exact(
        receipt["provider"],
        {"repository", "workflow_path", "workflow_blob", "run_id", "run_attempt", "event", "head_sha"},
        "SERVICE_RECEIPT_PROVIDER_INVALID",
    )
    if (
        provider["repository"] != TF_REPOSITORY
        or provider["workflow_path"] != DEPLOY_WORKFLOW
        or provider["event"] != "workflow_dispatch"
        or not SHA40.fullmatch(str(provider["workflow_blob"]))
        or not SHA40.fullmatch(str(provider["head_sha"]))
    ):
        raise ContractError("SERVICE_RECEIPT_PROVIDER_INVALID")
    _positive(provider["run_id"], "SERVICE_RECEIPT_PROVIDER_INVALID")
    _positive(provider["run_attempt"], "SERVICE_RECEIPT_PROVIDER_INVALID")
    requested_keys = {
        "allow_non_forward_image", "app_deploy_intent", "configuration_delta",
        "configuration_task_definition", "convergence_id", "deploy_strategy",
        "digest_aware_evidence", "digest_aware_reconcile", "expected_task_definition",
        "hold_seconds", "image_tag", "p4a_session_identity_cutover",
        "quarantine_recovery_snapshot_identifier", "required_broker_task_definition",
        "service", "snapshot_overflow_acknowledgement", "source_revision", "start_from_zero",
        "start_from_zero_confirmation", "target_color",
    }
    requested = _exact(receipt["requested"], requested_keys, "SERVICE_RECEIPT_REQUEST_INVALID")
    if requested["service"] not in SERVICE_ORDER:
        raise ContractError("SERVICE_RECEIPT_REQUEST_INVALID")
    failed_stage = _strict_slot(receipt["failed_stage"], "SERVICE_RECEIPT_FAILED_STAGE_INVALID")
    if failed_stage["status"] == "produced":
        failure = _exact(
            failed_stage["value"],
            {"primary", "additional", "unique"},
            "SERVICE_RECEIPT_FAILED_STAGE_INVALID",
        )
        if not isinstance(failure["additional"], list) or not isinstance(failure["unique"], bool):
            raise ContractError("SERVICE_RECEIPT_FAILED_STAGE_INVALID")
        for row in [failure["primary"], *failure["additional"]]:
            row = _exact(row, {"job", "step", "number"}, "SERVICE_RECEIPT_FAILED_STAGE_INVALID")
            if not isinstance(row["job"], str) or not isinstance(row["step"], str) or not isinstance(row["number"], int):
                raise ContractError("SERVICE_RECEIPT_FAILED_STAGE_INVALID")
    facts = receipt["facts"]
    if facts == {"status": "not_produced"}:
        checksum = receipt["receipt_sha256"]
        _sha64(checksum, "SERVICE_RECEIPT_CHECKSUM_INVALID")
        copy = json.loads(json.dumps(receipt))
        copy["receipt_sha256"] = ""
        if _sha(_canonical(copy)) != checksum:
            raise ContractError("SERVICE_RECEIPT_CHECKSUM_INVALID")
        _reject_secrets(receipt)
        return receipt
    if not isinstance(facts, dict) or facts.get("schema") != "leaf.platform-staging-service-facts.v1":
        raise ContractError("SERVICE_RECEIPT_FACTS_INVALID")
    facts_keys = {
        "candidate", "deployment_identity", "marker", "mutation_count", "p4a",
        "predecessor_task_definition", "prior_job_status", "rollback", "route", "schema",
        "service", "source", "supply", "terminal", "writer_census",
    }
    _exact(facts, facts_keys, "SERVICE_RECEIPT_FACTS_INVALID")
    for key in (
        "service", "supply", "predecessor_task_definition", "terminal", "mutation_count",
        "prior_job_status", "rollback", "route", "p4a", "deployment_identity", "marker",
        "writer_census",
    ):
        _strict_slot(facts[key], "SERVICE_RECEIPT_FACTS_INVALID")
    _exact(facts["source"], {"revision", "tree"}, "SERVICE_RECEIPT_FACTS_INVALID")
    source_revision = _strict_slot(facts["source"]["revision"], "SERVICE_RECEIPT_FACTS_INVALID")
    source_tree = _strict_slot(facts["source"]["tree"], "SERVICE_RECEIPT_FACTS_INVALID")
    if source_revision["status"] == "produced":
        _sha40(source_revision["value"], "SERVICE_RECEIPT_FACTS_INVALID")
    if source_tree["status"] == "produced":
        _sha40(source_tree["value"], "SERVICE_RECEIPT_FACTS_INVALID")
    service_slot = facts["service"]
    if service_slot["status"] == "produced" and service_slot["value"] != requested["service"]:
        raise ContractError("SERVICE_RECEIPT_FACTS_INVALID")
    supply_slot = facts["supply"]
    if supply_slot["status"] == "produced":
        supply_value = _exact(
            supply_slot["value"],
            {"artifact_id", "artifact_name", "manifest_sha256", "producer_run_id", "producer_run_attempt"},
            "SERVICE_RECEIPT_FACTS_INVALID",
        )
        _positive(supply_value["artifact_id"], "SERVICE_RECEIPT_FACTS_INVALID")
        _positive(supply_value["producer_run_id"], "SERVICE_RECEIPT_FACTS_INVALID")
        _positive(supply_value["producer_run_attempt"], "SERVICE_RECEIPT_FACTS_INVALID")
        _sha64(supply_value["manifest_sha256"], "SERVICE_RECEIPT_FACTS_INVALID")
        if not isinstance(supply_value["artifact_name"], str):
            raise ContractError("SERVICE_RECEIPT_FACTS_INVALID")
    predecessor = facts["predecessor_task_definition"]
    if predecessor["status"] == "produced" and not isinstance(predecessor["value"], str):
        raise ContractError("SERVICE_RECEIPT_FACTS_INVALID")
    _exact(facts["candidate"], {"task_definition", "image_digest"}, "SERVICE_RECEIPT_FACTS_INVALID")
    candidate_td = _strict_slot(facts["candidate"]["task_definition"], "SERVICE_RECEIPT_FACTS_INVALID")
    candidate_digest = _strict_slot(facts["candidate"]["image_digest"], "SERVICE_RECEIPT_FACTS_INVALID")
    if candidate_td["status"] == "produced" and not isinstance(candidate_td["value"], str):
        raise ContractError("SERVICE_RECEIPT_FACTS_INVALID")
    if candidate_digest["status"] == "produced" and not DIGEST.fullmatch(str(candidate_digest["value"])):
        raise ContractError("SERVICE_RECEIPT_FACTS_INVALID")
    terminal = facts["terminal"]
    if terminal["status"] == "produced":
        terminal_value = _exact(
            terminal["value"],
            {"service", "task_definition", "image_digest", "capacity", "primary_deployments", "stable_1_1_0"},
            "SERVICE_RECEIPT_FACTS_INVALID",
        )
        if not isinstance(terminal_value["service"], str) or not isinstance(terminal_value["task_definition"], str) or not isinstance(terminal_value["stable_1_1_0"], bool):
            raise ContractError("SERVICE_RECEIPT_FACTS_INVALID")
        terminal_digest = _strict_slot(terminal_value["image_digest"], "SERVICE_RECEIPT_FACTS_INVALID")
        if terminal_digest["status"] == "produced" and not DIGEST.fullmatch(str(terminal_digest["value"])):
            raise ContractError("SERVICE_RECEIPT_FACTS_INVALID")
        capacity = _exact(terminal_value["capacity"], {"desired", "running", "pending"}, "SERVICE_RECEIPT_FACTS_INVALID")
        if any(isinstance(capacity[key], bool) or not isinstance(capacity[key], int) for key in capacity):
            raise ContractError("SERVICE_RECEIPT_FACTS_INVALID")
        primary = terminal_value["primary_deployments"]
        if isinstance(primary, list):
            for row in primary:
                row = _exact(row, {"task_definition", "rollout_state", "status"}, "SERVICE_RECEIPT_FACTS_INVALID")
                if not all(isinstance(row[key], str) for key in row):
                    raise ContractError("SERVICE_RECEIPT_FACTS_INVALID")
        else:
            _strict_slot(primary, "SERVICE_RECEIPT_FACTS_INVALID")
    mutation = facts["mutation_count"]
    if mutation["status"] == "produced" and (
        isinstance(mutation["value"], bool) or not isinstance(mutation["value"], int) or mutation["value"] < 0
    ):
        raise ContractError("SERVICE_RECEIPT_FACTS_INVALID")
    identity_slot = facts["deployment_identity"]
    if identity_slot["status"] == "produced":
        identity_value = _exact(identity_slot["value"], {"body", "sha256"}, "SERVICE_RECEIPT_FACTS_INVALID")
        if not isinstance(identity_value["body"], dict):
            raise ContractError("SERVICE_RECEIPT_FACTS_INVALID")
        _sha64(identity_value["sha256"], "SERVICE_RECEIPT_FACTS_INVALID")
    if receipt["path"] not in {"deploy", "digest_aware_skip", "digest_aware_preflight"}:
        raise ContractError("SERVICE_RECEIPT_INVALID")
    if not all(isinstance(receipt[key], str) for key in ("preflight_result", "deploy_result", "terminal_result")):
        raise ContractError("SERVICE_RECEIPT_INVALID")
    checksum = receipt["receipt_sha256"]
    _sha64(checksum, "SERVICE_RECEIPT_CHECKSUM_INVALID")
    copy = json.loads(json.dumps(receipt))
    copy["receipt_sha256"] = ""
    if _sha(_canonical(copy)) != checksum:
        raise ContractError("SERVICE_RECEIPT_CHECKSUM_INVALID")
    _reject_secrets(receipt)
    return receipt


def _failed_steps(provider: Provider, run: dict[str, Any]) -> dict[str, Any]:
    jobs = provider.json(
        TF_REPOSITORY,
        f"/actions/runs/{run['run_id']}/attempts/{run['run_attempt']}/jobs?per_page=100",
    )
    if not isinstance(jobs, dict) or not isinstance(jobs.get("jobs"), list):
        raise ContractError("PROVIDER_JOBS_INVALID")
    if jobs.get("total_count") != len(jobs["jobs"]):
        raise ContractError("PROVIDER_JOBS_PAGINATION_UNPROVEN")
    failed: list[dict[str, Any]] = []
    for job in jobs["jobs"]:
        if not isinstance(job, dict) or not isinstance(job.get("steps"), list):
            raise ContractError("PROVIDER_JOBS_INVALID")
        for step in job["steps"]:
            if isinstance(step, dict) and step.get("conclusion") == "failure":
                row = {"job": job.get("name"), "step": step.get("name"), "number": step.get("number")}
                if not isinstance(row["job"], str) or not isinstance(row["step"], str) or not isinstance(row["number"], int):
                    raise ContractError("PROVIDER_JOBS_INVALID")
                failed.append(row)
    failed.sort(key=lambda row: (row["number"], row["job"], row["step"]))
    return (
        {"status": "produced", "value": {"primary": failed[0], "additional": failed[1:], "unique": len(failed) == 1}}
        if failed
        else {"status": "not_produced"}
    )


def _closed_json_slot(value: dict[str, Any]) -> dict[str, Any]:
    if value["status"] == "not_produced":
        return {"status": "not_produced"}
    raw = _canonical(value["value"])
    return {"status": "produced", "canonical_json": raw.decode("utf-8"), "sha256": _sha(raw)}


def _normalize_child(
    provider: Provider,
    run: dict[str, Any],
    artifact: dict[str, Any],
    receipt: dict[str, Any],
    role: str,
) -> dict[str, Any]:
    rp = receipt["provider"]
    if (
        rp["run_id"] != run["run_id"]
        or rp["run_attempt"] != run["run_attempt"]
        or rp["head_sha"] != run["head_sha"]
        or run["workflow_path"] != DEPLOY_WORKFLOW
        or run["event"] != "workflow_dispatch"
        or receipt["terminal_result"] != run["conclusion"]
    ):
        raise ContractError("SERVICE_RECEIPT_RUN_MISMATCH")
    tree, blob = _workflow_blob(provider, TF_REPOSITORY, run["head_sha"], DEPLOY_WORKFLOW)
    if rp["workflow_blob"] != blob:
        raise ContractError("SERVICE_RECEIPT_WORKFLOW_MISMATCH")
    provider_failed = _failed_steps(provider, run)
    if provider_failed != receipt["failed_stage"]:
        raise ContractError("SERVICE_RECEIPT_FAILED_STAGE_MISMATCH")
    facts = receipt["facts"]
    if facts == {"status": "not_produced"}:
        raise ContractError("SERVICE_RECEIPT_FACTS_NOT_PRODUCED")
    request_raw = _canonical(receipt["requested"])
    source_slot = facts["source"]["revision"]
    normalized_source = (
        source_slot["value"]
        if source_slot["status"] == "produced"
        else receipt["requested"]["source_revision"]
    )
    return {
        "role": role,
        "provider": {
            "repository": TF_REPOSITORY,
            "workflow_path": DEPLOY_WORKFLOW,
            "workflow_blob": blob,
            "source_tree": tree,
            "run_id": run["run_id"],
            "run_attempt": run["run_attempt"],
            "event": run["event"],
            "head_sha": run["head_sha"],
            "conclusion": run["conclusion"],
            "created_at": run["created_at"],
            "updated_at": run["updated_at"],
        },
        "artifact": artifact,
        "service_receipt_sha256": receipt["receipt_sha256"],
        "requested_sha256": _sha(request_raw),
        "service": receipt["requested"]["service"],
        "source_revision": normalized_source,
        "source_revision_evidence": facts["source"]["revision"],
        "source_tree": facts["source"]["tree"],
        "image_tag": receipt["requested"]["image_tag"],
        "app_deploy_intent": receipt["requested"]["app_deploy_intent"],
        "path": receipt["path"],
        "preflight_result": receipt["preflight_result"],
        "deploy_result": receipt["deploy_result"],
        "terminal_result": receipt["terminal_result"],
        "failed_stage": receipt["failed_stage"],
        "facts_sha256": _sha(_canonical(facts)),
        "supply_evidence": facts["supply"],
        "predecessor_task_definition": facts["predecessor_task_definition"],
        "candidate": facts["candidate"],
        "terminal": facts["terminal"],
        "mutation_count": facts["mutation_count"],
        "rollback": _closed_json_slot(facts["rollback"]),
        "posture": {
            key: _closed_json_slot(facts[source])
            for key, source in (
                ("route", "route"), ("p4a", "p4a"), ("marker", "marker"),
                ("writer_census", "writer_census"),
            )
        },
        "deployment_identity": facts["deployment_identity"],
    }


def _receipt_matches_supply(receipt: dict[str, Any], supply: dict[str, Any]) -> bool:
    requested = receipt["requested"]
    facts = receipt["facts"]
    if facts == {"status": "not_produced"}:
        return requested["source_revision"] == supply["source_revision"]
    source = facts["source"]["revision"]
    source_value = source.get("value") if source["status"] == "produced" else requested["source_revision"]
    return source_value == supply["source_revision"] and requested["service"] in SERVICE_ORDER


def _terminal_digest(child: dict[str, Any]) -> str:
    terminal = child["terminal"]
    if terminal.get("status") != "produced" or not isinstance(terminal.get("value"), dict):
        raise ContractError("SERVICE_TERMINAL_NOT_PRODUCED")
    value = terminal["value"]
    if set(value) != {"service", "task_definition", "image_digest", "capacity", "primary_deployments", "stable_1_1_0"}:
        raise ContractError("SERVICE_TERMINAL_INVALID")
    image = value["image_digest"]
    if not isinstance(image, dict) or image.get("status") != "produced" or not DIGEST.fullmatch(str(image.get("value", ""))):
        raise ContractError("SERVICE_TERMINAL_INVALID")
    capacity = value["capacity"]
    if capacity != {"desired": 1, "running": 1, "pending": 0} or value["stable_1_1_0"] is not True:
        raise ContractError("SERVICE_TERMINAL_UNHEALTHY")
    primary = value["primary_deployments"]
    if (
        not isinstance(primary, list)
        or len(primary) != 1
        or not isinstance(primary[0], dict)
        or set(primary[0]) != {"task_definition", "rollout_state", "status"}
        or primary[0].get("status") != "PRIMARY"
        or primary[0].get("rollout_state") != "COMPLETED"
        or primary[0].get("task_definition") != value["task_definition"]
    ):
        raise ContractError("SERVICE_TERMINAL_UNHEALTHY")
    return image["value"]


def _identity(child: dict[str, Any], supply: dict[str, Any]) -> dict[str, Any]:
    slot = child["deployment_identity"]
    if not isinstance(slot, dict) or slot.get("status") != "produced" or set(slot) != {"status", "value"}:
        raise ContractError("DEPLOYMENT_IDENTITY_NOT_PRODUCED")
    value = slot["value"]
    if not isinstance(value, dict) or set(value) != {"body", "sha256"}:
        raise ContractError("DEPLOYMENT_IDENTITY_INVALID")
    body = value["body"]
    if (
        not isinstance(body, dict)
        or set(body) != {"schema", "environment", "source_revision", "services"}
        or body["schema"] != "leaf.deployment-identity.v1"
        or body["environment"] != ENVIRONMENT
        or body["source_revision"] != supply["source_revision"]
        or not isinstance(body["services"], dict)
        or set(body["services"]) != set(SERVICE_ORDER)
    ):
        raise ContractError("DEPLOYMENT_IDENTITY_INVALID")
    for service in SERVICE_ORDER:
        row = body["services"][service]
        if (
            not isinstance(row, dict)
            or set(row) != {"image_digest", "source_revision"}
            or row["image_digest"] != supply["service_digests"][service]
            or row["source_revision"] != supply["source_revision"]
        ):
            raise ContractError("DEPLOYMENT_IDENTITY_MISMATCH")
    producer_body = {
        "schema": body["schema"],
        "environment": body["environment"],
        "source_revision": body["source_revision"],
        "services": {
            service: {
                "image_digest": body["services"][service]["image_digest"],
                "source_revision": body["services"][service]["source_revision"],
            }
            for service in SERVICE_ORDER
        },
    }
    raw = (json.dumps(producer_body, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if value["sha256"] != _sha(raw):
        raise ContractError("DEPLOYMENT_IDENTITY_CHECKSUM_INVALID")
    return {"body": body, "body_sha256": value["sha256"], "producer_run_id": child["provider"]["run_id"]}


def _build_receipt(provider: Provider, producer_build_run_id: int, current_frontier_run_id: int | None) -> dict[str, Any]:
    raw_build = provider.json(APP_REPOSITORY, f"/actions/runs/{producer_build_run_id}")
    build = _provider_run(raw_build, APP_REPOSITORY, BUILD_WORKFLOW, "push")
    if build["run_id"] != producer_build_run_id or build["head_branch"] != "main" or build["conclusion"] != "success":
        raise ContractError("BUILD_RUN_MISMATCH")
    build_tree, build_blob = _workflow_blob(provider, APP_REPOSITORY, build["head_sha"], BUILD_WORKFLOW)
    branch = provider.json(APP_REPOSITORY, "/branches/main")
    if (
        not isinstance(branch, dict)
        or not isinstance(branch.get("commit"), dict)
        or branch["commit"].get("sha") != build["head_sha"]
    ):
        raise ContractError("ARRIVAL_SOURCE_IS_NOT_CURRENT_MAIN")
    supply_name = f"staging-supply-set-{build['head_sha']}-attempt-{build['run_attempt']}"
    supply_artifact, manifest = _one_artifact(
        provider, APP_REPOSITORY, build["run_id"], supply_name, "staging-supply-set.json"
    )
    supply = _supply(manifest, build, build_tree)

    relay_matches: list[tuple[dict[str, Any], dict[str, Any], Any]] = []
    relay_name = f"staging-converged-{build['head_sha']}-attempt-{build['run_attempt']}"
    relay_rows = _workflow_run_rows(
        provider,
        APP_REPOSITORY,
        "dispatch-staging-deploys.yml",
        {
            "event": "workflow_run",
            "status": "completed",
            "head_sha": build["head_sha"],
            "per_page": 100,
        },
    )
    for raw in relay_rows:
        try:
            relay = _provider_run(raw, APP_REPOSITORY, RELAY_WORKFLOW, "workflow_run")
            if relay["head_sha"] != build["head_sha"] or relay["conclusion"] != "success":
                continue
            artifact, value = _one_artifact(
                provider, APP_REPOSITORY, relay["run_id"], relay_name, "staging-converged.json"
            )
            _relay_receipt(value, build, supply, relay["run_id"])
            relay_matches.append((relay, artifact, value))
        except ContractError as exc:
            if exc.reason not in {"PROVIDER_ARTIFACT_CARDINALITY"}:
                raise
    if len(relay_matches) != 1:
        raise ContractError("RELAY_RUN_CARDINALITY")
    relay, relay_artifact, _ = relay_matches[0]
    relay_tree, relay_blob = _workflow_blob(provider, APP_REPOSITORY, relay["head_sha"], RELAY_WORKFLOW)
    if relay_tree != build_tree:
        raise ContractError("RELAY_SOURCE_TREE_MISMATCH")
    relay_children, relay_logs_sha = _relay_children(provider, relay)

    frontier: dict[str, Any] | None = None
    if current_frontier_run_id is not None:
        frontier_raw = provider.json(TF_REPOSITORY, f"/actions/runs/{current_frontier_run_id}")
        frontier = _provider_run(frontier_raw, TF_REPOSITORY, DEPLOY_WORKFLOW, "workflow_dispatch")
        if frontier["run_id"] != current_frontier_run_id:
            raise ContractError("FRONTIER_RUN_MISMATCH")
    child_rows = _workflow_run_rows(
        provider,
        TF_REPOSITORY,
        "deploy-leaf-platform-staging.yml",
        {"event": "workflow_dispatch", "per_page": 100},
    )
    runs: dict[int, dict[str, Any]] = {}
    for raw in child_rows:
        if (
            isinstance(raw, dict)
            and raw.get("status") != "completed"
            and isinstance(raw.get("created_at"), str)
            and relay["created_at"] <= raw["created_at"]
        ):
            raise ContractError("ACTIVE_CHILD_RUN")
        if not isinstance(raw, dict) or raw.get("status") != "completed":
            continue
        run = _provider_run(raw, TF_REPOSITORY, DEPLOY_WORKFLOW, "workflow_dispatch")
        in_window = relay["created_at"] <= run["created_at"]
        if frontier is not None:
            in_window = in_window and run["created_at"] <= frontier["updated_at"]
        if in_window:
            if run["run_id"] in runs:
                raise ContractError("CHILD_RUN_DUPLICATE")
            runs[run["run_id"]] = run
    required_ids = set(relay_children.values())
    if current_frontier_run_id is not None:
        required_ids.add(current_frontier_run_id)
    if not required_ids.issubset(runs):
        raise ContractError("CHILD_RUN_WINDOW_MISMATCH")

    matching: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for run in runs.values():
        name = f"leaf-platform-staging-service-run-{run['run_id']}-attempt-{run['run_attempt']}"
        try:
            artifact, raw_receipt = _one_artifact(provider, TF_REPOSITORY, run["run_id"], name, ARTIFACT_FILE)
        except ContractError as exc:
            if run["run_id"] in required_ids:
                raise ContractError("UNCONFIGURED_CHILD_RECEIPTS") from exc
            if exc.reason == "PROVIDER_ARTIFACT_CARDINALITY":
                continue
            raise
        receipt = _service_receipt(raw_receipt)
        if _receipt_matches_supply(receipt, supply):
            matching.append((run, artifact, receipt))

    by_id = {run["run_id"]: (run, artifact, receipt) for run, artifact, receipt in matching}
    if not required_ids.issubset(by_id):
        raise ContractError("UNCONFIGURED_CHILD_RECEIPTS")
    if current_frontier_run_id is None:
        identity_candidates = [
            run["run_id"]
            for run, _, receipt in matching
            if run["run_id"] != relay_children["app"]
            and receipt["requested"]["service"] == "app"
            and isinstance(receipt["facts"], dict)
            and receipt["facts"].get("schema") == "leaf.platform-staging-service-facts.v1"
            and receipt["facts"]["deployment_identity"].get("status") == "produced"
        ]
        if len(identity_candidates) != 1:
            raise ContractError("FRONTIER_RUN_CARDINALITY")
        current_frontier_run_id = identity_candidates[0]
        frontier = runs[current_frontier_run_id]
    if (
        current_frontier_run_id not in by_id
        or frontier is None
        or current_frontier_run_id in set(relay_children.values())
    ):
        raise ContractError("UNCONFIGURED_FRONTIER_RECEIPT")
    roles: dict[int, str] = {
        relay_children["web"]: "relay_web",
        relay_children["app"]: "relay_app",
        current_frontier_run_id: "frontier_app_identity",
    }
    for service in SERVICE_ORDER:
        candidates = [
            (run["run_id"], receipt)
            for run, _, receipt in matching
            if receipt["requested"]["service"] == service and run["run_id"] not in roles
        ]
        successes = [
            run_id
            for run_id, receipt in candidates
            if receipt["terminal_result"] == "success"
        ]
        failures = [
            run_id
            for run_id, receipt in candidates
            if receipt["terminal_result"] != "success"
        ]
        if service in {"broker", "harness", "canonical-worker"}:
            if len(successes) != 1:
                raise ContractError("SERVICE_TERMINAL_CARDINALITY")
            roles[successes[0]] = f"terminal_{service.replace('-', '_')}"
        elif successes:
            raise ContractError("UNCLASSIFIED_MATCHING_CHILD")
        if len(failures) > 1:
            raise ContractError("SERVICE_FAILURE_CARDINALITY")
        if failures:
            roles[failures[0]] = f"prior_failed_{service.replace('-', '_')}"
    if set(roles) != set(by_id):
        raise ContractError("UNCLASSIFIED_MATCHING_CHILD")

    children: dict[str, dict[str, Any]] = {}
    per_service: dict[str, list[dict[str, Any]]] = {service: [] for service in SERVICE_ORDER}
    for run_id, role in roles.items():
        run, artifact, raw_receipt = by_id[run_id]
        child = _normalize_child(provider, run, artifact, raw_receipt, role)
        children[role] = child
        per_service[child["service"]].append(child)
        if child["source_revision_evidence"] != {
            "status": "produced",
            "value": supply["source_revision"],
        }:
            raise ContractError("SERVICE_SOURCE_REVISION_MISMATCH")
        source_tree = child["source_tree"]
        if source_tree != {"status": "produced", "value": supply["source_tree"]}:
            raise ContractError("SERVICE_SOURCE_TREE_MISMATCH")
        child_supply = child["supply_evidence"]
        expected_supply = {
            "status": "produced",
            "value": {
                "artifact_id": supply_artifact["artifact_id"],
                "artifact_name": supply_artifact["artifact_name"],
                "manifest_sha256": supply["manifest_sha256"],
                "producer_run_id": build["run_id"],
                "producer_run_attempt": build["run_attempt"],
            },
        }
        if child_supply != expected_supply:
            raise ContractError("SERVICE_SUPPLY_EVIDENCE_MISMATCH")
    if children["relay_web"]["service"] != "web" or children["relay_app"]["service"] != "app" or children["frontier_app_identity"]["service"] != "app":
        raise ContractError("CHILD_ROLE_SERVICE_MISMATCH")
    for service, attempts in per_service.items():
        attempts.sort(key=lambda item: (item["provider"]["created_at"], item["provider"]["run_id"]))
        for previous, current in zip(attempts, attempts[1:]):
            prior_terminal = previous["terminal"]
            predecessor = current["predecessor_task_definition"]
            if predecessor.get("status") != "produced":
                raise ContractError("SERVICE_PREDECESSOR_CHAIN_MISMATCH")
            if prior_terminal.get("status") == "produced":
                expected_predecessor = prior_terminal["value"].get("task_definition")
            else:
                previous_predecessor = previous["predecessor_task_definition"]
                if (
                    previous["mutation_count"] != {"status": "produced", "value": 0}
                    or previous_predecessor.get("status") != "produced"
                ):
                    raise ContractError("SERVICE_PREDECESSOR_CHAIN_MISMATCH")
                expected_predecessor = previous_predecessor["value"]
            if (
                predecessor.get("value") != expected_predecessor
            ):
                raise ContractError("SERVICE_PREDECESSOR_CHAIN_MISMATCH")

    selected = {
        "app": children["frontier_app_identity"],
        "web": children["relay_web"],
        "broker": children["terminal_broker"],
        "harness": children["terminal_harness"],
        "canonical-worker": children["terminal_canonical_worker"],
    }
    for service, child in selected.items():
        if child["terminal_result"] != "success" or child["failed_stage"] != {"status": "not_produced"}:
            raise ContractError("SERVICE_TERMINAL_RESULT_INVALID")
        candidate_digest = child["candidate"]["image_digest"]
        if (
            candidate_digest.get("status") != "produced"
            or candidate_digest.get("value") != supply["service_digests"][service]
        ):
            raise ContractError("SERVICE_CANDIDATE_DIGEST_MISMATCH")
        if child["mutation_count"].get("status") != "produced":
            raise ContractError("SERVICE_MUTATION_COUNT_NOT_PRODUCED")
        if _terminal_digest(child) != supply["service_digests"][service]:
            raise ContractError("SERVICE_DIGEST_MISMATCH")
    identity = _identity(selected["app"], supply)

    service_output: dict[str, Any] = {}
    for service in SERVICE_ORDER:
        attempts = per_service[service]
        service_output[service] = {
            "attempts": attempts,
            "selected_run_id": selected[service]["provider"]["run_id"],
            "terminal_image_digest": supply["service_digests"][service],
        }
    receipt: dict[str, Any] = {
        "schema": OUTPUT_SCHEMA,
        "environment": ENVIRONMENT,
        "finalizer": {
            "repository": APP_REPOSITORY,
            "workflow_path": ".github/workflows/finalize-platform-staging-convergence.yml",
        },
        "producer": {
            **{key: build[key] for key in ("repository", "workflow_path", "run_id", "run_attempt", "event", "head_sha", "conclusion")},
            "workflow_blob": build_blob,
            "source_tree": build_tree,
        },
        "supply": {**supply_artifact, **supply},
        "relay": {
            **{key: relay[key] for key in ("repository", "workflow_path", "run_id", "run_attempt", "event", "head_sha", "conclusion")},
            "workflow_blob": relay_blob,
            "artifact": relay_artifact,
            "logs_sha256": relay_logs_sha,
            "children": relay_children,
        },
        "frontier_run_id": current_frontier_run_id,
        "services": service_output,
        "deployment_identity": identity,
        "operational_posture": {
            key: selected["app"]["posture"][key]
            for key in ("route", "p4a", "marker", "writer_census")
        },
        "terminal_complete": True,
        "evidence_sha256": "",
    }
    _reject_secrets(receipt)
    receipt["evidence_sha256"] = _sha(_canonical(receipt))
    return receipt


def _parse_run_id(value: str | None, required: bool) -> int | None:
    if value is None and not required:
        return None
    if value is None or not DECIMAL.fullmatch(value):
        raise ContractError("RUN_ID_INVALID")
    return int(value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--producer-build-run-id", required=True)
    parser.add_argument("--current-frontier-run-id")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        build_run_id = _parse_run_id(args.producer_build_run_id, True)
        frontier_run_id = _parse_run_id(args.current_frontier_run_id, False)
        provider = GitHubProvider(
            os.environ.get("APP_GITHUB_TOKEN", ""),
            os.environ.get("TERRAFORM_GITHUB_TOKEN", ""),
        )
        assert build_run_id is not None
        receipt = _build_receipt(provider, build_run_id, frontier_run_id)
        args.output.write_bytes(_canonical(receipt) + b"\n")
        print(f"Prepared {OUTPUT_SCHEMA}; provider evidence body withheld.")
        return 0
    except ContractError as exc:
        print(f"ERROR:{exc.reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
