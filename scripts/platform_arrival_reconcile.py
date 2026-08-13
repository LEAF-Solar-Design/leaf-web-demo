#!/usr/bin/env python3
"""Dormant arrival-frontier decisions from one closed typed evidence bundle."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import json
import re
from typing import Any

from platform_semantic_eligibility import (
    ContractError,
    reject_secret_material,
    sha256_digest,
)


SERVICES = ("app", "broker", "canonical-worker", "harness", "web")
PRODUCER_REPOSITORY = "LEAF-Solar-Design/leaf-web-demo"
PRODUCER_WORKFLOW = ".github/workflows/build-platform-images.yml"
RELAY_REPOSITORY = "LEAF-Solar-Design/leaf-web-demo"
RELAY_WORKFLOW = ".github/workflows/dispatch-staging-deploys.yml"
SCHEMA = "leaf.platform-arrival-bundle.v2"
TOPOLOGY_VERSION = "leaf.platform-five-service.v1"
VERIFIER_VERSION = "leaf.platform-arrival-verifier.v2"
RESULT_SCHEMA = "leaf.platform-arrival-reconciliation.v2"
FIXTURE_NOW = 1786641000
MAX_EVIDENCE_LIFETIME_SECONDS = 31 * 24 * 60 * 60
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_STAGE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ARTIFACT_NAME = re.compile(
    r"^staging-supply-set-[0-9a-f]{40}-attempt-[1-9][0-9]*$"
)
_TASK_DEFINITION = re.compile(r"^leaf-platform-[a-z-]+:[1-9][0-9]*$")
_REQUIRED_PREDECESSORS = {
    "app": ("build",),
    "broker": ("build", "web"),
    "canonical-worker": ("build", "web", "broker", "harness"),
    "harness": ("build", "web"),
    "web": ("build",),
}


def _exact(value: Any, keys: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(code)
    observed = tuple(value)
    if len(observed) != len(keys) or set(observed) != keys:
        raise ContractError(code)
    return dict(value)


def _string(value: Any, pattern: re.Pattern[str], code: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ContractError(code)
    return value


def _sha(value: Any, code: str) -> str:
    return _string(value, _SHA, code)


def _digest(value: Any, code: str) -> str:
    return _string(value, _DIGEST, code)


def _integer(value: Any, minimum: int, maximum: int, code: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise ContractError(code)
    return value


def _enum(value: Any, allowed: set[str], code: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ContractError(code)
    return value


def _payload_without(value: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    excluded = set(keys)
    return {key: deepcopy(item) for key, item in value.items() if key not in excluded}


def _verify_checksum(value: dict[str, Any], code: str) -> None:
    checksum = _digest(value["checksum"], code)
    if checksum != sha256_digest(_payload_without(value, "checksum")):
        raise ContractError(code)


def _service_rows(value: Any, code: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) != len(SERVICES):
        raise ContractError(code)
    rows: list[dict[str, str]] = []
    for expected, raw in zip(SERVICES, value, strict=True):
        row = _exact(raw, {"name", "image_digest"}, code)
        if row["name"] != expected:
            raise ContractError(code)
        rows.append(
            {
                "name": expected,
                "image_digest": _digest(row["image_digest"], code),
            }
        )
    return rows


def _verify_arrival(value: Any) -> dict[str, Any]:
    result = _exact(
        value,
        {
            "repository",
            "pr_number",
            "sequence",
            "previous_source_revision",
            "source_revision",
            "source_tree",
            "arrived_at_epoch",
            "checksum",
        },
        "ARRIVAL_INVALID",
    )
    if result["repository"] != PRODUCER_REPOSITORY:
        raise ContractError("ARRIVAL_REPOSITORY_INVALID")
    _integer(result["pr_number"], 1, 1_000_000, "ARRIVAL_INVALID")
    _integer(result["sequence"], 1, 10**12, "ARRIVAL_INVALID")
    for key in ("previous_source_revision", "source_revision", "source_tree"):
        _sha(result[key], "ARRIVAL_SOURCE_INVALID")
    _integer(result["arrived_at_epoch"], 1, 4_102_444_800, "ARRIVAL_INVALID")
    _verify_checksum(result, "ARRIVAL_CHECKSUM_MISMATCH")
    return result


def _verify_producer(value: Any, arrival: dict[str, Any]) -> dict[str, Any]:
    result = _exact(
        value,
        {
            "repository",
            "workflow",
            "run_id",
            "source_revision",
            "source_tree",
            "conclusion",
            "checksum",
        },
        "PRODUCER_INVALID",
    )
    if (
        result["repository"] != PRODUCER_REPOSITORY
        or result["workflow"] != PRODUCER_WORKFLOW
    ):
        raise ContractError("PRODUCER_IDENTITY_INVALID")
    _integer(result["run_id"], 1, 10**15, "PRODUCER_INVALID")
    _sha(result["source_revision"], "PRODUCER_SOURCE_INVALID")
    _sha(result["source_tree"], "PRODUCER_SOURCE_INVALID")
    _enum(result["conclusion"], {"success", "failure"}, "PRODUCER_INVALID")
    _verify_checksum(result, "PRODUCER_CHECKSUM_MISMATCH")
    if (
        result["source_revision"] != arrival["source_revision"]
        or result["source_tree"] != arrival["source_tree"]
    ):
        raise ContractError("PRODUCER_ARRIVAL_LINEAGE_MISMATCH")
    return result


def _manifest_payload(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return _payload_without(manifest, "manifest_digest", "checksum")


def _verify_supply(
    value: Any,
    *,
    code: str,
    expected_source: str | None = None,
    expected_tree: str | None = None,
) -> dict[str, Any]:
    supply = _exact(value, {"artifact", "manifest", "checksum"}, code)
    artifact = _exact(
        supply["artifact"],
        {
            "artifact_id",
            "artifact_name",
            "content",
            "content_digest",
            "checksum",
        },
        code,
    )
    _integer(artifact["artifact_id"], 1, 10**15, code)
    if (
        not isinstance(artifact["artifact_name"], str)
        or _ARTIFACT_NAME.fullmatch(artifact["artifact_name"]) is None
    ):
        raise ContractError(code)
    content = _exact(
        artifact["content"],
        {"manifest_digest", "source_revision", "source_tree", "services"},
        code,
    )
    _digest(content["manifest_digest"], code)
    _sha(content["source_revision"], code)
    _sha(content["source_tree"], code)
    content["services"] = _service_rows(content["services"], code)
    if _digest(artifact["content_digest"], code) != sha256_digest(content):
        raise ContractError("SUPPLY_ARTIFACT_CONTENT_MISMATCH")
    _verify_checksum(artifact, "SUPPLY_ARTIFACT_CHECKSUM_MISMATCH")

    manifest = _exact(
        supply["manifest"],
        {
            "schema",
            "source_revision",
            "source_tree",
            "services",
            "manifest_digest",
            "checksum",
        },
        code,
    )
    if manifest["schema"] != "leaf.staging-supply-manifest.v1":
        raise ContractError(code)
    _sha(manifest["source_revision"], code)
    _sha(manifest["source_tree"], code)
    manifest["services"] = _service_rows(manifest["services"], code)
    if _digest(manifest["manifest_digest"], code) != sha256_digest(
        _manifest_payload(manifest)
    ):
        raise ContractError("SUPPLY_MANIFEST_DIGEST_MISMATCH")
    _verify_checksum(manifest, "SUPPLY_MANIFEST_CHECKSUM_MISMATCH")
    if (
        content["manifest_digest"] != manifest["manifest_digest"]
        or content["source_revision"] != manifest["source_revision"]
        or content["source_tree"] != manifest["source_tree"]
        or content["services"] != manifest["services"]
    ):
        raise ContractError("SUPPLY_ARTIFACT_MANIFEST_MISMATCH")
    if artifact["artifact_name"] != (
        f"staging-supply-set-{manifest['source_revision']}-attempt-1"
    ):
        raise ContractError("SUPPLY_ARTIFACT_SOURCE_MISMATCH")
    if expected_source is not None and manifest["source_revision"] != expected_source:
        raise ContractError("SUPPLY_SOURCE_MISMATCH")
    if expected_tree is not None and manifest["source_tree"] != expected_tree:
        raise ContractError("SUPPLY_TREE_MISMATCH")
    _verify_checksum(supply, "SUPPLY_CHECKSUM_MISMATCH")
    supply["artifact"] = artifact
    supply["manifest"] = manifest
    return supply


def _verify_relay(
    value: Any,
    arrival: dict[str, Any],
    supply: dict[str, Any],
) -> dict[str, Any]:
    result = _exact(
        value,
        {
            "repository",
            "workflow",
            "run_id",
            "source_revision",
            "source_tree",
            "supply_artifact_id",
            "supply_artifact_content_digest",
            "supply_manifest_digest",
            "predecessor_receipt_digest",
            "conclusion",
            "checksum",
        },
        "RELAY_INVALID",
    )
    if result["repository"] != RELAY_REPOSITORY or result["workflow"] != RELAY_WORKFLOW:
        raise ContractError("RELAY_IDENTITY_INVALID")
    _integer(result["run_id"], 1, 10**15, "RELAY_INVALID")
    _sha(result["source_revision"], "RELAY_INVALID")
    _sha(result["source_tree"], "RELAY_INVALID")
    _integer(result["supply_artifact_id"], 1, 10**15, "RELAY_INVALID")
    for key in (
        "supply_artifact_content_digest",
        "supply_manifest_digest",
        "predecessor_receipt_digest",
    ):
        _digest(result[key], "RELAY_INVALID")
    _enum(result["conclusion"], {"success", "failure", "in_progress"}, "RELAY_INVALID")
    _verify_checksum(result, "RELAY_CHECKSUM_MISMATCH")
    artifact = supply["artifact"]
    manifest = supply["manifest"]
    if (
        result["source_revision"] != arrival["source_revision"]
        or result["source_tree"] != arrival["source_tree"]
        or result["supply_artifact_id"] != artifact["artifact_id"]
        or result["supply_artifact_content_digest"] != artifact["content_digest"]
        or result["supply_manifest_digest"] != manifest["manifest_digest"]
    ):
        raise ContractError("RELAY_LINEAGE_MISMATCH")
    return result


def _child_receipt_payload(child: Mapping[str, Any]) -> dict[str, Any]:
    return _payload_without(child, "receipt_digest", "checksum")


def _verify_children(
    value: Any,
    *,
    arrival: dict[str, Any],
    supply: dict[str, Any],
    relay: dict[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= len(SERVICES):
        raise ContractError("CHILD_RUNS_INVALID")
    result: list[dict[str, Any]] = []
    run_ids: set[int] = set()
    services: set[str] = set()
    expected_predecessor = relay["predecessor_receipt_digest"]
    previous_run_id = 0
    for raw in value:
        child = _exact(
            raw,
            {
                "service",
                "run_id",
                "relay_run_id",
                "source_revision",
                "source_tree",
                "supply_artifact_id",
                "supply_artifact_content_digest",
                "supply_manifest_digest",
                "predecessor_receipt_digest",
                "result",
                "failed_stage",
                "receipt_digest",
                "checksum",
            },
            "CHILD_RUN_INVALID",
        )
        service = _enum(child["service"], set(SERVICES), "CHILD_RUN_INVALID")
        run_id = _integer(child["run_id"], 1, 10**15, "CHILD_RUN_INVALID")
        if service in services or run_id in run_ids:
            raise ContractError("DUPLICATE_CHILD_RUN")
        if run_id <= previous_run_id:
            raise ContractError("CHILD_RUN_ORDER_INVALID")
        previous_run_id = run_id
        services.add(service)
        run_ids.add(run_id)
        _integer(child["relay_run_id"], 1, 10**15, "CHILD_RUN_INVALID")
        _sha(child["source_revision"], "CHILD_RUN_INVALID")
        _sha(child["source_tree"], "CHILD_RUN_INVALID")
        _integer(child["supply_artifact_id"], 1, 10**15, "CHILD_RUN_INVALID")
        for key in (
            "supply_artifact_content_digest",
            "supply_manifest_digest",
            "predecessor_receipt_digest",
        ):
            _digest(child[key], "CHILD_RUN_INVALID")
        observed_result = _enum(
            child["result"],
            {"success", "failure", "in_progress", "cancelled"},
            "CHILD_RUN_INVALID",
        )
        if child["failed_stage"] is None:
            if observed_result == "failure":
                raise ContractError("CHILD_FAILED_STAGE_MISSING")
        else:
            _string(child["failed_stage"], _STAGE, "CHILD_RUN_INVALID")
            if observed_result != "failure":
                raise ContractError("CHILD_FAILED_STAGE_CONFLICT")
        expected = (
            child["relay_run_id"] == relay["run_id"]
            and child["source_revision"] == arrival["source_revision"]
            and child["source_tree"] == arrival["source_tree"]
            and child["supply_artifact_id"] == supply["artifact"]["artifact_id"]
            and child["supply_artifact_content_digest"]
            == supply["artifact"]["content_digest"]
            and child["supply_manifest_digest"]
            == supply["manifest"]["manifest_digest"]
        )
        if not expected:
            raise ContractError("CHILD_RUN_LINEAGE_MISMATCH")
        if child["predecessor_receipt_digest"] != expected_predecessor:
            raise ContractError("CHILD_PREDECESSOR_MISMATCH")
        receipt = _digest(child["receipt_digest"], "CHILD_RUN_INVALID")
        if receipt != sha256_digest(_child_receipt_payload(child)):
            raise ContractError("CHILD_RECEIPT_DIGEST_MISMATCH")
        _verify_checksum(child, "CHILD_CHECKSUM_MISMATCH")
        expected_predecessor = receipt
        result.append(child)
    return result


def _child_outcome(child: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "service": child["service"],
        "run_id": child["run_id"],
        "result": child["result"],
        "failed_stage": child["failed_stage"],
        "receipt_digest": child["receipt_digest"],
    }


def _verify_live_identity(
    value: Any,
    *,
    arrival: dict[str, Any],
    supply: dict[str, Any],
) -> dict[str, Any]:
    identity = _exact(
        value,
        {
            "source_revision",
            "source_tree",
            "supply_manifest_digest",
            "body_digest",
            "services",
            "checksum",
        },
        "LIVE_IDENTITY_INVALID",
    )
    _sha(identity["source_revision"], "LIVE_IDENTITY_INVALID")
    _sha(identity["source_tree"], "LIVE_IDENTITY_INVALID")
    _digest(identity["supply_manifest_digest"], "LIVE_IDENTITY_INVALID")
    _digest(identity["body_digest"], "LIVE_IDENTITY_INVALID")
    rows = identity["services"]
    if not isinstance(rows, list) or len(rows) != len(SERVICES):
        raise ContractError("LIVE_IDENTITY_INVALID")
    supply_map = {
        row["name"]: row["image_digest"] for row in supply["manifest"]["services"]
    }
    canonical_rows: list[dict[str, str]] = []
    for expected, raw in zip(SERVICES, rows, strict=True):
        row = _exact(raw, {"name", "image_digest", "task_definition"}, "LIVE_IDENTITY_INVALID")
        if row["name"] != expected:
            raise ContractError("LIVE_IDENTITY_INVALID")
        digest = _digest(row["image_digest"], "LIVE_IDENTITY_INVALID")
        if digest != supply_map[expected]:
            raise ContractError("LIVE_IDENTITY_SUPPLY_MISMATCH")
        if (
            not isinstance(row["task_definition"], str)
            or _TASK_DEFINITION.fullmatch(row["task_definition"]) is None
        ):
            raise ContractError("LIVE_IDENTITY_INVALID")
        canonical_rows.append(deepcopy(row))
    identity["services"] = canonical_rows
    if (
        identity["source_revision"] != arrival["source_revision"]
        or identity["source_tree"] != arrival["source_tree"]
        or identity["supply_manifest_digest"]
        != supply["manifest"]["manifest_digest"]
    ):
        raise ContractError("LIVE_IDENTITY_LINEAGE_MISMATCH")
    _verify_checksum(identity, "LIVE_IDENTITY_CHECKSUM_MISMATCH")
    return identity


def _convergence_content(convergence: Mapping[str, Any]) -> dict[str, Any]:
    return _payload_without(
        convergence,
        "artifact_content_digest",
        "receipt_digest",
        "checksum",
    )


def _verify_convergence(
    value: Any,
    *,
    arrival: dict[str, Any],
    supply: dict[str, Any],
    children: list[dict[str, Any]],
) -> dict[str, Any]:
    result = _exact(
        value,
        {
            "artifact_id",
            "artifact_content_digest",
            "receipt_digest",
            "source_revision",
            "source_tree",
            "supply_manifest_digest",
            "outcome",
            "service_outcomes",
            "live_identity",
            "checksum",
        },
        "CONVERGENCE_INVALID",
    )
    _integer(result["artifact_id"], 1, 10**15, "CONVERGENCE_INVALID")
    _sha(result["source_revision"], "CONVERGENCE_INVALID")
    _sha(result["source_tree"], "CONVERGENCE_INVALID")
    _digest(result["supply_manifest_digest"], "CONVERGENCE_INVALID")
    outcome = _enum(
        result["outcome"],
        {"converged", "failed", "in_progress"},
        "CONVERGENCE_INVALID",
    )
    if (
        result["source_revision"] != arrival["source_revision"]
        or result["source_tree"] != arrival["source_tree"]
        or result["supply_manifest_digest"]
        != supply["manifest"]["manifest_digest"]
    ):
        raise ContractError("CONVERGENCE_LINEAGE_MISMATCH")
    expected_outcomes = [_child_outcome(child) for child in children]
    if result["service_outcomes"] != expected_outcomes:
        raise ContractError("CONVERGENCE_CHILD_RESULTS_MISMATCH")
    if outcome == "converged":
        if (
            [item["service"] for item in expected_outcomes] != list(SERVICES)
            or any(item["result"] != "success" for item in expected_outcomes)
            or result["live_identity"] is None
        ):
            raise ContractError("CONVERGENCE_INCOMPLETE")
        result["live_identity"] = _verify_live_identity(
            result["live_identity"], arrival=arrival, supply=supply
        )
    elif outcome == "failed":
        if sum(item["result"] == "failure" for item in expected_outcomes) != 1:
            raise ContractError("CONVERGENCE_FAILURE_AMBIGUOUS")
        if result["live_identity"] is not None:
            raise ContractError("CONVERGENCE_FAILURE_IDENTITY_CONFLICT")
    else:
        if not any(item["result"] == "in_progress" for item in expected_outcomes):
            raise ContractError("CONVERGENCE_PROGRESS_MISSING")
        if result["live_identity"] is not None:
            raise ContractError("CONVERGENCE_PROGRESS_IDENTITY_CONFLICT")
    content = _convergence_content(result)
    if _digest(result["artifact_content_digest"], "CONVERGENCE_INVALID") != sha256_digest(
        {"artifact": content}
    ):
        raise ContractError("CONVERGENCE_ARTIFACT_DIGEST_MISMATCH")
    if _digest(result["receipt_digest"], "CONVERGENCE_INVALID") != sha256_digest(
        {"receipt": content}
    ):
        raise ContractError("CONVERGENCE_RECEIPT_DIGEST_MISMATCH")
    _verify_checksum(result, "CONVERGENCE_CHECKSUM_MISMATCH")
    return result


def _verify_failed_stage(value: Any, children: list[dict[str, Any]]) -> dict[str, Any] | None:
    failures = [child for child in children if child["result"] == "failure"]
    if value is None:
        if failures:
            raise ContractError("FAILED_STAGE_BINDING_MISSING")
        return None
    result = _exact(
        value,
        {"service", "run_id", "stage", "checksum"},
        "FAILED_STAGE_INVALID",
    )
    _enum(result["service"], set(SERVICES), "FAILED_STAGE_INVALID")
    _integer(result["run_id"], 1, 10**15, "FAILED_STAGE_INVALID")
    _string(result["stage"], _STAGE, "FAILED_STAGE_INVALID")
    _verify_checksum(result, "FAILED_STAGE_CHECKSUM_MISMATCH")
    if len(failures) != 1:
        raise ContractError("FAILED_STAGE_AMBIGUOUS")
    failure = failures[0]
    if (
        result["service"] != failure["service"]
        or result["run_id"] != failure["run_id"]
        or result["stage"] != failure["failed_stage"]
    ):
        raise ContractError("FAILED_STAGE_NOT_OBSERVED")
    return result


def _stage_receipt_payload(receipt: Mapping[str, Any]) -> dict[str, Any]:
    return _payload_without(receipt, "receipt_digest", "checksum")


def _verify_stage_receipts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 16:
        raise ContractError("STAGE_RECEIPTS_INVALID")
    result: list[dict[str, Any]] = []
    stages: set[str] = set()
    for raw in value:
        receipt = _exact(
            raw,
            {
                "stage",
                "source_revision",
                "source_tree",
                "supply_manifest_digest",
                "receipt_digest",
                "checksum",
            },
            "STAGE_RECEIPT_INVALID",
        )
        stage = _string(receipt["stage"], _STAGE, "STAGE_RECEIPT_INVALID")
        if stage in stages:
            raise ContractError("DUPLICATE_STAGE_RECEIPT")
        stages.add(stage)
        _sha(receipt["source_revision"], "STAGE_RECEIPT_INVALID")
        _sha(receipt["source_tree"], "STAGE_RECEIPT_INVALID")
        _digest(receipt["supply_manifest_digest"], "STAGE_RECEIPT_INVALID")
        if _digest(receipt["receipt_digest"], "STAGE_RECEIPT_INVALID") != sha256_digest(
            _stage_receipt_payload(receipt)
        ):
            raise ContractError("STAGE_RECEIPT_DIGEST_MISMATCH")
        _verify_checksum(receipt, "STAGE_RECEIPT_CHECKSUM_MISMATCH")
        result.append(receipt)
    return result


def _verify_frontier_arrival(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    result = _exact(
        value,
        {
            "sequence",
            "source_revision",
            "source_tree",
            "owner_class",
            "state",
            "bundle_digest",
            "checksum",
        },
        "FRONTIER_ARRIVAL_INVALID",
    )
    _integer(result["sequence"], 1, 10**12, "FRONTIER_ARRIVAL_INVALID")
    _sha(result["source_revision"], "FRONTIER_ARRIVAL_INVALID")
    _sha(result["source_tree"], "FRONTIER_ARRIVAL_INVALID")
    _enum(
        result["owner_class"],
        {"same_train", "external_owner", "unknown"},
        "FRONTIER_ARRIVAL_INVALID",
    )
    _enum(result["state"], {"queued", "active", "terminal"}, "FRONTIER_ARRIVAL_INVALID")
    _digest(result["bundle_digest"], "FRONTIER_ARRIVAL_INVALID")
    _verify_checksum(result, "FRONTIER_ARRIVAL_CHECKSUM_MISMATCH")
    return result


def _verify_frontier(value: Any) -> dict[str, Any]:
    result = _exact(
        value,
        {
            "current_sequence",
            "current_source_revision",
            "current_source_tree",
            "current_supply",
            "current_supply_manifest_digest",
            "live_services",
            "prior_successful_stage_receipts",
            "terminal_receipt_digest",
            "arrival",
            "checksum",
        },
        "FRONTIER_INVALID",
    )
    _integer(result["current_sequence"], 1, 10**12, "FRONTIER_INVALID")
    _sha(result["current_source_revision"], "FRONTIER_INVALID")
    _sha(result["current_source_tree"], "FRONTIER_INVALID")
    result["current_supply"] = _verify_supply(
        result["current_supply"],
        code="FRONTIER_SUPPLY_INVALID",
        expected_source=result["current_source_revision"],
        expected_tree=result["current_source_tree"],
    )
    if _digest(result["current_supply_manifest_digest"], "FRONTIER_INVALID") != (
        result["current_supply"]["manifest"]["manifest_digest"]
    ):
        raise ContractError("FRONTIER_MANIFEST_MISMATCH")
    result["live_services"] = _service_rows(
        result["live_services"], "FRONTIER_LIVE_SERVICES_INVALID"
    )
    result["prior_successful_stage_receipts"] = _verify_stage_receipts(
        result["prior_successful_stage_receipts"]
    )
    _digest(result["terminal_receipt_digest"], "FRONTIER_INVALID")
    result["arrival"] = _verify_frontier_arrival(result["arrival"])
    _verify_checksum(result, "FRONTIER_CHECKSUM_MISMATCH")
    return result


@dataclass(frozen=True, slots=True, init=False)
class ValidatedArrivalBundle:
    """Immutable verifier result with no public constructor."""

    content_digest: str
    payload_json: str
    expires_at_epoch: int

    def __new__(cls, *_args: Any, **_kwargs: Any) -> ValidatedArrivalBundle:
        raise TypeError("validated arrival bundles have no public constructor")


def _new_validated(payload: dict[str, Any]) -> ValidatedArrivalBundle:
    instance = object.__new__(ValidatedArrivalBundle)
    object.__setattr__(instance, "content_digest", payload["bundle_digest"])
    object.__setattr__(
        instance,
        "payload_json",
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
    )
    object.__setattr__(instance, "expires_at_epoch", payload["expires_at_epoch"])
    return instance


def verify_arrival_bundle(value: Any, *, now_epoch: int) -> ValidatedArrivalBundle:
    """Verify every checksum and cross-object lineage in one closed bundle."""

    now = _integer(now_epoch, 0, 4_102_444_800, "BUNDLE_TIME_INVALID")
    bundle = _exact(
        value,
        {
            "schema",
            "version",
            "environment",
            "topology_version",
            "verifier_version",
            "selectors",
            "issued_at_epoch",
            "expires_at_epoch",
            "arrival",
            "producer",
            "supply",
            "relay",
            "child_runs",
            "convergence",
            "failed_stage",
            "frontier",
            "bundle_digest",
        },
        "ARRIVAL_BUNDLE_INVALID",
    )
    if bundle["schema"] != SCHEMA or bundle["version"] != 2:
        raise ContractError("ARRIVAL_BUNDLE_VERSION_INVALID")
    if bundle["environment"] != "staging":
        raise ContractError("ARRIVAL_BUNDLE_ENVIRONMENT_INVALID")
    if bundle["topology_version"] != TOPOLOGY_VERSION:
        raise ContractError("ARRIVAL_BUNDLE_TOPOLOGY_INVALID")
    if bundle["verifier_version"] != VERIFIER_VERSION:
        raise ContractError("ARRIVAL_BUNDLE_VERIFIER_INVALID")
    selectors = _exact(
        bundle["selectors"],
        {"arrival_frontier"},
        "ARRIVAL_BUNDLE_SELECTORS_INVALID",
    )
    if selectors != {"arrival_frontier": "UNCONFIGURED"}:
        raise ContractError("SELECTOR_ACTIVATION_FORBIDDEN")
    issued = _integer(bundle["issued_at_epoch"], 0, 4_102_444_800, "BUNDLE_TIME_INVALID")
    expires = _integer(bundle["expires_at_epoch"], 1, 4_102_444_800, "BUNDLE_TIME_INVALID")
    if (
        issued > now
        or now >= expires
        or expires <= issued
        or expires - issued > MAX_EVIDENCE_LIFETIME_SECONDS
    ):
        raise ContractError("ARRIVAL_BUNDLE_EXPIRED")

    arrival = _verify_arrival(bundle["arrival"])
    producer = _verify_producer(bundle["producer"], arrival)
    supply = _verify_supply(
        bundle["supply"],
        code="SUPPLY_INVALID",
        expected_source=arrival["source_revision"],
        expected_tree=arrival["source_tree"],
    )
    relay = _verify_relay(bundle["relay"], arrival, supply)
    children = _verify_children(
        bundle["child_runs"], arrival=arrival, supply=supply, relay=relay
    )
    convergence = _verify_convergence(
        bundle["convergence"],
        arrival=arrival,
        supply=supply,
        children=children,
    )
    failed_stage = _verify_failed_stage(bundle["failed_stage"], children)
    if (convergence["outcome"] == "failed") != (failed_stage is not None):
        raise ContractError("FAILED_STAGE_CONVERGENCE_MISMATCH")
    frontier = _verify_frontier(bundle["frontier"])

    frontier_arrival = frontier["arrival"]
    frontier_matches_arrival = (
        frontier["current_source_revision"] == arrival["source_revision"]
        and frontier["current_source_tree"] == arrival["source_tree"]
    )
    if not frontier_matches_arrival:
        if (
            frontier_arrival is None
            or frontier_arrival["state"] != "terminal"
            or frontier_arrival["sequence"] != frontier["current_sequence"]
            or frontier_arrival["source_revision"]
            != frontier["current_source_revision"]
            or frontier_arrival["source_tree"] != frontier["current_source_tree"]
        ):
            raise ContractError("FRONTIER_LINEAGE_UNEXPLAINED")
    elif (
        frontier_arrival is not None
        and frontier_arrival["sequence"] == arrival["sequence"]
        and (
            frontier_arrival["source_revision"] != arrival["source_revision"]
            or frontier_arrival["source_tree"] != arrival["source_tree"]
        )
    ):
        raise ContractError("FRONTIER_ARRIVAL_LINEAGE_MISMATCH")

    if producer["run_id"] in {relay["run_id"], *(item["run_id"] for item in children)}:
        raise ContractError("RUN_ID_REUSE")
    if relay["run_id"] in {item["run_id"] for item in children}:
        raise ContractError("RUN_ID_REUSE")
    if convergence["outcome"] == "converged":
        if frontier["terminal_receipt_digest"] != convergence["receipt_digest"]:
            raise ContractError("FRONTIER_TERMINAL_RECEIPT_MISMATCH")
    for receipt in frontier["prior_successful_stage_receipts"]:
        if (
            receipt["source_revision"] != arrival["source_revision"]
            or receipt["source_tree"] != arrival["source_tree"]
            or receipt["supply_manifest_digest"]
            != supply["manifest"]["manifest_digest"]
        ):
            raise ContractError("STAGE_RECEIPT_LINEAGE_MISMATCH")

    expected_digest = sha256_digest(_payload_without(bundle, "bundle_digest"))
    if _digest(bundle["bundle_digest"], "ARRIVAL_BUNDLE_INVALID") != expected_digest:
        raise ContractError("ARRIVAL_BUNDLE_DIGEST_MISMATCH")
    canonical = deepcopy(bundle)
    canonical.update(
        arrival=arrival,
        producer=producer,
        supply=supply,
        relay=relay,
        child_runs=children,
        convergence=convergence,
        failed_stage=failed_stage,
        frontier=frontier,
    )
    return _new_validated(canonical)


def _supply_identity(supply: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        supply["artifact"]["artifact_id"],
        supply["artifact"]["content_digest"],
        supply["manifest"]["manifest_digest"],
        tuple(
            (item["name"], item["image_digest"])
            for item in supply["manifest"]["services"]
        ),
    )


def _disposition(payload: dict[str, Any]) -> tuple[str, str, str | None, str | None]:
    arrival = payload["arrival"]
    producer = payload["producer"]
    relay = payload["relay"]
    convergence = payload["convergence"]
    failed = payload["failed_stage"]
    frontier = payload["frontier"]
    active = frontier["arrival"]

    if active is not None and active["sequence"] > arrival["sequence"]:
        if active["state"] in {"queued", "active"} and active["owner_class"] == "same_train":
            return "serialize_wait", "newer_owned_arrival_active", None, None
        if active["state"] == "terminal":
            return "superseded", "newer_arrival_terminal", None, None
        return "blocked", "newer_arrival_unowned", None, None
    if frontier["current_sequence"] > arrival["sequence"]:
        if active is not None and active["state"] == "terminal":
            return "superseded", "current_frontier_newer", None, None
        return "blocked", "current_frontier_ambiguous", None, None

    if failed is not None:
        stages = {
            receipt["stage"] for receipt in frontier["prior_successful_stage_receipts"]
        }
        required = set(_REQUIRED_PREDECESSORS[failed["service"]])
        if (
            producer["conclusion"] == "success"
            and relay["conclusion"] == "failure"
            and convergence["outcome"] == "failed"
            and required.issubset(stages)
        ):
            return (
                "resume_failed_stage",
                "single_failed_child_with_bound_predecessors",
                failed["service"],
                failed["stage"],
            )
        return "blocked", "failed_stage_predecessor_incomplete", None, None

    supply_matches = _supply_identity(payload["supply"]) == _supply_identity(
        frontier["current_supply"]
    )
    live_rows = tuple(
        (item["name"], item["image_digest"]) for item in frontier["live_services"]
    )
    supply_rows = tuple(
        (item["name"], item["image_digest"])
        for item in payload["supply"]["manifest"]["services"]
    )
    if (
        producer["conclusion"] == "success"
        and relay["conclusion"] == "success"
        and convergence["outcome"] == "converged"
        and frontier["current_sequence"] == arrival["sequence"]
        and frontier["current_source_revision"] == arrival["source_revision"]
        and frontier["current_source_tree"] == arrival["source_tree"]
        and supply_matches
        and live_rows == supply_rows
    ):
        return "adopt_frontier", "exact_arrival_fully_converged", None, None
    if relay["conclusion"] == "in_progress" or convergence["outcome"] == "in_progress":
        return "serialize_wait", "current_arrival_active", None, None
    return "blocked", "verified_evidence_incomplete", None, None


def reconcile_arrival(
    bundle: Any,
    *,
    now_epoch: int,
    fixture_enabled: bool = False,
) -> dict[str, Any]:
    """Return one closed advisory disposition from a freshly verified bundle."""

    if not fixture_enabled:
        raise ContractError("UNCONFIGURED")
    validated = verify_arrival_bundle(bundle, now_epoch=now_epoch)
    payload = json.loads(validated.payload_json)
    disposition, reason, resume_service, resume_stage = _disposition(payload)
    frontier = payload["frontier"]
    result = {
        "schema": RESULT_SCHEMA,
        "state": "SHADOW",
        "selector": "UNCONFIGURED",
        "bundle_digest": validated.content_digest,
        "arrival_source_revision": payload["arrival"]["source_revision"],
        "arrival_source_tree": payload["arrival"]["source_tree"],
        "arrival_sequence": payload["arrival"]["sequence"],
        "frontier_source_revision": frontier["current_source_revision"],
        "frontier_source_tree": frontier["current_source_tree"],
        "frontier_sequence": frontier["current_sequence"],
        "producer_run_id": payload["producer"]["run_id"],
        "relay_run_id": payload["relay"]["run_id"],
        "supply_artifact_id": payload["supply"]["artifact"]["artifact_id"],
        "supply_manifest_digest": payload["supply"]["manifest"]["manifest_digest"],
        "convergence_receipt_digest": payload["convergence"]["receipt_digest"],
        "prior_stage_receipt_digests": [
            item["receipt_digest"]
            for item in frontier["prior_successful_stage_receipts"]
        ],
        "disposition": disposition,
        "reason_code": reason,
        "resume_service": resume_service,
        "resume_stage": resume_stage,
        "authority": {
            "dispatch": False,
            "cancel": False,
            "merge": False,
            "claim": False,
            "selector_activation": False,
            "provider_call": False,
            "source_mutation": False,
            "live_mutation": False,
        },
    }
    reject_secret_material(result)
    return result


def workflow_preflight(shadow_enabled: bool) -> dict[str, Any]:
    return {
        "schema": "leaf.platform-arrival-reconciliation-preflight.v2",
        "state": "UNCONFIGURED",
        "shadow_enabled": shadow_enabled,
        "selectors": {"arrival_frontier": "UNCONFIGURED"},
        "provider_calls": 0,
        "receipt_published": False,
        "dispatch_authorized": False,
        "cancel_authorized": False,
        "writer_acquisition_authorized": False,
        "live_mutation_authorized": False,
    }


def _boolean(value: str) -> bool:
    normalized = value.casefold()
    if normalized not in {"true", "false"}:
        raise argparse.ArgumentTypeError("expected true or false")
    return normalized == "true"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("workflow-preflight")
    preflight.add_argument("--shadow-enabled", type=_boolean, required=True)
    args = parser.parse_args(argv)
    if args.command == "workflow-preflight":
        print(json.dumps(workflow_preflight(args.shadow_enabled), sort_keys=True))
        return 78
    raise AssertionError("unreachable")


__all__ = [
    "ValidatedArrivalBundle",
    "reconcile_arrival",
    "verify_arrival_bundle",
    "workflow_preflight",
]


if __name__ == "__main__":
    raise SystemExit(main())
