from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


MODULE_PATH = Path(__file__).with_name("platform_release_watch.py")
ROOT = MODULE_PATH.parents[1]
SCHEMA_PATH = ROOT / "contract" / "platform-release-watch.v1.schema.json"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "qualify-platform-release-watch.yml"
SPEC = importlib.util.spec_from_file_location("platform_release_watch", MODULE_PATH)
assert SPEC and SPEC.loader
watch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = watch
SPEC.loader.exec_module(watch)

BASE_TIME = datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc)


def jsonschema_module():
    """Import jsonschema without the repository's platform package shadow."""

    sys.modules.pop("platform", None)
    original_path = list(sys.path)
    repository = ROOT.resolve()
    sys.path = [
        entry
        for entry in sys.path
        if Path(entry or ".").resolve() != repository
    ]
    try:
        stdlib_platform = importlib.import_module("platform")
        sys.modules["platform"] = stdlib_platform
        return importlib.import_module("jsonschema")
    finally:
        sys.path = original_path


def digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


def timestamp(seq: int) -> str:
    return (BASE_TIME + timedelta(seconds=seq)).isoformat().replace("+00:00", "Z")


def intent(stage: str, ordinal: int) -> dict[str, object]:
    live = stage in watch.LIVE_STAGES
    service = "canonical-worker" if stage == "canonical-worker" else stage
    return {
        "stage": stage,
        "ordinal": ordinal,
        "payload_sha256": digest(f"payload:{stage}"),
        "mutation_idempotency_key": f"mutation:{stage}" if live else None,
        "resource_locks": [f"service:{service}"] if live else [],
        "rollback_target": (
            "arn:aws:ecs:us-east-1:807034087062:task-definition/"
            f"leaf-platform-{service}:100"
            if live
            else None
        ),
        "changes_live_state": live,
    }


def capture() -> dict[str, object]:
    return {
        "schema": watch.INPUT_SCHEMA,
        "identity": {
            "release_id": "release.level2.001",
            "parent_run_id": 31600000000,
            "run_attempt": 1,
            "watcher_id": "watcher.level2.001",
            "source_commit": "a" * 40,
            "source_tree": "b" * 40,
            "workflow_blob": "c" * 40,
            "supply_artifact_id": 9140000000,
            "supply_predicate_sha256": digest("predicate"),
            "dependency_generation_sha256": digest("generation"),
        },
        "watch": {
            "mode": "event",
            "observed_at": timestamp(1000),
            "event_timeout_seconds": 120,
            "max_poll_seconds": 30,
            "active_watcher_count": 1,
            "watcher_lease_generation_sha256": digest("watcher-lease"),
        },
        "intents": [intent(stage, ordinal) for ordinal, stage in enumerate(watch.STAGES)],
        "events": [],
    }


def seal_event(data: dict[str, object]) -> dict[str, object]:
    result = deepcopy(data)
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    result["event_sha256"] = "sha256:" + hashlib.sha256(encoded.encode("ascii")).hexdigest()
    return result


def event(
    document: dict[str, object],
    stage: str,
    kind: str,
    *,
    seq: int,
    attempt: int = 1,
    status: str | None = None,
    changed_live_state: bool = False,
    lease_id: str | None = None,
    writer_count: int | None = None,
    open_marker_count: int = 0,
    semantic_receipt_sha256: str | None = None,
) -> dict[str, object]:
    identity = document["identity"]
    watch_policy = document["watch"]
    assert isinstance(identity, dict) and isinstance(watch_policy, dict)
    intents = document["intents"]
    assert isinstance(intents, list)
    stage_intent = intents[watch.STAGE_ORDINAL[stage]]
    assert isinstance(stage_intent, dict)
    live = bool(stage_intent["changes_live_state"])
    started = kind in {"stage_started", "rollback_started"}
    if status is None:
        status = "active" if started else "success"
    if lease_id is None and live:
        lease_id = f"lease.{stage}.{attempt:03d}"
    if writer_count is None:
        writer_count = 1 if started and live else 0
    return seal_event(
        {
            "schema": watch.EVENT_SCHEMA,
            "release_id": identity["release_id"],
            "watcher_id": identity["watcher_id"],
            "watcher_lease_generation_sha256": watch_policy[
                "watcher_lease_generation_sha256"
            ],
            "release_identity_sha256": "sha256:"
            + hashlib.sha256(
                json.dumps(
                    identity,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("ascii")
            ).hexdigest(),
            "dependency_generation_sha256": identity["dependency_generation_sha256"],
            "seq": seq,
            "occurred_at": timestamp(seq),
            "kind": kind,
            "stage": stage,
            "attempt": attempt,
            "lease_id": lease_id,
            "payload_sha256": stage_intent["payload_sha256"],
            "mutation_idempotency_key": stage_intent["mutation_idempotency_key"],
            "resource_locks": stage_intent["resource_locks"],
            "changed_live_state": changed_live_state,
            "rollback_target": stage_intent["rollback_target"],
            "status": status,
            "final_verification_passed": not started,
            "writer_count": writer_count,
            "open_marker_count": open_marker_count,
            "semantic_receipt_sha256": semantic_receipt_sha256,
        }
    )


def append_terminal(
    document: dict[str, object],
    stage: str,
    seq: int,
    *,
    attempt: int = 1,
    changed_live_state: bool | None = None,
) -> int:
    intents = document["intents"]
    events = document["events"]
    assert isinstance(intents, list) and isinstance(events, list)
    stage_intent = intents[watch.STAGE_ORDINAL[stage]]
    assert isinstance(stage_intent, dict)
    live = bool(stage_intent["changes_live_state"])
    if changed_live_state is None:
        changed_live_state = live
    lease = f"lease.{stage}.{attempt:03d}" if live else None
    events.append(event(document, stage, "stage_started", seq=seq, attempt=attempt, lease_id=lease))
    events.append(
        event(
            document,
            stage,
            "stage_terminal",
            seq=seq + 1,
            attempt=attempt,
            lease_id=lease,
            changed_live_state=changed_live_state,
            semantic_receipt_sha256=digest("terminal-receipt") if stage == "receipt" else None,
        )
    )
    return seq + 2


def append_through(document: dict[str, object], last_stage: str) -> int:
    seq = 1
    for stage in watch.STAGES:
        seq = append_terminal(document, stage, seq)
        if stage == last_stage:
            return seq
    raise AssertionError(last_stage)


def reseal(item: dict[str, object]) -> None:
    item.pop("event_sha256", None)
    item.update(seal_event(item))


def test_empty_history_starts_source_attempt_one() -> None:
    plan = watch.compile_watch_plan(capture())
    assert plan["next_action"] == "start_stage"
    assert plan["next_stage"] == "source"
    assert plan["next_attempt"] == 1
    assert plan["preserved_stages"] == []


def test_failure_before_mutation_retries_only_app() -> None:
    document = capture()
    seq = append_through(document, "web")
    events = document["events"]
    assert isinstance(events, list)
    events.extend(
        [
            event(document, "app", "stage_started", seq=seq),
            event(
                document,
                "app",
                "stage_terminal",
                seq=seq + 1,
                status="failed",
                changed_live_state=False,
            ),
        ]
    )
    plan = watch.compile_watch_plan(document)
    assert plan["code"] == "retry_failed_stage"
    assert plan["next_stage"] == "app"
    assert plan["next_attempt"] == 2
    assert plan["preserved_stages"][-1] == "web"


def test_failure_after_mutation_requires_exact_rollback_then_retry() -> None:
    document = capture()
    seq = append_through(document, "web")
    events = document["events"]
    assert isinstance(events, list)
    events.extend(
        [
            event(document, "app", "stage_started", seq=seq),
            event(
                document,
                "app",
                "stage_terminal",
                seq=seq + 1,
                status="failed",
                changed_live_state=True,
            ),
        ]
    )
    plan = watch.compile_watch_plan(document)
    assert plan["next_action"] == "rollback_stage"
    assert plan["next_stage"] == "app"
    assert plan["rollback_target"].endswith("leaf-platform-app:100")

    events.extend(
        [
            event(document, "app", "rollback_started", seq=seq + 2, changed_live_state=True),
            event(document, "app", "rollback_terminal", seq=seq + 3, changed_live_state=True),
        ]
    )
    plan = watch.compile_watch_plan(document)
    assert plan["code"] == "retry_failed_stage"
    assert plan["next_stage"] == "app"
    assert plan["next_attempt"] == 2


def test_process_loss_after_broker_resumes_at_harness() -> None:
    document = capture()
    append_through(document, "broker")
    plan = watch.compile_watch_plan(document)
    assert plan["next_stage"] == "harness"
    assert "broker" in plan["preserved_stages"]


def test_fresh_active_stage_waits_for_event() -> None:
    document = capture()
    events = document["events"]
    watch_policy = document["watch"]
    assert isinstance(events, list) and isinstance(watch_policy, dict)
    events.append(event(document, "source", "stage_started", seq=1))
    watch_policy["observed_at"] = timestamp(30)
    plan = watch.compile_watch_plan(document)
    assert plan["next_action"] == "wait_event"
    assert plan["wake_mode"] == "event"
    assert plan["next_stage"] == "source"
    assert plan["payload_sha256"] == digest("payload:source")


def test_stale_active_stage_requests_bounded_poll() -> None:
    document = capture()
    events = document["events"]
    watch_policy = document["watch"]
    assert isinstance(events, list) and isinstance(watch_policy, dict)
    events.append(event(document, "source", "stage_started", seq=1))
    watch_policy["observed_at"] = timestamp(200)
    watch_policy["max_poll_seconds"] = 90
    plan = watch.compile_watch_plan(document)
    assert plan["next_action"] == "poll"
    assert plan["poll_after_seconds"] == 60
    assert plan["dispatch_authorized"] is False


def test_complete_train_requires_receipt_and_clean_settlement() -> None:
    document = capture()
    append_through(document, "receipt")
    plan = watch.compile_watch_plan(document)
    assert plan["status"] == "complete"
    assert plan["next_action"] == "complete"
    assert plan["preserved_stages"] == list(watch.STAGES)
    assert plan["live_mutation_authorized"] is False


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("release_id", "foreign.release", "release_event_identity_mismatch"),
        ("watcher_id", "foreign.watcher", "release_event_identity_mismatch"),
        ("dependency_generation_sha256", digest("foreign"), "release_event_identity_mismatch"),
    ],
)
def test_foreign_event_identity_refuses(field: str, value: str, code: str) -> None:
    document = capture()
    item = event(document, "source", "stage_started", seq=1)
    item[field] = value
    reseal(item)
    document["events"] = [item]
    with pytest.raises(watch.ContractError, match=code):
        watch.compile_watch_plan(document)


def test_dependency_generation_change_inside_stream_refuses() -> None:
    document = capture()
    first = event(document, "source", "stage_started", seq=1)
    second = event(document, "source", "stage_terminal", seq=2)
    second["dependency_generation_sha256"] = digest("new-generation")
    reseal(second)
    document["events"] = [first, second]
    with pytest.raises(watch.ContractError, match="release_event_identity_mismatch"):
        watch.compile_watch_plan(document)


def test_source_identity_change_after_event_capture_refuses() -> None:
    document = capture()
    document["events"] = [event(document, "source", "stage_started", seq=1)]
    identity = document["identity"]
    assert isinstance(identity, dict)
    identity["source_commit"] = "d" * 40
    with pytest.raises(watch.ContractError, match="release_event_identity_mismatch"):
        watch.compile_watch_plan(document)


def test_duplicate_or_missing_watcher_ownership_refuses() -> None:
    for count in (0, 2):
        document = capture()
        policy = document["watch"]
        assert isinstance(policy, dict)
        policy["active_watcher_count"] = count
        with pytest.raises(watch.ContractError, match="watcher_ownership_invalid"):
            watch.compile_watch_plan(document)


def test_second_active_stage_refuses() -> None:
    document = capture()
    document["events"] = [
        event(document, "source", "stage_started", seq=1),
        event(document, "ci", "stage_started", seq=2),
    ]
    with pytest.raises(watch.ContractError, match="stage_start_order_invalid"):
        watch.compile_watch_plan(document)


def test_writer_count_above_one_refuses() -> None:
    document = capture()
    seq = append_through(document, "preflight")
    events = document["events"]
    assert isinstance(events, list)
    events.append(event(document, "web", "stage_started", seq=seq, writer_count=2))
    with pytest.raises(watch.ContractError, match="duplicate_writer_present"):
        watch.compile_watch_plan(document)


def test_payload_and_mutation_key_drift_refuse() -> None:
    for field, replacement in (
        ("payload_sha256", digest("drift")),
        ("mutation_idempotency_key", "mutation:other"),
    ):
        document = capture()
        seq = append_through(document, "preflight")
        item = event(document, "web", "stage_started", seq=seq)
        item[field] = replacement
        reseal(item)
        events = document["events"]
        assert isinstance(events, list)
        events.append(item)
        with pytest.raises(watch.ContractError, match="release_event_intent_mismatch"):
            watch.compile_watch_plan(document)


def test_duplicate_mutation_key_in_intents_refuses() -> None:
    document = capture()
    intents = document["intents"]
    assert isinstance(intents, list)
    intents[watch.STAGE_ORDINAL["app"]]["mutation_idempotency_key"] = "mutation:web"
    with pytest.raises(watch.ContractError, match="duplicate_mutation_idempotency_key"):
        watch.compile_watch_plan(document)


def test_retry_before_required_rollback_refuses() -> None:
    document = capture()
    seq = append_through(document, "web")
    events = document["events"]
    assert isinstance(events, list)
    events.extend(
        [
            event(document, "app", "stage_started", seq=seq),
            event(
                document,
                "app",
                "stage_terminal",
                seq=seq + 1,
                status="failed",
                changed_live_state=True,
            ),
            event(document, "app", "stage_started", seq=seq + 2, attempt=2),
        ]
    )
    with pytest.raises(watch.ContractError, match="stage_retry_before_rollback"):
        watch.compile_watch_plan(document)


def test_wrong_rollback_target_refuses() -> None:
    document = capture()
    seq = append_through(document, "web")
    events = document["events"]
    assert isinstance(events, list)
    events.extend(
        [
            event(document, "app", "stage_started", seq=seq),
            event(
                document,
                "app",
                "stage_terminal",
                seq=seq + 1,
                status="failed",
                changed_live_state=True,
            ),
        ]
    )
    item = event(document, "app", "rollback_started", seq=seq + 2, changed_live_state=True)
    item["rollback_target"] = (
        "arn:aws:ecs:us-east-1:807034087062:task-definition/leaf-platform-app:99"
    )
    reseal(item)
    events.append(item)
    with pytest.raises(watch.ContractError, match="release_event_intent_mismatch"):
        watch.compile_watch_plan(document)


def test_failed_rollback_refuses() -> None:
    document = capture()
    seq = append_through(document, "web")
    events = document["events"]
    assert isinstance(events, list)
    events.extend(
        [
            event(document, "app", "stage_started", seq=seq),
            event(
                document,
                "app",
                "stage_terminal",
                seq=seq + 1,
                status="failed",
                changed_live_state=True,
            ),
            event(document, "app", "rollback_started", seq=seq + 2, changed_live_state=True),
            event(
                document,
                "app",
                "rollback_terminal",
                seq=seq + 3,
                status="failed",
                changed_live_state=True,
            ),
        ]
    )
    with pytest.raises(watch.ContractError, match="rollback_failed"):
        watch.compile_watch_plan(document)


def test_settlement_with_open_marker_refuses() -> None:
    document = capture()
    seq = append_through(document, "verify")
    events = document["events"]
    assert isinstance(events, list)
    events.extend(
        [
            event(document, "settlement", "stage_started", seq=seq),
            event(
                document,
                "settlement",
                "stage_terminal",
                seq=seq + 1,
                open_marker_count=1,
            ),
        ]
    )
    with pytest.raises(watch.ContractError, match="settlement_not_clean"):
        watch.compile_watch_plan(document)


def test_open_marker_blocks_stage_start() -> None:
    document = capture()
    document["events"] = [
        event(document, "source", "stage_started", seq=1, open_marker_count=1)
    ]
    with pytest.raises(watch.ContractError, match="open_marker_present"):
        watch.compile_watch_plan(document)


def test_receipt_before_settlement_refuses() -> None:
    document = capture()
    seq = append_through(document, "verify")
    events = document["events"]
    assert isinstance(events, list)
    events.append(event(document, "receipt", "stage_started", seq=seq))
    with pytest.raises(watch.ContractError, match="stage_start_order_invalid"):
        watch.compile_watch_plan(document)


def test_event_after_receipt_refuses() -> None:
    document = capture()
    seq = append_through(document, "receipt")
    events = document["events"]
    assert isinstance(events, list)
    events.append(event(document, "receipt", "stage_started", seq=seq, attempt=2))
    with pytest.raises(watch.ContractError, match="event_after_terminal_receipt"):
        watch.compile_watch_plan(document)


def test_non_increasing_sequence_and_timestamp_refuse() -> None:
    for field in ("seq", "occurred_at"):
        document = capture()
        first = event(document, "source", "stage_started", seq=1)
        second = event(document, "source", "stage_terminal", seq=2)
        second[field] = first[field]
        reseal(second)
        document["events"] = [first, second]
        with pytest.raises(watch.ContractError, match="release_event_order_invalid"):
            watch.compile_watch_plan(document)


def test_event_integrity_and_extra_keys_refuse() -> None:
    document = capture()
    item = event(document, "source", "stage_started", seq=1)
    item["status"] = "success"
    document["events"] = [item]
    with pytest.raises(watch.ContractError, match="release_event_integrity_invalid"):
        watch.compile_watch_plan(document)
    item = event(capture(), "source", "stage_started", seq=1)
    item["extra"] = "no"
    document["events"] = [item]
    with pytest.raises(watch.ContractError, match="release_event_invalid"):
        watch.compile_watch_plan(document)


def test_duplicate_json_key_and_nonfinite_poll_refuse() -> None:
    with pytest.raises(watch.ContractError, match="duplicate_json_key"):
        watch.load_json(__import__("io").StringIO('{"schema":"x","schema":"y"}'))
    document = capture()
    policy = document["watch"]
    assert isinstance(policy, dict)
    policy["max_poll_seconds"] = float("nan")
    with pytest.raises(watch.ContractError, match="watch_policy_invalid"):
        watch.compile_watch_plan(document)


def test_cli_failure_is_closed_and_writes_no_authority(tmp_path: Path) -> None:
    output = tmp_path / "plan.json"
    result = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--input", "-", "--output", str(output)],
        input="{}",
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "invalid"
    assert payload["dispatch_authorized"] is False
    assert payload["selector_activation_authorized"] is False
    assert payload["live_mutation_authorized"] is False


def test_source_contains_no_provider_or_dispatch_path() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8").lower()
    for token in ("boto3", "botocore", "aws ecs", "gh workflow run", "requests.", "urllib.request"):
        assert token not in source


def test_schema_meta_validates_actual_runtime_fixture() -> None:
    jsonschema = jsonschema_module()

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    document = capture()
    events = document["events"]
    assert isinstance(events, list)
    events.append(event(document, "source", "stage_started", seq=1))
    jsonschema.Draft202012Validator(schema).validate(document)
    assert watch.compile_watch_plan(document)["status"] == "waiting"


def test_schema_and_runtime_both_reject_extra_root_key() -> None:
    jsonschema = jsonschema_module()

    document = capture()
    document["extra"] = "refuse"
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(document)
    with pytest.raises(watch.ContractError, match="release_watch_invalid"):
        watch.compile_watch_plan(document)


@pytest.mark.parametrize("field", ["event_timeout_seconds", "max_poll_seconds"])
def test_schema_and_runtime_both_reject_subsecond_timeout(field: str) -> None:
    jsonschema = jsonschema_module()
    document = capture()
    policy = document["watch"]
    assert isinstance(policy, dict)
    policy[field] = 0.5
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(document)
    with pytest.raises(watch.ContractError, match="watch_policy_invalid"):
        watch.compile_watch_plan(document)


def test_workflow_is_manual_read_only_and_non_executable() -> None:
    import yaml

    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    parsed = yaml.load(source, Loader=yaml.BaseLoader)
    assert set(parsed["on"]) == {"workflow_dispatch"}
    assert parsed["permissions"] == {"contents": "read"}
    assert source.count("python3 scripts/platform_release_watch.py") == 1
    assert "persist-credentials: false" in source
    for forbidden in (
        "aws-actions/",
        "aws ecs",
        "gh workflow run",
        "workflow_run:",
        "push:",
        "pull_request:",
        "environment:",
        "id-token:",
        "secrets.",
    ):
        assert forbidden not in source
