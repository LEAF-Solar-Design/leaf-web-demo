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
    PRODUCER_REPOSITORY,
    PRODUCER_WORKFLOW,
    SERVICES,
    classify_source_impact,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "contract" / "platform-source-impact.v1.schema.json"
NOW = 1786638600
ISSUED = 1786628168
EXPIRES = 1789220167
SOURCE_REVISION = "6ebf16d2b032d3e460669b7aed253d16d06f7fb4"
SOURCE_TREE = "0a2eaab98582526b8f9579f443b6965a945270ec"
BASE_SOURCE_REVISION = "41c24487ce3c25c923d28ffd4778818af6fb49fb"
OLD_TREE = "698efba6b35b2a08eece8c548ba77f71d8859c21"
WORKFLOW_BLOB = "babae5cbbb819896e662499175c5c691d14e3573"
RUN_ID = 31705183737
REPOSITORY_ID = 1304548236
ARTIFACT_ID = 9183077573
ARTIFACT_NAME = f"staging-supply-set-{SOURCE_REVISION}-attempt-1"
ARCHIVE_DIGEST = "sha256:bf17a6601f35cacba1c3c9de87146e4aad8db494b529cd41edabce2cc7be4f5e"
MANIFEST_DIGEST = "sha256:7dc138f6bc217a4f0f68a43faec242e0782d70b53781f587bf0ac41d7eba5f0d"
TERMINAL_RECEIPT_DIGEST = "sha256:151a522f52cbf5c492278037c864853950d78fa8de8fad2305d325a274089127"
SUPPLY = {
    "app": "sha256:f8af2d90bb86b088473570cfab1a6f4f7217fb7ce8ee07b332ffdb0b92a1bfba",
    "broker": "sha256:de29217e7d9b0bea56fbf405971ccd5eeff3de2bdc905820bff04da5a45039cd",
    "canonical-worker": "sha256:184a799fc4c577d8987af3926a3b9b66c88df23e81d78ca54c4b9e571b04bf18",
    "harness": "sha256:8ecc697af13a0a1e61dc0b47c51c40a744ff0f0e86cf143deaad4708a72d326b",
    "web": "sha256:21fa2e82576cb7808d35173000c0e0cd9131fed59d6cb3af678c9abc348c4faa",
}


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


def producer_graph() -> dict:
    return {
        "schema": "leaf.platform-producer-input-graph.v1",
        "version": 1,
        "complete": True,
        "services": [
            {
                "name": name,
                "complete": True,
                "image_digest": SUPPLY[name],
                "old_fingerprint": digest(f"producer-inputs-{name}"),
                "new_fingerprint": digest(f"producer-inputs-{name}"),
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


def envelope() -> dict:
    value = {
        "schema": "leaf.platform-producer-evidence-envelope.v1",
        "version": 1,
        "producer": {
            "repository": PRODUCER_REPOSITORY,
            "workflow": PRODUCER_WORKFLOW,
            "workflow_blob": WORKFLOW_BLOB,
            "run_id": RUN_ID,
            "repository_id": REPOSITORY_ID,
        },
        "artifact": {
            "artifact_id": ARTIFACT_ID,
            "artifact_name": ARTIFACT_NAME,
            "archive_digest": ARCHIVE_DIGEST,
            "manifest_digest": MANIFEST_DIGEST,
        },
        "release": {
            "source_revision": SOURCE_REVISION,
            "source_tree": SOURCE_TREE,
            "base_source_revision": BASE_SOURCE_REVISION,
            "base_source_tree": OLD_TREE,
            "terminal_receipt_digest": TERMINAL_RECEIPT_DIGEST,
            "release_lineage_digest": digest("level-2-release-lineage"),
            "tenant_set_digest": digest("tenant-set"),
            "identity_shape_digest": digest("five-service-identity"),
            "approval_scope_digest": digest("level-2-approval"),
            "rollback_digest": digest("level-2-rollback"),
            "verifier_digest": digest("level-2-verifier"),
        },
        "supply": deepcopy(SUPPLY),
        "producer_graph": producer_graph(),
        "producer_graph_digest": "",
        "supply_digest": "",
        "issued_at_epoch": ISSUED,
        "expires_at_epoch": EXPIRES,
        "content_digest": "",
    }
    return seal(value)


def seal(value: dict) -> dict:
    value["producer_graph_digest"] = sha256_digest(value["producer_graph"])
    value["supply_digest"] = sha256_digest(
        {name: value["supply"][name] for name in SERVICES}
    )
    without_digest = {key: deepcopy(item) for key, item in value.items() if key != "content_digest"}
    value["content_digest"] = sha256_digest(without_digest)
    return value


def trust_anchor(value: dict) -> dict:
    producer_identity_digest = sha256_digest(value["producer"])
    release = value["release"]
    artifact = value["artifact"]
    release_scope_digest = sha256_digest(
        {
            "producer_identity_digest": producer_identity_digest,
            "artifact_id": artifact["artifact_id"],
            "archive_digest": artifact["archive_digest"],
            "manifest_digest": artifact["manifest_digest"],
            "terminal_receipt_digest": release["terminal_receipt_digest"],
            "release_lineage_digest": release["release_lineage_digest"],
            "tenant_set_digest": release["tenant_set_digest"],
            "identity_shape_digest": release["identity_shape_digest"],
            "approval_scope_digest": release["approval_scope_digest"],
            "rollback_digest": release["rollback_digest"],
            "verifier_digest": release["verifier_digest"],
            "producer_graph_digest": value["producer_graph_digest"],
            "supply_digest": value["supply_digest"],
        }
    )
    return {
        "schema": "leaf.platform-producer-trust-anchor.v1",
        "version": 1,
        "producer_identity_digest": producer_identity_digest,
        "workflow_blob": value["producer"]["workflow_blob"],
        "run_id": value["producer"]["run_id"],
        "artifact_id": artifact["artifact_id"],
        "artifact_archive_digest": artifact["archive_digest"],
        "manifest_digest": artifact["manifest_digest"],
        "terminal_receipt_digest": release["terminal_receipt_digest"],
        "source_revision": release["source_revision"],
        "source_tree": release["source_tree"],
        "base_source_revision": release["base_source_revision"],
        "base_source_tree": release["base_source_tree"],
        "producer_graph_digest": value["producer_graph_digest"],
        "supply_digest": value["supply_digest"],
        "release_scope_digest": release_scope_digest,
        "envelope_content_digest": value["content_digest"],
        "expires_at_epoch": value["expires_at_epoch"],
    }


def evidence(producer_evidence: dict | None = None) -> dict:
    return {
        "schema": "leaf.platform-source-impact-input.v2",
        "selector": "UNCONFIGURED",
        "old_tree": OLD_TREE,
        "new_tree": SOURCE_TREE,
        "relay_base_tree": OLD_TREE,
        "deferred": False,
        "producer_evidence": envelope() if producer_evidence is None else producer_evidence,
    }


def evaluate(value: dict, anchor: dict | None = None, now: int = NOW) -> dict:
    producer_evidence = value["producer_evidence"]
    return classify_source_impact(
        value,
        producer_trust_anchor=trust_anchor(producer_evidence) if anchor is None else anchor,
        now_epoch=now,
        fixture_enabled=True,
    )


def test_real_level2_producer_envelope_allows_nil_impact_only_with_exact_anchor():
    value = evidence()
    result = evaluate(value)

    assert result["classification"] == "nil_impact"
    assert result["affected_services"] == []
    assert result["producer_source_revision"] == SOURCE_REVISION
    assert result["new_tree"] == SOURCE_TREE
    assert result["terminal_receipt_digest"] == TERMINAL_RECEIPT_DIGEST
    assert result["producer_supply_digest"] == value["producer_evidence"]["supply_digest"]
    assert result["selector_activation_authorized"] is False


def test_fabricated_equal_fingerprints_without_independent_anchor_never_yield_nil():
    with pytest.raises(ContractError, match="PRODUCER_EVIDENCE_UNCONFIGURED"):
        classify_source_impact(evidence(), now_epoch=NOW, fixture_enabled=True)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item["producer"].update(repository="attacker/example"),
        lambda item: item["producer"].update(workflow=".github/workflows/other.yml"),
        lambda item: item["producer"].update(workflow_blob="f" * 40),
        lambda item: item["producer"].update(run_id=RUN_ID + 1),
        lambda item: item["producer"].update(repository_id=REPOSITORY_ID + 1),
        lambda item: item["artifact"].update(artifact_id=ARTIFACT_ID + 1),
        lambda item: item["artifact"].update(
            artifact_name=f"staging-supply-set-{SOURCE_REVISION}-attempt-2"
        ),
        lambda item: item["artifact"].update(archive_digest=digest("wrong-archive")),
        lambda item: item["artifact"].update(manifest_digest=digest("wrong-manifest")),
        lambda item: item["release"].update(source_revision="f" * 40),
        lambda item: item["release"].update(source_tree="f" * 40),
        lambda item: item["release"].update(base_source_revision="f" * 40),
        lambda item: item["release"].update(base_source_tree="f" * 40),
        lambda item: item["release"].update(terminal_receipt_digest=digest("wrong-receipt")),
        lambda item: item["release"].update(release_lineage_digest=digest("replayed-lineage")),
        lambda item: item["supply"].update(app=digest("wrong-app-image")),
    ],
)
def test_resealed_wrong_producer_artifact_source_or_lineage_refuses_old_anchor(mutation):
    original = envelope()
    anchor = trust_anchor(original)
    altered = deepcopy(original)
    mutation(altered)
    seal(altered)
    with pytest.raises(ContractError):
        evaluate(evidence(altered), anchor=anchor)


def test_swapped_envelope_and_payload_is_refused():
    original = envelope()
    swapped = deepcopy(original)
    swapped["release"]["source_tree"] = "f" * 40
    seal(swapped)
    with pytest.raises(ContractError, match="PRODUCER_EVIDENCE_TRUST_MISMATCH"):
        evaluate(evidence(swapped), anchor=trust_anchor(original))


def test_trusted_candidate_cannot_be_paired_with_an_arbitrary_old_tree():
    value = evidence()
    value["old_tree"] = "f" * 40
    with pytest.raises(ContractError, match="PRODUCER_SOURCE_TREE_MISMATCH"):
        evaluate(value)


def test_altered_graph_with_stale_digest_is_refused_before_classification():
    value = envelope()
    value["producer_graph"]["services"][0]["new_fingerprint"] = digest("altered")
    with pytest.raises(ContractError, match="PRODUCER_GRAPH_DIGEST_MISMATCH"):
        evaluate(evidence(value), anchor=trust_anchor(envelope()))


def test_expired_unsupported_and_extra_field_envelopes_fail_closed():
    value = envelope()
    with pytest.raises(ContractError, match="PRODUCER_EVIDENCE_EXPIRED"):
        evaluate(evidence(value), now=EXPIRES)

    unsupported = envelope()
    unsupported["version"] = 2
    seal(unsupported)
    with pytest.raises(ContractError, match="PRODUCER_EVIDENCE_VERSION_INVALID"):
        evaluate(evidence(unsupported), anchor=trust_anchor(envelope()))

    extra = envelope()
    extra["raw_path"] = "forbidden"
    with pytest.raises(ContractError, match="PRODUCER_EVIDENCE_INVALID"):
        evaluate(evidence(extra), anchor=trust_anchor(envelope()))

    missing = envelope()
    del missing["artifact"]["manifest_digest"]
    with pytest.raises(ContractError, match="PRODUCER_ARTIFACT_INVALID"):
        evaluate(evidence(missing), anchor=trust_anchor(envelope()))


@pytest.mark.parametrize("input_class", [
    "base_images", "build_args", "dependencies", "dockerfile",
    "required_config", "source_inputs", "toolchain",
])
def test_each_incomplete_producer_input_class_is_product_impact(input_class: str):
    value = envelope()
    value["producer_graph"]["services"][0]["input_classes"][input_class] = False
    seal(value)
    result = evaluate(evidence(value))
    assert result["classification"] == "product_impact"
    assert result["affected_services"] == ["app"]
    assert result["reason_code"] == "producer_graph_incomplete"


def test_changed_producer_fingerprint_is_product_impact():
    value = envelope()
    value["producer_graph"]["services"][2]["new_fingerprint"] = digest("changed")
    seal(value)
    result = evaluate(evidence(value))
    assert result["classification"] == "product_impact"
    assert result["affected_services"] == ["canonical-worker"]


def test_incomplete_graph_and_deferred_stale_base_fail_closed():
    value = envelope()
    value["producer_graph"]["complete"] = False
    seal(value)
    assert evaluate(evidence(value))["affected_services"] == list(SERVICES)

    deferred = evidence()
    deferred["deferred"] = True
    deferred["relay_base_tree"] = "f" * 40
    result = evaluate(deferred)
    assert result["reason_code"] == "deferred_reclassification_required"


def test_noncanonical_service_order_and_path_oracle_are_rejected():
    value = envelope()
    value["producer_graph"]["services"].reverse()
    seal(value)
    with pytest.raises(ContractError, match="SERVICE_SET_INVALID"):
        evaluate(evidence(value), anchor=trust_anchor(envelope()))

    path_oracle = evidence()
    path_oracle["changed_paths"] = ["README.md"]
    with pytest.raises(ContractError, match="SOURCE_IMPACT_INPUT_INVALID"):
        evaluate(path_oracle)


def test_default_is_unconfigured_and_output_is_closed_and_redacted():
    with pytest.raises(ContractError, match="UNCONFIGURED"):
        classify_source_impact(evidence())
    result = evaluate(evidence())
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema = jsonschema_module()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(result)
    lowered = json.dumps(result).casefold()
    for token in ("tenant", "subject", "path", "secret", "token", "artifact_name"):
        assert token not in lowered
