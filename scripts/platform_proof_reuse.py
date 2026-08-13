#!/usr/bin/env python3
"""Dormant semantic-proof reuse from two independently verified tokens."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from platform_semantic_eligibility import ContractError, sha256_digest
from platform_source_impact import (
    TrustedProducerRoots,
    _impact_from_validated,
    verify_producer_evidence_token,
)


PROFILES = ("browser", "cad", "solar_cad", "ios")


def _exact(value: Any, keys: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ContractError(code)
    return dict(value)


def _profiles(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or tuple(value) != PROFILES:
        raise ContractError("WORKSPACE_PROFILE_SET_INVALID")
    return tuple(value)


def evaluate_proof_reuse(
    document: Mapping[str, Any],
    *,
    current_trusted_roots: TrustedProducerRoots | Any,
    admitted_trusted_roots: TrustedProducerRoots | Any,
    now_epoch: int,
    fixture_enabled: bool = False,
) -> dict[str, Any]:
    """Verify both full tokens before evaluating their proof relation."""

    if not fixture_enabled:
        raise ContractError("UNCONFIGURED")
    root = _exact(
        document,
        {
            "schema",
            "selector",
            "current_token",
            "admitted_token",
            "prior_receipt",
            "candidate",
        },
        "PROOF_REUSE_INPUT_INVALID",
    )
    if (
        root["schema"] != "leaf.platform-proof-reuse-input.v3"
        or root["selector"] != "UNCONFIGURED"
    ):
        raise ContractError("PROOF_REUSE_INPUT_INVALID")
    current = verify_producer_evidence_token(
        root["current_token"], current_trusted_roots, now_epoch=now_epoch
    )
    admitted = verify_producer_evidence_token(
        root["admitted_token"], admitted_trusted_roots, now_epoch=now_epoch
    )
    prior = _exact(
        root["prior_receipt"],
        {
            "terminal_state",
            "verifier_result",
            "rollback_result",
            "product_mutation_result",
            "receipt_digest",
        },
        "PRIOR_RECEIPT_INVALID",
    )
    candidate = _exact(
        root["candidate"],
        {
            "current_lineage_digest",
            "admitted_lineage_digest",
            "workspace_profiles",
            "lineage_complete",
            "relay_base_tree",
            "deferred",
        },
        "PROOF_REUSE_CANDIDATE_INVALID",
    )
    profiles = _profiles(candidate["workspace_profiles"])
    if not isinstance(candidate["lineage_complete"], bool):
        raise ContractError("LINEAGE_INVALID")
    current_impact = _impact_from_validated(
        current,
        relay_base_tree=candidate["relay_base_tree"],
        deferred=candidate["deferred"],
    )

    exact_continuity = (
        current.tenant_binding_digest == admitted.tenant_binding_digest
        and current.approval_scope_digest == admitted.approval_scope_digest
        and current.deployment_identity_digest
        == admitted.deployment_identity_digest
        and current.deployment_identity_body_digest
        == admitted.deployment_identity_body_digest
        and current.rollback_digest == admitted.rollback_digest
        and current.verifier_digest == admitted.verifier_digest
        and current.topology_digest == admitted.topology_digest
        and current.environment == admitted.environment
        and current.base_source_revision == admitted.base_source_revision
        and current.base_source_tree == admitted.base_source_tree
        and current.candidate_source_revision
        == admitted.candidate_source_revision
        and current.candidate_source_tree == admitted.candidate_source_tree
        and current.release_scope_digest == admitted.release_scope_digest
        and current.graph_digest == admitted.graph_digest
        and current.supply_digest == admitted.supply_digest
        and current.services == admitted.services
    )
    checks = (
        (
            prior["terminal_state"] == "terminal_green",
            "prior_not_terminal_green",
        ),
        (prior["verifier_result"] == "pass", "verifier_not_green"),
        (prior["rollback_result"] == "pass", "rollback_not_green"),
        (
            prior["product_mutation_result"] == "clean",
            "product_mutation_not_clean",
        ),
        (
            prior["receipt_digest"] == admitted.terminal_receipt_digest,
            "receipt_binding_mismatch",
        ),
        (candidate["lineage_complete"], "lineage_incomplete"),
        (
            current_impact["classification"] == "nil_impact",
            "source_impact_not_nil",
        ),
        (
            candidate["current_lineage_digest"]
            == current.release_lineage_digest,
            "current_lineage_mismatch",
        ),
        (
            candidate["admitted_lineage_digest"]
            == admitted.release_lineage_digest,
            "admitted_lineage_mismatch",
        ),
        (exact_continuity, "token_continuity_mismatch"),
    )
    failed = next((reason for passed, reason in checks if not passed), None)
    decision = "reuse" if failed is None else "fresh_proof"
    reason = "proof_reuse_exact" if failed is None else failed
    attachment = None
    if decision == "reuse":
        attachment = sha256_digest(
            {
                "current_token_digest": current.content_digest,
                "admitted_token_digest": admitted.content_digest,
                "admitted_lineage_digest": admitted.release_lineage_digest,
                "prior_receipt_digest": prior["receipt_digest"],
                "deployment_identity_digest": current.deployment_identity_digest,
                "release_scope_digest": current.release_scope_digest,
                "workspace_profiles": list(profiles),
            }
        )
    return {
        "schema": "leaf.platform-proof-reuse.v3",
        "state": "SHADOW",
        "decision": decision,
        "reason_code": reason,
        "admitted_lineage_digest": admitted.release_lineage_digest,
        "prior_receipt_digest": prior["receipt_digest"],
        "deployment_identity_digest": current.deployment_identity_digest,
        "current_token_digest": current.content_digest,
        "admitted_token_digest": admitted.content_digest,
        "release_scope_digest": current.release_scope_digest,
        "workspace_profile_count": len(profiles),
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
