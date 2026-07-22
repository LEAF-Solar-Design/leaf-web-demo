from datetime import datetime, timedelta, timezone
import uuid

import pytest

from leaf_platform import access, store
from leaf_platform.db import cursor


def _project(make_org, name="Share project"):
    org = make_org(name)
    project = store.create_project(org.org_id, name)
    owner = store.create_identity_binding(org.org_id, "auth0", f"auth0|{name}-owner", role="owner")
    return org, project, owner


def test_share_grant_is_hashed_expiring_revocable_and_audited(make_org):
    org, project, owner = _project(make_org)
    start = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    grant = access.create_project_share_grant(
        org.org_id, project.project_id, owner.binding_id,
        role="reviewer", ttl_seconds=120, now=start)
    resolved = access.resolve_project_share_token(grant["token"], now=start)
    assert resolved["org_id"] == str(org.org_id)
    assert resolved["project_id"] == str(project.project_id)
    assert resolved["role"] == "reviewer"
    assert access.resolve_project_share_token(
        grant["token"], now=start + timedelta(seconds=121)) is None
    assert access.revoke_project_share_grant(
        org.org_id, project.project_id, uuid.UUID(grant["grant_id"]), owner.binding_id,
        now=start + timedelta(seconds=1)) == "revoked"
    assert access.resolve_project_share_token(grant["token"], now=start + timedelta(seconds=2)) is None
    assert access.revoke_project_share_grant(
        org.org_id, project.project_id, uuid.UUID(grant["grant_id"]), owner.binding_id,
        now=start + timedelta(seconds=3)) == "duplicate"
    with cursor() as cur:
        cur.execute("SELECT token_digest FROM project_share_grants WHERE grant_id = %(id)s",
                    {"id": grant["grant_id"]})
        assert cur.fetchone()["token_digest"] != grant["token"]
        cur.execute("SELECT event_type FROM outbox_entries WHERE aggregate_id = %(id)s ORDER BY event_type",
                    {"id": grant["grant_id"]})
        assert [row["event_type"] for row in cur.fetchall()] == [
            "share.grant.created", "share.grant.revoked"]


def test_share_grant_admin_and_tenant_boundaries(make_org):
    org, project, owner = _project(make_org, "Share owner")
    reviewer = store.create_identity_binding(
        org.org_id, "auth0", "auth0|share-reviewer", role="reviewer")
    other, other_project, other_owner = _project(make_org, "Share other")
    for actor in (reviewer.binding_id, other_owner.binding_id):
        with pytest.raises(PermissionError):
            access.create_project_share_grant(
                org.org_id, project.project_id, actor, ttl_seconds=60)
    grant = access.create_project_share_grant(
        org.org_id, project.project_id, owner.binding_id, ttl_seconds=60)
    assert access.revoke_project_share_grant(
        other.org_id, other_project.project_id, uuid.UUID(grant["grant_id"]),
        other_owner.binding_id) == "missing"
    with pytest.raises(ValueError):
        access.resolve_project_share_token("short")


def test_share_grant_api_returns_token_once_and_revokes_immediately(client, make_org):
    org, project, owner = _project(make_org, "Share API")
    headers = {"X-Org-Id": str(org.org_id),
               "X-Actor-Binding-Id": str(owner.binding_id)}
    created = client.post(
        f"/api/projects/{project.project_id}/share-grants",
        headers=headers, json={"role": "read_only", "ttl_seconds": 300})
    assert created.status_code == 201, created.text
    grant = created.json()["grant"]
    assert grant["role"] == "read_only"
    resolved = client.post("/api/share-grants/resolve", json={"token": grant["token"]})
    assert resolved.status_code == 200
    assert "token" not in resolved.json()["grant"]
    revoked = client.delete(
        f"/api/projects/{project.project_id}/share-grants/{grant['grant_id']}",
        headers=headers)
    assert revoked.status_code == 200
    assert client.post(
        "/api/share-grants/resolve", json={"token": grant["token"]}).status_code == 404


@pytest.mark.parametrize("role", ["owner", "editor", "admin", ""])
def test_share_grant_rejects_mutating_roles(make_org, role):
    org, project, owner = _project(make_org, f"Role {role or 'blank'}")
    with pytest.raises(ValueError):
        access.create_project_share_grant(
            org.org_id, project.project_id, owner.binding_id, role=role)
