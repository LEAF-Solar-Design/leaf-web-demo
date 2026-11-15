"""Archive/restore/delete HTTP contracts and DB-free exact-identity sweeps."""
from __future__ import annotations

import uuid

import pytest
from psycopg.types.json import Jsonb

from leaf_platform import project_lifecycle as lifecycle, store
from leaf_platform.db import cursor


class FakeSweepStore:
    """Small transactional queue fake; it never opens a database connection."""

    def __init__(self, status="deleted"):
        self.status = status
        self.rows = [dict(object_key="exact/a.bin", object_version_or_sha256="version-17", swept=False),
                     dict(object_key="exact/b.bin", object_version_or_sha256="a" * 64, swept=False)]
        self.result = []

    def run_transaction(self, operation, **kwargs):
        assert kwargs == {"isolation": "read committed", "max_attempts": 1}
        return operation(self)

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, args):
        if sql.startswith("SELECT status"):
            self.result = [{"status": self.status}]
        elif sql.startswith("SELECT object_key"):
            self.result = [dict(row) for row in self.rows if not row["swept"]]
        elif sql.startswith("UPDATE project_artifact_sweep"):
            _, key, identity = args
            for row in self.rows:
                if (row["object_key"], row["object_version_or_sha256"]) == (key, identity):
                    row["swept"] = True
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return list(self.result)


def test_sweep_passes_exact_key_and_version_or_sha256():
    fake = FakeSweepStore()
    calls = []
    assert lifecycle.sweep_project_artifacts(uuid.uuid4(), lambda *args: calls.append(args), store=fake) == 2
    assert calls == [("exact/a.bin", "version-17"), ("exact/b.bin", "a" * 64)]


def test_sweep_is_idempotent():
    fake = FakeSweepStore()
    calls = []
    project_id = uuid.uuid4()
    delete = lambda *args: calls.append(args)
    assert lifecycle.sweep_project_artifacts(project_id, delete, store=fake) == 2
    assert lifecycle.sweep_project_artifacts(project_id, delete, store=fake) == 0
    assert len(calls) == 2


def test_sweep_keeps_failed_row_queued_and_continues():
    fake = FakeSweepStore()
    project_id = uuid.uuid4()

    def delete(key, identity):
        if key == "exact/a.bin":
            raise OSError("temporary backend failure")

    assert lifecycle.sweep_project_artifacts(project_id, delete, store=fake) == 1
    assert [row["swept"] for row in fake.rows] == [False, True]
    calls = []
    assert lifecycle.sweep_project_artifacts(project_id, lambda *args: calls.append(args), store=fake) == 1
    assert calls == [("exact/a.bin", "version-17")]


@pytest.mark.parametrize("status", ["active", "archived"])
def test_sweep_refuses_projects_that_are_not_deleted(status):
    fake = FakeSweepStore(status)
    calls = []
    assert lifecycle.sweep_project_artifacts(uuid.uuid4(), lambda *args: calls.append(args), store=fake) == 0
    assert calls == []
    assert not any(row["swept"] for row in fake.rows)


def _headers(org_id, binding_id, key=None):
    headers = {"X-Org-Id": str(org_id), "X-Actor-Binding-Id": str(binding_id)}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


@pytest.fixture
def project(client, make_org):
    org = make_org("Archive disposable fixture")
    owner = store.create_identity_binding(org.org_id, "auth0", str(uuid.uuid4()), role="owner")
    name = f"Disposable {uuid.uuid4()}"
    response = client.post("/api/projects/blank", json={"name": name},
                           headers=_headers(org.org_id, owner.binding_id, str(uuid.uuid4())))
    assert response.status_code == 201, response.text
    return org.org_id, owner.binding_id, response.json()["project"]["project_id"], name


def _action(client, project, action, key=None, **kwargs):
    org_id, owner_id, project_id, name = project
    headers = _headers(org_id, owner_id, key or str(uuid.uuid4()))
    if action == "delete":
        return client.request("DELETE", f"/api/projects/{project_id}", headers=headers,
                              json=kwargs.pop("json", {"confirm_name": name}), **kwargs)
    return client.post(f"/api/projects/{project_id}/{action}", headers=headers, **kwargs)


@pytest.mark.parametrize("action", ["archive", "restore", "delete"])
def test_foreign_org_cannot_transition_project(client, make_org, project, action):
    foreign = make_org("Archive foreign tenant")
    actor = store.create_identity_binding(foreign.org_id, "auth0", str(uuid.uuid4()), role="owner")
    target = (foreign.org_id, actor.binding_id, project[2], project[3])
    assert _action(client, target, action).status_code == 404


@pytest.mark.parametrize("action", ["archive", "restore", "delete"])
def test_non_owner_member_cannot_transition(client, project, action):
    org_id, owner_id, project_id, name = project
    member = store.create_identity_binding(org_id, "auth0", str(uuid.uuid4()), role="editor")
    lifecycle.invite_project_member(org_id, uuid.UUID(project_id), owner_id,
        member_binding_id=member.binding_id, role="editor", idempotency_key=str(uuid.uuid4()))
    target = (org_id, member.binding_id, project_id, name)
    assert _action(client, target, action).status_code == 403


def test_delete_requires_archive_and_exact_current_name(client, project):
    assert _action(client, project, "delete").status_code == 409
    for body in ({}, {"confirm_name": "wrong"}, {"confirm_name": project[3] + " "}):
        assert _action(client, project, "delete", json=body).status_code == 422
    assert _action(client, project, "archive").status_code == 200
    assert _action(client, project, "delete", json={}).status_code == 422


def test_archive_restore_listing_and_receipt_replay(client, project):
    org_id, owner_id, project_id, _ = project
    archived = _action(client, project, "archive", "archive-once")
    assert archived.status_code == 200, archived.text
    replay = _action(client, project, "archive", "archive-once")
    assert replay.status_code == 200
    assert replay.json()["receipt"] == archived.json()["receipt"]
    assert replay.json()["replayed"] is True
    assert _action(client, project, "archive").status_code == 409
    headers = _headers(org_id, owner_id)
    assert project_id not in [p["project_id"] for p in client.get("/api/projects", headers=headers).json()["projects"]]
    rows = client.get("/api/projects?archived=true", headers=headers).json()["projects"]
    assert [p["project_id"] for p in rows] == [project_id]
    assert rows[0]["archived_at"] and rows[0]["restore_until"]
    restored = _action(client, project, "restore", "restore-once")
    assert restored.status_code == 200, restored.text
    replay = _action(client, project, "restore", "restore-once")
    assert replay.json()["receipt"] == restored.json()["receipt"]
    assert _action(client, project, "restore").status_code == 409
    assert project_id in [p["project_id"] for p in client.get("/api/projects", headers=headers).json()["projects"]]
    assert client.get("/api/projects?archived=true", headers=headers).json()["projects"] == []


def test_restore_retention_window(client, project):
    assert _action(client, project, "archive").status_code == 200
    with cursor() as cur:
        cur.execute("UPDATE projects SET archived_at=NOW()-INTERVAL '31 days' WHERE project_id=%s", (project[2],))
    response = _action(client, project, "restore")
    assert response.status_code == 409
    assert response.json()["detail"] == "restore window expired"


@pytest.mark.parametrize("status", ["queued", "running"])
def test_archive_refuses_active_jobs(client, project, status):
    org_id, _, project_id, _ = project
    with cursor() as cur:
        cur.execute("INSERT INTO jobs (job_id, org_id, project_id, kind, status) VALUES (%s,%s,%s,'run',%s)",
                    (uuid.uuid4(), org_id, project_id, status))
    response = _action(client, project, "archive")
    assert response.status_code == 409
    assert "job" in response.json()["detail"]


def test_archive_refuses_active_drawing_checkout(client, project):
    org_id, owner_id, project_id, _ = project
    drawing = store.create_drawing_artifact(org_id, uuid.UUID(project_id), "Disposable drawing")
    with cursor() as cur:
        cur.execute("INSERT INTO project_drawing_checkouts "
                    "(org_id,project_id,drawing_id,holder,holder_binding_id,acquired_at,expires_at,fence) "
                    "VALUES (%s,%s,%s,'fixture',%s,NOW(),NOW()+INTERVAL '1 hour',1)",
                    (org_id, project_id, drawing.drawing_id, owner_id))
    response = _action(client, project, "archive")
    assert response.status_code == 409
    assert "checkout" in response.json()["detail"]


@pytest.mark.parametrize("path,body", [
    ("rename", {"name": "Changed"}),
    ("files", {"path": "a.txt", "content": "hello", "media_type": "text/plain"}),
    ("jobs", {"kind": "run"}),
])
def test_archived_platform_writes_return_409(client, project, path, body):
    assert _action(client, project, "archive").status_code == 200
    response = client.request("PUT" if path == "files" else "POST",
        f"/api/projects/{project[2]}/{path}", json=body,
        headers=_headers(project[0], project[1], str(uuid.uuid4())))
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "project is archived"


def test_delete_retains_receipts_revokes_members_and_sweep_failure_is_best_effort(client, project, monkeypatch):
    from leaf_platform import api
    org_id, owner_id, project_id, _ = project
    lifecycle.put_project_file(org_id, uuid.UUID(project_id), owner_id, path="a.txt",
        content="fixture", media_type="text/plain", idempotency_key="put-fixture")
    assert _action(client, project, "archive").status_code == 200

    def fail(*args, **kwargs):
        raise OSError("sweep unavailable")

    monkeypatch.setattr(api.project_lifecycle, "sweep_project_artifacts", fail)
    deleted = _action(client, project, "delete", "delete-once")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["artifacts_queued"] == 0
    assert deleted.json()["artifact_inventory"] == "drawing_publications_only"
    replay = _action(client, project, "delete", "delete-once")
    assert replay.json()["receipt"] == deleted.json()["receipt"]
    assert replay.json()["replayed"] is True
    assert _action(client, project, "restore").status_code == 409
    with cursor() as cur:
        cur.execute("SELECT count(*) AS count FROM project_files WHERE project_id=%s", (project_id,))
        assert cur.fetchone()["count"] == 0
        cur.execute("SELECT status FROM project_member_bindings WHERE project_id=%s", (project_id,))
        assert all(row["status"] == "revoked" for row in cur.fetchall())
        cur.execute("SELECT action FROM project_lifecycle_receipts WHERE project_id=%s", (project_id,))
        assert {row["action"] for row in cur.fetchall()} == {
            "project_created", "file_put", "project_archived", "project_deleted"}


def test_delete_queues_only_exclusive_publication_identities_and_keeps_shared_material(client, project):
    org_id, owner_id, project_id, _ = project
    other = store.create_project(org_id, "Other disposable project")
    drawing = store.create_drawing_artifact(org_id, uuid.UUID(project_id))
    other_drawing = store.create_drawing_artifact(org_id, other.project_id)
    prefix = f"tenants/{org_id}/projects/{project_id}/drawings/{drawing.drawing_id}/"
    owned_key, shared_key = prefix + "sources/owned.dxf", prefix + "intakes/shared.json"
    owned_sha, shared_sha, material_sha = "a" * 64, "b" * 64, "c" * 64
    proof = {"source": {"ref": owned_key, "sha256": owned_sha},
             "intake": {"ref": shared_key, "sha256": shared_sha}}
    with cursor() as cur:
        cur.execute("INSERT INTO drawing_versions "
                    "(version_id, org_id, project_id, drawing_id, seq, oss_object, intake_ref, provenance) "
                    "VALUES (%s,%s,%s,%s,1,%s,%s,%s)",
                    (uuid.uuid4(), org_id, project_id, drawing.drawing_id, owned_key, shared_key, Jsonb(proof)))
        cur.execute("INSERT INTO drawing_versions "
                    "(version_id, org_id, project_id, drawing_id, seq, intake_ref) VALUES (%s,%s,%s,%s,1,%s)",
                    (uuid.uuid4(), org_id, other.project_id, other_drawing.drawing_id, shared_key))
        for target in (project_id, other.project_id):
            cur.execute("INSERT INTO project_materials "
                        "(material_id,org_id,project_id,actor_binding_id,name,media_type,byte_size,sha256,"
                        "width,height,preview_sha256) VALUES (%s,%s,%s,%s,'shared','image/png',10,%s,1,1,%s)",
                        (uuid.uuid4(), org_id, target, owner_id, material_sha, material_sha))
    assert _action(client, project, "archive").status_code == 200
    deleted = _action(client, project, "delete")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["artifacts_queued"] == 1
    assert deleted.json()["artifact_inventory"] == "drawing_publications_only"
    with cursor() as cur:
        cur.execute("SELECT result_json FROM project_lifecycle_receipts "
                    "WHERE project_id=%s AND action='project_deleted'", (project_id,))
        result = cur.fetchone()["result_json"]
        assert result["artifacts_queued"] == 1
        assert result["artifact_inventory"] == "drawing_publications_only"
        cur.execute("SELECT object_key, object_version_or_sha256 FROM project_artifact_sweep "
                    "WHERE project_id=%s", (project_id,))
        assert cur.fetchall() == [{"object_key": owned_key, "object_version_or_sha256": owned_sha}]
        cur.execute("SELECT project_id FROM project_materials WHERE org_id=%s AND sha256=%s",
                    (org_id, material_sha))
        assert [row["project_id"] for row in cur.fetchall()] == [other.project_id]


def test_delete_preserves_audit_referenced_parents_and_receipts(client, project):
    org_id, owner_id, project_id, _ = project
    drawing = store.create_drawing_artifact(org_id, uuid.UUID(project_id))
    version_id, job_id = uuid.uuid4(), uuid.uuid4()
    with cursor() as cur:
        cur.execute("INSERT INTO drawing_versions "
                    "(version_id,org_id,project_id,drawing_id,seq) VALUES (%s,%s,%s,%s,1)",
                    (version_id, org_id, project_id, drawing.drawing_id))
        cur.execute("INSERT INTO jobs (job_id,org_id,project_id,kind,status,input_version_id) "
                    "VALUES (%s,%s,%s,'run','failed',%s)",
                    (job_id, org_id, project_id, version_id))
        # Terminal sizing custody is immutable and references both job and drawing parents.
        cur.execute("INSERT INTO project_sizing_exchanges "
                    "(job_id,org_id,project_id,drawing_id,actor_binding_id,target_settings_id,"
                    "admitted_tenant,grant_ref,classification,state,request_bytes,request_sha256,refusal_code) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,'fixture','synthetic','refused',"
                    "'{}'::bytea,encode(sha256('{}'::bytea),'hex'),'cloud_upstream_failure')",
                    (job_id, org_id, project_id, drawing.drawing_id, owner_id,
                     f"leaf:settings:{uuid.uuid4()}", str(org_id)))
        parents = {}
        for table in ("drawing_artifacts", "drawing_versions", "jobs", "project_sizing_exchanges"):
            cur.execute(f"SELECT * FROM {table} WHERE project_id=%s", (project_id,))
            parents[table] = cur.fetchall()
    assert _action(client, project, "archive").status_code == 200
    with cursor() as cur:
        cur.execute("SELECT * FROM project_lifecycle_receipts WHERE project_id=%s ORDER BY receipt_id",
                    (project_id,))
        receipts = cur.fetchall()
    assert _action(client, project, "delete").status_code == 200
    with cursor() as cur:
        for table, rows in parents.items():
            cur.execute(f"SELECT * FROM {table} WHERE project_id=%s", (project_id,))
            assert cur.fetchall() == rows
        cur.execute("SELECT * FROM project_lifecycle_receipts WHERE project_id=%s "
                    "AND action<>'project_deleted' ORDER BY receipt_id", (project_id,))
        assert cur.fetchall() == receipts
        cur.execute("SELECT status,deleted_at FROM projects WHERE project_id=%s", (project_id,))
        tombstone = cur.fetchone()
        assert tombstone["status"] == "deleted" and tombstone["deleted_at"] is not None
