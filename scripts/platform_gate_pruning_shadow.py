#!/usr/bin/env python3
"""Evaluate dormant marker-gate replacement evidence without changing a gate."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import hashlib
import math
from pathlib import Path
import re
import statistics
import sys
from typing import Any, Mapping, TextIO


INPUT_SCHEMA = "leaf.platform-gate-pruning-shadow.v1"
PLAN_SCHEMA = "leaf.platform-gate-pruning-shadow-plan.v1"
GATE_ID = "legacy-marker-census-per-transaction"
SAFETY_JOB = "detect-any-transaction-owing-settlement"
REQUIRED_SCENARIOS = {
    "normal_success",
    "forced_cancellation",
    "missing_artifact",
    "failed_settlement",
    "expired_lease",
}
MARKER_LEDGER_MODES = {"disabled", "shadow", "enabled"}

_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,199}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


class ContractError(ValueError):
    """Input is malformed and cannot produce even a dormant recommendation."""


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("duplicate_json_key")
        result[key] = value
    return result


def load_json(stream: TextIO) -> dict[str, Any]:
    try:
        value = json.load(stream, object_pairs_hook=_object)
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("gate_pruning_json_invalid") from exc
    if not isinstance(value, dict):
        raise ContractError("gate_pruning_root_invalid")
    return value


def _exact(value: Any, keys: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ContractError(code)
    return value


def _integer(value: Any, code: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractError(code)
    return value


def _number(value: Any, code: str, *, minimum: float = 0.0) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= minimum
    ):
        raise ContractError(code)
    return float(value)


def _pattern(value: Any, pattern: re.Pattern[str], code: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ContractError(code)
    return value


def _timestamp(value: Any, code: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})",
        value,
    ) is None:
        raise ContractError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(code) from exc
    if parsed.tzinfo is None:
        raise ContractError(code)
    return value, parsed


def _marker_result(value: Any, code: str) -> str:
    if value not in {"EMPTY", "OPEN"}:
        raise ContractError(code)
    return value


def _json_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


FULL_KEYS = {
    "schema",
    "workflow_blob",
    "checkpoint_sha256",
    "result",
    "open_count",
    "open_set_sha256",
    "duration_seconds",
}


def _legacy(value: Any) -> dict[str, Any]:
    result = _exact(value, FULL_KEYS, "legacy_census_invalid")
    if result["schema"] != "leaf.legacy-marker-census.v1":
        raise ContractError("legacy_census_invalid")
    normalized = {
        "schema": result["schema"],
        "workflow_blob": _pattern(result["workflow_blob"], _SHA, "legacy_census_invalid"),
        "checkpoint_sha256": _pattern(
            result["checkpoint_sha256"], _DIGEST, "legacy_census_invalid"
        ),
        "result": _marker_result(result["result"], "legacy_census_invalid"),
        "open_count": _integer(result["open_count"], "legacy_census_invalid"),
        "open_set_sha256": _pattern(
            result["open_set_sha256"], _HASH, "legacy_census_invalid"
        ),
        "duration_seconds": _number(
            result["duration_seconds"], "legacy_census_invalid"
        ),
    }
    if (normalized["open_count"] == 0) != (normalized["result"] == "EMPTY"):
        raise ContractError("legacy_census_invalid")
    return normalized


INDEXED_KEYS = {"checkpoint_sha256", "duration_seconds", "receipt"}
INDEXED_RECEIPT_KEYS = {
    "schema",
    "result",
    "strong_consistent",
    "writer_lock_held",
    "ledger_union_delta_exact",
    "pre_post_snapshot_stable",
    "fallback_to_full_scan_on_error",
    "bounded_delta_seconds",
    "lock_acquired_at",
    "open_count",
    "open_set_sha256",
}


def _indexed(value: Any) -> dict[str, Any]:
    envelope = _exact(value, INDEXED_KEYS, "indexed_census_invalid")
    receipt = _exact(
        envelope["receipt"], INDEXED_RECEIPT_KEYS, "indexed_census_invalid"
    )
    if (
        receipt["schema"] != "leaf.staging-marker-ledger-census.v1"
        or receipt["strong_consistent"] is not True
        or receipt["writer_lock_held"] is not True
        or receipt["ledger_union_delta_exact"] is not True
        or receipt["pre_post_snapshot_stable"] is not True
        or receipt["fallback_to_full_scan_on_error"] is not True
    ):
        raise ContractError("indexed_census_invalid")
    bounded_delta_seconds = _number(
        receipt["bounded_delta_seconds"], "indexed_census_invalid"
    )
    lock_acquired_at, lock_acquired_at_parsed = _timestamp(
        receipt["lock_acquired_at"], "indexed_census_invalid"
    )
    if bounded_delta_seconds > 86400:
        raise ContractError("indexed_census_invalid")
    normalized = {
        "checkpoint_sha256": _pattern(
            envelope["checkpoint_sha256"], _DIGEST, "indexed_census_invalid"
        ),
        "duration_seconds": _number(
            envelope["duration_seconds"], "indexed_census_invalid"
        ),
        "receipt": {
            "schema": receipt["schema"],
            "result": _marker_result(receipt["result"], "indexed_census_invalid"),
            "strong_consistent": True,
            "writer_lock_held": True,
            "ledger_union_delta_exact": True,
            "pre_post_snapshot_stable": True,
            "fallback_to_full_scan_on_error": True,
            "bounded_delta_seconds": bounded_delta_seconds,
            "lock_acquired_at": lock_acquired_at,
            "lock_acquired_at_parsed": lock_acquired_at_parsed,
            "open_count": _integer(receipt["open_count"], "indexed_census_invalid"),
            "open_set_sha256": _pattern(
                receipt["open_set_sha256"], _HASH, "indexed_census_invalid"
            ),
        },
    }
    indexed_receipt = normalized["receipt"]
    if (indexed_receipt["open_count"] == 0) != (
        indexed_receipt["result"] == "EMPTY"
    ):
        raise ContractError("indexed_census_invalid")
    return normalized


SOURCE_KEYS = {
    "source_commit",
    "source_tree",
    "deploy_workflow_blob",
    "restore_workflow_blob",
    "ledger_script_blob",
    "legacy_census_script_blob",
    "migration_horizon_sha256",
    "ledger_generation_sha256",
    "writer_lock_generation_sha256",
    "capture_id",
    "captured_at",
}


def _source(value: Any) -> dict[str, Any]:
    source = _exact(value, SOURCE_KEYS, "source_evidence_invalid")
    captured_at, parsed = _timestamp(source["captured_at"], "source_evidence_invalid")
    return {
        "source_commit": _pattern(source["source_commit"], _SHA, "source_evidence_invalid"),
        "source_tree": _pattern(source["source_tree"], _SHA, "source_evidence_invalid"),
        "deploy_workflow_blob": _pattern(
            source["deploy_workflow_blob"], _SHA, "source_evidence_invalid"
        ),
        "restore_workflow_blob": _pattern(
            source["restore_workflow_blob"], _SHA, "source_evidence_invalid"
        ),
        "ledger_script_blob": _pattern(
            source["ledger_script_blob"], _SHA, "source_evidence_invalid"
        ),
        "legacy_census_script_blob": _pattern(
            source["legacy_census_script_blob"], _SHA, "source_evidence_invalid"
        ),
        "migration_horizon_sha256": _pattern(
            source["migration_horizon_sha256"], _DIGEST, "source_evidence_invalid"
        ),
        "ledger_generation_sha256": _pattern(
            source["ledger_generation_sha256"], _DIGEST, "source_evidence_invalid"
        ),
        "writer_lock_generation_sha256": _pattern(
            source["writer_lock_generation_sha256"],
            _DIGEST,
            "source_evidence_invalid",
        ),
        "capture_id": _pattern(source["capture_id"], _ID, "source_evidence_invalid"),
        "captured_at": captured_at,
        "captured_at_parsed": parsed,
    }


COMPARISON_KEYS = {
    "transaction_id",
    "source_evidence_sha256",
    "scenario",
    "source_commit",
    "deploy_workflow_blob",
    "migration_horizon_sha256",
    "ledger_generation_sha256",
    "writer_lock_generation_sha256",
    "active_writers",
    "terminal",
    "terminal_receipt_sha256",
    "completed_at",
    "checkpoint_content",
    "legacy",
    "indexed",
}


def _comparison(value: Any) -> dict[str, Any]:
    item = _exact(value, COMPARISON_KEYS, "shadow_comparison_invalid")
    if item["scenario"] not in REQUIRED_SCENARIOS:
        raise ContractError("shadow_comparison_invalid")
    completed_at, completed_parsed = _timestamp(
        item["completed_at"], "shadow_comparison_invalid"
    )
    return {
        "transaction_id": _pattern(
            item["transaction_id"], _ID, "shadow_comparison_invalid"
        ),
        "source_evidence_sha256": _pattern(
            item["source_evidence_sha256"], _DIGEST, "shadow_comparison_invalid"
        ),
        "scenario": item["scenario"],
        "source_commit": _pattern(
            item["source_commit"], _SHA, "shadow_comparison_invalid"
        ),
        "deploy_workflow_blob": _pattern(
            item["deploy_workflow_blob"], _SHA, "shadow_comparison_invalid"
        ),
        "migration_horizon_sha256": _pattern(
            item["migration_horizon_sha256"], _DIGEST, "shadow_comparison_invalid"
        ),
        "ledger_generation_sha256": _pattern(
            item["ledger_generation_sha256"], _DIGEST, "shadow_comparison_invalid"
        ),
        "writer_lock_generation_sha256": _pattern(
            item["writer_lock_generation_sha256"],
            _DIGEST,
            "shadow_comparison_invalid",
        ),
        "active_writers": _integer(
            item["active_writers"], "shadow_comparison_invalid"
        ),
        "terminal": item["terminal"],
        "terminal_receipt_sha256": _pattern(
            item["terminal_receipt_sha256"], _DIGEST, "shadow_comparison_invalid"
        ),
        "completed_at": completed_at,
        "completed_at_parsed": completed_parsed,
        "checkpoint_content": _checkpoint_content(item["checkpoint_content"]),
        "legacy": _legacy(item["legacy"]),
        "indexed": _indexed(item["indexed"]),
    }


CONTROL_KEYS = {
    "scenario",
    "transaction_id",
    "source_evidence_sha256",
    "source_commit",
    "deploy_workflow_blob",
    "migration_horizon_sha256",
    "ledger_generation_sha256",
    "writer_lock_generation_sha256",
    "active_writers",
    "terminal",
    "terminal_receipt_sha256",
    "completed_at",
    "checkpoint_content",
    "replacement_blocks",
    "integrity_alarm",
    "legacy",
    "indexed",
}


def _control(value: Any, expected: str) -> dict[str, Any]:
    item = _exact(value, CONTROL_KEYS, "negative_control_invalid")
    if item["scenario"] != expected:
        raise ContractError("negative_control_invalid")
    if (
        not isinstance(item["replacement_blocks"], bool)
        or not isinstance(item["integrity_alarm"], bool)
        or not isinstance(item["terminal"], bool)
    ):
        raise ContractError("negative_control_invalid")
    completed_at, completed_parsed = _timestamp(
        item["completed_at"], "negative_control_invalid"
    )
    return {
        "scenario": expected,
        "transaction_id": _pattern(
            item["transaction_id"], _ID, "negative_control_invalid"
        ),
        "source_evidence_sha256": _pattern(
            item["source_evidence_sha256"], _DIGEST, "negative_control_invalid"
        ),
        "source_commit": _pattern(
            item["source_commit"], _SHA, "negative_control_invalid"
        ),
        "deploy_workflow_blob": _pattern(
            item["deploy_workflow_blob"], _SHA, "negative_control_invalid"
        ),
        "migration_horizon_sha256": _pattern(
            item["migration_horizon_sha256"],
            _DIGEST,
            "negative_control_invalid",
        ),
        "ledger_generation_sha256": _pattern(
            item["ledger_generation_sha256"],
            _DIGEST,
            "negative_control_invalid",
        ),
        "writer_lock_generation_sha256": _pattern(
            item["writer_lock_generation_sha256"],
            _DIGEST,
            "negative_control_invalid",
        ),
        "active_writers": _integer(
            item["active_writers"], "negative_control_invalid"
        ),
        "terminal": item["terminal"],
        "terminal_receipt_sha256": _pattern(
            item["terminal_receipt_sha256"], _DIGEST, "negative_control_invalid"
        ),
        "completed_at": completed_at,
        "completed_at_parsed": completed_parsed,
        "checkpoint_content": _checkpoint_content(item["checkpoint_content"]),
        "replacement_blocks": item["replacement_blocks"],
        "integrity_alarm": item["integrity_alarm"],
        "legacy": _legacy(item["legacy"]),
        "indexed": _indexed(item["indexed"]),
    }


CHECKPOINT_CONTENT_KEYS = {
    "schema",
    "source_commit",
    "source_tree",
    "deploy_workflow_blob",
    "restore_workflow_blob",
    "ledger_script_blob",
    "legacy_census_script_blob",
    "migration_horizon_sha256",
    "scan_started_at",
    "scan_completed_at",
    "result",
    "open_count",
    "open_set_sha256",
}


def _checkpoint_content(value: Any) -> dict[str, Any]:
    item = _exact(value, CHECKPOINT_CONTENT_KEYS, "checkpoint_content_invalid")
    if item["schema"] != "leaf.staging-marker-checkpoint-anchor.v1":
        raise ContractError("checkpoint_content_invalid")
    started_at, started = _timestamp(
        item["scan_started_at"], "checkpoint_content_invalid"
    )
    completed_at, completed = _timestamp(
        item["scan_completed_at"], "checkpoint_content_invalid"
    )
    if completed < started:
        raise ContractError("checkpoint_content_invalid")
    normalized = {
        "schema": item["schema"],
        "source_commit": _pattern(
            item["source_commit"], _SHA, "checkpoint_content_invalid"
        ),
        "source_tree": _pattern(
            item["source_tree"], _SHA, "checkpoint_content_invalid"
        ),
        "deploy_workflow_blob": _pattern(
            item["deploy_workflow_blob"], _SHA, "checkpoint_content_invalid"
        ),
        "restore_workflow_blob": _pattern(
            item["restore_workflow_blob"], _SHA, "checkpoint_content_invalid"
        ),
        "ledger_script_blob": _pattern(
            item["ledger_script_blob"], _SHA, "checkpoint_content_invalid"
        ),
        "legacy_census_script_blob": _pattern(
            item["legacy_census_script_blob"], _SHA, "checkpoint_content_invalid"
        ),
        "migration_horizon_sha256": _pattern(
            item["migration_horizon_sha256"],
            _DIGEST,
            "checkpoint_content_invalid",
        ),
        "scan_started_at": started_at,
        "scan_completed_at": completed_at,
        "result": _marker_result(item["result"], "checkpoint_content_invalid"),
        "open_count": _integer(item["open_count"], "checkpoint_content_invalid"),
        "open_set_sha256": _pattern(
            item["open_set_sha256"], _HASH, "checkpoint_content_invalid"
        ),
    }
    if (normalized["open_count"] == 0) != (normalized["result"] == "EMPTY"):
        raise ContractError("checkpoint_content_invalid")
    return normalized


def _audit(value: Any) -> dict[str, Any]:
    audit = _exact(
        value,
        {
            "enabled",
            "anchor_type",
            "checkpoint_sha256",
            "checkpoint_content",
            "workflow_blob",
            "last_completed_at",
            "maximum_age_seconds",
            "terminal_receipt_sha256",
        },
        "scheduled_audit_invalid",
    )
    if not isinstance(audit["enabled"], bool):
        raise ContractError("scheduled_audit_invalid")
    if audit["anchor_type"] != "successful_full_artifact_scan":
        raise ContractError("scheduled_audit_invalid")
    completed, parsed = _timestamp(
        audit["last_completed_at"], "scheduled_audit_invalid"
    )
    checkpoint_content = _checkpoint_content(audit["checkpoint_content"])
    checkpoint_sha256 = _pattern(
        audit["checkpoint_sha256"], _DIGEST, "scheduled_audit_invalid"
    )
    if (
        checkpoint_sha256 != _json_sha256(checkpoint_content)
        or completed != checkpoint_content["scan_completed_at"]
    ):
        raise ContractError("scheduled_audit_invalid")
    maximum_age_seconds = _number(
        audit["maximum_age_seconds"], "scheduled_audit_invalid"
    )
    if maximum_age_seconds > 604800:
        raise ContractError("scheduled_audit_invalid")
    return {
        "enabled": audit["enabled"],
        "anchor_type": audit["anchor_type"],
        "checkpoint_sha256": checkpoint_sha256,
        "checkpoint_content": checkpoint_content,
        "workflow_blob": _pattern(
            audit["workflow_blob"], _SHA, "scheduled_audit_invalid"
        ),
        "last_completed_at": completed,
        "last_completed_at_parsed": parsed,
        "maximum_age_seconds": maximum_age_seconds,
        "terminal_receipt_sha256": _pattern(
            audit["terminal_receipt_sha256"], _DIGEST, "scheduled_audit_invalid"
        ),
    }


def validate_evidence(value: Any) -> dict[str, Any]:
    root = _exact(
        value,
        {"schema", "gate", "source", "posture", "scheduled_audit", "comparisons", "negative_controls"},
        "gate_pruning_evidence_invalid",
    )
    if root["schema"] != INPUT_SCHEMA:
        raise ContractError("gate_pruning_evidence_invalid")
    gate = _exact(
        root["gate"],
        {"gate_id", "safety_job", "state_machine_stages", "current_control", "proposed_replacement"},
        "gate_identity_invalid",
    )
    if (
        gate["gate_id"] != GATE_ID
        or gate["safety_job"] != SAFETY_JOB
        or gate["state_machine_stages"] != ["preflight", "settlement"]
        or gate["current_control"] != "full-62-day-history-census"
        or gate["proposed_replacement"] != "indexed-marker-ledger"
    ):
        raise ContractError("gate_identity_invalid")
    source = _source(root["source"])
    posture = _exact(
        root["posture"],
        {"marker_ledger_mode", "digest_aware_reconcile", "active_writers"},
        "selector_posture_invalid",
    )
    if not isinstance(posture["digest_aware_reconcile"], bool):
        raise ContractError("selector_posture_invalid")
    if posture["marker_ledger_mode"] not in MARKER_LEDGER_MODES:
        raise ContractError("selector_posture_invalid")
    posture = {
        "marker_ledger_mode": posture["marker_ledger_mode"],
        "digest_aware_reconcile": posture["digest_aware_reconcile"],
        "active_writers": _integer(posture["active_writers"], "selector_posture_invalid"),
    }
    audit = _audit(root["scheduled_audit"])
    if not isinstance(root["comparisons"], list) or len(root["comparisons"]) > 100:
        raise ContractError("shadow_comparisons_invalid")
    comparisons = [_comparison(item) for item in root["comparisons"]]
    controls = _exact(
        root["negative_controls"],
        {"planted_open_row", "artifact_without_ledger", "stale_or_missing_checkpoint"},
        "negative_controls_invalid",
    )
    checkpoint_control = _exact(
        controls["stale_or_missing_checkpoint"],
        {
            "scenario",
            "transaction_id",
            "source_evidence_sha256",
            "source_commit",
            "deploy_workflow_blob",
            "migration_horizon_sha256",
            "ledger_generation_sha256",
            "writer_lock_generation_sha256",
            "active_writers",
            "terminal",
            "terminal_receipt_sha256",
            "completed_at",
            "checkpoint_state",
            "observed_checkpoint_sha256",
            "replacement_blocks",
            "fallback_to_full_scan",
            "fallback_checkpoint_content",
            "legacy",
        },
        "checkpoint_control_invalid",
    )
    if (
        checkpoint_control["scenario"] != "stale_or_missing_checkpoint"
        or checkpoint_control["checkpoint_state"] not in {"stale", "missing"}
        or checkpoint_control["replacement_blocks"] is not True
        or checkpoint_control["fallback_to_full_scan"] is not True
        or not isinstance(checkpoint_control["terminal"], bool)
    ):
        raise ContractError("checkpoint_control_invalid")
    observed_checkpoint_sha256 = checkpoint_control["observed_checkpoint_sha256"]
    if checkpoint_control["checkpoint_state"] == "missing":
        if observed_checkpoint_sha256 is not None:
            raise ContractError("checkpoint_control_invalid")
    else:
        observed_checkpoint_sha256 = _pattern(
            observed_checkpoint_sha256, _DIGEST, "checkpoint_control_invalid"
        )
    checkpoint_completed_at, checkpoint_completed_parsed = _timestamp(
        checkpoint_control["completed_at"], "checkpoint_control_invalid"
    )
    fallback_checkpoint_content = _checkpoint_content(
        checkpoint_control["fallback_checkpoint_content"]
    )
    return {
        "gate": gate,
        "source": source,
        "posture": posture,
        "scheduled_audit": audit,
        "comparisons": comparisons,
        "negative_controls": {
            "planted_open_row": _control(
                controls["planted_open_row"], "planted_open_row"
            ),
            "artifact_without_ledger": _control(
                controls["artifact_without_ledger"], "artifact_without_ledger"
            ),
            "stale_or_missing_checkpoint": {
                "scenario": checkpoint_control["scenario"],
                "transaction_id": _pattern(
                    checkpoint_control["transaction_id"],
                    _ID,
                    "checkpoint_control_invalid",
                ),
                "source_evidence_sha256": _pattern(
                    checkpoint_control["source_evidence_sha256"],
                    _DIGEST,
                    "checkpoint_control_invalid",
                ),
                "source_commit": _pattern(
                    checkpoint_control["source_commit"],
                    _SHA,
                    "checkpoint_control_invalid",
                ),
                "deploy_workflow_blob": _pattern(
                    checkpoint_control["deploy_workflow_blob"],
                    _SHA,
                    "checkpoint_control_invalid",
                ),
                "migration_horizon_sha256": _pattern(
                    checkpoint_control["migration_horizon_sha256"],
                    _DIGEST,
                    "checkpoint_control_invalid",
                ),
                "ledger_generation_sha256": _pattern(
                    checkpoint_control["ledger_generation_sha256"],
                    _DIGEST,
                    "checkpoint_control_invalid",
                ),
                "writer_lock_generation_sha256": _pattern(
                    checkpoint_control["writer_lock_generation_sha256"],
                    _DIGEST,
                    "checkpoint_control_invalid",
                ),
                "active_writers": _integer(
                    checkpoint_control["active_writers"],
                    "checkpoint_control_invalid",
                ),
                "terminal": checkpoint_control["terminal"],
                "terminal_receipt_sha256": _pattern(
                    checkpoint_control["terminal_receipt_sha256"],
                    _DIGEST,
                    "checkpoint_control_invalid",
                ),
                "completed_at": checkpoint_completed_at,
                "completed_at_parsed": checkpoint_completed_parsed,
                "checkpoint_state": checkpoint_control["checkpoint_state"],
                "observed_checkpoint_sha256": observed_checkpoint_sha256,
                "replacement_blocks": True,
                "fallback_to_full_scan": True,
                "fallback_checkpoint_content": fallback_checkpoint_content,
                "legacy": _legacy(checkpoint_control["legacy"]),
            },
        },
    }


def _base(evidence: Mapping[str, Any]) -> dict[str, Any]:
    comparisons = evidence["comparisons"]
    legacy_times = [item["legacy"]["duration_seconds"] for item in comparisons]
    indexed_times = [item["indexed"]["duration_seconds"] for item in comparisons]
    return {
        "schema": PLAN_SCHEMA,
        "gate_id": GATE_ID,
        "safety_job": SAFETY_JOB,
        "state_machine_stages": ["preflight", "settlement"],
        "disposition": "retain_blocking_gate",
        "code": "insufficient_evidence",
        "comparison_count": len(comparisons),
        "shadow_case_count": len(comparisons) + 3,
        "scenario_coverage": sorted({item["scenario"] for item in comparisons}),
        "disagreement_count": 0,
        "measured_legacy_total_seconds": sum(legacy_times),
        "measured_legacy_median_seconds": (
            statistics.median(legacy_times) if legacy_times else None
        ),
        "measured_indexed_total_seconds": sum(indexed_times),
        "measured_indexed_median_seconds": (
            statistics.median(indexed_times) if indexed_times else None
        ),
        "measured_savings_seconds": None,
        "inferred_savings_seconds": None,
        "scheduled_audit_age_seconds": None,
        "planted_open_row_passed": False,
        "artifact_without_ledger_passed": False,
        "stale_or_missing_checkpoint_passed": False,
        "rollback": {
            "marker_ledger_mode": "disabled",
            "blocking_control": "full-62-day-history-census",
        },
        "gate_change_authorized": False,
        "selector_activation_authorized": False,
        "dispatch_authorized": False,
        "live_mutation_authorized": False,
    }


def _retain(plan: dict[str, Any], code: str) -> dict[str, Any]:
    plan["code"] = code
    return plan


def _equal_markers(legacy: Mapping[str, Any], indexed: Mapping[str, Any]) -> bool:
    receipt = indexed["receipt"]
    return bool(
        legacy["checkpoint_sha256"] == indexed["checkpoint_sha256"]
        and legacy["result"] == receipt["result"]
        and legacy["open_count"] == receipt["open_count"]
        and legacy["open_set_sha256"] == receipt["open_set_sha256"]
    )


def _source_hash(source: Mapping[str, Any]) -> str:
    public = {key: value for key, value in source.items() if key != "captured_at_parsed"}
    encoded = json.dumps(
        public, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _checkpoint_matches_source(
    content: Mapping[str, Any], source: Mapping[str, Any]
) -> bool:
    return bool(
        content["source_commit"] == source["source_commit"]
        and content["source_tree"] == source["source_tree"]
        and content["deploy_workflow_blob"] == source["deploy_workflow_blob"]
        and content["restore_workflow_blob"] == source["restore_workflow_blob"]
        and content["ledger_script_blob"] == source["ledger_script_blob"]
        and content["legacy_census_script_blob"]
        == source["legacy_census_script_blob"]
        and content["migration_horizon_sha256"]
        == source["migration_horizon_sha256"]
    )


def _legacy_matches_checkpoint(
    legacy: Mapping[str, Any], content: Mapping[str, Any]
) -> bool:
    return bool(
        legacy["checkpoint_sha256"] == _json_sha256(content)
        and legacy["result"] == content["result"]
        and legacy["open_count"] == content["open_count"]
        and legacy["open_set_sha256"] == content["open_set_sha256"]
    )


def evaluate_gate(value: Any) -> dict[str, Any]:
    """Return a safe recommendation, never gate-change authority."""

    evidence = validate_evidence(value)
    plan = _base(evidence)
    posture = evidence["posture"]
    if (
        posture["marker_ledger_mode"] != "disabled"
        or posture["digest_aware_reconcile"] is not False
    ):
        return _retain(plan, "selector_posture_not_dormant")
    if posture["active_writers"] != 0:
        return _retain(plan, "active_writer_present")

    audit = evidence["scheduled_audit"]
    age = (
        evidence["source"]["captured_at_parsed"] - audit["last_completed_at_parsed"]
    ).total_seconds()
    plan["scheduled_audit_age_seconds"] = age
    if age < 0 or not audit["enabled"] or age > audit["maximum_age_seconds"]:
        return _retain(plan, "scheduled_audit_unavailable")

    comparisons = evidence["comparisons"]
    if len(comparisons) != 6:
        return _retain(plan, "shadow_sample_too_small")
    controls = list(evidence["negative_controls"].values())
    shadow_items = [*comparisons, *controls]
    transaction_ids = [item["transaction_id"] for item in shadow_items]
    receipt_ids = [item["terminal_receipt_sha256"] for item in shadow_items]
    if len(transaction_ids) != len(set(transaction_ids)) or len(receipt_ids) != len(
        set(receipt_ids)
    ):
        return _retain(plan, "duplicate_shadow_evidence")
    if not REQUIRED_SCENARIOS.issubset(plan["scenario_coverage"]):
        return _retain(plan, "required_scenario_missing")
    if sum(item["scenario"] == "normal_success" for item in comparisons) != 2:
        return _retain(plan, "clean_shadow_count_invalid")

    source = evidence["source"]
    source_evidence_sha256 = _source_hash(source)
    if audit["workflow_blob"] != source["restore_workflow_blob"]:
        return _retain(plan, "scheduled_audit_unavailable")
    checkpoint_content = audit["checkpoint_content"]
    if not _checkpoint_matches_source(checkpoint_content, source):
        return _retain(plan, "checkpoint_anchor_source_drift")
    for item in shadow_items:
        if (
            item["source_evidence_sha256"] != source_evidence_sha256
            or item["source_commit"] != source["source_commit"]
            or item["deploy_workflow_blob"] != source["deploy_workflow_blob"]
            or item["migration_horizon_sha256"]
            != source["migration_horizon_sha256"]
            or item["ledger_generation_sha256"]
            != source["ledger_generation_sha256"]
            or item["writer_lock_generation_sha256"]
            != source["writer_lock_generation_sha256"]
        ):
            return _retain(plan, "shadow_source_drift")
        if (
            item["active_writers"] != 0
            or item["terminal"] is not True
            or item["completed_at_parsed"] > source["captured_at_parsed"]
        ):
            return _retain(plan, "shadow_not_terminal")
    anchored_items = [
        *comparisons,
        evidence["negative_controls"]["planted_open_row"],
        evidence["negative_controls"]["artifact_without_ledger"],
    ]
    for item in anchored_items:
        checkpoint_completed_at = datetime.fromisoformat(
            item["checkpoint_content"]["scan_completed_at"].replace("Z", "+00:00")
        )
        lock_acquired_at = item["indexed"]["receipt"]["lock_acquired_at_parsed"]
        actual_delta_seconds = (
            lock_acquired_at - checkpoint_completed_at
        ).total_seconds()
        if (
            not _checkpoint_matches_source(item["checkpoint_content"], source)
            or not _legacy_matches_checkpoint(
                item["legacy"], item["checkpoint_content"]
            )
            or actual_delta_seconds <= 0
            or actual_delta_seconds > 86400
            or actual_delta_seconds
            != item["indexed"]["receipt"]["bounded_delta_seconds"]
            or lock_acquired_at > item["completed_at_parsed"]
        ):
            return _retain(plan, "shadow_checkpoint_anchor_invalid")
    disagreements = 0
    for item in comparisons:
        if (
            item["legacy"]["workflow_blob"] != source["deploy_workflow_blob"]
            or not _equal_markers(item["legacy"], item["indexed"])
        ):
            disagreements += 1
    plan["disagreement_count"] = disagreements
    if disagreements:
        return _retain(plan, "marker_shadow_mismatch")

    open_control = evidence["negative_controls"]["planted_open_row"]
    open_passed = bool(
        open_control["source_evidence_sha256"] == source_evidence_sha256
        and open_control["legacy"]["workflow_blob"] == source["deploy_workflow_blob"]
        and
        open_control["replacement_blocks"]
        and not open_control["integrity_alarm"]
        and _equal_markers(open_control["legacy"], open_control["indexed"])
        and open_control["legacy"]["result"] == "OPEN"
        and open_control["legacy"]["open_count"] > 0
    )
    plan["planted_open_row_passed"] = open_passed
    if not open_passed:
        return _retain(plan, "planted_open_row_control_failed")

    mismatch_control = evidence["negative_controls"]["artifact_without_ledger"]
    mismatch_passed = bool(
        mismatch_control["source_evidence_sha256"] == source_evidence_sha256
        and mismatch_control["legacy"]["workflow_blob"]
        == source["deploy_workflow_blob"]
        and
        mismatch_control["replacement_blocks"]
        and mismatch_control["integrity_alarm"]
        and mismatch_control["legacy"]["result"] == "OPEN"
        and mismatch_control["legacy"]["open_count"] > 0
        and not _equal_markers(
            mismatch_control["legacy"], mismatch_control["indexed"]
        )
    )
    plan["artifact_without_ledger_passed"] = mismatch_passed
    if not mismatch_passed:
        return _retain(plan, "artifact_without_ledger_control_failed")

    checkpoint_control = evidence["negative_controls"]["stale_or_missing_checkpoint"]
    checkpoint_passed = bool(
        checkpoint_control["source_evidence_sha256"] == source_evidence_sha256
        and checkpoint_control["legacy"]["workflow_blob"]
        == source["deploy_workflow_blob"]
        and (
            checkpoint_control["checkpoint_state"] == "missing"
            and checkpoint_control["observed_checkpoint_sha256"] is None
            or checkpoint_control["checkpoint_state"] == "stale"
            and checkpoint_control["observed_checkpoint_sha256"]
            != audit["checkpoint_sha256"]
        )
        and checkpoint_control["replacement_blocks"]
        and checkpoint_control["fallback_to_full_scan"]
        and checkpoint_control["legacy"]["checkpoint_sha256"]
        == _json_sha256(checkpoint_control["fallback_checkpoint_content"])
        and _checkpoint_matches_source(
            checkpoint_control["fallback_checkpoint_content"], source
        )
        and checkpoint_control["fallback_checkpoint_content"]["scan_completed_at"]
        == checkpoint_control["completed_at"]
    )
    plan["stale_or_missing_checkpoint_passed"] = checkpoint_passed
    if not checkpoint_passed:
        return _retain(plan, "checkpoint_fallback_control_failed")

    if plan["measured_indexed_median_seconds"] >= 10:
        return _retain(plan, "indexed_path_too_slow")

    plan.update(
        disposition="eligible_for_staging_cutover_review",
        code="shadow_equivalence_ready",
    )
    return plan


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="-", help="Evidence JSON path, or - for stdin")
    parser.add_argument("--output", help="Optional path for the safe recommendation")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = _arguments(argv)
    try:
        if arguments.input == "-":
            evidence = load_json(sys.stdin)
        else:
            with Path(arguments.input).open(encoding="utf-8") as handle:
                evidence = load_json(handle)
        plan = evaluate_gate(evidence)
    except (ContractError, OSError) as exc:
        plan = {
            "schema": PLAN_SCHEMA,
            "disposition": "invalid",
            "code": str(exc),
            "gate_change_authorized": False,
            "selector_activation_authorized": False,
            "dispatch_authorized": False,
            "live_mutation_authorized": False,
        }
        rendered = canonical_json(plan) + "\n"
        if arguments.output:
            Path(arguments.output).write_text(rendered, encoding="utf-8", newline="\n")
        sys.stdout.write(rendered)
        return 2
    rendered = canonical_json(plan) + "\n"
    if arguments.output:
        Path(arguments.output).write_text(rendered, encoding="utf-8", newline="\n")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
