"""Hermetic custody tests. No real KMS client or credentials are used."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from server import integration_turn_grant_crypto as crypto
from leaf_platform.integration_turn_grant_store import (
    Authority, Binding, GrantStoreError, LEGACY_OPERATIONS, CURRENT_OPERATIONS,
)

ARN = "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-4234-8234-123456789012"


def binding():
    return Binding(Authority(*(str(uuid4()) for _ in range(4)), "auth0", "auth0|trusted",
                             "studio-session", "turn-1", "https://api.example.com"),
                   str(uuid4()), 1000, 1120, 1110, 1100)


class FakeKeys:
    def __init__(self):
        self.key = os.urandom(32)
        self.buffers = []
        self.contexts = []

    def canonical_key_arn(self):
        return ARN

    def generate_data_key(self, key_arn, context):
        assert key_arn == ARN
        self.contexts.append(context)
        key = bytearray(self.key)
        self.buffers.append(key)
        return key, b"random-test-wrapped-key"

    def decrypt_data_key(self, key_arn, wrapped, context):
        assert key_arn == ARN and wrapped == b"random-test-wrapped-key"
        assert context == self.contexts[0]
        self.contexts.append(context)
        key = bytearray(self.key)
        self.buffers.append(key)
        return key


@pytest.fixture(autouse=True)
def no_real_kms(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("real KMS must never run")
    monkeypatch.setattr(crypto.KmsKeyService, "canonical_key_arn", forbidden)


@pytest.mark.parametrize("operations", [LEGACY_OPERATIONS, CURRENT_OPERATIONS])
def test_round_trip_context_and_scrubbing(caplog, capsys, operations):
    b, keys = binding(), FakeKeys()
    b = replace(b, authority=replace(b.authority, operations=operations))
    token = b"secret-test-bearer"
    envelope = crypto.seal(token, b, keys)
    exposed = []
    def consume(value):
        assert value == token
        exposed.append(value)
        return "trusted-result"
    assert crypto.with_decrypted(envelope, b, consume, keys) == "trusted-result"
    assert keys.contexts[0] == {"purpose": b.purpose, "schema": "1", "grant_id": b.grant_id,
                                "tenant_id": b.authority.tenant_id,
                                "binding_sha256": hashlib.sha256(b.canonical_bytes()).hexdigest()}
    assert all(not any(buf) for buf in keys.buffers + exposed)
    assert token not in envelope
    assert token.decode() not in caplog.text + str(capsys.readouterr())


@pytest.mark.parametrize("operations,altered_operations", [
    (LEGACY_OPERATIONS, CURRENT_OPERATIONS), (CURRENT_OPERATIONS, LEGACY_OPERATIONS),
])
def test_changing_only_bound_operations_fails_authentication(operations, altered_operations):
    b = binding()
    b = replace(b, authority=replace(b.authority, operations=operations))
    keys = FakeKeys()
    envelope = crypto.seal(b"synthetic-bearer", b, keys)
    altered = replace(b, authority=replace(b.authority, operations=altered_operations))
    # Use a synthetic provider that returns the same key even for changed AAD,
    # proving AES-GCM itself rejects the altered binding as well as KMS context.
    keys.decrypt_data_key = lambda *args: bytearray(keys.key)
    with pytest.raises(crypto.GrantCryptoError):
        crypto.with_decrypted(envelope, altered, lambda _: pytest.fail("altered binding"), keys)


@pytest.mark.parametrize("field", list(Authority.__dataclass_fields__))
def test_every_authority_field_is_authenticated(field):
    b, keys = binding(), FakeKeys()
    envelope = crypto.seal(b"bearer", b, keys)
    changes = {"tenant_id": str(uuid4()), "project_id": str(uuid4()),
               "platform_user_id": str(uuid4()), "binding_id": str(uuid4()),
               "subject": "auth0|other", "session_id": "other-session", "turn_id": "other-turn",
               "origin": "https://other.example.com", "external_authority": "other",
               "operations": ("dataset_query",)}
    if field in ("external_authority", "operations"):
        with pytest.raises(GrantStoreError):
            replace(b.authority, **{field: changes[field]})
    else:
        altered = replace(b, authority=replace(b.authority, **{field: changes[field]}))
        with pytest.raises(crypto.GrantCryptoError):
            crypto.with_decrypted(envelope, altered, lambda _: pytest.fail("foreign callback"), keys)


@pytest.mark.parametrize("field,value", [("expires_at", 1121), ("expires_at", 1000),
    ("expires_at", float("nan")), ("issued_at", float("inf")),
    ("turn_deadline", 1099), ("verified_jwt_exp", 1099), ("schema", True), ("purpose", "other")])
def test_expiry_and_binding_bounds(field, value):
    with pytest.raises(GrantStoreError):
        replace(binding(), **{field: value})


@pytest.mark.parametrize("token", [b"", b"x" * 16385, b"\xff", b"a\x00b", b"a\nb", "bearer"])
def test_invalid_token_before_key_side_effect(token):
    keys = FakeKeys()
    with pytest.raises(crypto.GrantCryptoError):
        crypto.seal(token, binding(), keys)
    assert keys.contexts == []


@pytest.mark.parametrize("origin", ["http://api.example.com", "https://u:p@api.example.com",
    "https://api.example.com/", "https://api.example.com?x", "https://api.example.com#x",
    "https://API.example.com", "https://api.example.com:443", "https://api.example.com\\x"])
def test_origin_is_literal_canonical(origin):
    with pytest.raises(GrantStoreError):
        replace(binding().authority, origin=origin)


@pytest.mark.parametrize("field", ["key_arn", "wrapped_key", "nonce", "ciphertext", "version", "extra"])
def test_tampered_envelope(field):
    b, keys = binding(), FakeKeys()
    data = json.loads(crypto.seal(b"bearer", b, keys))
    data[field] = "invalid" if field != "version" else 2
    with pytest.raises(crypto.GrantCryptoError):
        crypto.with_decrypted(json.dumps(data).encode(), b, lambda _: pytest.fail("tampered"), keys)


def test_ciphertext_bit_flip_and_callback_failure(caplog):
    b, keys = binding(), FakeKeys()
    envelope = crypto.seal(b"private-bearer", b, keys)
    data = json.loads(envelope)
    raw = bytearray(crypto.base64.b64decode(data["ciphertext"]))
    raw[0] ^= 1
    data["ciphertext"] = crypto._encode(raw)
    with pytest.raises(crypto.GrantCryptoError):
        crypto.with_decrypted(json.dumps(data).encode(), b, lambda _: pytest.fail("tampered"), keys)
    exposed = []
    def failure(value):
        exposed.append(value)
        raise RuntimeError(bytes(value).decode())
    with pytest.raises(crypto.GrantCryptoError) as exc:
        crypto.with_decrypted(envelope, b, failure, keys)
    assert str(exc.value) == "GRANT_CRYPTO_DECRYPT_FAILED"
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
    assert not any(exposed[0]) and all(not any(v) for v in keys.buffers)
    assert "private-bearer" not in caplog.text


def test_strict_envelope_and_key_identity():
    b, keys = binding(), FakeKeys()
    envelope = crypto.seal(b"bearer", b, keys)
    for bad in (b"{}", b"x" * 32769, b'{"version":1,"version":1}', b"\xff"):
        with pytest.raises(crypto.GrantCryptoError):
            crypto.validate_envelope(bad)
    data = json.loads(envelope)
    data["key_arn"] = ARN.replace("123456789012", "999999999999")
    with pytest.raises(crypto.GrantCryptoError):
        crypto.with_decrypted(json.dumps(data).encode(), b, lambda _: pytest.fail("wrong key"), keys)


def test_kms_alias_resolution_and_fixed_identity(monkeypatch):
    # Inject the SDK itself to inspect the production adapter without real I/O.
    import types
    calls = []
    key = os.urandom(32)
    class Client:
        def describe_key(self, **kwargs):
            calls.append(("describe", kwargs))
            return {"KeyMetadata": {"Arn": ARN, "Enabled": True, "KeyState": "Enabled",
                                    "KeySpec": "SYMMETRIC_DEFAULT", "KeyUsage": "ENCRYPT_DECRYPT"}}
        def generate_data_key(self, **kwargs):
            calls.append(("generate", kwargs))
            return {"KeyId": ARN, "Plaintext": key, "CiphertextBlob": b"wrapped"}
        def decrypt(self, **kwargs):
            calls.append(("decrypt", kwargs))
            return {"KeyId": ARN, "Plaintext": key}
    def client(service, **kwargs):
        assert service == "kms"
        config = kwargs["config"]
        assert config.connect_timeout <= 1 and config.read_timeout <= 1
        assert config.retries["total_max_attempts"] == 1
        return Client()
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=client))
    # Undo only the no-network method stub; the fake boto3 remains installed.
    monkeypatch.setattr(crypto.KmsKeyService, "canonical_key_arn", _REAL_RESOLVE)
    monkeypatch.setenv("LEAF_INTEGRATION_TURN_GRANTS_KMS_KEY_ID", "alias/studio")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    service = crypto.KmsKeyService()
    assert calls == []
    b = binding()
    envelope = crypto.seal(b"bearer", b, service)
    assert crypto.with_decrypted(envelope, b, lambda value: len(value), service) == 6
    assert calls[0] == ("describe", {"KeyId": "alias/studio"})
    assert calls[1][1]["KeyId"] == calls[2][1]["KeyId"] == ARN
    monkeypatch.delenv("AWS_REGION")
    with pytest.raises(crypto.GrantCryptoError, match="GRANT_CRYPTO_CONFIG"):
        crypto.KmsKeyService().canonical_key_arn()


@pytest.mark.parametrize("field,value", [("Enabled", False), ("KeyState", "Disabled"),
    ("KeySpec", "RSA_2048"), ("KeyUsage", "SIGN_VERIFY"), ("Arn", "alias/not-canonical")])
def test_kms_refuses_ineligible_described_keys(monkeypatch, field, value):
    import types
    metadata = {"Arn": ARN, "Enabled": True, "KeyState": "Enabled",
                "KeySpec": "SYMMETRIC_DEFAULT", "KeyUsage": "ENCRYPT_DECRYPT"}
    metadata[field] = value
    client = types.SimpleNamespace(describe_key=lambda **kwargs: {"KeyMetadata": metadata})
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *a, **k: client))
    monkeypatch.setattr(crypto.KmsKeyService, "canonical_key_arn", _REAL_RESOLVE)
    monkeypatch.setenv("LEAF_INTEGRATION_TURN_GRANTS_KMS_KEY_ID", "alias/studio")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    with pytest.raises(crypto.GrantCryptoError, match="GRANT_CRYPTO_UNAVAILABLE"):
        crypto.KmsKeyService().canonical_key_arn()


@pytest.mark.parametrize("key,region", [("", "us-east-1"), ("alias/bad\nkey", "us-east-1"),
    ("alias/studio", ""), ("alias/studio", "not a region")])
def test_invalid_config_precedes_sdk_creation(monkeypatch, key, region):
    import types
    def forbidden(*args, **kwargs):
        pytest.fail("SDK created for invalid configuration")
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=forbidden))
    monkeypatch.setattr(crypto.KmsKeyService, "canonical_key_arn", _REAL_RESOLVE)
    monkeypatch.setenv("LEAF_INTEGRATION_TURN_GRANTS_KMS_KEY_ID", key)
    monkeypatch.setenv("AWS_REGION", region)
    with pytest.raises(crypto.GrantCryptoError, match="GRANT_CRYPTO_CONFIG"):
        crypto.KmsKeyService().canonical_key_arn()


_REAL_RESOLVE = crypto.KmsKeyService.canonical_key_arn
