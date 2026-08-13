#!/usr/bin/env python3
"""Reduce captured release events into a dormant watched-train plan.

The reducer is intentionally pure. It has no provider client, credential path,
workflow dispatch, selector, or live mutation authority.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Mapping, TextIO


INPUT_SCHEMA = "leaf.platform-release-watch.v1"
EVENT_SCHEMA = "leaf.platform-release-watch-event.v1"
PLAN_SCHEMA = "leaf.platform-release-watch-plan.v1"
STAGES = (
    "source",
    "ci",
    "build",
    "merge",
    "preflight",
    "web",
    "app",
    "broker",
    "harness",
    "canonical-worker",
    "identity",
    "verify",
    "settlement",
    "receipt",
)
STAGE_ORDINAL = {stage: index for index, stage in enumerate(STAGES)}
LIVE_STAGES = {"web", "app", "broker", "harness", "canonical-worker", "identity"}

_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,199}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_LEASE = re.compile(r"^[a-z0-9][a-z0-9._:-]{7,199}$")
_ROLLBACK = re.compile(
    r"^arn:aws:ecs:us-east-1:[0-9]{12}:task-definition/"
    r"leaf-platform-[a-z-]+:[1-9][0-9]*$"
)


class ContractError(ValueError):
    """Captured evidence cannot safely produce a watched release plan."""


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
        raise ContractError("release_watch_json_invalid") from exc
    if not isinstance(value, dict):
        raise ContractError("release_watch_root_invalid")
    return value


def _exact(value: Any, keys: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ContractError(code)
    return value


def _integer(value: Any, code: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractError(code)
    if maximum is not None and value > maximum:
        raise ContractError(code)
    return value


def _number(value: Any, code: str, *, minimum: float = 0.0) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < minimum
    ):
        raise ContractError(code)
    return float(value)


def _pattern(value: Any, pattern: re.Pattern[str], code: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ContractError(code)
    return value


def _timestamp(value: Any, code: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})",
        value,
    ):
        raise ContractError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(code) from exc
    if parsed.tzinfo is None:
        raise ContractError(code)
    return parsed


def _nullable_pattern(value: Any, pattern: re.Pattern[str], code: str) -> str | None:
    return None if value is None else _pattern(value, pattern, code)


def _string_list(value: Any, code: str) -> list[str]:
    if not isinstance(value, list):
        raise ContractError(code)
    result = [_pattern(item, _ID, code) for item in value]
    if result != sorted(set(result)):
        raise ContractError(code)
    return result


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


IDENTITY_KEYS = {
    "release_id",
    "parent_run_id",
    "run_attempt",
    "watcher_id",
    "source_commit",
    "source_tree",
    "workflow_blob",
    "supply_artifact_id",
    "supply_predicate_sha256",
    "dependency_generation_sha256",
}


def _validate_identity(value: Any) -> dict[str, Any]:
    identity = _exact(value, IDENTITY_KEYS, "release_identity_invalid")
    return {
        "release_id": _pattern(identity["release_id"], _ID, "release_identity_invalid"),
        "parent_run_id": _integer(identity["parent_run_id"], "release_identity_invalid", minimum=1),
        "run_attempt": _integer(identity["run_attempt"], "release_identity_invalid", minimum=1),
        "watcher_id": _pattern(identity["watcher_id"], _ID, "release_identity_invalid"),
        "source_commit": _pattern(identity["source_commit"], _SHA, "release_identity_invalid"),
        "source_tree": _pattern(identity["source_tree"], _SHA, "release_identity_invalid"),
        "workflow_blob": _pattern(identity["workflow_blob"], _SHA, "release_identity_invalid"),
        "supply_artifact_id": _integer(identity["supply_artifact_id"], "release_identity_invalid", minimum=1),
        "supply_predicate_sha256": _pattern(
            identity["supply_predicate_sha256"], _DIGEST, "release_identity_invalid"
        ),
        "dependency_generation_sha256": _pattern(
            identity["dependency_generation_sha256"], _DIGEST, "release_identity_invalid"
        ),
    }


INTENT_KEYS = {
    "stage",
    "ordinal",
    "payload_sha256",
    "mutation_idempotency_key",
    "resource_locks",
    "rollback_target",
    "changes_live_state",
}


def _validate_intents(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != len(STAGES):
        raise ContractError("stage_intents_invalid")
    result: list[dict[str, Any]] = []
    mutation_keys: set[str] = set()
    for ordinal, expected_stage in enumerate(STAGES):
        intent = _exact(value[ordinal], INTENT_KEYS, "stage_intent_invalid")
        if intent["stage"] != expected_stage or intent["ordinal"] != ordinal:
            raise ContractError("stage_intent_order_invalid")
        payload = _pattern(intent["payload_sha256"], _DIGEST, "stage_intent_invalid")
        mutation_key = _nullable_pattern(
            intent["mutation_idempotency_key"], _ID, "stage_intent_invalid"
        )
        locks = _string_list(intent["resource_locks"], "stage_intent_invalid")
        rollback_target = _nullable_pattern(
            intent["rollback_target"], _ROLLBACK, "stage_intent_invalid"
        )
        if not isinstance(intent["changes_live_state"], bool):
            raise ContractError("stage_intent_invalid")
        changes_live_state = intent["changes_live_state"]
        if expected_stage in LIVE_STAGES:
            if not changes_live_state or mutation_key is None or not locks or rollback_target is None:
                raise ContractError("live_stage_intent_invalid")
        elif changes_live_state or mutation_key is not None or locks or rollback_target is not None:
            raise ContractError("nonlive_stage_intent_invalid")
        if mutation_key is not None:
            if mutation_key in mutation_keys:
                raise ContractError("duplicate_mutation_idempotency_key")
            mutation_keys.add(mutation_key)
        result.append(
            {
                "stage": expected_stage,
                "ordinal": ordinal,
                "payload_sha256": payload,
                "mutation_idempotency_key": mutation_key,
                "resource_locks": locks,
                "rollback_target": rollback_target,
                "changes_live_state": changes_live_state,
            }
        )
    return result


EVENT_KEYS = {
    "schema",
    "release_id",
    "watcher_id",
    "watcher_lease_generation_sha256",
    "release_identity_sha256",
    "dependency_generation_sha256",
    "seq",
    "occurred_at",
    "kind",
    "stage",
    "attempt",
    "lease_id",
    "payload_sha256",
    "mutation_idempotency_key",
    "resource_locks",
    "changed_live_state",
    "rollback_target",
    "status",
    "final_verification_passed",
    "writer_count",
    "open_marker_count",
    "semantic_receipt_sha256",
    "event_sha256",
}


def _validate_event(
    value: Any, identity: Mapping[str, Any], watch: Mapping[str, Any]
) -> dict[str, Any]:
    event = _exact(value, EVENT_KEYS, "release_event_invalid")
    if event["schema"] != EVENT_SCHEMA:
        raise ContractError("release_event_invalid")
    supplied_hash = _pattern(event["event_sha256"], _DIGEST, "release_event_integrity_invalid")
    unsigned = {key: item for key, item in event.items() if key != "event_sha256"}
    actual_hash = "sha256:" + hashlib.sha256(_canonical(unsigned).encode("ascii")).hexdigest()
    if supplied_hash != actual_hash:
        raise ContractError("release_event_integrity_invalid")
    expected_identity_sha256 = "sha256:" + hashlib.sha256(
        _canonical(identity).encode("ascii")
    ).hexdigest()
    if (
        event["release_id"] != identity["release_id"]
        or event["watcher_id"] != identity["watcher_id"]
        or event["watcher_lease_generation_sha256"]
        != watch["watcher_lease_generation_sha256"]
        or event["release_identity_sha256"] != expected_identity_sha256
        or event["dependency_generation_sha256"] != identity["dependency_generation_sha256"]
    ):
        raise ContractError("release_event_identity_mismatch")
    stage = event["stage"]
    if stage not in STAGE_ORDINAL:
        raise ContractError("release_event_invalid")
    if event["kind"] not in {"stage_started", "stage_terminal", "rollback_started", "rollback_terminal"}:
        raise ContractError("release_event_invalid")
    if event["status"] not in {"active", "success", "failed"}:
        raise ContractError("release_event_invalid")
    if not isinstance(event["changed_live_state"], bool) or not isinstance(
        event["final_verification_passed"], bool
    ):
        raise ContractError("release_event_invalid")
    return {
        **event,
        "seq": _integer(event["seq"], "release_event_invalid", minimum=1),
        "occurred_at_parsed": _timestamp(event["occurred_at"], "release_event_time_invalid"),
        "attempt": _integer(event["attempt"], "release_event_invalid", minimum=1),
        "lease_id": _nullable_pattern(event["lease_id"], _LEASE, "release_event_invalid"),
        "payload_sha256": _pattern(event["payload_sha256"], _DIGEST, "release_event_invalid"),
        "mutation_idempotency_key": _nullable_pattern(
            event["mutation_idempotency_key"], _ID, "release_event_invalid"
        ),
        "resource_locks": _string_list(event["resource_locks"], "release_event_invalid"),
        "rollback_target": _nullable_pattern(event["rollback_target"], _ROLLBACK, "release_event_invalid"),
        "writer_count": _integer(event["writer_count"], "release_event_invalid", maximum=2),
        "open_marker_count": _integer(event["open_marker_count"], "release_event_invalid"),
        "semantic_receipt_sha256": _nullable_pattern(
            event["semantic_receipt_sha256"], _DIGEST, "release_event_invalid"
        ),
    }


WATCH_KEYS = {
    "mode",
    "observed_at",
    "event_timeout_seconds",
    "max_poll_seconds",
    "active_watcher_count",
    "watcher_lease_generation_sha256",
}


def _validate_watch(value: Any) -> dict[str, Any]:
    watch = _exact(value, WATCH_KEYS, "watch_policy_invalid")
    if watch["mode"] not in {"event", "poll"}:
        raise ContractError("watch_policy_invalid")
    if watch["active_watcher_count"] != 1:
        raise ContractError("watcher_ownership_invalid")
    return {
        "mode": watch["mode"],
        "observed_at": watch["observed_at"],
        "observed_at_parsed": _timestamp(watch["observed_at"], "watch_policy_invalid"),
        "event_timeout_seconds": _number(
            watch["event_timeout_seconds"], "watch_policy_invalid", minimum=1
        ),
        "max_poll_seconds": _number(
            watch["max_poll_seconds"], "watch_policy_invalid", minimum=1
        ),
        "active_watcher_count": 1,
        "watcher_lease_generation_sha256": _pattern(
            watch["watcher_lease_generation_sha256"],
            _DIGEST,
            "watcher_ownership_invalid",
        ),
    }


@dataclass
class Active:
    kind: str
    stage: str
    attempt: int
    lease_id: str | None


@dataclass
class Failed:
    stage: str
    attempt: int
    changed_live_state: bool
    rollback_complete: bool = False


def _assert_event_intent(event: Mapping[str, Any], intent: Mapping[str, Any]) -> None:
    if (
        event["payload_sha256"] != intent["payload_sha256"]
        or event["mutation_idempotency_key"] != intent["mutation_idempotency_key"]
        or event["resource_locks"] != intent["resource_locks"]
        or event["rollback_target"] != intent["rollback_target"]
    ):
        raise ContractError("release_event_intent_mismatch")


def _expected_stage(successful: set[str]) -> str:
    for stage in STAGES:
        if stage not in successful:
            return stage
    return STAGES[-1]


def validate_capture(value: Any) -> dict[str, Any]:
    capture = _exact(value, {"schema", "identity", "watch", "intents", "events"}, "release_watch_invalid")
    if capture["schema"] != INPUT_SCHEMA:
        raise ContractError("release_watch_invalid")
    identity = _validate_identity(capture["identity"])
    watch = _validate_watch(capture["watch"])
    intents = _validate_intents(capture["intents"])
    if not isinstance(capture["events"], list) or len(capture["events"]) > 500:
        raise ContractError("release_events_invalid")
    events = [_validate_event(item, identity, watch) for item in capture["events"]]
    previous_seq = 0
    previous_time: datetime | None = None
    for event in events:
        if event["seq"] <= previous_seq or (
            previous_time is not None and event["occurred_at_parsed"] <= previous_time
        ):
            raise ContractError("release_event_order_invalid")
        if event["occurred_at_parsed"] > watch["observed_at_parsed"]:
            raise ContractError("release_event_time_invalid")
        previous_seq = event["seq"]
        previous_time = event["occurred_at_parsed"]
    return {"identity": identity, "watch": watch, "intents": intents, "events": events}


def _plan_base(capture: Mapping[str, Any]) -> dict[str, Any]:
    identity = capture["identity"]
    return {
        "schema": PLAN_SCHEMA,
        "release_id": capture["identity"]["release_id"],
        "watcher_id": capture["identity"]["watcher_id"],
        "release_identity_sha256": "sha256:"
        + hashlib.sha256(_canonical(identity).encode("ascii")).hexdigest(),
        "dependency_generation_sha256": identity["dependency_generation_sha256"],
        "watcher_lease_generation_sha256": capture["watch"][
            "watcher_lease_generation_sha256"
        ],
        "status": "ready",
        "code": "stage_ready",
        "preserved_stages": [],
        "active_stage": None,
        "next_action": "start_stage",
        "next_stage": None,
        "next_attempt": None,
        "payload_sha256": None,
        "mutation_idempotency_key": None,
        "rollback_target": None,
        "resource_locks": [],
        "writer_lease": None,
        "wake_mode": "event",
        "poll_after_seconds": None,
        "dispatch_authorized": False,
        "selector_activation_authorized": False,
        "live_mutation_authorized": False,
    }


def _set_next(plan: dict[str, Any], intent: Mapping[str, Any], attempt: int) -> None:
    plan.update(
        next_stage=intent["stage"],
        next_attempt=attempt,
        payload_sha256=intent["payload_sha256"],
        mutation_idempotency_key=intent["mutation_idempotency_key"],
        rollback_target=intent["rollback_target"],
        resource_locks=intent["resource_locks"],
    )


def compile_watch_plan(value: Any) -> dict[str, Any]:
    """Return one deterministic non-executable action from captured evidence."""

    capture = validate_capture(value)
    intents = capture["intents"]
    successful: set[str] = set()
    attempts: dict[str, int] = {}
    active: Active | None = None
    failed: Failed | None = None
    writer_lease: str | None = None
    receipt_seen = False

    for event in capture["events"]:
        if receipt_seen:
            raise ContractError("event_after_terminal_receipt")
        if event["writer_count"] > 1:
            raise ContractError("duplicate_writer_present")
        if event["open_marker_count"] != 0:
            if event["stage"] == "settlement" and event["kind"] == "stage_terminal":
                raise ContractError("settlement_not_clean")
            raise ContractError("open_marker_present")
        stage = event["stage"]
        intent = intents[STAGE_ORDINAL[stage]]
        _assert_event_intent(event, intent)
        expected = _expected_stage(successful)
        kind = event["kind"]

        if kind == "stage_started":
            if event["status"] != "active" or event["final_verification_passed"]:
                raise ContractError("stage_start_invalid")
            if active is not None or stage != expected or stage in successful:
                raise ContractError("stage_start_order_invalid")
            expected_attempt = attempts.get(stage, 0) + 1
            if failed is not None:
                if failed.stage != stage or (failed.changed_live_state and not failed.rollback_complete):
                    raise ContractError("stage_retry_before_rollback")
                expected_attempt = failed.attempt + 1
            if event["attempt"] != expected_attempt:
                raise ContractError("stage_attempt_invalid")
            if intent["changes_live_state"]:
                if event["writer_count"] != 1 or event["lease_id"] is None:
                    raise ContractError("writer_lease_invalid")
                if writer_lease is not None and writer_lease != event["lease_id"]:
                    raise ContractError("writer_lease_changed")
                writer_lease = event["lease_id"]
            elif event["writer_count"] or event["lease_id"] is not None:
                raise ContractError("nonlive_writer_invalid")
            if event["changed_live_state"] or event["semantic_receipt_sha256"] is not None:
                raise ContractError("stage_start_invalid")
            active = Active("stage", stage, event["attempt"], event["lease_id"])
            attempts[stage] = event["attempt"]
            failed = None
            continue

        if kind == "stage_terminal":
            if active is None or active.kind != "stage" or active.stage != stage:
                raise ContractError("stage_terminal_without_start")
            if event["attempt"] != active.attempt or event["lease_id"] != active.lease_id:
                raise ContractError("stage_terminal_identity_mismatch")
            if event["writer_count"] != 0 or not event["final_verification_passed"]:
                raise ContractError("stage_terminal_premature")
            if event["status"] not in {"success", "failed"}:
                raise ContractError("stage_terminal_invalid")
            if not intent["changes_live_state"] and event["changed_live_state"]:
                raise ContractError("nonlive_mutation_invalid")
            if stage == "settlement" and event["status"] != "success":
                raise ContractError("settlement_not_clean")
            if stage == "receipt":
                if event["status"] != "success" or event["semantic_receipt_sha256"] is None:
                    raise ContractError("semantic_receipt_invalid")
            elif event["semantic_receipt_sha256"] is not None:
                raise ContractError("semantic_receipt_out_of_stage")
            active = None
            writer_lease = None
            if event["status"] == "success":
                successful.add(stage)
                failed = None
                if stage == "receipt":
                    receipt_seen = True
            else:
                failed = Failed(stage, event["attempt"], event["changed_live_state"])
            continue

        if kind == "rollback_started":
            if (
                event["status"] != "active"
                or event["final_verification_passed"]
                or active is not None
                or failed is None
                or failed.stage != stage
                or not failed.changed_live_state
                or failed.rollback_complete
                or event["attempt"] != failed.attempt
                or event["writer_count"] != 1
                or event["lease_id"] is None
                or event["changed_live_state"] is not True
                or event["semantic_receipt_sha256"] is not None
            ):
                raise ContractError("rollback_start_invalid")
            if writer_lease is not None and writer_lease != event["lease_id"]:
                raise ContractError("writer_lease_changed")
            writer_lease = event["lease_id"]
            active = Active("rollback", stage, event["attempt"], event["lease_id"])
            continue

        if (
            event["status"] not in {"success", "failed"}
            or active is None
            or active.kind != "rollback"
            or active.stage != stage
            or event["attempt"] != active.attempt
            or event["lease_id"] != active.lease_id
            or event["writer_count"] != 0
            or not event["final_verification_passed"]
            or event["changed_live_state"] is not True
            or event["semantic_receipt_sha256"] is not None
            or failed is None
        ):
            raise ContractError("rollback_terminal_invalid")
        active = None
        writer_lease = None
        if event["status"] == "failed":
            raise ContractError("rollback_failed")
        failed.rollback_complete = True

    plan = _plan_base(capture)
    plan["preserved_stages"] = [stage for stage in STAGES if stage in successful]
    if receipt_seen:
        if successful != set(STAGES) or active is not None or failed is not None:
            raise ContractError("terminal_receipt_state_invalid")
        plan.update(status="complete", code="release_complete", next_action="complete")
        return plan

    if active is not None:
        plan.update(
            status="waiting",
            code="event_pending",
            active_stage=active.stage,
            next_action="wait_event",
            writer_lease=active.lease_id,
            wake_mode="event",
        )
        _set_next(plan, intents[STAGE_ORDINAL[active.stage]], active.attempt)
        last_time = capture["events"][-1]["occurred_at_parsed"]
        elapsed = (capture["watch"]["observed_at_parsed"] - last_time).total_seconds()
        if elapsed >= capture["watch"]["event_timeout_seconds"]:
            poll_seconds = min(60.0, capture["watch"]["max_poll_seconds"])
            plan.update(
                code="bounded_poll_due",
                next_action="poll",
                wake_mode="poll",
                poll_after_seconds=poll_seconds,
            )
        return plan

    if failed is not None and failed.changed_live_state and not failed.rollback_complete:
        intent = intents[STAGE_ORDINAL[failed.stage]]
        plan.update(code="rollback_required", next_action="rollback_stage")
        _set_next(plan, intent, failed.attempt)
        return plan

    next_stage = _expected_stage(successful)
    intent = intents[STAGE_ORDINAL[next_stage]]
    attempt = failed.attempt + 1 if failed is not None else attempts.get(next_stage, 0) + 1
    plan["code"] = "retry_failed_stage" if failed is not None else "stage_ready"
    _set_next(plan, intent, attempt)
    return plan


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="-", help="Capture JSON path, or - for stdin")
    parser.add_argument("--output", help="Optional path for the safe plan JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = _arguments(argv)
    try:
        if arguments.input == "-":
            captured = load_json(sys.stdin)
        else:
            with Path(arguments.input).open(encoding="utf-8") as handle:
                captured = load_json(handle)
        plan = compile_watch_plan(captured)
    except (ContractError, OSError) as exc:
        plan = {
            "schema": PLAN_SCHEMA,
            "status": "invalid",
            "code": str(exc),
            "dispatch_authorized": False,
            "selector_activation_authorized": False,
            "live_mutation_authorized": False,
        }
        rendered = _canonical(plan) + "\n"
        if arguments.output:
            Path(arguments.output).write_text(rendered, encoding="utf-8", newline="\n")
        sys.stdout.write(rendered)
        return 2
    rendered = _canonical(plan) + "\n"
    if arguments.output:
        Path(arguments.output).write_text(rendered, encoding="utf-8", newline="\n")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
