#!/usr/bin/env python3
"""Dormant release admission for the unified Leaf settlement window."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
import re
from typing import Any

from platform_semantic_eligibility import ContractError, sha256_digest


_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


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


def _bounded_integer(value: Any, minimum: int, maximum: int, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ContractError(code)
    return value


def _urgent_authority_valid(
    authority: Any,
    *,
    expected_scope: str,
    displaced_train: str,
    producer_evidence_digest: str,
    evidence_binding_digest: str,
    release_scope_digest: str,
) -> bool:
    if not isinstance(authority, Mapping):
        return False
    if set(authority) != {
        "approval_scope_digest",
        "displaced_train_digest",
        "rollback_digest",
        "producer_evidence_digest",
        "evidence_binding_digest",
        "release_scope_digest",
    }:
        return False
    return (
        authority.get("approval_scope_digest") == expected_scope
        and authority.get("displaced_train_digest") == displaced_train
        and authority.get("producer_evidence_digest") == producer_evidence_digest
        and authority.get("evidence_binding_digest") == evidence_binding_digest
        and authority.get("release_scope_digest") == release_scope_digest
        and isinstance(authority.get("rollback_digest"), str)
        and _DIGEST.fullmatch(authority["rollback_digest"]) is not None
    )


def evaluate_release_admission(
    document: Mapping[str, Any], *, fixture_enabled: bool = False
) -> dict[str, Any]:
    """Return admit, hold, or coalesce without acquiring any writer."""

    if not fixture_enabled:
        raise ContractError("UNCONFIGURED")
    root = _exact(
        document,
        {"schema", "selector", "candidate", "settlement", "limits", "urgent_authority"},
        "ADMISSION_INPUT_INVALID",
    )
    if root["schema"] != "leaf.platform-release-admission-input.v2" or root["selector"] != "UNCONFIGURED":
        raise ContractError("ADMISSION_INPUT_INVALID")
    candidate = _exact(
        root["candidate"],
        {
            "source_tree",
            "source_revision",
            "impact_classification",
            "impact_digest",
            "producer_evidence_digest",
            "evidence_binding_digest",
            "release_scope_digest",
            "classification_base_tree",
            "approval_scope_digest",
            "queue_age_seconds",
            "queue_count",
            "urgent",
        },
        "CANDIDATE_INVALID",
    )
    settlement = _exact(
        root["settlement"],
        {
            "active",
            "census_started",
            "terminal_receipt_published",
            "release_ready",
            "identity_restamp_active",
            "active_writers",
            "open_markers",
            "census_head",
            "source_head",
            "expected_approval_scope_digest",
            "prior_train_digest",
            "expected_source_revision",
            "expected_source_tree",
            "expected_impact_digest",
            "expected_producer_evidence_digest",
            "expected_evidence_binding_digest",
            "expected_release_scope_digest",
        },
        "SETTLEMENT_INVALID",
    )
    limits = _exact(
        root["limits"], {"max_queue_age_seconds", "max_queue_count"}, "QUEUE_LIMIT_INVALID"
    )
    for key in (
        "active",
        "census_started",
        "terminal_receipt_published",
        "release_ready",
        "identity_restamp_active",
    ):
        if not isinstance(settlement[key], bool):
            raise ContractError("SETTLEMENT_INVALID")
    source_tree = _sha(candidate["source_tree"], "CANDIDATE_INVALID")
    source_revision = _sha(candidate["source_revision"], "CANDIDATE_INVALID")
    classification_base = _sha(candidate["classification_base_tree"], "CANDIDATE_INVALID")
    census_head = _sha(settlement["census_head"], "SETTLEMENT_INVALID")
    source_head = _sha(settlement["source_head"], "SETTLEMENT_INVALID")
    impact_digest = _digest(candidate["impact_digest"], "CANDIDATE_INVALID")
    producer_evidence_digest = _digest(candidate["producer_evidence_digest"], "CANDIDATE_INVALID")
    evidence_binding_digest = _digest(candidate["evidence_binding_digest"], "CANDIDATE_INVALID")
    release_scope_digest = _digest(candidate["release_scope_digest"], "CANDIDATE_INVALID")
    approval_scope = _digest(candidate["approval_scope_digest"], "CANDIDATE_INVALID")
    expected_scope = _digest(
        settlement["expected_approval_scope_digest"], "SETTLEMENT_INVALID"
    )
    prior_train = _digest(settlement["prior_train_digest"], "SETTLEMENT_INVALID")
    expected_source_revision = _sha(settlement["expected_source_revision"], "SETTLEMENT_INVALID")
    expected_source_tree = _sha(settlement["expected_source_tree"], "SETTLEMENT_INVALID")
    expected_impact_digest = _digest(settlement["expected_impact_digest"], "SETTLEMENT_INVALID")
    expected_producer_evidence = _digest(
        settlement["expected_producer_evidence_digest"], "SETTLEMENT_INVALID"
    )
    expected_evidence_binding = _digest(
        settlement["expected_evidence_binding_digest"], "SETTLEMENT_INVALID"
    )
    expected_release_scope = _digest(
        settlement["expected_release_scope_digest"], "SETTLEMENT_INVALID"
    )
    queue_age = _bounded_integer(candidate["queue_age_seconds"], 0, 604800, "QUEUE_INVALID")
    queue_count = _bounded_integer(candidate["queue_count"], 1, 1000, "QUEUE_INVALID")
    max_age = _bounded_integer(limits["max_queue_age_seconds"], 1, 604800, "QUEUE_LIMIT_INVALID")
    max_count = _bounded_integer(limits["max_queue_count"], 1, 1000, "QUEUE_LIMIT_INVALID")
    active_writers = _bounded_integer(settlement["active_writers"], 0, 1000, "SETTLEMENT_INVALID")
    open_markers = _bounded_integer(settlement["open_markers"], 0, 1000, "SETTLEMENT_INVALID")
    if candidate["impact_classification"] not in {"nil_impact", "product_impact"} or not isinstance(candidate["urgent"], bool):
        raise ContractError("CANDIDATE_INVALID")

    decision = "hold"
    reason = "settlement_in_progress"
    if queue_age > max_age or queue_count > max_count:
        reason = "queue_expired_reclassify"
    elif (
        source_revision != expected_source_revision
        or source_tree != expected_source_tree
        or impact_digest != expected_impact_digest
        or producer_evidence_digest != expected_producer_evidence
        or evidence_binding_digest != expected_evidence_binding
        or release_scope_digest != expected_release_scope
    ):
        reason = "producer_evidence_mismatch"
    elif classification_base != source_head or census_head != source_head:
        reason = "classification_or_census_stale"
    elif approval_scope != expected_scope:
        reason = "approval_scope_mismatch"
    elif active_writers or open_markers or settlement["identity_restamp_active"]:
        reason = "settlement_occupied"
    else:
        receipt_pending = (
            settlement["active"]
            or settlement["census_started"]
            or settlement["release_ready"]
        ) and not settlement["terminal_receipt_published"]
        if receipt_pending:
            if candidate["urgent"] and _urgent_authority_valid(
                root["urgent_authority"],
                expected_scope=expected_scope,
                displaced_train=prior_train,
                producer_evidence_digest=producer_evidence_digest,
                evidence_binding_digest=evidence_binding_digest,
                release_scope_digest=release_scope_digest,
            ):
                decision = "admit"
                reason = "urgent_authority_exact"
            elif candidate["impact_classification"] == "nil_impact":
                decision = "coalesce"
                reason = "nil_impact_held_during_settlement"
            else:
                reason = "prior_receipt_pending"
        else:
            decision = "admit"
            reason = "admission_window_open"

    return {
        "schema": "leaf.platform-release-admission.v2",
        "state": "SHADOW",
        "decision": decision,
        "reason_code": reason,
        "candidate_source_tree": source_tree,
        "impact_digest": impact_digest,
        "producer_evidence_digest": producer_evidence_digest,
        "evidence_binding_digest": evidence_binding_digest,
        "release_scope_digest": release_scope_digest,
        "prior_train_digest": prior_train,
        "admission_window_digest": sha256_digest(
            {
                "source_head": source_head,
                "census_head": census_head,
                "prior_train_digest": prior_train,
                "approval_scope_digest": expected_scope,
                "producer_evidence_digest": producer_evidence_digest,
                "evidence_binding_digest": evidence_binding_digest,
                "release_scope_digest": release_scope_digest,
            }
        ),
        "queue_count": queue_count,
        "selector_activation_authorized": False,
        "writer_acquisition_authorized": False,
    }


def workflow_preflight(*, shadow_enabled: bool) -> dict[str, Any]:
    return {
        "schema": "leaf.platform-release-admission-preflight.v1",
        "state": "UNCONFIGURED",
        "shadow_enabled": bool(shadow_enabled),
        "selectors": {
            "release_admission": "UNCONFIGURED",
            "semantic_proof_reuse": "UNCONFIGURED",
            "source_impact": "UNCONFIGURED",
            "supply_coalescing": "UNCONFIGURED",
        },
        "deployment_effect": False,
        "receipt_published": False,
        "writer_acquisition_authorized": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("workflow-preflight")
    preflight.add_argument("--shadow-enabled", choices=("true", "false"), required=True)
    args = parser.parse_args()
    if args.command == "workflow-preflight":
        print(
            json.dumps(
                workflow_preflight(shadow_enabled=args.shadow_enabled == "true"),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 78
    return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["evaluate_release_admission", "workflow_preflight"]
