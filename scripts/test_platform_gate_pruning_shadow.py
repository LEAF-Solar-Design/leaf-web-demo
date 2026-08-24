from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


MODULE_PATH = Path(__file__).with_name("platform_gate_pruning_shadow.py")
ROOT = MODULE_PATH.parents[1]
SCHEMA_PATH = ROOT / "contract" / "platform-gate-pruning-shadow.v1.schema.json"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "qualify-platform-gate-pruning-shadow.yml"
SPEC = importlib.util.spec_from_file_location("platform_gate_pruning_shadow", MODULE_PATH)
assert SPEC and SPEC.loader
pruning = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pruning
SPEC.loader.exec_module(pruning)

CAPTURED_AT = datetime(2026, 8, 13, 9, 0, tzinfo=timezone.utc)
EMPTY_HASH = hashlib.sha256(b"").hexdigest()


def digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


def bare_hash(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


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


def legacy(
    source: dict[str, object],
    *,
    result: str = "EMPTY",
    count: int = 0,
    open_hash: str = EMPTY_HASH,
    duration: float = 400.0,
    checkpoint: str | None = None,
) -> dict[str, object]:
    return {
        "schema": "leaf.legacy-marker-census.v1",
        "workflow_blob": source["deploy_workflow_blob"],
        "checkpoint_sha256": checkpoint or digest("checkpoint"),
        "result": result,
        "open_count": count,
        "open_set_sha256": open_hash,
        "duration_seconds": duration,
    }


def indexed(
    *,
    result: str = "EMPTY",
    count: int = 0,
    open_hash: str = EMPTY_HASH,
    duration: float = 0.25,
    checkpoint: str | None = None,
    bounded_delta_seconds: float = 60.0,
    lock_acquired_at: str | None = None,
) -> dict[str, object]:
    return {
        "checkpoint_sha256": checkpoint or digest("checkpoint"),
        "duration_seconds": duration,
        "receipt": {
            "schema": "leaf.staging-marker-ledger-census.v1",
            "result": result,
            "strong_consistent": True,
            "writer_lock_held": True,
            "ledger_union_delta_exact": True,
            "pre_post_snapshot_stable": True,
            "fallback_to_full_scan_on_error": True,
            "bounded_delta_seconds": bounded_delta_seconds,
            "lock_acquired_at": lock_acquired_at
            or iso(CAPTURED_AT - timedelta(minutes=10)),
            "open_count": count,
            "open_set_sha256": open_hash,
        },
    }


def source_hash(source: dict[str, object]) -> str:
    encoded = json.dumps(
        source, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def checkpoint_content(
    source: dict[str, object],
    *,
    completed_at: datetime,
    result: str = "EMPTY",
    count: int = 0,
    open_hash: str = EMPTY_HASH,
) -> dict[str, object]:
    return {
        "schema": "leaf.staging-marker-checkpoint-anchor.v1",
        "source_commit": source["source_commit"],
        "source_tree": source["source_tree"],
        "deploy_workflow_blob": source["deploy_workflow_blob"],
        "restore_workflow_blob": source["restore_workflow_blob"],
        "ledger_script_blob": source["ledger_script_blob"],
        "legacy_census_script_blob": source["legacy_census_script_blob"],
        "migration_horizon_sha256": source["migration_horizon_sha256"],
        "scan_started_at": iso(completed_at - timedelta(minutes=6)),
        "scan_completed_at": iso(completed_at),
        "result": result,
        "open_count": count,
        "open_set_sha256": open_hash,
    }


def content_hash(content: dict[str, object]) -> str:
    encoded = json.dumps(
        content, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def shadow_context(
    document: dict[str, object], index: int
) -> dict[str, object]:
    source = document["source"]
    assert isinstance(source, dict)
    return {
        "transaction_id": f"transaction.shadow.{index:03d}",
        "source_evidence_sha256": source_hash(source),
        "source_commit": source["source_commit"],
        "deploy_workflow_blob": source["deploy_workflow_blob"],
        "migration_horizon_sha256": source["migration_horizon_sha256"],
        "ledger_generation_sha256": source["ledger_generation_sha256"],
        "writer_lock_generation_sha256": source["writer_lock_generation_sha256"],
        "active_writers": 0,
        "terminal": True,
        "terminal_receipt_sha256": digest(f"receipt:{index}"),
        "completed_at": iso(CAPTURED_AT - timedelta(minutes=30 - index)),
    }


def comparison(
    document: dict[str, object], index: int, scenario: str
) -> dict[str, object]:
    source = document["source"]
    assert isinstance(source, dict)
    completed_at = CAPTURED_AT - timedelta(minutes=30 - index)
    checkpoint_completed_at = completed_at - timedelta(minutes=5)
    lock_acquired_at = checkpoint_completed_at + timedelta(seconds=60)
    checkpoint_value = checkpoint_content(
        source, completed_at=checkpoint_completed_at
    )
    checkpoint = content_hash(checkpoint_value)
    return {
        **shadow_context(document, index),
        "scenario": scenario,
        "checkpoint_content": checkpoint_value,
        "legacy": legacy(
            source, duration=390.0 + index, checkpoint=checkpoint
        ),
        "indexed": indexed(
            duration=0.2 + index / 100,
            checkpoint=checkpoint,
            bounded_delta_seconds=60.0,
            lock_acquired_at=iso(lock_acquired_at),
        ),
    }


def control(
    document: dict[str, object], scenario: str
) -> dict[str, object]:
    source = document["source"]
    assert isinstance(source, dict)
    open_hash = bare_hash(f"{scenario}:open")
    index = 7 if scenario == "planted_open_row" else 8
    completed_at = CAPTURED_AT - timedelta(minutes=30 - index)
    checkpoint_completed_at = completed_at - timedelta(minutes=5)
    lock_acquired_at = checkpoint_completed_at + timedelta(seconds=60)
    checkpoint_value = checkpoint_content(
        source,
        completed_at=checkpoint_completed_at,
        result="OPEN",
        count=1,
        open_hash=open_hash,
    )
    checkpoint = content_hash(checkpoint_value)
    if scenario == "planted_open_row":
        legacy_value = legacy(
            source,
            result="OPEN",
            count=1,
            open_hash=open_hash,
            checkpoint=checkpoint,
        )
        indexed_value = indexed(
            result="OPEN",
            count=1,
            open_hash=open_hash,
            checkpoint=checkpoint,
            bounded_delta_seconds=60.0,
            lock_acquired_at=iso(lock_acquired_at),
        )
        alarm = False
    else:
        legacy_value = legacy(
            source,
            result="OPEN",
            count=1,
            open_hash=open_hash,
            checkpoint=checkpoint,
        )
        indexed_value = indexed(
            checkpoint=checkpoint,
            bounded_delta_seconds=60.0,
            lock_acquired_at=iso(lock_acquired_at),
        )
        alarm = True
    return {
        **shadow_context(document, index),
        "scenario": scenario,
        "checkpoint_content": checkpoint_value,
        "replacement_blocks": True,
        "integrity_alarm": alarm,
        "legacy": legacy_value,
        "indexed": indexed_value,
    }


def checkpoint_control(
    document: dict[str, object], checkpoint_state: str = "stale"
) -> dict[str, object]:
    source = document["source"]
    assert isinstance(source, dict)
    fallback_completed_at = CAPTURED_AT - timedelta(minutes=5)
    fallback_content = checkpoint_content(source, completed_at=fallback_completed_at)
    return {
        **shadow_context(document, 9),
        "scenario": "stale_or_missing_checkpoint",
        "completed_at": iso(fallback_completed_at),
        "checkpoint_state": checkpoint_state,
        "observed_checkpoint_sha256": (
            None if checkpoint_state == "missing" else digest("stale-checkpoint")
        ),
        "replacement_blocks": True,
        "fallback_to_full_scan": True,
        "fallback_checkpoint_content": fallback_content,
        "legacy": legacy(source, checkpoint=content_hash(fallback_content)),
    }


def evidence() -> dict[str, object]:
    source: dict[str, object] = {
        "source_commit": "a" * 40,
        "source_tree": "b" * 40,
        "deploy_workflow_blob": "c" * 40,
        "restore_workflow_blob": "d" * 40,
        "ledger_script_blob": "e" * 40,
        "legacy_census_script_blob": "f" * 40,
        "migration_horizon_sha256": digest("horizon"),
        "ledger_generation_sha256": digest("ledger-generation"),
        "writer_lock_generation_sha256": digest("writer-lock"),
        "capture_id": "capture.marker.shadow.001",
        "captured_at": iso(CAPTURED_AT),
    }
    audit_completed_at = CAPTURED_AT - timedelta(hours=1)
    audit_content = checkpoint_content(source, completed_at=audit_completed_at)
    document: dict[str, object] = {
        "schema": pruning.INPUT_SCHEMA,
        "gate": {
            "gate_id": pruning.GATE_ID,
            "safety_job": pruning.SAFETY_JOB,
            "state_machine_stages": ["preflight", "settlement"],
            "current_control": "full-62-day-history-census",
            "proposed_replacement": "indexed-marker-ledger",
        },
        "source": source,
        "posture": {
            "marker_ledger_mode": "disabled",
            "digest_aware_reconcile": False,
            "active_writers": 0,
        },
        "scheduled_audit": {
            "enabled": True,
            "anchor_type": "successful_full_artifact_scan",
            "checkpoint_sha256": content_hash(audit_content),
            "checkpoint_content": audit_content,
            "workflow_blob": source["restore_workflow_blob"],
            "last_completed_at": iso(audit_completed_at),
            "maximum_age_seconds": 86400,
            "terminal_receipt_sha256": digest("audit-receipt"),
        },
        "comparisons": [],
        "negative_controls": {},
    }
    scenarios = [
        "normal_success",
        "forced_cancellation",
        "missing_artifact",
        "failed_settlement",
        "expired_lease",
        "normal_success",
    ]
    document["comparisons"] = [
        comparison(document, index, scenario)
        for index, scenario in enumerate(scenarios, start=1)
    ]
    document["negative_controls"] = {
        "planted_open_row": control(document, "planted_open_row"),
        "artifact_without_ledger": control(document, "artifact_without_ledger"),
        "stale_or_missing_checkpoint": checkpoint_control(document),
    }
    return document


def test_exact_equivalence_is_only_review_eligible_with_no_authority() -> None:
    plan = pruning.evaluate_gate(evidence())
    assert plan["disposition"] == "eligible_for_staging_cutover_review"
    assert plan["code"] == "shadow_equivalence_ready"
    assert plan["comparison_count"] == 6
    assert plan["shadow_case_count"] == 9
    assert plan["scenario_coverage"] == sorted(pruning.REQUIRED_SCENARIOS)
    assert plan["disagreement_count"] == 0
    assert plan["planted_open_row_passed"] is True
    assert plan["artifact_without_ledger_passed"] is True
    assert plan["stale_or_missing_checkpoint_passed"] is True
    assert plan["measured_legacy_total_seconds"] > 0
    assert plan["measured_indexed_total_seconds"] > 0
    assert plan["measured_savings_seconds"] is None
    assert plan["inferred_savings_seconds"] is None
    for key in (
        "gate_change_authorized",
        "selector_activation_authorized",
        "dispatch_authorized",
        "live_mutation_authorized",
    ):
        assert plan[key] is False


def test_five_comparisons_retain_gate() -> None:
    document = evidence()
    comparisons = document["comparisons"]
    assert isinstance(comparisons, list)
    comparisons.pop()
    plan = pruning.evaluate_gate(document)
    assert plan["code"] == "shadow_sample_too_small"
    assert plan["disposition"] == "retain_blocking_gate"


def test_seven_comparisons_retain_gate() -> None:
    document = evidence()
    comparisons = document["comparisons"]
    assert isinstance(comparisons, list)
    comparisons.append(comparison(document, 7, "normal_success"))
    assert pruning.evaluate_gate(document)["code"] == "shadow_sample_too_small"


def test_exactly_two_clean_passes_are_required() -> None:
    document = evidence()
    comparisons = document["comparisons"]
    assert isinstance(comparisons, list)
    comparisons[-1]["scenario"] = "expired_lease"
    assert pruning.evaluate_gate(document)["code"] == "clean_shadow_count_invalid"


@pytest.mark.parametrize("scenario", sorted(pruning.REQUIRED_SCENARIOS))
def test_each_missing_scenario_retains_gate(scenario: str) -> None:
    document = evidence()
    comparisons = document["comparisons"]
    assert isinstance(comparisons, list)
    for item in comparisons:
        if item["scenario"] == scenario:
            item["scenario"] = "normal_success" if scenario != "normal_success" else "expired_lease"
    plan = pruning.evaluate_gate(document)
    assert plan["code"] == "required_scenario_missing"


@pytest.mark.parametrize("field", ["open_count", "open_set_sha256"])
def test_marker_disagreement_retains_gate(field: str) -> None:
    document = evidence()
    comparisons = document["comparisons"]
    assert isinstance(comparisons, list)
    indexed_receipt = comparisons[0]["indexed"]["receipt"]
    indexed_receipt[field] = 1 if field == "open_count" else bare_hash("different")
    if field == "open_count":
        indexed_receipt["result"] = "OPEN"
    plan = pruning.evaluate_gate(document)
    assert plan["code"] == "marker_shadow_mismatch"
    assert plan["disagreement_count"] == 1


@pytest.mark.parametrize("field", ["transaction_id", "terminal_receipt_sha256"])
def test_duplicate_transaction_or_receipt_retains_gate(field: str) -> None:
    document = evidence()
    comparisons = document["comparisons"]
    assert isinstance(comparisons, list)
    comparisons[1][field] = comparisons[0][field]
    assert pruning.evaluate_gate(document)["code"] == "duplicate_shadow_evidence"


@pytest.mark.parametrize("field", ["transaction_id", "terminal_receipt_sha256"])
def test_control_identity_must_be_unique(field: str) -> None:
    document = evidence()
    comparisons = document["comparisons"]
    controls = document["negative_controls"]
    assert isinstance(comparisons, list)
    assert isinstance(controls, dict)
    controls["planted_open_row"][field] = comparisons[0][field]
    assert pruning.evaluate_gate(document)["code"] == "duplicate_shadow_evidence"


@pytest.mark.parametrize(
    ("field", "value"),
    [("active_writers", 1), ("terminal", False)],
)
def test_nonterminal_or_writer_comparison_retains_gate(field: str, value: object) -> None:
    document = evidence()
    comparisons = document["comparisons"]
    assert isinstance(comparisons, list)
    comparisons[0][field] = value
    assert pruning.evaluate_gate(document)["code"] == "shadow_not_terminal"


def test_nonterminal_control_retains_gate() -> None:
    document = evidence()
    controls = document["negative_controls"]
    assert isinstance(controls, dict)
    controls["artifact_without_ledger"]["terminal"] = False
    assert pruning.evaluate_gate(document)["code"] == "shadow_not_terminal"


@pytest.mark.parametrize(
    "field",
    [
        "source_commit",
        "deploy_workflow_blob",
        "migration_horizon_sha256",
        "ledger_generation_sha256",
        "writer_lock_generation_sha256",
    ],
)
def test_comparison_tuple_drift_retains_gate(field: str) -> None:
    document = evidence()
    comparisons = document["comparisons"]
    assert isinstance(comparisons, list)
    comparisons[0][field] = "0" * 40 if field.endswith("blob") or field == "source_commit" else digest("drift")
    assert pruning.evaluate_gate(document)["code"] == "shadow_source_drift"


def test_full_source_tuple_drift_after_capture_retains_gate() -> None:
    document = evidence()
    source = document["source"]
    assert isinstance(source, dict)
    source["source_tree"] = "9" * 40
    assert pruning.evaluate_gate(document)["code"] == "checkpoint_anchor_source_drift"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("enabled", False),
        ("workflow_blob", "0" * 40),
    ],
)
def test_disabled_foreign_or_stale_scheduled_audit_retains_gate(
    field: str, value: object
) -> None:
    document = evidence()
    audit = document["scheduled_audit"]
    assert isinstance(audit, dict)
    audit[field] = value
    assert pruning.evaluate_gate(document)["code"] == "scheduled_audit_unavailable"


def test_audit_timestamp_must_match_content_addressed_scan() -> None:
    document = evidence()
    audit = document["scheduled_audit"]
    assert isinstance(audit, dict)
    audit["last_completed_at"] = "2026-08-01T00:00:00Z"
    with pytest.raises(pruning.ContractError, match="scheduled_audit_invalid"):
        pruning.evaluate_gate(document)


def test_unbounded_scheduled_audit_age_refuses_schema_and_runtime() -> None:
    jsonschema = jsonschema_module()
    document = evidence()
    audit = document["scheduled_audit"]
    assert isinstance(audit, dict)
    audit["maximum_age_seconds"] = 604801
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(document)
    with pytest.raises(pruning.ContractError, match="scheduled_audit_invalid"):
        pruning.evaluate_gate(document)


@pytest.mark.parametrize(
    ("control_name", "field", "value", "code"),
    [
        ("planted_open_row", "replacement_blocks", False, "planted_open_row_control_failed"),
        ("planted_open_row", "integrity_alarm", True, "planted_open_row_control_failed"),
        ("artifact_without_ledger", "replacement_blocks", False, "artifact_without_ledger_control_failed"),
        ("artifact_without_ledger", "integrity_alarm", False, "artifact_without_ledger_control_failed"),
    ],
)
def test_negative_control_must_block_and_alarm_as_expected(
    control_name: str, field: str, value: object, code: str
) -> None:
    document = evidence()
    controls = document["negative_controls"]
    assert isinstance(controls, dict)
    controls[control_name][field] = value
    assert pruning.evaluate_gate(document)["code"] == code


def test_planted_open_row_missed_by_index_retains_gate() -> None:
    document = evidence()
    controls = document["negative_controls"]
    assert isinstance(controls, dict)
    indexed_receipt = controls["planted_open_row"]["indexed"]["receipt"]
    indexed_receipt.update(result="EMPTY", open_count=0, open_set_sha256=EMPTY_HASH)
    assert pruning.evaluate_gate(document)["code"] == "planted_open_row_control_failed"


def test_artifact_without_ledger_false_agreement_retains_gate() -> None:
    document = evidence()
    controls = document["negative_controls"]
    assert isinstance(controls, dict)
    legacy_value = controls["artifact_without_ledger"]["legacy"]
    indexed_receipt = controls["artifact_without_ledger"]["indexed"]["receipt"]
    indexed_receipt.update(
        result=legacy_value["result"],
        open_count=legacy_value["open_count"],
        open_set_sha256=legacy_value["open_set_sha256"],
    )
    assert pruning.evaluate_gate(document)["code"] == "artifact_without_ledger_control_failed"


@pytest.mark.parametrize("checkpoint_state", ["stale", "missing"])
def test_stale_or_missing_checkpoint_falls_back_to_full_scan(
    checkpoint_state: str,
) -> None:
    document = evidence()
    controls = document["negative_controls"]
    assert isinstance(controls, dict)
    controls["stale_or_missing_checkpoint"] = checkpoint_control(
        document, checkpoint_state
    )
    plan = pruning.evaluate_gate(document)
    assert plan["stale_or_missing_checkpoint_passed"] is True
    assert plan["disposition"] == "eligible_for_staging_cutover_review"


@pytest.mark.parametrize(
    ("field", "value"),
    [("replacement_blocks", False), ("fallback_to_full_scan", False)],
)
def test_checkpoint_fallback_control_fails_closed(
    field: str, value: object
) -> None:
    document = evidence()
    controls = document["negative_controls"]
    assert isinstance(controls, dict)
    controls["stale_or_missing_checkpoint"][field] = value
    with pytest.raises(pruning.ContractError, match="checkpoint_control_invalid"):
        pruning.evaluate_gate(document)


def test_current_checkpoint_does_not_prove_stale_fallback() -> None:
    document = evidence()
    controls = document["negative_controls"]
    audit = document["scheduled_audit"]
    assert isinstance(controls, dict)
    assert isinstance(audit, dict)
    controls["stale_or_missing_checkpoint"]["legacy"]["checkpoint_sha256"] = audit[
        "checkpoint_sha256"
    ]
    assert pruning.evaluate_gate(document)["code"] == "checkpoint_fallback_control_failed"


def test_indexed_median_must_be_under_ten_seconds() -> None:
    document = evidence()
    comparisons = document["comparisons"]
    assert isinstance(comparisons, list)
    for item in comparisons:
        item["indexed"]["duration_seconds"] = 10.0
    assert pruning.evaluate_gate(document)["code"] == "indexed_path_too_slow"


@pytest.mark.parametrize(
    "field",
    [
        "writer_lock_held",
        "ledger_union_delta_exact",
        "pre_post_snapshot_stable",
        "fallback_to_full_scan_on_error",
    ],
)
def test_indexed_control_predicates_fail_closed(field: str) -> None:
    document = evidence()
    comparisons = document["comparisons"]
    assert isinstance(comparisons, list)
    comparisons[0]["indexed"]["receipt"][field] = False
    with pytest.raises(pruning.ContractError, match="indexed_census_invalid"):
        pruning.evaluate_gate(document)


def test_indexed_delta_must_be_bounded() -> None:
    document = evidence()
    comparisons = document["comparisons"]
    assert isinstance(comparisons, list)
    comparisons[0]["indexed"]["receipt"]["bounded_delta_seconds"] = 86401
    with pytest.raises(pruning.ContractError, match="indexed_census_invalid"):
        pruning.evaluate_gate(document)


def test_reported_delta_must_equal_checkpoint_to_lock_interval() -> None:
    document = evidence()
    comparisons = document["comparisons"]
    assert isinstance(comparisons, list)
    comparisons[0]["indexed"]["receipt"]["bounded_delta_seconds"] = 1.0
    assert pruning.evaluate_gate(document)["code"] == "shadow_checkpoint_anchor_invalid"


def test_old_checkpoint_cannot_claim_a_short_delta() -> None:
    document = evidence()
    comparisons = document["comparisons"]
    assert isinstance(comparisons, list)
    item = comparisons[0]
    content = item["checkpoint_content"]
    content["scan_started_at"] = "2026-07-29T00:00:00Z"
    content["scan_completed_at"] = "2026-07-29T00:06:00Z"
    checkpoint = content_hash(content)
    item["legacy"]["checkpoint_sha256"] = checkpoint
    item["indexed"]["checkpoint_sha256"] = checkpoint
    assert pruning.evaluate_gate(document)["code"] == "shadow_checkpoint_anchor_invalid"


def test_checkpoint_anchor_must_be_a_successful_full_scan() -> None:
    document = evidence()
    audit = document["scheduled_audit"]
    assert isinstance(audit, dict)
    audit["anchor_type"] = "mutable_pointer"
    with pytest.raises(pruning.ContractError, match="scheduled_audit_invalid"):
        pruning.evaluate_gate(document)


def test_checkpoint_content_tamper_breaks_its_hash() -> None:
    document = evidence()
    audit = document["scheduled_audit"]
    assert isinstance(audit, dict)
    audit["checkpoint_content"]["open_set_sha256"] = bare_hash("tampered")
    with pytest.raises(pruning.ContractError, match="scheduled_audit_invalid"):
        pruning.evaluate_gate(document)


def test_shadow_checkpoint_content_must_match_full_scan_receipt() -> None:
    document = evidence()
    comparisons = document["comparisons"]
    assert isinstance(comparisons, list)
    comparisons[0]["checkpoint_content"]["open_set_sha256"] = bare_hash("tampered")
    assert pruning.evaluate_gate(document)["code"] == "shadow_checkpoint_anchor_invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    [("marker_ledger_mode", "shadow"), ("digest_aware_reconcile", True)],
)
def test_enabled_selector_posture_retains_gate(field: str, value: object) -> None:
    document = evidence()
    posture = document["posture"]
    assert isinstance(posture, dict)
    posture[field] = value
    assert pruning.evaluate_gate(document)["code"] == "selector_posture_not_dormant"


def test_active_writer_retains_gate() -> None:
    document = evidence()
    posture = document["posture"]
    assert isinstance(posture, dict)
    posture["active_writers"] = 1
    assert pruning.evaluate_gate(document)["code"] == "active_writer_present"


def test_duplicate_json_key_extra_key_and_nonfinite_duration_refuse() -> None:
    with pytest.raises(pruning.ContractError, match="duplicate_json_key"):
        pruning.load_json(__import__("io").StringIO('{"schema":"x","schema":"y"}'))
    document = evidence()
    document["extra"] = "no"
    with pytest.raises(pruning.ContractError, match="gate_pruning_evidence_invalid"):
        pruning.evaluate_gate(document)
    document = evidence()
    comparisons = document["comparisons"]
    assert isinstance(comparisons, list)
    comparisons[0]["legacy"]["duration_seconds"] = float("nan")
    with pytest.raises(pruning.ContractError, match="legacy_census_invalid"):
        pruning.evaluate_gate(document)


def test_schema_meta_validates_real_marker_shapes() -> None:
    jsonschema = jsonschema_module()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(evidence())


def test_schema_and_runtime_accept_enabled_posture_as_safe_retain() -> None:
    jsonschema = jsonschema_module()
    document = evidence()
    posture = document["posture"]
    assert isinstance(posture, dict)
    posture["marker_ledger_mode"] = "enabled"
    posture["digest_aware_reconcile"] = True
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(document)
    assert pruning.evaluate_gate(document)["code"] == "selector_posture_not_dormant"


def test_schema_and_runtime_reject_empty_result_with_open_count() -> None:
    jsonschema = jsonschema_module()
    document = evidence()
    comparisons = document["comparisons"]
    assert isinstance(comparisons, list)
    comparisons[0]["legacy"]["open_count"] = 1
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(document)
    with pytest.raises(pruning.ContractError, match="legacy_census_invalid"):
        pruning.evaluate_gate(document)


def test_workflow_is_manual_read_only_and_non_executable() -> None:
    import yaml

    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    parsed = yaml.load(source, Loader=yaml.BaseLoader)
    assert set(parsed["on"]) == {"workflow_dispatch"}
    assert parsed["permissions"] == {"contents": "read"}
    assert source.count("python3 scripts/platform_gate_pruning_shadow.py") == 1
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
    source = MODULE_PATH.read_text(encoding="utf-8").lower()
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
        assert token not in source
        assert token not in workflow
