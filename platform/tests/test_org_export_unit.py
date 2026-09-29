"""Unit tests for the organisation-wide export (P-153), platform/org_export.py.

Pure unit tests: the lifecycle and the project listing are fakes injected through
``export_org``'s parameters, and the route runs under a FastAPI dependency
override. No database, no DATABASE_URL, no skip markers.
"""
import functools
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from leaf_platform import api, org_export, project_lifecycle


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    """Shadow conftest's session migration: these tests never touch PostgreSQL,
    even on a host where DATABASE_URL happens to be configured."""
    yield


def _projects(org_id, count):
    return [SimpleNamespace(project_id=uuid.uuid4(), org_id=org_id, name=f"p{i}")
            for i in range(count)]


class _FakeLifecycle:
    def __init__(self, fail=()):
        self.calls = []
        self.fail = dict(fail)

    def export_project(self, org_id, project_id, actor_binding_id, *, idempotency_key):
        self.calls.append((org_id, project_id, actor_binding_id, idempotency_key))
        if project_id in self.fail:
            raise self.fail[project_id]
        return {
            "export_sha256": "a" * 64,
            "file_count": 2,
            "member_count": 1,
            "receipt": {"receipt_id": str(uuid.uuid4())},
            "replayed": False,
            "export": {"files": ["big content never copied into the manifest"]},
        }


def _actor(org_id):
    return api._LifecycleActor(org_id, uuid.uuid4())


def test_own_org_returns_every_project_row():
    org_id = uuid.uuid4()
    actor = _actor(org_id)
    projects = _projects(org_id, 3)
    lifecycle = _FakeLifecycle()

    manifest = org_export.export_org(
        org_id, actor, idempotency_key="k-1", lifecycle=lifecycle,
        list_projects=lambda oid: projects if oid == org_id else [],
    )

    assert manifest["org_id"] == str(org_id)
    assert manifest["truncated"] is False
    assert manifest["generated_at"]
    assert [r["project_id"] for r in manifest["projects"]] == [str(p.project_id) for p in projects]
    assert [r["name"] for r in manifest["projects"]] == ["p0", "p1", "p2"]
    for row in manifest["projects"]:
        assert "error" not in row
        assert row["export_ref"]["export_sha256"] == "a" * 64
        assert row["export_ref"]["file_count"] == 2
        assert row["export_ref"]["receipt_id"]
        assert "export" not in row["export_ref"]
    assert [c[3] for c in lifecycle.calls] == ["k-1"] * 3
    assert all(c[0] == org_id and c[2] == actor.binding_id for c in lifecycle.calls)


def test_other_org_actor_is_refused_before_any_read():
    org_id = uuid.uuid4()
    lifecycle = _FakeLifecycle()
    listed = []

    with pytest.raises(org_export.OrgExportForbidden):
        org_export.export_org(
            org_id, _actor(uuid.uuid4()), idempotency_key="k-1", lifecycle=lifecycle,
            list_projects=lambda oid: listed.append(oid) or [],
        )
    assert listed == []
    assert lifecycle.calls == []


def test_one_failing_project_is_reported_and_the_rest_succeed():
    org_id = uuid.uuid4()
    projects = _projects(org_id, 4)
    lifecycle = _FakeLifecycle(fail={
        projects[1].project_id: project_lifecycle.LifecycleForbidden(),
        projects[2].project_id: RuntimeError("secret internal detail"),
    })

    manifest = org_export.export_org(
        org_id, _actor(org_id), idempotency_key="k-2", lifecycle=lifecycle,
        list_projects=lambda oid: projects,
    )

    rows = manifest["projects"]
    assert len(rows) == 4 and len(lifecycle.calls) == 4
    assert rows[1]["error"] == "forbidden" and "export_ref" not in rows[1]
    assert rows[2]["error"] == "export_failed"
    assert "secret internal detail" not in repr(manifest)
    assert "export_ref" in rows[0] and "export_ref" in rows[3]
    assert "error" not in rows[0] and "error" not in rows[3]


def test_more_than_max_projects_sets_truncated_and_stops():
    org_id = uuid.uuid4()
    projects = _projects(org_id, 7)
    lifecycle = _FakeLifecycle()

    manifest = org_export.export_org(
        org_id, _actor(org_id), idempotency_key="k-3", lifecycle=lifecycle,
        list_projects=lambda oid: projects, max_projects=5,
    )

    assert manifest["truncated"] is True
    assert len(manifest["projects"]) == 5
    assert len(lifecycle.calls) == 5
    assert [c[1] for c in lifecycle.calls] == [p.project_id for p in projects[:5]]

    exact = org_export.export_org(
        org_id, _actor(org_id), idempotency_key="k-3", lifecycle=_FakeLifecycle(),
        list_projects=lambda oid: projects[:5], max_projects=5,
    )
    assert exact["truncated"] is False and len(exact["projects"]) == 5


def test_invalid_key_or_bound_is_refused_before_listing():
    org_id = uuid.uuid4()
    listed = []
    lister = lambda oid: listed.append(oid) or []  # noqa: E731
    with pytest.raises(ValueError):
        org_export.export_org(org_id, _actor(org_id), idempotency_key="  ",
                              lifecycle=_FakeLifecycle(), list_projects=lister)
    with pytest.raises(ValueError):
        org_export.export_org(org_id, _actor(org_id), idempotency_key="k",
                              lifecycle=_FakeLifecycle(), list_projects=lister,
                              max_projects=0)
    assert listed == []


def _client(monkeypatch, actor, projects):
    lifecycle = _FakeLifecycle()
    monkeypatch.setattr(org_export, "export_org", functools.partial(
        org_export.export_org, lifecycle=lifecycle, list_projects=lambda oid: projects,
    ))
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api._get_lifecycle_actor] = lambda: actor
    return TestClient(app), lifecycle


def test_route_returns_manifest_and_requires_idempotency_key(monkeypatch):
    org_id = uuid.uuid4()
    projects = _projects(org_id, 2)
    client, lifecycle = _client(monkeypatch, _actor(org_id), projects)

    missing = client.post(f"/api/orgs/{org_id}/export")
    assert missing.status_code == 422
    assert lifecycle.calls == []

    ok = client.post(f"/api/orgs/{org_id}/export", headers={"Idempotency-Key": "k-4"})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["org_id"] == str(org_id)
    assert body["truncated"] is False
    assert [r["project_id"] for r in body["projects"]] == [str(p.project_id) for p in projects]
    assert [c[3] for c in lifecycle.calls] == ["k-4", "k-4"]


def test_route_other_org_actor_gets_403(monkeypatch):
    org_id = uuid.uuid4()
    client, lifecycle = _client(monkeypatch, _actor(uuid.uuid4()), _projects(org_id, 2))

    resp = client.post(f"/api/orgs/{org_id}/export", headers={"Idempotency-Key": "k-5"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "org access denied"
    assert lifecycle.calls == []
