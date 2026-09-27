"""PostgreSQL and live-token tests for native first-binding authorization."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import time
from types import SimpleNamespace
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from leaf_platform import binding_grant_issuance as issuance, db, store
from leaf_platform.binding_grant import LocalTestSigner, verify_grant

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="requires DATABASE_URL")
ISSUER = "https://binding-issuance.test/"
AUDIENCE = "https://binding-issuance.test/api"
NAMESPACE = "https://leafdesign.ai/"


@pytest.fixture(scope="module")
def keys():
    return (rsa.generate_private_key(public_exponent=65537, key_size=2048),
            rsa.generate_private_key(public_exponent=65537, key_size=3072))


@pytest.fixture
def setup(client, make_org, monkeypatch, tmp_path, keys):
    auth_key, grant_key = keys
    jwk = json.loads(RSAAlgorithm.to_jwk(auth_key.public_key()))
    jwk.update(kid="binding-auth", alg="RS256", use="sig")
    jwks = tmp_path / "jwks.json"
    jwks.write_text(json.dumps({"keys": [jwk]}), encoding="utf-8")
    for key, value in {
        "LEAF_AUTH_LIVE": "1", "LEAF_AUTH0_ISSUER": ISSUER,
        "LEAF_AUTH0_AUDIENCE": AUDIENCE, "LEAF_TENANT_CLAIM_NS": NAMESPACE,
        "LEAF_AUTH0_JWKS_FILE": str(jwks),
    }.items():
        monkeypatch.setenv(key, value)
    config = issuance.SigningConfig(LocalTestSigner(grant_key), ISSUER)
    client.app.dependency_overrides[issuance.configured_signer] = lambda: config
    org = make_org("Binding workspace")
    project = store.create_project(org.org_id, "Binding project", authority_mode="postgres_canonical")

    def member(role="owner", tenant_role="owner", *, member_project=None, member_org=None):
        target_org = member_org or org.org_id
        subject = f"auth0|binding-{uuid.uuid4()}"
        binding = store.create_identity_binding(target_org, "auth0", subject, role=tenant_role)
        if role:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO project_member_bindings "
                    "(membership_id, org_id, project_id, binding_id, role, invited_by_binding_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (uuid.uuid4(), target_org, member_project or project.project_id,
                     binding.binding_id, role,
                     binding.binding_id if role == "owner" else owner.binding_id),
                )
        now = int(time.time())
        token = jwt.encode({
            "iss": ISSUER, "aud": AUDIENCE, "sub": subject, "iat": now, "exp": now + 3600,
            NAMESPACE + "tenant_id": str(target_org), NAMESPACE + "org_id": str(target_org),
        }, auth_key, algorithm="RS256", headers={"kid": "binding-auth"})
        return binding, {"Authorization": "Bearer " + token}

    def drawing(*, drawing_project=None, drawing_org=None):
        drawing_id, version_id = uuid.uuid4(), uuid.uuid4()
        target_org = drawing_org or org.org_id
        target_project = drawing_project or project.project_id
        with db.cursor() as cur:
            cur.execute("INSERT INTO drawing_artifacts (drawing_id, org_id, project_id, name) "
                        "VALUES (%s, %s, %s, %s)",
                        (drawing_id, target_org, target_project, f"Drawing {drawing_id}"))
            cur.execute("INSERT INTO drawing_versions "
                        "(version_id, drawing_id, org_id, project_id, seq, oss_object) "
                        "VALUES (%s, %s, %s, %s, 1, %s)",
                        (version_id, drawing_id, target_org, target_project, f"objects/{version_id}.dwg"))
        return drawing_id, version_id

    owner, headers = member()
    drawing_id, version_id = drawing()
    def post(*, auth=None, target_project=None, target_version=None, session=None):
        return client.post(
            f"/api/projects/{target_project or project.project_id}/drawing-versions/"
            f"{target_version or version_id}/binding-grants",
            headers=headers if auth is None else auth,
            json={"pluginSessionId": session or str(uuid.uuid4()),
                  "documentFingerprint": "sha256:" + "a" * 64},
        )
    yield SimpleNamespace(org=org, project=project, owner=owner, headers=headers, member=member,
                          drawing=drawing, drawing_id=drawing_id, version_id=version_id,
                          post=post, config=config)
    client.app.dependency_overrides.pop(issuance.configured_signer, None)


@pytest.mark.parametrize("role,tenant_role", [("owner", "owner"), ("editor", "read_only")])
def test_owner_and_invited_editor_succeed(setup, role, tenant_role):
    binding, headers = setup.member(role, tenant_role)
    response = setup.post(auth=headers)
    assert response.status_code == 200, response.text
    claims = verify_grant(response.json()["grant"], setup.config.signer.private_key.public_key(),
                          now=time.time(), expected_iss=ISSUER)
    assert claims.actorBindingId == str(binding.binding_id)
    assert claims.platformTenantId == str(setup.org.org_id)
    assert claims.projectId == str(setup.project.project_id)
    assert claims.drawingId == str(setup.drawing_id)
    assert claims.drawingVersionId == str(setup.version_id)
    assert claims.workspaceName == "Binding workspace"
    assert claims.projectName == "Binding project"
    assert claims.versionName == "Version 1"
    assert response.headers["cache-control"] == "no-store"


def test_read_only_member_is_403(setup):
    _, headers = setup.member("read_only")
    assert setup.post(auth=headers).status_code == 403


def test_nonmember_including_tenant_owner_is_404(setup):
    _, headers = setup.member(None)
    assert setup.post(auth=headers).status_code == 404


def test_cross_tenant_project_and_version_are_generic_404(setup, make_org):
    other = make_org("Other binding org")
    project = store.create_project(other.org_id, "Other project", authority_mode="postgres_canonical")
    _, version = setup.drawing(drawing_project=project.project_id, drawing_org=other.org_id)
    responses = [setup.post(target_project=project.project_id, target_version=version),
                 setup.post(target_version=version), setup.post(target_project=uuid.uuid4())]
    assert all(r.status_code == 404 for r in responses)
    assert len({r.text for r in responses}) == 1


def test_version_from_another_projects_drawing_is_404(setup):
    project = store.create_project(setup.org.org_id, "Other drawing project",
                                   authority_mode="postgres_canonical")
    _, version = setup.drawing(drawing_project=project.project_id)
    assert setup.post(target_version=version).status_code == 404


@pytest.mark.parametrize("table,column,value", [
    ("drawing_versions", "deleted_at", "NOW()"),
    ("drawing_versions", "purge_requested_at", "NOW()"),
    ("drawing_versions", "oss_object", "NULL"),
    ("drawing_artifacts", "status", "'archived'"),
    ("projects", "status", "'archived'"),
    ("projects", "deleted_at", "NOW()"),
    ("orgs", "status", "'offboarding'"),
])
def test_unavailable_material_is_404(setup, table, column, value):
    # SQL identifiers and expressions come only from the fixed cases above.
    key, identity = {
        "drawing_versions": ("version_id", setup.version_id),
        "drawing_artifacts": ("drawing_id", setup.drawing_id),
        "projects": ("project_id", setup.project.project_id),
        "orgs": ("org_id", setup.org.org_id),
    }[table]
    with db.cursor() as cur:
        cur.execute(f"UPDATE {table} SET {column} = {value} WHERE {key} = %s", (identity,))
    assert setup.post().status_code == 404


def test_legacy_authority_is_unavailable(setup):
    store.set_project_authority_mode(setup.org.org_id, setup.project.project_id, "legacy_sqlite")
    assert setup.post().status_code == 404


def test_audit_has_digests_and_no_fingerprint_or_grant(setup):
    response = setup.post()
    assert response.status_code == 200, response.text
    grant = response.json()["grant"]
    claims = verify_grant(grant, setup.config.signer.private_key.public_key(),
                          now=time.time(), expected_iss=ISSUER)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM binding_grant_audit WHERE org_id = %s", (setup.org.org_id,))
        rows = cur.fetchall()
    assert len(rows) == 1
    audit = rows[0]
    assert audit["actor_binding_id"] == setup.owner.binding_id
    assert audit["drawing_id"] == setup.drawing_id
    assert audit["version_id"] == setup.version_id
    assert audit["fingerprint_sha256"] == hashlib.sha256(claims.documentFingerprint.encode()).hexdigest()
    assert audit["nonce_sha256"] == hashlib.sha256(claims.nonce.encode()).hexdigest()
    assert audit["iat"] == claims.iat and audit["exp"] == claims.exp
    assert audit["decision"] == "authorized" and audit["refusal_reason"] is None
    assert audit["key_id"] == setup.config.signer.kid
    serialized = json.dumps(audit, default=str)
    assert claims.documentFingerprint not in serialized and grant not in serialized
    assert claims.nonce not in serialized


def test_expired_during_signing_is_503_and_audit_already_committed(setup, monkeypatch):
    now = [1000]
    original = setup.config.signer.sign
    def delayed(message):
        with db.cursor() as cur:
            cur.execute("SELECT exp FROM binding_grant_audit WHERE org_id = %s", (setup.org.org_id,))
            assert cur.fetchone()["exp"] == 1120
        now[0] = 1120
        return original(message)
    monkeypatch.setattr(setup.config.signer, "sign", delayed)
    issue = issuance.issue_grant
    monkeypatch.setattr(issuance, "issue_grant", lambda *args: issue(*args, clock=lambda: now[0]))
    response = setup.post()
    assert response.status_code == 503
    assert response.json()["detail"] == "binding_grant_expired_during_signing"
    assert "grant" not in response.json()


def test_twelve_parallel_requests_yield_exactly_ten_successes(setup, monkeypatch):
    # Distinct plugin sessions isolate the actor bucket; freeze its minute so
    # scheduling across a wall-clock boundary cannot make this nondeterministic.
    now = int(time.time())
    issue = issuance.issue_grant
    monkeypatch.setattr(issuance, "issue_grant", lambda *args: issue(*args, clock=lambda: now))
    with ThreadPoolExecutor(max_workers=12) as pool:
        responses = list(pool.map(lambda _: setup.post(), range(12)))
    assert sorted(r.status_code for r in responses) == [200] * 10 + [429] * 2
    assert all(int(r.headers["retry-after"]) > 0 for r in responses if r.status_code == 429)
    with db.cursor() as cur:
        cur.execute("SELECT decision, count(*) AS count FROM binding_grant_audit "
                    "WHERE org_id = %s GROUP BY decision", (setup.org.org_id,))
        assert {r["decision"]: r["count"] for r in cur.fetchall()} == {"authorized": 10, "refused": 2}


def test_session_limit_rollback_does_not_spend_actor_budget(setup, monkeypatch):
    now = int(time.time())
    issue = issuance.issue_grant
    monkeypatch.setattr(issuance, "issue_grant", lambda *args: issue(*args, clock=lambda: now))
    session = str(uuid.uuid4())
    assert [setup.post(session=session).status_code for _ in range(4)] == [200, 200, 200, 429]
    assert [setup.post().status_code for _ in range(7)] == [200] * 7
    assert setup.post().status_code == 429


def test_revoked_membership_is_unavailable(setup):
    with db.cursor() as cur:
        cur.execute("UPDATE project_member_bindings SET status = 'revoked', revoked_at = NOW() "
                    "WHERE org_id = %s AND project_id = %s AND binding_id = %s",
                    (setup.org.org_id, setup.project.project_id, setup.owner.binding_id))
    assert setup.post().status_code == 404


def test_invalid_token_and_org_hints_do_not_issue(setup):
    response = setup.post(auth={"Authorization": "Bearer invalid",
                                "X-Org-Id": str(setup.org.org_id),
                                "X-Actor-Binding-Id": str(setup.owner.binding_id)})
    assert response.status_code == 401
