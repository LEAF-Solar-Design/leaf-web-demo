#!/usr/bin/env python3
"""Qualify dormant marker-ledger cutover evidence without changing a gate."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import sys
from typing import Any, Mapping, TextIO


INPUT_SCHEMA = "leaf.platform-marker-cutover-qualification.v1"
PLAN_SCHEMA = "leaf.platform-marker-cutover-plan.v1"
SCENARIOS = {
    "normal_success",
    "forced_cancellation",
    "missing_artifact",
    "failed_settlement",
    "expired_lease",
    "planted_open_row",
    "artifact_without_ledger",
    "stale_or_missing_checkpoint",
}
MATCHED_FAULTS = {
    "forced_cancellation",
    "missing_artifact",
    "failed_settlement",
    "expired_lease",
    "planted_open_row",
}

_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,199}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)


class ContractError(ValueError):
    """Evidence is malformed and cannot produce a dormant recommendation."""


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ContractError("duplicate_json_key")
        value[key] = item
    return value


def load_json(stream: TextIO) -> dict[str, Any]:
    try:
        value = json.load(stream, object_pairs_hook=_object)
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("evidence_json_invalid") from exc
    if not isinstance(value, dict):
        raise ContractError("evidence_root_invalid")
    return value


def _exact(value: Any, keys: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ContractError(code)
    return value


def _pattern(value: Any, pattern: re.Pattern[str], code: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ContractError(code)
    return value


def _integer(value: Any, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError(code)
    return value


def _number(value: Any, code: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ContractError(code)
    return float(value)


def _timestamp(value: Any, code: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise ContractError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(code) from exc
    if parsed.tzinfo is None:
        raise ContractError(code)
    return value, parsed


def _json_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


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
    "captured_at",
}


def _source(value: Any) -> dict[str, Any]:
    item = _exact(value, SOURCE_KEYS, "source_invalid")
    captured_at, captured = _timestamp(item["captured_at"], "source_invalid")
    return {
        "source_commit": _pattern(item["source_commit"], _SHA, "source_invalid"),
        "source_tree": _pattern(item["source_tree"], _SHA, "source_invalid"),
        "deploy_workflow_blob": _pattern(
            item["deploy_workflow_blob"], _SHA, "source_invalid"
        ),
        "restore_workflow_blob": _pattern(
            item["restore_workflow_blob"], _SHA, "source_invalid"
        ),
        "ledger_script_blob": _pattern(
            item["ledger_script_blob"], _SHA, "source_invalid"
        ),
        "legacy_census_script_blob": _pattern(
            item["legacy_census_script_blob"], _SHA, "source_invalid"
        ),
        "migration_horizon_sha256": _pattern(
            item["migration_horizon_sha256"], _DIGEST, "source_invalid"
        ),
        "ledger_generation_sha256": _pattern(
            item["ledger_generation_sha256"], _DIGEST, "source_invalid"
        ),
        "writer_lock_generation_sha256": _pattern(
            item["writer_lock_generation_sha256"], _DIGEST, "source_invalid"
        ),
        "captured_at": captured_at,
        "captured_at_parsed": captured,
    }


CHECKPOINT_KEYS = {
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


def _result_fields(item: Mapping[str, Any], code: str) -> dict[str, Any]:
    if item["result"] not in {"EMPTY", "OPEN"}:
        raise ContractError(code)
    count = _integer(item["open_count"], code)
    if (count == 0) != (item["result"] == "EMPTY"):
        raise ContractError(code)
    return {
        "result": item["result"],
        "open_count": count,
        "open_set_sha256": _pattern(item["open_set_sha256"], _HASH, code),
    }


def _checkpoint(value: Any) -> dict[str, Any]:
    envelope = _exact(value, {"sha256", "content"}, "checkpoint_invalid")
    content = _exact(envelope["content"], CHECKPOINT_KEYS, "checkpoint_invalid")
    if content["schema"] != "leaf.staging-marker-checkpoint-anchor.v1":
        raise ContractError("checkpoint_invalid")
    started_at, started = _timestamp(
        content["scan_started_at"], "checkpoint_invalid"
    )
    completed_at, completed = _timestamp(
        content["scan_completed_at"], "checkpoint_invalid"
    )
    if completed < started:
        raise ContractError("checkpoint_invalid")
    normalized = {
        "schema": content["schema"],
        "source_commit": _pattern(
            content["source_commit"], _SHA, "checkpoint_invalid"
        ),
        "source_tree": _pattern(content["source_tree"], _SHA, "checkpoint_invalid"),
        "deploy_workflow_blob": _pattern(
            content["deploy_workflow_blob"], _SHA, "checkpoint_invalid"
        ),
        "restore_workflow_blob": _pattern(
            content["restore_workflow_blob"], _SHA, "checkpoint_invalid"
        ),
        "ledger_script_blob": _pattern(
            content["ledger_script_blob"], _SHA, "checkpoint_invalid"
        ),
        "legacy_census_script_blob": _pattern(
            content["legacy_census_script_blob"], _SHA, "checkpoint_invalid"
        ),
        "migration_horizon_sha256": _pattern(
            content["migration_horizon_sha256"], _DIGEST, "checkpoint_invalid"
        ),
        "scan_started_at": started_at,
        "scan_completed_at": completed_at,
        **_result_fields(content, "checkpoint_invalid"),
    }
    sha256 = _pattern(envelope["sha256"], _DIGEST, "checkpoint_invalid")
    if sha256 != _json_hash(normalized):
        raise ContractError("checkpoint_invalid")
    return {
        "sha256": sha256,
        "content": normalized,
        "scan_completed_at_parsed": completed,
    }


FULL_SCAN_KEYS = {
    "schema",
    "checkpoint",
    "workflow_blob",
    "result",
    "open_count",
    "open_set_sha256",
    "duration_seconds",
}


def _full_scan(value: Any) -> dict[str, Any]:
    item = _exact(value, FULL_SCAN_KEYS, "full_scan_invalid")
    if item["schema"] != "leaf.legacy-marker-census.v1":
        raise ContractError("full_scan_invalid")
    return {
        "schema": item["schema"],
        "checkpoint": _checkpoint(item["checkpoint"]),
        "workflow_blob": _pattern(
            item["workflow_blob"], _SHA, "full_scan_invalid"
        ),
        **_result_fields(item, "full_scan_invalid"),
        "duration_seconds": _number(item["duration_seconds"], "full_scan_invalid"),
    }


INDEXED_KEYS = {
    "schema",
    "checkpoint_sha256",
    "result",
    "open_count",
    "open_set_sha256",
    "duration_seconds",
    "lock_acquired_at",
    "bounded_delta_seconds",
    "strong_consistent",
    "writer_lock_held",
    "ledger_union_delta_exact",
    "pre_post_snapshot_stable",
    "fallback_to_full_scan_on_error",
}


def _indexed(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    item = _exact(value, INDEXED_KEYS, "indexed_invalid")
    if (
        item["schema"] != "leaf.staging-marker-ledger-census.v1"
        or item["strong_consistent"] is not True
        or item["writer_lock_held"] is not True
        or item["ledger_union_delta_exact"] is not True
        or item["pre_post_snapshot_stable"] is not True
        or item["fallback_to_full_scan_on_error"] is not True
    ):
        raise ContractError("indexed_invalid")
    lock_at, lock = _timestamp(item["lock_acquired_at"], "indexed_invalid")
    delta = _number(item["bounded_delta_seconds"], "indexed_invalid")
    if delta > 86400:
        raise ContractError("indexed_invalid")
    return {
        "schema": item["schema"],
        "checkpoint_sha256": _pattern(
            item["checkpoint_sha256"], _DIGEST, "indexed_invalid"
        ),
        **_result_fields(item, "indexed_invalid"),
        "duration_seconds": _number(item["duration_seconds"], "indexed_invalid"),
        "lock_acquired_at": lock_at,
        "lock_acquired_at_parsed": lock,
        "bounded_delta_seconds": delta,
        "strong_consistent": True,
        "writer_lock_held": True,
        "ledger_union_delta_exact": True,
        "pre_post_snapshot_stable": True,
        "fallback_to_full_scan_on_error": True,
    }


SHADOW_KEYS = {
    "transaction_id",
    "terminal_receipt_sha256",
    "scenario",
    "source_evidence_sha256",
    "terminal",
    "active_writers",
    "completed_at",
    "checkpoint_state",
    "observed_checkpoint_sha256",
    "full_scan",
    "indexed",
    "replacement_blocks",
    "integrity_alarm",
    "fallback_to_full_scan",
}


def _shadow(value: Any) -> dict[str, Any]:
    item = _exact(value, SHADOW_KEYS, "shadow_invalid")
    if (
        item["scenario"] not in SCENARIOS
        or item["checkpoint_state"] not in {"current", "stale", "missing"}
        or not isinstance(item["terminal"], bool)
        or not isinstance(item["replacement_blocks"], bool)
        or not isinstance(item["integrity_alarm"], bool)
        or not isinstance(item["fallback_to_full_scan"], bool)
    ):
        raise ContractError("shadow_invalid")
    observed = item["observed_checkpoint_sha256"]
    if observed is not None:
        observed = _pattern(observed, _DIGEST, "shadow_invalid")
    completed_at, completed = _timestamp(item["completed_at"], "shadow_invalid")
    return {
        "transaction_id": _pattern(item["transaction_id"], _ID, "shadow_invalid"),
        "terminal_receipt_sha256": _pattern(
            item["terminal_receipt_sha256"], _DIGEST, "shadow_invalid"
        ),
        "scenario": item["scenario"],
        "source_evidence_sha256": _pattern(
            item["source_evidence_sha256"], _DIGEST, "shadow_invalid"
        ),
        "terminal": item["terminal"],
        "active_writers": _integer(item["active_writers"], "shadow_invalid"),
        "completed_at": completed_at,
        "completed_at_parsed": completed,
        "checkpoint_state": item["checkpoint_state"],
        "observed_checkpoint_sha256": observed,
        "full_scan": _full_scan(item["full_scan"]),
        "indexed": _indexed(item["indexed"]),
        "replacement_blocks": item["replacement_blocks"],
        "integrity_alarm": item["integrity_alarm"],
        "fallback_to_full_scan": item["fallback_to_full_scan"],
    }


def _source_public(source: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in source.items() if not key.endswith("_parsed")}


def _checkpoint_matches_source(
    checkpoint: Mapping[str, Any], source: Mapping[str, Any]
) -> bool:
    content = checkpoint["content"]
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


def _full_matches_checkpoint(full: Mapping[str, Any]) -> bool:
    content = full["checkpoint"]["content"]
    return bool(
        full["result"] == content["result"]
        and full["open_count"] == content["open_count"]
        and full["open_set_sha256"] == content["open_set_sha256"]
    )


def _results_equal(full: Mapping[str, Any], indexed: Mapping[str, Any]) -> bool:
    return bool(
        full["checkpoint"]["sha256"] == indexed["checkpoint_sha256"]
        and full["result"] == indexed["result"]
        and full["open_count"] == indexed["open_count"]
        and full["open_set_sha256"] == indexed["open_set_sha256"]
    )


def _base(shadows: list[Mapping[str, Any]]) -> dict[str, Any]:
    indexed_times = [
        item["indexed"]["duration_seconds"]
        for item in shadows
        if item["indexed"] is not None
    ]
    return {
        "schema": PLAN_SCHEMA,
        "disposition": "retain_blocking_gate",
        "code": "insufficient_evidence",
        "shadow_count": len(shadows),
        "scenario_coverage": sorted({item["scenario"] for item in shadows}),
        "disagreement_count": 0,
        "indexed_median_seconds": (
            statistics.median(indexed_times) if indexed_times else None
        ),
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


def evaluate(value: Any) -> dict[str, Any]:
    """Return only a dormant cutover recommendation."""

    root = _exact(
        value,
        {"schema", "source", "posture", "scheduled_anchor", "shadows"},
        "evidence_invalid",
    )
    if root["schema"] != INPUT_SCHEMA:
        raise ContractError("evidence_invalid")
    source = _source(root["source"])
    posture = _exact(
        root["posture"],
        {"marker_ledger_mode", "digest_aware_reconcile", "active_writers"},
        "posture_invalid",
    )
    if (
        posture["marker_ledger_mode"] not in {"disabled", "shadow", "enabled"}
        or not isinstance(posture["digest_aware_reconcile"], bool)
    ):
        raise ContractError("posture_invalid")
    posture = {
        **posture,
        "active_writers": _integer(posture["active_writers"], "posture_invalid"),
    }
    anchor = _exact(
        root["scheduled_anchor"],
        {
            "enabled",
            "terminal",
            "maximum_age_seconds",
            "terminal_receipt_sha256",
            "full_scan",
        },
        "scheduled_anchor_invalid",
    )
    if not isinstance(anchor["enabled"], bool) or anchor["terminal"] is not True:
        raise ContractError("scheduled_anchor_invalid")
    maximum_age = _number(
        anchor["maximum_age_seconds"], "scheduled_anchor_invalid"
    )
    if maximum_age > 604800:
        raise ContractError("scheduled_anchor_invalid")
    scheduled = {
        "enabled": anchor["enabled"],
        "terminal": True,
        "maximum_age_seconds": maximum_age,
        "terminal_receipt_sha256": _pattern(
            anchor["terminal_receipt_sha256"],
            _DIGEST,
            "scheduled_anchor_invalid",
        ),
        "full_scan": _full_scan(anchor["full_scan"]),
    }
    if not isinstance(root["shadows"], list) or len(root["shadows"]) != 9:
        raise ContractError("shadows_invalid")
    shadows = [_shadow(item) for item in root["shadows"]]
    plan = _base(shadows)

    if (
        posture["marker_ledger_mode"] != "disabled"
        or posture["digest_aware_reconcile"] is not False
    ):
        return _retain(plan, "selector_posture_not_dormant")
    if posture["active_writers"] != 0:
        return _retain(plan, "active_writer_present")

    scheduled_full = scheduled["full_scan"]
    scheduled_age = (
        source["captured_at_parsed"]
        - scheduled_full["checkpoint"]["scan_completed_at_parsed"]
    ).total_seconds()
    if (
        not scheduled["enabled"]
        or scheduled_age < 0
        or scheduled_age > scheduled["maximum_age_seconds"]
        or scheduled_full["workflow_blob"] != source["restore_workflow_blob"]
        or not _checkpoint_matches_source(scheduled_full["checkpoint"], source)
        or not _full_matches_checkpoint(scheduled_full)
    ):
        return _retain(plan, "scheduled_anchor_unavailable")

    transaction_ids = [item["transaction_id"] for item in shadows]
    receipt_ids = [item["terminal_receipt_sha256"] for item in shadows]
    if len(set(transaction_ids)) != 9 or len(set(receipt_ids)) != 9:
        return _retain(plan, "duplicate_shadow_identity")
    counts = {scenario: 0 for scenario in SCENARIOS}
    for item in shadows:
        counts[item["scenario"]] += 1
    if counts["normal_success"] != 2 or any(
        counts[scenario] != 1 for scenario in SCENARIOS - {"normal_success"}
    ):
        return _retain(plan, "scenario_cardinality_invalid")

    source_hash = _json_hash(_source_public(source))
    disagreements = 0
    indexed_times: list[float] = []
    for item in shadows:
        full = item["full_scan"]
        scenario = item["scenario"]
        if (
            item["source_evidence_sha256"] != source_hash
            or item["terminal"] is not True
            or item["active_writers"] != 0
            or item["completed_at_parsed"] > source["captured_at_parsed"]
            or full["workflow_blob"] != source["deploy_workflow_blob"]
            or not _checkpoint_matches_source(full["checkpoint"], source)
            or not _full_matches_checkpoint(full)
            or full["checkpoint"]["scan_completed_at_parsed"]
            > item["completed_at_parsed"]
        ):
            return _retain(plan, "shadow_identity_or_checkpoint_invalid")

        if scenario == "stale_or_missing_checkpoint":
            if (
                item["checkpoint_state"] == "current"
                or item["indexed"] is not None
                or item["replacement_blocks"] is not True
                or item["integrity_alarm"] is not False
                or item["fallback_to_full_scan"] is not True
                or item["checkpoint_state"] == "missing"
                and item["observed_checkpoint_sha256"] is not None
                or item["checkpoint_state"] == "stale"
                and (
                    item["observed_checkpoint_sha256"] is None
                    or item["observed_checkpoint_sha256"]
                    == full["checkpoint"]["sha256"]
                )
            ):
                return _retain(plan, "checkpoint_fallback_invalid")
            continue

        indexed = item["indexed"]
        if (
            item["checkpoint_state"] != "current"
            or item["observed_checkpoint_sha256"]
            != full["checkpoint"]["sha256"]
            or indexed is None
            or item["fallback_to_full_scan"] is not False
        ):
            return _retain(plan, "indexed_shadow_invalid")
        actual_delta = (
            indexed["lock_acquired_at_parsed"]
            - full["checkpoint"]["scan_completed_at_parsed"]
        ).total_seconds()
        if (
            actual_delta <= 0
            or actual_delta > 86400
            or actual_delta != indexed["bounded_delta_seconds"]
            or indexed["lock_acquired_at_parsed"] >= item["completed_at_parsed"]
        ):
            return _retain(plan, "indexed_delta_invalid")
        indexed_times.append(indexed["duration_seconds"])

        equal = _results_equal(full, indexed)
        if scenario == "artifact_without_ledger":
            if (
                equal
                or full["result"] != "OPEN"
                or full["open_count"] == 0
                or item["replacement_blocks"] is not True
                or item["integrity_alarm"] is not True
            ):
                return _retain(plan, "artifact_without_ledger_control_failed")
            continue
        if not equal:
            disagreements += 1
            continue
        if scenario == "normal_success":
            if (
                full["result"] != "EMPTY"
                or item["replacement_blocks"] is not False
                or item["integrity_alarm"] is not False
            ):
                return _retain(plan, "clean_shadow_invalid")
        elif scenario in MATCHED_FAULTS and (
            full["result"] != "OPEN"
            or full["open_count"] == 0
            or item["replacement_blocks"] is not True
            or item["integrity_alarm"] is not False
        ):
            return _retain(plan, "fault_shadow_invalid")

    plan["disagreement_count"] = disagreements
    if disagreements:
        return _retain(plan, "marker_shadow_mismatch")
    plan["indexed_median_seconds"] = statistics.median(indexed_times)
    if plan["indexed_median_seconds"] >= 10:
        return _retain(plan, "indexed_path_too_slow")
    plan.update(
        disposition="eligible_for_staging_cutover_review",
        code="shadow_evidence_ready",
    )
    return plan


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="-", help="Evidence JSON path, or -")
    parser.add_argument("--output", help="Optional output path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = _arguments(argv)
    try:
        if arguments.input == "-":
            evidence = load_json(sys.stdin)
        else:
            with Path(arguments.input).open(encoding="utf-8") as stream:
                evidence = load_json(stream)
        plan = evaluate(evidence)
        code = 0
    except ContractError as exc:
        plan = {
            "schema": PLAN_SCHEMA,
            "disposition": "invalid",
            "code": str(exc),
            "gate_change_authorized": False,
            "selector_activation_authorized": False,
            "dispatch_authorized": False,
            "live_mutation_authorized": False,
        }
        code = 2
    rendered = json.dumps(plan, sort_keys=True, separators=(",", ":")) + "\n"
    if arguments.output:
        Path(arguments.output).write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
