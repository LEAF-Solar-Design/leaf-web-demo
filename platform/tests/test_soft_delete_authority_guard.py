"""Regression contract for the 2026-08-28 soft-deleted-project incident.

WHAT HAPPENED. `projects` is soft-deleted, but `project_authority_modes` rows
survive it (their FK is ON DELETE CASCADE and a soft delete never DELETEs). A
fixture-mint script discovered a "canonical" project by joining the two tables
with no liveness guard, picked a project soft-deleted two days earlier, and
`create_drawing_version` then failed with a bare ``ValueError("project not
found")``. That message conflates three different situations, so the cause was
misdiagnosed twice (first as a flaky permission gate, then as an app bug) and a
release lane stalled about 40 minutes. A read-only query against staging the
same day found 20+ orphan authority rows across two orgs.

WHAT THIS FILE PINS.
  1. The unguarded join really does return a soft-deleted project (the orphan
     row is still there), and migration 0050's view really does exclude it. If
     someone deletes the view and "fixes" the callers by hand, the first
     assertion still proves the hazard is real.
  2. Both soft-delete SHAPES are caught: store.soft_delete_project sets
     deleted_at and leaves status 'active'; project_lifecycle.delete_project
     sets status 'deleted'. A guard that checks one column misses the other.
  3. The write paths raise ProjectSoftDeleted, naming deleted_at, rather than a
     generic not-found -- the one change that would have saved the 40 minutes.
  4. A genuinely unknown project stays a DIFFERENT, distinguishable error.
"""
import uuid

import pytest

from leaf_platform import db, project_lifecycle, store

# The exact discovery query from the incident script, minus its liveness guard.
_UNGUARDED_DISCOVERY = (
    "SELECT p.project_id FROM projects p "
    "JOIN project_authority_modes pam "
    "ON pam.org_id = p.org_id AND pam.project_id = p.project_id "
    "WHERE p.org_id = %(org)s AND pam.authority_mode = 'postgres_canonical'"
)

# The same query written against migration 0050's view. This is what callers
# are supposed to write, and what the static guard steers them to.
_GUARDED_DISCOVERY = (
    "SELECT pam.project_id FROM live_project_authority_modes pam "
    "WHERE pam.org_id = %(org)s AND pam.authority_mode = 'postgres_canonical'"
)


def _discovered(sql: str, org_id: uuid.UUID) -> set:
    with db.cursor() as cur:
        cur.execute(sql, {"org": org_id})
        return {str(row["project_id"]) for row in cur.fetchall()}


def _canonical_project(org_id: uuid.UUID, name: str):
    project = store.create_project(org_id, name)
    store.set_project_authority_mode(org_id, project.project_id, "postgres_canonical")
    return project


def test_soft_deleted_project_is_still_reachable_through_the_unguarded_join(make_org):
    """The hazard is real: the orphan authority row outlives its project."""
    org = make_org(name="Orphan authority org")
    project = _canonical_project(org.org_id, "Soft deleted canonical project")
    assert store.soft_delete_project(org.org_id, project.project_id) is True

    with db.cursor() as cur:
        cur.execute(
            "SELECT authority_mode FROM project_authority_modes "
            "WHERE org_id = %(org)s AND project_id = %(project)s",
            {"org": org.org_id, "project": project.project_id},
        )
        orphan = cur.fetchone()
    assert orphan is not None and orphan["authority_mode"] == "postgres_canonical", (
        "the authority row is expected to SURVIVE the soft delete -- that is the "
        "hazard this module guards, not a bug to fix here"
    )

    assert str(project.project_id) in _discovered(_UNGUARDED_DISCOVERY, org.org_id)
    assert str(project.project_id) not in _discovered(_GUARDED_DISCOVERY, org.org_id)


def test_lifecycle_delete_shape_is_caught_by_the_same_guard(make_org):
    """The other soft-delete writer sets status='deleted'; one guard covers both."""
    org = make_org(name="Lifecycle delete org")
    project = _canonical_project(org.org_id, "Lifecycle deleted canonical project")
    with db.cursor() as cur:
        cur.execute(
            "UPDATE projects SET status = 'deleted', updated_at = NOW() "
            "WHERE org_id = %(org)s AND project_id = %(project)s",
            {"org": org.org_id, "project": project.project_id},
        )

    assert str(project.project_id) in _discovered(_UNGUARDED_DISCOVERY, org.org_id)
    assert str(project.project_id) not in _discovered(_GUARDED_DISCOVERY, org.org_id)
    assert store.get_authority_mode(org.org_id, project.project_id) == "legacy_sqlite"


def test_create_drawing_version_names_the_soft_delete_instead_of_not_found(make_org):
    org = make_org(name="Drawing version guard org")
    project = _canonical_project(org.org_id, "Version guard project")
    store.soft_delete_project(org.org_id, project.project_id)

    with pytest.raises(store.ProjectSoftDeleted) as excinfo:
        store.create_drawing_version(
            org.org_id, project.project_id, oss_object="guard/input.dwg")

    error = excinfo.value
    assert error.reason == "soft_deleted"
    assert error.deleted_at is not None
    message = str(error)
    assert str(project.project_id) in message
    assert "soft-deleted" in message
    assert error.deleted_at.isoformat() in message
    # The old message. Its return is the regression.
    assert message != "project not found"
    # Still a ValueError, so every existing `except ValueError` keeps working.
    assert isinstance(error, ValueError)


def test_create_drawing_version_is_refused_after_its_artifact_already_exists(make_org):
    """The common path: the project is deleted AFTER its drawing artifact exists.

    The pre-fix code only guarded the branch that CREATES the artifact, so this
    case -- the far more likely one in production -- appended versions into a
    deleted project silently, with no error at all.
    """
    org = make_org(name="Existing artifact guard org")
    project = _canonical_project(org.org_id, "Existing artifact project")
    first = store.create_drawing_version(
        org.org_id, project.project_id, oss_object="guard/v1.dwg")
    assert first.seq == 1

    store.soft_delete_project(org.org_id, project.project_id)

    with pytest.raises(store.ProjectSoftDeleted):
        store.create_drawing_version(
            org.org_id, project.project_id, oss_object="guard/v2.dwg")
    with pytest.raises(store.ProjectSoftDeleted):
        store.create_drawing_artifact(
            org.org_id, project.project_id, "Second drawing")
    # No version 2 was appended.
    with db.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM drawing_versions "
            "WHERE org_id = %(org)s AND project_id = %(project)s",
            {"org": org.org_id, "project": project.project_id},
        )
        assert cur.fetchone()["n"] == 1


def test_unknown_project_stays_a_distinguishable_error(make_org):
    """"soft-deleted" and "no such project" must not collapse back together."""
    org = make_org(name="Unknown project org")
    unknown = uuid.uuid4()

    with pytest.raises(store.ProjectNotFound) as excinfo:
        store.create_drawing_version(org.org_id, unknown, oss_object="guard/x.dwg")

    assert excinfo.value.reason == "not_found"
    assert excinfo.value.deleted_at is None
    assert not isinstance(excinfo.value, store.ProjectSoftDeleted)


def test_another_orgs_project_is_not_disclosed_as_existing(make_org):
    """A cross-org id reads as not_found, never as "exists but is deleted".

    Distinguishing "wrong org" from "no such project" in the message would turn
    this error into a cross-tenant existence oracle, which is exactly what this
    module's "404, never 403" rule forbids. The two cases stay merged BY DESIGN.
    """
    owner = make_org(name="Cross org owner")
    stranger = make_org(name="Cross org stranger")
    project = _canonical_project(owner.org_id, "Cross org project")
    store.soft_delete_project(owner.org_id, project.project_id)

    with pytest.raises(store.ProjectNotFound) as excinfo:
        store.create_drawing_version(
            stranger.org_id, project.project_id, oss_object="guard/y.dwg")
    assert excinfo.value.reason == "not_found"
    assert "soft-deleted" not in str(excinfo.value)


def test_authority_mode_cannot_be_selected_for_a_dead_project(make_org):
    """The write path fails closed too, so no NEW orphan can be minted."""
    org = make_org(name="Authority write guard org")
    project = _canonical_project(org.org_id, "Authority write guard project")
    store.soft_delete_project(org.org_id, project.project_id)

    with pytest.raises(store.ProjectSoftDeleted):
        store.set_project_authority_mode(
            org.org_id, project.project_id, "legacy_sqlite")
    with db.cursor() as cur:
        cur.execute(
            "SELECT authority_mode FROM project_authority_modes "
            "WHERE org_id = %(org)s AND project_id = %(project)s",
            {"org": org.org_id, "project": project.project_id},
        )
        assert cur.fetchone()["authority_mode"] == "postgres_canonical", (
            "the refused write must not have changed the stored authority")


def test_tenant_authority_fallback_does_not_revive_a_dead_project(make_org):
    """The tenant fallback has no project dimension, so it needs its own guard.

    Without it, a tenant-wide 'postgres_canonical' answers for a project that no
    longer exists, and every write gated by _require_postgres_authority sails
    through into a deleted project.
    """
    org = make_org(name="Tenant fallback guard org")
    store.set_tenant_authority_mode(org.org_id, "postgres_canonical")
    project = store.create_project(org.org_id, "Tenant fallback project")
    assert store.get_authority_mode(org.org_id, project.project_id) == "postgres_canonical"

    store.soft_delete_project(org.org_id, project.project_id)
    assert store.get_authority_mode(org.org_id, project.project_id) == "legacy_sqlite"


def test_lifecycle_operations_refuse_a_deleted_at_only_soft_delete(make_org):
    """project_lifecycle's gate checked status only, so this project stayed mutable."""
    org = make_org(name="Lifecycle gate org")
    project = store.create_project(org.org_id, "Lifecycle gate project")
    binding = store.create_identity_binding(
        org.org_id, "auth0", f"auth0|lifecycle-gate-{uuid.uuid4().hex}")
    store.soft_delete_project(org.org_id, project.project_id)

    with pytest.raises(project_lifecycle.LifecycleUnavailable):
        project_lifecycle.clone_project(
            org.org_id, project.project_id, binding.binding_id,
            name="Clone of a deleted project",
            idempotency_key=f"clone-{uuid.uuid4().hex}",
        )
