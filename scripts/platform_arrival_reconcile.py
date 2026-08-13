#!/usr/bin/env python3
"""Pure, dormant arrival-frontier reconciliation for Leaf release evidence."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from copy import deepcopy
import json
import re
from typing import Any

from platform_semantic_eligibility import (
    ContractError,
    reject_secret_material,
    sha256_digest,
)


SERVICES = ("app", "broker", "canonical-worker", "harness", "web")
MAX_RUNS = 32
MAX_RECEIPTS = 8
MAX_EVIDENCE_AGE_SECONDS = 31 * 24 * 60 * 60
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_WORKFLOW = re.compile(r"^[A-Za-z0-9_./-]{1,160}\.ya?ml$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]{1,80}/[A-Za-z0-9_.-]{1,80}$")
_STAGE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


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


def _selectors(value: Any) -> dict[str, str]:
    selectors = _exact(
        value,
        {"arrival_observation", "frontier_reconciliation"},
        "SELECTORS_INVALID",
    )
    if any(item != "UNCONFIGURED" for item in selectors.values()):
        raise ContractError("SELECTOR_ACTIVATION_FORBIDDEN")
    return selectors


def _arrival(value: Any) -> dict[str, Any]:
    arrival = _exact(
        value,
        {
            "arrival_id",
            "repository",
            "previous_main",
            "merge_commit",
            "current_main",
            "current_tree",
            "pr_number",
            "changed_path_set_digest",
            "observed_at_epoch",
        },
        "ARRIVAL_INVALID",
    )
    _digest(arrival["arrival_id"], "ARRIVAL_INVALID")
    if arrival["repository"] != "LEAF-Solar-Design/leaf-web-demo":
        raise ContractError("ARRIVAL_REPOSITORY_INVALID")
    for key in ("previous_main", "merge_commit", "current_main", "current_tree"):
        _sha(arrival[key], "ARRIVAL_SOURCE_INVALID")
    if arrival["merge_commit"] != arrival["current_main"]:
        raise ContractError("ARRIVAL_SOURCE_CONFLICT")
    _integer(arrival["pr_number"], 1, 1_000_000, "ARRIVAL_INVALID")
    _digest(arrival["changed_path_set_digest"], "ARRIVAL_INVALID")
    _integer(
        arrival["observed_at_epoch"], 1, 4_102_444_800, "ARRIVAL_TIME_INVALID"
    )
    return arrival


def _level3_decision(value: Any, *, allowed: set[str], code: str) -> dict[str, Any]:
    decision = _exact(
        value,
        {
            "receipt_digest",
            "decision",
            "producer_token_digest",
            "release_scope_digest",
        },
        code,
    )
    for key in ("receipt_digest", "producer_token_digest", "release_scope_digest"):
        _digest(decision[key], code)
    _enum(decision["decision"], allowed, code)
    return decision


def _level3(value: Any) -> dict[str, Any]:
    level3 = _exact(
        value,
        {"source_impact", "release_admission", "supply_coalescing", "proof_reuse"},
        "LEVEL3_EVIDENCE_INVALID",
    )
    result = {
        "source_impact": _level3_decision(
            level3["source_impact"],
            allowed={"nil_impact", "product_impact", "unknown"},
            code="SOURCE_IMPACT_INVALID",
        ),
        "release_admission": _level3_decision(
            level3["release_admission"],
            allowed={"admit", "hold", "coalesce"},
            code="RELEASE_ADMISSION_INVALID",
        ),
        "supply_coalescing": _level3_decision(
            level3["supply_coalescing"],
            allowed={"coalesce", "new_supply", "hold"},
            code="SUPPLY_COALESCING_INVALID",
        ),
        "proof_reuse": _level3_decision(
            level3["proof_reuse"],
            allowed={"reuse", "fresh_proof", "hold"},
            code="PROOF_REUSE_INVALID",
        ),
    }
    token_digests = {item["producer_token_digest"] for item in result.values()}
    scope_digests = {item["release_scope_digest"] for item in result.values()}
    if len(token_digests) != 1 or len(scope_digests) != 1:
        raise ContractError("LEVEL3_EVIDENCE_REBOUND")
    return result


def _run(value: Any) -> dict[str, Any]:
    run = _exact(
        value,
        {
            "run_id",
            "workflow",
            "conclusion",
            "source_revision",
            "duration_seconds",
            "owner_class",
        },
        "RUN_EVIDENCE_INVALID",
    )
    _integer(run["run_id"], 1, 10**15, "RUN_EVIDENCE_INVALID")
    _string(run["workflow"], _WORKFLOW, "RUN_EVIDENCE_INVALID")
    _enum(
        run["conclusion"],
        {"success", "failure", "cancelled", "in_progress", "queued", "skipped"},
        "RUN_EVIDENCE_INVALID",
    )
    _sha(run["source_revision"], "RUN_EVIDENCE_INVALID")
    _integer(run["duration_seconds"], 0, 86_400, "RUN_EVIDENCE_INVALID")
    _enum(
        run["owner_class"],
        {"same_train", "external_owner", "unknown"},
        "RUN_EVIDENCE_INVALID",
    )
    return run


def _runs(value: Any, code: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_RUNS:
        raise ContractError(code)
    return [_run(item) for item in value]


def _supply(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    supply = _exact(
        value,
        {
            "artifact_id",
            "artifact_digest",
            "manifest_digest",
            "service_count",
            "complete",
        },
        "SUPPLY_EVIDENCE_INVALID",
    )
    _integer(supply["artifact_id"], 1, 10**15, "SUPPLY_EVIDENCE_INVALID")
    _digest(supply["artifact_digest"], "SUPPLY_EVIDENCE_INVALID")
    _digest(supply["manifest_digest"], "SUPPLY_EVIDENCE_INVALID")
    _integer(supply["service_count"], 1, len(SERVICES), "SUPPLY_EVIDENCE_INVALID")
    if not isinstance(supply["complete"], bool):
        raise ContractError("SUPPLY_EVIDENCE_INVALID")
    if supply["complete"] and supply["service_count"] != len(SERVICES):
        raise ContractError("SUPPLY_COMPLETENESS_CONFLICT")
    return supply


def _convergence(value: Any, current_main: str) -> dict[str, Any] | None:
    if value is None:
        return None
    convergence = _exact(
        value,
        {"artifact_id", "artifact_digest", "source_revision", "state", "service_count"},
        "CONVERGENCE_EVIDENCE_INVALID",
    )
    _integer(
        convergence["artifact_id"], 1, 10**15, "CONVERGENCE_EVIDENCE_INVALID"
    )
    _digest(convergence["artifact_digest"], "CONVERGENCE_EVIDENCE_INVALID")
    _sha(convergence["source_revision"], "CONVERGENCE_EVIDENCE_INVALID")
    _enum(
        convergence["state"],
        {"in_progress", "converged", "failed"},
        "CONVERGENCE_EVIDENCE_INVALID",
    )
    _integer(
        convergence["service_count"], 0, len(SERVICES), "CONVERGENCE_EVIDENCE_INVALID"
    )
    if convergence["source_revision"] != current_main:
        raise ContractError("CONVERGENCE_SOURCE_CONFLICT")
    if convergence["state"] == "converged" and convergence["service_count"] != len(SERVICES):
        raise ContractError("CONVERGENCE_PARTIAL")
    return convergence


def _dispatch_shape(value: Any, code: str) -> dict[str, bool]:
    shape = _exact(value, {"sha_set", "image_tag_set"}, code)
    if any(not isinstance(item, bool) for item in shape.values()):
        raise ContractError(code)
    return shape


def _failed_stage(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    failed = _exact(
        value,
        {
            "name",
            "run_id",
            "classification",
            "credentials_configured",
            "live_mutation_started",
            "observed_dispatch",
            "resume_dispatch",
        },
        "FAILED_STAGE_INVALID",
    )
    _string(failed["name"], _STAGE, "FAILED_STAGE_INVALID")
    _integer(failed["run_id"], 1, 10**15, "FAILED_STAGE_INVALID")
    if failed["classification"] != "protected_input_validation":
        raise ContractError("FAILED_STAGE_INVALID")
    if failed["credentials_configured"] is not False or failed["live_mutation_started"] is not False:
        raise ContractError("FAILED_STAGE_MUTATION_CONFLICT")
    observed = _dispatch_shape(failed["observed_dispatch"], "FAILED_DISPATCH_INVALID")
    resume = _dispatch_shape(failed["resume_dispatch"], "RESUME_DISPATCH_INVALID")
    if observed != {"sha_set": True, "image_tag_set": True}:
        raise ContractError("FAILED_DISPATCH_INVALID")
    if resume != {"sha_set": False, "image_tag_set": True}:
        raise ContractError("RESUME_DISPATCH_INVALID")
    return failed


def _later_writer(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    writer = _exact(
        value,
        {
            "repository",
            "workflow",
            "run_id",
            "conclusion",
            "owner_class",
            "image_digest_changed",
            "terminal_handoff_digest",
        },
        "LATER_WRITER_INVALID",
    )
    _string(writer["repository"], _REPOSITORY, "LATER_WRITER_INVALID")
    _string(writer["workflow"], _WORKFLOW, "LATER_WRITER_INVALID")
    _integer(writer["run_id"], 1, 10**15, "LATER_WRITER_INVALID")
    _enum(
        writer["conclusion"],
        {"success", "failure", "cancelled", "in_progress", "queued"},
        "LATER_WRITER_INVALID",
    )
    _enum(
        writer["owner_class"],
        {"same_train", "external_owner", "unknown"},
        "LATER_WRITER_INVALID",
    )
    if not isinstance(writer["image_digest_changed"], bool):
        raise ContractError("LATER_WRITER_INVALID")
    _digest(writer["terminal_handoff_digest"], "LATER_WRITER_INVALID")
    return writer


def _effects(value: Any, current_main: str) -> dict[str, Any]:
    effects = _exact(
        value,
        {
            "producer_runs",
            "relay_runs",
            "supply",
            "convergence",
            "failed_stage",
            "later_writer",
        },
        "EFFECTS_INVALID",
    )
    result = {
        "producer_runs": _runs(effects["producer_runs"], "PRODUCER_RUNS_INVALID"),
        "relay_runs": _runs(effects["relay_runs"], "RELAY_RUNS_INVALID"),
        "supply": _supply(effects["supply"]),
        "convergence": _convergence(effects["convergence"], current_main),
        "failed_stage": _failed_stage(effects["failed_stage"]),
        "later_writer": _later_writer(effects["later_writer"]),
    }
    identifiers = [
        run["run_id"] for run in result["producer_runs"] + result["relay_runs"]
    ]
    if len(identifiers) != len(set(identifiers)):
        raise ContractError("DUPLICATE_RUN_EVIDENCE")
    return result


def _terminal(value: Any) -> dict[str, Any]:
    terminal = _exact(
        value,
        {"receipt_digests", "preserved_stages", "rollback_source_revision"},
        "TERMINAL_EVIDENCE_INVALID",
    )
    receipts = terminal["receipt_digests"]
    if not isinstance(receipts, list) or not 1 <= len(receipts) <= MAX_RECEIPTS:
        raise ContractError("TERMINAL_EVIDENCE_INVALID")
    for receipt in receipts:
        _digest(receipt, "TERMINAL_EVIDENCE_INVALID")
    if len(receipts) != len(set(receipts)):
        raise ContractError("DUPLICATE_RECEIPT_EVIDENCE")
    stages = terminal["preserved_stages"]
    if not isinstance(stages, list) or len(stages) > 16:
        raise ContractError("TERMINAL_EVIDENCE_INVALID")
    if any(_STAGE.fullmatch(item) is None for item in stages if isinstance(item, str)):
        raise ContractError("TERMINAL_EVIDENCE_INVALID")
    if any(not isinstance(item, str) for item in stages) or len(stages) != len(set(stages)):
        raise ContractError("TERMINAL_EVIDENCE_INVALID")
    _sha(terminal["rollback_source_revision"], "TERMINAL_EVIDENCE_INVALID")
    return terminal


def _superseded_by(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    result = _exact(
        value,
        {"current_main", "supply_manifest_digest", "terminal_handoff_digest"},
        "SUPERSEDE_EVIDENCE_INVALID",
    )
    _sha(result["current_main"], "SUPERSEDE_EVIDENCE_INVALID")
    _digest(result["supply_manifest_digest"], "SUPERSEDE_EVIDENCE_INVALID")
    _digest(result["terminal_handoff_digest"], "SUPERSEDE_EVIDENCE_INVALID")
    return result


def _frontier(value: Any) -> dict[str, Any]:
    frontier = _exact(
        value,
        {
            "current_main",
            "current_supply_manifest_digest",
            "current_identity_digest",
            "active_writer_count",
            "marker_count",
            "live_services_exact",
            "owner_class",
            "superseded_by",
        },
        "FRONTIER_INVALID",
    )
    _sha(frontier["current_main"], "FRONTIER_INVALID")
    _digest(frontier["current_supply_manifest_digest"], "FRONTIER_INVALID")
    _digest(frontier["current_identity_digest"], "FRONTIER_INVALID")
    _integer(frontier["active_writer_count"], 0, 1_000, "FRONTIER_INVALID")
    _integer(frontier["marker_count"], 0, 1_000, "FRONTIER_INVALID")
    if not isinstance(frontier["live_services_exact"], bool):
        raise ContractError("FRONTIER_INVALID")
    _enum(
        frontier["owner_class"],
        {"same_train", "external_owner", "unknown"},
        "FRONTIER_INVALID",
    )
    frontier["superseded_by"] = _superseded_by(frontier["superseded_by"])
    return frontier


def _impact_axes(effects: dict[str, Any], product_impact: str) -> tuple[str, str, str]:
    relay_runs = effects["relay_runs"]
    producer_runs = effects["producer_runs"]
    if relay_runs:
        trigger = "relay"
    elif producer_runs:
        trigger = "build"
    elif effects["later_writer"] is not None:
        trigger = "writer"
    else:
        trigger = "none"

    supply = effects["supply"]
    if supply is None:
        supply_effect = "none"
    elif supply["complete"]:
        supply_effect = "complete_new_supply"
    else:
        supply_effect = "partial_supply"

    if effects["failed_stage"] is not None:
        live_effect = "failed_stage"
    elif effects["convergence"] is None:
        live_effect = "none"
    else:
        live_effect = effects["convergence"]["state"]
    return trigger, supply_effect, live_effect


def _disposition(
    *,
    arrival: dict[str, Any],
    level3: dict[str, Any],
    effects: dict[str, Any],
    terminal: dict[str, Any],
    frontier: dict[str, Any],
    product_impact: str,
    supply_effect: str,
    live_effect: str,
) -> tuple[str, str, str, str | None]:
    writer = effects["later_writer"]
    relay_in_progress = any(
        run["conclusion"] in {"in_progress", "queued"} for run in effects["relay_runs"]
    )
    if frontier["owner_class"] == "unknown" or (
        writer is not None and writer["owner_class"] == "unknown"
    ):
        return "hold", "owner_unattributed", "hold", None
    if frontier["current_main"] != arrival["current_main"]:
        if frontier["superseded_by"] is not None:
            return "stand_down", "newer_frontier_terminal", "stand_down", None
        return "hold", "current_main_drift", "hold", None
    if frontier["active_writer_count"] or frontier["marker_count"]:
        return "hold", "frontier_occupied", "hold", None
    if writer is not None:
        if writer["image_digest_changed"]:
            return "rebind", "later_writer_supply_changed", "reclassify", None
        if writer["conclusion"] != "success":
            return "hold", "later_writer_nonterminal", "hold", None
    if relay_in_progress:
        return "hold", "relay_nonterminal", "hold", None
    if supply_effect == "partial_supply":
        return "hold", "partial_supply", "hold", None
    if not frontier["live_services_exact"] and live_effect != "none":
        return "hold", "live_frontier_inexact", "hold", None
    if effects["failed_stage"] is not None:
        if product_impact != "build_input":
            return "hold", "failed_stage_product_mismatch", "hold", None
        convergence = effects["convergence"]
        if convergence is None or convergence["state"] != "converged":
            return "hold", "failed_stage_not_terminal", "hold", None
        if not {"build", "web"}.issubset(terminal["preserved_stages"]):
            return "hold", "failed_stage_predecessor_missing", "hold", None
        return (
            "resume_failed_stage",
            "single_failed_stage_preserved",
            "resume_failed_stage",
            effects["failed_stage"]["name"],
        )
    if live_effect == "in_progress":
        return "hold", "live_frontier_in_progress", "hold", None
    if product_impact == "nil" and supply_effect == "complete_new_supply":
        if live_effect != "converged":
            return "hold", "new_supply_not_converged", "hold", None
        if (
            level3["supply_coalescing"]["decision"] != "coalesce"
            or level3["proof_reuse"]["decision"] != "reuse"
        ):
            return "hold", "level3_adoption_evidence_incomplete", "hold", None
        return "adopt_frontier", "product_nil_operational_frontier", "adopt", None
    if product_impact == "nil" and supply_effect == "none" and live_effect == "none":
        return "preserve", "no_operational_frontier", "preserve", None
    if product_impact == "build_input":
        return "rebind", "product_reclassification_required", "reclassify", None
    return "hold", "evidence_incomplete", "hold", None


def reconcile_arrival(value: Any, *, fixture_enabled: bool = False) -> dict[str, Any]:
    """Return one closed, non-executing arrival disposition."""

    if not fixture_enabled:
        raise ContractError("UNCONFIGURED")
    document = _exact(
        value,
        {
            "schema",
            "selectors",
            "arrival",
            "level3",
            "effects",
            "terminal",
            "frontier",
            "observed_at_epoch",
        },
        "ARRIVAL_DOCUMENT_INVALID",
    )
    if document["schema"] != "leaf.platform-arrival-reconciliation-input.v1":
        raise ContractError("ARRIVAL_DOCUMENT_VERSION_INVALID")
    selectors = _selectors(document["selectors"])
    arrival = _arrival(document["arrival"])
    observed_at = _integer(
        document["observed_at_epoch"], 1, 4_102_444_800, "ARRIVAL_TIME_INVALID"
    )
    if (
        observed_at < arrival["observed_at_epoch"]
        or observed_at - arrival["observed_at_epoch"] > MAX_EVIDENCE_AGE_SECONDS
    ):
        raise ContractError("ARRIVAL_EVIDENCE_STALE")
    level3 = _level3(document["level3"])
    effects = _effects(document["effects"], arrival["current_main"])
    terminal = _terminal(document["terminal"])
    frontier = _frontier(document["frontier"])

    source_decision = level3["source_impact"]["decision"]
    product_impact = {
        "nil_impact": "nil",
        "product_impact": "build_input",
        "unknown": "unknown",
    }[source_decision]
    trigger_impact, supply_effect, live_effect = _impact_axes(effects, product_impact)
    disposition, reason, next_action, failed_stage = _disposition(
        arrival=arrival,
        level3=level3,
        effects=effects,
        terminal=terminal,
        frontier=frontier,
        product_impact=product_impact,
        supply_effect=supply_effect,
        live_effect=live_effect,
    )
    supply = effects["supply"]
    convergence = effects["convergence"]
    result = {
        "schema": "leaf.platform-arrival-reconciliation.v1",
        "state": "SHADOW",
        "arrival_id": arrival["arrival_id"],
        "previous_source_revision": arrival["previous_main"],
        "current_source_revision": arrival["current_main"],
        "current_source_tree": arrival["current_tree"],
        "product_impact": product_impact,
        "trigger_impact": trigger_impact,
        "supply_effect": supply_effect,
        "live_effect": live_effect,
        "producer_run_ids": [item["run_id"] for item in effects["producer_runs"]],
        "relay_run_ids": [item["run_id"] for item in effects["relay_runs"]],
        "supply_artifact_id": None if supply is None else supply["artifact_id"],
        "supply_artifact_digest": None if supply is None else supply["artifact_digest"],
        "convergence_artifact_id": (
            None if convergence is None else convergence["artifact_id"]
        ),
        "convergence_artifact_digest": (
            None if convergence is None else convergence["artifact_digest"]
        ),
        "terminal_receipt_digests": deepcopy(terminal["receipt_digests"]),
        "preserved_stages": deepcopy(terminal["preserved_stages"]),
        "failed_or_remaining_stage": failed_stage,
        "owner_class": frontier["owner_class"],
        "disposition": disposition,
        "reason_code": reason,
        "current_writer_count": frontier["active_writer_count"],
        "marker_count": frontier["marker_count"],
        "next_level2_action": next_action,
        "rollback_source_revision": terminal["rollback_source_revision"],
        "evidence_binding_digest": sha256_digest(
            {
                "arrival_id": arrival["arrival_id"],
                "level3_receipts": [
                    level3[name]["receipt_digest"]
                    for name in (
                        "source_impact",
                        "release_admission",
                        "supply_coalescing",
                        "proof_reuse",
                    )
                ],
                "terminal_receipts": terminal["receipt_digests"],
                "current_supply_manifest_digest": frontier[
                    "current_supply_manifest_digest"
                ],
                "current_identity_digest": frontier["current_identity_digest"],
            }
        ),
        "authority": {
            "dispatch": False,
            "cancel": False,
            "merge": False,
            "selector_activation": False,
            "source_mutation": False,
            "build_or_relay": False,
            "writer_or_marker_mutation": False,
            "deployment_or_aws": False,
            "auth0_or_credentials": False,
            "live_state_mutation": False,
        },
        "selectors": selectors,
    }
    reject_secret_material(result)
    return result


def workflow_preflight(shadow_enabled: bool) -> dict[str, Any]:
    return {
        "schema": "leaf.platform-arrival-reconciliation-preflight.v1",
        "state": "UNCONFIGURED",
        "shadow_enabled": shadow_enabled,
        "selectors": {
            "arrival_observation": "UNCONFIGURED",
            "frontier_reconciliation": "UNCONFIGURED",
        },
        "receipt_published": False,
        "provider_calls": 0,
        "dispatch_authorized": False,
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


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["reconcile_arrival", "workflow_preflight"]
