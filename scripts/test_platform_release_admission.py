from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import sysconfig

import pytest
import yaml

from platform_release_admission import evaluate_release_admission, workflow_preflight
from platform_semantic_eligibility import ContractError, sha256_digest


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "contract" / "platform-release-admission.v1.schema.json"
WORKFLOW = ROOT / ".github" / "workflows" / "qualify-platform-release-admission.yml"
SCRIPT = ROOT / "scripts" / "platform_release_admission.py"


def jsonschema_module():
    loaded = sys.modules.get("platform")
    if loaded is None or not hasattr(loaded, "python_implementation"):
        path = Path(sysconfig.get_path("stdlib")) / "platform.py"
        spec = importlib.util.spec_from_file_location("platform", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules["platform"] = module
        spec.loader.exec_module(module)
    return importlib.import_module("jsonschema")


def digest(name: str) -> str:
    return sha256_digest({"name": name})


def evidence() -> dict:
    return {
        "schema": "leaf.platform-release-admission-input.v1",
        "selector": "UNCONFIGURED",
        "candidate": {
            "source_tree": "b" * 40,
            "impact_classification": "product_impact",
            "impact_digest": digest("impact"),
            "classification_base_tree": "a" * 40,
            "approval_scope_digest": digest("scope"),
            "queue_age_seconds": 30,
            "queue_count": 1,
            "urgent": False,
        },
        "settlement": {
            "active": False,
            "census_started": False,
            "terminal_receipt_published": True,
            "release_ready": False,
            "identity_restamp_active": False,
            "active_writers": 0,
            "open_markers": 0,
            "census_head": "a" * 40,
            "source_head": "a" * 40,
            "expected_approval_scope_digest": digest("scope"),
            "prior_train_digest": digest("prior"),
        },
        "limits": {"max_queue_age_seconds": 3600, "max_queue_count": 8},
        "urgent_authority": None,
    }


def evaluate(value: dict) -> dict:
    return evaluate_release_admission(value, fixture_enabled=True)


def test_open_window_admits_without_authorizing_a_writer():
    result = evaluate(evidence())

    assert result["decision"] == "admit"
    assert result["reason_code"] == "admission_window_open"
    assert result["writer_acquisition_authorized"] is False
    assert result["selector_activation_authorized"] is False


def test_no_release_is_admitted_between_census_and_terminal_receipt():
    value = evidence()
    value["settlement"].update(
        active=True,
        census_started=True,
        terminal_receipt_published=False,
        release_ready=True,
    )

    result = evaluate(value)

    assert result["decision"] == "hold"
    assert result["reason_code"] == "prior_receipt_pending"


def test_timeline_pr579_nil_impact_is_held_and_coalesced_during_settlement():
    value = evidence()
    value["candidate"]["impact_classification"] = "nil_impact"
    value["settlement"].update(active=True, census_started=True, terminal_receipt_published=False)

    result = evaluate(value)

    assert result["decision"] == "coalesce"
    assert result["reason_code"] == "nil_impact_held_during_settlement"


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda value: value["settlement"].update(identity_restamp_active=True), "settlement_occupied"),
        (lambda value: value["settlement"].update(active_writers=1), "settlement_occupied"),
        (lambda value: value["settlement"].update(open_markers=1), "settlement_occupied"),
        (lambda value: value["settlement"].update(census_head="c" * 40), "classification_or_census_stale"),
        (lambda value: value["candidate"].update(classification_base_tree="c" * 40), "classification_or_census_stale"),
        (lambda value: value["candidate"].update(approval_scope_digest=digest("other")), "approval_scope_mismatch"),
        (lambda value: value["candidate"].update(queue_age_seconds=3601), "queue_expired_reclassify"),
        (lambda value: value["candidate"].update(queue_count=9), "queue_expired_reclassify"),
    ],
)
def test_settlement_drift_and_bounded_queue_fail_closed(mutation, reason: str):
    value = evidence()
    mutation(value)
    result = evaluate(value)
    assert result["decision"] == "hold"
    assert result["reason_code"] == reason


def test_urgent_bypass_requires_exact_displaced_train_scope_and_rollback():
    value = evidence()
    value["candidate"]["urgent"] = True
    value["settlement"].update(active=True, census_started=True, terminal_receipt_published=False)
    value["urgent_authority"] = {
        "approval_scope_digest": digest("scope"),
        "displaced_train_digest": digest("prior"),
        "rollback_digest": digest("rollback"),
    }
    assert evaluate(value)["reason_code"] == "urgent_authority_exact"

    value["urgent_authority"]["displaced_train_digest"] = digest("wrong")
    assert evaluate(value)["decision"] == "hold"


def test_timeline_pr577_product_change_is_held_for_identity_shape_regression():
    value = evidence()
    value["settlement"].update(active=True, census_started=True, terminal_receipt_published=False)
    result = evaluate(value)
    assert result == {**result, "decision": "hold", "reason_code": "prior_receipt_pending"}


def test_output_schema_and_default_unconfigured_contract():
    with pytest.raises(ContractError, match="UNCONFIGURED"):
        evaluate_release_admission(evidence())
    result = evaluate(evidence())
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema = jsonschema_module()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(result)


def test_manual_workflow_has_no_live_or_publication_surface(tmp_path: Path):
    text = WORKFLOW.read_text(encoding="utf-8")
    document = yaml.load(text, Loader=yaml.BaseLoader)
    assert set(document["on"]) == {"workflow_dispatch"}
    assert document["permissions"] == {"contents": "read"}
    assert document["on"]["workflow_dispatch"]["inputs"]["shadow_enabled"]["default"] == "false"
    lowered = text.casefold()
    for token in (
        "aws-actions/", "terraform apply", "workflow_run:", "schedule:", "push:",
        "pull_request:", "repository_dispatch", "upload-artifact", "gh workflow run",
    ):
        assert token not in lowered
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "workflow-preflight", "--shadow-enabled", "false"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 78
    assert json.loads(result.stdout) == workflow_preflight(shadow_enabled=False)
    assert list(tmp_path.iterdir()) == []


def test_workflow_run_blocks_are_valid_bash():
    document = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    blocks = [
        step["run"]
        for job in document["jobs"].values()
        for step in job.get("steps", [])
        if "run" in step
    ]
    assert len(blocks) == 2
    for block in blocks:
        result = subprocess.run([str(bash), "-n"], input=block.encode(), capture_output=True)
        assert result.returncode == 0, result.stderr.decode(errors="replace")
