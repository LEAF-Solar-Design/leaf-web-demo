"""PP-1: cryptographic identity, host confinement and real database authority."""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
import uuid
from unittest.mock import Mock

from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request
import jwt
import pytest

import auth

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("pp1_identity_test", ROOT / "scripts/proof_project_identity.py")
identity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(identity)
DB_REASON = "PostgreSQL integration test requires DATABASE_URL"
database = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason=DB_REASON)


@pytest.fixture
def proof(monkeypatch, tmp_path):
    issuer = identity.FixtureIssuer.create("pp1-" + uuid.uuid4().hex)
    path = tmp_path / "public.jwks"
    issuer.write_public_jwks(path)
    for key in tuple(os.environ):
        if key.startswith("LEAF_PROOF_"):
            monkeypatch.delenv(key)
    for key, value in issuer.profile_env(path).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "test")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_TENANT_CLAIM_NS", auth.DEFAULT_CLAIM_NS)
    token = issuer.mint("owner", tenant_id="tenant", org_id="tenant",
                        tier="hosted_pro", claim_ns=auth.claim_ns())
    return SimpleNamespace(issuer=issuer, path=path, token=token,
                           claims=jwt.decode(token, options={"verify_signature": False}))


def signed(proof, **changes):
    return jwt.encode({**proof.claims, **changes}, proof.issuer._private_key,
                      algorithm="RS256", headers={"kid": proof.issuer.kid})


def refused(token, monkeypatch, detail="fixture identity verification failed"):
    ordinary = Mock(side_effect=AssertionError("fixture reached ordinary key lookup"))
    remote = Mock(side_effect=AssertionError("fixture reached remote JWKS"))
    monkeypatch.setattr(auth, "_signing_key", ordinary)
    monkeypatch.setattr(auth, "PyJWKClient", remote)
    monkeypatch.setattr(auth, "issuer", Mock(side_effect=AssertionError("ordinary issuer")))
    with pytest.raises(HTTPException) as caught:
        auth.verify_platform_token("Bearer " + token)
    assert (caught.value.status_code, caught.value.detail) == (401, detail)
    ordinary.assert_not_called()
    remote.assert_not_called()


def test_pp1_p05_positive_control(proof):
    claims = auth.verify_platform_token("Bearer " + proof.token)
    assert claims == proof.claims
    assert auth.extract_tenant_claims(claims) == {
        "tenant_id": "tenant", "org_id": "tenant", "tier": "hosted_pro", "roles": None}


def test_pp1_p05_missing_token(proof):
    with pytest.raises(HTTPException) as caught:
        auth.verify_platform_token(None)
    assert (caught.value.status_code, caught.value.detail) == (401, "missing bearer token (Authorization header)")


def test_pp1_p05_expired(proof, monkeypatch):
    now = int(time.time())
    refused(signed(proof, iat=now - 1000, nbf=now - 1000, exp=now - 1), monkeypatch)


def test_pp1_p05_not_yet_valid(proof, monkeypatch):
    refused(signed(proof, nbf=int(time.time()) + 600), monkeypatch)


def test_pp1_p05_wrong_key_same_kid(proof, monkeypatch):
    other = identity.FixtureIssuer.create("another-run")
    token = jwt.encode(proof.claims, other._private_key, algorithm="RS256", headers={"kid": proof.issuer.kid})
    refused(token, monkeypatch)


def test_pp1_p05_wrong_issuer(proof, monkeypatch):
    refused(signed(proof, iss="https://ordinary.invalid/"), monkeypatch)


def test_pp1_p05_wrong_audience(proof, monkeypatch):
    refused(signed(proof, aud="wrong-audience"), monkeypatch)


def test_pp1_p05_alg_none(proof, monkeypatch):
    refused(jwt.encode(proof.claims, key="", algorithm="none", headers={"kid": proof.issuer.kid}), monkeypatch)


def test_pp1_p05_hs256_public_key(proof, monkeypatch):
    # Build the forgery directly: PyJWT correctly refuses PEM HMAC secrets at mint time.
    public = proof.issuer._private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    enc = lambda value: base64.urlsafe_b64encode(value).rstrip(b"=")
    head = enc(json.dumps({"alg": "HS256", "kid": proof.issuer.kid}).encode())
    body = enc(json.dumps(proof.claims).encode())
    message = head + b"." + body
    token = (message + b"." + enc(hmac.new(public, message, hashlib.sha256).digest())).decode()
    refused(token, monkeypatch)


def test_pp1_p05_kid_absent(proof, monkeypatch):
    refused(jwt.encode(proof.claims, proof.issuer._private_key, algorithm="RS256"), monkeypatch)


def test_pp1_p05_kid_mismatched(proof, monkeypatch):
    refused(jwt.encode(proof.claims, proof.issuer._private_key, algorithm="RS256", headers={"kid": "wrong"}), monkeypatch)


def test_pp1_p05_lifetime_over_limit(proof, monkeypatch):
    refused(signed(proof, exp=proof.claims["iat"] + 3601), monkeypatch)


def test_pp1_p05_subject_without_prefix(proof, monkeypatch):
    refused(signed(proof, sub="ordinary|owner"), monkeypatch)


def confinement(proof, monkeypatch, name, value):
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(auth, "Path", Mock(side_effect=AssertionError("file access before confinement")))
    monkeypatch.setattr(auth.importlib.util, "spec_from_file_location",
                        Mock(side_effect=AssertionError("policy imported before confinement")))
    refused(proof.token, monkeypatch, "fixture identity is not permitted in this runtime")


def test_pp1_p06_profile_unset(proof, monkeypatch):
    confinement(proof, monkeypatch, "LEAF_PROOF_PROFILE", None)


def test_pp1_p06_profile_account(proof, monkeypatch):
    confinement(proof, monkeypatch, "LEAF_PROOF_PROFILE", "account")


def test_pp1_p06_posture_unset(proof, monkeypatch):
    confinement(proof, monkeypatch, "LEAF_RUNTIME_ENV", None)


def test_pp1_p06_posture_staging(proof, monkeypatch):
    confinement(proof, monkeypatch, "LEAF_RUNTIME_ENV", "staging")


def test_pp1_p06_posture_production(proof, monkeypatch):
    confinement(proof, monkeypatch, "LEAF_RUNTIME_ENV", "production")


def test_pp1_p06_posture_empty(proof, monkeypatch):
    confinement(proof, monkeypatch, "LEAF_RUNTIME_ENV", "")


def test_pp1_p06_run_id_mismatch(proof, monkeypatch):
    monkeypatch.setenv("LEAF_PROOF_PROJECT_RUN_ID", "another-run")
    refused(proof.token, monkeypatch)


def test_pp1_p06_thumbprint_mismatch(proof, monkeypatch):
    monkeypatch.setenv("LEAF_PROOF_PROJECT_KEY_SHA256", "wrong")
    refused(proof.token, monkeypatch)


def test_pp1_p06_two_keys(proof, monkeypatch):
    jwks = proof.issuer.public_jwks()
    jwks["keys"].append(dict(jwks["keys"][0]))
    proof.path.write_text(json.dumps(jwks), encoding="utf-8")
    refused(proof.token, monkeypatch)


@pytest.mark.parametrize("member", ["d", "p", "q", "dp", "dq", "qi", "oth"])
def test_pp1_p06_private_member(proof, monkeypatch, member):
    jwks = proof.issuer.public_jwks()
    jwks["keys"][0][member] = "forbidden"
    proof.path.write_text(json.dumps(jwks), encoding="utf-8")
    refused(proof.token, monkeypatch)


def test_pp1_p06_policy_absent(proof, monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "__file__", str(tmp_path / "server" / "auth.py"))
    assert all(os.environ.get(k) for k in proof.issuer.profile_env(proof.path))
    refused(proof.token, monkeypatch, "fixture identity is not available in this runtime")


def ordinary_token(proof, monkeypatch, *, subject="auth0|ordinary"):
    monkeypatch.setenv("LEAF_AUTH0_JWKS_FILE", str(proof.path))
    monkeypatch.setenv("LEAF_AUTH0_ISSUER", auth.DEFAULT_ISSUER)
    monkeypatch.setenv("LEAF_AUTH0_AUDIENCE", auth.DEFAULT_AUDIENCE)
    return signed(proof, iss=auth.DEFAULT_ISSUER, aud=auth.DEFAULT_AUDIENCE, sub=subject)


def test_pp1_p06_fixture_subject_ordinary_key(proof, monkeypatch):
    token = ordinary_token(proof, monkeypatch, subject=identity.SUBJECT_PREFIX + "ordinary-key")
    refused(token, monkeypatch)


def test_pp1_p06_ordinary_token_untouched(proof, monkeypatch):
    token = ordinary_token(proof, monkeypatch)
    original_path = auth.Path
    paths = []
    def path_spy(path):
        paths.append(str(path))
        assert str(path) == str(proof.path)
        return original_path(path)
    monkeypatch.setattr(auth, "Path", path_spy)
    loader = Mock(side_effect=AssertionError("ordinary token imported fixture module"))
    monkeypatch.setattr(auth.importlib.util, "spec_from_file_location", loader)
    assert auth.verify_platform_token("Bearer " + token)["sub"] == "auth0|ordinary"
    assert paths == [str(proof.path)]
    loader.assert_not_called()


@pytest.mark.parametrize("claim,prefix", [("iss", identity.ISSUER_PREFIX), ("sub", identity.SUBJECT_PREFIX)])
def test_pp1_p06_namespace_casefold(proof, monkeypatch, claim, prefix):
    claims = {"iss": auth.DEFAULT_ISSUER, "sub": "auth0|ordinary", claim: prefix.upper() + "case"}
    refused(signed(proof, **claims), monkeypatch)


def test_pp1_p06_both_dependency_guards(proof, monkeypatch):
    import deps
    from app import app  # establishes the existing leaf_platform alias
    from leaf_platform import deps as platform_deps
    del app
    for module, invoke in (
        (auth, lambda: deps.require_tenant(Request({"type": "http", "method": "GET", "path": "/api/tools", "headers": []}),
                                          authorization="Bearer " + proof.token, x_tenant_id=None,
                                          x_dispatch_secret=None, x_guest_session=None)),
        (platform_deps._server_auth(), lambda: platform_deps.get_write_binding_id(
            authorization="Bearer " + proof.token)),
    ):
        spy = Mock(side_effect=HTTPException(401, "pp1 guard spy"))
        with monkeypatch.context() as scoped:
            scoped.setattr(module, "verify_platform_token", spy)
            with pytest.raises(HTTPException, match="pp1 guard spy"):
                invoke()
            spy.assert_called_once_with("Bearer " + proof.token)


def test_pp1_p06_constants_equal():
    assert auth.ISSUER_PREFIX == identity.ISSUER_PREFIX == "https://proof-project.leaf.invalid/"
    assert auth.SUBJECT_PREFIX == identity.SUBJECT_PREFIX == "leaf-proof-project|"


def test_pp1_p06_dockerignore():
    from test_postgres_container_wiring import _is_dockerignored
    lines = (ROOT / ".dockerignore").read_text().splitlines()
    for pattern in ("scripts/proof_project*.py", "scripts/test_proof_project*.py", ".proof-project-runs/"):
        assert pattern in lines
    for path in ("scripts/proof_project_identity.py", "scripts/proof_project_pg.py",
                 "scripts/test_proof_project_identity.py", ".proof-project-runs"):
        assert _is_dockerignored(path)
    assert not _is_dockerignored("server/tests/test_project_proof_identity.py")


def test_pp1_p06_dockerfiles():
    from test_postgres_container_wiring import _instructions
    paths = sorted((ROOT / "deploy").glob("Dockerfile*"))
    paths += [p for p in (ROOT / "deploy/e2b.Dockerfile", ROOT / "e2b.Dockerfile") if p.exists()]
    assert len(paths) >= 7
    for path in paths:
        for kind, body in _instructions(path.read_text(encoding="utf-8")):
            if kind in {"COPY", "ADD"}:
                # Explicit source pins below also catch broad COPY scripts/ or COPY .
                assert "proof_project" not in body
                assert not body.startswith(("scripts/ ", ". "))
    app = (ROOT / "deploy/Dockerfile.app").read_text(encoding="utf-8")
    script_copies = [line.split()[1] for line in app.splitlines() if line.startswith("COPY scripts/")]
    assert script_copies == ["scripts/reconcile_" + name + "_authority.py"
                             for name in ("customization", "sessions", "drawing", "upload")]


@pytest.mark.parametrize("run_id", ["", "short", "UPPER-case", "bad/slash", "-leading", "x" * 65, "valid-run\n"])
def test_pp1_p06_invalid_run_id(run_id):
    with pytest.raises(identity.FixtureRefused) as caught:
        identity.FixtureIssuer.create(run_id)
    assert caught.value.code == "run_id"


def test_pp1_p05_issuer_contract(proof, tmp_path):
    issuer = proof.issuer
    second = identity.FixtureIssuer.create(issuer.run_id)
    assert second.kid != issuer.kid
    jwk = issuer.public_jwks()["keys"][0]
    canonical = json.dumps({key: jwk[key] for key in ("e", "kty", "n")}, sort_keys=True, separators=(",", ":"))
    thumbprint = base64.urlsafe_b64encode(hashlib.sha256(canonical.encode()).digest()).rstrip(b"=").decode()
    assert issuer.kid == issuer.thumbprint == jwk["kid"] == thumbprint
    assert set(jwk) == {"kty", "key_ops", "n", "e", "alg", "use", "kid"}
    before = proof.path.read_bytes()
    with pytest.raises(FileExistsError):
        second.write_public_jwks(proof.path)
    assert proof.path.read_bytes() == before
    assert not list(tmp_path.glob(".proof-project-*"))
    assert set(issuer.profile_env(proof.path)) == {
        "LEAF_PROOF_PROFILE", "LEAF_PROOF_PROJECT_RUN_ID",
        "LEAF_PROOF_PROJECT_KEY_SHA256", "LEAF_PROOF_PROJECT_JWKS_FILE"}
    for lifetime in (59, 3601):
        with pytest.raises(ValueError):
            issuer.mint("owner", tenant_id="t", org_id="o", tier="hosted_pro", claim_ns="custom", lifetime_s=lifetime)
    for lifetime in (60, 3600):
        token = issuer.mint("owner", tenant_id="t", org_id="o", tier="hosted_pro", claim_ns="custom", lifetime_s=lifetime)
        claims = auth.verify_platform_token("Bearer " + token)
        assert claims["exp"] - claims["iat"] == lifetime
        assert claims["custom/tenant_id"] == "t"


@pytest.mark.parametrize("claim", ["exp", "iat", "sub", "iss", "aud"])
def test_pp1_p05_required_claim(proof, monkeypatch, claim):
    claims = dict(proof.claims)
    del claims[claim]
    refused(jwt.encode(claims, proof.issuer._private_key, algorithm="RS256", headers={"kid": proof.issuer.kid}), monkeypatch)


@pytest.mark.parametrize("posture", ["local", " LOCAL ", " Test "])
def test_pp1_p06_local_posture(proof, monkeypatch, posture):
    monkeypatch.setenv("LEAF_RUNTIME_ENV", posture)
    assert auth.verify_platform_token("Bearer " + proof.token)["sub"] == proof.claims["sub"]


@pytest.mark.parametrize("profile", ["PROJECT", " project", "project "])
def test_pp1_p06_profile_exact(proof, monkeypatch, profile):
    confinement(proof, monkeypatch, "LEAF_PROOF_PROFILE", profile)


@pytest.mark.parametrize("field,value", [("alg", "HS256"), ("use", "enc"), ("kid", "wrong")])
def test_pp1_p06_jwk_metadata(proof, monkeypatch, field, value):
    jwks = proof.issuer.public_jwks()
    jwks["keys"][0][field] = value
    proof.path.write_text(json.dumps(jwks), encoding="utf-8")
    refused(proof.token, monkeypatch)


def test_pp1_p06_policy_load_failure(proof, monkeypatch):
    monkeypatch.setattr(auth.importlib.util, "spec_from_file_location", Mock(side_effect=RuntimeError("private diagnostic")))
    refused(proof.token, monkeypatch, "fixture identity is not available in this runtime")


@pytest.fixture
def authority(proof, monkeypatch):
    from app import app
    from leaf_platform import db, store
    db.apply_migration()
    suffix = uuid.uuid4().hex
    org = store.create_org("PP1 " + suffix, tier="hosted_pro")
    binding = store.create_identity_binding(org.org_id, "auth0", identity.SUBJECT_PREFIX + suffix, role="owner")
    def headers(subject=suffix, tenant=None):
        token = proof.issuer.mint(subject, tenant_id=str(tenant or org.org_id), org_id=str(org.org_id),
                                  tier="hosted_pro", claim_ns=auth.claim_ns())
        return {"Authorization": "Bearer " + token}
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, org=org, binding=binding, store=store,
                              db=db, headers=headers, suffix=suffix)
    db.reset_pool()


@database
def test_pp1_p07_tenant_read(authority):
    response = authority.client.get("/api/tools", headers=authority.headers())
    assert response.status_code == 200, response.text
    assert isinstance(response.json()["tools"], list)


@database
def test_pp1_p07_owner_project_create(authority):
    response = authority.client.post("/api/projects", headers=authority.headers(), json={"name": "PP1 project"})
    assert response.status_code == 200, response.text
    assert response.json()["project"]["org_id"] == str(authority.org.org_id)


@database
def test_pp1_p07_revoked_binding(authority):
    headers = authority.headers()
    assert authority.client.get("/api/tools", headers=headers).status_code == 200
    with authority.db.cursor() as cur:
        cur.execute("UPDATE identity_bindings SET status = 'revoked', revoked_at = NOW() WHERE binding_id = %s", (authority.binding.binding_id,))
    for response in (
        authority.client.get("/api/tools", headers=headers),
        authority.client.post("/api/projects", headers=headers, json={"name": "Revoked denied"}),
    ):
        assert response.status_code == 403, response.text
        assert response.json()["error"]["error_code"] == "FORBIDDEN"


@database
def test_pp1_p07_no_binding(authority):
    headers = authority.headers(subject=uuid.uuid4().hex)
    for response in (
        authority.client.get("/api/tools", headers=headers),
        authority.client.post("/api/projects", headers=headers, json={"name": "Unbound denied"}),
    ):
        assert response.status_code == 403, response.text
        assert response.json()["error"]["error_code"] == "FORBIDDEN"


@database
def test_pp1_p07_viewer_mutation(authority):
    suffix = uuid.uuid4().hex
    authority.store.create_identity_binding(authority.org.org_id, "auth0", identity.SUBJECT_PREFIX + suffix, role="read_only")
    response = authority.client.post("/api/projects", headers=authority.headers(subject=suffix), json={"name": "Denied viewer"})
    assert response.status_code == 403, response.text
    assert response.json()["error"]["error_code"] == "FORBIDDEN"
    assert authority.store.list_projects(authority.org.org_id) == []


@database
def test_pp1_p07_tenant_claim_mismatch(authority):
    other = authority.store.create_org("PP1 other " + uuid.uuid4().hex)
    response = authority.client.get("/api/tools", headers=authority.headers(tenant=other.org_id))
    assert response.status_code == 409, response.text
    assert response.json()["error"]["error_code"] == "INTERNAL"


@database
def test_pp1_p07_forged_headers(authority):
    other = authority.store.create_org("PP1 forged " + uuid.uuid4().hex)
    binding = authority.store.create_identity_binding(other.org_id, "auth0", identity.SUBJECT_PREFIX + uuid.uuid4().hex, role="owner")
    headers = {**authority.headers(), "X-Org-Id": str(other.org_id), "X-Actor-Binding-Id": str(binding.binding_id)}
    response = authority.client.post("/api/projects", headers=headers, json={"name": "Scoped project"})
    assert response.status_code == 200, response.text
    project = response.json()["project"]
    assert project["org_id"] == str(authority.org.org_id)
    assert authority.store.list_projects(other.org_id) == []
    with authority.db.cursor() as cur:
        cur.execute("SELECT binding_id FROM project_member_bindings WHERE project_id = %s", (uuid.UUID(project["project_id"]),))
        assert [row["binding_id"] for row in cur.fetchall()] == [authority.binding.binding_id]
