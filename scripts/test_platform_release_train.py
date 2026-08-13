from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from platform_release_train import ContractError, SERVICES, compile_resume_plan


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "qualify-platform-release-resume.yml"
TRAIN_SCHEMA = ROOT / "contract" / "platform-release-train.v1.schema.json"
RECEIPT_SCHEMA = ROOT / "contract" / "platform-release-stage-receipt.v1.schema.json"
SCRIPT = ROOT / "scripts" / "platform_release_train.py"


def digest(number: int) -> str:
    return "sha256:" + format(number, "064x")


def task_definition(service: str, revision: int) -> str:
    return (
        "arn:aws:ecs:us-east-1:807034087062:task-definition/"
        f"leaf-platform-{service}:{revision}"
    )


def service_evidence(service: str, number: int) -> dict:
    migration = digest(800) if service == "app" else None
    return {
        "live_digest": digest(number),
        "live_task_definition": task_definition(service, 100 + number),
        "rollback_task_definition": task_definition(service, 90 + number),
        "component_source_sha256": digest(200 + number),
        "expected_component_source_sha256": digest(200 + number),
        "runtime_contract_sha256": digest(300 + number),
        "expected_runtime_contract_sha256": digest(300 + number),
        "migration_fingerprint": migration,
        "expected_migration_fingerprint": migration,
        "route_stable": True,
        "health_stable": True,
    }


def deployment_identity(source: str, service_digests: dict[str, str]) -> dict:
    body = {
        "schema": "leaf.deployment-identity.v1",
        "environment": "staging",
        "source_revision": source,
        "services": {
            service: {
                "image_digest": service_digests[service],
                "source_revision": source,
            }
            for service in SERVICES
        },
    }
    payload = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return {**body, "body_sha256": "sha256:" + hashlib.sha256(payload).hexdigest()}


def train() -> dict:
    service_digests = {
        service: digest(index) for index, service in enumerate(SERVICES, start=1)
    }
    source = "a" * 40
    result = {
        "schema": "leaf.platform-release-train.v1",
        "convergence_id": "release-evidence-1",
        "parent_run_id": 31600000000,
        "run_attempt": 1,
        "source": {
            "commit": source,
            "tree": "b" * 40,
            "workflow_blob": "c" * 40,
        },
        "supply": {
            "artifact_id": 9140000000,
            "artifact_sha256": digest(900),
            "predicate_sha256": digest(901),
            "producer_ancestry_verified": True,
            "services": service_digests,
        },
        "fresh_state": {
            "active_writers": 0,
            "open_markers": 0,
            "retained_snapshot_count": 7,
            "snapshot_overflow_acknowledgement": (
                "snapshot-overflow:807034087062:us-east-1:"
                "leaf-platform-staging:7"
            ),
            "drawing_fence": "open",
            "identity": deployment_identity(source, service_digests),
        },
        "services": {
            service: service_evidence(service, index)
            for index, service in enumerate(SERVICES, start=1)
        },
        "receipts": [],
    }
    result["receipts"] = [receipt(result, "build")]
    return result


def receipt(value: dict, stage: str, *, state: str = "terminal") -> dict:
    ordinal = ("build", *SERVICES, "identity").index(stage)
    service = stage if stage in SERVICES else None
    service_value = value["services"].get(stage)
    mutation_started = service is not None and state == "terminal"
    decision = "failed" if state == "failed" else (
        "adopted" if stage == "build" else "restamped" if stage == "identity" else "deployed"
    )
    result = {
        "schema": "leaf.platform-release-stage-receipt.v1",
        "convergence_id": value["convergence_id"],
        "parent_run_id": value["parent_run_id"],
        "run_attempt": value["run_attempt"],
        "stage": stage,
        "ordinal": ordinal,
        "state": state,
        "source_commit": value["source"]["commit"],
        "source_tree": value["source"]["tree"],
        "workflow_blob": value["source"]["workflow_blob"],
        "supply_artifact_id": value["supply"]["artifact_id"],
        "supply_sha256": value["supply"]["artifact_sha256"],
        "service": service,
        "decision": decision,
        "candidate_digest": value["supply"]["services"].get(stage),
        "terminal_digest": service_value["live_digest"] if service_value else None,
        "terminal_task_definition": (
            service_value["live_task_definition"] if service_value else None
        ),
        "rollback_task_definition": (
            service_value["rollback_task_definition"] if service_value else None
        ),
        "mutation_started": mutation_started,
        "mutation_idempotency_key": f"mutation-{stage}" if mutation_started else None,
        "started_at": "2026-08-13T12:00:00Z",
        "completed_at": "2026-08-13T12:01:00Z",
        "duration_seconds": 60,
        "finalized_after_verify": True,
        "rollback_invoked": False,
        "failure_class": "pre_mutation_input_invalid" if state == "failed" else None,
    }
    return seal_receipt(result)


def seal_receipt(value: dict) -> dict:
    value.pop("payload_sha256", None)
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    value["payload_sha256"] = "sha256:" + hashlib.sha256(payload).hexdigest()
    return value


def make_stale(value: dict, *services: str) -> None:
    for offset, service in enumerate(services, start=50):
        value["services"][service]["live_digest"] = digest(offset)
    value["fresh_state"]["identity"] = None


def test_failed_app_resume_preserves_build_and_web_and_carries_child_inputs():
    value = train()
    make_stale(value, "app")
    value["receipts"] = [
        receipt(value, "build"),
        receipt(value, "web"),
        receipt(value, "app", state="failed"),
    ]

    plan = compile_resume_plan(value)

    assert plan["preserved_stages"] == ["build", "web"]
    assert [action["stage"] for action in plan["actions"]] == ["app", "identity"]
    assert plan["actions"][0] == {
        "ordinal": 2,
        "stage": "app",
        "service": "app",
        "candidate_digest": digest(2),
        "expected_task_definition": "auto-live",
        "rollback_task_definition": task_definition("app", 92),
        "snapshot_overflow_acknowledgement": (
            "snapshot-overflow:807034087062:us-east-1:leaf-platform-staging:7"
        ),
        "drawing_fence_ownership": "transactional-auto-live",
    }
    assert plan["dispatch_authorized"] is False


def test_snapshot_authority_is_bound_to_the_fresh_count():
    value = train()
    make_stale(value, "app")
    value["fresh_state"]["retained_snapshot_count"] = 8

    plan = compile_resume_plan(value)

    assert plan["status"] == "stopped"
    assert plan["code"] == "fresh_snapshot_authority_required"
    assert plan["actions"] == []


def test_stale_three_converge_in_order_then_identity_restamps():
    value = train()
    make_stale(value, "broker", "harness", "canonical-worker")

    plan = compile_resume_plan(value)

    assert [action["stage"] for action in plan["actions"]] == [
        "broker",
        "harness",
        "canonical-worker",
        "identity",
    ]
    assert plan["identity_restamp"] is True
    assert plan["dispositions"]["web"] == "skipped"
    assert plan["dispositions"]["app"] == "skipped"


def test_resume_after_broker_terminal_starts_at_harness():
    value = train()
    make_stale(value, "harness", "canonical-worker")
    value["receipts"] = [receipt(value, "build"), receipt(value, "broker")]

    plan = compile_resume_plan(value)

    assert plan["preserved_stages"] == ["build", "broker"]
    assert [action["stage"] for action in plan["actions"]] == [
        "harness",
        "canonical-worker",
        "identity",
    ]


def test_terminal_service_drift_stops_instead_of_replaying_the_stage():
    value = train()
    value["receipts"].append(receipt(value, "web"))
    value["services"]["web"]["runtime_contract_sha256"] = digest(999)

    plan = compile_resume_plan(value)

    assert plan["status"] == "stopped"
    assert plan["code"] == "terminal_stage_drift"
    assert plan["preserved_stages"] == ["build", "web"]
    assert plan["actions"] == []


def test_later_terminal_receipt_cannot_skip_an_incomplete_earlier_stage():
    value = train()
    make_stale(value, "app")
    value["receipts"].append(receipt(value, "broker"))

    plan = compile_resume_plan(value)

    assert plan["status"] == "stopped"
    assert plan["code"] == "stage_receipt_order_invalid"
    assert plan["actions"] == []


def test_exact_five_service_identity_produces_an_empty_complete_plan():
    plan = compile_resume_plan(train())

    assert plan["status"] == "complete"
    assert plan["code"] == "already_converged"
    assert plan["actions"] == []
    assert set(plan["dispositions"].values()) == {"skipped"}
    assert plan["identity_restamp"] is False


def test_deployment_identity_body_digest_and_service_revision_are_exact():
    value = train()
    value["fresh_state"]["identity"]["body_sha256"] = digest(999)
    with pytest.raises(ContractError, match="deployment_identity_invalid"):
        compile_resume_plan(value)

    value = train()
    value["fresh_state"]["identity"]["services"]["web"]["source_revision"] = "d" * 40
    with pytest.raises(ContractError, match="deployment_identity_invalid"):
        compile_resume_plan(value)


def test_migration_or_runtime_drift_prevents_an_app_skip():
    value = train()
    value["services"]["app"]["expected_migration_fingerprint"] = digest(801)
    value["fresh_state"]["identity"] = None

    plan = compile_resume_plan(value)

    assert plan["dispositions"]["app"] == "deploy"
    assert [action["stage"] for action in plan["actions"]] == ["app", "identity"]


@pytest.mark.parametrize(
    ("field", "code"),
    [("active_writers", "active_writer_present"), ("open_markers", "open_marker_present")],
)
def test_open_writer_or_marker_stops_before_any_action(field: str, code: str):
    value = train()
    make_stale(value, "web")
    value["fresh_state"][field] = 1

    plan = compile_resume_plan(value)

    assert plan["status"] == "stopped"
    assert plan["code"] == code
    assert plan["actions"] == []


def test_resume_requires_one_terminal_build_receipt():
    value = train()
    make_stale(value, "web")
    value["receipts"] = []

    plan = compile_resume_plan(value)

    assert plan["status"] == "stopped"
    assert plan["code"] == "terminal_build_receipt_required"
    assert plan["actions"] == []


def test_mutable_tag_without_closed_signed_supply_shape_is_rejected():
    value = train()
    value["supply"]["image_tag"] = "latest"
    with pytest.raises(ContractError, match="signed_supply_invalid"):
        compile_resume_plan(value)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item.update(source_commit="d" * 40),
        lambda item: item.update(run_attempt=2),
        lambda item: item.update(supply_artifact_id=999),
        lambda item: item.update(workflow_blob="d" * 40),
    ],
)
def test_receipt_from_another_producer_tuple_is_rejected(mutate):
    value = train()
    item = receipt(value, "build")
    mutate(item)
    seal_receipt(item)
    value["receipts"] = [item]
    with pytest.raises(ContractError, match="stage_receipt_identity_mismatch"):
        compile_resume_plan(value)


def test_duplicate_stage_and_mutation_receipts_are_rejected():
    value = train()
    first = receipt(value, "web")
    value["receipts"] = [first, deepcopy(first)]
    with pytest.raises(ContractError, match="duplicate_stage_receipt"):
        compile_resume_plan(value)

    value = train()
    first = receipt(value, "web")
    second = receipt(value, "app")
    second["mutation_idempotency_key"] = first["mutation_idempotency_key"]
    seal_receipt(second)
    value["receipts"] = [first, second]
    with pytest.raises(ContractError, match="duplicate_mutation_idempotency_key"):
        compile_resume_plan(value)


def test_receipts_must_be_ordered_and_a_failure_is_terminal_for_the_attempt():
    value = train()
    value["receipts"] = [receipt(value, "web"), receipt(value, "build")]
    with pytest.raises(ContractError, match="stage_receipt_order_invalid"):
        compile_resume_plan(value)

    value = train()
    value["receipts"] = [
        receipt(value, "build"),
        receipt(value, "app", state="failed"),
        receipt(value, "broker"),
    ]
    with pytest.raises(ContractError, match="stage_receipt_order_invalid"):
        compile_resume_plan(value)

def test_terminal_receipt_must_match_fresh_digest_and_be_written_after_verify():
    value = train()
    item = receipt(value, "web")
    item["terminal_digest"] = digest(99)
    seal_receipt(item)
    value["receipts"] = [item]
    with pytest.raises(ContractError, match="stage_receipt_terminal_mismatch"):
        compile_resume_plan(value)

    value = train()
    item = receipt(value, "web")
    item["finalized_after_verify"] = False
    seal_receipt(item)
    value["receipts"] = [item]
    with pytest.raises(ContractError, match="stage_receipt_premature"):
        compile_resume_plan(value)


def test_receipt_payload_integrity_is_checked_before_reuse():
    value = train()
    item = receipt(value, "build")
    item["completed_at"] = "2026-08-13T12:02:00Z"
    value["receipts"] = [item]
    with pytest.raises(ContractError, match="stage_receipt_integrity_invalid"):
        compile_resume_plan(value)


def test_cli_emits_only_a_closed_non_executable_plan(tmp_path: Path):
    evidence = tmp_path / "train.json"
    evidence.write_text(json.dumps(train()), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--input", str(evidence)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stderr == ""
    plan = json.loads(result.stdout)
    assert plan["dispatch_authorized"] is False
    assert "token" not in result.stdout.lower()
    assert "secret" not in result.stdout.lower()


def test_contract_schemas_and_workflow_keep_the_dormant_boundary():
    train_schema = json.loads(TRAIN_SCHEMA.read_text(encoding="utf-8"))
    receipt_schema = json.loads(RECEIPT_SCHEMA.read_text(encoding="utf-8"))
    assert train_schema["additionalProperties"] is False
    assert receipt_schema["additionalProperties"] is False
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert "contents: read" in text
    assert "dispatch_authorized\"] is False" in text
    assert "persist-credentials: false" in text
    for forbidden in (
        "aws-actions/",
        "aws ecs",
        "aws s3",
        "gh workflow run",
        "repository_dispatch",
        "workflow_call:",
        "schedule:",
        "id-token: write",
        "secrets.",
    ):
        assert forbidden not in text
