from __future__ import annotations

from copy import deepcopy
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import sysconfig

import pytest

from platform_proof_reuse import PROFILES, evaluate_proof_reuse
from platform_semantic_eligibility import ContractError, sha256_digest
from platform_source_impact import (
    FIXTURE_NOW,
    _fixture_token_payload,
    _fixture_trusted_roots,
    _seal_token,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "contract" / "platform-proof-reuse.v1.schema.json"
BASE_TREE = "698efba6b35b2a08eece8c548ba77f71d8859c21"


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


def evidence() -> dict:
    token = _fixture_token_payload()
    lineage = token["terminal"]["release_lineage_digest"]
    return {
        "schema": "leaf.platform-proof-reuse-input.v3",
        "selector": "UNCONFIGURED",
        "current_token": deepcopy(token),
        "admitted_token": deepcopy(token),
        "prior_receipt": {
            "terminal_state": "terminal_green",
            "verifier_result": "pass",
            "rollback_result": "pass",
            "product_mutation_result": "clean",
            "receipt_digest": token["terminal"]["receipt_digest"],
        },
        "candidate": {
            "current_lineage_digest": lineage,
            "admitted_lineage_digest": lineage,
            "workspace_profiles": list(PROFILES),
            "lineage_complete": True,
            "relay_base_tree": BASE_TREE,
            "deferred": False,
        },
    }


def evaluate(value: dict) -> dict:
    return evaluate_proof_reuse(
        value,
        current_trusted_roots=_fixture_trusted_roots(),
        admitted_trusted_roots=_fixture_trusted_roots(),
        now_epoch=FIXTURE_NOW,
        fixture_enabled=True,
    )


def test_exact_full_tokens_attach_terminal_proof_once():
    result = evaluate(evidence())

    assert result["decision"] == "reuse"
    assert result["reason_code"] == "proof_reuse_exact"
    assert result["reuse_attachment_digest"].startswith("sha256:")
    assert result["workspace_profile_count"] == 4
    assert result["selector_activation_authorized"] is False
    assert result["proof_execution_authorized"] is False


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda value: value["prior_receipt"].update(
                terminal_state="failed"
            ),
            "prior_not_terminal_green",
        ),
        (
            lambda value: value["prior_receipt"].update(
                verifier_result="failed"
            ),
            "verifier_not_green",
        ),
        (
            lambda value: value["prior_receipt"].update(
                rollback_result="failed"
            ),
            "rollback_not_green",
        ),
        (
            lambda value: value["prior_receipt"].update(
                product_mutation_result="dirty"
            ),
            "product_mutation_not_clean",
        ),
        (
            lambda value: value["prior_receipt"].update(
                receipt_digest=sha256_digest("other")
            ),
            "receipt_binding_mismatch",
        ),
        (
            lambda value: value["candidate"].update(
                lineage_complete=False
            ),
            "lineage_incomplete",
        ),
        (
            lambda value: value["candidate"].update(
                current_lineage_digest=sha256_digest("other")
            ),
            "current_lineage_mismatch",
        ),
        (
            lambda value: value["candidate"].update(
                admitted_lineage_digest=sha256_digest("other")
            ),
            "admitted_lineage_mismatch",
        ),
    ],
)
def test_each_receipt_and_lineage_relation_fails_closed(mutation, reason: str):
    value = evidence()
    mutation(value)
    result = evaluate(value)
    assert result["decision"] == "fresh_proof"
    assert result["reason_code"] == reason
    assert result["reuse_attachment_digest"] is None


@pytest.mark.parametrize(
    "mutation",
    [
        lambda token: token["terminal"].update(
            tenant_binding={
                **token["terminal"]["tenant_binding"],
                "tenant_scope": "other-tenant",
            }
        ),
        lambda token: token["terminal"].update(
            approval_scope={
                **token["terminal"]["approval_scope"],
                "class": "other-approval",
            }
        ),
        lambda token: token["terminal"].update(
            rollback={
                **token["terminal"]["rollback"],
                "source_revision": "f" * 40,
            }
        ),
        lambda token: token["terminal"].update(
            verifier={
                **token["terminal"]["verifier"],
                "contract": "other-verifier",
            }
        ),
        lambda token: token["deployment_identity"].update(
            body_digest=sha256_digest("other-identity")
        ),
    ],
)
def test_current_admitted_pair_cannot_rebind_authority(mutation):
    value = evidence()
    mutation(value["current_token"])
    value["current_token"] = _seal_token(value["current_token"])
    with pytest.raises(ContractError):
        evaluate(value)


def test_digest_pairs_and_digest_only_tokens_never_authorize_reuse():
    r2_counterexample = {
        "schema": "leaf.platform-proof-reuse-input.v2",
        "selector": "UNCONFIGURED",
        "prior_receipt": {},
        "candidate": {
            "producer_evidence_digest": sha256_digest("producer"),
            "admitted_producer_evidence_digest": sha256_digest("producer"),
            "evidence_binding_digest": sha256_digest("binding"),
            "admitted_evidence_binding_digest": sha256_digest("binding"),
        },
    }
    with pytest.raises(ContractError, match="PROOF_REUSE_INPUT_INVALID"):
        evaluate(r2_counterexample)

    value = evidence()
    value["current_token"] = value["current_token"]["content_digest"]
    with pytest.raises(ContractError, match="PRODUCER_TOKEN_INVALID"):
        evaluate(value)


def test_profile_order_is_one_malleable_workspace_contract():
    value = evidence()
    value["candidate"]["workspace_profiles"] = [
        "browser",
        "cad",
        "ios",
        "solar_cad",
    ]
    with pytest.raises(ContractError, match="WORKSPACE_PROFILE_SET_INVALID"):
        evaluate(value)


def test_default_unconfigured_and_output_schema_is_closed():
    with pytest.raises(ContractError, match="UNCONFIGURED"):
        evaluate_proof_reuse(
            evidence(),
            current_trusted_roots=_fixture_trusted_roots(),
            admitted_trusted_roots=_fixture_trusted_roots(),
            now_epoch=FIXTURE_NOW,
        )
    result = evaluate(evidence())
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema = jsonschema_module()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(result)
    lowered = json.dumps(result).casefold()
    for token in ("subject", "secret", "artifact_name", "raw_catalog"):
        assert token not in lowered
