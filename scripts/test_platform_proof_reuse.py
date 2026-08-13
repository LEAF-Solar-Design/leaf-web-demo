from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path
import sys
import sysconfig

import pytest

from platform_proof_reuse import PROFILES, evaluate_proof_reuse
from platform_semantic_eligibility import ContractError, sha256_digest


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "contract" / "platform-proof-reuse.v1.schema.json"


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
        "schema": "leaf.platform-proof-reuse-input.v1",
        "selector": "UNCONFIGURED",
        "prior_receipt": {
            "terminal_state": "terminal_green",
            "verifier_result": "pass",
            "rollback_result": "pass",
            "product_mutation_result": "clean",
            "tenant_set_digest": digest("tenants"),
            "approval_scope_digest": digest("scope"),
            "identity_shape_digest": digest("identity"),
            "source_impact_digest": digest("impact"),
            "lineage_digest": digest("prior-lineage"),
            "receipt_digest": digest("receipt"),
            "workspace_profiles": list(PROFILES),
            "lineage_complete": True,
        },
        "candidate": {
            "tenant_set_digest": digest("tenants"),
            "approval_scope_digest": digest("scope"),
            "identity_shape_digest": digest("identity"),
            "source_impact_digest": digest("new-nil-impact"),
            "admitted_source_impact_digest": digest("new-nil-impact"),
            "source_impact_classification": "nil_impact",
            "predecessor_lineage_digest": digest("prior-lineage"),
            "admitted_lineage_digest": digest("new-lineage"),
            "workspace_profiles": list(PROFILES),
            "lineage_complete": True,
        },
    }


def evaluate(value: dict) -> dict:
    return evaluate_proof_reuse(value, fixture_enabled=True)


def test_exact_terminal_proof_is_atomically_attached_to_new_lineage():
    result = evaluate(evidence())

    assert result["decision"] == "reuse"
    assert result["reason_code"] == "proof_reuse_exact"
    assert result["reuse_attachment_digest"].startswith("sha256:")
    assert result["workspace_profile_count"] == 4
    assert result["selector_activation_authorized"] is False
    assert result["proof_execution_authorized"] is False


def test_verifier_failed_receipt_is_nonterminal_even_after_cleanup_and_rollback():
    value = evidence()
    value["prior_receipt"]["verifier_result"] = "failed"
    result = evaluate(value)
    assert result["decision"] == "fresh_proof"
    assert result["reason_code"] == "verifier_not_green"
    assert result["reuse_attachment_digest"] is None


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda value: value["prior_receipt"].update(terminal_state="failed"), "prior_not_terminal_green"),
        (lambda value: value["prior_receipt"].update(rollback_result="failed"), "rollback_not_green"),
        (lambda value: value["prior_receipt"].update(product_mutation_result="dirty"), "product_mutation_not_clean"),
        (lambda value: value["candidate"].update(lineage_complete=False), "lineage_incomplete"),
        (lambda value: value["candidate"].update(source_impact_classification="product_impact"), "source_impact_not_nil"),
        (lambda value: value["candidate"].update(predecessor_lineage_digest=digest("other")), "lineage_mismatch"),
        (lambda value: value["candidate"].update(tenant_set_digest=digest("other")), "tenant_set_mismatch"),
        (lambda value: value["candidate"].update(approval_scope_digest=digest("other")), "approval_scope_mismatch"),
        (lambda value: value["candidate"].update(identity_shape_digest=digest("other")), "identity_shape_mismatch"),
        (
            lambda value: value["candidate"].update(
                admitted_source_impact_digest=digest("other")
            ),
            "source_impact_mismatch",
        ),
    ],
)
def test_each_reuse_predicate_fails_closed(mutation, reason: str):
    value = evidence()
    mutation(value)
    result = evaluate(value)
    assert result["decision"] == "fresh_proof"
    assert result["reason_code"] == reason


def test_malleable_profiles_share_one_identity_and_separate_state_is_refused():
    value = evidence()
    assert evaluate(value)["decision"] == "reuse"

    value["candidate"]["workspace_profiles"] = ["browser", "cad", "ios", "solar_cad"]
    with pytest.raises(ContractError, match="WORKSPACE_PROFILE_SET_INVALID"):
        evaluate(value)


def test_default_is_unconfigured_and_output_is_closed_and_redacted():
    with pytest.raises(ContractError, match="UNCONFIGURED"):
        evaluate_proof_reuse(evidence())
    result = evaluate(evidence())
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema = jsonschema_module()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(result)
    lowered = json.dumps(result).casefold()
    for token in ("tenant", "subject", "secret", "token", "browser", "solar_cad"):
        assert token not in lowered
