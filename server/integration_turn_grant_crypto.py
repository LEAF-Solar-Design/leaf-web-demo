"""Lazy KMS envelope encryption; plaintext is exposed only to a trusted closure.

Mutable buffers are scrubbed best-effort. Python/library immutable bytes and
callback-created copies cannot be reliably erased. Never log these values.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from typing import Callable, Protocol, TypeVar
import unicodedata

if __package__:
    from .platform_link import _ensure_platform_package
else:
    from platform_link import _ensure_platform_package

_ensure_platform_package()
from leaf_platform.integration_turn_grant_store import Binding, PURPOSE, SCHEMA

MAX_TOKEN_BYTES = 16384
_ARN = re.compile(r"arn:aws(?:-us-gov|-cn)?:kms:[a-z0-9-]+:[0-9]{12}:key/"
                  r"(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|mrk-[0-9a-f]{32})\Z")


class GrantCryptoError(Exception):
    """Fixed, nonsecret failure code."""


class KeyService(Protocol):
    def canonical_key_arn(self) -> str: ...
    def generate_data_key(self, key_arn: str, context: dict[str, str]) -> tuple[bytearray, bytes]: ...
    def decrypt_data_key(self, key_arn: str, wrapped: bytes, context: dict[str, str]) -> bytearray: ...


class KmsKeyService:
    """No SDK initialization, credentials lookup or network until first use."""
    def __init__(self):
        self._client = None
        self._arn = None

    def canonical_key_arn(self):
        if self._arn is not None:
            return self._arn
        key = os.environ.get("LEAF_INTEGRATION_TURN_GRANTS_KMS_KEY_ID", "")
        region = os.environ.get("AWS_REGION", "")
        if (not re.fullmatch(r"[A-Za-z0-9:/_-]{1,2048}", key)
                or not re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-[0-9]+", region)):
            raise GrantCryptoError("GRANT_CRYPTO_CONFIG") from None
        try:
            import boto3
            from botocore.config import Config
            client = boto3.client("kms", region_name=region, config=Config(
                connect_timeout=1, read_timeout=1, retries={"total_max_attempts": 1, "mode": "standard"}))
            metadata = client.describe_key(KeyId=key)["KeyMetadata"]
            arn = metadata["Arn"]
            if (not _ARN.fullmatch(arn) or metadata.get("Enabled") is not True
                    or metadata.get("KeyState") != "Enabled"
                    or metadata.get("KeySpec") != "SYMMETRIC_DEFAULT"
                    or metadata.get("KeyUsage") != "ENCRYPT_DECRYPT"):
                raise ValueError()
            self._client, self._arn = client, arn
            return arn
        except Exception:
            raise GrantCryptoError("GRANT_CRYPTO_UNAVAILABLE") from None

    def generate_data_key(self, key_arn, context):
        if key_arn != self.canonical_key_arn():
            raise GrantCryptoError("GRANT_CRYPTO_INVALID")
        response = self._client.generate_data_key(KeyId=key_arn, KeySpec="AES_256", EncryptionContext=context)
        key = bytearray(response.pop("Plaintext"))
        if response.get("KeyId") != key_arn or type(response.get("CiphertextBlob")) is not bytes:
            _scrub(key)
            raise GrantCryptoError("GRANT_CRYPTO_INVALID")
        return key, response["CiphertextBlob"]

    def decrypt_data_key(self, key_arn, wrapped, context):
        if key_arn != self.canonical_key_arn():
            raise GrantCryptoError("GRANT_CRYPTO_INVALID")
        response = self._client.decrypt(KeyId=key_arn, CiphertextBlob=wrapped,
                                        EncryptionAlgorithm="SYMMETRIC_DEFAULT", EncryptionContext=context)
        key = bytearray(response.pop("Plaintext"))
        if response.get("KeyId") != key_arn:
            _scrub(key)
            raise GrantCryptoError("GRANT_CRYPTO_INVALID")
        return key


def _scrub(value):
    if isinstance(value, bytearray):
        value[:] = b"\x00" * len(value)


def _token(value):
    if type(value) not in (bytes, bytearray) or not 1 <= len(value) <= MAX_TOKEN_BYTES:
        raise GrantCryptoError("GRANT_CRYPTO_INVALID")
    text = value.decode("utf-8", errors="strict")
    if any(unicodedata.category(c).startswith("C") for c in text):
        raise GrantCryptoError("GRANT_CRYPTO_INVALID")


def _binding(binding):
    if type(binding) is not Binding:
        raise GrantCryptoError("GRANT_CRYPTO_INVALID")
    binding.__post_init__()
    aad = binding.canonical_bytes()
    return aad, {"purpose": PURPOSE, "schema": str(SCHEMA), "grant_id": binding.grant_id,
                 "tenant_id": binding.authority.tenant_id,
                 "binding_sha256": hashlib.sha256(aad).hexdigest()}


def _encode(value):
    return base64.b64encode(value).decode("ascii")


def _decode(value, minimum, maximum):
    if type(value) is not str or not 1 <= len(value) <= 4 * ((maximum + 2) // 3):
        raise ValueError()
    raw = base64.b64decode(value, validate=True)
    if not minimum <= len(raw) <= maximum or _encode(raw) != value:
        raise ValueError()
    return raw


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def validate_envelope(envelope):
    try:
        if type(envelope) is not bytes or not 1 <= len(envelope) <= 32768:
            raise ValueError()
        data = json.loads(envelope.decode("utf-8"), object_pairs_hook=_pairs)
        if (type(data) is not dict or set(data) != {"version", "key_arn", "wrapped_key", "nonce", "ciphertext"}
                or type(data["version"]) is not int or data["version"] != 1
                or type(data["key_arn"]) is not str or not _ARN.fullmatch(data["key_arn"])):
            raise ValueError()
        return (data["key_arn"], _decode(data["wrapped_key"], 1, 6144),
                _decode(data["nonce"], 12, 12), _decode(data["ciphertext"], 17, MAX_TOKEN_BYTES + 16))
    except Exception:
        raise GrantCryptoError("GRANT_CRYPTO_INVALID") from None


def seal(token_bytes: bytes, binding: Binding, deps: KeyService | None = None) -> bytes:
    key = None
    plaintext = None
    try:
        aad, context = _binding(binding)
        _token(token_bytes)
        service = deps if deps is not None else KmsKeyService()
        arn = service.canonical_key_arn()
        if type(arn) is not str or not _ARN.fullmatch(arn):
            raise ValueError()
        key, wrapped = service.generate_data_key(arn, context)
        if type(key) is not bytearray or len(key) != 32 or type(wrapped) is not bytes or not 1 <= len(wrapped) <= 6144:
            raise ValueError()
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = os.urandom(12)
        plaintext = bytearray(token_bytes)
        ciphertext = AESGCM(bytes(key)).encrypt(nonce, bytes(plaintext), aad)
        envelope = json.dumps({"version": 1, "key_arn": arn, "wrapped_key": _encode(wrapped),
                               "nonce": _encode(nonce), "ciphertext": _encode(ciphertext)},
                              sort_keys=True, separators=(",", ":")).encode("ascii")
        validate_envelope(envelope)
        return envelope
    except Exception:
        pass
    finally:
        _scrub(key)
        _scrub(plaintext)
    # Raise outside the handler so secret-bearing provider exceptions are not
    # retained even as a suppressed __context__ on the public exception.
    raise GrantCryptoError("GRANT_CRYPTO_SEAL_FAILED") from None


T = TypeVar("T")


def with_decrypted(envelope: bytes, binding: Binding, consume: Callable[[bytearray], T],
                   deps: KeyService | None = None) -> T:
    key = None
    plaintext = None
    try:
        aad, context = _binding(binding)
        arn, wrapped, nonce, ciphertext = validate_envelope(envelope)
        if not callable(consume):
            raise ValueError()
        service = deps if deps is not None else KmsKeyService()
        # The stored ARN is never used to select a more permissive key policy.
        if arn != service.canonical_key_arn():
            raise ValueError()
        key = service.decrypt_data_key(arn, wrapped, context)
        if type(key) is not bytearray or len(key) != 32:
            raise ValueError()
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        plaintext = bytearray(AESGCM(bytes(key)).decrypt(nonce, ciphertext, aad))
        _token(plaintext)
        return consume(plaintext)
    except Exception:
        pass
    finally:
        _scrub(key)
        _scrub(plaintext)
    raise GrantCryptoError("GRANT_CRYPTO_DECRYPT_FAILED") from None
