"""PostgreSQL integration proof for canonical ledger idempotency and tamper guards."""
from __future__ import annotations

import uuid

import pytest

import leaf_platform.db as db
import leaf_platform.store as store


def test_history_and_solve_are_idempotent_immutable_and_verified(make_org):
    org = make_org("Ledger Org")
    project = store.create_project(org.org_id, "Ledger project")
    store.set_project_authority_mode(org.org_id, project.project_id, "postgres_canonical")

    first = store.append_history_operation(
        org.org_id, project.project_id, "drawing.mutation", {"layers": ["A-WALL"]}, "history-key",
        branch_name="main",
    )
    duplicate = store.append_history_operation(
        org.org_id, project.project_id, "drawing.mutation", {"layers": ["A-WALL"]}, "history-key",
        branch_name="main",
    )
    assert duplicate.operation_id == first.operation_id
    with pytest.raises(ValueError, match="different history input"):
        store.append_history_operation(
            org.org_id, project.project_id, "drawing.mutation", {"layers": ["changed"]}, "history-key",
        )
    assert store.verify_history_operation(org.org_id, first.operation_id)

    solve = store.append_solve_record(org.org_id, project.project_id, {"solver": "v1", "answer": 7}, "solve-key")
    assert store.append_solve_record(
        org.org_id, project.project_id, {"solver": "v1", "answer": 7}, "solve-key"
    ).solve_id == solve.solve_id
    with pytest.raises(ValueError, match="different solve input"):
        store.append_solve_record(org.org_id, project.project_id, {"answer": 999}, "solve-key")
    assert store.verify_solve_record(org.org_id, solve.solve_id)

    with pytest.raises(Exception):
        with db.cursor() as cur:
            cur.execute("UPDATE history_operations SET operation_type = 'tampered' WHERE operation_id = %(id)s",
                        {"id": first.operation_id})
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS count FROM outbox_entries WHERE org_id = %(org_id)s", {"org_id": org.org_id})
        assert cur.fetchone()["count"] == 2


def test_legacy_authority_cannot_write_canonical_ledger(make_org):
    org = make_org("Legacy authority")
    project = store.create_project(org.org_id, "Compatibility project")
    assert store.get_authority_mode(org.org_id, project.project_id) == "legacy_sqlite"
    with pytest.raises(RuntimeError, match="legacy_sqlite"):
        store.append_solve_record(org.org_id, project.project_id, {"answer": 7}, "key")


def test_active_external_subject_binding_is_tenant_specific(make_org):
    org = make_org("Binding org")
    binding = store.create_identity_binding(org.org_id, "auth0", "auth0|ledger-user")
    resolved = store.resolve_active_identity_binding("auth0", "auth0|ledger-user")
    assert resolved is not None and resolved.binding_id == binding.binding_id
    other = make_org("Other binding org")
    with pytest.raises(ValueError, match="another platform tenant"):
        store.create_identity_binding(other.org_id, "auth0", "auth0|ledger-user")


def test_org_bootstrap_binding_conflict_rolls_back_tenant():
    first = store.create_org_with_identity("Bootstrap one", "auth0", "auth0|bootstrap-ledger")
    with pytest.raises(ValueError, match="already has"):
        store.create_org_with_identity("Bootstrap duplicate", "auth0", "auth0|bootstrap-ledger")
    with db.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS count FROM orgs "
            "WHERE name IN ('Bootstrap one', 'Bootstrap duplicate')",
        )
        assert cur.fetchone()["count"] == 1


def test_database_rejects_cross_project_history_edges(make_org):
    org = make_org("Edge org")
    first_project = store.create_project(org.org_id, "First")
    second_project = store.create_project(org.org_id, "Second")
    for project in (first_project, second_project):
        store.set_project_authority_mode(org.org_id, project.project_id, "postgres_canonical")
    first = store.append_history_operation(
        org.org_id, first_project.project_id, "drawing.mutation", {"n": 1}, "first"
    )
    second = store.append_history_operation(
        org.org_id, second_project.project_id, "drawing.mutation", {"n": 2}, "second"
    )
    with pytest.raises(Exception):
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO history_edges "
                "(edge_id, org_id, project_id, parent_operation_id, child_operation_id) "
                "VALUES (%(edge)s, %(org)s, %(project)s, %(parent)s, %(child)s)",
                {"edge": uuid.uuid4(), "org": org.org_id, "project": first_project.project_id,
                 "parent": first.operation_id, "child": second.operation_id},
            )


def test_database_rejects_cross_project_job_drawing_versions(make_org):
    org = make_org("Job version isolation")
    first_project = store.create_project(org.org_id, "First job project")
    second_project = store.create_project(org.org_id, "Second version project")
    foreign_version = store.create_drawing_version(
        org.org_id, second_project.project_id, oss_object="oss/foreign.dwg"
    )
    with pytest.raises(Exception):
        store.create_job(
            org.org_id, first_project.project_id, "run", input_version_id=foreign_version.version_id
        )


def test_history_idempotency_rejects_different_topology_or_branch(make_org):
    org = make_org("History topology")
    project = store.create_project(org.org_id, "Topology project")
    store.set_project_authority_mode(org.org_id, project.project_id, "postgres_canonical")
    parent = store.append_history_operation(
        org.org_id, project.project_id, "drawing.mutation", {"n": 1}, "parent"
    )
    store.append_history_operation(
        org.org_id, project.project_id, "drawing.mutation", {"n": 2}, "same-key",
        parent_operation_ids=[parent.operation_id], branch_name="main",
    )
    with pytest.raises(ValueError, match="topology or branch"):
        store.append_history_operation(
            org.org_id, project.project_id, "drawing.mutation", {"n": 2}, "same-key",
            parent_operation_ids=[], branch_name="main",
        )
    with pytest.raises(ValueError, match="topology or branch"):
        store.append_history_operation(
            org.org_id, project.project_id, "drawing.mutation", {"n": 2}, "same-key",
            parent_operation_ids=[parent.operation_id], branch_name="release",
        )


def test_api_returns_same_logical_canonical_records_for_idempotency_key(client, make_org):
    org = make_org("Ledger API org")
    project = store.create_project(org.org_id, "Ledger API project")
    store.set_project_authority_mode(org.org_id, project.project_id, "postgres_canonical")
    headers = {"X-Org-Id": str(org.org_id), "Idempotency-Key": "api-history-key"}
    first = client.post(f"/api/projects/{project.project_id}/history",
                        json={"payload": {"kind": "move"}, "branch_name": "main"}, headers=headers)
    duplicate = client.post(f"/api/projects/{project.project_id}/history",
                            json={"payload": {"kind": "move"}, "branch_name": "main"}, headers=headers)
    assert first.status_code == duplicate.status_code == 200
    assert first.json()["operation"]["operation_id"] == duplicate.json()["operation"]["operation_id"]
    conflict = client.post(f"/api/projects/{project.project_id}/history",
                           json={"payload": {"kind": "ignored"}}, headers=headers)
    assert conflict.status_code == 409

    headers["Idempotency-Key"] = "api-solve-key"
    solve = client.post(f"/api/projects/{project.project_id}/solves", json={"payload": {"answer": 7}}, headers=headers)
    assert solve.status_code == 405
