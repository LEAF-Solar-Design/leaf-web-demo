#!/usr/bin/env python3
"""Dormant producer-bound source-impact classification.

Only an independently anchored, content-addressed producer envelope may yield
``nil_impact``. Production use remains intentionally unconfigured.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import re
from typing import Any

from platform_semantic_eligibility import ContractError, sha256_digest


SERVICES = ("app", "broker", "canonical-worker", "harness", "web")
PRODUCER_REPOSITORY = "LEAF-Solar-Design/leaf-web-demo"
PRODUCER_WORKFLOW = ".github/workflows/build-platform-images.yml"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ARTIFACT_NAME = re.compile(r"^staging-supply-set-[0-9a-f]{40}-attempt-[1-9][0-9]*$")
_INPUT_CLASSES = (
    "base_images",
    "build_args",
    "dependencies",
    "dockerfile",
    "required_config",
    "source_inputs",
    "toolchain",
)
_MAX_EVIDENCE_LIFETIME_SECONDS = 31 * 24 * 60 * 60


def _exact(value: Any, keys: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ContractError(code)
    return dict(value)


def _sha(value: Any, code: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ContractError(code)
    return value


def _digest(value: Any, code: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ContractError(code)
    return value


def _integer(value: Any, minimum: int, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractError(code)
    return value


def _canonical_graph(raw: Any, supply: Mapping[str, str]) -> tuple[dict[str, Any], list[str], list[str]]:
    graph = _exact(
        raw,
        {"schema", "version", "complete", "services"},
        "PRODUCER_GRAPH_INVALID",
    )
    if (
        graph["schema"] != "leaf.platform-producer-input-graph.v1"
        or graph["version"] != 1
        or not isinstance(graph["complete"], bool)
        or not isinstance(graph["services"], list)
        or len(graph["services"]) != len(SERVICES)
    ):
        raise ContractError("PRODUCER_GRAPH_INVALID")

    canonical_services: list[dict[str, Any]] = []
    changed: list[str] = []
    incomplete: list[str] = []
    for expected_name, raw_service in zip(SERVICES, graph["services"], strict=True):
        service = _exact(
            raw_service,
            {
                "name",
                "complete",
                "image_digest",
                "old_fingerprint",
                "new_fingerprint",
                "input_classes",
            },
            "PRODUCER_GRAPH_INVALID",
        )
        if service["name"] != expected_name or not isinstance(service["complete"], bool):
            raise ContractError("SERVICE_SET_INVALID")
        image_digest = _digest(service["image_digest"], "FINGERPRINT_INVALID")
        if image_digest != supply[expected_name]:
            raise ContractError("PRODUCER_GRAPH_SUPPLY_MISMATCH")
        old_fingerprint = _digest(service["old_fingerprint"], "FINGERPRINT_INVALID")
        new_fingerprint = _digest(service["new_fingerprint"], "FINGERPRINT_INVALID")
        classes = _exact(
            service["input_classes"], set(_INPUT_CLASSES), "INPUT_CLASS_INVALID"
        )
        if any(not isinstance(classes[key], bool) for key in _INPUT_CLASSES):
            raise ContractError("INPUT_CLASS_INVALID")
        if not service["complete"] or not all(classes.values()):
            incomplete.append(expected_name)
        if old_fingerprint != new_fingerprint:
            changed.append(expected_name)
        canonical_services.append(deepcopy(service))
    return (
        {
            "schema": graph["schema"],
            "version": graph["version"],
            "complete": graph["complete"],
            "services": canonical_services,
        },
        changed,
        incomplete,
    )


def verify_producer_evidence(
    envelope_value: Any,
    trust_anchor_value: Any,
    *,
    now_epoch: int,
) -> dict[str, Any]:
    """Validate one envelope against an independently obtained trust anchor."""

    now = _integer(now_epoch, 0, "PRODUCER_EVIDENCE_TIME_INVALID")
    envelope = _exact(
        envelope_value,
        {
            "schema",
            "version",
            "producer",
            "artifact",
            "release",
            "supply",
            "producer_graph",
            "producer_graph_digest",
            "supply_digest",
            "issued_at_epoch",
            "expires_at_epoch",
            "content_digest",
        },
        "PRODUCER_EVIDENCE_INVALID",
    )
    if envelope["schema"] != "leaf.platform-producer-evidence-envelope.v1" or envelope["version"] != 1:
        raise ContractError("PRODUCER_EVIDENCE_VERSION_INVALID")

    producer = _exact(
        envelope["producer"],
        {"repository", "workflow", "workflow_blob", "run_id", "repository_id"},
        "PRODUCER_IDENTITY_INVALID",
    )
    if producer["repository"] != PRODUCER_REPOSITORY or producer["workflow"] != PRODUCER_WORKFLOW:
        raise ContractError("PRODUCER_IDENTITY_INVALID")
    workflow_blob = _sha(producer["workflow_blob"], "PRODUCER_IDENTITY_INVALID")
    run_id = _integer(producer["run_id"], 1, "PRODUCER_IDENTITY_INVALID")
    repository_id = _integer(producer["repository_id"], 1, "PRODUCER_IDENTITY_INVALID")

    artifact = _exact(
        envelope["artifact"],
        {"artifact_id", "artifact_name", "archive_digest", "manifest_digest"},
        "PRODUCER_ARTIFACT_INVALID",
    )
    artifact_id = _integer(artifact["artifact_id"], 1, "PRODUCER_ARTIFACT_INVALID")
    if not isinstance(artifact["artifact_name"], str) or _ARTIFACT_NAME.fullmatch(artifact["artifact_name"]) is None:
        raise ContractError("PRODUCER_ARTIFACT_INVALID")
    archive_digest = _digest(artifact["archive_digest"], "PRODUCER_ARTIFACT_INVALID")
    manifest_digest = _digest(artifact["manifest_digest"], "PRODUCER_ARTIFACT_INVALID")

    release = _exact(
        envelope["release"],
        {
            "source_revision",
            "source_tree",
            "base_source_revision",
            "base_source_tree",
            "terminal_receipt_digest",
            "release_lineage_digest",
            "tenant_set_digest",
            "identity_shape_digest",
            "approval_scope_digest",
            "rollback_digest",
            "verifier_digest",
        },
        "PRODUCER_RELEASE_INVALID",
    )
    source_revision = _sha(release["source_revision"], "PRODUCER_RELEASE_INVALID")
    source_tree = _sha(release["source_tree"], "PRODUCER_RELEASE_INVALID")
    base_source_revision = _sha(
        release["base_source_revision"], "PRODUCER_RELEASE_INVALID"
    )
    base_source_tree = _sha(release["base_source_tree"], "PRODUCER_RELEASE_INVALID")
    if artifact["artifact_name"] != f"staging-supply-set-{source_revision}-attempt-1":
        raise ContractError("PRODUCER_ARTIFACT_SOURCE_MISMATCH")
    for key in (
        "terminal_receipt_digest",
        "release_lineage_digest",
        "tenant_set_digest",
        "identity_shape_digest",
        "approval_scope_digest",
        "rollback_digest",
        "verifier_digest",
    ):
        _digest(release[key], "PRODUCER_RELEASE_INVALID")

    supply = _exact(envelope["supply"], set(SERVICES), "PRODUCER_SUPPLY_INVALID")
    for name in SERVICES:
        _digest(supply[name], "PRODUCER_SUPPLY_INVALID")
    canonical_graph, changed, incomplete = _canonical_graph(envelope["producer_graph"], supply)
    graph_digest = _digest(envelope["producer_graph_digest"], "PRODUCER_GRAPH_INVALID")
    if graph_digest != sha256_digest(canonical_graph):
        raise ContractError("PRODUCER_GRAPH_DIGEST_MISMATCH")
    supply_digest = _digest(envelope["supply_digest"], "PRODUCER_SUPPLY_INVALID")
    if supply_digest != sha256_digest({name: supply[name] for name in SERVICES}):
        raise ContractError("PRODUCER_SUPPLY_DIGEST_MISMATCH")

    issued_at = _integer(envelope["issued_at_epoch"], 0, "PRODUCER_EVIDENCE_TIME_INVALID")
    expires_at = _integer(envelope["expires_at_epoch"], 1, "PRODUCER_EVIDENCE_TIME_INVALID")
    if (
        issued_at > now
        or now >= expires_at
        or expires_at <= issued_at
        or expires_at - issued_at > _MAX_EVIDENCE_LIFETIME_SECONDS
    ):
        raise ContractError("PRODUCER_EVIDENCE_EXPIRED")

    producer_identity_digest = sha256_digest(
        {
            "repository": producer["repository"],
            "workflow": producer["workflow"],
            "workflow_blob": workflow_blob,
            "run_id": run_id,
            "repository_id": repository_id,
        }
    )
    release_scope_digest = sha256_digest(
        {
            "producer_identity_digest": producer_identity_digest,
            "artifact_id": artifact_id,
            "archive_digest": archive_digest,
            "manifest_digest": manifest_digest,
            "terminal_receipt_digest": release["terminal_receipt_digest"],
            "release_lineage_digest": release["release_lineage_digest"],
            "tenant_set_digest": release["tenant_set_digest"],
            "identity_shape_digest": release["identity_shape_digest"],
            "approval_scope_digest": release["approval_scope_digest"],
            "rollback_digest": release["rollback_digest"],
            "verifier_digest": release["verifier_digest"],
            "producer_graph_digest": graph_digest,
            "supply_digest": supply_digest,
        }
    )
    normalized_without_digest = {
        "schema": envelope["schema"],
        "version": envelope["version"],
        "producer": deepcopy(producer),
        "artifact": deepcopy(artifact),
        "release": deepcopy(release),
        "supply": {name: supply[name] for name in SERVICES},
        "producer_graph": canonical_graph,
        "producer_graph_digest": graph_digest,
        "supply_digest": supply_digest,
        "issued_at_epoch": issued_at,
        "expires_at_epoch": expires_at,
    }
    content_digest = _digest(envelope["content_digest"], "PRODUCER_EVIDENCE_INVALID")
    if content_digest != sha256_digest(normalized_without_digest):
        raise ContractError("PRODUCER_EVIDENCE_CONTENT_MISMATCH")

    trust_anchor = _exact(
        trust_anchor_value,
        {
            "schema",
            "version",
            "producer_identity_digest",
            "workflow_blob",
            "run_id",
            "artifact_id",
            "artifact_archive_digest",
            "manifest_digest",
            "terminal_receipt_digest",
            "source_revision",
            "source_tree",
            "base_source_revision",
            "base_source_tree",
            "producer_graph_digest",
            "supply_digest",
            "release_scope_digest",
            "envelope_content_digest",
            "expires_at_epoch",
        },
        "PRODUCER_TRUST_ANCHOR_INVALID",
    )
    if trust_anchor["schema"] != "leaf.platform-producer-trust-anchor.v1" or trust_anchor["version"] != 1:
        raise ContractError("PRODUCER_TRUST_ANCHOR_INVALID")
    expected_anchor = {
        "schema": "leaf.platform-producer-trust-anchor.v1",
        "version": 1,
        "producer_identity_digest": producer_identity_digest,
        "workflow_blob": workflow_blob,
        "run_id": run_id,
        "artifact_id": artifact_id,
        "artifact_archive_digest": archive_digest,
        "manifest_digest": manifest_digest,
        "terminal_receipt_digest": release["terminal_receipt_digest"],
        "source_revision": source_revision,
        "source_tree": source_tree,
        "base_source_revision": base_source_revision,
        "base_source_tree": base_source_tree,
        "producer_graph_digest": graph_digest,
        "supply_digest": supply_digest,
        "release_scope_digest": release_scope_digest,
        "envelope_content_digest": content_digest,
        "expires_at_epoch": expires_at,
    }
    if trust_anchor != expected_anchor:
        raise ContractError("PRODUCER_EVIDENCE_TRUST_MISMATCH")

    evidence_binding_digest = sha256_digest(
        {
            "producer_evidence_digest": content_digest,
            "release_scope_digest": release_scope_digest,
            "source_revision": source_revision,
            "source_tree": source_tree,
            "base_source_revision": base_source_revision,
            "base_source_tree": base_source_tree,
        }
    )
    return {
        "producer_evidence_digest": content_digest,
        "producer_identity_digest": producer_identity_digest,
        "source_revision": source_revision,
        "source_tree": source_tree,
        "base_source_revision": base_source_revision,
        "base_source_tree": base_source_tree,
        "producer_graph_digest": graph_digest,
        "supply_digest": supply_digest,
        "terminal_receipt_digest": release["terminal_receipt_digest"],
        "release_scope_digest": release_scope_digest,
        "evidence_binding_digest": evidence_binding_digest,
        "graph_complete": canonical_graph["complete"],
        "changed_services": changed,
        "incomplete_services": incomplete,
    }


def classify_source_impact(
    document: Mapping[str, Any],
    *,
    producer_trust_anchor: Mapping[str, Any] | None = None,
    now_epoch: int | None = None,
    fixture_enabled: bool = False,
) -> dict[str, Any]:
    """Return a closed impact decision for one exact trusted envelope."""

    if not fixture_enabled:
        raise ContractError("UNCONFIGURED")
    if producer_trust_anchor is None or now_epoch is None:
        raise ContractError("PRODUCER_EVIDENCE_UNCONFIGURED")
    root = _exact(
        document,
        {
            "schema",
            "selector",
            "old_tree",
            "new_tree",
            "relay_base_tree",
            "deferred",
            "producer_evidence",
        },
        "SOURCE_IMPACT_INPUT_INVALID",
    )
    if (
        root["schema"] != "leaf.platform-source-impact-input.v2"
        or root["selector"] != "UNCONFIGURED"
        or not isinstance(root["deferred"], bool)
    ):
        raise ContractError("SOURCE_IMPACT_INPUT_INVALID")
    old_tree = _sha(root["old_tree"], "SOURCE_TREE_INVALID")
    new_tree = _sha(root["new_tree"], "SOURCE_TREE_INVALID")
    relay_base = _sha(root["relay_base_tree"], "SOURCE_TREE_INVALID")
    verified = verify_producer_evidence(
        root["producer_evidence"], producer_trust_anchor, now_epoch=now_epoch
    )
    if new_tree != verified["source_tree"] or old_tree != verified["base_source_tree"]:
        raise ContractError("PRODUCER_SOURCE_TREE_MISMATCH")

    changed = verified["changed_services"]
    incomplete = verified["incomplete_services"]
    if root["deferred"] and relay_base != old_tree:
        classification = "product_impact"
        affected = list(SERVICES)
        reason = "deferred_reclassification_required"
    elif not verified["graph_complete"] or incomplete:
        classification = "product_impact"
        affected = list(SERVICES) if not verified["graph_complete"] else [
            name for name in SERVICES if name in incomplete or name in changed
        ]
        reason = "producer_graph_incomplete"
    elif changed:
        classification = "product_impact"
        affected = [name for name in SERVICES if name in changed]
        reason = "producer_input_changed"
    else:
        classification = "nil_impact"
        affected = []
        reason = "producer_inputs_equal"

    impact_digest = sha256_digest(
        {
            "classification": classification,
            "reason_code": reason,
            "affected_services": affected,
            "old_tree": old_tree,
            "new_tree": new_tree,
            "relay_base_tree": relay_base,
            "producer_evidence_digest": verified["producer_evidence_digest"],
            "evidence_binding_digest": verified["evidence_binding_digest"],
        }
    )
    return {
        "schema": "leaf.platform-source-impact.v2",
        "state": "SHADOW",
        "classification": classification,
        "reason_code": reason,
        "affected_services": affected,
        "old_tree": old_tree,
        "new_tree": new_tree,
        "relay_base_tree": relay_base,
        "impact_digest": impact_digest,
        "producer_evidence_digest": verified["producer_evidence_digest"],
        "evidence_binding_digest": verified["evidence_binding_digest"],
        "producer_identity_digest": verified["producer_identity_digest"],
        "producer_source_revision": verified["source_revision"],
        "producer_base_source_revision": verified["base_source_revision"],
        "producer_graph_digest": verified["producer_graph_digest"],
        "producer_supply_digest": verified["supply_digest"],
        "terminal_receipt_digest": verified["terminal_receipt_digest"],
        "release_scope_digest": verified["release_scope_digest"],
        "selector_activation_authorized": False,
    }


def workflow_preflight() -> dict[str, Any]:
    return {
        "schema": "leaf.platform-source-impact-preflight.v1",
        "state": "UNCONFIGURED",
        "selector_activation_authorized": False,
        "deployment_effect": False,
    }


__all__ = [
    "PRODUCER_REPOSITORY",
    "PRODUCER_WORKFLOW",
    "SERVICES",
    "classify_source_impact",
    "verify_producer_evidence",
    "workflow_preflight",
]
