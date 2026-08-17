#!/usr/bin/env python3
"""Evaluate dormant release guardrails from closed captured evidence.

The evaluator is pure. It has no provider client, credential access, workflow
dispatch, selector activation, or live mutation authority.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping, TextIO


INPUT_SCHEMA = "leaf.platform-release-guardrails.v1"
OUTPUT_SCHEMA = "leaf.platform-release-guardrail-result.v1"
SERVICES = ("web", "app", "broker", "harness", "canonical-worker")
RECEIPT_BINDING_FIELDS = (
    "convergence_id",
    "parent_run_id",
    "run_attempt",
    "dependency_generation",
    "source_commit",
    "source_tree",
    "workflow_blob",
    "supply_artifact_id",
    "supply_sha256",
    "supply_predicate_sha256",
    "payload_sha256",
    "mutation_idempotency_key",
)
SERVICE_STATE_FIELDS = (
    "image_digest",
    "source_revision",
    "runtime_contract_sha256",
    "migration_fingerprint_sha256",
    "configuration_fingerprint_sha256",
    "route_state",
    "health_state",
)
AUTHORITY_FIELDS = (
    "target_sha256",
    "blast_radius_sha256",
    "rollback_sha256",
    "permissions_sha256",
    "credentials_sha256",
    "destructive_actions_sha256",
    "external_recipients_sha256",
    "workflow_blobs_sha256",
    "rendered_actions_sha256",
)
HARM_CLASSES = {
    "externally_required_control",
    "irreversibility_without_verified_rollback",
    "authority_or_secret_mutation",
    "live_lifecycle_ownership_conflict",
    "cross_session_shared_state_collision",
    "tenancy_authorization_or_data_integrity",
}

_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,199}$")


class ContractError(ValueError):
    """Captured evidence does not satisfy the closed guardrail contract."""


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
        raise ContractError("guardrail_json_invalid") from exc
    if not isinstance(value, dict):
        raise ContractError("guardrail_root_invalid")
    return value


def _exact(value: Any, keys: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ContractError(code)
    return value


def _string(value: Any, pattern: re.Pattern[str], code: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ContractError(code)
    return value


def _integer(value: Any, code: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractError(code)
    return value


def _boolean(value: Any, code: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(code)
    return value


def _digest(value: Any, code: str) -> str:
    return _string(value, _DIGEST, code)


def _sha(value: Any, code: str) -> str:
    return _string(value, _SHA, code)


def _id(value: Any, code: str) -> str:
    return _string(value, _ID, code)


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _receipt_binding(value: Any, code: str) -> dict[str, Any]:
    receipt = _exact(value, set(RECEIPT_BINDING_FIELDS), code)
    parsed = dict(receipt)
    parsed["convergence_id"] = _id(receipt["convergence_id"], code)
    parsed["parent_run_id"] = _integer(receipt["parent_run_id"], code, 1)
    parsed["run_attempt"] = _integer(receipt["run_attempt"], code, 1)
    parsed["dependency_generation"] = _integer(
        receipt["dependency_generation"], code, 1
    )
    for key in ("source_commit", "source_tree", "workflow_blob"):
        parsed[key] = _sha(receipt[key], code)
    parsed["supply_artifact_id"] = _integer(
        receipt["supply_artifact_id"], code, 1
    )
    for key in (
        "supply_sha256",
        "supply_predicate_sha256",
        "payload_sha256",
    ):
        parsed[key] = _digest(receipt[key], code)
    mutation_key = receipt["mutation_idempotency_key"]
    if mutation_key is not None:
        parsed["mutation_idempotency_key"] = _id(mutation_key, code)
    return parsed


def _substitute_decision(value: Any) -> dict[str, Any]:
    item = _exact(
        value,
        {"receipt_tool_failed", "expected_binding", "substitute_binding"},
        "substitute_evidence_invalid",
    )
    failed = _boolean(item["receipt_tool_failed"], "substitute_evidence_invalid")
    expected = _receipt_binding(
        item["expected_binding"], "substitute_expected_binding_invalid"
    )
    candidate = _receipt_binding(
        item["substitute_binding"], "substitute_candidate_binding_invalid"
    )
    mismatches = [
        field
        for field in RECEIPT_BINDING_FIELDS
        if expected[field] != candidate[field]
    ]
    advisory = failed and not mismatches
    return {
        "decision": "advisory" if advisory else "blocking",
        "code": "same_run_substitute_exact" if advisory else "substitute_binding_drift",
        "mismatches": mismatches,
    }


def _service_state(value: Any, code: str) -> dict[str, Any]:
    item = _exact(value, set(SERVICE_STATE_FIELDS), code)
    parsed = dict(item)
    parsed["image_digest"] = _digest(item["image_digest"], code)
    parsed["source_revision"] = _sha(item["source_revision"], code)
    for key in ("runtime_contract_sha256", "configuration_fingerprint_sha256"):
        parsed[key] = _digest(item[key], code)
    migration = item["migration_fingerprint_sha256"]
    if migration is not None:
        parsed["migration_fingerprint_sha256"] = _digest(migration, code)
    if item["route_state"] not in {"stable", "not_applicable"}:
        raise ContractError(code)
    if item["health_state"] != "healthy":
        raise ContractError(code)
    return parsed


def _service_map(value: Any, code: str) -> dict[str, dict[str, Any]]:
    raw = _exact(value, set(SERVICES), code)
    return {service: _service_state(raw[service], code) for service in SERVICES}


def _effective_state_decision(value: Any) -> dict[str, Any]:
    item = _exact(
        value,
        {"candidate_services", "live_services", "deployment_identity"},
        "effective_state_invalid",
    )
    candidate = _service_map(item["candidate_services"], "candidate_service_state_invalid")
    live = _service_map(item["live_services"], "live_service_state_invalid")
    identity = _exact(
        item["deployment_identity"],
        {"schema", "environment", "source_revision", "services"},
        "deployment_identity_invalid",
    )
    if identity["schema"] != "leaf.deployment-identity.v1":
        raise ContractError("deployment_identity_invalid")
    if identity["environment"] != "staging":
        raise ContractError("deployment_identity_invalid")
    identity_source = _sha(identity["source_revision"], "deployment_identity_invalid")
    raw_identity_services = _exact(
        identity["services"], set(SERVICES), "deployment_identity_invalid"
    )
    drift: list[str] = []
    for service in SERVICES:
        for field in SERVICE_STATE_FIELDS:
            if candidate[service][field] != live[service][field]:
                drift.append(f"{service}.{field}")
        identity_service = _exact(
            raw_identity_services[service],
            {"image_digest", "source_revision"},
            "deployment_identity_invalid",
        )
        if _digest(
            identity_service["image_digest"], "deployment_identity_invalid"
        ) != candidate[service]["image_digest"]:
            drift.append(f"identity.{service}.image_digest")
        if _sha(
            identity_service["source_revision"], "deployment_identity_invalid"
        ) != candidate[service]["source_revision"]:
            drift.append(f"identity.{service}.source_revision")
        if identity_source != candidate[service]["source_revision"]:
            drift.append(f"identity.{service}.release_source")
    return {
        "decision": "skip" if not drift else "reconcile",
        "code": "effective_state_exact" if not drift else "effective_state_drift",
        "drift": sorted(set(drift)),
    }


def _authority(value: Any, code: str) -> dict[str, str]:
    item = _exact(value, set(AUTHORITY_FIELDS), code)
    return {field: _digest(item[field], code) for field in AUTHORITY_FIELDS}


def _authority_decision(value: Any) -> dict[str, Any]:
    item = _exact(value, {"approved", "corrected"}, "authority_diff_invalid")
    approved = _authority(item["approved"], "approved_authority_invalid")
    corrected = _authority(item["corrected"], "corrected_authority_invalid")
    widened = [field for field in AUTHORITY_FIELDS if approved[field] != corrected[field]]
    return {
        "decision": "inherit" if not widened else "new_authority_required",
        "code": "effective_authority_exact" if not widened else "effective_authority_drift",
        "changed_fields": widened,
    }


def _restamp_decision(value: Any) -> dict[str, Any]:
    item = _exact(
        value,
        {"expected_parent_run_id", "expected_workflow_blob", "executions"},
        "restamp_executor_invalid",
    )
    parent = _integer(item["expected_parent_run_id"], "restamp_executor_invalid", 1)
    workflow_blob = _sha(item["expected_workflow_blob"], "restamp_executor_invalid")
    executions = item["executions"]
    if not isinstance(executions, list):
        raise ContractError("restamp_executor_invalid")
    valid = 0
    invalid = 0
    for execution in executions:
        entry = _exact(
            execution,
            {"parent_run_id", "workflow_blob", "intent"},
            "restamp_executor_invalid",
        )
        matches = (
            _integer(entry["parent_run_id"], "restamp_executor_invalid", 1) == parent
            and _sha(entry["workflow_blob"], "restamp_executor_invalid") == workflow_blob
            and entry["intent"] == "configuration"
        )
        valid += int(matches)
        invalid += int(not matches)
    allowed = valid == 1 and invalid == 0 and len(executions) == 1
    return {
        "decision": "allow" if allowed else "reject",
        "code": "canonical_relay_executor" if allowed else "restamp_executor_conflict",
        "execution_count": len(executions),
    }


def _oracle_decision(value: Any) -> dict[str, Any]:
    item = _exact(
        value,
        {
            "raw_provider_bytes_b64",
            "declared_raw_provider_bytes_sha256",
            "shared_predicate_sha256",
            "binding_recomputed_from_raw",
        },
        "provider_oracle_invalid",
    )
    encoded = item["raw_provider_bytes_b64"]
    if not isinstance(encoded, str) or not encoded:
        raise ContractError("provider_raw_bytes_invalid")
    try:
        raw_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ContractError("provider_raw_bytes_invalid") from exc
    if base64.b64encode(raw_bytes).decode("ascii") != encoded:
        raise ContractError("provider_raw_bytes_noncanonical")
    declared = _digest(
        item["declared_raw_provider_bytes_sha256"], "provider_oracle_invalid"
    )
    recomputed = f"sha256:{hashlib.sha256(raw_bytes).hexdigest()}"
    shared = _digest(item["shared_predicate_sha256"], "provider_oracle_invalid")
    bound = _boolean(item["binding_recomputed_from_raw"], "provider_oracle_invalid")
    exact = hmac.compare_digest(declared, recomputed) and bound
    return {
        "decision": "accept" if exact else "reject",
        "code": "raw_provider_recomputed" if exact else "raw_provider_oracle_mismatch",
        "shared_predicate_agreement": hmac.compare_digest(shared, declared),
    }


def _marker_decision(value: Any) -> dict[str, Any]:
    item = _exact(
        value,
        {
            "checkpoint_sha256",
            "delta",
            "indexed_open_count",
            "indexed_open_set_sha256",
            "full_audit_open_count",
            "full_audit_open_set_sha256",
            "artifact_without_ledger_count",
            "last_completed_full_audit_age_seconds",
            "maximum_full_audit_age_seconds",
        },
        "marker_proof_invalid",
    )
    checkpoint = _digest(item["checkpoint_sha256"], "marker_proof_invalid")
    delta = _exact(
        item["delta"],
        {"checkpoint_sha256", "settlements", "result_open_set_sha256", "delta_sha256"},
        "marker_delta_invalid",
    )
    settlements = delta["settlements"]
    if not isinstance(settlements, list):
        raise ContractError("marker_delta_invalid")
    parsed_settlements: list[dict[str, Any]] = []
    prior_ordinal = -1
    for raw in settlements:
        settlement = _exact(
            raw,
            {"ordinal", "transaction_id", "marker_sha256", "artifact_status"},
            "marker_delta_invalid",
        )
        ordinal = _integer(settlement["ordinal"], "marker_delta_invalid")
        if ordinal <= prior_ordinal:
            raise ContractError("marker_delta_order_invalid")
        prior_ordinal = ordinal
        if settlement["artifact_status"] not in {"terminal", "rolled_back"}:
            raise ContractError("marker_delta_invalid")
        parsed_settlements.append(
            {
                "ordinal": ordinal,
                "transaction_id": _id(
                    settlement["transaction_id"], "marker_delta_invalid"
                ),
                "marker_sha256": _digest(
                    settlement["marker_sha256"], "marker_delta_invalid"
                ),
                "artifact_status": settlement["artifact_status"],
            }
        )
    delta_body = {
        "checkpoint_sha256": _digest(
            delta["checkpoint_sha256"], "marker_delta_invalid"
        ),
        "settlements": parsed_settlements,
        "result_open_set_sha256": _digest(
            delta["result_open_set_sha256"], "marker_delta_invalid"
        ),
    }
    supplied_delta_sha = _digest(delta["delta_sha256"], "marker_delta_invalid")
    indexed_count = _integer(item["indexed_open_count"], "marker_proof_invalid")
    full_count = _integer(item["full_audit_open_count"], "marker_proof_invalid")
    indexed_set = _digest(item["indexed_open_set_sha256"], "marker_proof_invalid")
    full_set = _digest(item["full_audit_open_set_sha256"], "marker_proof_invalid")
    artifact_gap = _integer(
        item["artifact_without_ledger_count"], "marker_proof_invalid"
    )
    audit_age = _integer(
        item["last_completed_full_audit_age_seconds"], "marker_proof_invalid"
    )
    maximum_audit_age = _integer(
        item["maximum_full_audit_age_seconds"], "marker_proof_invalid", 1
    )
    failures: list[str] = []
    if delta_body["checkpoint_sha256"] != checkpoint:
        failures.append("stale_checkpoint_anchor")
    if not hmac.compare_digest(_canonical_sha256(delta_body), supplied_delta_sha):
        failures.append("delta_content_hash_mismatch")
    if indexed_count != full_count or indexed_set != full_set:
        failures.append("indexed_full_audit_mismatch")
    if delta_body["result_open_set_sha256"] != indexed_set:
        failures.append("delta_open_set_mismatch")
    if indexed_count != 0:
        failures.append("open_marker_present")
    if artifact_gap != 0:
        failures.append("artifact_without_ledger")
    if audit_age > maximum_audit_age:
        failures.append("scheduled_full_audit_stale")
    return {
        "decision": "accept" if not failures else "reject",
        "code": "checkpoint_delta_exact" if not failures else "marker_proof_failed",
        "failures": failures,
    }


def _gate_decisions(value: Any) -> dict[str, Any]:
    if not isinstance(value, list):
        raise ContractError("gate_candidates_invalid")
    results: list[dict[str, str]] = []
    for raw in value:
        gate = _exact(
            raw,
            {
                "gate_id",
                "stage",
                "enabled_path_failure",
                "harm_class",
                "reachable",
                "current_evidence_id",
                "evidence_fresh",
                "duplicates_stronger_evidence",
                "stronger_green_evidence_id",
                "why_unique",
            },
            "gate_candidate_invalid",
        )
        gate_id = _id(gate["gate_id"], "gate_candidate_invalid")
        stage = gate["stage"] if isinstance(gate["stage"], str) else ""
        failure = (
            gate["enabled_path_failure"]
            if isinstance(gate["enabled_path_failure"], str)
            else ""
        )
        harm = gate["harm_class"]
        reachable = _boolean(gate["reachable"], "gate_candidate_invalid")
        evidence = gate["current_evidence_id"]
        evidence_fresh = _boolean(gate["evidence_fresh"], "gate_candidate_invalid")
        duplicate = _boolean(
            gate["duplicates_stronger_evidence"], "gate_candidate_invalid"
        )
        stronger = gate["stronger_green_evidence_id"]
        why_unique = gate["why_unique"] if isinstance(gate["why_unique"], str) else ""
        complete = (
            bool(stage)
            and bool(failure)
            and harm in HARM_CLASSES
            and reachable
            and isinstance(evidence, str)
            and _ID.fullmatch(evidence) is not None
            and evidence_fresh
            and bool(why_unique)
        )
        if duplicate:
            complete = complete and isinstance(stronger, str) and _ID.fullmatch(stronger) is not None
        blocks = complete and not duplicate
        results.append(
            {
                "gate_id": gate_id,
                "decision": "blocking" if blocks else "advisory",
                "code": "minimum_blocker_satisfied" if blocks else "minimum_blocker_not_satisfied",
            }
        )
    return {
        "decision": "blocking" if any(x["decision"] == "blocking" for x in results) else "advisory",
        "gates": results,
    }


def evaluate(payload: Mapping[str, Any]) -> dict[str, Any]:
    root = _exact(
        payload,
        {
            "schema",
            "substitute_evidence",
            "effective_state",
            "authority_diff",
            "identity_restamp",
            "provider_oracle",
            "marker_proof",
            "proposed_gates",
        },
        "guardrail_contract_invalid",
    )
    if root["schema"] != INPUT_SCHEMA:
        raise ContractError("guardrail_schema_invalid")
    decisions = {
        "substitute_evidence": _substitute_decision(root["substitute_evidence"]),
        "effective_state": _effective_state_decision(root["effective_state"]),
        "authority_diff": _authority_decision(root["authority_diff"]),
        "identity_restamp": _restamp_decision(root["identity_restamp"]),
        "provider_oracle": _oracle_decision(root["provider_oracle"]),
        "marker_proof": _marker_decision(root["marker_proof"]),
        "proposed_gates": _gate_decisions(root["proposed_gates"]),
    }
    ready = (
        decisions["substitute_evidence"]["decision"] == "advisory"
        and decisions["effective_state"]["decision"] == "skip"
        and decisions["authority_diff"]["decision"] == "inherit"
        and decisions["identity_restamp"]["decision"] == "allow"
        and decisions["provider_oracle"]["decision"] == "accept"
        and decisions["marker_proof"]["decision"] == "accept"
        and decisions["proposed_gates"]["decision"] == "advisory"
    )
    result = {
        "schema": OUTPUT_SCHEMA,
        "status": "ready" if ready else "stopped",
        "code": "all_guardrails_satisfied" if ready else "guardrail_action_required",
        "decisions": decisions,
        "selector_activation_authorized": False,
        "dispatch_authorized": False,
        "live_mutation_authorized": False,
    }
    result["result_sha256"] = _canonical_sha256(result)
    return result


def _write_result(path: str, result: Mapping[str, Any]) -> None:
    text = json.dumps(result, sort_keys=True, indent=2) + "\n"
    if path == "-":
        sys.stdout.write(text)
    else:
        Path(path).write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="-")
    parser.add_argument("--output", default="-")
    args = parser.parse_args(argv)
    try:
        if args.input == "-":
            payload = load_json(sys.stdin)
        else:
            with Path(args.input).open(encoding="utf-8") as stream:
                payload = load_json(stream)
        result = evaluate(payload)
        _write_result(args.output, result)
        return 0 if result["status"] == "ready" else 78
    except ContractError as exc:
        result = {
            "schema": OUTPUT_SCHEMA,
            "status": "stopped",
            "code": str(exc),
            "decisions": {},
            "selector_activation_authorized": False,
            "dispatch_authorized": False,
            "live_mutation_authorized": False,
        }
        result["result_sha256"] = _canonical_sha256(result)
        _write_result(args.output, result)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
