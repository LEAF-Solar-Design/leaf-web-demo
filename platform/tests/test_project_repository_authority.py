"""Changed-surface PostgreSQL proofs for P8 project repository authority."""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from leaf_platform import store


def test_migration_0043_is_closed_to_the_full_authority_tuple():
    sql = (Path(__file__).resolve().parent.parent / "migrations" /
           "0043_project_repository_authority.sql").read_text(encoding="utf-8")
    assert "PRIMARY KEY (tenant_id, organization_id, project_id)" in sql
    assert "CHECK (tenant_id = organization_id)" in sql
    assert "UNIQUE (tenant_id, repo_key)" in sql
    for forbidden in ("repo_path", "repo_dir", "filesystem", "credential"):
        assert forbidden not in sql


def test_resolver_accepts_no_repository_hint_and_scopes_every_select():
    import inspect
    signature = inspect.signature(store.resolve_project_repository_authority)
    assert tuple(signature.parameters) == ("tenant_id", "organization_id", "project_id")
    source = inspect.getsource(store.resolve_project_repository_authority)
    for field in ("tenant_id", "organization_id", "project_id"):
        assert field in source


def test_project_mapping_replays_exact_and_resolves_without_repo_hint(make_org):
    org = make_org("P8 authority")
    project = store.create_project(org.org_id, "Project")
    repo_key = uuid.uuid4()
    first = store.register_project_repository_authority(
        str(org.org_id), str(org.org_id), str(project.project_id), str(repo_key))
    replay = store.register_project_repository_authority(
        str(org.org_id), str(org.org_id), str(project.project_id), str(repo_key))
    assert replay == first
    assert store.resolve_project_repository_authority(
        str(org.org_id), str(org.org_id), str(project.project_id)) == first


def test_project_mapping_conflict_and_cross_tenant_lookup_fail_closed(make_org):
    org = make_org("P8 authority owner")
    foreign = make_org("P8 authority foreign")
    project = store.create_project(org.org_id, "Project")
    store.register_project_repository_authority(
        str(org.org_id), str(org.org_id), str(project.project_id), str(uuid.uuid4()))
    with pytest.raises(store.ProjectRepositoryAuthorityConflict):
        store.register_project_repository_authority(
            str(org.org_id), str(org.org_id), str(project.project_id), str(uuid.uuid4()))
    assert store.resolve_project_repository_authority(
        str(foreign.org_id), str(foreign.org_id), str(project.project_id)) is None
    assert store.resolve_project_repository_authority(
        str(org.org_id), str(foreign.org_id), str(project.project_id)) is None


@pytest.mark.parametrize("field", ["tenant", "organization", "project", "repo"])
def test_mapping_rejects_noncanonical_uuid(make_org, field):
    org = make_org("P8 strict mapping")
    project = store.create_project(org.org_id, "Project")
    values = {
        "tenant": str(org.org_id), "organization": str(org.org_id),
        "project": str(project.project_id), "repo": str(uuid.uuid4()),
    }
    values[field] = values[field].upper()
    with pytest.raises(ValueError, match="canonical UUID"):
        store.register_project_repository_authority(
            values["tenant"], values["organization"], values["project"], values["repo"])


def test_ensure_concurrent_admissions_converge_and_deleted_replay_refuses(make_org):
    from concurrent.futures import ThreadPoolExecutor
    org = make_org("Source admission")
    project = store.create_project(org.org_id, "Source")
    args = (str(org.org_id), str(org.org_id), str(project.project_id))
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(lambda _: store.ensure_project_repository_authority(*args), range(2)))
    assert rows[0] == rows[1]
    assert str(uuid.UUID(rows[0]['repo_key'])) == rows[0]['repo_key']
    assert store.resolve_project_repository_authority(*args) == rows[0]
    with store.connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE projects SET deleted_at = now() WHERE org_id = %s AND project_id = %s",
                    (org.org_id, project.project_id))
    with pytest.raises(store.ProjectRepositoryAuthorityConflict):
        store.ensure_project_repository_authority(*args)
    assert store.resolve_project_repository_authority(*args) == rows[0]


def test_ensure_rejects_wrong_org_inactive_and_noncanonical(make_org):
    org, other = make_org("Source owner"), make_org("Source foreign")
    project = store.create_project(org.org_id, "Source")
    with pytest.raises(store.ProjectRepositoryAuthorityConflict):
        store.ensure_project_repository_authority(str(other.org_id), str(other.org_id), str(project.project_id))
    with pytest.raises(ValueError):
        store.ensure_project_repository_authority('invalid', str(org.org_id), str(project.project_id))
    with store.connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE projects SET status = 'archived' WHERE org_id = %s AND project_id = %s",
                    (org.org_id, project.project_id))
    with pytest.raises(store.ProjectRepositoryAuthorityConflict):
        store.ensure_project_repository_authority(str(org.org_id), str(org.org_id), str(project.project_id))
