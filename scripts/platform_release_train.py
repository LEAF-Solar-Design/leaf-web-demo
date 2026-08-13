#!/usr/bin/env python3
"""Compile immutable release receipts into a dormant failed-stage resume plan.

This module is intentionally a pure planner. It does not import an AWS or
GitHub client, acquire credentials, or dispatch a workflow. A later rail may
consume its closed output only after separate activation review.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping, TextIO


TRAIN_SCHEMA = "leaf.platform-release-train.v1"
STAGE_SCHEMA = "leaf.platform-release-stage-receipt.v1"
PLAN_SCHEMA = "leaf.platform-release-resume-plan.v1"
SERVICES = ("web", "app", "broker", "harness", "canonical-worker")
STAGES = ("build", *SERVICES, "identity")
STAGE_ORDINAL = {stage: index for index, stage in enumerate(STAGES)}
SNAPSHOT_LIMIT = 5
SNAPSHOT_ACK_PREFIX = (
    "snapshot-overflow:807034087062:us-east-1:leaf-platform-staging:"
)

_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,199}$")
_TASK_DEFINITION = re.compile(
    r"^arn:aws:ecs:us-east-1:[0-9]{12}:task-definition/"
    r"leaf-platform-[a-z-]+:[1-9][0-9]*$"
)


class ContractError(ValueError):
    """Input evidence cannot safely produce a resume plan."""


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ContractError("duplicate_json_key")
        value[key] = item
    return value


def _exact_object(value: Any, keys: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ContractError(code)
    return value


def _integer(value: Any, code: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractError(code)
    return value


def _pattern(value: Any, pattern: re.Pattern[str], code: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ContractError(code)
    return value


def _boolean(value: Any, code: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(code)
    return value


def _timestamp(value: Any, code: str) -> datetime:
    if not isinstance(value, str):
        raise ContractError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(code) from exc
    if parsed.tzinfo is None:
        raise ContractError(code)
    return parsed


def load_json(stream: TextIO) -> dict[str, Any]:
    try:
        value = json.load(stream, object_pairs_hook=_object)
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("release_train_json_invalid") from exc
    if not isinstance(value, dict):
        raise ContractError("release_train_root_invalid")
    return value


def _validate_digests(value: Any, code: str) -> dict[str, str]:
    result = _exact_object(value, set(SERVICES), code)
    return {
        service: _pattern(result[service], _DIGEST, code)
        for service in SERVICES
    }


def _validate_source(value: Any) -> dict[str, str]:
    source = _exact_object(
        value, {"commit", "tree", "workflow_blob"}, "source_identity_invalid"
    )
    return {
        key: _pattern(source[key], _SHA, "source_identity_invalid")
        for key in ("commit", "tree", "workflow_blob")
    }


def _validate_supply(value: Any) -> dict[str, Any]:
    supply = _exact_object(
        value,
        {
            "artifact_id",
            "artifact_sha256",
            "predicate_sha256",
            "producer_ancestry_verified",
            "services",
        },
        "signed_supply_invalid",
    )
    if supply["producer_ancestry_verified"] is not True:
        raise ContractError("signed_supply_invalid")
    return {
        "artifact_id": _integer(
            supply["artifact_id"], "signed_supply_invalid", minimum=1
        ),
        "artifact_sha256": _pattern(
            supply["artifact_sha256"], _DIGEST, "signed_supply_invalid"
        ),
        "predicate_sha256": _pattern(
            supply["predicate_sha256"], _DIGEST, "signed_supply_invalid"
        ),
        "producer_ancestry_verified": True,
        "services": _validate_digests(supply["services"], "signed_supply_invalid"),
    }


def _validate_identity(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    identity = _exact_object(
        value,
        {"schema", "environment", "source_revision", "services", "body_sha256"},
        "deployment_identity_invalid",
    )
    if (
        identity["schema"] != "leaf.deployment-identity.v1"
        or identity["environment"] != "staging"
    ):
        raise ContractError("deployment_identity_invalid")
    source_revision = _pattern(
        identity["source_revision"], _SHA, "deployment_identity_invalid"
    )
    raw_services = _exact_object(
        identity["services"], set(SERVICES), "deployment_identity_invalid"
    )
    services: dict[str, dict[str, str]] = {}
    for service in SERVICES:
        raw_service = _exact_object(
            raw_services[service],
            {"image_digest", "source_revision"},
            "deployment_identity_invalid",
        )
        if raw_service["source_revision"] != source_revision:
            raise ContractError("deployment_identity_invalid")
        services[service] = {
            "image_digest": _pattern(
                raw_service["image_digest"], _DIGEST, "deployment_identity_invalid"
            ),
            "source_revision": source_revision,
        }
    body = {
        "schema": "leaf.deployment-identity.v1",
        "environment": "staging",
        "source_revision": source_revision,
        "services": services,
    }
    expected_body_sha256 = "sha256:" + hashlib.sha256(
        json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
    ).hexdigest()
    if identity["body_sha256"] != expected_body_sha256:
        raise ContractError("deployment_identity_invalid")
    return {**body, "body_sha256": expected_body_sha256}


def _validate_fresh_state(value: Any) -> dict[str, Any]:
    state = _exact_object(
        value,
        {
            "active_writers",
            "open_markers",
            "retained_snapshot_count",
            "snapshot_overflow_acknowledgement",
            "drawing_fence",
            "identity",
        },
        "fresh_state_invalid",
    )
    acknowledgement = state["snapshot_overflow_acknowledgement"]
    if acknowledgement is not None:
        if not isinstance(acknowledgement, str) or not acknowledgement.startswith(
            SNAPSHOT_ACK_PREFIX
        ):
            raise ContractError("snapshot_authority_invalid")
        suffix = acknowledgement.removeprefix(SNAPSHOT_ACK_PREFIX)
        if not suffix.isdigit():
            raise ContractError("snapshot_authority_invalid")
    if state["drawing_fence"] not in {"open", "closed"}:
        raise ContractError("drawing_fence_invalid")
    return {
        "active_writers": _integer(state["active_writers"], "fresh_state_invalid"),
        "open_markers": _integer(state["open_markers"], "fresh_state_invalid"),
        "retained_snapshot_count": _integer(
            state["retained_snapshot_count"], "fresh_state_invalid"
        ),
        "snapshot_overflow_acknowledgement": acknowledgement,
        "drawing_fence": state["drawing_fence"],
        "identity": _validate_identity(state["identity"]),
    }


SERVICE_KEYS = {
    "live_digest",
    "live_task_definition",
    "rollback_task_definition",
    "component_source_sha256",
    "expected_component_source_sha256",
    "runtime_contract_sha256",
    "expected_runtime_contract_sha256",
    "migration_fingerprint",
    "expected_migration_fingerprint",
    "route_stable",
    "health_stable",
}


def _validate_service(value: Any) -> dict[str, Any]:
    service = _exact_object(value, SERVICE_KEYS, "service_evidence_invalid")
    migration: dict[str, str | None] = {}
    for key in ("migration_fingerprint", "expected_migration_fingerprint"):
        item = service[key]
        migration[key] = (
            None
            if item is None
            else _pattern(item, _DIGEST, "service_evidence_invalid")
        )
    return {
        "live_digest": _pattern(
            service["live_digest"], _DIGEST, "service_evidence_invalid"
        ),
        "live_task_definition": _pattern(
            service["live_task_definition"],
            _TASK_DEFINITION,
            "service_evidence_invalid",
        ),
        "rollback_task_definition": _pattern(
            service["rollback_task_definition"],
            _TASK_DEFINITION,
            "service_evidence_invalid",
        ),
        "component_source_sha256": _pattern(
            service["component_source_sha256"],
            _DIGEST,
            "service_evidence_invalid",
        ),
        "expected_component_source_sha256": _pattern(
            service["expected_component_source_sha256"],
            _DIGEST,
            "service_evidence_invalid",
        ),
        "runtime_contract_sha256": _pattern(
            service["runtime_contract_sha256"],
            _DIGEST,
            "service_evidence_invalid",
        ),
        "expected_runtime_contract_sha256": _pattern(
            service["expected_runtime_contract_sha256"],
            _DIGEST,
            "service_evidence_invalid",
        ),
        **migration,
        "route_stable": _boolean(service["route_stable"], "service_evidence_invalid"),
        "health_stable": _boolean(
            service["health_stable"], "service_evidence_invalid"
        ),
    }


RECEIPT_KEYS = {
    "schema",
    "convergence_id",
    "parent_run_id",
    "run_attempt",
    "stage",
    "ordinal",
    "state",
    "source_commit",
    "source_tree",
    "workflow_blob",
    "supply_artifact_id",
    "supply_sha256",
    "service",
    "decision",
    "candidate_digest",
    "terminal_digest",
    "terminal_task_definition",
    "rollback_task_definition",
    "mutation_started",
    "mutation_idempotency_key",
    "started_at",
    "completed_at",
    "duration_seconds",
    "finalized_after_verify",
    "rollback_invoked",
    "failure_class",
    "payload_sha256",
}


def _nullable_pattern(value: Any, pattern: re.Pattern[str], code: str) -> str | None:
    return None if value is None else _pattern(value, pattern, code)


def _validate_receipt(
    value: Any,
    *,
    train: Mapping[str, Any],
) -> dict[str, Any]:
    receipt = _exact_object(value, RECEIPT_KEYS, "stage_receipt_invalid")
    if receipt["schema"] != STAGE_SCHEMA:
        raise ContractError("stage_receipt_invalid")
    supplied_payload_sha256 = _pattern(
        receipt["payload_sha256"], _DIGEST, "stage_receipt_integrity_invalid"
    )
    unsigned = {key: item for key, item in receipt.items() if key != "payload_sha256"}
    actual_payload_sha256 = "sha256:" + hashlib.sha256(
        json.dumps(
            unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
    ).hexdigest()
    if supplied_payload_sha256 != actual_payload_sha256:
        raise ContractError("stage_receipt_integrity_invalid")
    stage = receipt["stage"]
    ordinal = _integer(receipt["ordinal"], "stage_receipt_invalid")
    if stage not in STAGES or ordinal != STAGE_ORDINAL[stage]:
        raise ContractError("stage_receipt_invalid")
    _integer(receipt["parent_run_id"], "stage_receipt_invalid", minimum=1)
    _integer(receipt["run_attempt"], "stage_receipt_invalid", minimum=1)
    _integer(receipt["supply_artifact_id"], "stage_receipt_invalid", minimum=1)
    expected_identity = {
        "convergence_id": train["convergence_id"],
        "parent_run_id": train["parent_run_id"],
        "run_attempt": train["run_attempt"],
        "source_commit": train["source"]["commit"],
        "source_tree": train["source"]["tree"],
        "workflow_blob": train["source"]["workflow_blob"],
        "supply_artifact_id": train["supply"]["artifact_id"],
        "supply_sha256": train["supply"]["artifact_sha256"],
    }
    if any(receipt[key] != item for key, item in expected_identity.items()):
        raise ContractError("stage_receipt_identity_mismatch")
    if receipt["state"] not in {"terminal", "failed"}:
        raise ContractError("stage_receipt_invalid")
    if receipt["decision"] not in {
        "adopted",
        "skipped",
        "deployed",
        "restamped",
        "failed",
    }:
        raise ContractError("stage_receipt_invalid")
    if receipt["state"] == "failed" and (
        receipt["decision"] != "failed" or receipt["failure_class"] is None
    ):
        raise ContractError("stage_receipt_invalid")
    if receipt["state"] == "terminal" and (
        receipt["decision"] == "failed" or receipt["failure_class"] is not None
    ):
        raise ContractError("stage_receipt_invalid")
    if receipt["state"] == "terminal" and receipt["rollback_invoked"] is not False:
        raise ContractError("stage_receipt_rolled_back")
    if receipt["finalized_after_verify"] is not True:
        raise ContractError("stage_receipt_premature")
    started = _timestamp(receipt["started_at"], "stage_receipt_time_invalid")
    completed = _timestamp(receipt["completed_at"], "stage_receipt_time_invalid")
    if completed < started:
        raise ContractError("stage_receipt_time_invalid")
    duration = receipt["duration_seconds"]
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration < 0:
        raise ContractError("stage_receipt_time_invalid")
    if abs((completed - started).total_seconds() - duration) > 0.001:
        raise ContractError("stage_receipt_time_invalid")
    mutation_started = _boolean(
        receipt["mutation_started"], "stage_receipt_invalid"
    )
    mutation_key = _nullable_pattern(
        receipt["mutation_idempotency_key"], _ID, "stage_receipt_invalid"
    )
    if mutation_started != (mutation_key is not None):
        raise ContractError("stage_receipt_invalid")
    _boolean(receipt["rollback_invoked"], "stage_receipt_invalid")
    failure_class = _nullable_pattern(
        receipt["failure_class"], _ID, "stage_receipt_invalid"
    )
    service = receipt["service"]
    if stage in SERVICES:
        if service != stage:
            raise ContractError("stage_receipt_service_mismatch")
        fresh_service = train["services"][stage]
        candidate = train["supply"]["services"][stage]
        if receipt["candidate_digest"] != candidate:
            raise ContractError("stage_receipt_digest_mismatch")
        if receipt["state"] == "terminal" and (
            receipt["decision"] not in {"skipped", "deployed"}
            or receipt["terminal_digest"] != candidate
            or receipt["terminal_digest"] != fresh_service["live_digest"]
            or receipt["terminal_task_definition"]
            != fresh_service["live_task_definition"]
            or receipt["rollback_task_definition"]
            != fresh_service["rollback_task_definition"]
        ):
            raise ContractError("stage_receipt_terminal_mismatch")
        if receipt["state"] == "terminal" and (
            (receipt["decision"] == "deployed" and not mutation_started)
            or (receipt["decision"] == "skipped" and mutation_started)
        ):
            raise ContractError("stage_receipt_invalid")
    elif service is not None:
        raise ContractError("stage_receipt_service_mismatch")
    elif stage == "build" and (
        receipt["state"] == "terminal"
        and (
            receipt["decision"] != "adopted"
            or mutation_started
            or any(
                receipt[key] is not None
                for key in (
                    "candidate_digest",
                    "terminal_digest",
                    "terminal_task_definition",
                    "rollback_task_definition",
                )
            )
        )
    ):
        raise ContractError("stage_receipt_invalid")
    elif stage == "identity" and receipt["state"] == "terminal":
        identity = train["fresh_state"]["identity"]
        if (
            receipt["decision"] != "restamped"
            or not mutation_started
            or identity is None
            or identity["source_revision"] != train["source"]["commit"]
            or {
                name: identity["services"][name]["image_digest"]
                for name in SERVICES
            }
            != train["supply"]["services"]
        ):
            raise ContractError("stage_receipt_terminal_mismatch")
    return {
        **deepcopy(receipt),
        "candidate_digest": _nullable_pattern(
            receipt["candidate_digest"], _DIGEST, "stage_receipt_invalid"
        ),
        "terminal_digest": _nullable_pattern(
            receipt["terminal_digest"], _DIGEST, "stage_receipt_invalid"
        ),
        "terminal_task_definition": _nullable_pattern(
            receipt["terminal_task_definition"],
            _TASK_DEFINITION,
            "stage_receipt_invalid",
        ),
        "rollback_task_definition": _nullable_pattern(
            receipt["rollback_task_definition"],
            _TASK_DEFINITION,
            "stage_receipt_invalid",
        ),
        "failure_class": failure_class,
    }


TRAIN_KEYS = {
    "schema",
    "convergence_id",
    "parent_run_id",
    "run_attempt",
    "source",
    "supply",
    "fresh_state",
    "services",
    "receipts",
}


def validate_train(value: Any) -> dict[str, Any]:
    train = _exact_object(value, TRAIN_KEYS, "release_train_invalid")
    if train["schema"] != TRAIN_SCHEMA:
        raise ContractError("release_train_invalid")
    normalized: dict[str, Any] = {
        "schema": TRAIN_SCHEMA,
        "convergence_id": _pattern(
            train["convergence_id"], _ID, "release_train_invalid"
        ),
        "parent_run_id": _integer(
            train["parent_run_id"], "release_train_invalid", minimum=1
        ),
        "run_attempt": _integer(
            train["run_attempt"], "release_train_invalid", minimum=1
        ),
        "source": _validate_source(train["source"]),
        "supply": _validate_supply(train["supply"]),
        "fresh_state": _validate_fresh_state(train["fresh_state"]),
    }
    services = _exact_object(train["services"], set(SERVICES), "services_invalid")
    normalized["services"] = {
        service: _validate_service(services[service]) for service in SERVICES
    }
    if not isinstance(train["receipts"], list) or len(train["receipts"]) > len(STAGES):
        raise ContractError("stage_receipts_invalid")
    normalized["receipts"] = []
    seen_stages: set[str] = set()
    seen_mutations: set[str] = set()
    last_ordinal = -1
    failed_ordinal: int | None = None
    for item in train["receipts"]:
        receipt = _validate_receipt(item, train=normalized)
        stage = receipt["stage"]
        if stage in seen_stages:
            raise ContractError("duplicate_stage_receipt")
        seen_stages.add(stage)
        ordinal = STAGE_ORDINAL[stage]
        if ordinal <= last_ordinal or (
            failed_ordinal is not None and ordinal > failed_ordinal
        ):
            raise ContractError("stage_receipt_order_invalid")
        last_ordinal = ordinal
        if receipt["state"] == "failed":
            failed_ordinal = ordinal
        mutation_key = receipt["mutation_idempotency_key"]
        if mutation_key is not None:
            if mutation_key in seen_mutations:
                raise ContractError("duplicate_mutation_idempotency_key")
            seen_mutations.add(mutation_key)
        normalized["receipts"].append(receipt)
    return normalized


def _service_is_exact(
    service: str, evidence: Mapping[str, Any], candidate_digest: str
) -> bool:
    return bool(
        evidence["live_digest"] == candidate_digest
        and evidence["component_source_sha256"]
        == evidence["expected_component_source_sha256"]
        and evidence["runtime_contract_sha256"]
        == evidence["expected_runtime_contract_sha256"]
        and evidence["migration_fingerprint"]
        == evidence["expected_migration_fingerprint"]
        and evidence["route_stable"]
        and evidence["health_stable"]
        and (service == "app" or evidence["migration_fingerprint"] is None)
    )


def _identity_is_exact(train: Mapping[str, Any]) -> bool:
    identity = train["fresh_state"]["identity"]
    return bool(
        identity is not None
        and identity["source_revision"] == train["source"]["commit"]
        and {
            service: identity["services"][service]["image_digest"]
            for service in SERVICES
        }
        == train["supply"]["services"]
    )


def _base_plan(train: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": PLAN_SCHEMA,
        "convergence_id": train["convergence_id"],
        "source_commit": train["source"]["commit"],
        "supply_artifact_id": train["supply"]["artifact_id"],
        "supply_sha256": train["supply"]["artifact_sha256"],
        "status": "ready",
        "code": "resume_ready",
        "preserved_stages": sorted(
            (
                receipt["stage"]
                for receipt in train["receipts"]
                if receipt["state"] == "terminal"
            ),
            key=STAGE_ORDINAL.__getitem__,
        ),
        "dispositions": {},
        "actions": [],
        "next_failed_stage": None,
        "identity_restamp": False,
        "dispatch_authorized": False,
    }


def _stopped(train: Mapping[str, Any], code: str) -> dict[str, Any]:
    plan = _base_plan(train)
    plan.update(status="stopped", code=code)
    return plan


def compile_resume_plan(value: Any) -> dict[str, Any]:
    """Return one closed, deterministic plan without performing side effects."""

    train = validate_train(value)
    fresh = train["fresh_state"]
    if fresh["active_writers"]:
        return _stopped(train, "active_writer_present")
    if fresh["open_markers"]:
        return _stopped(train, "open_marker_present")
    build_receipt = next(
        (
            receipt
            for receipt in train["receipts"]
            if receipt["stage"] == "build" and receipt["state"] == "terminal"
        ),
        None,
    )
    if build_receipt is None:
        return _stopped(train, "terminal_build_receipt_required")
    if any(
        not service["route_stable"] or not service["health_stable"]
        for service in train["services"].values()
    ):
        return _stopped(train, "live_surface_unstable")

    plan = _base_plan(train)
    terminal_services = {
        receipt["stage"]
        for receipt in train["receipts"]
        if receipt["state"] == "terminal" and receipt["stage"] in SERVICES
    }
    for terminal_service in terminal_services:
        terminal_ordinal = STAGE_ORDINAL[terminal_service]
        for earlier_service in SERVICES:
            if STAGE_ORDINAL[earlier_service] >= terminal_ordinal:
                break
            if earlier_service in terminal_services:
                continue
            earlier_evidence = train["services"][earlier_service]
            earlier_candidate = train["supply"]["services"][earlier_service]
            if not _service_is_exact(
                earlier_service, earlier_evidence, earlier_candidate
            ):
                return _stopped(train, "stage_receipt_order_invalid")

    for service in SERVICES:
        evidence = train["services"][service]
        candidate_digest = train["supply"]["services"][service]
        exact = _service_is_exact(service, evidence, candidate_digest)
        if service in terminal_services and not exact:
            return _stopped(train, "terminal_stage_drift")
        plan["dispositions"][service] = "skipped" if exact else "deploy"
        if exact:
            continue
        if service == "app" and fresh["retained_snapshot_count"] > SNAPSHOT_LIMIT:
            expected = SNAPSHOT_ACK_PREFIX + str(fresh["retained_snapshot_count"])
            if fresh["snapshot_overflow_acknowledgement"] != expected:
                return _stopped(train, "fresh_snapshot_authority_required")
        expected_task_definition = evidence["live_task_definition"]
        fence_ownership = "unchanged"
        if service == "app" and fresh["drawing_fence"] == "open":
            expected_task_definition = "auto-live"
            fence_ownership = "transactional-auto-live"
        plan["actions"].append(
            {
                "ordinal": STAGE_ORDINAL[service],
                "stage": service,
                "service": service,
                "candidate_digest": candidate_digest,
                "expected_task_definition": expected_task_definition,
                "rollback_task_definition": evidence["rollback_task_definition"],
                "snapshot_overflow_acknowledgement": (
                    fresh["snapshot_overflow_acknowledgement"]
                    if service == "app"
                    else None
                ),
                "drawing_fence_ownership": fence_ownership,
            }
        )

    if not _identity_is_exact(train):
        plan["actions"].append(
            {
                "ordinal": STAGE_ORDINAL["identity"],
                "stage": "identity",
                "service": "app",
                "candidate_digest": train["supply"]["services"]["app"],
                "expected_task_definition": "configuration-only",
                "rollback_task_definition": train["services"]["app"][
                    "rollback_task_definition"
                ],
                "snapshot_overflow_acknowledgement": None,
                "drawing_fence_ownership": "unchanged",
            }
        )
        plan["identity_restamp"] = True

    if plan["actions"]:
        plan["next_failed_stage"] = plan["actions"][0]["stage"]
    else:
        plan.update(status="complete", code="already_converged")
    return plan


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="-", help="Evidence JSON path, or - for stdin")
    parser.add_argument("--output", help="Optional output path for the safe plan JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = _arguments(argv)
    try:
        if arguments.input == "-":
            train = load_json(sys.stdin)
        else:
            with Path(arguments.input).open(encoding="utf-8") as handle:
                train = load_json(handle)
        plan = compile_resume_plan(train)
    except (ContractError, OSError) as exc:
        print(
            canonical_json(
                {
                    "schema": PLAN_SCHEMA,
                    "status": "invalid",
                    "code": str(exc),
                    "dispatch_authorized": False,
                }
            )
        )
        return 2
    rendered = canonical_json(plan) + "\n"
    if arguments.output:
        Path(arguments.output).write_text(rendered, encoding="utf-8", newline="\n")
    sys.stdout.write(rendered)
    return 78 if plan["status"] == "stopped" else 0


if __name__ == "__main__":
    raise SystemExit(main())
