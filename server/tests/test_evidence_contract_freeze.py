"""Structural freeze gate for contract/EVIDENCE.md.

This suite is deliberately stdlib-only apart from pytest's test runner. It
validates the two v1 wire shapes without importing the platform package, which
would shadow Python's stdlib ``platform`` module from the repository root.
"""
from __future__ import annotations

import ast
import base64
import copy
import math
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


SERVER_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SERVER_DIR.parent
CONTRACT = REPO_ROOT / "contract" / "EVIDENCE.md"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
UUID = "123e4567-e89b-12d3-a456-426614174000"
ROOT = "a" * 64

MANIFEST_KEYS = {"bundleVersion", "algorithm", "metadata", "entries", "rootSha256"}
ENTRY_KEYS = {"path", "size", "sha256"}
PAYLOAD_KEYS = {"signatureContract", "bundleId", "rootSha256", "credentialId", "signedAt"}
RECORD_KEYS = {
    "signature_id", "history_operation_id", "bundle_id", "credential_id",
    "root_sha256", "signature_algorithm", "signature_base64", "signed_payload",
}


def _exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    assert isinstance(value, dict), f"{label} must be an object"
    assert set(value) == expected, f"{label} keys drifted: {sorted(set(value) ^ expected)}"
    return value


def _sha256(value: Any, label: str) -> None:
    assert isinstance(value, str) and SHA256.fullmatch(value), f"{label} must be lowercase SHA-256"


def _uuid(value: Any, label: str) -> None:
    assert isinstance(value, str), f"{label} must be a UUID string"
    parsed = uuid.UUID(value)
    assert str(parsed) == value, f"{label} must be canonical hyphenated UUID text"


def _timestamp(value: Any, label: str) -> None:
    assert isinstance(value, str), f"{label} must be an ISO 8601 timestamp"
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None and parsed.utcoffset() is not None, (
        f"{label} must carry an explicit UTC offset")


def _json_value(value: Any) -> bool:
    if value is None or isinstance(value, (str, int, bool)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_value(item) for key, item in value.items())
    return False


def _validate_manifest(manifest: Any) -> None:
    manifest = _exact_keys(manifest, MANIFEST_KEYS, "manifest")
    assert manifest["bundleVersion"] == "leaf.evidence.v1"
    assert manifest["algorithm"] == "sha256-merkle-v1"
    metadata = manifest["metadata"]
    assert isinstance(metadata, dict) and metadata and _json_value(metadata), (
        "metadata must be a non-empty JSON object")
    entries = manifest["entries"]
    assert isinstance(entries, list) and entries, "entries must be a non-empty array"
    paths: list[str] = []
    for entry in entries:
        entry = _exact_keys(entry, ENTRY_KEYS, "entry")
        path = entry["path"]
        assert isinstance(path, str) and path and not path.startswith("/")
        assert ".." not in path.split("/"), "entry path traversal is forbidden"
        size = entry["size"]
        assert isinstance(size, int) and not isinstance(size, bool) and size >= 0
        _sha256(entry["sha256"], "entry.sha256")
        paths.append(path)
    assert paths == sorted(paths) and len(paths) == len(set(paths)), (
        "entry paths must be unique and sorted")
    _sha256(manifest["rootSha256"], "rootSha256")


def _validate_payload(payload: Any) -> None:
    payload = _exact_keys(payload, PAYLOAD_KEYS, "signed payload")
    assert payload["signatureContract"] == "leaf.review-signature.v1"
    _uuid(payload["bundleId"], "bundleId")
    _sha256(payload["rootSha256"], "rootSha256")
    _uuid(payload["credentialId"], "credentialId")
    _timestamp(payload["signedAt"], "signedAt")


def _validate_record(record: Any) -> None:
    record = _exact_keys(record, RECORD_KEYS, "signature record")
    for field in ("signature_id", "history_operation_id", "bundle_id", "credential_id"):
        _uuid(record[field], field)
    _sha256(record["root_sha256"], "root_sha256")
    assert record["signature_algorithm"] in {"ed25519", "ecdsa-p256-sha256"}
    assert isinstance(record["signature_base64"], str) and record["signature_base64"]
    base64.b64decode(record["signature_base64"], validate=True)
    _validate_payload(record["signed_payload"])
    assert record["bundle_id"] == record["signed_payload"]["bundleId"]
    assert record["credential_id"] == record["signed_payload"]["credentialId"]
    assert record["root_sha256"] == record["signed_payload"]["rootSha256"]


def _fixtures() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    manifest = {
        "bundleVersion": "leaf.evidence.v1",
        "algorithm": "sha256-merkle-v1",
        "metadata": {"projectId": UUID},
        "entries": [{"path": "records/solve.json", "size": 2, "sha256": "b" * 64}],
        "rootSha256": ROOT,
    }
    payload = {
        "signatureContract": "leaf.review-signature.v1", "bundleId": UUID,
        "rootSha256": ROOT, "credentialId": "123e4567-e89b-12d3-a456-426614174001",
        "signedAt": "2026-07-23T12:34:56+00:00",
    }
    record = {
        "signature_id": "123e4567-e89b-12d3-a456-426614174002",
        "history_operation_id": "123e4567-e89b-12d3-a456-426614174003",
        "bundle_id": UUID, "credential_id": payload["credentialId"],
        "root_sha256": ROOT, "signature_algorithm": "ed25519",
        "signature_base64": "c2lnbmF0dXJl", "signed_payload": payload,
    }
    return manifest, payload, record


def _reject(fn, value: Any) -> None:
    try:
        fn(value)
    except (AssertionError, TypeError, ValueError):
        return
    raise AssertionError("invalid contract shape was accepted")


def test_valid_evidence_and_signature_fixtures_pass_structural_validation():
    manifest, payload, record = _fixtures()
    _validate_manifest(manifest)
    _validate_payload(payload)
    _validate_record(record)


def test_every_required_field_omission_rejects():
    manifest, payload, record = _fixtures()
    cases = [(_validate_manifest, manifest, key) for key in MANIFEST_KEYS]
    cases += [(_validate_manifest, manifest, f"entries.0.{key}") for key in ENTRY_KEYS]
    cases += [(_validate_payload, payload, key) for key in PAYLOAD_KEYS]
    cases += [(_validate_record, record, key) for key in RECORD_KEYS]
    for validator, fixture, field in cases:
        candidate = copy.deepcopy(fixture)
        target = candidate["entries"][0] if field.startswith("entries.") else candidate
        target.pop(field.rsplit(".", 1)[-1])
        _reject(validator, candidate)


def test_every_field_type_violation_rejects():
    manifest, payload, record = _fixtures()
    cases = [(_validate_manifest, manifest, key, None) for key in MANIFEST_KEYS]
    cases += [(_validate_manifest, manifest, key, "entries") for key in ENTRY_KEYS]
    cases += [(_validate_payload, payload, key, None) for key in PAYLOAD_KEYS]
    cases += [(_validate_record, record, key, None) for key in RECORD_KEYS]
    for validator, fixture, field, container in cases:
        candidate = copy.deepcopy(fixture)
        target = candidate[container][0] if container else candidate
        target[field] = []
        _reject(validator, candidate)


def test_unknown_fields_reject_at_each_contract_object_level():
    manifest, payload, record = _fixtures()
    for validator, fixture, path in (
        (_validate_manifest, manifest, None),
        (_validate_manifest, manifest, "entries"),
        (_validate_payload, payload, None),
        (_validate_record, record, None),
    ):
        candidate = copy.deepcopy(fixture)
        target = candidate[path][0] if path else candidate
        target["unpromoted"] = True
        _reject(validator, candidate)


def test_signature_checks_are_structural_not_live_crypto():
    _, _, record = _fixtures()
    _validate_record(record)
    # This shape gate intentionally has no private key, network client, KMS
    # call, or signature verification. It only proves the record is well formed.
    assert base64.b64decode(record["signature_base64"], validate=True) == b"signature"


def test_contract_document_freezes_fields_version_law_and_kms_boundary():
    contract = CONTRACT.read_text(encoding="utf-8")
    assert "Status: **FROZEN**" in contract
    assert "KMS infrastructure is out of scope" in contract
    assert "unknown field" in contract and "incompatible change requires a new contract identifier" in contract
    for field in MANIFEST_KEYS | ENTRY_KEYS | PAYLOAD_KEYS | RECORD_KEYS:
        assert f"`{field}`" in contract, f"contract field missing: {field}"


def test_contract_and_shipped_code_name_the_same_frozen_identifiers_and_canonical_rules():
    contract = CONTRACT.read_text(encoding="utf-8")
    evidence_source = (REPO_ROOT / "platform" / "evidence.py").read_text(encoding="utf-8")
    signing_source = (REPO_ROOT / "platform" / "signing.py").read_text(encoding="utf-8")
    for token in ("leaf.evidence.v1", "sha256-merkle-v1", "leaf.review-signature.v1",
                  "sort_keys=True", 'separators=(",", ":")', "ensure_ascii=False",
                  "allow_nan=False"):
        assert token in contract or token in evidence_source
    assert 'BUNDLE_VERSION = "leaf.evidence.v1"' in evidence_source
    assert 'SIGNATURE_CONTRACT = "leaf.review-signature.v1"' in signing_source
    assert "sort_keys=True" in evidence_source and "ensure_ascii=False" in evidence_source
    assert "allow_nan=False" in evidence_source

    evidence_tree = ast.parse(evidence_source)
    manifest_keys = set()
    for node in ast.walk(evidence_tree):
        if isinstance(node, ast.Dict):
            keys = {key.value for key in node.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)}
            if {"bundleVersion", "entries"} <= keys:
                manifest_keys = keys
                break
    assert manifest_keys == MANIFEST_KEYS - {"rootSha256"}
    assert 'manifest["rootSha256"] = _root(entries, manifest["metadata"])' in evidence_source

    tree = ast.parse(signing_source)
    payload_keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = {key.value for key in node.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)}
            if "signatureContract" in keys:
                payload_keys = keys
                break
    assert payload_keys == PAYLOAD_KEYS
