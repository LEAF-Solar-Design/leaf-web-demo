from __future__ import annotations

from collections.abc import Iterator, Mapping
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
    TrustedProducerRoots,
    ValidatedProducerEvidenceToken,
    _fixture_token_payload,
    _fixture_trusted_roots,
    _seal_token,
    classify_source_impact,
    verify_producer_evidence_token,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "contract" / "platform-source-impact.v1.schema.json"
BASE_TREE = "698efba6b35b2a08eece8c548ba77f71d8859c21"


class DuplicateKeyMapping(Mapping[str, object]):
    def __init__(self, value: dict[str, object]) -> None:
        self.value = value

    def __getitem__(self, key: str) -> object:
        return self.value[key]

    def __iter__(self) -> Iterator[str]:
        first = next(iter(self.value))
        yield first
        yield first
        yield from list(self.value)[1:]

    def __len__(self) -> int:
        return len(self.value) + 1


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


def evidence(token: object | None = None) -> dict:
    return {
        "schema": "leaf.platform-source-impact-input.v3",
        "selector": "UNCONFIGURED",
        "relay_base_tree": BASE_TREE,
        "deferred": False,
        "producer_token": (
            _fixture_token_payload() if token is None else token
        ),
    }


def evaluate(value: dict | None = None) -> dict:
    return classify_source_impact(
        evidence() if value is None else value,
        trusted_roots=_fixture_trusted_roots(),
        now_epoch=FIXTURE_NOW,
        fixture_enabled=True,
    )


def test_real_level2_token_is_verified_before_nil_impact():
    token = _fixture_token_payload()
    validated = verify_producer_evidence_token(
        token, _fixture_trusted_roots(), now_epoch=FIXTURE_NOW
    )
    result = evaluate(evidence(token))

    assert isinstance(validated, ValidatedProducerEvidenceToken)
    assert result["classification"] == "nil_impact"
    assert result["affected_services"] == []
    assert result["producer_token_digest"] == token["content_digest"]
    assert result["release_scope_digest"] == token["release_scope_digest"]
    assert result["selector_activation_authorized"] is False


def test_authority_types_have_no_public_constructor_or_dict_shortcut():
    with pytest.raises(TypeError, match="no public constructor"):
        TrustedProducerRoots()
    with pytest.raises(TypeError, match="no public constructor"):
        ValidatedProducerEvidenceToken()
    with pytest.raises(ContractError, match="TRUSTED_ROOTS_UNCONFIGURED"):
        verify_producer_evidence_token(
            _fixture_token_payload(), {}, now_epoch=FIXTURE_NOW
        )


def test_fabricated_internally_consistent_token_is_not_authority():
    forged = _fixture_token_payload()
    forged["producer"]["run_id"] += 1
    forged = _seal_token(forged)

    with pytest.raises(ContractError, match="PRODUCER_TOKEN_UNTRUSTED"):
        evaluate(evidence(forged))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item["producer"].update(repository="attacker/repo"),
        lambda item: item["producer"].update(
            workflow=".github/workflows/attacker.yml"
        ),
        lambda item: item["producer"].update(workflow_blob="f" * 40),
        lambda item: item["producer"].update(run_id=1),
        lambda item: item["producer"].update(repository_id=1),
        lambda item: item["artifact"].update(artifact_id=1),
        lambda item: item["artifact"].update(
            artifact_name="staging-supply-set-" + "f" * 40 + "-attempt-1"
        ),
        lambda item: item["artifact"].update(
            archive_content_sha256=sha256_digest("forged")
        ),
        lambda item: item["artifact"].update(
            manifest_digest=sha256_digest("forged-manifest")
        ),
        lambda item: item["source"].update(base_revision="f" * 40),
        lambda item: item["source"].update(base_tree="f" * 40),
        lambda item: item["source"].update(candidate_revision="f" * 40),
        lambda item: item["source"].update(candidate_tree="f" * 40),
        lambda item: item["terminal"].update(
            receipt_digest=sha256_digest("forged-receipt")
        ),
        lambda item: item["terminal"].update(
            release_lineage={
                **item["terminal"]["release_lineage"],
                "candidate_tree": "f" * 40,
            }
        ),
        lambda item: item["terminal"].update(
            tenant_binding={
                **item["terminal"]["tenant_binding"],
                "tenant_scope": "other-tenant",
            }
        ),
        lambda item: item["terminal"].update(
            approval_scope={
                **item["terminal"]["approval_scope"],
                "class": "forged-approval",
            }
        ),
        lambda item: item["terminal"].update(
            rollback={
                **item["terminal"]["rollback"],
                "source_revision": "f" * 40,
            }
        ),
        lambda item: item["terminal"].update(
            verifier={
                **item["terminal"]["verifier"],
                "contract": "forged-verifier",
            }
        ),
        lambda item: item["terminal"].update(
            topology={
                **item["terminal"]["topology"],
                "writer_count": 1,
            }
        ),
        lambda item: item["deployment_identity"].update(
            body_digest=sha256_digest("forged-identity")
        ),
    ],
)
def test_resealed_anchor_source_tenant_or_lineage_change_is_refused(mutation):
    token = _fixture_token_payload()
    mutation(token)
    token = _seal_token(token)
    with pytest.raises(ContractError):
        evaluate(evidence(token))


def test_swapped_payload_and_stale_checksums_fail_before_classification():
    token = _fixture_token_payload()
    token["producer_graph"]["services"][0]["new_fingerprint"] = (
        sha256_digest("changed")
    )
    with pytest.raises(ContractError, match="PRODUCER_GRAPH_DIGEST_MISMATCH"):
        evaluate(evidence(token))

    swapped = _fixture_token_payload()
    other = _fixture_token_payload()
    other["source"]["candidate_tree"] = "f" * 40
    swapped["source"] = other["source"]
    with pytest.raises(ContractError):
        evaluate(evidence(swapped))


def test_expiry_version_extra_missing_and_digest_only_fail_closed():
    with pytest.raises(ContractError, match="PRODUCER_TOKEN_EXPIRED"):
        classify_source_impact(
            evidence(),
            trusted_roots=_fixture_trusted_roots(),
            now_epoch=_fixture_token_payload()["expires_at_epoch"],
            fixture_enabled=True,
        )

    unsupported = _fixture_token_payload()
    unsupported["version"] = 2
    with pytest.raises(ContractError, match="PRODUCER_TOKEN_VERSION_INVALID"):
        evaluate(evidence(unsupported))

    extra = _fixture_token_payload()
    extra["raw_catalog"] = "forbidden"
    with pytest.raises(ContractError, match="PRODUCER_TOKEN_INVALID"):
        evaluate(evidence(extra))

    missing = _fixture_token_payload()
    del missing["artifact"]["manifest_digest"]
    with pytest.raises(ContractError, match="PRODUCER_ARTIFACT_INVALID"):
        evaluate(evidence(missing))

    with pytest.raises(ContractError, match="PRODUCER_TOKEN_INVALID"):
        evaluate(evidence(_fixture_token_payload()["content_digest"]))

    duplicate = DuplicateKeyMapping(_fixture_token_payload())
    with pytest.raises(ContractError, match="PRODUCER_TOKEN_INVALID"):
        evaluate(evidence(duplicate))


def test_deferred_stale_base_is_product_impact_not_nil():
    value = evidence()
    value["deferred"] = True
    value["relay_base_tree"] = "f" * 40
    result = evaluate(value)
    assert result["classification"] == "product_impact"
    assert result["reason_code"] == "deferred_reclassification_required"


def test_default_unconfigured_output_schema_and_closed_result():
    with pytest.raises(ContractError, match="UNCONFIGURED"):
        classify_source_impact(
            evidence(),
            trusted_roots=_fixture_trusted_roots(),
            now_epoch=FIXTURE_NOW,
        )
    result = evaluate()
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema = jsonschema_module()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(result)
    lowered = json.dumps(result).casefold()
    for token in ("subject", "secret", "artifact_name", "raw_catalog"):
        assert token not in lowered
