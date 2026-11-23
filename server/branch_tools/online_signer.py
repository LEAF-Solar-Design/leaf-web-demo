"""Canonical online signing for tenant-bound envelopes and revocations.

Key material stays in KMS in production. LocalKeySigner is a test adapter.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from typing import Protocol
from uuid import UUID

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from tool_envelopes import valid_identity

_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_ENV = "LEAF_TOOL_SIGNER_KMS_KEY_ID"


class SignerUnavailable(RuntimeError):
    """The configured signing service could not safely sign the document."""


class Signer(Protocol):
    key_id: str

    def sign(self, message: bytes) -> bytes: ...


def key_id_for_spki(spki_der: bytes) -> str:
    return hashlib.sha256(spki_der).hexdigest()[:16]


def canonical_json(obj: object) -> bytes:
    """Port Package/CanonicalJson.cs's integer-only JSON domain and escaping."""
    def validate(value):
        if value is None or type(value) in (bool, int):
            return
        if isinstance(value, str):
            # Unpaired surrogates are not Unicode scalar values or valid UTF-8.
            try:
                value.encode("utf-8")
            except UnicodeError:
                raise ValueError("invalid Unicode string") from None
            return
        if isinstance(value, list):
            for item in value:
                validate(item)
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("JSON object keys must be strings")
                validate(key)
                validate(item)
            return
        raise ValueError("non-canonical JSON type")

    validate(obj)
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def strict_der(sig: bytes) -> bytes:
    """Require positive, in-range scalars and minimal DER accepted by EcdsaDer."""
    if not isinstance(sig, bytes) or not 8 <= len(sig) <= 72:
        raise ValueError("invalid ECDSA signature")
    try:
        r, s = utils.decode_dss_signature(sig)
    except ValueError:
        raise ValueError("invalid ECDSA DER") from None
    if not (0 < r < _ORDER and 0 < s < _ORDER):
        raise ValueError("invalid P-256 scalar")
    return utils.encode_dss_signature(r, s)


def _p256(key):
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("signer must use P-256")
    return key


class LocalKeySigner:
    def __init__(self, private_key):
        if not isinstance(private_key, ec.EllipticCurvePrivateKey):
            raise ValueError("signer must use a P-256 private key")
        public_key = _p256(private_key.public_key())
        self._private_key = private_key
        self.key_id = key_id_for_spki(public_key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))

    def sign(self, message: bytes) -> bytes:
        return strict_der(self._private_key.sign(message, ec.ECDSA(hashes.SHA256())))


class KmsSigner:
    def __init__(self, kms_key_id, client):
        self._kms_key_id = kms_key_id
        self._client = client
        try:
            response = client.get_public_key(KeyId=kms_key_id)
            spki = response["PublicKey"]
            _p256(serialization.load_der_public_key(spki))
            self.key_id = key_id_for_spki(spki)
        except Exception:
            raise SignerUnavailable("signer public key unavailable") from None

    def sign(self, message: bytes) -> bytes:
        try:
            response = self._client.sign(KeyId=self._kms_key_id, Message=message,
                                         MessageType="RAW", SigningAlgorithm="ECDSA_SHA_256")
            return strict_der(response["Signature"])
        except Exception:
            raise SignerUnavailable("signing unavailable") from None


def _matches(pattern, value):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _timestamp(value):
    if not _matches(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value):
        raise ValueError("timestamp must be whole-second UTC")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValueError("invalid timestamp") from None


def _package(name, version, package_digest):
    if not (isinstance(name, str) and isinstance(version, str) and valid_identity(name, version)):
        raise ValueError("invalid package identity")
    if not _matches(r"[0-9a-f]{64}", package_digest):
        raise ValueError("invalid package digest")
    return {"name": name, "version": version, "package_digest": package_digest}


def _signed(signer, document):
    if not _matches(r"[0-9a-f]{16}", signer.key_id):
        raise ValueError("invalid signer key ID")
    document.update(key_id=signer.key_id, alg="ECDSA_P256_SHA256")
    document["signature"] = strict_der(signer.sign(canonical_json(document))).hex()
    return canonical_json(document)


def sign_envelope_v2(signer, *, name, version, package_digest, tiers, audience_tenant, signed_at) -> bytes:
    package = _package(name, version, package_digest)
    if not (isinstance(tiers, list) and tiers
            and all(_matches(r"[a-z][a-z0-9_]{1,40}", tier) for tier in tiers)
            and len(set(tiers)) == len(tiers)):
        raise ValueError("invalid tiers")
    try:
        if not isinstance(audience_tenant, str) or str(UUID(audience_tenant)) != audience_tenant:
            raise ValueError("invalid audience tenant")
    except ValueError:
        raise ValueError("invalid audience tenant") from None
    _timestamp(signed_at)
    return _signed(signer, {"schema": "leaf.tool-envelope.v2", "package": package,
                            "tiers": list(tiers), "audience_tenant": audience_tenant,
                            "signed_at": signed_at})


def sign_revocations(signer, *, entries, issued_at) -> bytes:
    _timestamp(issued_at)
    if not isinstance(entries, list):
        raise ValueError("invalid revocation entries")
    copied = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
                "name", "version", "package_digest", "reason", "revoked_at"}:
            raise ValueError("invalid revocation entry fields")
        item = _package(entry["name"], entry["version"], entry["package_digest"])
        if not isinstance(entry["reason"], str):
            raise ValueError("invalid revocation reason")
        _timestamp(entry["revoked_at"])
        item.update(reason=entry["reason"], revoked_at=entry["revoked_at"])
        copied.append(item)
    return _signed(signer, {"schema": "leaf.tool-revocations.v1", "entries": copied,
                            "issued_at": issued_at})


def configured_signer() -> Signer | None:
    key = os.environ.get(_ENV)
    if not key:
        return None
    try:
        import boto3
        from botocore.config import Config

        client = boto3.client("kms", config=Config(connect_timeout=5, read_timeout=10,
                              retries={"mode": "standard", "max_attempts": 2}))
        return KmsSigner(key, client)
    except Exception:
        raise SignerUnavailable("signer configuration unavailable") from None
