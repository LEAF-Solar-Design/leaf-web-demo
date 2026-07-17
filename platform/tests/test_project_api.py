"""Project-API round-trip (acceptance criterion #4).

POST /api/projects {name} returns a project with a UUID project_id; GET
/api/projects lists it; GET /api/projects/{id} returns the workspace hydration
payload with drawing_versions[], jobs[], and built_tools[] arrays present (may be
empty). Also exercises the job create/poll/list round-trip.
"""
import uuid


def test_create_list_open_roundtrip(client, make_org):
    org = make_org(name="Round Trip Org")
    hdr = {"X-Org-Id": str(org.org_id)}

    # POST create
    r_create = client.post("/api/projects", json={"name": "Rooftop demo"}, headers=hdr)
    assert r_create.status_code == 200, r_create.text
    project = r_create.json()["project"]
    pid = project["project_id"]
    assert uuid.UUID(pid)  # valid UUID
    assert project["name"] == "Rooftop demo"

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
