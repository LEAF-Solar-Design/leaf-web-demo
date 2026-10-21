"""Canonical workspace scope through real request handlers and authority seams."""
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from leaf_platform import api, deps, project_lifecycle, store
from leaf_platform.models import IdentityBinding


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    """Storage is injected; this module never needs the parent DB migration."""
    yield


@pytest.fixture
def harness(monkeypatch):
    first = IdentityBinding(
        uuid.uuid4(), "auth0", "auth0|scope-member", uuid.uuid4(), uuid.uuid4(),
    )
    second = IdentityBinding(
        uuid.uuid4(), "auth0", "auth0|other-member", uuid.uuid4(), uuid.uuid4(),
    )
    state = SimpleNamespace(
        identity=first, other_identity=second, project=uuid.uuid4(),
        role="editor", identity_active=True, binding_active=True,
        member_active=True, visible=True, calls=[],
    )
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")

    def verify(authorization):
        state.calls.append("verify")
        # Opaque test inputs, not signed tokens. Only this verifier is replaced;
        # deps._verified_identity still resolves the current durable binding.
        subjects = {
            "Bearer first": first.external_subject,
            "Bearer second": second.external_subject,
        }
        if authorization not in subjects:
            raise HTTPException(401, "invalid bearer")
        return {"sub": subjects[authorization], "org_id": str(second.platform_tenant_id)}

    monkeypatch.setattr(
        deps, "_server_auth", lambda: SimpleNamespace(verify_platform_token=verify),
    )

    def identity(authority, subject):
        state.calls.append("identity")
        assert authority == "auth0"
        if subject == first.external_subject:
            return state.identity if state.identity_active else None
        assert subject == second.external_subject
        return state.other_identity

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, params=None):
            self.row = None
            if sql.startswith("SET LOCAL"):
                return
            if "FROM live_projects" in sql:
                state.calls.append("project")
                if (state.visible and params["org_id"] == first.platform_tenant_id
                        and params["project_id"] == state.project):
                    self.row = {"project_id": state.project, "org_id": first.platform_tenant_id}
            elif "FROM identity_bindings" in sql:
                state.calls.append("binding")
                assert params == {
                    "org_id": first.platform_tenant_id, "binding_id": first.binding_id,
                }
                assert "status = 'active'" in sql
                if state.binding_active:
                    self.row = {"role": "owner"}
            elif "FROM project_member_bindings" in sql:
                state.calls.append("membership")
                assert params == {
                    "org_id": first.platform_tenant_id,
                    "project_id": state.project, "binding_id": first.binding_id,
                }
                assert "status = 'active'" in sql
                if state.member_active:
                    self.row = {"role": state.role}
            else:
                raise AssertionError(sql)

        def fetchone(self):
            return self.row

    def transaction(operation, **kwargs):
        assert kwargs == {"isolation": "repeatable read"}
        return operation(SimpleNamespace(cursor=Cursor))

    monkeypatch.setattr(store, "resolve_active_identity_binding", identity)
    monkeypatch.setattr(project_lifecycle, "run_transaction", transaction)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        state.client = client
        state.url = f"/api/projects/{state.project}/scope"
        state.headers = {"Authorization": "Bearer first"}
        yield state


def expected_scope(h):
    return {
        "tenantId": str(h.identity.platform_tenant_id),
        "projectId": str(h.project),
        "platformUserId": str(h.identity.platform_user_id),
        "bindingId": str(h.identity.binding_id),
        "externalAuthority": "auth0",
        "externalSubject": h.identity.external_subject,
        "projectRole": h.role,
    }


@pytest.mark.parametrize("role", ["owner", "editor", "reviewer", "read_only"])
def test_canonical_scope_is_exact_and_private(harness, role):
    h = harness
    h.role = role
    response = h.client.get(h.url, headers=h.headers)
    assert response.status_code == 200, response.text
    assert response.json() == expected_scope(h)
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "Authorization"
    assert h.calls == ["verify", "identity", "project", "binding", "membership"]


def test_forged_headers_and_body_cannot_select_identity(harness):
    h = harness
    forged = {
        "X-Org-Id": str(h.other_identity.platform_tenant_id),
        "X-Project-Id": str(uuid.uuid4()),
        "X-Actor-Binding-Id": str(h.other_identity.binding_id),
    }
    response = h.client.request(
        "GET", h.url, headers={**h.headers, **forged},
        json={"tenantId": forged["X-Org-Id"], "projectId": forged["X-Project-Id"],
              "platformUserId": str(h.other_identity.platform_user_id),
              "bindingId": forged["X-Actor-Binding-Id"], "projectRole": "owner"},
    )
    assert response.status_code == 200
    assert response.json() == expected_scope(h)


def test_other_tenant_cannot_select_project_with_forged_headers(harness):
    h = harness
    response = h.client.get(h.url, headers={
        "Authorization": "Bearer second",
        "X-Org-Id": str(h.identity.platform_tenant_id),
        "X-Project-Id": str(h.project),
        "X-Actor-Binding-Id": str(h.identity.binding_id),
    })
    assert response.status_code == 404
    assert response.json() == {"detail": "project resource not found"}
    assert h.calls == ["verify", "identity", "project"]


@pytest.mark.parametrize("authorization", [None, "", "Bearer invalid", "Basic first"])
@pytest.mark.parametrize("alternate", [{}, {"X-Demo-Session": "demo"},
                                     {"X-Guest-Session": "guest"},
                                     {"X-Demo-Session": "demo", "X-Guest-Session": "guest"}])
def test_bearer_required_even_with_alternate_credentials(harness, authorization, alternate):
    h = harness
    headers = {
        "X-Org-Id": str(h.identity.platform_tenant_id),
        "X-Project-Id": str(h.project),
        "X-Actor-Binding-Id": str(h.identity.binding_id),
        **alternate,
    }
    if authorization is not None:
        headers["Authorization"] = authorization
    response = h.client.request(
        "GET", h.url, headers=headers,
        json={"authorization": "Bearer first", **expected_scope(h)},
    )
    assert response.status_code == 401
    assert h.calls == ["verify"]


def test_canonical_bearer_wins_over_demo_and_guest_headers(harness):
    h = harness
    response = h.client.get(h.url, headers={
        **h.headers, "X-Demo-Session": "demo", "X-Guest-Session": "guest",
    })
    assert response.status_code == 200
    assert response.json() == expected_scope(h)


@pytest.mark.parametrize("authorization", [None, "Bearer first"])
def test_auth_off_is_unavailable_even_with_dev_headers(harness, monkeypatch, authorization):
    h = harness
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    headers = {
        "X-Org-Id": str(h.identity.platform_tenant_id),
        "X-Project-Id": str(h.project),
        "X-Actor-Binding-Id": str(h.identity.binding_id),
    }
    if authorization:
        headers["Authorization"] = authorization
    response = h.client.get(h.url, headers=headers)
    assert response.status_code == 503
    assert response.json() == {"detail": "workspace scope unavailable"}
    assert h.calls == []


@pytest.mark.parametrize("failure", ["inactive", "binding_revoked", "removed", "unknown_role"])
def test_current_identity_and_membership_required(harness, failure):
    h = harness
    if failure == "inactive":
        h.identity_active = False
    elif failure == "binding_revoked":
        h.binding_active = False
    elif failure == "removed":
        h.member_active = False
    else:
        h.role = "unknown"
    response = h.client.get(h.url, headers=h.headers)
    assert response.status_code == 403
    assert "platformUserId" not in response.json()
    if failure == "inactive":
        assert h.calls == ["verify", "identity"]
    elif failure == "binding_revoked":
        assert h.calls == ["verify", "identity", "project", "binding"]


@pytest.mark.parametrize("failure", ["missing", "deleted"])
def test_missing_or_deleted_project_is_not_found(harness, failure):
    h = harness
    if failure == "missing":
        h.url = f"/api/projects/{uuid.uuid4()}/scope"
    else:
        # The authoritative live_projects view excludes deleted projects.
        h.visible = False
    response = h.client.get(h.url, headers=h.headers)
    assert response.status_code == 404
    assert response.json() == {"detail": "project resource not found"}


def test_role_changes_and_revocation_are_not_cached(harness):
    h = harness
    assert h.client.get(h.url, headers=h.headers).json()["projectRole"] == "editor"
    h.role = "read_only"
    assert h.client.get(h.url, headers=h.headers).json()["projectRole"] == "read_only"
    h.member_active = False
    assert h.client.get(h.url, headers=h.headers).status_code == 403
    assert h.calls.count("identity") == 3
    assert h.calls.count("membership") == 3


@pytest.mark.parametrize("stage", ["identity", "membership"])
def test_database_failures_are_sanitized(harness, monkeypatch, stage):
    def fail(*args, **kwargs):
        raise RuntimeError("backend-secret password=never-expose")

    if stage == "identity":
        monkeypatch.setattr(store, "resolve_active_identity_binding", fail)
    else:
        monkeypatch.setattr(project_lifecycle, "run_transaction", fail)
    response = harness.client.get(harness.url, headers=harness.headers)
    assert response.status_code == 503
    assert response.json() == {"detail": "workspace scope unavailable"}


@pytest.mark.parametrize("changes", [
    {"platform_user_id": "not-a-uuid"},
    {"external_authority": "unknown"},
    {"external_subject": ""},
    {"external_subject": "x" * 501},
    {"external_subject": 123},
])
def test_invalid_stored_identity_is_not_exposed(harness, changes):
    h = harness
    h.identity = replace(h.identity, **changes)
    response = h.client.get(h.url, headers=h.headers)
    assert response.status_code == 503
    assert response.json() == {"detail": "workspace scope unavailable"}
