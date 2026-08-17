#!/usr/bin/env python3
"""Build one provider-associated, read-only staging convergence receipt.

The production CLI accepts only GitHub run identifiers. All release authority is
discovered again from provider records and immutable artifacts. This module does
not dispatch workflows, call AWS, or accept a caller-built evidence bundle.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Protocol
import unicodedata
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
CONSUMER_CONTRACT_WORKFLOW = (
    ".github/workflows/publish-leaf-platform-staging-consumer-contract.yml"
)
CONSUMER_CONTRACT_SCHEMA_PATH = (
    "contract/leaf-platform-staging-consumer-contract.v1.schema.json"
)
CONSUMER_CONTRACT_SCHEMA = "leaf.platform-staging-consumer-contract.v1"
CONSUMER_DISPATCH_SCHEMA = "leaf.platform-staging-consumer-contract-dispatch.v1"
CONSUMER_CONTRACT_FILE = "consumer-contract.json"
CONSUMER_CONTRACT_ARTIFACT_PREFIX = (
    "leaf-platform-staging-consumer-contract-run-"
)
CONSUMER_CONTRACT_VERSION = 1
CONSUMER_DIGEST_MARKER = "leaf.staging-digest-aware-consumer.v1"
CONSUMER_MUTATION_GROUP = "leaf-platform-staging-ecs-mutation"
CONSUMER_DEPLOYMENT_ENVIRONMENT = "aws-apply"
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
SERVICE_RESULT_LABELS = {"success", "failure", "cancelled", "skipped"}
SUPPORTED_CHILD_CONCLUSIONS = {"success", "failure"}
PREFLIGHT_STATES = {"not_required", "passed", "failed"}
MUTATION_STATES = {"not_started", "candidate_active", "predecessor_restored"}
DEPLOYMENT_OUTCOMES = {
    "succeeded",
    "skipped_exact",
    "failed_before_mutation",
    "failed_after_mutation_rolled_back",
}
ROLLBACK_OUTCOMES = {"not_required", "succeeded"}
RECEIPT_OUTCOMES = {"verified"}
FAILED_STAGE_CODES = {"none", "input_resolution", "service_promotion"}
TERMINAL_REASON_CODES = {
    "exact_candidate_healthy",
    "exact_skip_healthy",
    "pre_mutation_failure",
    "post_mutation_predecessor_restored",
}
FAILED_STAGE_ALLOWLIST = {
    SERVICE_RECEIPT_SCHEMA: {
        ("Deploy", "Resolve inputs"): "input_resolution",
        ("deploy", "Resolve inputs"): "input_resolution",
        ("Deploy", "Resolve and validate deployment inputs"): "input_resolution",
        ("deploy", "Resolve and validate deployment inputs"): "input_resolution",
        ("Deploy", "Resolve reviewed live deployment identity"): "input_resolution",
        ("deploy", "Resolve reviewed live deployment identity"): "input_resolution",
        ("Deploy", "Promote one ECS service and verify task health"): "service_promotion",
        ("deploy", "Promote one ECS service and verify task health"): "service_promotion",
    }
}
ROLLBACK_STEP_RESULTS = {"success", "failure", "cancelled", "skipped"}
ROLLBACK_DETAIL_CODES = {
    "not_produced",
    "started",
    "old-color-ready-before-backedge-restore",
    "backedge-restored-data-plane-unproved",
    "backedge-restored-old-color-proved",
    "baseline-confirmed-candidate-scaled",
    "candidate-kept-live-old-color-drained",
    "old-color-ready-before-listener-restore",
    "restore-authorized",
    "main-listener-restored",
    "main-listener-restored-backedge-budget-refused",
    "main-listener-restored-backedge-drift-refused",
    "listener-weights-restored-data-plane-unproved",
    "restored-old-color-proved",
    "restored-web-public-identity-ambiguous-candidate-kept",
    "complete-candidate-scaled",
}
ROLLBACK_AUTHORITY_CODES = {
    "not_produced",
    "not-required",
    "restored",
    "restored-digest-pinned-revision",
}
MAX_SOURCE_STRING_BYTES = 4096
MAX_OUTPUT_STRING_BYTES = 512
MAX_STRUCTURED_EVIDENCE_BYTES = 32768
MAX_RECEIPT_BYTES = 262144
MAX_PROVIDER_JSON_BYTES = 4 * 1024 * 1024
MAX_PROVIDER_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_PROVIDER_LOG_BYTES = 16 * 1024 * 1024
MAX_PROVIDER_READ_ATTEMPTS = 3
MAX_PROVIDER_RETRY_DELAY_SECONDS = 30
MAX_PROVIDER_RUN_SNAPSHOT_SCANS = 3
VERIFIER_ONLY_MAIN_DRIFT_PATHS = frozenset(
    {
        "contract/platform-staging-convergence.v1.schema.json",
        "scripts/platform_staging_convergence.py",
        "scripts/test_platform_staging_convergence.py",
    }
)
SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA64 = re.compile(r"^[0-9a-f]{64}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
DECIMAL = re.compile(r"^[1-9][0-9]*$")
RFC3339_UTC = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z$")
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
        opener = urllib.request.build_opener(_ArtifactRedirectHandler())
        for attempt in range(1, MAX_PROVIDER_READ_ATTEMPTS + 1):
            try:
                with opener.open(request, timeout=45) as response:
                    raw = response.read(MAX_PROVIDER_ARCHIVE_BYTES + 1)
                    if len(raw) > MAX_PROVIDER_ARCHIVE_BYTES:
                        raise ContractError("PROVIDER_BODY_TOO_LARGE")
                    return raw
            except urllib.error.HTTPError as exc:
                retry_after = _retry_after_seconds(exc.headers.get("Retry-After"))
                transient = exc.code == 429 or 500 <= exc.code <= 599 or (exc.code == 403 and retry_after is not None)
                if not transient or attempt == MAX_PROVIDER_READ_ATTEMPTS:
                    raise ContractError("PROVIDER_READ_FAILED") from exc
                time.sleep(retry_after if retry_after is not None else 2 ** (attempt - 1))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt == MAX_PROVIDER_READ_ATTEMPTS:
                    raise ContractError("PROVIDER_READ_FAILED") from exc
                time.sleep(2 ** (attempt - 1))
        raise AssertionError("provider retry loop must return or raise")

    def json(self, repository: str, endpoint: str) -> Any:
        try:
            raw = self._request(repository, endpoint)
            if len(raw) > MAX_PROVIDER_JSON_BYTES:
                raise ContractError("PROVIDER_JSON_TOO_LARGE")
            return _load_json(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ContractError("PROVIDER_JSON_INVALID") from exc

    def bytes(self, repository: str, endpoint: str) -> bytes:
        return self._request(repository, endpoint)


class _ArtifactRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep GitHub credentials off provider artifact storage redirects."""

    def redirect_request(self, request: Any, file_pointer: Any, code: int, message: str, headers: Any, new_url: str) -> Any:
        redirected = super().redirect_request(request, file_pointer, code, message, headers, new_url)
        if redirected is not None and urllib.parse.urlparse(new_url).hostname != urllib.parse.urlparse(request.full_url).hostname:
            redirected.remove_header("Authorization")
        return redirected


def _retry_after_seconds(value: str | None) -> int | None:
    if value is None or not value.isascii() or not value.isdecimal():
        return None
    return min(int(value), MAX_PROVIDER_RETRY_DELAY_SECONDS)


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


def _has_unsafe_control(value: str) -> bool:
    return any(unicodedata.category(char).startswith("C") for char in value)


def _bounded_text(value: Any, reason: str, *, maximum: int = MAX_SOURCE_STRING_BYTES) -> str:
    if isinstance(value, str) and SECRET_VALUE.search(value):
        raise ContractError("SECRET_SHAPED_EVIDENCE")
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > maximum
        or _has_unsafe_control(value)
    ):
        raise ContractError(reason)
    return value


def _bounded_tree(value: Any, reason: str, *, maximum: int = MAX_STRUCTURED_EVIDENCE_BYTES) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _bounded_text(key, reason, maximum=128)
            if SECRET_KEY.search(key):
                raise ContractError("SECRET_SHAPED_EVIDENCE")
            _bounded_tree(item, reason, maximum=maximum)
    elif isinstance(value, list):
        for item in value:
            _bounded_tree(item, reason, maximum=maximum)
    elif isinstance(value, str):
        _bounded_text(value, reason)
    elif value is not None and not isinstance(value, (bool, int)):
        raise ContractError(reason)
    if len(_canonical(value)) > maximum:
        raise ContractError(reason)


def _validate_output(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _bounded_text(key, "OUTPUT_STRING_INVALID", maximum=128)
            if SECRET_KEY.search(key):
                raise ContractError("SECRET_SHAPED_EVIDENCE")
            _validate_output(item)
    elif isinstance(value, list):
        for item in value:
            _validate_output(item)
    elif isinstance(value, str):
        _bounded_text(value, "OUTPUT_STRING_INVALID", maximum=MAX_OUTPUT_STRING_BYTES)
    elif value is not None and not isinstance(value, (bool, int)):
        raise ContractError("OUTPUT_VALUE_INVALID")


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
    for key in ("repository", "workflow_path", "event", "head_branch", "status", "conclusion", "created_at", "updated_at"):
        _bounded_text(run[key], "PROVIDER_RUN_TEXT_INVALID", maximum=512)
    if not RFC3339_UTC.fullmatch(run["created_at"]) or not RFC3339_UTC.fullmatch(run["updated_at"]):
        raise ContractError("PROVIDER_RUN_TIME_INVALID")
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


def _verify_arrival_source(provider: Provider, build_sha: str) -> None:
    branch = provider.json(APP_REPOSITORY, "/branches/main")
    if not isinstance(branch, dict) or not isinstance(branch.get("commit"), dict):
        raise ContractError("ARRIVAL_SOURCE_IS_NOT_CURRENT_MAIN")
    current_sha = _sha40(branch["commit"].get("sha"), "ARRIVAL_SOURCE_IS_NOT_CURRENT_MAIN")
    if current_sha == build_sha:
        return
    comparison = provider.json(APP_REPOSITORY, f"/compare/{build_sha}...{current_sha}")
    if (
        not isinstance(comparison, dict)
        or not isinstance(comparison.get("files"), list)
        or not isinstance(comparison.get("commits"), list)
    ):
        raise ContractError("ARRIVAL_SOURCE_IS_NOT_CURRENT_MAIN")
    base = comparison.get("base_commit")
    merge_base = comparison.get("merge_base_commit")
    commits = comparison["commits"]
    ahead_by = comparison.get("ahead_by")
    behind_by = comparison.get("behind_by")
    total_commits = comparison.get("total_commits")
    if (
        comparison.get("status") != "ahead"
        or isinstance(ahead_by, bool)
        or not isinstance(ahead_by, int)
        or ahead_by < 1
        or isinstance(behind_by, bool)
        or behind_by != 0
        or isinstance(total_commits, bool)
        or total_commits != ahead_by
        or len(commits) != ahead_by
        or not isinstance(base, dict)
        or base.get("sha") != build_sha
        or not isinstance(merge_base, dict)
        or merge_base.get("sha") != build_sha
    ):
        raise ContractError("ARRIVAL_SOURCE_IS_NOT_CURRENT_MAIN")
    commit_shas: list[str] = []
    for row in commits:
        if not isinstance(row, dict):
            raise ContractError("ARRIVAL_SOURCE_IS_NOT_CURRENT_MAIN")
        commit_sha = _sha40(row.get("sha"), "ARRIVAL_SOURCE_IS_NOT_CURRENT_MAIN")
        if commit_sha in commit_shas:
            raise ContractError("ARRIVAL_SOURCE_IS_NOT_CURRENT_MAIN")
        commit_shas.append(commit_sha)
    if commit_shas[-1] != current_sha:
        raise ContractError("ARRIVAL_SOURCE_IS_NOT_CURRENT_MAIN")
    changed_paths: set[str] = set()
    for row in comparison["files"]:
        if not isinstance(row, dict):
            raise ContractError("ARRIVAL_SOURCE_IS_NOT_CURRENT_MAIN")
        path = row.get("filename")
        if (
            not isinstance(path, str)
            or path not in VERIFIER_ONLY_MAIN_DRIFT_PATHS
            or path in changed_paths
            or row.get("status") != "modified"
        ):
            raise ContractError("ARRIVAL_SOURCE_IS_NOT_CURRENT_MAIN")
        changed_paths.add(path)
    if not changed_paths:
        raise ContractError("ARRIVAL_SOURCE_IS_NOT_CURRENT_MAIN")


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
    previous: tuple[list[dict[str, Any]], tuple[int, ...], int] | None = None
    minimum_total = 0
    for _scan in range(MAX_PROVIDER_RUN_SNAPSHOT_SCANS):
        rows, total, growth = _workflow_run_snapshot(provider, repository, base, minimum_total)
        if growth is not None:
            minimum_total = growth
            continue
        run_ids = tuple(row["id"] for row in rows)
        if previous is None:
            previous = (rows, run_ids, total)
            minimum_total = total
            continue
        _, previous_ids, previous_total = previous
        if total < previous_total:
            raise ContractError("PROVIDER_RUN_LIST_DRIFT")
        if total == previous_total:
            if run_ids != previous_ids:
                raise ContractError("PROVIDER_RUN_LIST_DRIFT")
            return rows
        added = total - previous_total
        if run_ids[added:] != previous_ids:
            raise ContractError("PROVIDER_RUN_LIST_DRIFT")
        return rows
    raise ContractError("PROVIDER_RUN_LIST_DRIFT")


def _workflow_run_snapshot(
    provider: Provider,
    repository: str,
    base: str,
    minimum_total: int,
) -> tuple[list[dict[str, Any]], int, int | None]:
    page = 1
    output: list[dict[str, Any]] = []
    total: int | None = None
    seen: set[int] = set()
    while total is None or len(output) < total:
        endpoint = base if page == 1 else f"{base}&page={page}"
        raw = provider.json(repository, endpoint)
        if not isinstance(raw, dict) or not isinstance(raw.get("workflow_runs"), list):
            raise ContractError("PROVIDER_RUN_LIST_INVALID")
        current_total = raw.get("total_count")
        if isinstance(current_total, bool) or not isinstance(current_total, int) or current_total < 0:
            raise ContractError("PROVIDER_RUN_LIST_INVALID")
        if total is None:
            total = current_total
            if total < minimum_total:
                raise ContractError("PROVIDER_RUN_LIST_DRIFT")
        elif current_total > total:
            return [], total, current_total
        elif current_total < total:
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
    return output, total, None


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
    if len(zip_raw) > MAX_PROVIDER_ARCHIVE_BYTES:
        raise ContractError("PROVIDER_ARCHIVE_TOO_LARGE")
    payload = _zip_member(zip_raw, filename)
    if len(payload) > MAX_PROVIDER_JSON_BYTES:
        raise ContractError("PROVIDER_ARTIFACT_JSON_TOO_LARGE")
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


def _consumer_contract_for_head(provider: Provider, head_sha: str) -> dict[str, Any]:
    """Resolve one provider-owned Terraform consumer contract using Actions only."""
    rows = _workflow_run_rows(
        provider,
        TF_REPOSITORY,
        "publish-leaf-platform-staging-consumer-contract.yml",
        {"branch": "main", "event": "push", "status": "success", "per_page": 100},
    )
    matches: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict) or raw.get("head_sha") != head_sha:
            continue
        run = _provider_run(
            raw,
            TF_REPOSITORY,
            CONSUMER_CONTRACT_WORKFLOW,
            "push",
        )
        if run["head_branch"] != "main" or run["conclusion"] != "success":
            raise ContractError("CONSUMER_CONTRACT_RUN_INVALID")
        matches.append(run)
    if len(matches) != 1:
        raise ContractError("CONSUMER_CONTRACT_RUN_CARDINALITY")
    run = matches[0]
    name = (
        f"{CONSUMER_CONTRACT_ARTIFACT_PREFIX}{run['run_id']}"
        f"-attempt-{run['run_attempt']}"
    )
    artifacts = [
        row
        for row in _artifact_rows(provider, TF_REPOSITORY, run["run_id"])
        if row.get("name") == name and row.get("expired") is False
    ]
    if len(artifacts) != 1:
        raise ContractError("CONSUMER_CONTRACT_ARTIFACT_CARDINALITY")
    artifact = artifacts[0]
    workflow_run = artifact.get("workflow_run")
    if (
        not isinstance(workflow_run, dict)
        or workflow_run.get("id") != run["run_id"]
        or workflow_run.get("head_sha") != head_sha
        or workflow_run.get("head_branch") != "main"
    ):
        raise ContractError("CONSUMER_CONTRACT_ARTIFACT_ASSOCIATION")
    artifact_id = _positive(
        artifact.get("id"), "CONSUMER_CONTRACT_ARTIFACT_INVALID"
    )
    provider_digest = artifact.get("digest")
    if (
        not isinstance(provider_digest, str)
        or not provider_digest.startswith("sha256:")
    ):
        raise ContractError("CONSUMER_CONTRACT_ARTIFACT_DIGEST_INVALID")
    provider_sha = _sha64(
        provider_digest.removeprefix("sha256:"),
        "CONSUMER_CONTRACT_ARTIFACT_DIGEST_INVALID",
    )
    zip_raw = provider.bytes(
        TF_REPOSITORY, f"/actions/artifacts/{artifact_id}/zip"
    )
    archive_sha = _sha(zip_raw)
    if archive_sha != provider_sha:
        raise ContractError("CONSUMER_CONTRACT_ARTIFACT_HASH_MISMATCH")
    raw_contract = _zip_member(zip_raw, CONSUMER_CONTRACT_FILE)
    try:
        contract = _load_json(raw_contract)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ContractError("CONSUMER_CONTRACT_JSON_INVALID") from exc
    contract = _exact(
        contract,
        {"artifact", "consumer", "payload_sha256", "producer", "schema", "version"},
        "CONSUMER_CONTRACT_INVALID",
    )
    if (
        contract["schema"] != CONSUMER_CONTRACT_SCHEMA
        or contract["version"] != CONSUMER_CONTRACT_VERSION
    ):
        raise ContractError("CONSUMER_CONTRACT_INVALID")
    payload_sha = _sha64(
        contract["payload_sha256"], "CONSUMER_CONTRACT_DIGEST_INVALID"
    )
    unsigned = dict(contract)
    del unsigned["payload_sha256"]
    if _sha(_canonical(unsigned)) != payload_sha:
        raise ContractError("CONSUMER_CONTRACT_DIGEST_MISMATCH")
    contract_artifact = _exact(
        contract["artifact"], {"file", "name"}, "CONSUMER_CONTRACT_INVALID"
    )
    if contract_artifact != {"file": CONSUMER_CONTRACT_FILE, "name": name}:
        raise ContractError("CONSUMER_CONTRACT_ARTIFACT_MISMATCH")
    producer = _exact(
        contract["producer"],
        {
            "repository", "workflow_path", "workflow_blob", "run_id",
            "run_attempt", "event", "branch", "head_sha", "head_tree",
        },
        "CONSUMER_CONTRACT_PRODUCER_INVALID",
    )
    if (
        producer["repository"] != TF_REPOSITORY
        or producer["workflow_path"] != CONSUMER_CONTRACT_WORKFLOW
        or producer["run_id"] != run["run_id"]
        or producer["run_attempt"] != run["run_attempt"]
        or producer["event"] != "push"
        or producer["branch"] != "main"
        or producer["head_sha"] != head_sha
    ):
        raise ContractError("CONSUMER_CONTRACT_PRODUCER_MISMATCH")
    _sha40(producer["workflow_blob"], "CONSUMER_CONTRACT_PRODUCER_INVALID")
    tree = _sha40(producer["head_tree"], "CONSUMER_CONTRACT_PRODUCER_INVALID")
    consumer = _exact(
        contract["consumer"],
        {
            "contract_schema_path", "contract_schema_blob", "contract_version",
            "deploy_workflow_path", "deploy_workflow_blob", "pins",
        },
        "CONSUMER_CONTRACT_CONSUMER_INVALID",
    )
    if (
        consumer["contract_schema_path"] != CONSUMER_CONTRACT_SCHEMA_PATH
        or consumer["contract_version"] != CONSUMER_CONTRACT_VERSION
        or consumer["deploy_workflow_path"] != DEPLOY_WORKFLOW
    ):
        raise ContractError("CONSUMER_CONTRACT_CONSUMER_MISMATCH")
    _sha40(
        consumer["contract_schema_blob"], "CONSUMER_CONTRACT_CONSUMER_INVALID"
    )
    workflow_blob = _sha40(
        consumer["deploy_workflow_blob"], "CONSUMER_CONTRACT_CONSUMER_INVALID"
    )
    pins = _exact(
        consumer["pins"],
        {"deployment_environment", "digest_aware_marker", "mutation_group"},
        "CONSUMER_CONTRACT_PINS_INVALID",
    )
    if pins != {
        "deployment_environment": CONSUMER_DEPLOYMENT_ENVIRONMENT,
        "digest_aware_marker": CONSUMER_DIGEST_MARKER,
        "mutation_group": CONSUMER_MUTATION_GROUP,
    }:
        raise ContractError("CONSUMER_CONTRACT_PINS_MISMATCH")
    dispatch: dict[str, Any] = {
        "artifact": {
            "id": artifact_id,
            "name": name,
            "producer_run_id": run["run_id"],
            "producer_run_attempt": run["run_attempt"],
            "provider_sha256": provider_sha,
            "archive_sha256": archive_sha,
            "file_sha256": _sha(raw_contract),
        },
        "contract": contract,
        "schema": CONSUMER_DISPATCH_SCHEMA,
    }
    dispatch["envelope_sha256"] = _sha(_canonical(dispatch))
    encoded = base64.urlsafe_b64encode(_canonical(dispatch)).rstrip(b"=")
    return {
        "source_tree": tree,
        "workflow_blob": workflow_blob,
        "request_slot": {
            "status": "produced",
            "sha256": _sha(encoded),
            "utf8_bytes": len(encoded),
        },
    }


def _zip_member(raw: bytes, filename: str) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = [info.filename for info in archive.infolist() if not info.is_dir()]
            if names != [filename] or filename.startswith(("/", "\\")) or ".." in Path(filename).parts:
                raise ContractError("PROVIDER_ARCHIVE_SHAPE_INVALID")
            info = archive.getinfo(filename)
            if info.file_size > MAX_PROVIDER_JSON_BYTES or info.compress_size > MAX_PROVIDER_ARCHIVE_BYTES:
                raise ContractError("PROVIDER_ARCHIVE_TOO_LARGE")
            return archive.read(filename)
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise ContractError("PROVIDER_ARCHIVE_INVALID") from exc


def _supply(
    manifest: Any,
    build: dict[str, Any],
    tree: str,
    *,
    file_sha256: str | None = None,
) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise ContractError("SUPPLY_MANIFEST_INVALID")
    try:
        validate_manifest(manifest)
    except Exception as exc:
        raise ContractError("SUPPLY_MANIFEST_INVALID") from exc
    if manifest["schema"] == SCHEMA_V3:
        manifest_sha256 = _sha64(file_sha256, "SUPPLY_MANIFEST_INVALID")
        source = manifest["release_source_revision"]
        source_tree = manifest["release_source_tree"]
        build_tag = "v3-" + source[:12]
        if manifest["build_run_id"] != build["run_id"] or manifest["build_run_attempt"] != build["run_attempt"]:
            raise ContractError("SUPPLY_BUILD_IDENTITY_MISMATCH")
    else:
        manifest_sha256 = _sha(_canonical(manifest))
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
        "manifest_sha256": manifest_sha256,
        "service_digests": {
            service: manifest["services"][service]["image_digest"] for service in SERVICE_ORDER
        },
    }


def _relay_receipt(
    value: Any,
    build: dict[str, Any],
    supply: dict[str, Any],
    run_id: int,
    manifest: dict[str, Any],
) -> None:
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
            or value["candidate_supply_set"] != manifest
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


def _relay_children(
    provider: Provider,
    relay: dict[str, Any],
    *,
    require_complete: bool,
) -> tuple[dict[str, int], str]:
    raw = provider.bytes(APP_REPOSITORY, f"/actions/runs/{relay['run_id']}/logs")
    if len(raw) > MAX_PROVIDER_LOG_BYTES:
        raise ContractError("RELAY_LOGS_TOO_LARGE")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if sum(info.file_size for info in archive.infolist()) > MAX_PROVIDER_LOG_BYTES:
                raise ContractError("RELAY_LOGS_TOO_LARGE")
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
        child_run_id = int(run_id)
        if service in children and children[service] != child_run_id:
            raise ContractError("RELAY_CHILD_CARDINALITY")
        children[service] = child_run_id
    valid_services = {"web", "app"}
    if (
        not children
        or not set(children).issubset(valid_services)
        or len(set(children.values())) != len(children)
        or (require_complete and set(children) != valid_services)
    ):
        raise ContractError("RELAY_CHILD_CARDINALITY")
    return children, _sha(raw)


def _relay_failure_evidence(provider: Provider, relay: dict[str, Any]) -> dict[str, Any]:
    raw = provider.json(
        APP_REPOSITORY,
        f"/actions/runs/{relay['run_id']}/attempts/{relay['run_attempt']}/jobs?per_page=100",
    )
    if not isinstance(raw, dict):
        raise ContractError("RELAY_FAILURE_EVIDENCE_INVALID")
    jobs = raw.get("jobs")
    if raw.get("total_count") != 1 or not isinstance(jobs, list) or len(jobs) != 1:
        raise ContractError("RELAY_FAILURE_EVIDENCE_INVALID")
    job = jobs[0]
    if not isinstance(job, dict) or job.get("name") != "dispatch" or not isinstance(job.get("steps"), list):
        raise ContractError("RELAY_FAILURE_EVIDENCE_INVALID")
    selected: dict[str, dict[str, Any]] = {}
    expected = {
        "Deploy each staging service in turn and prove each one landed": "failure",
        "Publish the convergence receipt": "skipped",
    }
    for step in job["steps"]:
        if not isinstance(step, dict) or step.get("name") not in expected:
            continue
        name = step["name"]
        if name in selected or step.get("conclusion") != expected[name]:
            raise ContractError("RELAY_FAILURE_EVIDENCE_INVALID")
        selected[name] = {
            "name": name,
            "number": _positive(step.get("number"), "RELAY_FAILURE_EVIDENCE_INVALID"),
            "conclusion": step["conclusion"],
        }
    if set(selected) != set(expected):
        raise ContractError("RELAY_FAILURE_EVIDENCE_INVALID")
    return {
        "status": "produced",
        "value": {
            "failed_stage": "service_dispatch",
            "jobs_sha256": _sha(_canonical([selected[name] for name in sorted(selected)])),
        },
    }


def _strict_slot(value: Any, reason: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("status") not in {"produced", "not_produced"}:
        raise ContractError(reason)
    expected = {"status", "value"} if value["status"] == "produced" else {"status"}
    return _exact(value, expected, reason)


def _requested_evidence_slot(value: Any) -> dict[str, Any]:
    reason = "SERVICE_RECEIPT_REQUEST_INVALID"
    if value == {"status": "not_produced"}:
        return value
    slot = _exact(value, {"status", "sha256", "utf8_bytes"}, reason)
    if slot["status"] != "produced":
        raise ContractError(reason)
    _sha64(slot["sha256"], reason)
    if isinstance(slot["utf8_bytes"], bool) or not isinstance(slot["utf8_bytes"], int) or slot["utf8_bytes"] < 1:
        raise ContractError(reason)
    return slot


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
    _bounded_tree(receipt, "SERVICE_RECEIPT_TEXT_INVALID", maximum=MAX_PROVIDER_JSON_BYTES)
    if receipt["schema"] != SERVICE_RECEIPT_SCHEMA or receipt["environment"] != ENVIRONMENT:
        raise ContractError("SERVICE_RECEIPT_INVALID")
    if any(receipt[key] not in SERVICE_RESULT_LABELS for key in ("preflight_result", "deploy_result", "terminal_result")):
        raise ContractError("SERVICE_OUTCOME_LABEL_INVALID")
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
        "consumer_contract", "digest_aware_evidence", "digest_aware_reconcile",
        "expected_task_definition",
        "hold_seconds", "image_tag", "p4a_session_identity_cutover",
        "quarantine_recovery_snapshot_identifier", "required_broker_task_definition",
        "service", "snapshot_overflow_acknowledgement", "source_revision", "start_from_zero",
        "start_from_zero_confirmation", "supply_evidence", "target_color",
    }
    requested = _exact(receipt["requested"], requested_keys, "SERVICE_RECEIPT_REQUEST_INVALID")
    if requested["service"] not in SERVICE_ORDER:
        raise ContractError("SERVICE_RECEIPT_REQUEST_INVALID")
    if requested["app_deploy_intent"] not in {"forward", "configuration", "authority-bootstrap", "rollback"}:
        raise ContractError("SERVICE_RECEIPT_REQUEST_INVALID")
    _requested_evidence_slot(requested["consumer_contract"])
    _requested_evidence_slot(requested["supply_evidence"])
    _bounded_text(requested["image_tag"], "SERVICE_RECEIPT_REQUEST_INVALID", maximum=128)
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
            if not isinstance(row["number"], int) or row["number"] < 1:
                raise ContractError("SERVICE_RECEIPT_FAILED_STAGE_INVALID")
            _bounded_text(row["job"], "SERVICE_RECEIPT_FAILED_STAGE_INVALID", maximum=128)
            _bounded_text(row["step"], "SERVICE_RECEIPT_FAILED_STAGE_INVALID", maximum=128)
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
    prior_status = facts["prior_job_status"]
    if prior_status["status"] == "produced" and prior_status["value"] not in {"success", "failure", "cancelled"}:
        raise ContractError("SERVICE_OUTCOME_LABEL_INVALID")
    identity_slot = facts["deployment_identity"]
    if identity_slot["status"] == "produced":
        identity_value = _exact(identity_slot["value"], {"body", "sha256"}, "SERVICE_RECEIPT_FACTS_INVALID")
        if not isinstance(identity_value["body"], dict):
            raise ContractError("SERVICE_RECEIPT_FACTS_INVALID")
        _sha64(identity_value["sha256"], "SERVICE_RECEIPT_FACTS_INVALID")
    if receipt["path"] not in {"deploy", "digest_aware_skip", "digest_aware_preflight"}:
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
                _bounded_text(row["job"], "PROVIDER_JOBS_INVALID", maximum=128)
                _bounded_text(row["step"], "PROVIDER_JOBS_INVALID", maximum=128)
                failed.append(row)
    failed.sort(key=lambda row: (row["number"], row["job"], row["step"]))
    return (
        {"status": "produced", "value": {"primary": failed[0], "additional": failed[1:], "unique": len(failed) == 1}}
        if failed
        else {"status": "not_produced"}
    )


def _evidence_digest_slot(value: dict[str, Any]) -> dict[str, Any]:
    if value["status"] == "not_produced":
        return {"status": "not_produced"}
    _bounded_tree(value["value"], "STRUCTURED_EVIDENCE_INVALID")
    raw = _canonical(value["value"])
    return {"status": "produced", "sha256": _sha(raw), "utf8_bytes": len(raw)}


def _normalized_failed_stage(receipt_stage: dict[str, Any], provider_stage: dict[str, Any]) -> str:
    if provider_stage != receipt_stage:
        raise ContractError("SERVICE_RECEIPT_FAILED_STAGE_MISMATCH")
    if provider_stage == {"status": "not_produced"}:
        return "none"
    failure = provider_stage["value"]
    if failure["additional"] or failure["unique"] is not True:
        raise ContractError("SERVICE_FAILED_STAGE_AMBIGUOUS")
    primary = failure["primary"]
    code = FAILED_STAGE_ALLOWLIST[SERVICE_RECEIPT_SCHEMA].get((primary["job"], primary["step"]))
    if code is None or code not in FAILED_STAGE_CODES:
        raise ContractError("SERVICE_FAILED_STAGE_UNSUPPORTED")
    return code


def _rollback_outcome(
    value: dict[str, Any],
    *,
    mutation_count: int,
    provider_conclusion: str,
    terminal_healthy: bool,
    terminal_td: str | None,
    predecessor_td: str | None,
) -> str:
    if value["status"] != "produced" or not isinstance(value.get("value"), dict):
        raise ContractError("SERVICE_ROLLBACK_EVIDENCE_INVALID")
    raw = value["value"]
    if set(raw) == {"result"}:
        if raw["result"] != "not_required":
            raise ContractError("SERVICE_ROLLBACK_EVIDENCE_INVALID")
        return "not_required"
    raw = _exact(
        raw,
        {"bluegreen_step", "bluegreen_detail", "direct_failure_step", "direct_cancel_step", "authority_result"},
        "SERVICE_ROLLBACK_EVIDENCE_INVALID",
    )
    for key in ("bluegreen_step", "direct_failure_step", "direct_cancel_step"):
        if raw[key] not in ROLLBACK_STEP_RESULTS:
            raise ContractError("SERVICE_ROLLBACK_EVIDENCE_INVALID")
    if raw["bluegreen_detail"] not in ROLLBACK_DETAIL_CODES or raw["authority_result"] not in ROLLBACK_AUTHORITY_CODES:
        raise ContractError("SERVICE_ROLLBACK_EVIDENCE_INVALID")
    rollback_steps = ("bluegreen_step", "direct_failure_step", "direct_cancel_step")
    handler_succeeded = any(raw[key] == "success" for key in rollback_steps)
    restored = (
        handler_succeeded
        or raw["authority_result"] in {"restored", "restored-digest-pinned-revision"}
    )
    if provider_conclusion == "success" and mutation_count in {0, 1}:
        if (
            raw["direct_failure_step"] != "skipped"
            or raw["direct_cancel_step"] != "skipped"
            or raw["authority_result"] not in {"not_produced", "not_required"}
        ):
            raise ContractError("SERVICE_ROLLBACK_EVIDENCE_CONFLICT")
        return "not_required"
    if provider_conclusion == "failure" and mutation_count == 0:
        if any(raw[key] == "failure" for key in rollback_steps):
            raise ContractError("SERVICE_ROLLBACK_EVIDENCE_CONFLICT")
        if raw["authority_result"] in {"restored", "restored-digest-pinned-revision"}:
            raise ContractError("SERVICE_ROLLBACK_EVIDENCE_CONFLICT")
        # A successful failure handler proves that the handler completed. It
        # does not claim a rollback mutation when the receipt records zero.
        if handler_succeeded and (
            not terminal_healthy or predecessor_td is None or terminal_td != predecessor_td
        ):
            raise ContractError("SERVICE_ROLLBACK_EVIDENCE_CONFLICT")
        return "not_required"
    if mutation_count == 2 and provider_conclusion == "failure" and restored:
        return "succeeded"
    raise ContractError("SERVICE_ROLLBACK_EVIDENCE_CONFLICT")


def _terminal_evidence(
    facts: dict[str, Any], *, allow_unreported_primary: bool = False
) -> tuple[bool, str | None, str | None]:
    terminal = facts["terminal"]
    if terminal["status"] == "not_produced":
        return False, None, None
    value = terminal["value"]
    image = value["image_digest"]
    digest = image.get("value") if image.get("status") == "produced" else None
    primary = value["primary_deployments"]
    primary_is_exact = (
        isinstance(primary, list)
        and len(primary) == 1
        and primary[0]
        == {
            "task_definition": value["task_definition"],
            "rollout_state": "COMPLETED",
            "status": "PRIMARY",
        }
    )
    primary_is_unreported_skip = allow_unreported_primary and primary == {"status": "not_produced"}
    healthy = (
        value["stable_1_1_0"] is True
        and value["capacity"] == {"desired": 1, "running": 1, "pending": 0}
        and (primary_is_exact or primary_is_unreported_skip)
        and isinstance(digest, str)
        and DIGEST.fullmatch(digest) is not None
    )
    return healthy, value["task_definition"], digest


def _normalized_outcome(provider: Provider, run: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    if run["conclusion"] not in SUPPORTED_CHILD_CONCLUSIONS:
        raise ContractError("SERVICE_PROVIDER_OUTCOME_UNSUPPORTED")
    if receipt["terminal_result"] != run["conclusion"]:
        raise ContractError("SERVICE_OUTCOME_CONFLICT")
    provider_stage = _failed_steps(provider, run)
    failed_stage = _normalized_failed_stage(receipt["failed_stage"], provider_stage)
    facts = receipt["facts"]
    if facts == {"status": "not_produced"}:
        raise ContractError("SERVICE_RECEIPT_FACTS_NOT_PRODUCED")
    prior = facts["prior_job_status"]
    if prior != {"status": "produced", "value": run["conclusion"]}:
        raise ContractError("SERVICE_OUTCOME_CONFLICT")
    mutation = facts["mutation_count"]
    if mutation.get("status") != "produced" or mutation.get("value") not in {0, 1, 2}:
        raise ContractError("SERVICE_MUTATION_COUNT_INVALID")
    mutation_count = mutation["value"]
    predecessor = facts["predecessor_task_definition"]
    predecessor_td = predecessor.get("value") if predecessor.get("status") == "produced" else None
    candidate = facts["candidate"]
    candidate_td_slot = candidate["task_definition"]
    candidate_digest_slot = candidate["image_digest"]
    candidate_td = candidate_td_slot.get("value") if candidate_td_slot.get("status") == "produced" else None
    candidate_digest = candidate_digest_slot.get("value") if candidate_digest_slot.get("status") == "produced" else None
    path = receipt["path"]
    terminal_healthy, terminal_td, terminal_digest = _terminal_evidence(
        facts, allow_unreported_primary=path == "digest_aware_skip"
    )
    rollback_outcome = _rollback_outcome(
        facts["rollback"],
        mutation_count=mutation_count,
        provider_conclusion=run["conclusion"],
        terminal_healthy=terminal_healthy,
        terminal_td=terminal_td,
        predecessor_td=predecessor_td,
    )
    digest_aware = receipt["requested"]["digest_aware_reconcile"]
    if not isinstance(digest_aware, bool):
        raise ContractError("SERVICE_RECEIPT_REQUEST_INVALID")

    if path == "digest_aware_skip":
        if (
            run["conclusion"] != "success"
            or receipt["preflight_result"] != "success"
            or receipt["deploy_result"] != "skipped"
            or failed_stage != "none"
            or not digest_aware
            or mutation_count != 0
            or rollback_outcome != "not_required"
            or not terminal_healthy
            or terminal_td != predecessor_td
            or candidate_digest != terminal_digest
        ):
            raise ContractError("SERVICE_OUTCOME_CONFLICT")
        outcome = {
            "preflight_state": "passed",
            "mutation_state": "not_started",
            "mutation_count": 0,
            "deployment_outcome": "skipped_exact",
            "rollback_outcome": "not_required",
            "receipt_outcome": "verified",
            "failed_stage": "none",
            "terminal_reason_code": "exact_skip_healthy",
            "terminal_healthy": True,
        }
    elif path == "deploy" and run["conclusion"] == "success":
        expected_preflight = "success" if digest_aware else "skipped"
        if not terminal_healthy:
            raise ContractError("SERVICE_TERMINAL_UNHEALTHY")
        if terminal_digest != candidate_digest:
            raise ContractError("SERVICE_DIGEST_MISMATCH")
        if (
            receipt["preflight_result"] != expected_preflight
            or receipt["deploy_result"] != "success"
            or failed_stage != "none"
            or mutation_count != 1
            or rollback_outcome != "not_required"
            or terminal_td != candidate_td
        ):
            raise ContractError("SERVICE_OUTCOME_CONFLICT")
        outcome = {
            "preflight_state": "passed" if digest_aware else "not_required",
            "mutation_state": "candidate_active",
            "mutation_count": 1,
            "deployment_outcome": "succeeded",
            "rollback_outcome": "not_required",
            "receipt_outcome": "verified",
            "failed_stage": "none",
            "terminal_reason_code": "exact_candidate_healthy",
            "terminal_healthy": True,
        }
    elif path == "deploy" and run["conclusion"] == "failure":
        expected_preflight = "success" if digest_aware else "skipped"
        if receipt["preflight_result"] != expected_preflight or receipt["deploy_result"] != "failure" or failed_stage == "none":
            raise ContractError("SERVICE_OUTCOME_CONFLICT")
        if mutation_count == 0 and rollback_outcome == "not_required" and terminal_td in {None, predecessor_td}:
            outcome = {
                "preflight_state": "passed" if digest_aware else "not_required",
                "mutation_state": "not_started",
                "mutation_count": 0,
                "deployment_outcome": "failed_before_mutation",
                "rollback_outcome": "not_required",
                "receipt_outcome": "verified",
                "failed_stage": failed_stage,
                "terminal_reason_code": "pre_mutation_failure",
                "terminal_healthy": terminal_healthy,
            }
        elif (
            mutation_count == 2
            and rollback_outcome == "succeeded"
            and terminal_healthy
            and predecessor_td is not None
            and terminal_td == predecessor_td
            and candidate_td is not None
            and candidate_td != predecessor_td
        ):
            outcome = {
                "preflight_state": "passed" if digest_aware else "not_required",
                "mutation_state": "predecessor_restored",
                "mutation_count": 2,
                "deployment_outcome": "failed_after_mutation_rolled_back",
                "rollback_outcome": "succeeded",
                "receipt_outcome": "verified",
                "failed_stage": failed_stage,
                "terminal_reason_code": "post_mutation_predecessor_restored",
                "terminal_healthy": True,
            }
        else:
            raise ContractError("SERVICE_OUTCOME_CONFLICT")
    else:
        raise ContractError("SERVICE_OUTCOME_UNSUPPORTED")
    if (
        outcome["preflight_state"] not in PREFLIGHT_STATES
        or outcome["mutation_state"] not in MUTATION_STATES
        or outcome["deployment_outcome"] not in DEPLOYMENT_OUTCOMES
        or outcome["rollback_outcome"] not in ROLLBACK_OUTCOMES
        or outcome["receipt_outcome"] not in RECEIPT_OUTCOMES
        or outcome["failed_stage"] not in FAILED_STAGE_CODES
        or outcome["terminal_reason_code"] not in TERMINAL_REASON_CODES
    ):
        raise ContractError("NORMALIZED_OUTCOME_INVALID")
    return outcome


def _prior_relay_role(
    *,
    service: str,
    relay_attempt: int,
    run: dict[str, Any],
    receipt: dict[str, Any],
    selected_run: dict[str, Any],
    selected_receipt: dict[str, Any],
) -> str | None:
    if service not in {"web", "app"} or relay_attempt <= 1:
        return None
    convergence_id = receipt["requested"]["convergence_id"]
    selected_convergence_id = selected_receipt["requested"]["convergence_id"]
    if (
        not isinstance(convergence_id, str)
        or convergence_id == "not_produced"
        or convergence_id != selected_convergence_id
        or run["created_at"] >= selected_run["created_at"]
    ):
        return None
    return f"prior_relay_{service}"


def _expected_predecessor(previous: dict[str, Any]) -> str | None:
    prior_terminal = previous["terminal"]
    if prior_terminal.get("status") == "produced":
        return prior_terminal["value"].get("task_definition")
    previous_predecessor = previous["predecessor_task_definition"]
    if previous["outcome"]["mutation_count"] != 0:
        raise ContractError("SERVICE_PREDECESSOR_CHAIN_MISMATCH")
    if previous_predecessor.get("status") == "produced":
        return previous_predecessor["value"]
    if previous["role"].startswith("prior_failed_"):
        return None
    raise ContractError("SERVICE_PREDECESSOR_CHAIN_MISMATCH")


def _normalize_child(
    run: dict[str, Any],
    artifact: dict[str, Any],
    receipt: dict[str, Any],
    role: str,
    outcome: dict[str, Any],
    consumer_contract: dict[str, Any],
) -> dict[str, Any]:
    rp = receipt["provider"]
    if (
        rp["run_id"] != run["run_id"]
        or rp["run_attempt"] != run["run_attempt"]
        or rp["head_sha"] != run["head_sha"]
        or run["workflow_path"] != DEPLOY_WORKFLOW
        or run["event"] != "workflow_dispatch"
    ):
        raise ContractError("SERVICE_RECEIPT_RUN_MISMATCH")
    tree = consumer_contract["source_tree"]
    blob = consumer_contract["workflow_blob"]
    if rp["workflow_blob"] != blob:
        raise ContractError("SERVICE_RECEIPT_WORKFLOW_MISMATCH")
    if receipt["requested"]["consumer_contract"] != consumer_contract["request_slot"]:
        raise ContractError("SERVICE_RECEIPT_CONSUMER_CONTRACT_MISMATCH")
    facts = receipt["facts"]
    if facts == {"status": "not_produced"}:
        raise ContractError("SERVICE_RECEIPT_FACTS_NOT_PRODUCED")
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
            "created_at": run["created_at"],
            "updated_at": run["updated_at"],
        },
        "artifact": artifact,
        "service": receipt["requested"]["service"],
        "source_revision": normalized_source,
        "source_revision_evidence": facts["source"]["revision"],
        "source_tree": facts["source"]["tree"],
        "image_tag": receipt["requested"]["image_tag"],
        "app_deploy_intent": receipt["requested"]["app_deploy_intent"],
        "path": receipt["path"],
        "outcome": outcome,
        "supply_evidence": facts["supply"],
        "predecessor_task_definition": facts["predecessor_task_definition"],
        "candidate": facts["candidate"],
        "terminal": facts["terminal"],
        "posture": {
            key: _evidence_digest_slot(facts[source])
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
    primary_unreported_skip = (
        primary == {"status": "not_produced"}
        and child["outcome"]["deployment_outcome"] == "skipped_exact"
    )
    if not primary_unreported_skip and (
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
    _verify_arrival_source(provider, build["head_sha"])
    supply_name = f"staging-supply-set-{build['head_sha']}-attempt-{build['run_attempt']}"
    supply_artifact, manifest = _one_artifact(
        provider, APP_REPOSITORY, build["run_id"], supply_name, "staging-supply-set.json"
    )
    supply = _supply(
        manifest,
        build,
        build_tree,
        file_sha256=supply_artifact["file_sha256"],
    )

    relay_matches: list[
        tuple[
            dict[str, Any],
            dict[str, Any],
            dict[str, Any],
            dict[str, int],
            str,
        ]
    ] = []
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
            if relay["head_sha"] != build["head_sha"]:
                continue
            if relay["conclusion"] == "success":
                artifact, value = _one_artifact(
                    provider, APP_REPOSITORY, relay["run_id"], relay_name, "staging-converged.json"
                )
                _relay_receipt(value, build, supply, relay["run_id"], manifest)
                children, logs_sha = _relay_children(provider, relay, require_complete=True)
                relay_matches.append(
                    (
                        relay,
                        {"status": "produced", "value": artifact},
                        {"status": "not_produced"},
                        children,
                        logs_sha,
                    )
                )
            elif relay["conclusion"] == "failure":
                if any(row.get("name") == relay_name for row in _artifact_rows(provider, APP_REPOSITORY, relay["run_id"])):
                    raise ContractError("FAILED_RELAY_ARTIFACT_PRESENT")
                failure = _relay_failure_evidence(provider, relay)
                children, logs_sha = _relay_children(provider, relay, require_complete=False)
                relay_matches.append(
                    (
                        relay,
                        {"status": "not_produced"},
                        failure,
                        children,
                        logs_sha,
                    )
                )
        except ContractError as exc:
            if exc.reason not in {"PROVIDER_ARTIFACT_CARDINALITY"}:
                raise
    if len(relay_matches) != 1:
        raise ContractError("RELAY_RUN_CARDINALITY")
    relay, relay_artifact, relay_failure, relay_children, relay_logs_sha = relay_matches[0]
    relay_tree, relay_blob = _workflow_blob(provider, APP_REPOSITORY, relay["head_sha"], RELAY_WORKFLOW)
    if relay_tree != build_tree:
        raise ContractError("RELAY_SOURCE_TREE_MISMATCH")
    resumed_after_failure = relay["conclusion"] == "failure"

    frontier: dict[str, Any] | None = None
    if current_frontier_run_id is not None:
        frontier_raw = provider.json(TF_REPOSITORY, f"/actions/runs/{current_frontier_run_id}")
        frontier = _provider_run(frontier_raw, TF_REPOSITORY, DEPLOY_WORKFLOW, "workflow_dispatch")
        if frontier["run_id"] != current_frontier_run_id:
            raise ContractError("FRONTIER_RUN_MISMATCH")
    created_window = f">={relay['created_at']}"
    if frontier is not None:
        if frontier["updated_at"] < relay["created_at"]:
            raise ContractError("CHILD_RUN_WINDOW_MISMATCH")
        created_window = f"{relay['created_at']}..{frontier['updated_at']}"
    child_rows = _workflow_run_rows(
        provider,
        TF_REPOSITORY,
        "deploy-leaf-platform-staging.yml",
        {
            "event": "workflow_dispatch",
            "created": created_window,
            "per_page": 100,
        },
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

    matching: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]] = []
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
            outcome = _normalized_outcome(provider, run, receipt)
            matching.append((run, artifact, receipt, outcome))

    by_id = {run["run_id"]: (run, artifact, receipt, outcome) for run, artifact, receipt, outcome in matching}
    if not required_ids.issubset(by_id):
        raise ContractError("UNCONFIGURED_CHILD_RECEIPTS")
    if current_frontier_run_id is None:
        identity_candidates = [
            run["run_id"]
            for run, _, receipt, _ in matching
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
    roles: dict[int, str] = {current_frontier_run_id: "frontier_app_identity"}
    if resumed_after_failure:
        for service, run_id in relay_children.items():
            roles[run_id] = f"prior_failed_{service.replace('-', '_')}"
    else:
        roles[relay_children["web"]] = "relay_web"
        roles[relay_children["app"]] = "relay_app"
    for service in SERVICE_ORDER:
        candidates = [
            (run["run_id"], receipt, outcome)
            for run, _, receipt, outcome in matching
            if receipt["requested"]["service"] == service and run["run_id"] not in roles
        ]
        successes = [
            run_id
            for run_id, _, outcome in candidates
            if outcome["deployment_outcome"] in {"succeeded", "skipped_exact"}
        ]
        failures = [
            run_id
            for run_id, _, outcome in candidates
            if outcome["deployment_outcome"] in {"failed_before_mutation", "failed_after_mutation_rolled_back"}
        ]
        if service != "app" and (resumed_after_failure or service in {"broker", "harness", "canonical-worker"}):
            if len(successes) != 1:
                raise ContractError("SERVICE_TERMINAL_CARDINALITY")
            roles[successes[0]] = f"terminal_{service.replace('-', '_')}"
        elif successes:
            selected_id = relay_children[service]
            selected_run, _, selected_receipt, _ = by_id[selected_id]
            for run_id in successes:
                run, _, receipt, _ = by_id[run_id]
                role = _prior_relay_role(
                    service=service,
                    relay_attempt=relay["run_attempt"],
                    run=run,
                    receipt=receipt,
                    selected_run=selected_run,
                    selected_receipt=selected_receipt,
                )
                if role is None:
                    raise ContractError("UNCLASSIFIED_MATCHING_CHILD")
                roles[run_id] = role
        for run_id in failures:
            roles[run_id] = f"prior_failed_{service.replace('-', '_')}"
    if set(roles) != set(by_id):
        raise ContractError("UNCLASSIFIED_MATCHING_CHILD")

    normalized_by_id: dict[int, dict[str, Any]] = {}
    consumer_contracts: dict[str, dict[str, Any]] = {}
    per_service: dict[str, list[dict[str, Any]]] = {service: [] for service in SERVICE_ORDER}
    for run_id, role in roles.items():
        run, artifact, raw_receipt, outcome = by_id[run_id]
        if run["head_sha"] not in consumer_contracts:
            consumer_contracts[run["head_sha"]] = _consumer_contract_for_head(
                provider, run["head_sha"]
            )
        child = _normalize_child(
            run,
            artifact,
            raw_receipt,
            role,
            outcome,
            consumer_contracts[run["head_sha"]],
        )
        normalized_by_id[run_id] = child
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
    frontier_child = normalized_by_id[current_frontier_run_id]
    if frontier_child["service"] != "app":
        raise ContractError("CHILD_ROLE_SERVICE_MISMATCH")
    if resumed_after_failure:
        for service, run_id in relay_children.items():
            child = normalized_by_id[run_id]
            if (
                child["service"] != service
                or child["outcome"]["deployment_outcome"]
                not in {"failed_before_mutation", "failed_after_mutation_rolled_back"}
            ):
                raise ContractError("FAILED_RELAY_CHILD_INVALID")
    elif (
        normalized_by_id[relay_children["web"]]["service"] != "web"
        or normalized_by_id[relay_children["app"]]["service"] != "app"
    ):
        raise ContractError("CHILD_ROLE_SERVICE_MISMATCH")
    for service, attempts in per_service.items():
        attempts.sort(key=lambda item: (item["provider"]["created_at"], item["provider"]["run_id"]))
        for previous, current in zip(attempts, attempts[1:]):
            predecessor = current["predecessor_task_definition"]
            if predecessor.get("status") != "produced":
                raise ContractError("SERVICE_PREDECESSOR_CHAIN_MISMATCH")
            expected_predecessor = _expected_predecessor(previous)
            if expected_predecessor is None:
                continue
            if (
                predecessor.get("value") != expected_predecessor
            ):
                raise ContractError("SERVICE_PREDECESSOR_CHAIN_MISMATCH")

    selected_ids = {
        "app": current_frontier_run_id,
        "broker": next(run_id for run_id, role in roles.items() if role == "terminal_broker"),
        "harness": next(run_id for run_id, role in roles.items() if role == "terminal_harness"),
        "canonical-worker": next(
            run_id for run_id, role in roles.items() if role == "terminal_canonical_worker"
        ),
        "web": (
            next(run_id for run_id, role in roles.items() if role == "terminal_web")
            if resumed_after_failure
            else relay_children["web"]
        ),
    }
    selected = {service: normalized_by_id[run_id] for service, run_id in selected_ids.items()}
    for service, child in selected.items():
        if (
            child["outcome"]["deployment_outcome"] not in {"succeeded", "skipped_exact"}
            or child["outcome"]["failed_stage"] != "none"
            or child["outcome"]["terminal_healthy"] is not True
        ):
            raise ContractError("SERVICE_TERMINAL_RESULT_INVALID")
        candidate_digest = child["candidate"]["image_digest"]
        if (
            candidate_digest.get("status") != "produced"
            or candidate_digest.get("value") != supply["service_digests"][service]
        ):
            raise ContractError("SERVICE_CANDIDATE_DIGEST_MISMATCH")
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
            "mode": "resumed_after_failure" if resumed_after_failure else "converged_relay",
            "workflow_blob": relay_blob,
            "artifact": relay_artifact,
            "failure_evidence": relay_failure,
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
    _validate_output(receipt)
    if len(_canonical(receipt) + b"\n") > MAX_RECEIPT_BYTES:
        raise ContractError("OUTPUT_RECEIPT_TOO_LARGE")
    return receipt


def _receipt_bytes(receipt: dict[str, Any]) -> bytes:
    _validate_output(receipt)
    raw = _canonical(receipt) + b"\n"
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ContractError("OUTPUT_RECEIPT_TOO_LARGE")
    return raw


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
        args.output.write_bytes(_receipt_bytes(receipt))
        print(f"Prepared {OUTPUT_SCHEMA}; provider evidence body withheld.")
        return 0
    except ContractError as exc:
        print(f"ERROR:{exc.reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
