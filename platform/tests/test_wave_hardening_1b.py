"""Wave-1 lane 1B security hardening — adversarial acceptance for F6 + F16.

F6 (HIGH): platform/deps.get_org_id must STOP trusting the client X-Org-Id header
in live-auth mode. The caller's org is derived from the VERIFIED Auth0 RS256
session (server/auth.py); a client-supplied X-Org-Id is ignored so it can never
name another tenant's org. POST /api/orgs is gated so it cannot be called
unauthenticated in live-auth mode. The dev seam (header) is preserved ONLY when
auth is off.

F16 (LOW): the admin-token compare in api.offboard uses hmac.compare_digest
(constant-time), preserving the fail-closed-when-env-unset behavior.

Hermetic: a throwaway RS256 keypair + local JWKS (no live Auth0, no network),
mirroring server/tests/test_wave5.py. DB comes from conftest (DATABASE_URL /
platform/.env.local). Env is toggled per-test via monkeypatch (get_org_id and
server/auth.py both read env at call time).

Run:  cd C:/tmp && python -m pytest C:/tmp/leaf-web-demo/platform/tests -q
"""
from __future__ import annotations

import json
import pathlib
import tempfile
import time

import jwt
import pytest

import leaf_platform.store as platform_store
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

# --------------------------------------------------------------------------- #
# local RS256 keypair + JWKS (no live Auth0) — same pattern as test_wave5.py
# --------------------------------------------------------------------------- #
ISS = "https://leaf-1b.example/"
AUD = "https://api.leaf-1b.example"
NS = "https://leafdesign.ai/"
KID = "leaf-1b-key-1"

_priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_priv_pem = _priv.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
)
_jwk = json.loads(RSAAlgorithm.to_jwk(_priv.public_key()))
_jwk.update({"kid": KID, "alg": "RS256", "use": "sig"})
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="leaf-1b-auth-"))
_JWKS_FILE = _TMP / "jwks.json"
_JWKS_FILE.write_text(json.dumps({"keys": [_jwk]}), encoding="utf-8")


def _mint(org_id: str, *, tenant_id: str = "tenant-1b", with_org: bool = True,
          subject: str = "auth0|1b") -> str:
    now = int(time.time())
    payload = {
        "iss": ISS, "aud": AUD, "iat": now, "exp": now + 3600, "sub": subject,
        NS + "tenant_id": tenant_id,
    }
    if with_org:
        payload[NS + "org_id"] = org_id
    return jwt.encode(payload, _priv_pem, algorithm="RS256", headers={"kid": KID})


def _bearer(org_id: str, **kw) -> dict:
    return {"Authorization": "Bearer " + _mint(org_id, **kw)}


def _bind(org, subject: str):
    """Live platform auth resolves the verified subject through this binding."""
    platform_store.create_identity_binding(org.org_id, "auth0", subject)


def _bind_as(org, subject: str, role: str):
    platform_store.create_identity_binding(org.org_id, "auth0", subject, role=role)


@pytest.fixture
def live_auth(monkeypatch):
    """Turn Auth0 verification ON, pointed at the local RS256 JWKS (no network)."""
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_AUTH0_ISSUER", ISS)
    monkeypatch.setenv("LEAF_AUTH0_AUDIENCE", AUD)
    monkeypatch.setenv("LEAF_TENANT_CLAIM_NS", NS)
    monkeypatch.setenv("LEAF_AUTH0_JWKS_FILE", str(_JWKS_FILE))
    yield


@pytest.fixture
def off_auth(monkeypatch):
    """Force the default off-auth demo path (byte-identical dev seam)."""
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    yield


# --------------------------------------------------------------------------- #
# F6 — live auth: the X-Org-Id header is NOT trusted; the verified session wins
# --------------------------------------------------------------------------- #
def test_live_auth_ignores_cross_org_header_exploit_now_blocked(client, make_org, live_auth):
    """EXPLOIT (pre-fix): session for A sets `X-Org-Id: <B>` and reads B's org (200).
    POST-FIX: the header is ignored, caller org == verified A, so reading B -> 404."""
    org_a = make_org(name="Victim-adjacent A")
    org_b = make_org(name="Other-tenant B")
    subject = "auth0|1b-cross-org"
    _bind(org_a, subject)

    # Attacker holds a VALID session for A but forges the header to name B.
    forged = _bearer(str(org_a.org_id), subject=subject)
    forged["X-Org-Id"] = str(org_b.org_id)   # the pre-fix trusted input

    r_cross = client.get(f"/api/orgs/{org_b.org_id}", headers=forged)
    assert r_cross.status_code == 404, r_cross.text   # header NOT trusted -> B invisible

    # The same session reads its OWN org fine (verified claim wins).
    r_self = client.get(
        f"/api/orgs/{org_a.org_id}", headers=_bearer(str(org_a.org_id), subject=subject)
    )
    assert r_self.status_code == 200, r_self.text
    assert r_self.json()["org"]["org_id"] == str(org_a.org_id)


def test_live_auth_project_scope_follows_verified_org_not_header(client, make_org, live_auth):
    """A forged `X-Org-Id: <B>` cannot pivot the project list off the verified org A."""
    org_a = make_org(name="Proj Owner A")
    org_b = make_org(name="Empty B")
    subject = "auth0|1b-project-scope"
    _bind(org_a, subject)

    # create a project as verified A
    r_make = client.post("/api/projects", json={"name": "A-only project"},
                         headers=_bearer(str(org_a.org_id), subject=subject))
    assert r_make.status_code == 200, r_make.text
    pid = r_make.json()["project"]["project_id"]

    # session A, but header forged to B -> still scoped to A (sees A's project)
    forged = _bearer(str(org_a.org_id), subject=subject)
    forged["X-Org-Id"] = str(org_b.org_id)
    r_list = client.get("/api/projects", headers=forged)
    assert r_list.status_code == 200, r_list.text
    assert pid in [p["project_id"] for p in r_list.json()["projects"]]


def test_live_auth_unauthenticated_read_is_401(client, live_auth):
    """No bearer in live-auth mode -> 401 (unauthenticated), never a header fallback."""
    r = client.get("/api/projects")
    assert r.status_code == 401, r.text
    # a bare X-Org-Id header is not a substitute for a verified session
    r2 = client.get("/api/projects", headers={"X-Org-Id": "11111111-1111-1111-1111-111111111111"})
    assert r2.status_code == 401, r2.text


def test_live_auth_verified_token_without_org_claim_is_403(client, live_auth):
    """A verified identity that carries no org claim is authenticated-but-unprovisioned -> 403."""
    tok = _mint("unused", with_org=False, subject="auth0|1b-unbound")
    r = client.get("/api/projects", headers={"Authorization": "Bearer " + tok})
    assert r.status_code == 403, r.text


# --------------------------------------------------------------------------- #
# F6 — POST /api/orgs gate
# --------------------------------------------------------------------------- #
def test_live_auth_post_orgs_unauthenticated_is_rejected(client, live_auth):
    """EXPLOIT (pre-fix): anyone can mint an org unauthenticated. POST-FIX: 401."""
    r = client.post("/api/orgs", json={"name": "sneaky org"})
    assert r.status_code == 401, r.text


def test_live_auth_post_orgs_with_valid_session_succeeds(client, live_auth):
    """A verified caller may still bootstrap an org (org claim not required to mint)."""
    tok = _mint("unused", with_org=False, subject="auth0|1b-bootstrap")
    r = client.post("/api/orgs", json={"name": "legit org"},
                    headers={"Authorization": "Bearer " + tok})
    assert r.status_code == 200, r.text
    assert r.json()["org"]["name"] == "legit org"


def test_off_auth_post_orgs_stays_open_bootstrap(client, off_auth):
    """DEV SEAM preserved: with auth off, POST /api/orgs is ungated (byte-identical)."""
    r = client.post("/api/orgs", json={"name": "demo bootstrap org"})
    assert r.status_code == 200, r.text
    assert r.json()["org"]["name"] == "demo bootstrap org"


# --------------------------------------------------------------------------- #
# F6 — off-auth dev seam unchanged (header still resolves the org)
# --------------------------------------------------------------------------- #
def test_off_auth_header_dev_seam_unchanged(client, make_org, off_auth):
    org = make_org(name="Dev Seam Org")
    hdr = {"X-Org-Id": str(org.org_id)}
    r = client.get(f"/api/orgs/{org.org_id}", headers=hdr)
    assert r.status_code == 200, r.text
    assert r.json()["org"]["org_id"] == str(org.org_id)
    # missing header still 400 (unchanged contract)
    assert client.get("/api/projects").status_code == 400


@pytest.mark.parametrize("role", ["reviewer", "read_only"])
def test_live_auth_read_only_roles_cannot_mutate_canonical_records_or_jobs(client, make_org, live_auth, role):
    org = make_org(name=f"{role} org")
    project = platform_store.create_project(org.org_id, f"{role} project")
    platform_store.set_project_authority_mode(org.org_id, project.project_id, "postgres_canonical")
    subject = f"auth0|{role}-denied"
    _bind_as(org, subject, role)
    headers = _bearer(str(org.org_id), subject=subject)
    headers["Idempotency-Key"] = "reviewer-key"
    assert client.post(f"/api/projects/{project.project_id}/history", json={"payload": {"n": 1}},
                       headers=headers).status_code == 403
    assert client.post(f"/api/projects/{project.project_id}/solves", json={"payload": {"n": 1}},
                       headers=headers).status_code == 403
    assert client.post(f"/api/projects/{project.project_id}/jobs", json={"kind": "run"},
                       headers=headers).status_code == 403


@pytest.mark.parametrize("role", ["owner", "editor"])
def test_live_auth_owner_and_editor_can_mutate_canonical_records_and_jobs(client, make_org, live_auth, role):
    org = make_org(name=f"{role} org")
    project = platform_store.create_project(org.org_id, f"{role} project")
    platform_store.set_project_authority_mode(org.org_id, project.project_id, "postgres_canonical")
    subject = f"auth0|{role}-allowed"
    _bind_as(org, subject, role)
    headers = _bearer(str(org.org_id), subject=subject)
    headers["Idempotency-Key"] = f"{role}-history"
    assert client.post(f"/api/projects/{project.project_id}/history", json={"payload": {"n": 1}},
                       headers=headers).status_code == 200
    headers["Idempotency-Key"] = f"{role}-solve"
    assert client.post(f"/api/projects/{project.project_id}/solves", json={"payload": {"n": 1}},
                       headers=headers).status_code == 405
    assert client.post(f"/api/projects/{project.project_id}/jobs", json={"kind": "run"},
                       headers=headers).status_code == 200


# --------------------------------------------------------------------------- #
# F16 — constant-time admin-token compare, fail-closed-when-unset preserved
# --------------------------------------------------------------------------- #
def test_offboard_uses_constant_time_compare_source(off_auth):
    """The load-bearing proof that the compare is constant-time: the `!=` compare
    is gone and hmac.compare_digest is used."""
    src = (pathlib.Path(__file__).resolve().parent.parent / "api.py").read_text(encoding="utf-8")
    assert "hmac.compare_digest" in src
    assert "if x_admin_token != admin_token" not in src


def test_offboard_admin_token_env_unset_is_503(client, make_org, monkeypatch, off_auth):
    monkeypatch.delenv("PLATFORM_ADMIN_TOKEN", raising=False)
    org = make_org(name="Unset Token Org")
    r = client.delete(f"/api/orgs/{org.org_id}", headers={"X-Admin-Token": "anything"})
    assert r.status_code == 503, r.text   # fail closed when env unset


def test_offboard_wrong_and_missing_admin_token_is_403(client, make_org, monkeypatch, off_auth):
    monkeypatch.setenv("PLATFORM_ADMIN_TOKEN", "s3cret-admin-token")
    org = make_org(name="Wrong Token Org")
    # wrong token -> 403 (constant-time reject)
    r_wrong = client.delete(f"/api/orgs/{org.org_id}", headers={"X-Admin-Token": "not-the-token"})
    assert r_wrong.status_code == 403, r_wrong.text
    # missing header -> 403 (None guarded before compare_digest, no 500)
    r_missing = client.delete(f"/api/orgs/{org.org_id}")
    assert r_missing.status_code == 403, r_missing.text


def test_offboard_correct_admin_token_proceeds(client, make_org, monkeypatch, off_auth):
    """The correct token passes the constant-time compare and offboards the org."""
    monkeypatch.setenv("PLATFORM_ADMIN_TOKEN", "s3cret-admin-token")
    org = make_org(name="Right Token Org")
    r = client.delete(f"/api/orgs/{org.org_id}", headers={"X-Admin-Token": "s3cret-admin-token"})
    assert r.status_code == 200, r.text
    assert r.json()["org_id"] == str(org.org_id)
    assert r.json()["status"] == "deleted"
