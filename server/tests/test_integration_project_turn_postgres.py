"""Opt-in project producer custody proof; starts AFTER JWT verification.

Only KMS is synthetic. Sessions, identity, membership, capture, authority and
encrypted persistence all use their production PostgreSQL paths.
"""
from dataclasses import replace
from functools import partial
from pathlib import Path
import sys
import time

import pytest
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import platform_link

platform_link._ensure_platform_package()
import deps
import integration_tool_service as service
import integration_turn_grant_crypto as crypto
import integration_turn_grants as grants
import session_store
from leaf_platform.integration_turn_grant_store import Authority, CURRENT_OPERATIONS, IntegrationTurnGrantStore
from server.tests.integration_project_postgres_fixture import (
    connection_factory, project_database, scoped,
)
from server.tests.test_integration_turn_grant_crypto import ARN, FakeKeys


ORIGIN = "https://integration.example"
TURN = "colliding-turn-label"
DRAWING = "colliding-drawing-id"


class ContextKeys:
    """One existing crypto-test fake per grant, preserving its context checks."""
    def __init__(self):
        self.keys = {}

    def canonical_key_arn(self):
        return ARN

    def generate_data_key(self, key_arn, context):
        grant = context["grant_id"]
        assert grant not in self.keys
        self.keys[grant] = FakeKeys()
        return self.keys[grant].generate_data_key(key_arn, context)

    def decrypt_data_key(self, key_arn, wrapped, context):
        return self.keys[context["grant_id"]].decrypt_data_key(key_arn, wrapped, context)


def _callback(scope, token):
    tenant = deps.TenantContext(scope.org, org_id=scope.org, subject=scope.subject,
                                tier="hosted_starter", authority_resolved=True)
    request = Request({"type": "http", "method": "POST", "path": "/synthetic",
                       "headers": [(b"authorization", ("Bearer " + token).encode())]})
    request.state._integration_verified_expiry = deps._VerifiedPlatformExpiry(
        scope.subject, time.time() + 240)
    callback = grants.for_request(request, tenant)
    assert callback is not None
    return callback


def test_project_producer_custody_and_current_authority(
        project_database, monkeypatch, caplog, capsys):
    h = project_database
    for name, value in {
        "LEAF_SESSIONS_STORE": "postgres", "LEAF_AUTH_LIVE": "1",
        "LEAF_INTEGRATION_TURN_GRANTS_ENABLED": "1",
        "LEAF_INTEGRATION_SERVICE_ORIGIN": ORIGIN,
        "LEAF_INTEGRATION_TURN_GRANTS_KMS_KEY_ID": "alias/synthetic-project-test",
        "AWS_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(grants, "_references", {})
    keys = ContextKeys()
    monkeypatch.setattr(crypto, "KmsKeyService", lambda: keys)
    # Keep S11 _adapters, seal, the grant store and its default pool connection.
    reader = IntegrationTurnGrantStore(partial(connection_factory, h.connections[1]))
    sessions, authorities, tokens = [], [], []
    for index, scope in enumerate(h.scopes):
        session = session_store.get_or_create_session(
            scope.org, DRAWING, org_id=scope.org, project_id=scope.project)
        sid = session["session_id"]
        sessions.append(sid)
        assert session["tenant_id"] == scope.org
        assert session_store.try_begin_turn(sid, TURN, 300, tier="hosted_starter",
                                             subject=scope.subject)
        with h.db.cursor() as cur:
            cur.execute("SELECT tenant_id FROM public.app_sessions WHERE session_id=%s", (sid,))
            marker = cur.fetchone()["tenant_id"]
        assert marker == f"project:{scope.org}:{scope.project}"
        assert session_store.get_session(sid)["tenant_id"] == scope.org
        for tenant in (scope.org, marker):
            assert session_store.active_turn_subject(sid, TURN, tenant, 300) == scope.subject
        other = h.scopes[1 - index]
        assert session_store.active_turn_subject(sid, TURN, other.org, 300) is None
        assert session_store.active_turn_subject(sid, "wrong-turn", scope.org, 300) is None
        authority = Authority(scope.org, scope.project, scope.user, scope.binding,
                              "auth0", scope.subject, sid, TURN, ORIGIN, operations=CURRENT_OPERATIONS)
        authorities.append(authority)
        token = "S13P-SYNTHETIC-PRIVATE-BEARER-" + str(index)
        tokens.append(token)
        callback = _callback(scope, token)
        result = callback(sid, TURN, 300)
        assert result.code == "captured"
        assert callback(sid, TURN, 300).code == "unavailable"
        assert service.resolve_current(scope.org, sid, TURN, write=True).authority == authority
        loaded = reader.load_active(authority, time.time())
        assert loaded is not None and loaded.binding.authority == authority
        assert loaded.binding.grant_id == result.grant_id
        exposed = []
        def consume(value):
            exposed.append(value)
            return value == token.encode()
        assert crypto.with_decrypted(loaded.envelope, loaded.binding, consume, keys)
        assert not any(exposed[0])
        assert token.encode() not in loaded.envelope

    assert sessions[0] != sessions[1]
    assert h.scopes[0].subject != h.scopes[1].subject
    for index, authority in enumerate(authorities):
        conn = h.connections[index]
        role = conn.execute("SELECT rolsuper,rolbypassrls FROM pg_roles "
                            "WHERE rolname=current_user").fetchone()
        assert role == (False, False)
        assert conn.execute("SELECT count(*) FROM pg_class WHERE relnamespace="
                            "'public'::regnamespace AND relowner=(SELECT oid FROM pg_roles "
                            "WHERE rolname=current_user)").fetchone()[0] == 0
        assert conn.execute("SELECT relrowsecurity,relforcerowsecurity FROM pg_class "
                            "WHERE oid='public.integration_turn_grants'::regclass").fetchone() == (True, True)
        # A raw SELECT without a WHERE must be scoped by FORCE RLS.
        assert conn.execute("SELECT count(*) FROM public.integration_turn_grants").fetchone()[0] == 0
        with scoped(conn, authority):
            rows = conn.execute("SELECT tenant_id::text,project_id::text FROM "
                                "public.integration_turn_grants").fetchall()
            assert rows == [(authority.tenant_id, authority.project_id)]
        other = authorities[1 - index]
        with pytest.raises(service.ReadFailure, match="denied"):
            service.resolve_current(authority.tenant_id, other.session_id, TURN, write=True)
        assert _callback(h.scopes[index], tokens[index])(other.session_id, TURN, 300).code == "unavailable"
        assert reader.load_active(replace(authority, session_id=other.session_id), time.time()) is None
        assert reader.load_active(replace(authority, subject=other.subject), time.time()) is None

    first, second = authorities
    # Revocation must be observed by fresh authority reads while the encrypted
    # row and active session still exist. The sibling organization stays valid.
    h.owner.execute("UPDATE public.project_member_bindings SET status='revoked',revoked_at=NOW() "
                    "WHERE membership_id=%s", (h.scopes[0].membership,))
    assert reader.load_active(first, time.time()) is not None
    with pytest.raises(service.ReadFailure, match="denied"):
        service.resolve_current(first.tenant_id, first.session_id, TURN, write=True)
    assert _callback(h.scopes[0], tokens[0])(first.session_id, TURN, 300).code == "unavailable"
    assert service.resolve_current(second.tenant_id, second.session_id, TURN, write=True).authority == second
    sibling = reader.load_active(second, time.time())
    assert sibling is not None
    assert crypto.with_decrypted(sibling.envelope, sibling.binding,
                                 lambda value: value == tokens[1].encode(), keys)

    # Replace the remaining authorized turn through the real producer.
    session_store.end_turn(second.session_id, TURN)
    assert session_store.try_begin_turn(second.session_id, "replacement-turn", 300,
                                       tier="hosted_starter", subject=second.subject)
    with pytest.raises(service.ReadFailure, match="conflict"):
        service.resolve_current(second.tenant_id, second.session_id, TURN, write=True)
    assert _callback(h.scopes[1], tokens[1])(second.session_id, TURN, 300).code == "unavailable"
    current_second = replace(second, turn_id="replacement-turn")
    assert service.resolve_current(second.tenant_id, second.session_id, "replacement-turn",
                                   write=True).authority == current_second

    # Real release invokes S11 cleanup and scrubs the encrypted material.
    for authority in (first, current_second):
        original = authorities[0] if authority == first else second
        session_store.end_turn(authority.session_id, authority.turn_id)
        assert reader.load_active(original, time.time()) is None
        with scoped(h.connections[1], original):
            row = h.connections[1].execute("SELECT envelope,revoked_at FROM "
                                           "public.integration_turn_grants").fetchone()
            assert row[0] is None and row[1] is not None
        assert session_store.get_session(authority.session_id)["active_turn_id"] is None
    assert all(not any(buf) for fake in keys.keys.values() for buf in fake.buffers)
    output = caplog.text + str(capsys.readouterr())
    assert all(token not in output for token in tokens)
    assert "postgresql://" not in output and "postgres://" not in output


@pytest.fixture
def bootstrap_database():
    """Independent logins; the bootstrap itself never authenticates as owner."""
    import secrets
    from types import SimpleNamespace
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict
    from server.tests.test_integration_turn_grant_postgres import _safe_dsn

    dsn = _safe_dsn()
    params = conninfo_to_dict(dsn)
    owner = psycopg.connect(dsn, autocommit=True, connect_timeout=2)
    suffix = secrets.token_hex(6)
    master_name, app_name = "custody_master_" + suffix, "custody_app_" + suffix
    class Harness(SimpleNamespace):
        def __repr__(self):
            return "<disposable custody database; credentials withheld>"
    roles, connections = [], []
    started = False
    try:
        assert owner.execute("SELECT current_database()").fetchone()[0] == "ops_integration_grant_test"
        assert int(owner.execute("SHOW server_version_num").fetchone()[0]) // 10000 == 16
        # Do not take ownership of, mutate, or clean preexisting objects or roles.
        assert not owner.execute("""SELECT 1 FROM pg_class c JOIN pg_namespace n
            ON n.oid=c.relnamespace WHERE n.nspname='public'""").fetchone()
        assert not owner.execute("""SELECT 1 FROM pg_proc p JOIN pg_namespace n
            ON n.oid=p.pronamespace WHERE n.nspname='public'""").fetchone()
        assert not owner.execute("SELECT 1 FROM pg_namespace WHERE nspname='leaf_platform'").fetchone()
        assert not owner.execute("SELECT 1 FROM pg_roles WHERE rolname='leaf_integration_grant_app'").fetchone()
        master_password, app_password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        for name, password, create in ((master_name, master_password, "CREATEROLE"),
                                       (app_name, app_password, "NOCREATEROLE")):
            owner.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS "
                                  "NOCREATEDB NOREPLICATION " + create + " PASSWORD {}").format(
                                      sql.Identifier(name), sql.Literal(password)))
            roles.append(name)
        owner.execute(sql.SQL("GRANT CREATE ON SCHEMA public TO {}").format(sql.Identifier(master_name)))
        # The producer fixture may have recreated public without PUBLIC USAGE.
        # Only this fixture's master receives the ability to grant runtime USAGE.
        owner.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {} WITH GRANT OPTION").format(
            sql.Identifier(master_name)))
        owner.execute(sql.SQL("CREATE SCHEMA leaf_platform AUTHORIZATION {}").format(sql.Identifier(app_name)))
        started = True
        master = psycopg.connect(**dict(params, user=master_name, password=master_password),
                                 autocommit=True, connect_timeout=2)
        connections.append(master)
        app = psycopg.connect(**dict(params, user=app_name, password=app_password),
                              autocommit=True, connect_timeout=2)
        connections.append(app)
        app.execute("""CREATE TABLE leaf_platform.leaf_schema_migrations (
            name TEXT PRIMARY KEY, sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        # An unrelated historical receipt must survive the fixed-purpose CLI.
        app.execute("INSERT INTO leaf_platform.leaf_schema_migrations(name,sha256) VALUES ('prior.sql',%s)",
                    ("a" * 64,))
        environ = dict(MASTER_USERNAME=master_name, MASTER_PASSWORD=master_password,
                       APP_USERNAME=app_name, APP_PASSWORD=app_password,
                       DB_HOST=params["host"], DB_NAME=params["dbname"],
                       DB_PORT=params.get("port", "5432"), DB_SSLMODE="disable")
        yield Harness(owner=owner, master=master, app=app, environ=environ,
                      master_name=master_name, app_name=app_name)
    finally:
        for conn in connections:
            conn.close()
        if started:
            owner.execute("DROP SCHEMA leaf_platform CASCADE")
            # public was admitted empty. Removing the owned fixture schema also
            # removes USAGE granted by the non-superuser master, whose grantor
            # differs from this cleanup login on PostgreSQL 16.
            owner.execute("DROP SCHEMA public CASCADE")
            owner.execute("CREATE SCHEMA public")
            if owner.execute("SELECT 1 FROM pg_roles WHERE rolname='leaf_integration_grant_app'").fetchone():
                owner.execute("DROP OWNED BY leaf_integration_grant_app")
                owner.execute("DROP ROLE leaf_integration_grant_app")
        for role in roles:
            owner.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            owner.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        owner.close()


def _bootstrap_snapshot(h):
    """Catalog identities and ACLs as well as data catch writes during inspect/replay."""
    return (
        h.owner.execute("""SELECT n.oid,n.nspname,n.nspowner,n.nspacl::text
            FROM pg_namespace n WHERE n.nspname IN ('public','leaf_platform') ORDER BY n.nspname""").fetchall(),
        h.owner.execute("""SELECT c.oid,c.relname,c.relowner,c.relacl::text,
            c.relrowsecurity,c.relforcerowsecurity FROM pg_class c JOIN pg_namespace n
            ON n.oid=c.relnamespace WHERE n.nspname IN ('public','leaf_platform') ORDER BY c.oid""").fetchall(),
        h.owner.execute("""SELECT p.oid,p.proowner,pg_get_functiondef(p.oid)
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='public' ORDER BY p.oid""").fetchall(),
        h.owner.execute("""SELECT name,sha256,applied_at FROM leaf_platform.leaf_schema_migrations
            ORDER BY name""").fetchall(),
        h.owner.execute("""SELECT oid,rolname,rolcanlogin,rolsuper,rolbypassrls,rolcreaterole,
            rolcreatedb,rolreplication,rolinherit FROM pg_roles ORDER BY oid""").fetchall(),
        h.owner.execute("SELECT * FROM pg_auth_members ORDER BY roleid,member,grantor").fetchall(),
    )


def test_non_superuser_custody_bootstrap_inspect_apply_replay(bootstrap_database, monkeypatch, capsys):
    import psycopg
    from hashlib import sha256
    from server import integration_custody_bootstrap as bootstrap
    from leaf_platform.integration_turn_grant_store import _GUARD
    from server.tests.test_integration_turn_grant_crypto import binding
    from server.integration_turn_grant_crypto import seal
    h = bootstrap_database
    assert h.master.execute("SELECT rolsuper,rolcreaterole FROM pg_roles WHERE rolname=current_user").fetchone() == (False, True)
    assert h.app.execute("SELECT has_schema_privilege(%s,'leaf_platform','USAGE'), "
                         "has_schema_privilege(%s,'leaf_platform','CREATE')",
                         (h.master_name, h.master_name)).fetchone() == (False, False)
    before = _bootstrap_snapshot(h)
    for key, value in h.environ.items():
        monkeypatch.setenv(key, value)
    # Exercise the actual default command path and its one JSON receipt.
    assert bootstrap.main([]) == 0
    import json
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["mode"] == "inspect" and inspected["ok"] and not inspected["ready"]
    assert [step["state"] for step in inspected["steps"]] == ["pending", "pending"]
    assert _bootstrap_snapshot(h) == before
    db = platform_link.platform_db()
    old_pool = db._pool
    result = bootstrap.bootstrap("apply", h.environ)
    assert result["ok"] and result["ready"], result
    assert db._pool is old_pool
    migration_dir = Path(__file__).resolve().parents[2] / "platform/migrations"
    expected = [(name, sha256((migration_dir / name).read_bytes()).hexdigest())
                for name in bootstrap.MIGRATIONS]
    assert [(step["name"], step["sha256"]) for step in result["steps"]] == expected
    assert h.app.execute("SELECT name,sha256 FROM leaf_platform.leaf_schema_migrations "
                         "WHERE name <> 'prior.sql' ORDER BY name").fetchall() == expected
    assert h.app.execute("SELECT sha256 FROM leaf_platform.leaf_schema_migrations "
                         "WHERE name='prior.sql'").fetchone() == ("a" * 64,)
    assert h.app.execute("SELECT pg_get_userbyid(relowner) FROM pg_class "
                         "WHERE oid='public.integration_turn_grants'::regclass").fetchone() == (h.master_name,)
    assert h.app.execute(_GUARD).fetchone() == (True,)
    assert h.app.execute("SELECT pg_has_role(current_user,%s,'MEMBER')", (h.master_name,)).fetchone() == (False,)
    assert h.app.execute("SELECT has_schema_privilege(%s,'leaf_platform','USAGE'), "
                         "has_schema_privilege(%s,'leaf_platform','CREATE'), "
                         "has_table_privilege(%s,'leaf_platform.leaf_schema_migrations','SELECT'), "
                         "has_table_privilege(%s,'leaf_platform.leaf_schema_migrations','INSERT')",
                         (h.master_name,) * 4).fetchone() == (False,) * 4
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        h.app.execute("CREATE ROLE custody_must_never_be_created")
    store = IntegrationTurnGrantStore(partial(connection_factory, h.app))
    now = time.time()
    b = replace(binding(), issued_at=now, verified_jwt_exp=now + 120,
                turn_deadline=now + 110, expires_at=now + 100)
    assert store.put_once(b, seal(b"bootstrap-private-bearer", b, FakeKeys())) == "created"
    assert store.load_active(b.authority, now + 1).binding == b
    assert h.app.execute("SELECT count(*) FROM public.integration_turn_grants").fetchone() == (0,)
    with scoped(h.app, b.authority):
        assert h.app.execute("SELECT tenant_id::text FROM public.integration_turn_grants").fetchall() == [(b.authority.tenant_id,)]
    after = _bootstrap_snapshot(h)
    assert bootstrap.bootstrap("apply", h.environ)["ready"]
    assert bootstrap.bootstrap(environ=h.environ)["ready"]
    assert _bootstrap_snapshot(h) == after
    # Existing effective access also survives temporary-ACL cleanup.
    from psycopg import sql
    h.app.execute(sql.SQL("GRANT USAGE ON SCHEMA leaf_platform TO {}").format(sql.Identifier(h.master_name)))
    assert bootstrap.bootstrap("apply", h.environ)["ready"]
    assert h.app.execute("SELECT has_schema_privilege(%s,'leaf_platform','USAGE')",
                         (h.master_name,)).fetchone() == (True,)
    output = str(capsys.readouterr())
    assert all(h.environ[key] not in output for key in ("MASTER_PASSWORD", "APP_PASSWORD"))


def test_custody_bootstrap_hash_drift_refuses_before_writes(bootstrap_database):
    from server import integration_custody_bootstrap as bootstrap
    h = bootstrap_database
    h.app.execute("INSERT INTO leaf_platform.leaf_schema_migrations(name,sha256) VALUES (%s,%s)",
                  (bootstrap.MIGRATIONS[1], "b" * 64))
    before = _bootstrap_snapshot(h)
    receipt = bootstrap.bootstrap("apply", h.environ)
    assert not receipt["ok"] and receipt["error"] == "MIGRATION_HASH_DRIFT"
    assert _bootstrap_snapshot(h) == before


def test_custody_bootstrap_unsafe_runtime_role_refuses(bootstrap_database):
    from server import integration_custody_bootstrap as bootstrap
    h = bootstrap_database
    # Fixture owner creates the hazard; the restricted master must refuse it.
    h.owner.execute("CREATE ROLE leaf_integration_grant_app LOGIN NOSUPERUSER NOBYPASSRLS")
    before = _bootstrap_snapshot(h)
    receipt = bootstrap.bootstrap("apply", h.environ)
    assert not receipt["ok"] and receipt["error"] == "RUNTIME_ROLE_UNSAFE"
    assert _bootstrap_snapshot(h) == before


def test_custody_bootstrap_partial_progress_and_acl_recovery(bootstrap_database):
    from psycopg import sql
    from server import integration_custody_bootstrap as bootstrap
    h = bootstrap_database
    # Preserve an original privilege while provisioning and later removing the
    # other three missing migration privileges.
    h.app.execute(sql.SQL("GRANT USAGE ON SCHEMA leaf_platform TO {}").format(sql.Identifier(h.master_name)))
    h.app.execute(sql.SQL("ALTER TABLE leaf_platform.leaf_schema_migrations ADD CONSTRAINT "
                          "fixture_reject_second_receipt CHECK (name <> {})").format(
                              sql.Literal(bootstrap.MIGRATIONS[1])))
    result = bootstrap.bootstrap("apply", h.environ)
    assert not result["ok"] and not result["ready"]
    assert [step["state"] for step in result["steps"]] == ["applied", "pending"]
    assert h.app.execute("SELECT name FROM leaf_platform.leaf_schema_migrations "
                         "WHERE name <> 'prior.sql'").fetchall() == [(bootstrap.MIGRATIONS[0],)]
    assert h.app.execute("SELECT has_schema_privilege(%s,'leaf_platform','USAGE'), "
                         "has_schema_privilege(%s,'leaf_platform','CREATE'), "
                         "has_table_privilege(%s,'leaf_platform.leaf_schema_migrations','SELECT'), "
                         "has_table_privilege(%s,'leaf_platform.leaf_schema_migrations','INSERT')",
                         (h.master_name,) * 4).fetchone() == (True, False, False, False)
    # The second migration's DDL must have rolled back with its failed receipt.
    assert h.owner.execute("SELECT 1 FROM pg_constraint WHERE conrelid="
                           "'public.integration_turn_grants'::regclass "
                           "AND conname='integration_turn_grants_check2'").fetchone() == (1,)
    h.app.execute("ALTER TABLE leaf_platform.leaf_schema_migrations "
                  "DROP CONSTRAINT fixture_reject_second_receipt")
    assert bootstrap.bootstrap("apply", h.environ)["ready"]
    assert h.app.execute("SELECT has_schema_privilege(%s,'leaf_platform','USAGE')",
                         (h.master_name,)).fetchone() == (True,)


def test_custody_bootstrap_refuses_app_owner_membership(bootstrap_database):
    from psycopg import sql
    from server import integration_custody_bootstrap as bootstrap
    h = bootstrap_database
    h.owner.execute(sql.SQL("GRANT {} TO {}").format(
        sql.Identifier(h.master_name), sql.Identifier(h.app_name)))
    before = _bootstrap_snapshot(h)
    receipt = bootstrap.bootstrap("apply", h.environ)
    assert not receipt["ok"] and receipt["error"] == "OWNER_MEMBERSHIP_UNSAFE"
    assert _bootstrap_snapshot(h) == before
