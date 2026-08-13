from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path
import sys
import sysconfig

import pytest

from platform_semantic_eligibility import ContractError, sha256_digest
from platform_supply_coalesce import SERVICES, evaluate_supply_coalescing


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "contract" / "platform-supply-coalesce.v1.schema.json"


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


def services() -> list[dict]:
    return [
        {
            "name": name,
            "image_digest": "sha256:" + format(index, "064x"),
            "provenance_digest": digest(f"provenance-{name}"),
            "producer_source_revision": "a" * 40,
            "producer_source_tree": "b" * 40,
            "surface_fingerprint": digest(f"surface-{name}"),
            "recipe_fingerprint": digest(f"recipe-{name}"),
            "toolchain_digest": digest(f"toolchain-{name}"),
            "dependencies_digest": digest(f"dependencies-{name}"),
            "build_arguments_digest": digest(f"arguments-{name}"),
            "required_config_digest": digest(f"config-{name}"),
        }
        for index, name in enumerate(SERVICES, start=1)
    ]


def movement(tree: str) -> dict:
    return {
        "source_revision": ("c" if tree.startswith("c") else "d") * 40,
        "source_tree": tree,
        "impact_classification": "nil_impact",
        "impact_digest": digest(f"impact-{tree}"),
        "producer_evidence_digest": digest(f"producer-evidence-{tree}"),
        "evidence_binding_digest": digest(f"evidence-binding-{tree}"),
        "release_scope_digest": digest("level2-release-scope"),
        "services": services(),
    }


def evidence() -> dict:
    return {
        "schema": "leaf.platform-supply-coalesce-input.v2",
        "selector": "UNCONFIGURED",
        "admission_window_digest": digest("window"),
        "movements": [movement("c" * 40), movement("d" * 40)],
    }


def evaluate(value: dict) -> dict:
    return evaluate_supply_coalescing(value, fixture_enabled=True)


def test_complete_five_service_equivalence_plans_one_lineage():
    result = evaluate(evidence())

    assert result["decision"] == "plan"
    assert result["reason_code"] == "complete_supply_equivalent"
    assert result["movement_count"] == 2
    assert result["affected_services"] == []
    assert result["planned_lineage_digest"].startswith("sha256:")
    assert result["supply_mint_authorized"] is False


@pytest.mark.parametrize("retained", [3, 4])
def test_partial_three_or_four_of_five_supply_is_refused(retained: int):
    value = evidence()
    value["movements"][1]["services"] = value["movements"][1]["services"][:retained]
    with pytest.raises(ContractError, match="SUPPLY_SERVICE_SET_INVALID"):
        evaluate(value)


@pytest.mark.parametrize(
    "field",
    [
        "image_digest",
        "provenance_digest",
        "producer_source_revision",
        "producer_source_tree",
        "surface_fingerprint",
        "recipe_fingerprint",
        "toolchain_digest",
        "dependencies_digest",
        "build_arguments_digest",
        "required_config_digest",
    ],
)
def test_each_changed_producer_input_or_lineage_refuses_coalescing(field: str):
    value = evidence()
    service = value["movements"][1]["services"][0]
    if field in {"producer_source_revision", "producer_source_tree"}:
        service[field] = "e" * 40
    else:
        service[field] = digest(f"changed-{field}")
    result = evaluate(value)
    assert result["decision"] == "refuse"
    assert result["reason_code"] == "producer_inputs_changed"
    assert result["affected_services"] == ["app"]
    assert result["planned_lineage_digest"] is None


def test_product_impact_refuses_even_when_supply_bytes_match():
    value = evidence()
    value["movements"][1]["impact_classification"] = "product_impact"
    result = evaluate(value)
    assert result["decision"] == "refuse"
    assert result["reason_code"] == "product_impact_present"


def test_cross_release_replay_and_duplicate_evidence_are_refused():
    value = evidence()
    value["movements"][1]["release_scope_digest"] = digest("other-release")
    result = evaluate(value)
    assert result["decision"] == "refuse"
    assert result["reason_code"] == "producer_evidence_changed"

    duplicate = evidence()
    duplicate["movements"][1]["producer_evidence_digest"] = duplicate["movements"][0]["producer_evidence_digest"]
    duplicate["movements"][1]["evidence_binding_digest"] = duplicate["movements"][0]["evidence_binding_digest"]
    with pytest.raises(ContractError, match="COALESCE_EVIDENCE_REPLAY"):
        evaluate(duplicate)


def test_duplicate_source_movement_is_rejected():
    value = evidence()
    value["movements"][1]["source_tree"] = value["movements"][0]["source_tree"]
    with pytest.raises(ContractError, match="COALESCE_MOVEMENT_DUPLICATE"):
        evaluate(value)


def test_default_is_unconfigured_and_output_schema_is_closed():
    with pytest.raises(ContractError, match="UNCONFIGURED"):
        evaluate_supply_coalescing(evidence())
    result = evaluate(evidence())
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema = jsonschema_module()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(result)
    assert set(result) == {
        "schema", "state", "decision", "reason_code", "movement_count",
        "affected_services", "admission_window_digest", "planned_lineage_digest",
        "release_scope_digest", "evidence_chain_digest",
        "selector_activation_authorized", "supply_mint_authorized",
    }
