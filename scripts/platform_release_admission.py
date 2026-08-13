#!/usr/bin/env python3
"""Dormant release admission from one verified producer token."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
import re
from typing import Any

from platform_semantic_eligibility import ContractError, sha256_digest
from platform_source_impact import (
    TrustedProducerRoots,
    _impact_from_validated,
    verify_producer_evidence_token,
)


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


def _bounded_integer(
    value: Any, minimum: int, maximum: int, code: str
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ContractError(code)
    return value


def _urgent_authority_valid(
    authority: Any,
    *,
    validated: Any,
    displaced_train: str,
) -> bool:
    if not isinstance(authority, Mapping):
        return False
    if set(authority) != {
        "approval_scope_digest",
        "displaced_train_digest",
        "rollback_digest",
        "release_lineage_digest",
    }:
        return False
    return (
        authority.get("approval_scope_digest")
        == validated.approval_scope_digest
        and authority.get("displaced_train_digest") == displaced_train
        and authority.get("rollback_digest") == validated.rollback_digest
        and authority.get("release_lineage_digest")
        == validated.release_lineage_digest
    )


def evaluate_release_admission(
    document: Mapping[str, Any],
    *,
    trusted_roots: TrustedProducerRoots | Any,
    now_epoch: int,
    fixture_enabled: bool = False,
) -> dict[str, Any]:
    """Verify the full token, then return admit, hold, or coalesce."""

    if not fixture_enabled:
        raise ContractError("UNCONFIGURED")
    root = _exact(
        document,
        {
            "schema",
            "selector",
            "producer_token",
            "candidate",
            "settlement",
            "limits",
            "urgent_authority",
        },
        "ADMISSION_INPUT_INVALID",
    )
    if (
        root["schema"] != "leaf.platform-release-admission-input.v3"
        or root["selector"] != "UNCONFIGURED"
    ):
        raise ContractError("ADMISSION_INPUT_INVALID")
    validated = verify_producer_evidence_token(
        root["producer_token"], trusted_roots, now_epoch=now_epoch
    )
    candidate = _exact(
        root["candidate"],
        {
            "relay_base_tree",
            "deferred",
            "queue_age_seconds",
            "queue_count",
            "urgent",
        },
        "CANDIDATE_INVALID",
    )
    impact = _impact_from_validated(
        validated,
        relay_base_tree=candidate["relay_base_tree"],
        deferred=candidate["deferred"],
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
            "prior_train_digest",
        },
        "SETTLEMENT_INVALID",
    )
    limits = _exact(
        root["limits"],
        {"max_queue_age_seconds", "max_queue_count"},
        "QUEUE_LIMIT_INVALID",
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
    if not isinstance(candidate["urgent"], bool):
        raise ContractError("CANDIDATE_INVALID")

    census_head = _sha(settlement["census_head"], "SETTLEMENT_INVALID")
    source_head = _sha(settlement["source_head"], "SETTLEMENT_INVALID")
    prior_train = _digest(
        settlement["prior_train_digest"], "SETTLEMENT_INVALID"
    )
    queue_age = _bounded_integer(
        candidate["queue_age_seconds"], 0, 604800, "QUEUE_INVALID"
    )
    queue_count = _bounded_integer(
        candidate["queue_count"], 1, 1000, "QUEUE_INVALID"
    )
    max_age = _bounded_integer(
        limits["max_queue_age_seconds"], 1, 604800, "QUEUE_LIMIT_INVALID"
    )
    max_count = _bounded_integer(
        limits["max_queue_count"], 1, 1000, "QUEUE_LIMIT_INVALID"
    )
    active_writers = _bounded_integer(
        settlement["active_writers"], 0, 1000, "SETTLEMENT_INVALID"
    )
    open_markers = _bounded_integer(
        settlement["open_markers"], 0, 1000, "SETTLEMENT_INVALID"
    )

    decision = "hold"
    reason = "settlement_in_progress"
    if queue_age > max_age or queue_count > max_count:
        reason = "queue_expired_reclassify"
    elif (
        source_head != validated.candidate_source_tree
        or census_head != source_head
        or impact["relay_base_tree"] != validated.base_source_tree
    ):
        reason = "classification_or_census_stale"
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
                validated=validated,
                displaced_train=prior_train,
            ):
                decision = "admit"
                reason = "urgent_authority_exact"
            elif impact["classification"] == "nil_impact":
                decision = "coalesce"
                reason = "nil_impact_held_during_settlement"
            else:
                reason = "prior_receipt_pending"
        else:
            decision = "admit"
            reason = "admission_window_open"

    return {
        "schema": "leaf.platform-release-admission.v3",
        "state": "SHADOW",
        "decision": decision,
        "reason_code": reason,
        "candidate_source_tree": validated.candidate_source_tree,
        "impact_digest": impact["impact_digest"],
        "producer_token_digest": validated.content_digest,
        "release_scope_digest": validated.release_scope_digest,
        "prior_train_digest": prior_train,
        "admission_window_digest": sha256_digest(
            {
                "source_head": source_head,
                "census_head": census_head,
                "prior_train_digest": prior_train,
                "approval_scope_digest": validated.approval_scope_digest,
                "producer_token_digest": validated.content_digest,
                "release_scope_digest": validated.release_scope_digest,
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
    preflight.add_argument(
        "--shadow-enabled", choices=("true", "false"), required=True
    )
    args = parser.parse_args()
    if args.command == "workflow-preflight":
        print(
            json.dumps(
                workflow_preflight(
                    shadow_enabled=args.shadow_enabled == "true"
                ),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 78
    return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["evaluate_release_admission", "workflow_preflight"]
