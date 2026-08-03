"""
server/test_auth.py — automated gate for Concern-1 (Auth0 platform identity).

NO live Auth0: a throwaway RS256 keypair is generated, published as a local
JWKS file, and the verifier (server/auth.py) is pointed at it via
LEAF_AUTH0_JWKS_FILE. Covers:

  UNIT (auth.verify_platform_token / auth.extract_tenant_claims):
    - valid token (+ tenant claim)      -> ACCEPTED, claims extracted
    - tampered signature                -> 401
    - signed by a foreign key           -> 401
    - expired                           -> 401
    - wrong audience                    -> 401
    - wrong issuer                      -> 401
    - missing Authorization / not Bearer-> 401
    - verified but missing tenant claim -> 403 (extract_tenant_claims)

  HTTP matrix (LEAF_AUTH_LIVE=1, TestClient over /api/session):
    - no token                          -> 401
    - valid token + tenant claim        -> 200 + intake + echoed tenant_id/org_id
    - valid token, missing tenant claim -> 403

Run (cwd MUST be server/, to avoid the repo-root platform/ stdlib shadow):
    cd server && python test_auth.py           # -> exit 0
    cd server && python -m pytest test_auth.py -q
    cd server && python -m pytest -q           # whole suite; safe (the live-auth
                                               # env is fixture-scoped to this module)
"""
from __future__ import annotations

# Pin the stdlib `platform` module BEFORE importing deps (which prepends the repo
# root — containing a platform/ package — to sys.path). Neutralizes the shadow.
import platform  # noqa: F401  (import-order guard, intentional)

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Ensure server/ is importable regardless of invocation cwd.
SERVER_DIR = Path(__file__).resolve().parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jwt.algorithms import RSAAlgorithm

# --------------------------------------------------------------------------- #
# local RS256 keypair + JWKS (no network / no live Auth0)
# --------------------------------------------------------------------------- #
ISS = "https://leaf-test.example/"
AUD = "https://api.leaf-test.example"
NS = "https://leafdesign.ai/"
PLATFORM_TENANT_ID = "bccb0d64-04c9-4108-bcc1-f27b8bb3924d"
KID = "leaf-test-key-1"

_priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_priv_pem = _priv.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
)
# a SECOND, unrelated key — its tokens must be rejected (not in the JWKS)
_foreign = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_foreign_pem = _foreign.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
)

_jwk = json.loads(RSAAlgorithm.to_jwk(_priv.public_key()))
_jwk.update({"kid": KID, "alg": "RS256", "use": "sig"})
_TMP = Path(tempfile.mkdtemp(prefix="leaf-auth-test-"))
_JWKS_FILE = _TMP / "jwks.json"
_JWKS_FILE.write_text(json.dumps({"keys": [_jwk]}), encoding="utf-8")

# env that points the verifier + tenant store at the local fixtures and turns
# auth ON. Applied ONLY inside the module-scoped fixture below (pytest) or
# _run_all (script mode) — NEVER at import time: pytest imports every test
# module during collection, before any test runs, so an import-time
# os.environ.update would leak LEAF_AUTH_LIVE=1 into the whole process and
# 401 the entire tests/ suite in a bare `python -m pytest -q` run.
_ENV = {
    "LEAF_AUTH_LIVE": "1",
    "LEAF_AUTH0_ISSUER": ISS,
    "LEAF_AUTH0_AUDIENCE": AUD,
    "LEAF_TENANT_CLAIM_NS": NS,
    "LEAF_AUTH0_JWKS_FILE": str(_JWKS_FILE),
    "LEAF_TENANTS_FILE": str(SERVER_DIR.parent / "data" / "tenants.sample.json"),
}

import auth  # noqa: E402
import deps  # noqa: E402
import tenancy  # noqa: E402


def _active_test_tenant(subject):
    assert subject == "auth0|tester"
    return PLATFORM_TENANT_ID, "hosted_pro"


@pytest.fixture(autouse=True, scope="module")
def _auth_env():
    """Scope the live-auth env to THIS module (same pattern as tests/
    test_wave5.py's live_auth fixture). Teardown restores the prior env and
    drops the cached tenant store so later-collected modules inherit a clean
    process — auth.py/deps.py read the env at call time, so restore is enough."""
    mp = pytest.MonkeyPatch()
    for k, v in _ENV.items():
        mp.setenv(k, v)
    mp.setattr(deps, "resolve_active_platform_tenant_authority", _active_test_tenant)
    tenancy.reset_store()
    yield
    mp.undo()
    tenancy.reset_store()


def mint(*, key=_priv_pem, kid=KID, aud=AUD, iss=ISS, exp_delta=3600,
         include_tenant=True, tenant_id="org_acme_solar",
         org_id=None) -> str:
    now = int(time.time())
    payload = {"iss": iss, "aud": aud, "iat": now, "exp": now + exp_delta, "sub": "auth0|tester"}
    if include_tenant:
        payload[NS + "tenant_id"] = tenant_id
        payload[NS + "org_id"] = tenant_id if org_id is None else org_id
        payload[NS + "tier"] = "hosted_pro"
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": kid})


def bearer(tok: str) -> str:
    return "Bearer " + tok


# --------------------------------------------------------------------------- #
# UNIT tests
# --------------------------------------------------------------------------- #
def test_valid_token_accepted():
    payload = auth.verify_platform_token(bearer(mint()))
    assert payload[NS + "tenant_id"] == "org_acme_solar"
    claims = auth.extract_tenant_claims(payload)
    # `roles` is RAW here (None when the token predates the roles claim);
    # normalization happens at the deps seam (contract/AUTH.md §11.5).
    assert claims == {"tenant_id": "org_acme_solar", "org_id": "org_acme_solar",
                      "tier": "hosted_pro", "roles": None}


def test_tampered_signature_rejected():
    tok = mint()
    head, body, sig = tok.split(".")
    bad = sig[:-2] + ("aa" if sig[-2:] != "aa" else "bb")
    with pytest.raises(HTTPException) as ei:
        auth.verify_platform_token(bearer(f"{head}.{body}.{bad}"))
    assert ei.value.status_code == 401


def test_foreign_key_rejected():
    with pytest.raises(HTTPException) as ei:
        auth.verify_platform_token(bearer(mint(key=_foreign_pem)))
    assert ei.value.status_code == 401


def test_expired_rejected():
    with pytest.raises(HTTPException) as ei:
        auth.verify_platform_token(bearer(mint(exp_delta=-30)))
    assert ei.value.status_code == 401


def test_wrong_audience_rejected():
    with pytest.raises(HTTPException) as ei:
        auth.verify_platform_token(bearer(mint(aud="https://api.wrong.example")))
    assert ei.value.status_code == 401


def test_wrong_issuer_rejected():
    with pytest.raises(HTTPException) as ei:
        auth.verify_platform_token(bearer(mint(iss="https://evil.example/")))
    assert ei.value.status_code == 401


def test_missing_or_bad_authorization_rejected():
    for hdr in (None, "", "Basic abc", "Bearer", "Bearer   "):
        with pytest.raises(HTTPException) as ei:
            auth.verify_platform_token(hdr)
        assert ei.value.status_code == 401, hdr


def test_missing_tenant_claim_rejected_403():
    payload = auth.verify_platform_token(bearer(mint(include_tenant=False)))  # verifies OK
    with pytest.raises(HTTPException) as ei:
        auth.extract_tenant_claims(payload)
    assert ei.value.status_code == 403


# --------------------------------------------------------------------------- #
# HTTP matrix over /api/session (LEAF_AUTH_LIVE=1)
# --------------------------------------------------------------------------- #
def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from envelopes import install_error_handlers
    from routers import session as session_router

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(session_router.router)
    return TestClient(app, raise_server_exceptions=False)


def test_http_session_no_token_401():
    r = _client().get("/api/session")
    assert r.status_code == 401


def test_http_session_valid_token_200_echoes_tenant():
    r = _client().get(
        "/api/session",
        headers={"Authorization": bearer(mint(
            tenant_id=PLATFORM_TENANT_ID, org_id="website_org_cuid"))},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == PLATFORM_TENANT_ID
    assert body["org_id"] == PLATFORM_TENANT_ID
    assert isinstance(body["intake"], dict) and "polylines" in body["intake"]


def _raw_tenant_client():
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/tenant")
    def tenant(tenant_id=Depends(deps.require_tenant)):
        return deps.tenant_echo({}, tenant_id)

    return TestClient(app, raise_server_exceptions=False)


def test_live_tenant_namespace_comes_from_the_active_binding():
    r = _raw_tenant_client().get(
        "/tenant",
        headers={"Authorization": bearer(mint(
            tenant_id=PLATFORM_TENANT_ID, org_id="website_org_cuid"))},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == PLATFORM_TENANT_ID
    assert body["org_id"] == PLATFORM_TENANT_ID
    assert body["tier"] == "hosted_pro"


def test_a_stale_tenant_claim_fails_closed_instead_of_rerouting():
    r = _raw_tenant_client().get(
        "/tenant", headers={"Authorization": bearer(mint())})
    assert r.status_code == 409
    assert r.json()["detail"] == (
        "verified tenant claim conflicts with the active platform binding")


def test_post_login_action_prefers_the_root_platform_tenant_id():
    action = (
        Path(__file__).resolve().parent
        / "auth0-actions"
        / "post-login-add-tenant-claim.js"
    )
    script = (
        "const a=require(process.argv[1]);"
        "const c=a.deriveClaims({user:{app_metadata:{"
        "leaf_platform_tenant_id:'" + PLATFORM_TENANT_ID + "',"
        "leaf:{organization_id:'website_org_cuid',plan:'pro'}}}});"
        "process.stdout.write(JSON.stringify(c));"
    )
    result = subprocess.run(
        ["node", "-e", script, str(action)],
        check=True, capture_output=True, text=True,
    )
    assert json.loads(result.stdout) == {
        "tenant_id": PLATFORM_TENANT_ID,
        "org_id": "website_org_cuid",
        "tier": "hosted_pro",
        "roles": [],
    }


def test_http_session_missing_claim_403():
    r = _client().get("/api/session", headers={"Authorization": bearer(mint(include_tenant=False))})
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# script runner (acceptance: `python test_auth.py` exits 0)
# --------------------------------------------------------------------------- #
def _run_all() -> int:
    # script mode has no pytest fixtures: apply the env directly (process-local,
    # the interpreter exits right after the run so nothing can leak).
    os.environ.update(_ENV)
    tenancy.reset_store()
    original_resolver = deps.resolve_active_platform_tenant_authority
    deps.resolve_active_platform_tenant_authority = _active_test_tenant
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    try:
        for t in tests:
            try:
                t()
                print(f"PASS {t.__name__}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {t.__name__}: {type(exc).__name__}: {exc}")
    finally:
        deps.resolve_active_platform_tenant_authority = original_resolver
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
