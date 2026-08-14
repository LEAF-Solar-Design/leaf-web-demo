from __future__ import annotations

from copy import deepcopy
import base64
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "platform_release_guardrails.py"
SPEC = importlib.util.spec_from_file_location("platform_release_guardrails", MODULE_PATH)
assert SPEC and SPEC.loader
guardrails = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guardrails)


def digest(char: str) -> str:
    return f"sha256:{char * 64}"


def sha(char: str) -> str:
    return char * 40


def receipt() -> dict[str, object]:
    return {
        "convergence_id": "wave-b-project-conversation",
        "parent_run_id": 31684025271,
        "run_attempt": 1,
        "dependency_generation": 7,
        "source_commit": sha("a"),
        "source_tree": sha("b"),
        "workflow_blob": sha("c"),
        "supply_artifact_id": 9183077573,
        "supply_sha256": digest("1"),
        "supply_predicate_sha256": digest("2"),
        "payload_sha256": digest("3"),
        "mutation_idempotency_key": "wave-b:broker:1",
    }


def service_state(service: str) -> dict[str, object]:
    index = guardrails.SERVICES.index(service)
    return {
        "image_digest": digest(str(index + 1)),
        "source_revision": sha("a"),
        "runtime_contract_sha256": digest("a"),
        "migration_fingerprint_sha256": digest("b") if service == "app" else None,
        "configuration_fingerprint_sha256": digest("c"),
        "route_state": "stable" if service in {"web", "app"} else "not_applicable",
        "health_state": "healthy",
    }


def authority() -> dict[str, str]:
    return {field: digest("d") for field in guardrails.AUTHORITY_FIELDS}


def positive_payload() -> dict[str, object]:
    services = {service: service_state(service) for service in guardrails.SERVICES}
    identity_services = {
        service: {
            "image_digest": services[service]["image_digest"],
            "source_revision": services[service]["source_revision"],
        }
        for service in guardrails.SERVICES
    }
    delta_body = {
        "checkpoint_sha256": digest("e"),
        "settlements": [
            {
                "ordinal": 1,
                "transaction_id": "wave-b:broker",
                "marker_sha256": digest("f"),
                "artifact_status": "terminal",
            }
        ],
        "result_open_set_sha256": digest("0"),
    }
    base_receipt = receipt()
    provider_bytes = b"raw provider evidence\n"
    provider_digest = f"sha256:{hashlib.sha256(provider_bytes).hexdigest()}"
    return {
        "schema": "leaf.platform-release-guardrails.v1",
        "substitute_evidence": {
            "receipt_tool_failed": True,
            "expected_binding": base_receipt,
            "substitute_binding": deepcopy(base_receipt),
        },
        "effective_state": {
            "candidate_services": services,
            "live_services": deepcopy(services),
            "deployment_identity": {
                "schema": "leaf.deployment-identity.v1",
                "environment": "staging",
                "source_revision": sha("a"),
                "services": identity_services,
            },
        },
        "authority_diff": {
            "approved": authority(),
            "corrected": authority(),
        },
        "identity_restamp": {
            "expected_parent_run_id": 31684025271,
            "expected_workflow_blob": sha("4"),
            "executions": [
                {
                    "parent_run_id": 31684025271,
                    "workflow_blob": sha("4"),
                    "intent": "configuration",
                }
            ],
        },
        "provider_oracle": {
            "raw_provider_bytes_b64": base64.b64encode(provider_bytes).decode("ascii"),
            "declared_raw_provider_bytes_sha256": provider_digest,
            "shared_predicate_sha256": digest("6"),
            "binding_recomputed_from_raw": True,
        },
        "marker_proof": {
            "checkpoint_sha256": digest("e"),
            "delta": {
                **delta_body,
                "delta_sha256": guardrails._canonical_sha256(delta_body),
            },
            "indexed_open_count": 0,
            "indexed_open_set_sha256": digest("0"),
            "full_audit_open_count": 0,
            "full_audit_open_set_sha256": digest("0"),
            "artifact_without_ledger_count": 0,
            "last_completed_full_audit_age_seconds": 300,
            "maximum_full_audit_age_seconds": 86400,
        },
        "proposed_gates": [
            {
                "gate_id": "fable-unavailable",
                "stage": "publish_ci",
                "enabled_path_failure": "model unavailable",
                "harm_class": None,
                "reachable": True,
                "current_evidence_id": "model-unavailable",
                "evidence_fresh": True,
                "duplicates_stronger_evidence": True,
                "stronger_green_evidence_id": "required-ci-green",
                "why_unique": "",
            }
        ],
    }


def test_positive_fixture_is_ready_and_never_authorizes_execution() -> None:
    result = guardrails.evaluate(positive_payload())
    assert result["status"] == "ready"
    assert result["selector_activation_authorized"] is False
    assert result["dispatch_authorized"] is False
    assert result["live_mutation_authorized"] is False
    assert set(result["decisions"]) == {
        "substitute_evidence",
        "effective_state",
        "authority_diff",
        "identity_restamp",
        "provider_oracle",
        "marker_proof",
        "proposed_gates",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_attempt", 2),
        ("dependency_generation", 8),
        ("supply_predicate_sha256", digest("9")),
        ("payload_sha256", digest("8")),
        ("mutation_idempotency_key", None),
    ],
)
def test_substitute_receipt_drift_blocks(field: str, value: object) -> None:
    payload = positive_payload()
    payload["substitute_evidence"]["substitute_binding"][field] = value
    result = guardrails.evaluate(payload)
    decision = result["decisions"]["substitute_evidence"]
    assert decision["decision"] == "blocking"
    assert field in decision["mismatches"]


@pytest.mark.parametrize(
    "field",
    [
        "image_digest",
        "runtime_contract_sha256",
        "migration_fingerprint_sha256",
        "configuration_fingerprint_sha256",
    ],
)
def test_effective_state_drift_requires_reconciliation(field: str) -> None:
    payload = positive_payload()
    payload["effective_state"]["live_services"]["app"][field] = digest("9")
    decision = guardrails.evaluate(payload)["decisions"]["effective_state"]
    assert decision["decision"] == "reconcile"
    assert f"app.{field}" in decision["drift"]


def test_identity_relation_drift_requires_reconciliation() -> None:
    payload = positive_payload()
    payload["effective_state"]["deployment_identity"]["services"]["broker"][
        "image_digest"
    ] = digest("9")
    decision = guardrails.evaluate(payload)["decisions"]["effective_state"]
    assert "identity.broker.image_digest" in decision["drift"]


@pytest.mark.parametrize(
    "field",
    [
        "permissions_sha256",
        "credentials_sha256",
        "external_recipients_sha256",
        "workflow_blobs_sha256",
        "rendered_actions_sha256",
    ],
)
def test_authority_drift_requires_new_authority(field: str) -> None:
    payload = positive_payload()
    payload["authority_diff"]["corrected"][field] = digest("9")
    decision = guardrails.evaluate(payload)["decisions"]["authority_diff"]
    assert decision["decision"] == "new_authority_required"
    assert field in decision["changed_fields"]


def test_standalone_or_duplicate_restamp_executor_is_rejected() -> None:
    payload = positive_payload()
    payload["identity_restamp"]["executions"].append(
        {
            "parent_run_id": 999,
            "workflow_blob": sha("9"),
            "intent": "configuration",
        }
    )
    decision = guardrails.evaluate(payload)["decisions"]["identity_restamp"]
    assert decision == {
        "decision": "reject",
        "code": "restamp_executor_conflict",
        "execution_count": 2,
    }


def test_shared_predicate_agreement_cannot_replace_raw_oracle() -> None:
    payload = positive_payload()
    payload["provider_oracle"]["declared_raw_provider_bytes_sha256"] = digest("6")
    payload["provider_oracle"]["shared_predicate_sha256"] = digest("6")
    decision = guardrails.evaluate(payload)["decisions"]["provider_oracle"]
    assert decision["decision"] == "reject"
    assert decision["shared_predicate_agreement"] is True


def test_provider_oracle_rejects_noncanonical_raw_encoding() -> None:
    payload = positive_payload()
    payload["provider_oracle"]["raw_provider_bytes_b64"] += "\n"
    with pytest.raises(guardrails.ContractError, match="provider_raw_bytes_invalid"):
        guardrails.evaluate(payload)


@pytest.mark.parametrize(
    "mutation",
    ["stale_anchor", "delta_hash", "open_marker", "artifact_gap", "audit_stale"],
)
def test_marker_failures_block(mutation: str) -> None:
    payload = positive_payload()
    marker = payload["marker_proof"]
    if mutation == "stale_anchor":
        marker["delta"]["checkpoint_sha256"] = digest("9")
    elif mutation == "delta_hash":
        marker["delta"]["delta_sha256"] = digest("9")
    elif mutation == "open_marker":
        marker["indexed_open_count"] = 1
        marker["full_audit_open_count"] = 1
    elif mutation == "artifact_gap":
        marker["artifact_without_ledger_count"] = 1
    else:
        marker["last_completed_full_audit_age_seconds"] = 86401
    decision = guardrails.evaluate(payload)["decisions"]["marker_proof"]
    assert decision["decision"] == "reject"


def test_reachable_data_integrity_gate_blocks() -> None:
    payload = positive_payload()
    payload["proposed_gates"] = [
        {
            "gate_id": "migration-expand-only",
            "stage": "deploy",
            "enabled_path_failure": "rolling task reads a removed conflict target",
            "harm_class": "tenancy_authorization_or_data_integrity",
            "reachable": True,
            "current_evidence_id": "wave-b-migration-rollback-conflict",
            "evidence_fresh": True,
            "duplicates_stronger_evidence": False,
            "stronger_green_evidence_id": None,
            "why_unique": "This check is the only pre-mutation proof of rolling compatibility.",
        }
    ]
    decision = guardrails.evaluate(payload)["decisions"]["proposed_gates"]
    assert decision["decision"] == "blocking"
    assert decision["gates"][0]["code"] == "minimum_blocker_satisfied"


def test_duplicate_gate_with_named_stronger_evidence_is_advisory() -> None:
    payload = positive_payload()
    gate = payload["proposed_gates"][0]
    gate.update(
        {
            "harm_class": "externally_required_control",
            "why_unique": "Required CI proves the same source property.",
        }
    )
    decision = guardrails.evaluate(payload)["decisions"]["proposed_gates"]
    assert decision["decision"] == "advisory"


def test_duplicate_json_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text('{"schema":"a","schema":"b"}', encoding="utf-8")
    with path.open(encoding="utf-8") as stream:
        with pytest.raises(guardrails.ContractError, match="duplicate_json_key"):
            guardrails.load_json(stream)


def test_contract_schema_and_workflow_are_dormant() -> None:
    schema = json.loads(
        (ROOT / "contract" / "platform-release-guardrails.v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert schema["additionalProperties"] is False
    workflow = (
        ROOT / ".github" / "workflows" / "qualify-platform-release-guardrails.yml"
    ).read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "contents: read" in workflow
    assert "dispatch_authorized\"] is False" in workflow
    assert "live_mutation_authorized\"] is False" in workflow
    for forbidden in ("aws-actions/", "id-token: write", "schedule:", "push:"):
        assert forbidden not in workflow
