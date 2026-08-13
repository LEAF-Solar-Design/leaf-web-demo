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

from platform_arrival_reconcile import reconcile_arrival
from platform_semantic_eligibility import ContractError, sha256_digest


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "contract" / "platform-arrival-reconciliation.v1.schema.json"
WORKFLOW = ROOT / ".github" / "workflows" / "qualify-platform-arrival-reconciliation.yml"
SCRIPT = ROOT / "scripts" / "platform_arrival_reconcile.py"
PR583 = "e3b709a08dd822e320cd4fb410f63e887aca0357"
PR585 = "ee71607c9d9a8b347f9526ca3ec9509919170d5d"
TREE583 = "d" * 40
TREE585 = "29e560c0f7639d6b8ab0e8381e8da9f8878d81a1"
NOW = 1786641000


def digest(label: str) -> str:
    return sha256_digest({"fixture": label})


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


def run(
    run_id: int,
    workflow: str,
    conclusion: str,
    source: str,
    duration: int,
    owner: str = "same_train",
) -> dict:
    return {
        "run_id": run_id,
        "workflow": workflow,
        "conclusion": conclusion,
        "source_revision": source,
        "duration_seconds": duration,
        "owner_class": owner,
    }


def level3(product: str, *, coalesce: str = "coalesce", reuse: str = "reuse") -> dict:
    token = digest("producer-token")
    scope = digest("release-scope")

    def decision(name: str, value: str) -> dict:
        return {
            "receipt_digest": digest(name),
            "decision": value,
            "producer_token_digest": token,
            "release_scope_digest": scope,
        }

    return {
        "source_impact": decision("source-impact", product),
        "release_admission": decision("release-admission", "admit"),
        "supply_coalescing": decision("supply-coalescing", coalesce),
        "proof_reuse": decision("proof-reuse", reuse),
    }


def base_document(source: str, tree: str, pr_number: int, product: str) -> dict:
    return {
        "schema": "leaf.platform-arrival-reconciliation-input.v1",
        "selectors": {
            "arrival_observation": "UNCONFIGURED",
            "frontier_reconciliation": "UNCONFIGURED",
        },
        "arrival": {
            "arrival_id": digest(f"arrival-{pr_number}"),
            "repository": "LEAF-Solar-Design/leaf-web-demo",
            "previous_main": "a" * 40,
            "merge_commit": source,
            "current_main": source,
            "current_tree": tree,
            "pr_number": pr_number,
            "changed_path_set_digest": digest(f"paths-{pr_number}"),
            "observed_at_epoch": NOW - 60,
        },
        "level3": level3(product),
        "effects": {
            "producer_runs": [],
            "relay_runs": [],
            "supply": None,
            "convergence": None,
            "failed_stage": None,
            "later_writer": None,
        },
        "terminal": {
            "receipt_digests": [digest(f"terminal-{pr_number}")],
            "preserved_stages": [],
            "rollback_source_revision": source,
        },
        "frontier": {
            "current_main": source,
            "current_supply_manifest_digest": digest(f"manifest-{pr_number}"),
            "current_identity_digest": digest(f"identity-{pr_number}"),
            "active_writer_count": 0,
            "marker_count": 0,
            "live_services_exact": True,
            "owner_class": "same_train",
            "superseded_by": None,
        },
        "observed_at_epoch": NOW,
    }


def pr583_document() -> dict:
    value = base_document(PR583, TREE583, 583, "product_impact")
    value["effects"] = {
        "producer_runs": [
            run(31696766443, ".github/workflows/build-platform-images.yml", "success", PR583, 102),
            run(31696923082, ".github/workflows/deploy-web.yml", "success", PR583, 480),
            run(31697109132, ".github/workflows/deploy-harness.yml", "failure", PR583, 18),
            run(31697654883, ".github/workflows/deploy-app.yml", "success", PR583, 600),
            run(31697818897, ".github/workflows/deploy-harness.yml", "success", PR583, 540),
        ],
        "relay_runs": [
            run(31696892936, ".github/workflows/dispatch-staging-deploys.yml", "success", PR583, 1578)
        ],
        "supply": {
            "artifact_id": 9181000000,
            "artifact_digest": digest("pr583-supply-artifact"),
            "manifest_digest": digest("pr583-manifest"),
            "service_count": 5,
            "complete": True,
        },
        "convergence": {
            "artifact_id": 9181000001,
            "artifact_digest": digest("pr583-convergence"),
            "source_revision": PR583,
            "state": "converged",
            "service_count": 5,
        },
        "failed_stage": {
            "name": "harness",
            "run_id": 31697109132,
            "classification": "protected_input_validation",
            "credentials_configured": False,
            "live_mutation_started": False,
            "observed_dispatch": {"sha_set": True, "image_tag_set": True},
            "resume_dispatch": {"sha_set": False, "image_tag_set": True},
        },
        "later_writer": {
            "repository": "LEAF-Solar-Design/leaf-automation-aws-terraform",
            "workflow": ".github/workflows/upstream-sink.yml",
            "run_id": 31699210098,
            "conclusion": "success",
            "owner_class": "external_owner",
            "image_digest_changed": False,
            "terminal_handoff_digest": digest("pr583-later-writer"),
        },
    }
    value["terminal"] = {
        "receipt_digests": [
            "sha256:477729f8fbc41a4ea0b11923f4d4543d52bd6604629ee9f4e7ae19f024475344",
            digest("pr583-writer-terminal"),
        ],
        "preserved_stages": ["build", "web", "relay", "live_readback"],
        "rollback_source_revision": PR583,
    }
    value["frontier"]["current_supply_manifest_digest"] = digest("pr583-manifest")
    return value


def pr585_document() -> dict:
    value = base_document(PR585, TREE585, 585, "nil_impact")
    value["effects"] = {
        "producer_runs": [
            run(31702420838, ".github/workflows/build-platform-images.yml", "success", PR585, 112),
            run(31702420620, ".github/workflows/build-instant-execution-image.yml", "success", PR585, 88),
        ],
        "relay_runs": [
            run(31702568953, ".github/workflows/dispatch-staging-deploys.yml", "success", PR585, 1404)
        ],
        "supply": {
            "artifact_id": 9182500000,
            "artifact_digest": digest("pr585-supply-artifact"),
            "manifest_digest": "sha256:4d49ce9fea51c26eafc6783dfa3cadd6eeec4d3ffd8ac22e7e9b877ec4141587",
            "service_count": 5,
            "complete": True,
        },
        "convergence": {
            "artifact_id": 9182583677,
            "artifact_digest": "sha256:a72517edc0b6e5a72f197e0b68e775986891ac181be59dc8729653a318a554b8",
            "source_revision": PR585,
            "state": "converged",
            "service_count": 5,
        },
        "failed_stage": None,
        "later_writer": None,
    }
    value["terminal"] = {
        "receipt_digests": [digest("pr585-terminal")],
        "preserved_stages": ["build", "instant_build", "relay", "convergence"],
        "rollback_source_revision": PR585,
    }
    value["frontier"]["current_supply_manifest_digest"] = (
        "sha256:4d49ce9fea51c26eafc6783dfa3cadd6eeec4d3ffd8ac22e7e9b877ec4141587"
    )
    return value


def evaluate(value: dict) -> dict:
    return reconcile_arrival(value, fixture_enabled=True)


def validate_schema(result: dict) -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema = jsonschema_module()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(result)


def test_pr583_preserves_green_predecessors_and_resumes_only_harness():
    result = evaluate(pr583_document())
    validate_schema(result)
    assert result["product_impact"] == "build_input"
    assert result["trigger_impact"] == "relay"
    assert result["supply_effect"] == "complete_new_supply"
    assert result["live_effect"] == "failed_stage"
    assert result["disposition"] == "resume_failed_stage"
    assert result["reason_code"] == "single_failed_stage_preserved"
    assert result["failed_or_remaining_stage"] == "harness"
    assert {"build", "web"}.issubset(result["preserved_stages"])
    assert result["next_level2_action"] == "resume_failed_stage"
    assert not any(result["authority"].values())


def test_pr585_product_nil_still_adopts_complete_operational_frontier():
    result = evaluate(pr585_document())
    validate_schema(result)
    assert result["product_impact"] == "nil"
    assert result["trigger_impact"] == "relay"
    assert result["supply_effect"] == "complete_new_supply"
    assert result["live_effect"] == "converged"
    assert result["disposition"] == "adopt_frontier"
    assert result["reason_code"] == "product_nil_operational_frontier"
    assert result["convergence_artifact_id"] == 9182583677
    assert result["relay_run_ids"] == [31702568953]


def test_pr585_fixtures_bind_measured_1404_second_rail_occupation():
    value = pr585_document()
    assert value["effects"]["producer_runs"][0]["duration_seconds"] == 112
    assert value["effects"]["producer_runs"][1]["duration_seconds"] == 88
    assert value["effects"]["relay_runs"][0]["duration_seconds"] == 1404
    assert evaluate(value)["disposition"] == "adopt_frontier"


def test_pr583_invalid_resume_shape_fails_closed():
    value = pr583_document()
    value["effects"]["failed_stage"]["resume_dispatch"] = {
        "sha_set": True,
        "image_tag_set": True,
    }
    with pytest.raises(ContractError, match="RESUME_DISPATCH_INVALID"):
        evaluate(value)


def test_unattributed_writer_holds_without_takeover():
    value = pr583_document()
    value["effects"]["later_writer"]["owner_class"] = "unknown"
    result = evaluate(value)
    assert result["disposition"] == "hold"
    assert result["reason_code"] == "owner_unattributed"
    assert not result["authority"]["cancel"]


def test_later_writer_image_change_requires_rebind():
    value = pr583_document()
    value["effects"]["later_writer"]["image_digest_changed"] = True
    result = evaluate(value)
    assert result["disposition"] == "rebind"
    assert result["reason_code"] == "later_writer_supply_changed"


def test_partial_supply_and_partial_live_convergence_fail_closed():
    value = pr585_document()
    value["effects"]["supply"].update(complete=False, service_count=4)
    result = evaluate(value)
    assert result["disposition"] == "hold"
    assert result["reason_code"] == "partial_supply"

    value = pr585_document()
    value["effects"]["convergence"]["service_count"] = 4
    with pytest.raises(ContractError, match="CONVERGENCE_PARTIAL"):
        evaluate(value)


def test_relay_in_progress_cannot_produce_terminal_disposition():
    value = pr585_document()
    value["effects"]["relay_runs"][0]["conclusion"] = "in_progress"
    value["effects"]["convergence"]["state"] = "in_progress"
    value["effects"]["convergence"]["service_count"] = 0
    result = evaluate(value)
    assert result["disposition"] == "hold"
    assert result["reason_code"] == "relay_nonterminal"


def test_product_nil_requires_level3_coalescing_and_proof_reuse():
    for key, decision in (("supply_coalescing", "hold"), ("proof_reuse", "fresh_proof")):
        value = pr585_document()
        value["level3"][key]["decision"] = decision
        result = evaluate(value)
        assert result["disposition"] == "hold"
        assert result["reason_code"] == "level3_adoption_evidence_incomplete"


def test_dormant_selector_does_not_prove_quiet_push_producer():
    value = pr585_document()
    assert value["selectors"] == {
        "arrival_observation": "UNCONFIGURED",
        "frontier_reconciliation": "UNCONFIGURED",
    }
    result = evaluate(value)
    assert result["trigger_impact"] == "relay"
    assert result["supply_effect"] == "complete_new_supply"


def test_docs_noop_marker_is_not_a_five_service_supply():
    value = base_document("b" * 40, "c" * 40, 591, "nil_impact")
    value["effects"]["producer_runs"] = [
        run(40000000001, ".github/workflows/build-platform-images.yml", "success", "b" * 40, 12)
    ]
    result = evaluate(value)
    assert result["trigger_impact"] == "build"
    assert result["supply_effect"] == "none"
    assert result["disposition"] == "preserve"


def test_newer_terminal_frontier_stands_old_train_down():
    value = pr585_document()
    value["frontier"]["current_main"] = "f" * 40
    value["frontier"]["superseded_by"] = {
        "current_main": "f" * 40,
        "supply_manifest_digest": digest("newer-supply"),
        "terminal_handoff_digest": digest("newer-handoff"),
    }
    result = evaluate(value)
    assert result["disposition"] == "stand_down"
    assert result["reason_code"] == "newer_frontier_terminal"
    assert not result["authority"]["dispatch"]


def test_newer_unsettled_frontier_holds_old_train():
    value = pr585_document()
    value["frontier"]["current_main"] = "f" * 40
    result = evaluate(value)
    assert result["disposition"] == "hold"
    assert result["reason_code"] == "current_main_drift"


def test_writer_or_marker_occupancy_holds():
    for field in ("active_writer_count", "marker_count"):
        value = pr585_document()
        value["frontier"][field] = 1
        result = evaluate(value)
        assert result["disposition"] == "hold"
        assert result["reason_code"] == "frontier_occupied"


def test_duplicate_runs_receipts_and_level3_rebinding_are_refused():
    value = pr585_document()
    value["effects"]["relay_runs"][0]["run_id"] = value["effects"]["producer_runs"][0]["run_id"]
    with pytest.raises(ContractError, match="DUPLICATE_RUN_EVIDENCE"):
        evaluate(value)

    value = pr585_document()
    value["terminal"]["receipt_digests"] *= 2
    with pytest.raises(ContractError, match="DUPLICATE_RECEIPT_EVIDENCE"):
        evaluate(value)

    value = pr585_document()
    value["level3"]["proof_reuse"]["producer_token_digest"] = digest("other-token")
    with pytest.raises(ContractError, match="LEVEL3_EVIDENCE_REBOUND"):
        evaluate(value)


def test_missing_extra_stale_and_selector_activation_fail_closed():
    value = pr585_document()
    del value["arrival"]["current_tree"]
    with pytest.raises(ContractError, match="ARRIVAL_INVALID"):
        evaluate(value)

    value = pr585_document()
    value["raw_log"] = "forbidden"
    with pytest.raises(ContractError, match="ARRIVAL_DOCUMENT_INVALID"):
        evaluate(value)

    value = pr585_document()
    value["observed_at_epoch"] = value["arrival"]["observed_at_epoch"] + 31 * 24 * 60 * 60 + 1
    with pytest.raises(ContractError, match="ARRIVAL_EVIDENCE_STALE"):
        evaluate(value)

    value = pr585_document()
    value["selectors"]["arrival_observation"] = "ENABLED"
    with pytest.raises(ContractError, match="SELECTOR_ACTIVATION_FORBIDDEN"):
        evaluate(value)


def test_default_path_is_unconfigured_and_closed_output_is_private():
    with pytest.raises(ContractError, match="UNCONFIGURED"):
        reconcile_arrival(pr585_document())
    result = evaluate(pr585_document())
    lowered = json.dumps(result).casefold()
    for token in (
        "tenant",
        "subject",
        "secret",
        "token",
        "request_body",
        "raw_log",
        "exception",
        "environment",
    ):
        assert token not in lowered
    assert not any(result["authority"].values())


def test_workflow_is_manual_read_only_and_preflight_exits_unconfigured():
    parsed = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    trigger = parsed.get("on", parsed.get(True))
    assert set(trigger) == {"workflow_dispatch"}
    assert parsed["permissions"] == {"contents": "read"}
    source = WORKFLOW.read_text(encoding="utf-8")
    for forbidden in (
        "aws-actions/configure-aws-credentials",
        "gh workflow run",
        "gh api",
        "workflow_call",
        "schedule:",
        "push:",
        "pull_request:",
        "upload-artifact",
    ):
        assert forbidden not in source
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "workflow-preflight", "--shadow-enabled", "false"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 78
    value = json.loads(completed.stdout)
    assert value["state"] == "UNCONFIGURED"
    assert value["provider_calls"] == 0
    assert value["receipt_published"] is False
    assert value["dispatch_authorized"] is False
    assert value["live_mutation_authorized"] is False


def test_product_module_has_no_provider_or_process_client():
    source = SCRIPT.read_text(encoding="utf-8")
    for forbidden in ("boto3", "requests", "httpx", "urllib", "subprocess", "socket"):
        assert forbidden not in source
