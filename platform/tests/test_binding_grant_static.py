"""Producer and strict verifier contract, without database or network access."""

import importlib.util
import json
from dataclasses import replace
from pathlib import Path
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("binding_fixture_generator", ROOT / "scripts/gen_binding_grant_fixture.py")
generator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = generator
SPEC.loader.exec_module(generator)
grant = generator.grant


@pytest.fixture(scope="module")
def signer():
    return grant.LocalTestSigner(rsa.generate_private_key(public_exponent=65537, key_size=3072))


def refused(code, action):
    with pytest.raises(grant.GrantError) as error:
        action()
    assert error.value.code == code


def test_round_trip(signer):
    for name in ("Workspace", 'A "quote" \\ path', "Café 東京", "😀" * 200):
        claims = replace(generator.sample_claims(), workspaceName=name)
        token = grant.encode_grant(claims, signer)
        assert grant.verify_grant(token, signer.private_key.public_key(),
                                  now=claims.iat, expected_iss=claims.iss) == claims
    # D form does not imply a UUID version or variant restriction.
    grant.validate_claims(replace(claims, projectId="ffffffff-ffff-ffff-ffff-ffffffffffff"))


def test_claim_validation():
    claims = generator.sample_claims()
    failures = [
        ("schemaVersion", True, "schema_version"), ("schemaVersion", 2, "schema_version"),
        ("schemaVersion", 1.0, "schema_version"), ("iss", "", "issuer"),
        ("iss", None, "issuer"), ("iss", "bad\nissuer", "issuer"),
        ("aud", "other", "audience"), ("documentFingerprint", "sha256:" + "A" * 64, "fingerprint"),
        ("documentFingerprint", "a" * 64, "fingerprint"), ("documentFingerprint", None, "fingerprint"),
        ("iat", True, "time_type"), ("iat", 1.0, "time_type"),
        ("exp", None, "time_type"), ("exp", claims.iat, "lifetime"),
        ("exp", claims.iat - 1, "lifetime"), ("exp", claims.iat + 301, "lifetime"),
        ("nonce", grant.b64u(bytes(31)), "nonce"), ("nonce", claims.nonce + "=", "nonce"),
        ("nonce", "!", "nonce"), ("nonce", None, "nonce"),
    ]
    for field in ("platformTenantId", "projectId", "drawingId", "drawingVersionId",
                  "actorBindingId", "pluginSessionId"):
        for value in ("00000000-0000-0000-0000-000000000000",
                      "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
                      "11111111111111111111111111111111", None):
            failures.append((field, value, "uuid"))
    for field in ("workspaceName", "projectName", "drawingName", "versionName"):
        for value in ("", "a" * 201, None, "\x00", "\x7f", "\x85", "\ud800",
                      "\u061c", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c",
                      "\u202d", "\u202e", "\u2066", "\u2067", "\u2068", "\u2069"):
            failures.append((field, value, "name"))
    for field, value, code in failures:
        refused(code, lambda: grant.validate_claims(replace(claims, **{field: value})))
    grant.validate_claims(replace(claims, exp=claims.iat + 300))
    for kid in ("", "a" * 65, "a.b", "é", None):
        refused("kid", lambda: grant.canonical_header(kid))
    for text in ("Zg=", "Zg==", "Zh", "A", "+w", "/w", " Zg", None):
        refused("base64url", lambda: grant.b64u_decode(text))
    assert grant.b64u_decode("Zg") == b"f"


def test_canonical_literal():
    expected = (
        b'{"schemaVersion":1,"iss":"https://binding.example.test",'
        b'"aud":"leaf-automation:autocad:drawing.bind:v1",'
        b'"platformTenantId":"11111111-1111-1111-1111-111111111111","workspaceName":"Workspace",'
        b'"projectId":"22222222-2222-2222-2222-222222222222","projectName":"Project",'
        b'"drawingId":"33333333-3333-3333-3333-333333333333","drawingName":"Drawing",'
        b'"drawingVersionId":"44444444-4444-4444-4444-444444444444","versionName":"Version 1",'
        b'"actorBindingId":"55555555-5555-5555-5555-555555555555",'
        b'"pluginSessionId":"66666666-6666-6666-6666-666666666666",'
        b'"documentFingerprint":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"iat":1700000000,"exp":1700000120,"nonce":"AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"}'
    )
    assert grant.canonical_payload(generator.sample_claims()) == expected
    assert grant.canonical_header("fixture-key") == b'{"alg":"RS256","kid":"fixture-key","typ":"leaf-binding-grant+jws"}'


def test_kms_arguments_and_signature_length():
    class Client:
        signature = b"x" * 384

        def sign(self, **kwargs):
            self.args = kwargs
            return {"Signature": self.signature}

    client = Client()
    signer = grant.KmsSigner("test-key-id", "test-kid", client)
    assert signer.sign(b"H.P") == b"x" * 384
    assert client.args == {"KeyId": "test-key-id", "Message": b"H.P", "MessageType": "RAW",
                           "SigningAlgorithm": "RSASSA_PKCS1_V1_5_SHA_256"}
    client.signature = b"x" * 383
    refused("signature_length", lambda: signer.sign(b"H.P"))
    refused("signature_length", lambda: grant.encode_grant(generator.sample_claims(), signer))


def test_committed_fixture_public_only():
    text = generator.FIXTURE.read_text(encoding="utf-8")
    fixture = json.loads(text)
    generator.check_fixture(fixture)
    assert "PRIVATE KEY" not in text
    def public_only(value):
        if isinstance(value, dict):
            assert not set(value).intersection({"d", "p", "q", "dp", "dq", "qi"})
            for item in value.values():
                public_only(item)
        elif isinstance(value, list):
            for item in value:
                public_only(item)
    public_only(fixture)
    assert len(fixture["cases"]) >= 15


def test_strict_verifier(signer):
    claims = generator.sample_claims()
    public_key = signer.private_key.public_key()
    def verify(token, **options):
        return grant.verify_grant(token, public_key, now=options.get("now", claims.iat),
                                  expected_iss=options.get("issuer", claims.iss))
    token = grant.encode_grant(claims, signer)
    refused("issuer", lambda: verify(token, issuer="wrong"))
    refused("expired", lambda: verify(token, now=claims.exp))
    refused("token_format", lambda: verify("one.two"))
    refused("token_size", lambda: grant.encode_grant(replace(claims, iss="a" * 8192), signer))
    header = grant.b64u(grant.canonical_header(signer.kid))
    def signed(raw):
        message = header + "." + grant.b64u(raw)
        return message + "." + grant.b64u(signer.sign(message.encode("ascii")))
    raw = grant.canonical_payload(claims)
    for body, code in (
        (b'{"schemaVersion":1,' + raw[1:], "json"),
        (raw.replace(b'"iat":1700000000', b'"iat":1.7e9'), "json"),
        (b"[]", "json"),
        (raw.replace(b'"workspaceName":"Workspace",', b""), "payload_fields"),
        (raw.replace(b'"workspaceName":"Workspace"', b'"workspaceName":null'), "name"),
        (b"\xef\xbb\xbf" + raw, "json"),
        (raw + b"\n", "noncanonical"),
    ):
        refused(code, lambda: verify(signed(body)))
    # Signature rejection precedes JSON parsing for received bytes.
    broken = signed(b"not json").split(".")
    signature = bytearray(grant.b64u_decode(broken[2]))
    signature[-1] ^= 1
    broken[2] = grant.b64u(signature)
    refused("signature", lambda: verify(".".join(broken)))
