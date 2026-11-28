"""Frozen v2 issuer, binding, caller, rate and real RSA signature boundaries."""
import base64
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

import deps
import estimator_handoff as handoff

TENANT = "11111111-1111-4111-8111-111111111111"
PROJECT = "22222222-2222-4222-8222-222222222222"
ORIGIN = "https://estimator-staging.leafdesign.ai"
KID = "arn:aws:kms:us-east-1:807034087062:key/11111111-1111-4111-8111-111111111111"
CHAN = "a" * 43
ROW = dict(tenant_id=TENANT, studio_project_id=PROJECT, deployment_id="studio-staging",
           estimator_origin=ORIGIN, project="xriver")


class StubKms:
    def __init__(self, key=None):
        self.key = key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.get_calls = 0
        self.sign_calls = 0

    def get_public_key(self, **kwargs):
        assert kwargs == {"KeyId": "alias/test"}
        self.get_calls += 1
        return {"KeyId": KID, "PublicKey": self.key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)}

    def sign(self, **kwargs):
        assert kwargs["KeyId"] == KID
        assert kwargs["MessageType"] == "RAW"
        assert kwargs["SigningAlgorithm"] == "RSASSA_PKCS1_V1_5_SHA_256"
        self.sign_calls += 1
        return {"Signature": self.key.sign(kwargs["Message"], padding.PKCS1v15(), hashes.SHA256())}


def decode(segment):
    raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    assert handoff.b64(raw) == segment
    return raw


@pytest.mark.parametrize("bits", [2048, 3072])
def test_exact_claims_and_header_verify_with_stub_public_key(bits):
    kms = StubKms(rsa.generate_private_key(public_exponent=65537, key_size=bits))
    signer = handoff.KmsRs256Signer("alias/test", kms)
    claims = handoff.claims_for(handoff.Binding(**ROW), "auth0|human", CHAN, "staging", now=1800000000)
    token = signer.sign(claims)
    header, payload, signature = token.split(".")
    assert handoff.TOKEN.fullmatch(token) and len(token) <= 2048
    assert json.loads(decode(header)) == {"alg": "RS256", "typ": "leaf-estimator-handoff+jwt", "kid": KID}
    assert json.loads(decode(payload)) == claims
    assert set(claims) == {"ver", "iss", "aud", "sub", "tenant", "sproj", "project", "iat", "nbf", "exp", "jti", "chn"}
    assert claims["ver"] == 2 and claims["nbf"] == claims["iat"] and claims["exp"] - claims["iat"] == 60
    assert claims["iss"] == "https://platform-staging.leafdesign.ai"
    assert claims["aud"] == "leaf-estimator:studio-staging"
    assert claims["tenant"] == TENANT and claims["sproj"] == PROJECT and claims["chn"] == CHAN
    assert "auth0|human" not in decode(payload).decode()
    kms.key.public_key().verify(decode(signature), f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    signer.sign(claims)
    assert kms.get_calls == 1 and kms.sign_calls == 2
    assert decode(payload) == json.dumps(claims, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


@pytest.mark.parametrize("key", [
    rsa.generate_private_key(public_exponent=65537, key_size=1024),
    rsa.generate_private_key(public_exponent=3, key_size=2048),
    ec.generate_private_key(ec.SECP256R1()),
])
def test_invalid_key_refused_before_sign(key):
    kms = StubKms(key)
    with pytest.raises(ValueError):
        handoff.KmsRs256Signer("alias/test", kms).sign({})
    assert kms.sign_calls == 0


def test_origin_table_and_local_posture_are_frozen():
    assert dict(handoff.ESTIMATOR_ORIGINS) == {
        "https://estimator-staging.leafdesign.ai": ("studio-staging", "staging"),
        "https://estimator-studio-staging-973968415611.us-central1.run.app": ("studio-staging", "staging"),
    }
    with pytest.raises(TypeError):
        handoff.ESTIMATOR_ORIGINS["https://evil.test"] = ("studio-staging", "staging")
    local = dict(ROW, deployment_id="dev-local", estimator_origin="http://localhost:8080")
    assert handoff.parse_bindings(json.dumps([local]), "local")[0].deployment_id == "dev-local"
    with pytest.raises(ValueError):
        handoff.parse_bindings(json.dumps([local]), "staging")
    for origin in ["http://localhost:0", "http://localhost:65536", "http://localhost:80/", "http://localhost:80\n",
                   "https://xriver-platform-973968415611.us-central1.run.app", "https://estimator-staging.leafdesign.ai.evil.test"]:
        assert handoff.origin_entry(origin, "local") is None


@pytest.mark.parametrize("rows", [
    {}, [ROW] * 33, [ROW, ROW], [dict(ROW, deployment_id="another")],
    [dict(ROW, tenant_id=TENANT.replace("-", ""))], [dict(ROW, studio_project_id="{" + PROJECT + "}")],
    [dict(ROW, project="xriver\n")], [dict(ROW, extra=True)],
    [dict(ROW, estimator_origin=ORIGIN + "/")],
    [ROW, dict(ROW, studio_project_id=TENANT, estimator_origin="https://estimator-studio-staging-973968415611.us-central1.run.app")],
])
def test_binding_whole_config_refused(rows):
    with pytest.raises(ValueError):
        handoff.parse_bindings(json.dumps(rows), "staging")


@pytest.mark.parametrize("raw", ["broken", '[{"tenant_id":"a","tenant_id":"b"}]', '[NaN]', '[Infinity]'])
def test_invalid_json_and_duplicate_keys_refused(raw):
    with pytest.raises(ValueError):
        handoff.parse_bindings(raw, "staging")


def test_production_tier_row_refused_in_staging(monkeypatch):
    monkeypatch.setattr(handoff, "ESTIMATOR_ORIGINS", {ORIGIN: ("studio-staging", "production")})
    with pytest.raises(ValueError):
        handoff.parse_bindings(json.dumps([ROW]), "staging")


@pytest.mark.parametrize("changes", [
    {"subject": None}, {"subject": ""}, {"subject": "machine@clients"},
    {"subject": handoff.SUBJECT_PREFIX + "staging"}, {"authority_resolved": False},
    {"authority_resolved": 1}, {"verified_demo": True}, {"backedge": True},
])
def test_positive_human_check_rejects_other_authorities(monkeypatch, changes):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    args = dict(subject="auth0|human", authority_resolved=True)
    args.update(changes)
    assert not handoff.human_caller(deps.TenantContext(TENANT, **args))


def test_guest_demo_actor_and_auth_off_refused(monkeypatch):
    tenant = deps.TenantContext(TENANT, subject="auth0|human", authority_resolved=True)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    assert handoff.human_caller(tenant)
    assert not handoff.human_caller(TENANT)
    monkeypatch.setattr(handoff, "resolve_demo_actor", lambda _: object())
    assert not handoff.human_caller(tenant)
    monkeypatch.setattr(handoff, "resolve_demo_actor", lambda _: None)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    assert not handoff.human_caller(tenant)


def test_rate_limit_atomic_window_and_bounded_subjects():
    limiter = handoff.MintLimiter()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: limiter.admit("human", now=1000), range(30)))
    assert results.count(0) == 20 and results.count(300) == 10
    assert limiter.admit("human", now=1299) == 1
    assert limiter.admit("human", now=1300) == 0
    for number in range(10001):
        limiter.admit(str(number), now=1400)
    assert len(limiter.subjects) == 10000 and "0" not in limiter.subjects


def test_invalid_channel_and_boolean_clock_never_become_claims():
    for chan in [True, CHAN + "\n", CHAN[:-1], "=" * 43]:
        with pytest.raises(ValueError):
            handoff.claims_for(handoff.Binding(**ROW), "human", chan, "staging")
    with pytest.raises(ValueError):
        handoff.claims_for(handoff.Binding(**ROW), "human", CHAN, "staging", now=True)


def test_client_is_lazy_once_and_uses_bounded_kms_config(monkeypatch):
    import boto3

    calls = []
    kms = StubKms()
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: calls.append((args, kwargs)) or kms)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    signer = handoff.KmsRs256Signer("alias/test")
    assert not calls
    claims = handoff.claims_for(handoff.Binding(**ROW), "human", CHAN, "staging")
    signer.sign(claims)
    signer.sign(claims)
    assert len(calls) == 1 and calls[0][0] == ("kms",)
    config = calls[0][1]["config"]
    assert calls[0][1]["region_name"] == "us-east-1"
    assert config.connect_timeout == config.read_timeout == 3
    assert config.retries == {"max_attempts": 1}


def test_pseudonym_is_per_deployment_and_jti_is_fresh():
    first = handoff.claims_for(handoff.Binding(**ROW), "human", CHAN, "staging")
    again = handoff.claims_for(handoff.Binding(**ROW), "human", CHAN, "staging")
    local = handoff.Binding(**dict(ROW, deployment_id="dev-local", estimator_origin="http://localhost:8080"))
    other = handoff.claims_for(local, "human", CHAN, "local")
    assert first["sub"] == again["sub"] != other["sub"]
    assert first["jti"] != again["jti"] and len(first["jti"]) >= 22
    assert other["iss"] == "http://localhost:3000"


def test_duplicate_project_pair_and_origin_disable_the_whole_config():
    local = dict(ROW, deployment_id="dev-local", estimator_origin="http://localhost:8080")
    for rows in [[ROW, local], [ROW, dict(ROW, studio_project_id=TENANT)]]:
        with pytest.raises(ValueError):
            handoff.parse_bindings(json.dumps(rows), "local")
