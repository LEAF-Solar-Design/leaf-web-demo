"""Offline signer contracts and hand-derived CanonicalJson.cs byte vectors."""
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from branch_tools import online_signer as signing

ROOT = Path(__file__).resolve().parents[2]
VECTORS = json.loads((Path(__file__).parent / "fixtures/online_signer/vectors.json").read_text(encoding="utf-8"))
SPKI = bytes.fromhex(
    "3059301306072a8648ce3d020106082a8648ce3d03010703420004"
    "24795705eeb29b5acdc90fb19519bedd0bbcfb8d6f5886a707aab554d15e29d5"
    "bb2b13a1ca4d8bc9089994f551acfed1f692d24d9a06e352aa5686942d89a0e8")
ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
NOW = "2026-11-22T12:00:00Z"
FIELDS = dict(name="solar-tool", version="1.2.3", package_digest="a" * 64,
              tiers=["hosted_pro"], audience_tenant="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
              signed_at=NOW)
ENTRY = dict(name="solar-tool", version="1.2.3", package_digest="a" * 64,
             reason="withdrawn", revoked_at=NOW)


@pytest.fixture
def key():
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
def signer(key):
    return signing.LocalKeySigner(key)


@pytest.mark.parametrize("vector", VECTORS, ids=[v["name"] for v in VECTORS])
def test_hand_derived_csharp_canonical_vectors(vector):
    assert signing.canonical_json(vector["input"]) == vector["expected"].encode("utf-8")


def test_existing_signed_v1_fixtures_recanonicalize():
    found = []
    for directory in (ROOT / "harness/branch-compile", ROOT / "server/tests"):
        for path in directory.rglob("*.json"):
            try:
                raw = path.read_bytes()
                document = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            if (isinstance(document, dict) and document.get("schema") == "leaf.tool-envelope.v1"
                    and isinstance(document.get("signature"), str)):
                found.append(path)
                assert signing.canonical_json(document) == raw, str(path)
    if not found:
        pytest.skip("no signed v1 JSON fixture exists in the specified directories")


@pytest.mark.parametrize("value", [1.0, float("nan"), float("inf"), (1,), b"x",
                                        {1: "x"}, {"x": [2.5]}, "\ud800"])
def test_canonical_rejects_outside_integer_json_domain(value):
    with pytest.raises(ValueError):
        signing.canonical_json(value)


def test_production_key_id():
    assert signing.key_id_for_spki(SPKI) == "6c61f1ce07135f9a"


def verify_document(raw, key):
    document = json.loads(raw)
    assert signing.canonical_json(document) == raw
    signature = bytes.fromhex(document.pop("signature"))
    assert signing.strict_der(signature) == signature
    key.public_key().verify(signature, signing.canonical_json(document), ec.ECDSA(hashes.SHA256()))
    return document


def test_envelope_signature_covers_all_unsigned_fields(signer, key):
    document = verify_document(signing.sign_envelope_v2(signer, **FIELDS), key)
    assert document == dict(schema="leaf.tool-envelope.v2",
                            package={k: FIELDS[k] for k in ("name", "version", "package_digest")},
                            tiers=FIELDS["tiers"], audience_tenant=FIELDS["audience_tenant"],
                            signed_at=NOW, key_id=signer.key_id, alg="ECDSA_P256_SHA256")
    spki = key.public_key().public_bytes(serialization.Encoding.DER,
                                       serialization.PublicFormat.SubjectPublicKeyInfo)
    assert signer.key_id == signing.key_id_for_spki(spki)


@pytest.mark.parametrize("field,bad", [
    ("name", None), ("name", "ab"), ("name", "A-tool"), ("name", "a" * 65),
    ("name", "bad--name"), ("name", "tool\n"),
    ("version", None), ("version", "01.2.3"), ("version", "1.2"), ("version", "1.2.3-beta"),
    ("package_digest", None), ("package_digest", "A" * 64), ("package_digest", "g" * 64),
    ("package_digest", "a" * 63), ("tiers", None), ("tiers", []), ("tiers", "hosted_pro"),
    ("tiers", ["hosted_pro", "hosted_pro"]), ("tiers", ["a"]), ("tiers", ["Pro"]),
    ("tiers", ["a" * 42]), ("tiers", ["bad-tier"]), ("tiers", [{}]),
    ("audience_tenant", None), ("audience_tenant", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
    ("audience_tenant", "aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa"), ("audience_tenant", "internal"),
    ("signed_at", None), ("signed_at", "2026-02-30T12:00:00Z"),
    ("signed_at", "2026-11-22T12:00:00.1Z"), ("signed_at", "2026-11-22T12:00:00+00:00"),
    ("signed_at", "2026-11-22T24:00:00Z"),
])
def test_envelope_rejects_each_malformed_field_before_signing(field, bad):
    signer = Mock(key_id="a" * 16)
    with pytest.raises(ValueError):
        signing.sign_envelope_v2(signer, **dict(FIELDS, **{field: bad}))
    signer.sign.assert_not_called()


@pytest.mark.parametrize("entries", [[], [ENTRY]])
def test_revocations_signed_and_inputs_unchanged(signer, key, entries):
    before = deepcopy(entries)
    document = verify_document(signing.sign_revocations(signer, entries=entries, issued_at=NOW), key)
    assert document == dict(schema="leaf.tool-revocations.v1", entries=entries, issued_at=NOW,
                            key_id=signer.key_id, alg="ECDSA_P256_SHA256")
    assert entries == before


@pytest.mark.parametrize("field,bad", [("name", 1), ("version", "1"),
    ("package_digest", "x" * 64), ("reason", None), ("revoked_at", "yesterday")])
def test_revocation_rejects_each_malformed_field(signer, field, bad):
    with pytest.raises(ValueError):
        signing.sign_revocations(signer, entries=[dict(ENTRY, **{field: bad})], issued_at=NOW)


@pytest.mark.parametrize("entries", [None, {}, [None], [dict(ENTRY, extra=1)],
                                     [{k: v for k, v in ENTRY.items() if k != "reason"}]])
def test_revocation_rejects_malformed_structure(signer, entries):
    with pytest.raises(ValueError):
        signing.sign_revocations(signer, entries=entries, issued_at=NOW)


def test_revocation_rejects_issued_at(signer):
    with pytest.raises(ValueError):
        signing.sign_revocations(signer, entries=[], issued_at="2026-11-22")


@pytest.mark.parametrize("key_id", [None, "ABCDEF0123456789", "0" * 15])
def test_invalid_signer_id(key_id):
    with pytest.raises(ValueError):
        signing.sign_envelope_v2(Mock(key_id=key_id), **FIELDS)


@pytest.mark.parametrize("curve", [ec.SECP384R1(), ec.SECP256K1()])
def test_local_signer_rejects_other_curves(curve):
    with pytest.raises(ValueError):
        signing.LocalKeySigner(ec.generate_private_key(curve))


def test_local_signer_rejects_non_private_key():
    with pytest.raises(ValueError):
        signing.LocalKeySigner(None)


@pytest.mark.parametrize("pair", [(1, 1), (128, 255), (ORDER - 1, ORDER - 1)])
def test_strict_der_accepts_minimal_positive_scalars(pair):
    der = utils.encode_dss_signature(*pair)
    assert signing.strict_der(der) == der


@pytest.mark.parametrize("hex_sig", [
    "", "3006020101020101ff",             # total length and trailing data
    "3106020101020101",                   # wrong sequence tag
    "3007020101020101",                   # sequence length mismatch
    "308106020101020101",                 # long-form length
    "3006020002020101",                   # zero-length integer
    "3006020180020101",                   # negative integer
    "300702020001020101",                 # unnecessary leading zero
    "3006020100020101",                   # zero r
    "3006020101020100",                   # zero s
    "3006020101030101",                   # wrong second integer tag
    "300702010102020001",                 # nonminimal s
    "3006020101020180",                   # negative s
    "3006020101020201",                   # truncated second integer
    "3026022101" + "00" * 32 + "020101", # 33-byte integer without zero prefix
    "302702220000" + "01" * 32 + "020101", # overlong integer
])
def test_strict_der_rejects_malformed_encoding(hex_sig):
    with pytest.raises(ValueError):
        signing.strict_der(bytes.fromhex(hex_sig))


@pytest.mark.parametrize("pair", [(ORDER, 1), (1, ORDER), (0, 1), (1, 0)])
def test_strict_der_rejects_out_of_range_scalars(pair):
    with pytest.raises(ValueError):
        signing.strict_der(utils.encode_dss_signature(*pair))


def kms_client(key):
    spki = key.public_key().public_bytes(serialization.Encoding.DER,
                                       serialization.PublicFormat.SubjectPublicKeyInfo)
    return Mock(get_public_key=Mock(return_value={"PublicKey": spki}),
                sign=Mock(return_value={"Signature": utils.encode_dss_signature(1, 2)}))


def test_kms_signs_raw_and_caches_public_key_across_five_signs(key):
    client = kms_client(key)
    signer = signing.KmsSigner("test-key-reference", client)
    for _ in range(5):
        assert signer.sign(b"message") == utils.encode_dss_signature(1, 2)
    client.get_public_key.assert_called_once_with(KeyId="test-key-reference")
    assert client.sign.call_count == 5
    client.sign.assert_called_with(KeyId="test-key-reference", Message=b"message",
                                   MessageType="RAW", SigningAlgorithm="ECDSA_SHA_256")


def test_kms_sign_failure_is_unavailable_and_redacted(key):
    client = kms_client(key)
    signer = signing.KmsSigner("test-key-reference", client)
    client.sign.side_effect = RuntimeError("private service detail")
    with pytest.raises(signing.SignerUnavailable, match="^signing unavailable$") as error:
        signing.sign_envelope_v2(signer, **FIELDS)
    assert error.value.__suppress_context__


@pytest.mark.parametrize("response", [{}, {"Signature": b"invalid"}])
def test_kms_bad_signature_fails_closed(key, response):
    client = kms_client(key)
    signer = signing.KmsSigner("test-key-reference", client)
    client.sign.return_value = response
    with pytest.raises(signing.SignerUnavailable):
        signer.sign(b"message")


@pytest.mark.parametrize("response", [None, {}, {"PublicKey": b"invalid"}])
def test_kms_public_key_failure_is_unavailable(response):
    client = Mock(get_public_key=Mock(return_value=response))
    with pytest.raises(signing.SignerUnavailable):
        signing.KmsSigner("test-key-reference", client)


def test_kms_public_key_client_exception_is_unavailable():
    client = Mock(get_public_key=Mock(side_effect=RuntimeError("private service detail")))
    with pytest.raises(signing.SignerUnavailable, match="^signer public key unavailable$"):
        signing.KmsSigner("test-key-reference", client)


def test_kms_rejects_non_p256_public_key():
    client = kms_client(ec.generate_private_key(ec.SECP384R1()))
    with pytest.raises(signing.SignerUnavailable):
        signing.KmsSigner("test-key-reference", client)


def test_configured_signer_unset_does_not_construct_client(monkeypatch):
    monkeypatch.delenv("LEAF_TOOL_SIGNER_KMS_KEY_ID", raising=False)
    client = Mock(side_effect=AssertionError("must remain offline"))
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=client))
    assert signing.configured_signer() is None
    client.assert_not_called()


def test_configured_signer_has_bounded_timeouts_and_retries(monkeypatch, key):
    client = kms_client(key)
    factory = Mock(return_value=client)
    config = Mock(side_effect=lambda **kwargs: kwargs)
    monkeypatch.setenv("LEAF_TOOL_SIGNER_KMS_KEY_ID", "test-key-reference")
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=factory))
    monkeypatch.setitem(sys.modules, "botocore.config", SimpleNamespace(Config=config))
    assert isinstance(signing.configured_signer(), signing.KmsSigner)
    config.assert_called_once_with(connect_timeout=5, read_timeout=10,
                                   retries={"mode": "standard", "max_attempts": 2})
    factory.assert_called_once_with("kms", config={
        "connect_timeout": 5, "read_timeout": 10,
        "retries": {"mode": "standard", "max_attempts": 2}})


def test_configured_signer_client_creation_failure(monkeypatch):
    monkeypatch.setenv("LEAF_TOOL_SIGNER_KMS_KEY_ID", "test-key-reference")
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=Mock(side_effect=RuntimeError())))
    monkeypatch.setitem(sys.modules, "botocore.config", SimpleNamespace(Config=Mock()))
    with pytest.raises(signing.SignerUnavailable):
        signing.configured_signer()
