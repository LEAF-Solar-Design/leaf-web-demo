"""Investor provisioning and revocation in a migrated, isolated PostgreSQL schema."""
import secrets
import uuid

import pytest

from test_demo_sessions_postgres import postgres_registry
import demo_sessions
import investor_demo as demo
import platform_link


@pytest.fixture
def database(postgres_registry, monkeypatch):
    # The registry supplies independent connections scoped to the test schema.
    monkeypatch.setattr(platform_link, "platform_db", demo_sessions._db)
    monkeypatch.setenv("LEAF_INVESTOR_DEMO_ENABLED", "1")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "staging")
    monkeypatch.setenv("LEAF_INVESTOR_DEMO_SECRET", secrets.token_urlsafe(32))
    return postgres_registry


def test_provision_twice(database):
    assert not demo.binding_ok()
    for _ in range(2):
        assert demo.provision_at_startup() == "ready"
    assert demo.binding_ok()
    with database.connect() as conn:
        assert conn.execute(
            "SELECT name, tier, account_class, status FROM orgs WHERE org_id = %s",
            (demo.ORG_ID,)).fetchall() == [
                dict(name="Leaf Automation investor demo", tier="restricted", account_class="demo",
                     status="active")]
        assert conn.execute(
            "SELECT binding_id, external_authority, external_subject, platform_user_id, role, "
            "status, revoked_at FROM identity_bindings WHERE platform_tenant_id = %s",
            (demo.ORG_ID,)).fetchall() == [
                dict(binding_id=demo.BINDING_ID, external_authority="auth0", external_subject=demo.SUBJECT,
                     platform_user_id=demo.PLATFORM_USER_ID, role="read_only", status="active", revoked_at=None)]
        assert conn.execute(
            "SELECT 1 FROM projects WHERE org_id = %s", (demo.ORG_ID,)).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM project_authority_modes WHERE org_id = %s", (demo.ORG_ID,)).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM project_member_bindings WHERE org_id = %s", (demo.ORG_ID,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM broker_tenants WHERE tenant_id = %s",
                            (str(demo.ORG_ID),)).fetchone() is None


@pytest.mark.parametrize("statement,identifier", [
    ("UPDATE identity_bindings SET role = 'editor' WHERE binding_id = %s", demo.BINDING_ID),
    ("UPDATE identity_bindings SET status = 'revoked', revoked_at = now() WHERE binding_id = %s", demo.BINDING_ID),
    ("UPDATE orgs SET tier = 'hosted_free' WHERE org_id = %s", demo.ORG_ID),
    ("UPDATE orgs SET status = 'offboarding' WHERE org_id = %s", demo.ORG_ID),
    ("UPDATE orgs SET account_class = 'customer' WHERE org_id = %s", demo.ORG_ID),
], ids=["role", "revoked", "tier", "org-status", "account-class"])
def test_authority_mismatch_is_never_repaired(database, statement, identifier):
    assert demo.provision_at_startup() == "ready"
    with database.connect() as conn:
        conn.execute(statement, (identifier,))
    assert not demo.binding_ok()
    assert demo.provision_at_startup() == "mismatch"
    assert not demo.binding_ok()


def test_second_active_binding(database):
    from psycopg.errors import UniqueViolation
    assert demo.provision_at_startup() == "ready"
    other = uuid.uuid4()
    with database.connect() as conn:
        conn.execute("INSERT INTO orgs (org_id, name) VALUES (%s, 'Other workspace')", (other,))
        try:
            with conn.transaction():
                conn.execute(
                    "INSERT INTO identity_bindings (binding_id, external_authority, external_subject, "
                    "platform_tenant_id, platform_user_id, role) VALUES (%s, 'auth0', %s, %s, %s, 'read_only')",
                    (uuid.uuid4(), demo.SUBJECT, other, uuid.uuid4()))
        except UniqueViolation as exc:
            assert exc.diag.constraint_name == "identity_bindings_active_subject_uq"
            return
    assert not demo.binding_ok()


def test_disabled_startup_never_opens_transaction(database, monkeypatch):
    monkeypatch.delenv("LEAF_INVESTOR_DEMO_ENABLED")
    assert demo.provision_at_startup() == "disabled"
    assert not database.backend_ids
    with database.connect() as conn:
        assert conn.execute("SELECT 1 FROM orgs WHERE org_id = %s", (demo.ORG_ID,)).fetchone() is None
