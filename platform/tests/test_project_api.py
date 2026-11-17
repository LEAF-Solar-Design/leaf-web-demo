"""Project-API round-trip (acceptance criterion #4).

POST /api/projects {name} returns a project with a UUID project_id; GET
/api/projects lists it; GET /api/projects/{id} returns the workspace hydration
payload with drawing_versions[], jobs[], and built_tools[] arrays present (may be
empty). Also exercises the job create/poll/list round-trip.
"""
import uuid

import pytest

from leaf_platform import project_lifecycle, store


def test_create_list_open_roundtrip(client, make_org):
    org = make_org(name="Round Trip Org")
    hdr = {"X-Org-Id": str(org.org_id)}

    # POST create
    r_create = client.post(
        "/api/projects",
        json={"name": "Rooftop demo", "authority_mode": "legacy_sqlite"},
        headers=hdr,
    )
    assert r_create.status_code == 200, r_create.text
    project = r_create.json()["project"]
    pid = project["project_id"]
    assert uuid.UUID(pid)  # valid UUID
    assert project["name"] == "Rooftop demo"
    assert store.get_authority_mode(org.org_id, uuid.UUID(pid)) == "postgres_canonical"

    # GET list contains it
    r_list = client.get("/api/projects", headers=hdr)
    assert r_list.status_code == 200
    assert pid in [p["project_id"] for p in r_list.json()["projects"]]

    # GET open -> workspace hydration payload with all three arrays present
    r_open = client.get(f"/api/projects/{pid}", headers=hdr)
    assert r_open.status_code == 200
    payload = r_open.json()
    assert payload["project"]["project_id"] == pid
    for key in ("drawing_versions", "jobs", "built_tools"):
        assert key in payload and isinstance(payload[key], list)


def test_job_create_poll_list_roundtrip(client, make_org):
    org = make_org(name="Job Org")
    hdr = {"X-Org-Id": str(org.org_id)}

    pid = client.post("/api/projects", json={"name": "P"}, headers=hdr).json()["project"]["project_id"]

    # create a job
    r_job = client.post(
        f"/api/projects/{pid}/jobs",
        json={"kind": "run", "tool": "count-by-layer", "params": {"radius_in": 18}},
        headers=hdr,
    )
    assert r_job.status_code == 200, r_job.text
    job = r_job.json()["job"]
    jid = job["job_id"]
    assert uuid.UUID(jid)
    assert job["status"] == "queued"
    assert job["kind"] == "run"
    assert job["tool_name"] == "count-by-layer"

    # poll it
    r_poll = client.get(f"/api/jobs/{jid}", headers=hdr)
    assert r_poll.status_code == 200
    assert r_poll.json()["job"]["job_id"] == jid

    # list under the project
    r_list = client.get(f"/api/projects/{pid}/jobs", headers=hdr)
    assert r_list.status_code == 200
    assert jid in [j["job_id"] for j in r_list.json()["jobs"]]

    # it also shows up in the hydration payload
    r_open = client.get(f"/api/projects/{pid}", headers=hdr)
    assert jid in [j["job_id"] for j in r_open.json()["jobs"]]


def test_missing_org_header_is_400(client):
    assert client.get("/api/projects").status_code == 400


def test_invalid_kind_is_422(client, make_org):
    org = make_org(name="Bad Kind Org")
    hdr = {"X-Org-Id": str(org.org_id)}
    pid = client.post("/api/projects", json={"name": "P"}, headers=hdr).json()["project"]["project_id"]
    r = client.post(f"/api/projects/{pid}/jobs", json={"kind": "nonsense"}, headers=hdr)
    assert r.status_code == 422


@pytest.fixture
def report_project(client, make_org):
    org = make_org(name="Report files")
    owner = store.create_identity_binding(
        org.org_id, "auth0", f"report-{uuid.uuid4()}", role="owner",
    )
    created = project_lifecycle.create_blank_project(
        org.org_id, owner.binding_id, name="Reports", idempotency_key="create",
    )
    headers = {"X-Org-Id": str(org.org_id), "X-Actor-Binding-Id": str(owner.binding_id)}
    return org, uuid.UUID(created["project"]["project_id"]), owner, headers


def test_report_file_cas_replay_and_legacy_writes(client, report_project):
    org, project_id, _owner, headers = report_project
    url = f"/api/projects/{project_id}/files"
    body = {"path": "reports/manual.json", "media_type": "application/json", "content": "{}"}

    def put(key, **changes):
        return client.put(url, json={**body, **changes}, headers={**headers, "Idempotency-Key": key})

    absent = client.get(url + "/by-path", params={"path": body["path"]}, headers=headers)
    assert absent.status_code == 200, absent.text
    assert absent.json() == {
        "organization_id": str(org.org_id), "project_id": str(project_id),
        "can_write": True, "file": None,
    }
    for revision in (1, 2147483646):
        missing_update = put(f"missing-{revision}", expected_revision=revision)
        assert missing_update.status_code == 409, missing_update.text
        assert missing_update.json() == {"detail": "project_file_revision_conflict"}
    first = put("create-only", expected_revision=0)
    assert first.status_code == 200, first.text
    assert first.json()["file"]["revision"] == 1
    assert first.json()["receipt"]["input_digest"] == project_lifecycle._canonical_digest({
        "path": body["path"], "media_type": body["media_type"],
        "content_sha256": first.json()["file"]["content_sha256"], "expected_revision": 0,
    })
    duplicate = put("create-again", expected_revision=0)
    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "project_file_revision_conflict"}
    updated = put("update", content='{"total":1}', expected_revision=1)
    assert updated.status_code == 200, updated.text
    assert updated.json()["file"]["revision"] == 2
    stale = put("stale", content='{"total":2}', expected_revision=1)
    assert stale.status_code == 409
    assert stale.json() == {"detail": "project_file_revision_conflict"}
    replay = put("create-only", expected_revision=0)
    assert replay.status_code == 200, replay.text
    assert replay.json() == {**first.json(), "replayed": True}
    rebound = put("create-only", expected_revision=2)
    assert rebound.status_code == 409
    assert rebound.json() == {"detail": "idempotency key is already bound to another request"}

    legacy = put("legacy", content="legacy")
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["file"]["revision"] == 3
    assert legacy.json()["receipt"]["input_digest"] == project_lifecycle._canonical_digest({
        "path": body["path"], "media_type": body["media_type"],
        "content_sha256": legacy.json()["file"]["content_sha256"],
    })
    null_replay = put("legacy", content="legacy", expected_revision=None)
    assert null_replay.status_code == 200, null_replay.text
    assert null_replay.json()["replayed"] is True
    last = put("last", content="last")
    assert last.status_code == 200, last.text
    assert last.json()["file"]["revision"] == 4
    read = client.get(url + "/by-path", params={"path": body["path"]}, headers=headers)
    assert read.status_code == 200, read.text
    assert read.json()["file"] == {**last.json()["file"], "content": "last"}


@pytest.mark.parametrize("revision", [True, False, 1.0, 0.5, -1, 2147483647, "1"])
def test_report_file_api_rejects_non_strict_revision(client, report_project, revision):
    _org, project_id, _owner, headers = report_project
    response = client.put(
        f"/api/projects/{project_id}/files",
        json={"path": "report.json", "media_type": "application/json", "content": "{}",
              "expected_revision": revision},
        headers={**headers, "Idempotency-Key": "invalid"},
    )
    assert response.status_code == 422, response.text
