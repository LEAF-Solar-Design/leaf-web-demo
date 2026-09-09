#!/usr/bin/env python3
"""Verify app/web staging result artifacts against the Terraform receipt producer.

Receipts use jq -cS output including LF. Supply-set canonical hashes deliberately
retain their separate encoding in platform_release_manifest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from platform_release_manifest import (
    ContractError, _DIGEST, _SHA, _SOURCE_HASH, _TREE, _exact_keys, load_json,
)


def _producer_receipt_sha256(value: Any) -> str:
    """Use the exact producer encoder, not supply-set JSON canonicalization."""
    try:
        encoded = subprocess.run(
            ["jq", "-cS", "."], input=json.dumps(value).encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContractError("cannot encode the surface receipt with jq") from exc
    # The producer runs on Linux. Native Windows jq uses CRLF on stdout.
    return hashlib.sha256(encoded.replace(b"\r\n", b"\n")).hexdigest()


def _exact_integer(value: Any, expected: int | None = None) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and (
        expected is None or value == expected
    )


def _exact_string(value: Any, pattern: re.Pattern[str] | None = None) -> bool:
    return isinstance(value, str) and bool(value) and (
        pattern is None or pattern.fullmatch(value) is not None
    )


def _validate_routed_surface(value: Any) -> None:
    if not isinstance(value, dict):
        raise ContractError("surface routed state must be an object")
    _exact_keys(
        value,
        {
            "color",
            "service",
            "primary_task_definition",
            "route_priorities",
            "route_hash",
            "desired",
            "running",
            "pending",
            "healthy",
        },
        "surface routed state",
    )
    if (
        value["color"] not in {"primary", "alt"}
        or not _exact_string(value["service"])
        or not _exact_string(value["primary_task_definition"])
        or not isinstance(value["route_priorities"], list)
        or not value["route_priorities"]
        or any(not _exact_integer(p) or p <= 0 for p in value["route_priorities"])
        or not _exact_string(value["route_hash"], _SOURCE_HASH)
        or not _exact_integer(value["desired"], 1)
        or not _exact_integer(value["running"], 1)
        or not _exact_integer(value["pending"], 0)
        or value["healthy"] is not True
    ):
        raise ContractError("surface routed state is invalid")


def _validate_surface_snapshot(value: Any, label: str) -> None:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    _exact_keys(
        value,
        {
            "task_definition",
            "deployment_id",
            "desired",
            "running",
            "pending",
            "route_hash",
            "marker_census_sha256",
        },
        label,
    )
    if (
        not _exact_string(value["task_definition"])
        or not _exact_string(value["deployment_id"])
        or not _exact_integer(value["desired"], 1)
        or not _exact_integer(value["running"], 1)
        or not _exact_integer(value["pending"], 0)
        or not _exact_string(value["route_hash"], _SOURCE_HASH)
        or not _exact_string(value["marker_census_sha256"], _SOURCE_HASH)
    ):
        raise ContractError(f"{label} is invalid")


def _validate_surface_skip_receipt(
    receipt: dict[str, Any], *, service: str, convergence_id: str, digest: str
) -> None:
    _exact_keys(
        receipt,
        {
            "schema",
            "release_source_revision",
            "release_source_tree",
            "supply_artifact",
            "service",
            "convergence_id",
            "candidate_image_digest",
            "live_image_digest",
            "component_producer_source_revision",
            "component_producer_source_tree",
            "surface_fingerprint",
            "recipe_fingerprint",
            "candidate_runtime_contract_sha256",
            "live_runtime_contract_sha256",
            "migration_fingerprint",
            "provenance",
            "routed",
            "before",
            "after",
            "aws_mutation_count",
            "identity_stamped",
            "rollback_invoked",
            "migration_result",
            "lock_name",
            "github_run_id",
            "started_at",
            "finished_at",
        },
        "surface skip receipt",
    )
    supply = receipt["supply_artifact"]
    provenance = receipt["provenance"]
    if not isinstance(supply, dict) or not isinstance(provenance, dict):
        raise ContractError("surface skip provenance must be objects")
    _exact_keys(
        supply,
        {"name", "id", "sha256", "producer_run", "producer_attempt"},
        "surface supply artifact",
    )
    _exact_keys(
        provenance,
        {"subject", "digest", "workflow", "blob", "run", "attempt"},
        "surface provenance",
    )
    _validate_routed_surface(receipt["routed"])
    _validate_surface_snapshot(receipt["before"], "surface before state")
    _validate_surface_snapshot(receipt["after"], "surface after state")
    if receipt["before"] != receipt["after"]:
        raise ContractError("surface skip changed routed state")
    if (
        receipt["schema"] != "leaf.staging-surface-skip.v1"
        or receipt["service"] != service
        or receipt["convergence_id"] != convergence_id
        or receipt["candidate_image_digest"] != digest
        or receipt["live_image_digest"] != digest
        or not _exact_string(receipt["release_source_revision"], _SHA)
        or not _exact_string(receipt["release_source_tree"], _TREE)
        or not _exact_string(receipt["component_producer_source_revision"], _SHA)
        or not _exact_string(receipt["component_producer_source_tree"], _TREE)
        or not _exact_string(receipt["surface_fingerprint"], _SOURCE_HASH)
        or not _exact_string(receipt["recipe_fingerprint"], _SOURCE_HASH)
        or not _exact_string(receipt["migration_fingerprint"])
        or not _exact_string(receipt["candidate_runtime_contract_sha256"], _SOURCE_HASH)
        or receipt["candidate_runtime_contract_sha256"]
        != receipt["live_runtime_contract_sha256"]
        or not _exact_integer(receipt["aws_mutation_count"], 0)
        or receipt["identity_stamped"] is not False
        or receipt["rollback_invoked"] is not False
        or receipt["migration_result"]
        != ("unchanged" if service == "app" else "not_app")
        or receipt["lock_name"] != "leaf-platform-staging-ecs-mutation"
        or not _exact_string(receipt["github_run_id"])
        or not _exact_string(receipt["started_at"])
        or not _exact_string(receipt["finished_at"])
        or not _exact_string(supply["name"])
        or not _exact_integer(supply["id"]) or supply["id"] <= 0
        or not _exact_string(supply["sha256"], _SOURCE_HASH)
        or not _exact_integer(supply["producer_run"]) or supply["producer_run"] <= 0
        or not _exact_integer(supply["producer_attempt"]) or supply["producer_attempt"] <= 0
        or not _exact_string(provenance["subject"])
        or provenance["digest"] != digest
        or not _exact_string(provenance["workflow"])
        or not _exact_string(provenance["blob"], _SHA)
        or not _exact_integer(provenance["run"]) or provenance["run"] <= 0
        or not _exact_integer(provenance["attempt"]) or provenance["attempt"] <= 0
    ):
        raise ContractError("surface skip receipt is invalid")


def _validate_surface_deploy_receipt(
    receipt: dict[str, Any], *, service: str, convergence_id: str, digest: str
) -> None:
    _exact_keys(
        receipt,
        {
            "schema",
            "service",
            "convergence_id",
            "candidate_image_digest",
            "terminal_image_digest",
            "terminal_task_definition",
            "github_run_id",
            "aws_mutation_count",
            "identity_stamped",
            "rollback_invoked",
        },
        "surface deploy receipt",
    )
    if (
        receipt["schema"] != "leaf.staging-surface-deploy.v1"
        or receipt["service"] != service
        or receipt["convergence_id"] != convergence_id
        or receipt["candidate_image_digest"] != digest
        or receipt["terminal_image_digest"] != digest
        or not _exact_string(receipt["terminal_task_definition"])
        or not _exact_string(receipt["github_run_id"])
        or not _exact_integer(receipt["aws_mutation_count"])
        or receipt["aws_mutation_count"] <= 0
        or receipt["identity_stamped"] is not False
        or receipt["rollback_invoked"] is not False
    ):
        raise ContractError("surface deploy receipt is invalid")


def verify_surface_result(
    directory: Path,
    *,
    service: str,
    convergence_id: str,
    release_source_revision: str,
    candidate_digest: str,
    terraform_workflow_blob: str,
) -> dict[str, Any]:
    if service not in {"app", "web"}:
        raise ContractError("surface result service is invalid")
    if not _SHA.fullmatch(release_source_revision):
        raise ContractError("surface result release revision is invalid")
    if not _DIGEST.fullmatch(candidate_digest):
        raise ContractError("surface result digest is invalid")
    if not _SHA.fullmatch(terraform_workflow_blob):
        raise ContractError("surface result workflow blob is invalid")
    if not directory.is_dir() or directory.is_symlink():
        raise ContractError("surface result directory is invalid")
    files = {item.name for item in directory.iterdir() if item.is_file()}
    if len(files) != len(list(directory.iterdir())):
        raise ContractError("surface result artifact contains unsupported entries")
    result = load_json(directory / "surface-result.json")
    _exact_keys(
        result,
        {
            "schema",
            "release_source_revision",
            "service",
            "convergence_id",
            "outcome",
            "candidate_image_digest",
            "terminal_image_digest",
            "terraform_workflow_blob",
            "surface_receipt_sha256",
            "aws_mutation_count",
        },
        "surface result",
    )
    outcome = result["outcome"]
    receipt_name = {
        "skipped": "surface-skip-receipt.json",
        "deployed": "surface-deploy-receipt.json",
    }.get(outcome)
    if receipt_name is None or files != {"surface-result.json", receipt_name}:
        raise ContractError("surface result artifact file set is invalid")
    receipt = load_json(directory / receipt_name)
    if (
        result["schema"] != "leaf.staging-surface-result.v1"
        or result["release_source_revision"] != release_source_revision
        or result["service"] != service
        or result["convergence_id"] != convergence_id
        or result["candidate_image_digest"] != candidate_digest
        or result["terminal_image_digest"] != candidate_digest
        or result["terraform_workflow_blob"] != terraform_workflow_blob
        or not _exact_string(result["surface_receipt_sha256"], _SOURCE_HASH)
        or result["surface_receipt_sha256"] != _producer_receipt_sha256(receipt)
    ):
        raise ContractError("surface result is invalid or does not bind its receipt")
    if outcome == "skipped":
        if not _exact_integer(result["aws_mutation_count"], 0):
            raise ContractError("surface skip result claims a mutation")
        _validate_surface_skip_receipt(
            receipt,
            service=service,
            convergence_id=convergence_id,
            digest=candidate_digest,
        )
    else:
        if (
            not _exact_integer(result["aws_mutation_count"])
            or result["aws_mutation_count"] <= 0
        ):
            raise ContractError("surface deploy result lacks a mutation")
        _validate_surface_deploy_receipt(
            receipt,
            service=service,
            convergence_id=convergence_id,
            digest=candidate_digest,
        )
        if result["aws_mutation_count"] != receipt["aws_mutation_count"]:
            raise ContractError("surface deploy mutation count drifted")
    return result



def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    for field in ("service", "convergence-id", "release-source-revision",
                  "candidate-digest", "terraform-workflow-blob"):
        parser.add_argument("--" + field, required=True)
    args = vars(parser.parse_args())
    try:
        verify_surface_result(**args)
    except ContractError as exc:
        parser.exit(1, f"surface receipt refused: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
