#!/usr/bin/env python3
"""Dormant semantic-proof reuse evaluation for one admitted lineage."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from platform_semantic_eligibility import ContractError, sha256_digest


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
PROFILES = ("browser", "cad", "solar_cad", "ios")


def _exact(value: Any, keys: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ContractError(code)
    return dict(value)


def _digest(value: Any, code: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ContractError(code)
    return value


def _sha(value: Any, code: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ContractError(code)
    return value


def _profiles(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or tuple(value) != PROFILES:
        raise ContractError("WORKSPACE_PROFILE_SET_INVALID")
    return tuple(value)


def evaluate_proof_reuse(
    document: Mapping[str, Any], *, fixture_enabled: bool = False
) -> dict[str, Any]:
    """Return reuse only when every terminal and identity predicate matches."""

    if not fixture_enabled:
        raise ContractError("UNCONFIGURED")
    root = _exact(
        document,
        {"schema", "selector", "prior_receipt", "candidate"},
        "PROOF_REUSE_INPUT_INVALID",
    )
    if root["schema"] != "leaf.platform-proof-reuse-input.v2" or root["selector"] != "UNCONFIGURED":
        raise ContractError("PROOF_REUSE_INPUT_INVALID")
    prior = _exact(
        root["prior_receipt"],
        {
            "terminal_state",
            "verifier_result",
            "rollback_result",
            "product_mutation_result",
            "tenant_set_digest",
            "approval_scope_digest",
            "identity_shape_digest",
            "rollback_digest",
            "verifier_digest",
            "producer_graph_digest",
            "producer_supply_digest",
            "release_scope_digest",
            "source_impact_digest",
            "lineage_digest",
            "receipt_digest",
            "workspace_profiles",
            "lineage_complete",
        },
        "PRIOR_RECEIPT_INVALID",
    )
    candidate = _exact(
        root["candidate"],
        {
            "tenant_set_digest",
            "approval_scope_digest",
            "identity_shape_digest",
            "rollback_digest",
            "verifier_digest",
            "source_revision",
            "admitted_source_revision",
            "source_tree",
            "admitted_source_tree",
            "producer_graph_digest",
            "admitted_producer_graph_digest",
            "producer_supply_digest",
            "admitted_producer_supply_digest",
            "producer_evidence_digest",
            "admitted_producer_evidence_digest",
            "evidence_binding_digest",
            "admitted_evidence_binding_digest",
            "release_scope_digest",
            "admitted_release_scope_digest",
            "source_impact_digest",
            "admitted_source_impact_digest",
            "source_impact_classification",
            "predecessor_lineage_digest",
            "admitted_lineage_digest",
            "workspace_profiles",
            "lineage_complete",
        },
        "PROOF_REUSE_CANDIDATE_INVALID",
    )
    for value in (
        prior["tenant_set_digest"],
        prior["approval_scope_digest"],
        prior["identity_shape_digest"],
        prior["rollback_digest"],
        prior["verifier_digest"],
        prior["producer_graph_digest"],
        prior["producer_supply_digest"],
        prior["release_scope_digest"],
        prior["source_impact_digest"],
        prior["lineage_digest"],
        prior["receipt_digest"],
        candidate["tenant_set_digest"],
        candidate["approval_scope_digest"],
        candidate["identity_shape_digest"],
        candidate["rollback_digest"],
        candidate["verifier_digest"],
        candidate["producer_graph_digest"],
        candidate["admitted_producer_graph_digest"],
        candidate["producer_supply_digest"],
        candidate["admitted_producer_supply_digest"],
        candidate["producer_evidence_digest"],
        candidate["admitted_producer_evidence_digest"],
        candidate["evidence_binding_digest"],
        candidate["admitted_evidence_binding_digest"],
        candidate["release_scope_digest"],
        candidate["admitted_release_scope_digest"],
        candidate["source_impact_digest"],
        candidate["admitted_source_impact_digest"],
        candidate["predecessor_lineage_digest"],
        candidate["admitted_lineage_digest"],
    ):
        _digest(value, "PROOF_REUSE_DIGEST_INVALID")
    source_revision = _sha(candidate["source_revision"], "PROOF_REUSE_SOURCE_INVALID")
    source_tree = _sha(candidate["source_tree"], "PROOF_REUSE_SOURCE_INVALID")
    admitted_source_revision = _sha(
        candidate["admitted_source_revision"], "PROOF_REUSE_SOURCE_INVALID"
    )
    admitted_source_tree = _sha(
        candidate["admitted_source_tree"], "PROOF_REUSE_SOURCE_INVALID"
    )
    prior_profiles = _profiles(prior["workspace_profiles"])
    candidate_profiles = _profiles(candidate["workspace_profiles"])
    if not isinstance(prior["lineage_complete"], bool) or not isinstance(candidate["lineage_complete"], bool):
        raise ContractError("LINEAGE_INVALID")

    checks = (
        (prior["terminal_state"] == "terminal_green", "prior_not_terminal_green"),
        (prior["verifier_result"] == "pass", "verifier_not_green"),
        (prior["rollback_result"] == "pass", "rollback_not_green"),
        (prior["product_mutation_result"] == "clean", "product_mutation_not_clean"),
        (prior["lineage_complete"] and candidate["lineage_complete"], "lineage_incomplete"),
        (candidate["source_impact_classification"] == "nil_impact", "source_impact_not_nil"),
        (candidate["predecessor_lineage_digest"] == prior["lineage_digest"], "lineage_mismatch"),
        (candidate["tenant_set_digest"] == prior["tenant_set_digest"], "tenant_set_mismatch"),
        (candidate["approval_scope_digest"] == prior["approval_scope_digest"], "approval_scope_mismatch"),
        (candidate["identity_shape_digest"] == prior["identity_shape_digest"], "identity_shape_mismatch"),
        (
            candidate["rollback_digest"] == prior["rollback_digest"],
            "rollback_binding_mismatch",
        ),
        (
            candidate["verifier_digest"] == prior["verifier_digest"],
            "verifier_binding_mismatch",
        ),
        (
            source_revision == admitted_source_revision
            and source_tree == admitted_source_tree,
            "source_binding_mismatch",
        ),
        (
            candidate["producer_graph_digest"]
            == candidate["admitted_producer_graph_digest"]
            == prior["producer_graph_digest"],
            "graph_binding_mismatch",
        ),
        (
            candidate["producer_supply_digest"]
            == candidate["admitted_producer_supply_digest"]
            == prior["producer_supply_digest"],
            "supply_binding_mismatch",
        ),
        (
            candidate["producer_evidence_digest"]
            == candidate["admitted_producer_evidence_digest"],
            "producer_evidence_mismatch",
        ),
        (
            candidate["evidence_binding_digest"]
            == candidate["admitted_evidence_binding_digest"],
            "evidence_binding_mismatch",
        ),
        (
            candidate["release_scope_digest"]
            == candidate["admitted_release_scope_digest"]
            == prior["release_scope_digest"],
            "release_scope_mismatch",
        ),
        (
            candidate["source_impact_digest"]
            == candidate["admitted_source_impact_digest"],
            "source_impact_mismatch",
        ),
        (candidate_profiles == prior_profiles, "workspace_profile_mismatch"),
    )
    failed = next((reason for passed, reason in checks if not passed), None)
    decision = "reuse" if failed is None else "fresh_proof"
    reason = "proof_reuse_exact" if failed is None else failed
    attachment = None
    if decision == "reuse":
        attachment = sha256_digest(
            {
                "admitted_lineage_digest": candidate["admitted_lineage_digest"],
                "prior_receipt_digest": prior["receipt_digest"],
                "identity_shape_digest": candidate["identity_shape_digest"],
                "producer_evidence_digest": candidate["producer_evidence_digest"],
                "evidence_binding_digest": candidate["evidence_binding_digest"],
                "release_scope_digest": candidate["release_scope_digest"],
                "source_revision": source_revision,
                "source_tree": source_tree,
                "workspace_profiles": list(candidate_profiles),
            }
        )
    return {
        "schema": "leaf.platform-proof-reuse.v2",
        "state": "SHADOW",
        "decision": decision,
        "reason_code": reason,
        "admitted_lineage_digest": candidate["admitted_lineage_digest"],
        "prior_receipt_digest": prior["receipt_digest"],
        "identity_shape_digest": candidate["identity_shape_digest"],
        "producer_evidence_digest": candidate["producer_evidence_digest"],
        "evidence_binding_digest": candidate["evidence_binding_digest"],
        "release_scope_digest": candidate["release_scope_digest"],
        "workspace_profile_count": len(candidate_profiles),
        "reuse_attachment_digest": attachment,
        "selector_activation_authorized": False,
        "proof_execution_authorized": False,
    }


def workflow_preflight() -> dict[str, Any]:
    return {
        "schema": "leaf.platform-proof-reuse-preflight.v1",
        "state": "UNCONFIGURED",
        "selector_activation_authorized": False,
        "proof_execution_authorized": False,
    }


__all__ = ["PROFILES", "evaluate_proof_reuse", "workflow_preflight"]
