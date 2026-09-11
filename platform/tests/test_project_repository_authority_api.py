"""Existing-project repository identity API contract for later Forgejo setup."""
import json
import time
import uuid
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from leaf_platform import api, project_lifecycle, store


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    """This module injects storage and never needs the parent DB migration."""
    yield


@pytest.fixture
def harness(monkeypatch, tmp_path):
    org, project, binding = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    state = SimpleNamespace(
        org=org, project=project, binding=binding, role="editor",
        identity_active=True, member_active=True, status="active",
        visible=True, authority=None, calls=[],
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="repository-authority", alg="RS256", use="sig")
    jwks = tmp_path / "jwks.json"
    jwks.write_text(json.dumps({"keys": [jwk]}), encoding="utf-8")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_AUTH0_ISSUER", "https://authority.example/")
    monkeypatch.setenv("LEAF_AUTH0_AUDIENCE", "https://authority.example/api")
    monkeypatch.setenv("LEAF_AUTH0_JWKS_FILE", str(jwks))
    monkeypatch.setenv("LEAF_TENANT_CLAIM_NS", "https://leafdesign.ai/")
    now = int(time.time())
    token = jwt.encode(
        {"iss": "https://authority.example/", "aud": "https://authority.example/api",
         "sub": "authority-member", "iat": now, "exp": now + 3600,
         "https://leafdesign.ai/tenant_id": "authority-tenant"},
        key.private_bytes(serialization.Encoding.PEM,
                          serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()),
        algorithm="RS256", headers={"kid": "repository-authority"},
    )
    state.headers = {"Authorization": "Bearer " + token}

    def identity(authority, subject):
        state.calls.append("identity")
        assert (authority, subject) == ("auth0", "authority-member")
        return (SimpleNamespace(platform_tenant_id=org, binding_id=binding)
                if state.identity_active else None)

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, params=None):
            self.row = None
            if sql.startswith("SET LOCAL"):
                return
            assert params["org_id"] == org
            if "FROM live_projects" in sql:
                state.calls.append("role_project")
                if (state.visible and params["project_id"] == project
                        and state.status != "deleted"):
                    self.row = {"project_id": project, "org_id": org}
            elif "FROM identity_bindings" in sql:
                state.calls.append("role_identity")
                assert params["binding_id"] == binding
                if state.identity_active:
                    self.row = {"role": "owner"}
            elif "FROM project_member_bindings" in sql:
                state.calls.append("membership")
                assert params["binding_id"] == binding
                assert params["project_id"] == project
                if state.member_active:
                    self.row = {"role": state.role}
            else:
                raise AssertionError(sql)

        def fetchone(self):
            return self.row

    def transaction(operation, **kwargs):
        return operation(SimpleNamespace(cursor=Cursor))

    def get_project(org_id, project_id):
        state.calls.append("active_project")
        assert (org_id, project_id) == (org, project)
        return SimpleNamespace(status=state.status) if state.visible else None

    def resolve(tenant_id, organization_id, project_id):
        state.calls.append("resolve")
        assert (tenant_id, organization_id, project_id) == (org, org, project)
        return state.authority

    def ensure(tenant_id, organization_id, project_id):
        state.calls.append("ensure")
        assert (tenant_id, organization_id, project_id) == (org, org, project)
        if state.authority is None:
            state.authority = dict(tenant_id=str(org), organization_id=str(org),
                                   project_id=str(project), repo_key=str(uuid.uuid4()))
        return state.authority

    monkeypatch.setattr(store, "resolve_active_identity_binding", identity)
    monkeypatch.setattr(project_lifecycle, "run_transaction", transaction)
    monkeypatch.setattr(store, "get_project", get_project)
    monkeypatch.setattr(store, "resolve_project_repository_authority", resolve)
    monkeypatch.setattr(store, "ensure_project_repository_authority", ensure)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        state.client = client
        state.url = f"/api/projects/{project}/repository-authority"
        yield state


def request(h, method, **kwargs):
    return h.client.request(method, h.url, headers=h.headers,
                            **({"json": {}} if method == "POST" else {}), **kwargs)


def test_get_absent_does_not_mint_and_post_replays(harness):
    h = harness
    assert request(h, "GET").status_code == 404
    assert h.authority is None
    assert h.calls == ["identity", "role_project", "role_identity", "membership",
                       "active_project", "resolve"]
    h.calls.clear()
    first = request(h, "POST")
    assert first.status_code == 200, first.text
    assert h.calls == ["identity", "role_project", "role_identity", "membership",
                       "active_project", "ensure"]
    assert request(h, "POST").json() == first.json()
    assert request(h, "GET").json() == first.json()
    assert h.calls.count("ensure") == 2
    assert set(first.json()) == {"authority"}
    assert set(first.json()["authority"]) == {
        "tenant_id", "organization_id", "project_id", "repo_key"}


@pytest.mark.parametrize("role", ["owner", "editor", "reviewer", "read_only"])
def test_membership_controls_read_and_write(harness, role):
    h = harness
    assert request(h, "POST").status_code == 200
    h.role = role
    assert request(h, "GET").status_code == 200
    h.calls.clear()
    response = request(h, "POST")
    assert response.status_code == (200 if role in {"owner", "editor"} else 403)
    assert ("ensure" in h.calls) == (role in {"owner", "editor"})


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("failure", ["foreign", "missing", "deleted", "inactive", "revoked", "wrong_role"])
def test_unavailable_or_denied_project_never_reaches_authority(harness, method, failure):
    h = harness
    expected = 404
    if failure == "foreign":
        h.url = f"/api/projects/{uuid.uuid4()}/repository-authority"
    elif failure == "missing":
        h.visible = False
    elif failure in {"deleted", "inactive"}:
        h.status = failure
    elif failure == "revoked":
        h.member_active = False
        expected = 403
    else:
        h.role = "unknown"
        expected = 403
    assert request(h, method).status_code == expected
    assert "ensure" not in h.calls and "resolve" not in h.calls


@pytest.mark.parametrize("field", ["org_id", "tenant_id", "organization_id", "project_id", "repo_key", "binding_id"])
def test_body_cannot_override_authority(harness, field):
    h = harness
    response = h.client.post(h.url, headers=h.headers, json={field: str(uuid.uuid4())})
    assert response.status_code == 422
    assert "ensure" not in h.calls and "membership" not in h.calls


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("authorization", [None, "Bearer invalid"])
def test_live_auth_rejects_before_storage(harness, method, authorization):
    h = harness
    headers = {"X-Org-Id": str(h.org), "X-Actor-Binding-Id": str(h.binding)}
    if authorization:
        headers["Authorization"] = authorization
    response = h.client.request(method, h.url, headers=headers, json={})
    assert response.status_code == 401
    assert h.calls == []


def test_verified_identity_ignores_forged_headers_and_revocation(harness):
    h = harness
    h.headers.update({"X-Org-Id": str(uuid.uuid4()),
                      "X-Actor-Binding-Id": str(uuid.uuid4())})
    response = request(h, "POST")
    assert response.status_code == 200, response.text
    assert response.json()["authority"]["organization_id"] == str(h.org)
    h.identity_active = False
    h.calls.clear()
    assert request(h, "GET").status_code == 403
    assert h.calls == ["identity"]


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("defect", ["extra", "missing", "invalid", "noncanonical", "not_mapping",
                                    "uuid_object",
                                    "tenant_id", "organization_id", "project_id"])
def test_malformed_or_foreign_mapping_is_sanitized(harness, monkeypatch, method, defect):
    h = harness
    value = dict(tenant_id=str(h.org), organization_id=str(h.org),
                 project_id=str(h.project), repo_key=str(uuid.uuid4()))
    if defect == "extra":
        value["secret"] = "backend-secret"
    elif defect == "missing":
        del value["repo_key"]
    elif defect == "invalid":
        value["repo_key"] = "backend-secret"
    elif defect == "noncanonical":
        value["repo_key"] = value["repo_key"].replace("-", "")
    elif defect == "not_mapping":
        value = [value]
    elif defect == "uuid_object":
        value["repo_key"] = uuid.uuid4()
    else:
        value[defect] = str(uuid.uuid4())
    target = "ensure" if method == "POST" else "resolve"
    monkeypatch.setattr(store, target + "_project_repository_authority", lambda *args: value)
    response = request(h, method)
    assert response.status_code == 503
    assert response.json() == {"detail": "repository authority unavailable"}


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("stage", ["role", "project", "authority"])
def test_backend_errors_are_sanitized(harness, monkeypatch, method, stage):
    def fail(*args, **kwargs):
        raise RuntimeError("backend-secret password=never-expose")

    if stage == "role":
        monkeypatch.setattr(project_lifecycle, "run_transaction", fail)
    elif stage == "project":
        monkeypatch.setattr(store, "get_project", fail)
    else:
        target = "ensure" if method == "POST" else "resolve"
        monkeypatch.setattr(store, target + "_project_repository_authority", fail)
    response = request(harness, method)
    assert response.status_code == 503
    assert response.json() == {"detail": "repository authority unavailable"}
