"""Cross-org isolation (acceptance criterion #2).

Org A owns P_A, org B owns P_B. Every read scoped to B returns exactly P_B and
never P_A; a direct GET /api/projects/{P_A} under org-B context returns HTTP 404
(NOT 403 — no existence leak).
"""
import leaf_platform.store as store


def test_store_reads_are_org_isolated(make_org):
    org_a = make_org(name="Org A")
    org_b = make_org(name="Org B")
    p_a = store.create_project(org_a.org_id, "A project")
    p_b = store.create_project(org_b.org_id, "B project")

    # B sees only its own project
    b_projects = store.list_projects(org_b.org_id)
    assert [p.project_id for p in b_projects] == [p_b.project_id]

    # A sees only its own project
    a_projects = store.list_projects(org_a.org_id)
    assert [p.project_id for p in a_projects] == [p_a.project_id]

    # B cannot fetch A's project -> None (API turns this into 404)
    assert store.get_project(org_b.org_id, p_a.project_id) is None
    # sanity: A can fetch its own
    assert store.get_project(org_a.org_id, p_a.project_id) is not None


def test_api_cross_org_get_is_404_not_403(client, make_org):
    org_a = make_org(name="Org A")
    org_b = make_org(name="Org B")
    p_a = store.create_project(org_a.org_id, "A project")

    hdr_a = {"X-Org-Id": str(org_a.org_id)}
    hdr_b = {"X-Org-Id": str(org_b.org_id)}

    # owner can open it
    r_owner = client.get(f"/api/projects/{p_a.project_id}", headers=hdr_a)
    assert r_owner.status_code == 200

    # other org gets 404 (never 403) — no existence leak
    r_other = client.get(f"/api/projects/{p_a.project_id}", headers=hdr_b)
    assert r_other.status_code == 404
    assert r_other.status_code != 403

    # and B's list never contains A's project
    r_list = client.get("/api/projects", headers=hdr_b)
    assert r_list.status_code == 200
    ids = [p["project_id"] for p in r_list.json()["projects"]]
    assert str(p_a.project_id) not in ids
