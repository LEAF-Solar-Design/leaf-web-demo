from __future__ import annotations

from copy import deepcopy
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import sysconfig

import pytest

from platform_semantic_eligibility import ContractError, sha256_digest
from platform_source_impact import (
    FIXTURE_NOW,
    _fixture_token_payload,
    _fixture_trusted_roots,
    _seal_token,
)
from platform_supply_coalesce import evaluate_supply_coalescing


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "contract" / "platform-supply-coalesce.v1.schema.json"
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
    return {
        "schema": "leaf.platform-supply-coalesce-input.v3",
        "selector": "UNCONFIGURED",
        "admission_window_digest": sha256_digest("window"),
        "movements": [
            {
                "producer_token": _fixture_token_payload(),
                "relay_base_tree": BASE_TREE,
                "deferred": False,
            }
        ],
    }


def evaluate(value: dict) -> dict:
    return evaluate_supply_coalescing(
        value,
        trusted_roots=_fixture_trusted_roots(),
        now_epoch=FIXTURE_NOW,
        fixture_enabled=True,
    )


def test_exact_complete_token_plans_one_lineage():
    result = evaluate(evidence())

    assert result["decision"] == "plan"
    assert result["reason_code"] == "complete_supply_equivalent"
    assert result["movement_count"] == 1
    assert result["affected_services"] == []
    assert result["planned_lineage_digest"].startswith("sha256:")
    assert result["supply_mint_authorized"] is False


def test_duplicate_token_replay_is_refused_before_planning():
    value = evidence()
    value["movements"].append(deepcopy(value["movements"][0]))
    with pytest.raises(ContractError, match="COALESCE_EVIDENCE_REPLAY"):
        evaluate(value)


def test_deferred_wrong_base_becomes_product_impact_and_refuses():
    value = evidence()
    value["movements"][0]["deferred"] = True
    value["movements"][0]["relay_base_tree"] = "f" * 40
    result = evaluate(value)
    assert result["decision"] == "refuse"
    assert result["reason_code"] == "product_impact_present"
    assert result["affected_services"] == [
        "app",
        "broker",
        "canonical-worker",
        "harness",
        "web",
    ]


def test_internally_consistent_tenant_or_release_rebind_is_untrusted():
    for field in ("tenant_binding", "release_lineage"):
        value = evidence()
        token = value["movements"][0]["producer_token"]
        if field == "tenant_binding":
            token["terminal"][field]["tenant_scope"] = "forged-tenant"
        else:
            token["terminal"][field]["candidate_tree"] = "f" * 40
        value["movements"][0]["producer_token"] = _seal_token(token)
        with pytest.raises(ContractError):
            evaluate(value)


def test_unique_fabricated_digest_pairs_and_digest_only_token_are_blocked():
    r2_counterexample = {
        "schema": "leaf.platform-supply-coalesce-input.v2",
        "selector": "UNCONFIGURED",
        "admission_window_digest": sha256_digest("window"),
        "movements": [
            {
                "source_revision": "a" * 40,
                "source_tree": "b" * 40,
                "impact_classification": "nil_impact",
                "impact_digest": sha256_digest("impact"),
                "producer_evidence_digest": sha256_digest("unique-producer"),
                "evidence_binding_digest": sha256_digest("unique-binding"),
                "release_scope_digest": sha256_digest("unique-scope"),
                "services": [],
            }
        ],
    }
    with pytest.raises(ContractError, match="COALESCE_INPUT_INVALID"):
        evaluate(r2_counterexample)

    digest_only = evidence()
    digest_only["movements"][0]["producer_token"] = (
        _fixture_token_payload()["content_digest"]
    )
    with pytest.raises(ContractError, match="PRODUCER_TOKEN_INVALID"):
        evaluate(digest_only)


def test_default_unconfigured_and_output_schema_is_closed():
    with pytest.raises(ContractError, match="UNCONFIGURED"):
        evaluate_supply_coalescing(
            evidence(),
            trusted_roots=_fixture_trusted_roots(),
            now_epoch=FIXTURE_NOW,
        )
    result = evaluate(evidence())
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema = jsonschema_module()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(result)
    assert set(result) == {
        "schema",
        "state",
        "decision",
        "reason_code",
        "movement_count",
        "affected_services",
        "admission_window_digest",
        "planned_lineage_digest",
        "release_scope_digest",
        "evidence_chain_digest",
        "selector_activation_authorized",
        "supply_mint_authorized",
    }
