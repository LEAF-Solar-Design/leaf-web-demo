#!/usr/bin/env python3
"""Dormant supply coalescing from independently verified full tokens."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from platform_semantic_eligibility import ContractError, sha256_digest
from platform_source_impact import (
    SERVICES,
    TrustedProducerRoots,
    _impact_from_validated,
    verify_producer_evidence_token,
)


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _exact(value: Any, keys: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ContractError(code)
    return dict(value)


def evaluate_supply_coalescing(
    document: Mapping[str, Any],
    *,
    trusted_roots: TrustedProducerRoots | Any,
    now_epoch: int,
    fixture_enabled: bool = False,
) -> dict[str, Any]:
    """Verify each movement token and plan at most one exact lineage."""

    if not fixture_enabled:
        raise ContractError("UNCONFIGURED")
    root = _exact(
        document,
        {"schema", "selector", "admission_window_digest", "movements"},
        "COALESCE_INPUT_INVALID",
    )
    if (
        root["schema"] != "leaf.platform-supply-coalesce-input.v3"
        or root["selector"] != "UNCONFIGURED"
        or not isinstance(root["admission_window_digest"], str)
        or _DIGEST.fullmatch(root["admission_window_digest"]) is None
        or not isinstance(root["movements"], list)
        or not 1 <= len(root["movements"]) <= 100
    ):
        raise ContractError("COALESCE_INPUT_INVALID")

    movements: list[dict[str, Any]] = []
    token_digests: set[str] = set()
    for raw in root["movements"]:
        movement = _exact(
            raw,
            {"producer_token", "relay_base_tree", "deferred"},
            "COALESCE_MOVEMENT_INVALID",
        )
        validated = verify_producer_evidence_token(
            movement["producer_token"], trusted_roots, now_epoch=now_epoch
        )
        if validated.content_digest in token_digests:
            raise ContractError("COALESCE_EVIDENCE_REPLAY")
        token_digests.add(validated.content_digest)
        impact = _impact_from_validated(
            validated,
            relay_base_tree=movement["relay_base_tree"],
            deferred=movement["deferred"],
        )
        movements.append({"validated": validated, "impact": impact})

    first = movements[0]["validated"]
    changed_services: set[str] = set()
    product_impact = False
    scope_mismatch = False
    for movement in movements:
        validated = movement["validated"]
        impact = movement["impact"]
        product_impact = product_impact or impact["classification"] != "nil_impact"
        changed_services.update(impact["affected_services"])
        if (
            validated.release_scope_digest != first.release_scope_digest
            or validated.tenant_binding_digest != first.tenant_binding_digest
            or validated.approval_scope_digest != first.approval_scope_digest
            or validated.rollback_digest != first.rollback_digest
            or validated.verifier_digest != first.verifier_digest
            or validated.environment != first.environment
        ):
            scope_mismatch = True

    if scope_mismatch:
        decision = "refuse"
        reason = "producer_evidence_changed"
        lineage = None
    elif product_impact:
        decision = "refuse"
        reason = "product_impact_present"
        lineage = None
    else:
        decision = "plan"
        reason = "complete_supply_equivalent"
        lineage = sha256_digest(
            {
                "admission_window_digest": root["admission_window_digest"],
                "release_scope_digest": first.release_scope_digest,
                "movements": [
                    {
                        "producer_token_digest": item[
                            "validated"
                        ].content_digest,
                        "candidate_source_revision": item[
                            "validated"
                        ].candidate_source_revision,
                        "candidate_source_tree": item[
                            "validated"
                        ].candidate_source_tree,
                        "impact_digest": item["impact"]["impact_digest"],
                    }
                    for item in movements
                ],
                "services": list(first.services),
            }
        )

    evidence_chain = sha256_digest(
        [
            {
                "producer_token_digest": item["validated"].content_digest,
                "release_scope_digest": item[
                    "validated"
                ].release_scope_digest,
                "impact_digest": item["impact"]["impact_digest"],
            }
            for item in movements
        ]
    )
    return {
        "schema": "leaf.platform-supply-coalesce.v3",
        "state": "SHADOW",
        "decision": decision,
        "reason_code": reason,
        "movement_count": len(movements),
        "affected_services": [
            name for name in SERVICES if name in changed_services
        ],
        "admission_window_digest": root["admission_window_digest"],
        "release_scope_digest": first.release_scope_digest,
        "evidence_chain_digest": evidence_chain,
        "planned_lineage_digest": lineage,
        "selector_activation_authorized": False,
        "supply_mint_authorized": False,
    }


def workflow_preflight() -> dict[str, Any]:
    return {
        "schema": "leaf.platform-supply-coalesce-preflight.v1",
        "state": "UNCONFIGURED",
        "selector_activation_authorized": False,
        "supply_mint_authorized": False,
    }


__all__ = ["SERVICES", "evaluate_supply_coalescing", "workflow_preflight"]
