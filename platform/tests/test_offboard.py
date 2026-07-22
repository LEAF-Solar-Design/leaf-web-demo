"""Tenant offboarding cascade (acceptance criterion #3).

offboard_org(B) deletes all projects/drawing_versions/jobs/built_tools owned by B
(post-count == 0 in every table), fires the key-purge hook and blob-purge hook
exactly once per owned secret ref / blob ref (asserted via spies), tombstones
orgs.status='deleted' for B, and leaves ALL of org A's rows untouched.
"""
import leaf_platform.db as db
import leaf_platform.store as store
from leaf_platform.offboard import offboard_org


def _count(table: str, org_id) -> int:
    with db.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE org_id = %(org_id)s",
                    {"org_id": org_id})
        return cur.fetchone()["n"]


def _org_row(org_id):
    with db.cursor() as cur:
        cur.execute("SELECT status, offboarded_at FROM orgs WHERE org_id = %(org_id)s",
                    {"org_id": org_id})
        return cur.fetchone()


def _seed(org_id, tag: str):
    """One project with a drawing version (oss+intake blobs), a job, and a built tool."""
    p = store.create_project(org_id, f"{tag} project")
    store.create_drawing_version(
        org_id, p.project_id,
        oss_object=f"oss/{tag}/v1.dwg", intake_ref=f"intake/{tag}/v1.json", created_by="agent",
    )
    store.create_job(org_id, p.project_id, "run", tool_name="count-by-layer", params={"x": 1})
    store.create_built_tool(
        org_id, p.project_id, "count-by-layer",
        manifest={"name": "count-by-layer", "version": "1.0.0", "kind": "script"},
        source_ref=f"mushy/{tag}/count-by-layer",
    )
    return p


def test_offboard_cascade_hooks_and_org_a_untouched(make_org):
    org_a = make_org(name="Keep Org A")
    org_b = make_org(name="Offboard Org B")
    _seed(org_a.org_id, "a")
    _seed(org_b.org_id, "b")

    # spies
    key_calls, blob_calls = [], []
    secret_refs = [f"leaf/{org_b.org_id}/cred-1", f"leaf/{org_b.org_id}/cred-2"]

    result = offboard_org(
        org_b.org_id,
        key_purge_hook=key_calls.append,
        blob_purge_hook=blob_calls.append,
        secret_ref_provider=lambda oid: secret_refs,
    )

    # --- B fully deleted across all four child tables ---
    for table in ("projects", "drawing_versions", "jobs", "built_tools"):
        assert _count(table, org_b.org_id) == 0, f"{table} not empty for B"

    # --- key hook fired exactly once per secret ref ---
    assert sorted(key_calls) == sorted(secret_refs)
    assert len(key_calls) == len(set(key_calls)) == len(secret_refs)

    # --- blob hook fired exactly once per owned blob ref ---
    expected_blobs = {"oss/b/v1.dwg", "intake/b/v1.json", "mushy/b/count-by-layer"}
    assert set(blob_calls) == expected_blobs
    assert len(blob_calls) == len(set(blob_calls)) == len(expected_blobs)

    # --- B tombstoned (status='deleted', offboarded_at set); result reflects work ---
    row_b = _org_row(org_b.org_id)
    assert row_b["status"] == "deleted"
    assert row_b["offboarded_at"] is not None
    assert result.deleted_projects == 1
    assert result.status == "deleted"

    # --- org A entirely untouched ---
    assert _count("projects", org_a.org_id) == 1
    assert _count("drawing_versions", org_a.org_id) == 1
    assert _count("jobs", org_a.org_id) == 1
    assert _count("built_tools", org_a.org_id) == 1
    assert _org_row(org_a.org_id)["status"] == "active"


def test_offboard_default_secret_ref_provider(make_org):
    """With no provider injected, the org's canonical credential ref is purged once."""
    org = make_org(name="Default Provider Org")
    _seed(org.org_id, "d")
    key_calls, blob_calls = [], []

    result = offboard_org(
        org.org_id, key_purge_hook=key_calls.append, blob_purge_hook=blob_calls.append,
    )
    assert key_calls == [f"leaf/{org.org_id}/credentials"]
    assert result.status == "deleted"


def test_offboard_purges_authority_identity_ledger_and_outbox(make_org):
    org = make_org(name="Canonical purge org")
    project = store.create_project(org.org_id, "Canonical purge project")
    store.set_tenant_authority_mode(org.org_id, "postgres_canonical")
    store.set_project_authority_mode(org.org_id, project.project_id, "postgres_canonical")
    store.create_identity_binding(org.org_id, "auth0", "auth0|offboard-canonical")
    store.append_history_operation(
        org.org_id, project.project_id, "drawing.mutation", {"delete": True}, "purge-history",
        branch_name="main",
    )
    store.append_solve_record(org.org_id, project.project_id, {"answer": "purge"}, "purge-solve")

    result = offboard_org(org.org_id, key_purge_hook=lambda _: None, blob_purge_hook=lambda _: None)
    assert result.status == "deleted"
    with db.cursor() as cur:
        for table, predicate in (
            ("tenant_authority_modes", "org_id"),
            ("project_authority_modes", "org_id"),
            ("identity_bindings", "platform_tenant_id"),
            ("history_operations", "org_id"),
            ("history_edges", "org_id"),
            ("branch_refs", "org_id"),
            ("solve_records", "org_id"),
            ("outbox_entries", "org_id"),
        ):
            cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {predicate} = %(org_id)s",
                        {"org_id": org.org_id})
            assert cur.fetchone()["n"] == 0, table
