"""S1 source discovery and approval within the W4h iOS ship lane."""
from __future__ import annotations

import uuid

import pytest

from leaf_platform import ios_ship, store
from test_ios_ship_store import _seed, _launch


def _entry(**overrides):
    entry = {"catalog_key": "bakery-stock", "repository": "LEAF-Solar-Design/bakery-inventory",
             "source_revision": "83bbde1", "source_sha256": "a" * 64,
             "bundle_identifier": "ai.leafautomation.bakerystock",
             "marketing_version": "1.0", "build_number": "1",
             "producer_receipt_digest": "b" * 64}
    return {**entry, **overrides}


def _project(make_org):
    org = make_org()
    return org.org_id, store.create_project(org.org_id, "catalog project").project_id


def _approve(org, project, revision="r1", **overrides):
    entry = _entry()
    fields = {name: entry[name] for name in (
        "source_revision", "source_sha256", "bundle_identifier", "marketing_version", "build_number")}
    return ios_ship.approve_catalog_revision(
        org, project, revision=revision, approved_by="owner-binding", **{**fields, **overrides})


# S1 row1
def test_catalog_exact_replay_and_conflicting_bytes(make_org):
    org, project = _project(make_org)
    first = ios_ship.register_source_catalog_entry(org, project, _entry())
    assert all(first[key] == value for key, value in _entry().items())
    assert first["org_id"] == str(org) and first["project_id"] == str(project)
    assert first["imported_at"]
    assert ios_ship.register_source_catalog_entry(org, project, _entry()) == first
    with pytest.raises(ios_ship.CatalogConflict) as error:
        ios_ship.register_source_catalog_entry(org, project, _entry(build_number="2"))
    assert error.value.code == "catalog_conflict"
    assert ios_ship.get_source_catalog_entry(org, project, "83bbde1")["catalog_id"] == first["catalog_id"]
    with pytest.raises(ios_ship.ProjectUnavailable):
        ios_ship.register_source_catalog_entry(make_org().org_id, project, _entry())


# S1 row2
def test_catalog_cannot_be_updated_or_deleted(make_org):
    org, project = _project(make_org)
    row = ios_ship.register_source_catalog_entry(org, project, _entry())
    for statement in (
        "UPDATE ios_ship_source_catalog SET build_number='2' WHERE catalog_id=%s",
        "DELETE FROM ios_ship_source_catalog WHERE catalog_id=%s",
    ):
        with pytest.raises(Exception):
            with ios_ship.connection() as conn, conn.cursor() as cur:
                cur.execute(statement, (uuid.UUID(row["catalog_id"]),))
        assert ios_ship.get_source_catalog_entry(org, project, "83bbde1") == row


# S1 row3
def test_catalog_approval_requires_exact_tuple_and_existing_source(make_org):
    org, project = _project(make_org)
    ios_ship.register_source_catalog_entry(org, project, _entry())
    approval = _approve(org, project)
    assert approval["approved"] is True
    assert ios_ship.get_approved_ios_ship_revision(org, project, "r1")["approval_id"] == approval["approval_id"]
    with pytest.raises(ios_ship.IosShipError) as mismatch:
        _approve(org, project, "r2", source_sha256="c" * 64)
    assert mismatch.value.code == "approval_tuple_mismatch"
    assert str(mismatch.value) == "source_sha256"
    assert ios_ship.get_approved_ios_ship_revision(org, project, "r2") is None
    with pytest.raises(ios_ship.RevisionNotApproved) as missing:
        _approve(org, project, "r3", source_revision="unknown")
    assert missing.value.code == "catalog_entry_missing"
    assert missing.value.setup_action == "import-ios-source"
    assert len(ios_ship.list_revision_approvals(org, project)) == 1


# S1 row4
def test_catalog_and_approvals_stay_in_the_named_org_and_project(make_org):
    org, project_a = _project(make_org)
    project_b = store.create_project(org, "second project").project_id
    other_org, other_project = _project(make_org)
    expected = {}
    for scope in ((org, project_a), (org, project_b), (other_org, other_project)):
        entry = ios_ship.register_source_catalog_entry(*scope, _entry())
        approval = _approve(*scope)
        expected[scope] = (entry["catalog_id"], approval["approval_id"])
    for scope, (catalog_id, approval_id) in expected.items():
        assert [row["catalog_id"] for row in ios_ship.list_source_catalog(*scope)] == [catalog_id]
        rows = ios_ship.list_revision_approvals(*scope)
        assert [row["approval_id"] for row in rows] == [approval_id]
        assert rows[0]["consumed_at"] is None and isinstance(rows[0]["created_at"], str)
    assert ios_ship.list_source_catalog(other_org, project_a) == []
    assert ios_ship.list_revision_approvals(other_org, project_a) == []


# S1 row5
def test_latest_execution_crosses_revisions_but_not_tenants(make_org):
    org, project, tenant, principal, approval = _seed(make_org)
    dispatch = lambda _: {"status": "dispatched"}
    first = _launch(org, project, tenant, principal, approval, dispatch)
    second_approval = ios_ship.record_approval(
        org.org_id, project.project_id, "r2", source_revision="83bbde1",
        source_sha256="a" * 64, bundle_identifier="com.leaf.soundbeam",
        marketing_version="1.2", build_number="20", approved_by=principal)
    second = _launch(org, project, tenant, principal, second_approval, dispatch,
                     revision="r2", build_number="20", idempotency_key="launch-2")
    other_project = store.create_project(org.org_id, "newer execution project")
    with ios_ship.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO project_member_bindings "
            "(membership_id, org_id, project_id, binding_id, role, invited_by_binding_id) "
            "VALUES (%s, %s, %s, %s, 'owner', %s)",
            (uuid.uuid4(), org.org_id, other_project.project_id,
             uuid.UUID(principal), uuid.UUID(principal)))
    other_approval = ios_ship.record_approval(
        org.org_id, other_project.project_id, "r1", source_revision="83bbde1",
        source_sha256="a" * 64, bundle_identifier="com.leaf.soundbeam",
        marketing_version="1.2", build_number="19", approved_by=principal)
    other = _launch(org, other_project, tenant, principal, other_approval, dispatch)
    with ios_ship.connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE ios_ship_executions SET created_at=created_at - interval '1 day' "
                    "WHERE execution_id=%s", (uuid.UUID(first["execution_id"]),))
        cur.execute("UPDATE ios_ship_executions SET created_at=created_at + interval '1 day' "
                    "WHERE execution_id=%s", (uuid.UUID(other["execution_id"]),))
    latest = ios_ship.latest_execution_for_project(org.org_id, tenant, project.project_id)
    assert latest["execution_id"] == second["execution_id"] and latest["revision"] == "r2"
    assert set(latest) == {"execution_id", "revision", "status", "failed_stage",
                           "receipt_id", "dispatch_result", "updated_at"}
    assert ios_ship.latest_execution_for_project(org.org_id, "no-launch", project.project_id) is None
    assert ios_ship.latest_execution_for_project(make_org().org_id, tenant, project.project_id) is None


# S1 row6
def test_only_current_project_owner_resolves(make_org):
    org, project = _project(make_org)
    for role in ("owner", "editor"):
        subject = f"auth0|catalog-{uuid.uuid4()}"
        binding = store.create_identity_binding(org, "auth0", subject, role=role)
        with ios_ship.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO project_member_bindings "
                "(membership_id, org_id, project_id, binding_id, role, invited_by_binding_id) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (uuid.uuid4(), org, project, binding.binding_id, role, binding.binding_id))
        assert ios_ship.resolve_ship_owner(org, project, subject) == (
            str(binding.binding_id) if role == "owner" else None)
    outsider = f"auth0|catalog-{uuid.uuid4()}"
    store.create_identity_binding(org, "auth0", outsider, role="owner")
    assert ios_ship.resolve_ship_owner(org, project, outsider) is None
    assert ios_ship.resolve_ship_owner(org, project, "") is None
    for inactive in ("binding", "membership"):
        subject = f"auth0|catalog-{uuid.uuid4()}"
        binding = store.create_identity_binding(org, "auth0", subject, role="owner")
        with ios_ship.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO project_member_bindings "
                "(membership_id, org_id, project_id, binding_id, role, invited_by_binding_id) "
                "VALUES (%s, %s, %s, %s, 'owner', %s)",
                (uuid.uuid4(), org, project, binding.binding_id, binding.binding_id))
            if inactive == "binding":
                cur.execute("UPDATE identity_bindings SET status='revoked', revoked_at=NOW() "
                            "WHERE binding_id=%s",
                            (binding.binding_id,))
            else:
                cur.execute("UPDATE project_member_bindings SET status='revoked', revoked_at=NOW() "
                            "WHERE binding_id=%s", (binding.binding_id,))
        assert ios_ship.resolve_ship_owner(org, project, subject) is None


# S1 row7
def test_catalog_rejects_secrets_and_every_oversize_field_before_database(make_org, monkeypatch):
    org, project = _project(make_org)

    def no_database():
        pytest.fail("invalid catalog must fail before database access")

    monkeypatch.setattr(ios_ship, "connection", no_database)
    for entry in (_entry(repository="-----BEGIN material"), _entry(token="private")):
        with pytest.raises(ios_ship.SecretShapedFieldRejected):
            ios_ship.register_source_catalog_entry(org, project, entry)
    for name in _entry():
        with pytest.raises(ios_ship.IosShipError) as error:
            ios_ship.register_source_catalog_entry(org, project, _entry(**{name: "x" * 513}))
        assert error.value.code == "invalid_catalog_entry"


# S1 row13
def test_consumed_catalog_approval_is_refused_without_writing(make_org, monkeypatch):
    org, project, tenant, principal, _ = _seed(make_org)
    fields = {"bundle_identifier": "com.leaf.soundbeam",
              "marketing_version": "1.2", "build_number": "19"}
    ios_ship.register_source_catalog_entry(org.org_id, project.project_id, _entry(**fields))
    approval = _approve(org.org_id, project.project_id, **fields)
    with monkeypatch.context() as patch:
        patch.setattr(ios_ship, "record_approval", lambda *args, **kwargs: str(uuid.uuid4()))
        with pytest.raises(ios_ship.IosShipError) as missing_write:
            _approve(org.org_id, project.project_id, **fields)
        assert missing_write.value.code == "approval_not_recorded"
    _launch(org, project, tenant, principal, approval["approval_id"],
            lambda _: {"status": "dispatched"})
    before = ios_ship.list_revision_approvals(org.org_id, project.project_id)
    assert len(before) == 1 and before[0]["consumed_at"] is not None

    def no_write(*args, **kwargs):
        pytest.fail("consumed approval must be refused before record_approval")

    monkeypatch.setattr(ios_ship, "record_approval", no_write)
    with pytest.raises(ios_ship.LaunchConflict) as consumed:
        _approve(org.org_id, project.project_id, **fields)
    assert consumed.value.code == "approval_consumed"
    assert consumed.value.setup_action == "approve-new-revision"
    assert ios_ship.list_revision_approvals(org.org_id, project.project_id) == before
