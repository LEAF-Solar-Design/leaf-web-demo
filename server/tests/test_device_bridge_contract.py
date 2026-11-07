"""Freeze the device bridge shapes, fixtures, and shared Ed25519 wire vector."""
from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jsonschema import Draft202012Validator, FormatChecker
import pytest


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_DIR = ROOT / "contract"
SCHEMA = json.loads(
    (CONTRACT_DIR / "device-bridge.v1.schema.json").read_text(encoding="utf-8")
)
DOCUMENT = (CONTRACT_DIR / "DEVICE-BRIDGE.md").read_text(encoding="utf-8")
FIXTURES = CONTRACT_DIR / "fixtures" / "device-bridge"
VALID = json.loads((FIXTURES / "valid.json").read_text(encoding="utf-8"))["cases"]
INVALID = json.loads((FIXTURES / "invalid.json").read_text(encoding="utf-8"))["cases"]
FORMAT_CHECKER = FormatChecker()


@FORMAT_CHECKER.checks("date-time", raises=ValueError)
def _is_datetime(value):
    # jsonschema's date-time checker otherwise needs an optional dependency.
    if not isinstance(value, str):
        return True
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})",
        value,
    ):
        return False
    datetime.fromisoformat(value.upper().replace("Z", "+00:00"))
    return True


def _validator(definition: str) -> Draft202012Validator:
    assert definition in SCHEMA["$defs"], definition
    # Retain the root definitions and ID so local refs resolve without a fetch.
    return Draft202012Validator(
        {**SCHEMA, "$ref": f"#/$defs/{definition}"}, format_checker=FORMAT_CHECKER
    )


def _documented_json(label: str) -> dict:
    blocks = re.findall(rf"```{re.escape(label)}\n(.*?)\n```", DOCUMENT, re.DOTALL)
    assert len(blocks) == 1, f"Expected one {label} block"
    return json.loads(blocks[0])


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def test_schema_is_valid_and_object_envelopes_are_closed():
    Draft202012Validator.check_schema(SCHEMA)
    assert SCHEMA["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert SCHEMA["title"] == "leaf.device-bridge.v1"

    def check_objects(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False, node
            for value in node.values():
                check_objects(value)
        elif isinstance(node, list):
            for value in node:
                check_objects(value)

    check_objects(SCHEMA)


def test_all_valid_fixtures_validate():
    assert VALID
    for index, case in enumerate(VALID):
        errors = list(_validator(case["def"]).iter_errors(case["value"]))
        assert not errors, f"Valid case {index} ({case['def']}): {errors}"


def test_all_invalid_fixtures_fail():
    assert len(INVALID) >= 12
    for index, case in enumerate(INVALID):
        assert case["why"].strip()
        errors = list(_validator(case["def"]).iter_errors(case["value"]))
        assert errors, f"Invalid case {index} ({case['def']}): {case['why']}"


def test_fixtures_cover_every_definition_and_command_operation():
    assert {case["def"] for case in VALID} == set(SCHEMA["$defs"])
    operations = set(SCHEMA["$defs"]["Command"]["properties"]["op"]["enum"])
    assert {
        case["value"]["op"] for case in VALID if case["def"] == "Command"
    } == operations


def test_documented_signing_example_matches_cryptography():
    example = _documented_json("signing-example")
    assert example["seed_hex_byte"] == "01"
    assert example["seed_length"] == 32
    key = Ed25519PrivateKey.from_private_bytes(bytes([1]) * 32)
    public_key = key.public_key()
    assert _base64url(
        public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ) == example["public_key"]
    assert example["method"] == example["method"].upper()
    assert _base64url(bytes(range(16))) == example["nonce"]
    body_hash = hashlib.sha256(example["body"].encode("utf-8")).hexdigest()
    assert body_hash == example["body_sha256"]
    signed_string = "\n".join(
        [
            "LEAF-DEVICE-V1",
            example["method"],
            example["path"],
            example["timestamp"],
            example["nonce"],
            body_hash,
        ]
    )
    assert signed_string == example["signed_string"]
    assert not signed_string.endswith("\n")
    signature = key.sign(signed_string.encode("utf-8"))
    assert _base64url(signature) == example["signature"]
    public_key.verify(signature, signed_string.encode("utf-8"))
    # Query bytes are part of the signature, not just the route path.
    with pytest.raises(InvalidSignature):
        public_key.verify(signature, signed_string.replace("wait=25", "wait=24").encode("utf-8"))


def test_document_and_schema_have_identical_operation_allowlists():
    command = SCHEMA["$defs"]["Command"]
    operations = command["properties"]["op"]["enum"]
    documented = re.findall(r"^\| `([a-z]+\.[a-z.]+)` \|", DOCUMENT, re.MULTILINE)
    branches = [branch["properties"]["op"]["const"] for branch in command["oneOf"]]
    assert len(operations) == len(set(operations))
    assert len(documented) == len(operations)
    assert len(branches) == len(operations)
    assert set(documented) == set(branches) == set(operations)


def test_documented_home_assistant_catalog_matches_the_fixture():
    entry = _documented_json("package-catalog-example")
    _validator("PackageCatalogEntry").validate(entry)
    assert entry == next(case["value"] for case in VALID if case["def"] == "PackageCatalogEntry")
    assert entry["package"] == "homeassistant"
    assert entry["port"] == 8123
    assert entry["health_path"] == "/api/"
    assert [(rule["method"], rule["path_prefix"]) for rule in entry["relay_allow"]] == [
        ("GET", "/api/states"),
        ("POST", "/api/services/light/"),
        ("POST", "/api/services/scene/"),
        ("POST", "/api/services/homeassistant/turn_off"),
        ("POST", "/api/services/homeassistant/turn_on"),
    ]


def test_documented_relay_contract_uses_token_bound_device_and_fixed_package_path():
    response = _documented_json("relay-token-example")
    _validator("RelayTokenResponse").validate(response)
    assert response == next(case["value"] for case in VALID if case["def"] == "RelayTokenResponse")
    assert response["path_prefix"] == "/api/device-relay/homeassistant"
    assert "`ANY /api/device-relay/{package}/{path...}`" in DOCUMENT
    assert "/api/device-relay/{device_id}" not in DOCUMENT
    assert "403 `relay_token_mismatch`" in DOCUMENT
    assert "`GET /api/devices/relay-openapi/{package}.json`" in DOCUMENT
    assert "`X-Leaf-Document-Sha256`" in DOCUMENT
    assert "identical UTF-8 JSON" in DOCUMENT
    assert "Relay calls accept and drop an `Idempotency-Key` header" in DOCUMENT
