from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path
import sys
import sysconfig

import pytest

from platform_semantic_eligibility import ContractError, sha256_digest
from platform_source_impact import SERVICES, classify_source_impact


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "contract" / "platform-source-impact.v1.schema.json"


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


def producer_graph() -> dict:
    return {
        "schema": "leaf.platform-producer-input-graph.v1",
        "version": "v1",
        "complete": True,
        "services": [
            {
                "name": name,
                "complete": True,
                "old_fingerprint": sha256_digest({"service": name, "inputs": "same"}),
                "new_fingerprint": sha256_digest({"service": name, "inputs": "same"}),
                "input_classes": {
                    "base_images": True,
                    "build_args": True,
                    "dependencies": True,
                    "dockerfile": True,
                    "required_config": True,
                    "source_inputs": True,
                    "toolchain": True,
                },
            }
            for name in SERVICES
        ],
    }


def evidence() -> dict:
    return {
        "schema": "leaf.platform-source-impact-input.v1",
        "selector": "UNCONFIGURED",
        "old_tree": "a" * 40,
        "new_tree": "b" * 40,
        "relay_base_tree": "a" * 40,
        "deferred": False,
        "producer_graph": producer_graph(),
    }


def evaluate(value: dict) -> dict:
    return classify_source_impact(value, fixture_enabled=True)


def test_comment_test_or_manifest_only_movement_is_nil_only_from_equal_graph():
    result = evaluate(evidence())

    assert result["classification"] == "nil_impact"
    assert result["affected_services"] == []
    assert result["reason_code"] == "producer_inputs_equal"
    assert result["selector_activation_authorized"] is False


@pytest.mark.parametrize(
    "input_class",
    [
        "base_images",
        "build_args",
        "dependencies",
        "dockerfile",
        "required_config",
        "source_inputs",
        "toolchain",
    ],
)
def test_each_incomplete_producer_input_class_fails_closed(input_class: str):
    value = evidence()
    value["producer_graph"]["services"][0]["input_classes"][input_class] = False

    result = evaluate(value)

    assert result["classification"] == "product_impact"
    assert result["affected_services"] == ["app"]
    assert result["reason_code"] == "producer_graph_incomplete"


def test_changed_dockerfile_or_dependency_fingerprint_is_product_impact():
    value = evidence()
    value["producer_graph"]["services"][2]["new_fingerprint"] = sha256_digest(
        {"service": "canonical-worker", "inputs": "changed"}
    )

    result = evaluate(value)

    assert result["classification"] == "product_impact"
    assert result["affected_services"] == ["canonical-worker"]
    assert result["reason_code"] == "producer_input_changed"


def test_unknown_graph_is_product_impact_for_every_service():
    value = evidence()
    value["producer_graph"]["complete"] = False

    result = evaluate(value)

    assert result["classification"] == "product_impact"
    assert result["affected_services"] == list(SERVICES)


def test_deferred_classification_uses_relay_time_base_or_reclassifies():
    value = evidence()
    value["deferred"] = True
    value["relay_base_tree"] = "c" * 40

    result = evaluate(value)

    assert result["classification"] == "product_impact"
    assert result["reason_code"] == "deferred_reclassification_required"
    assert result["affected_services"] == list(SERVICES)


def test_paths_cannot_be_supplied_as_a_nil_impact_oracle():
    value = evidence()
    value["changed_paths"] = ["README.md"]
    with pytest.raises(ContractError, match="SOURCE_IMPACT_INPUT_INVALID"):
        evaluate(value)


def test_producer_graph_digest_is_stable_across_input_service_order():
    first = evidence()
    second = evidence()
    second["producer_graph"]["services"].reverse()

    assert evaluate(first)["producer_graph_digest"] == evaluate(second)[
        "producer_graph_digest"
    ]


def test_default_adapter_is_unconfigured():
    with pytest.raises(ContractError, match="UNCONFIGURED"):
        classify_source_impact(evidence())


def test_output_matches_closed_schema_and_contains_no_raw_path_or_identity():
    result = evaluate(evidence())
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema = jsonschema_module()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(result)
    lowered = json.dumps(result).casefold()
    assert "tenant" not in lowered
    assert "subject" not in lowered
    assert "path" not in lowered
    assert "secret" not in lowered
