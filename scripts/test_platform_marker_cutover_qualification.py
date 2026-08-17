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


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "platform_marker_cutover_qualification.py"
SCHEMA_PATH = ROOT / "contract" / "platform-marker-cutover-qualification.v1.schema.json"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "qualify-platform-marker-cutover.yml"
CAPTURED_AT = datetime(2026, 8, 13, 18, 0, tzinfo=timezone.utc)
EMPTY_HASH = hashlib.sha256(b"").hexdigest()

spec = importlib.util.spec_from_file_location("marker_cutover", MODULE_PATH)
assert spec is not None and spec.loader is not None
marker_cutover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(marker_cutover)


def digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


def bare_hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def canonical_hash(value: dict[str, object]) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def source() -> dict[str, object]:
    return {
        "source_commit": "a" * 40,
        "source_tree": "b" * 40,
        "deploy_workflow_blob": "c" * 40,
        "restore_workflow_blob": "d" * 40,
        "ledger_script_blob": "e" * 40,
        "legacy_census_script_blob": "f" * 40,
        "migration_horizon_sha256": digest("horizon"),
        "ledger_generation_sha256": digest("ledger-generation"),
        "writer_lock_generation_sha256": digest("writer-lock"),
        "captured_at": iso(CAPTURED_AT),
    }


def checkpoint(
    source_value: dict[str, object],
    completed_at: datetime,
    *,
    result: str = "EMPTY",
    count: int = 0,
    open_hash: str = EMPTY_HASH,
) -> dict[str, object]:
    content = {
        "schema": "leaf.staging-marker-checkpoint-anchor.v1",
        "source_commit": source_value["source_commit"],
        "source_tree": source_value["source_tree"],
        "deploy_workflow_blob": source_value["deploy_workflow_blob"],
        "restore_workflow_blob": source_value["restore_workflow_blob"],
        "ledger_script_blob": source_value["ledger_script_blob"],
        "legacy_census_script_blob": source_value["legacy_census_script_blob"],
        "migration_horizon_sha256": source_value["migration_horizon_sha256"],
        "scan_started_at": iso(completed_at - timedelta(minutes=6)),
        "scan_completed_at": iso(completed_at),
        "result": result,
        "open_count": count,
        "open_set_sha256": open_hash,
    }
    return {"sha256": canonical_hash(content), "content": content}


def full_scan(
    source_value: dict[str, object],
    completed_at: datetime,
    *,
    result: str = "EMPTY",
    count: int = 0,
    open_hash: str = EMPTY_HASH,
) -> dict[str, object]:
    return {
        "schema": "leaf.legacy-marker-census.v1",
        "checkpoint": checkpoint(
            source_value,
            completed_at,
            result=result,
            count=count,
            open_hash=open_hash,
        ),
        "workflow_blob": source_value["deploy_workflow_blob"],
        "result": result,
        "open_count": count,
        "open_set_sha256": open_hash,
        "duration_seconds": 390.0,
    }


def indexed(
    scan: dict[str, object],
    lock_at: datetime,
    *,
    result: str | None = None,
    count: int | None = None,
    open_hash: str | None = None,
    duration: float = 0.4,
) -> dict[str, object]:
    checkpoint_value = scan["checkpoint"]
    assert isinstance(checkpoint_value, dict)
    content = checkpoint_value["content"]
    assert isinstance(content, dict)
    checkpoint_completed = datetime.fromisoformat(
        str(content["scan_completed_at"]).replace("Z", "+00:00")
    )
    return {
        "schema": "leaf.staging-marker-ledger-census.v1",
        "checkpoint_sha256": checkpoint_value["sha256"],
        "result": result if result is not None else scan["result"],
        "open_count": count if count is not None else scan["open_count"],
        "open_set_sha256": (
            open_hash if open_hash is not None else scan["open_set_sha256"]
        ),
        "duration_seconds": duration,
        "lock_acquired_at": iso(lock_at),
        "bounded_delta_seconds": (lock_at - checkpoint_completed).total_seconds(),
        "strong_consistent": True,
        "writer_lock_held": True,
        "ledger_union_delta_exact": True,
        "pre_post_snapshot_stable": True,
        "fallback_to_full_scan_on_error": True,
    }


def source_hash(source_value: dict[str, object]) -> str:
    return canonical_hash(source_value)


def shadow(
    source_value: dict[str, object], index: int, scenario: str
) -> dict[str, object]:
    terminal_at = CAPTURED_AT - timedelta(minutes=30 - index)
    scan_at = terminal_at - timedelta(minutes=5)
    lock_at = scan_at + timedelta(seconds=60)
    open_hash = bare_hash(f"{scenario}:open")
    is_open = scenario not in {"normal_success", "stale_or_missing_checkpoint"}
    scan = full_scan(
        source_value,
        scan_at,
        result="OPEN" if is_open else "EMPTY",
        count=1 if is_open else 0,
        open_hash=open_hash if is_open else EMPTY_HASH,
    )
    item: dict[str, object] = {
        "transaction_id": f"transaction.marker.{index:03d}",
        "terminal_receipt_sha256": digest(f"receipt:{index}"),
        "scenario": scenario,
        "source_evidence_sha256": source_hash(source_value),
        "terminal": True,
        "active_writers": 0,
        "completed_at": iso(terminal_at),
        "checkpoint_state": "current",
        "observed_checkpoint_sha256": scan["checkpoint"]["sha256"],
        "full_scan": scan,
        "indexed": indexed(scan, lock_at),
        "replacement_blocks": is_open,
        "integrity_alarm": False,
        "fallback_to_full_scan": False,
    }
    if scenario == "artifact_without_ledger":
        item["indexed"] = indexed(
            scan, lock_at, result="EMPTY", count=0, open_hash=EMPTY_HASH
        )
        item["integrity_alarm"] = True
    if scenario == "stale_or_missing_checkpoint":
        item.update(
            checkpoint_state="stale",
            observed_checkpoint_sha256=digest("stale-observed"),
            indexed=None,
            replacement_blocks=True,
            fallback_to_full_scan=True,
        )
    return item


def evidence() -> dict[str, object]:
    source_value = source()
    scenarios = [
        "normal_success",
        "forced_cancellation",
        "missing_artifact",
        "failed_settlement",
        "expired_lease",
        "planted_open_row",
        "artifact_without_ledger",
        "stale_or_missing_checkpoint",
        "normal_success",
    ]
    anchor_at = CAPTURED_AT - timedelta(hours=1)
    anchor_scan = full_scan(source_value, anchor_at)
    anchor_scan["workflow_blob"] = source_value["restore_workflow_blob"]
    return {
        "schema": marker_cutover.INPUT_SCHEMA,
        "source": source_value,
        "posture": {
            "marker_ledger_mode": "disabled",
            "digest_aware_reconcile": False,
            "active_writers": 0,
        },
        "scheduled_anchor": {
            "enabled": True,
            "terminal": True,
            "maximum_age_seconds": 86400,
            "terminal_receipt_sha256": digest("scheduled-anchor"),
            "full_scan": anchor_scan,
        },
        "shadows": [
            shadow(source_value, index, scenario)
            for index, scenario in enumerate(scenarios, start=1)
        ],
    }


def test_exact_nine_receipts_are_review_eligible_without_authority() -> None:
    plan = marker_cutover.evaluate(evidence())
    assert plan["disposition"] == "eligible_for_staging_cutover_review"
    assert plan["code"] == "shadow_evidence_ready"
    assert plan["shadow_count"] == 9
    assert plan["disagreement_count"] == 0
    assert plan["indexed_median_seconds"] < 10
    for key in (
        "gate_change_authorized",
        "selector_activation_authorized",
        "dispatch_authorized",
        "live_mutation_authorized",
    ):
        assert plan[key] is False


@pytest.mark.parametrize("count", [8, 10])
def test_shadow_count_is_exact(count: int) -> None:
    document = evidence()
    shadows = document["shadows"]
    assert isinstance(shadows, list)
    if count == 8:
        shadows.pop()
    else:
        shadows.append(deepcopy(shadows[-1]))
    with pytest.raises(marker_cutover.ContractError, match="shadows_invalid"):
        marker_cutover.evaluate(document)


def test_two_clean_passes_are_required() -> None:
    document = evidence()
    shadows = document["shadows"]
    assert isinstance(shadows, list)
    shadows[-1]["scenario"] = "expired_lease"
    assert marker_cutover.evaluate(document)["code"] == "scenario_cardinality_invalid"


@pytest.mark.parametrize("field", ["transaction_id", "terminal_receipt_sha256"])
def test_shadow_identity_is_unique(field: str) -> None:
    document = evidence()
    shadows = document["shadows"]
    assert isinstance(shadows, list)
    shadows[1][field] = shadows[0][field]
    assert marker_cutover.evaluate(document)["code"] == "duplicate_shadow_identity"


@pytest.mark.parametrize(("field", "value"), [("terminal", False), ("active_writers", 1)])
def test_nonterminal_or_writer_shadow_retains_gate(field: str, value: object) -> None:
    document = evidence()
    shadows = document["shadows"]
    assert isinstance(shadows, list)
    shadows[0][field] = value
    assert marker_cutover.evaluate(document)["code"] == "shadow_identity_or_checkpoint_invalid"


def test_source_hash_drift_retains_gate() -> None:
    document = evidence()
    shadows = document["shadows"]
    assert isinstance(shadows, list)
    shadows[0]["source_evidence_sha256"] = digest("foreign-source")
    assert marker_cutover.evaluate(document)["code"] == "shadow_identity_or_checkpoint_invalid"


def test_checkpoint_content_tamper_refuses_contract() -> None:
    document = evidence()
    shadows = document["shadows"]
    assert isinstance(shadows, list)
    shadows[0]["full_scan"]["checkpoint"]["content"]["open_set_sha256"] = bare_hash(
        "tampered"
    )
    with pytest.raises(marker_cutover.ContractError, match="checkpoint_invalid"):
        marker_cutover.evaluate(document)


def test_fallback_full_scan_must_match_checkpoint_content() -> None:
    document = evidence()
    shadows = document["shadows"]
    assert isinstance(shadows, list)
    fallback = shadows[7]["full_scan"]
    fallback["open_set_sha256"] = bare_hash("contradiction")
    assert marker_cutover.evaluate(document)["code"] == "shadow_identity_or_checkpoint_invalid"


def test_fallback_full_scan_must_finish_before_terminal_receipt() -> None:
    document = evidence()
    shadows = document["shadows"]
    assert isinstance(shadows, list)
    item = shadows[7]
    content = item["full_scan"]["checkpoint"]["content"]
    content["scan_started_at"] = "2026-08-13T18:01:00Z"
    content["scan_completed_at"] = "2026-08-13T18:07:00Z"
    item["full_scan"]["checkpoint"]["sha256"] = canonical_hash(content)
    assert marker_cutover.evaluate(document)["code"] == "shadow_identity_or_checkpoint_invalid"


@pytest.mark.parametrize("checkpoint_state", ["stale", "missing"])
def test_stale_or_missing_checkpoint_uses_full_scan_fallback(
    checkpoint_state: str,
) -> None:
    document = evidence()
    shadows = document["shadows"]
    assert isinstance(shadows, list)
    item = shadows[7]
    item["checkpoint_state"] = checkpoint_state
    item["observed_checkpoint_sha256"] = (
        None if checkpoint_state == "missing" else digest("stale-observed")
    )
    assert marker_cutover.evaluate(document)["code"] == "shadow_evidence_ready"


def test_false_delta_duration_retains_gate() -> None:
    document = evidence()
    shadows = document["shadows"]
    assert isinstance(shadows, list)
    shadows[0]["indexed"]["bounded_delta_seconds"] = 1.0
    assert marker_cutover.evaluate(document)["code"] == "indexed_delta_invalid"


def test_lock_acquisition_must_strictly_precede_terminal_receipt() -> None:
    document = evidence()
    shadows = document["shadows"]
    assert isinstance(shadows, list)
    item = shadows[0]
    item["indexed"]["lock_acquired_at"] = item["completed_at"]
    checkpoint_completed = datetime.fromisoformat(
        item["full_scan"]["checkpoint"]["content"]["scan_completed_at"].replace(
            "Z", "+00:00"
        )
    )
    terminal = datetime.fromisoformat(item["completed_at"].replace("Z", "+00:00"))
    item["indexed"]["bounded_delta_seconds"] = (
        terminal - checkpoint_completed
    ).total_seconds()
    assert marker_cutover.evaluate(document)["code"] == "indexed_delta_invalid"


def test_checkpoint_older_than_24_hours_retains_gate() -> None:
    document = evidence()
    shadows = document["shadows"]
    assert isinstance(shadows, list)
    item = shadows[0]
    content = item["full_scan"]["checkpoint"]["content"]
    content["scan_started_at"] = "2026-08-10T00:00:00Z"
    content["scan_completed_at"] = "2026-08-10T00:06:00Z"
    item["full_scan"]["checkpoint"]["sha256"] = canonical_hash(content)
    item["indexed"]["checkpoint_sha256"] = canonical_hash(content)
    item["observed_checkpoint_sha256"] = canonical_hash(content)
    assert marker_cutover.evaluate(document)["code"] == "indexed_delta_invalid"


@pytest.mark.parametrize(
    "field",
    [
        "strong_consistent",
        "writer_lock_held",
        "ledger_union_delta_exact",
        "pre_post_snapshot_stable",
        "fallback_to_full_scan_on_error",
    ],
)
def test_indexed_control_predicates_fail_closed(field: str) -> None:
    document = evidence()
    shadows = document["shadows"]
    assert isinstance(shadows, list)
    shadows[0]["indexed"][field] = False
    with pytest.raises(marker_cutover.ContractError, match="indexed_invalid"):
        marker_cutover.evaluate(document)


def test_artifact_without_ledger_false_agreement_retains_gate() -> None:
    document = evidence()
    shadows = document["shadows"]
    assert isinstance(shadows, list)
    item = shadows[6]
    item["indexed"] = indexed(
        item["full_scan"],
        datetime.fromisoformat(item["indexed"]["lock_acquired_at"].replace("Z", "+00:00")),
    )
    assert marker_cutover.evaluate(document)["code"] == "artifact_without_ledger_control_failed"


def test_planted_open_row_missed_by_index_retains_gate() -> None:
    document = evidence()
    shadows = document["shadows"]
    assert isinstance(shadows, list)
    item = shadows[5]
    item["indexed"].update(result="EMPTY", open_count=0, open_set_sha256=EMPTY_HASH)
    assert marker_cutover.evaluate(document)["code"] == "marker_shadow_mismatch"


def test_indexed_median_must_be_under_ten_seconds() -> None:
    document = evidence()
    shadows = document["shadows"]
    assert isinstance(shadows, list)
    for item in shadows:
        if item["indexed"] is not None:
            item["indexed"]["duration_seconds"] = 10.0
    assert marker_cutover.evaluate(document)["code"] == "indexed_path_too_slow"


@pytest.mark.parametrize(
    ("field", "value"),
    [("marker_ledger_mode", "shadow"), ("digest_aware_reconcile", True)],
)
def test_selector_activation_is_never_qualified(field: str, value: object) -> None:
    document = evidence()
    posture = document["posture"]
    assert isinstance(posture, dict)
    posture[field] = value
    assert marker_cutover.evaluate(document)["code"] == "selector_posture_not_dormant"


def test_scheduled_anchor_must_be_fresh_and_content_addressed() -> None:
    document = evidence()
    anchor = document["scheduled_anchor"]
    assert isinstance(anchor, dict)
    anchor["full_scan"]["checkpoint"]["content"]["source_tree"] = "9" * 40
    with pytest.raises(marker_cutover.ContractError, match="checkpoint_invalid"):
        marker_cutover.evaluate(document)


def test_scheduled_anchor_must_be_terminal() -> None:
    document = evidence()
    anchor = document["scheduled_anchor"]
    assert isinstance(anchor, dict)
    anchor["terminal"] = False
    with pytest.raises(marker_cutover.ContractError, match="scheduled_anchor_invalid"):
        marker_cutover.evaluate(document)


def jsonschema_module():
    sys.modules.pop("platform", None)
    original_path = list(sys.path)
    repository = ROOT.resolve()
    sys.path = [
        entry for entry in sys.path if Path(entry or ".").resolve() != repository
    ]
    try:
        stdlib_platform = importlib.import_module("platform")
        sys.modules["platform"] = stdlib_platform
        return importlib.import_module("jsonschema")
    finally:
        sys.path = original_path


def test_schema_validates_the_real_positive_fixture() -> None:
    jsonschema = jsonschema_module()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(evidence())


def test_workflow_is_manual_read_only_and_non_executable() -> None:
    import yaml

    source_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    parsed = yaml.load(source_text, Loader=yaml.BaseLoader)
    assert set(parsed["on"]) == {"workflow_dispatch"}
    assert parsed["permissions"] == {"contents": "read"}
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
        assert forbidden not in source_text


def test_cli_invalid_input_has_no_authority(tmp_path: Path) -> None:
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
    assert payload["disposition"] == "invalid"
    assert payload["gate_change_authorized"] is False
    assert payload["selector_activation_authorized"] is False
    assert payload["dispatch_authorized"] is False
    assert payload["live_mutation_authorized"] is False


def test_source_and_workflow_have_no_provider_or_execution_path() -> None:
    source_text = MODULE_PATH.read_text(encoding="utf-8").lower()
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8").lower()
    for token in (
        "boto3",
        "botocore",
        "aws ecs",
        "gh workflow run",
        "requests.",
        "urllib.request",
        "marker_ledger_mode=shadow",
    ):
        assert token not in source_text
        assert token not in workflow
