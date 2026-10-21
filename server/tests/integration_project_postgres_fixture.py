"""Owner setup for the explicit, empty, loopback integration grant database."""
from contextlib import contextmanager
from pathlib import Path
import secrets
from types import SimpleNamespace
from uuid import uuid4

import pytest

from server.tests.test_integration_turn_grant_postgres import _safe_dsn


# Closure of the real session producer, identity/project reads, and grant store.
MIGRATIONS = (
    "0001_project_job", "0002_deletion_columns", "0003_canonical_history_ledger",
    "0012_sessions", "0019_sessions_model", "0022_sessions_active_turn_subject",
    "0035_session_request_journal", "0038_project_lifecycle", "0039_project_session_scope",
    "0050_live_project_guard", "0051_unit_economics", "0053_session_scope",
    "0075_org_acceptance_classification", "0079_integration_turn_grants",
    "0080_integration_turn_grant_action_discovery",
)


@contextmanager
def connection_factory(conn):
    yield conn


@contextmanager
def scoped(conn, authority):
    with conn.transaction():
        conn.execute("SELECT set_config('leaf.integration_tenant_id', %s, true), "
                     "set_config('leaf.integration_project_id', %s, true)",
                     (authority.tenant_id, authority.project_id))
        yield


@pytest.fixture
def project_database(monkeypatch):
    # This check precedes any pool access, so no .env/default DSN can opt in.
    dsn = _safe_dsn()
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict
    import platform_link
    db = platform_link.platform_db()
    owner = psycopg.connect(dsn, autocommit=True, connect_timeout=2)
    connections, roles = [], []
    started = False
    had_grant_role = True
    try:
        assert owner.execute("SELECT current_database()").fetchone()[0] == "ops_integration_grant_test"
        existing = owner.execute("""SELECT count(*) FROM pg_class c JOIN pg_namespace n
            ON n.oid=c.relnamespace WHERE n.nspname='public'""").fetchone()[0]
        functions = owner.execute("""SELECT count(*) FROM pg_proc p JOIN pg_namespace n
            ON n.oid=p.pronamespace WHERE n.nspname='public'""").fetchone()[0]
        if existing or functions:
            pytest.fail("disposable database must be empty", pytrace=False)
        had_grant_role = bool(owner.execute(
            "SELECT 1 FROM pg_roles WHERE rolname='leaf_integration_grant_app'"
        ).fetchone())
        db.reset_pool()
        monkeypatch.setenv("DATABASE_URL", dsn)
        started = True
        migration_dir = Path(__file__).resolve().parents[2] / "platform/migrations"
        for name in MIGRATIONS:
            db.apply_migration(migration_dir / (name + ".sql"))
        db.reset_pool()

        scopes = []
        for _ in range(2):
            org, project, user, binding, membership = (str(uuid4()) for _ in range(5))
            subject = "auth0|project-postgres-" + uuid4().hex
            with owner.transaction():
                owner.execute("INSERT INTO orgs (org_id,name,tier) VALUES (%s,%s,%s)",
                              (org, "Synthetic integration org", "hosted_starter"))
                owner.execute("INSERT INTO projects (project_id,org_id,name) VALUES (%s,%s,%s)",
                              (project, org, "Synthetic integration project"))
                # The canonical schema stores the platform user UUID on the binding;
                # there is no separate platform_users table in this closure.
                owner.execute("""INSERT INTO identity_bindings
                    (binding_id,external_authority,external_subject,platform_tenant_id,
                     platform_user_id,role,status) VALUES (%s,'auth0',%s,%s,%s,'editor','active')""",
                    (binding, subject, org, user))
                owner.execute("""INSERT INTO project_member_bindings
                    (membership_id,org_id,project_id,binding_id,role,invited_by_binding_id)
                    VALUES (%s,%s,%s,%s,'editor',%s)""",
                    (membership, org, project, binding, binding))
            scopes.append(SimpleNamespace(org=org, project=project, user=user,
                                          binding=binding, subject=subject, membership=membership))

        params = conninfo_to_dict(dsn)
        app_password = None
        for index in range(2):
            role = "project_grant_test_" + secrets.token_hex(6)
            password = secrets.token_urlsafe(32)
            owner.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)))
            roles.append(role)
            owner.execute(sql.SQL("GRANT leaf_integration_grant_app TO {}").format(sql.Identifier(role)))
            if index == 0:
                owner.execute(sql.SQL("GRANT SELECT ON public.orgs, public.projects, public.live_projects, "
                                      "public.identity_bindings, public.project_member_bindings TO {}").format(
                                          sql.Identifier(role)))
                owner.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE ON public.app_sessions TO {}").format(
                    sql.Identifier(role)))
            login = dict(params, user=role, password=password, connect_timeout=2)
            connections.append(psycopg.connect(**login, autocommit=True))
            if index == 0:
                app_password = password
        # The production pool validates URL syntax, so render a URL with safely
        # quoted generated credentials rather than passing libpq keyword syntax.
        from urllib.parse import quote, urlsplit, urlunsplit
        parsed = urlsplit(dsn)
        host = "[" + parsed.hostname + "]" if ":" in parsed.hostname else parsed.hostname
        netloc = quote(roles[0], safe="") + ":" + quote(
            app_password, safe="") + "@" + host
        if parsed.port is not None:
            netloc += ":" + str(parsed.port)
        monkeypatch.setenv("DATABASE_URL", urlunsplit(parsed._replace(netloc=netloc)))
        yield SimpleNamespace(db=db, owner=owner, connections=connections, scopes=scopes)
    finally:
        db.reset_pool()
        for conn in connections:
            conn.close()
        if started:
            # Only the schema accepted as empty above is ours to remove.
            owner.execute("DROP SCHEMA public CASCADE")
            owner.execute("CREATE SCHEMA public")
        for role in roles:
            owner.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        if started and not had_grant_role:
            owner.execute("DROP ROLE IF EXISTS leaf_integration_grant_app")
        owner.close()
