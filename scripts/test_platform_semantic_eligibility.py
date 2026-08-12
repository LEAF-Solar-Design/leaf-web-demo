from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from platform_semantic_eligibility import (
    ASSERTION_IDS,
    ContractError,
    SERVICES,
    attach_integrity,
    fixture_signature_verifier,
    fixture_signer,
    sha256_digest,
    validate_eligibility_receipt,
    validate_manifest,
)


NOW = datetime(2026, 8, 12, 16, 0, tzinfo=UTC)
PRODUCER = "github.com/LEAF-Solar-Design/leaf-web-demo/.github/workflows/build-platform-images.yml"
VERIFIER = "v1"
TOPOLOGY = "v1"


def manifest() -> dict:
    unsigned = {
        "schema": "platform-qualification-manifest.v1",
        "repository": "LEAF-Solar-Design/leaf-web-demo",
        "source_commit": "a" * 40,
        "source_tree": "b" * 40,
        "services": [
            {
                "name": name,
                "image_digest": "sha256:" + format(index, "064x"),
                "provenance": "adopted" if index % 2 else "full_build",
                "entrypoint": ["/leaf", name],
            }
            for index, name in enumerate(SERVICES, start=1)
        ],
        "config_contract_digest": "sha256:" + "c" * 64,
        "image_aliases": ["prod-discovery-only"],
        "producer": {
            "identity": PRODUCER,
            "workflow": ".github/workflows/build-platform-images.yml",
            "run_id": 123456,
            "attempt": 2,
        },
        "deployment_identity": {
            "schema": "leaf.deployment-identity.v1",
            "value": "sha256:" + "d" * 64,
        },
        "supported_deployment_path": "both",
        "verifier_version": VERIFIER,
        "topology_version": TOPOLOGY,
    }
    return attach_integrity(unsigned, fixture_signer)


def receipt(bound_manifest: dict | None = None) -> dict:
    bound_manifest = bound_manifest or manifest()
    unsigned = {
        "schema": "leaf.platform-semantic-eligibility.v1",
        "manifest_digest": bound_manifest["payload_digest"],
        "producer_identity": PRODUCER,
        "assertions": [
            {
                "id": assertion_id,
                "result": True,
                "evidence_digest": sha256_digest({"assertion": assertion_id}),
            }
            for assertion_id in ASSERTION_IDS
        ],
        "verifier_version": VERIFIER,
        "topology_version": TOPOLOGY,
        "rollback_result": {
            "restored": True,
            "images_rebuilt": False,
            "service_definitions_mutated": False,
        },
        "cleanup_census_digest": "sha256:" + "e" * 64,
        "issued_at": NOW.isoformat().replace("+00:00", "Z"),
        "expires_at": (NOW + timedelta(minutes=15)).isoformat().replace("+00:00", "Z"),
    }
    return attach_integrity(unsigned, fixture_signer)


def validate_manifest_fixture(value: dict, **overrides):
    return validate_manifest(
        value,
        expected_producer=overrides.get("producer", PRODUCER),
        expected_verifier_version=overrides.get("verifier", VERIFIER),
        expected_topology_version=overrides.get("topology", TOPOLOGY),
        signature_verifier=overrides.get("signature_verifier", fixture_signature_verifier),
    )


def test_mixed_provenance_manifest_round_trips_with_digest_only_images():
    value = validate_manifest_fixture(manifest())

    assert {entry["provenance"] for entry in value["services"]} == {"adopted", "full_build"}
    assert all(entry["image_digest"].startswith("sha256:") for entry in value["services"])
    assert value["image_aliases"] == ["prod-discovery-only"]


@pytest.mark.parametrize(
    ("deployment_path", "provenance"),
    [
        ("adopted_supply", "full_build"),
        ("full_build", "adopted"),
        ("both", "adopted"),
        ("both", "full_build"),
    ],
)
def test_deployment_path_and_service_provenance_cannot_be_confused(deployment_path, provenance):
    value = manifest()
    value["supported_deployment_path"] = deployment_path
    for service in value["services"]:
        service["provenance"] = provenance
    value.pop("signature")
    value.pop("payload_digest")
    value = attach_integrity(value, fixture_signer)

    with pytest.raises(ContractError, match="SCHEMA_INVALID"):
        validate_manifest_fixture(value)


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda value: value["services"][0].update(image_digest="prod-latest"), "SCHEMA_INVALID"),
        (lambda value: value["services"].pop(), "SCHEMA_INVALID"),
        (lambda value: value["services"][1].update(name="app"), "SERVICE_SET_INVALID"),
        (lambda value: value.update(extra="field"), "SCHEMA_INVALID"),
        (lambda value: value["producer"].update(authorization="Bearer abcdefghijklmnopqrstuvwxyz"), "SCHEMA_INVALID"),
    ],
)
def test_manifest_negative_controls_fail_closed(mutation, code):
    value = manifest()
    mutation(value)

    with pytest.raises(ContractError, match=code):
        validate_manifest_fixture(value)


def test_unsigned_and_unconfigured_manifest_consumption_fail_closed():
    unsigned = manifest()
    unsigned.pop("signature")
    unsigned.pop("payload_digest")
    with pytest.raises(ContractError, match="SCHEMA_INVALID"):
        validate_manifest_fixture(unsigned)
    with pytest.raises(ContractError, match="UNCONFIGURED"):
        validate_manifest_fixture(manifest(), signature_verifier=None)


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"producer": "wrong-producer"}, "PRODUCER_MISMATCH"),
        ({"verifier": "v2"}, "VERIFIER_VERSION_MISMATCH"),
        ({"topology": "v2"}, "TOPOLOGY_VERSION_MISMATCH"),
        ({"signature_verifier": lambda _signature, _digest: False}, "SIGNATURE_INVALID"),
    ],
)
def test_manifest_pins_producer_versions_and_signature(overrides, code):
    with pytest.raises(ContractError, match=code):
        validate_manifest_fixture(manifest(), **overrides)


def test_eligibility_receipt_round_trips_and_binds_every_assertion():
    bound_manifest = validate_manifest_fixture(manifest())
    value = validate_eligibility_receipt(
        receipt(bound_manifest),
        manifest_digest=bound_manifest["payload_digest"],
        expected_producer=PRODUCER,
        expected_verifier_version=VERIFIER,
        expected_topology_version=TOPOLOGY,
        signature_verifier=fixture_signature_verifier,
        now=NOW,
    )

    assert tuple(sorted(entry["id"] for entry in value["assertions"])) == tuple(sorted(ASSERTION_IDS))
    assert all(entry["result"] is True for entry in value["assertions"])


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda value: value["assertions"].pop(), "SCHEMA_INVALID"),
        (lambda value: value["assertions"][1].update(id=value["assertions"][0]["id"]), "ASSERTION_SET_INVALID"),
        (lambda value: value.update(extra="retained"), "SCHEMA_INVALID"),
        (lambda value: value["rollback_result"].update(images_rebuilt=True), "SCHEMA_INVALID"),
        (lambda value: value["signature"].update(value="fixture:" + "0" * 64), "PAYLOAD_DIGEST_MISMATCH"),
    ],
)
def test_receipt_negative_controls_fail_closed(mutation, code):
    bound_manifest = manifest()
    value = receipt(bound_manifest)
    mutation(value)
    with pytest.raises(ContractError, match=code):
        validate_eligibility_receipt(
            value,
            manifest_digest=bound_manifest["payload_digest"],
            expected_producer=PRODUCER,
            expected_verifier_version=VERIFIER,
            expected_topology_version=TOPOLOGY,
            signature_verifier=fixture_signature_verifier,
            now=NOW,
        )


def test_expired_wrong_producer_verifier_topology_and_manifest_fail_closed():
    bound_manifest = manifest()
    value = receipt(bound_manifest)
    checks = [
        ({"manifest_digest": "sha256:" + "0" * 64}, PRODUCER, VERIFIER, TOPOLOGY, NOW, "MANIFEST_MISMATCH"),
        ({}, "wrong", VERIFIER, TOPOLOGY, NOW, "PRODUCER_MISMATCH"),
        ({}, PRODUCER, "v2", TOPOLOGY, NOW, "VERIFIER_VERSION_MISMATCH"),
        ({}, PRODUCER, VERIFIER, "v2", NOW, "TOPOLOGY_VERSION_MISMATCH"),
        ({}, PRODUCER, VERIFIER, TOPOLOGY, NOW + timedelta(hours=1), "RECEIPT_EXPIRED"),
    ]
    for fields, producer, verifier, topology, observed, code in checks:
        with pytest.raises(ContractError, match=code):
            validate_eligibility_receipt(
                value,
                manifest_digest=fields.get("manifest_digest", bound_manifest["payload_digest"]),
                expected_producer=producer,
                expected_verifier_version=verifier,
                expected_topology_version=topology,
                signature_verifier=fixture_signature_verifier,
                now=observed,
            )


@pytest.mark.parametrize(
    "secret_value",
    [
        "Bearer abcdefghijklmnopqrstuvwxyz",
        "eyJhbGciOiJSUzI1NiJ9.abcdefghijklmnop.abcdefghijklmnop",
        "-----BEGIN PRIVATE KEY-----\nredacted",
        "AKIAABCDEFGHIJKLMNOP",
    ],
)
def test_secret_shaped_values_are_rejected_without_reflection(secret_value):
    value = manifest()
    value["image_aliases"] = [secret_value]
    # Restore the integrity fields so the secret screen, not a stale digest, owns the refusal.
    value.pop("signature")
    value.pop("payload_digest")
    value = attach_integrity(value, fixture_signer)
    with pytest.raises(ContractError) as error:
        validate_manifest_fixture(value)
    assert error.value.code == "SECRET_MATERIAL"
    assert secret_value not in str(error.value)
