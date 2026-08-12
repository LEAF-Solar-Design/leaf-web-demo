"""Strict, dormant contracts for Leaf semantic qualification.

This module has no network, cloud, deployment, or selector side effects.  A caller
must inject a signature verifier.  Without one, consumption fails closed with
``UNCONFIGURED``.  That keeps S1 useful for fixtures while making it impossible
for the first slice to grant deployment authority.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import sys
import sysconfig
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_SCHEMA_PATH = ROOT / "contract" / "platform-qualification-manifest.v1.schema.json"
ELIGIBILITY_SCHEMA_PATH = ROOT / "contract" / "platform-semantic-eligibility.v1.schema.json"

SERVICES = ("app", "broker", "canonical-worker", "harness", "web")
ASSERTION_IDS = (
    "deployment_identity",
    "app_to_harness_auth",
    "removal_worker_fence",
    "ordinary_authoring_recovery",
    "publication_terminal_state",
    "closed_projection",
    "tenant_upload_isolation",
    "rollback_cleanup",
)
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
JWT_RE = re.compile(r"^[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}$")
BEARER_RE = re.compile(r"(?i)(?:authorization\s*[:=]\s*)?bearer\s+[A-Za-z0-9._~+/-]{12,}")
AWS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
PEM_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE)-----")
SECRET_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "refreshtoken",
    "refresh_token",
    "oauth_token",
    "client_secret",
    "authorization",
    "password",
    "private_key",
    "secret",
    "cookie",
}
HIGH_ENTROPY_EXEMPT_KEYS = {
    "payload_digest",
    "signed_payload_digest",
    "image_digest",
    "config_contract_digest",
    "manifest_digest",
    "cleanup_census_digest",
    "evidence_digest",
    "source_commit",
    "source_tree",
    "deployment_identity",
    "value",
    "signature",
}


class ContractError(ValueError):
    """A sanitized, machine-classified contract refusal."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic JSON bytes, rejecting non-JSON numeric values."""

    def reject_constant(_value: str) -> None:
        raise ContractError("NON_CANONICAL_JSON")

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        json.loads(encoded, parse_constant=reject_constant)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ContractError("NON_CANONICAL_JSON") from exc
    return encoded


def sha256_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def signed_content_digest(document: Mapping[str, Any]) -> str:
    payload = {key: deepcopy(value) for key, value in document.items() if key not in {"signature", "payload_digest"}}
    return sha256_digest(payload)


def envelope_digest(document: Mapping[str, Any]) -> str:
    payload = {key: deepcopy(value) for key, value in document.items() if key != "payload_digest"}
    return sha256_digest(payload)


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _looks_high_entropy(value: str) -> bool:
    # Opaque credentials use a compact token alphabet.  Paths, URLs, workflow
    # identities, and ordinary prose may also have varied characters, but are
    # not opaque tokens and must not be rejected merely for being long.
    if (
        len(value) < 40
        or " " in value
        or SHA256_RE.fullmatch(value)
        or re.fullmatch(r"[A-Za-z0-9_+/=-]{40,}", value) is None
    ):
        return False
    alphabet = set(value)
    if len(alphabet) < 16:
        return False
    counts = {character: value.count(character) for character in alphabet}
    entropy = -sum((count / len(value)) * math.log2(count / len(value)) for count in counts.values())
    return entropy >= 4.0


def reject_secret_material(value: Any, *, key: str = "") -> None:
    """Reject high-confidence credential material without reflecting its value."""

    normalized = _normalized_key(key)
    if normalized in SECRET_KEYS:
        raise ContractError("SECRET_MATERIAL")
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            if not isinstance(child_key, str):
                raise ContractError("INVALID_KEY")
            reject_secret_material(child, key=child_key)
        return
    if isinstance(value, list):
        for child in value:
            reject_secret_material(child, key=key)
        return
    if not isinstance(value, str):
        return
    if BEARER_RE.search(value) or JWT_RE.fullmatch(value) or AWS_KEY_RE.search(value) or PEM_RE.search(value):
        raise ContractError("SECRET_MATERIAL")
    if normalized not in HIGH_ENTROPY_EXEMPT_KEYS and _looks_high_entropy(value):
        raise ContractError("SECRET_MATERIAL")


def _load_schema(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("SCHEMA_UNAVAILABLE") from exc
    if not isinstance(value, dict):
        raise ContractError("SCHEMA_UNAVAILABLE")
    return value


def _validate_schema(document: Mapping[str, Any], path: Path) -> None:
    # This repository has a top-level ``platform/`` package.  Pytest adds the
    # repository root to sys.path, so attrs/jsonschema can otherwise import that
    # package instead of Python's stdlib ``platform`` module.  Cache the stdlib
    # module by its absolute stdlib path before importing jsonschema.  This is
    # the same composition seam used by server/deps.py.
    loaded_platform = sys.modules.get("platform")
    if loaded_platform is None or not hasattr(loaded_platform, "python_implementation"):
        stdlib_platform = Path(sysconfig.get_path("stdlib")) / "platform.py"
        specification = importlib.util.spec_from_file_location("platform", stdlib_platform)
        if specification is None or specification.loader is None:
            raise ContractError("VALIDATOR_UNAVAILABLE")
        module = importlib.util.module_from_spec(specification)
        sys.modules["platform"] = module
        specification.loader.exec_module(module)
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError as exc:
        raise ContractError("VALIDATOR_UNAVAILABLE") from exc
    validator = Draft202012Validator(_load_schema(path), format_checker=FormatChecker())
    if next(validator.iter_errors(document), None) is not None:
        raise ContractError("SCHEMA_INVALID")


SignatureVerifier = Callable[[Mapping[str, Any], str], bool]
SignatureBuilder = Callable[[str], Mapping[str, Any]]


def attach_integrity(document: Mapping[str, Any], signer: SignatureBuilder) -> dict[str, Any]:
    """Attach an external signature envelope and final content address."""

    value = deepcopy(dict(document))
    if "signature" in value or "payload_digest" in value:
        raise ContractError("INTEGRITY_ALREADY_PRESENT")
    signed_digest = signed_content_digest(value)
    signature = dict(signer(signed_digest))
    if signature.get("signed_payload_digest") != signed_digest:
        raise ContractError("SIGNATURE_DIGEST_MISMATCH")
    value["signature"] = signature
    value["payload_digest"] = envelope_digest(value)
    return value


def _validate_integrity(document: Mapping[str, Any], verifier: SignatureVerifier | None) -> None:
    if verifier is None:
        raise ContractError("UNCONFIGURED")
    signature = document.get("signature")
    if not isinstance(signature, Mapping):
        raise ContractError("SIGNATURE_INVALID")
    signed_digest = signed_content_digest(document)
    if signature.get("signed_payload_digest") != signed_digest:
        raise ContractError("SIGNATURE_DIGEST_MISMATCH")
    if document.get("payload_digest") != envelope_digest(document):
        raise ContractError("PAYLOAD_DIGEST_MISMATCH")
    try:
        verified = verifier(signature, signed_digest)
    except Exception as exc:
        raise ContractError("SIGNATURE_INVALID") from exc
    if verified is not True:
        raise ContractError("SIGNATURE_INVALID")


def validate_manifest(
    document: Mapping[str, Any],
    *,
    expected_producer: str,
    expected_verifier_version: str,
    expected_topology_version: str,
    signature_verifier: SignatureVerifier | None,
) -> dict[str, Any]:
    value = deepcopy(dict(document))
    _validate_schema(value, MANIFEST_SCHEMA_PATH)
    reject_secret_material(value)
    service_names = [entry.get("name") for entry in value["services"]]
    if tuple(sorted(service_names)) != tuple(sorted(SERVICES)) or len(set(service_names)) != len(SERVICES):
        raise ContractError("SERVICE_SET_INVALID")
    provenances = {entry["provenance"] for entry in value["services"]}
    expected_provenances = {
        "adopted_supply": {"adopted"},
        "full_build": {"full_build"},
        "both": {"adopted", "full_build"},
    }[value["supported_deployment_path"]]
    if provenances != expected_provenances:
        raise ContractError("PROVENANCE_PATH_MISMATCH")
    if value["producer"]["identity"] != expected_producer:
        raise ContractError("PRODUCER_MISMATCH")
    if value["verifier_version"] != expected_verifier_version:
        raise ContractError("VERIFIER_VERSION_MISMATCH")
    if value["topology_version"] != expected_topology_version:
        raise ContractError("TOPOLOGY_VERSION_MISMATCH")
    if not value["deployment_identity"]["value"]:
        raise ContractError("DEPLOYMENT_IDENTITY_MISSING")
    _validate_integrity(value, signature_verifier)
    return value


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError("TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None:
        raise ContractError("TIMESTAMP_INVALID")
    return parsed.astimezone(UTC)


def validate_eligibility_receipt(
    document: Mapping[str, Any],
    *,
    manifest_digest: str,
    expected_producer: str,
    expected_verifier_version: str,
    expected_topology_version: str,
    signature_verifier: SignatureVerifier | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    value = deepcopy(dict(document))
    _validate_schema(value, ELIGIBILITY_SCHEMA_PATH)
    reject_secret_material(value)
    if value["manifest_digest"] != manifest_digest:
        raise ContractError("MANIFEST_MISMATCH")
    if value["producer_identity"] != expected_producer:
        raise ContractError("PRODUCER_MISMATCH")
    if value["verifier_version"] != expected_verifier_version:
        raise ContractError("VERIFIER_VERSION_MISMATCH")
    if value["topology_version"] != expected_topology_version:
        raise ContractError("TOPOLOGY_VERSION_MISMATCH")
    assertion_ids = [entry.get("id") for entry in value["assertions"]]
    if tuple(sorted(assertion_ids)) != tuple(sorted(ASSERTION_IDS)) or len(set(assertion_ids)) != len(ASSERTION_IDS):
        raise ContractError("ASSERTION_SET_INVALID")
    issued_at = _parse_timestamp(value["issued_at"])
    expires_at = _parse_timestamp(value["expires_at"])
    observed = (now or datetime.now(UTC)).astimezone(UTC)
    if expires_at <= issued_at or observed < issued_at or observed >= expires_at:
        raise ContractError("RECEIPT_EXPIRED")
    _validate_integrity(value, signature_verifier)
    return value


def fixture_signer(signed_digest: str) -> Mapping[str, Any]:
    """Deterministic test-only signer. Production consumers never trust it by default."""

    value = hashlib.sha256(("leaf-fixture-signature-v1:" + signed_digest).encode()).hexdigest()
    return {
        "algorithm": "external-v1",
        "key_id": "fixture-only",
        "signed_payload_digest": signed_digest,
        "value": "fixture:" + value,
    }


def fixture_signature_verifier(signature: Mapping[str, Any], signed_digest: str) -> bool:
    return dict(signature) == dict(fixture_signer(signed_digest))


__all__ = [
    "ASSERTION_IDS",
    "ContractError",
    "SERVICES",
    "attach_integrity",
    "canonical_json_bytes",
    "envelope_digest",
    "fixture_signature_verifier",
    "fixture_signer",
    "reject_secret_material",
    "sha256_digest",
    "validate_eligibility_receipt",
    "validate_manifest",
]
