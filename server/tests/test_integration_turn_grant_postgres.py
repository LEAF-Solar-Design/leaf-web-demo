"""Explicit opt-in isolation proof, ONLY on a named disposable loopback DB.

Owner setup and two independently authenticated restricted logins are separate.
No default DSN, env-file loading, SET ROLE, or production migrations are used.
"""
from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import secrets
import sys
import time
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from server.tests.test_integration_turn_grant_crypto import binding, FakeKeys
from server.integration_turn_grant_crypto import seal, with_decrypted
from leaf_platform.integration_turn_grant_store import (
    IntegrationTurnGrantStore, GrantStoreError, TenantProject,
)

DSN_ENV = "INTEGRATION_GRANT_TEST_DATABASE_URL"


def _safe_dsn():
    dsn = os.environ.get(DSN_ENV, "")
    if not dsn:
        pytest.skip("INTEGRATION_GRANT_TEST_DATABASE_URL absent: PostgreSQL isolation unverified")
    try:
        parsed = urlsplit(dsn)
        options = parse_qsl(parsed.query, strict_parsing=True)
        safe_options = (not options or (len(options) == 1 and options[0][0] == "sslmode"
                                       and options[0][1] in ("disable", "prefer", "require")))
        valid = (parsed.scheme in ("postgres", "postgresql")
                 and parsed.hostname in ("localhost", "127.0.0.1", "::1")
                 and parsed.path == "/ops_integration_grant_test"
                 and safe_options and not parsed.fragment
                 and (parsed.port is None or 1 <= parsed.port <= 65535))
    except ValueError:
        valid = False
    if not valid:
        pytest.fail("disposable database DSN rejected", pytrace=False)
    return dsn


@pytest.fixture
def database():
    dsn = _safe_dsn()
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict
    suffix = secrets.token_hex(6)
    roles = ["grant_test_a_" + suffix, "grant_test_b_" + suffix]
    owner = psycopg.connect(dsn, autocommit=True, connect_timeout=2)
    connections = []
    created_roles = []
    created_table = False
    try:
        # Refuse a nonempty DB rather than deleting or reusing any caller data.
        assert owner.execute("SELECT current_database()").fetchone()[0] == "ops_integration_grant_test"
        existing = owner.execute("""SELECT count(*) FROM pg_class c JOIN pg_namespace n
            ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m')""").fetchone()[0]
        if existing:
            pytest.fail("disposable database must be empty", pytrace=False)
        migration = Path(__file__).resolve().parents[2] / "platform/migrations/0079_integration_turn_grants.sql"
        with owner.transaction():
            owner.execute(migration.read_text(encoding="utf-8"))
        created_table = True
        params = conninfo_to_dict(dsn)
        for role in roles:
            password = secrets.token_urlsafe(32)
            owner.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)))
            created_roles.append(role)
            owner.execute(sql.SQL("GRANT leaf_integration_grant_app TO {}").format(sql.Identifier(role)))
            connections.append(psycopg.connect(**dict(params, user=role, password=password,
                                                       connect_timeout=2), autocommit=True))
        yield owner, connections
    finally:
        for conn in connections:
            conn.close()
        if created_table:
            owner.execute("DROP TABLE public.integration_turn_grants")
            owner.execute("DROP FUNCTION public.guard_integration_turn_grant()")
        for role in created_roles:
            owner.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        owner.close()


def _factory(conn):
    @contextmanager
    def factory():
        yield conn
    return factory


@contextmanager
def scoped(conn, authority):
    with conn.transaction():
        conn.execute("SELECT set_config('leaf.integration_tenant_id', %s, true), "
                     "set_config('leaf.integration_project_id', %s, true)",
                     (authority.tenant_id, authority.project_id))
        yield


def test_real_restricted_postgres_custody_and_isolation(database, caplog, capsys):
    import psycopg
    owner, (c1, c2) = database
    s1, s2 = IntegrationTurnGrantStore(_factory(c1)), IntegrationTurnGrantStore(_factory(c2))
    now = time.time()
    b = replace(binding(), issued_at=now, verified_jwt_exp=now + 120,
                turn_deadline=now + 110, expires_at=now + 100)
    keys = FakeKeys()
    token = b"postgres-proof-private-bearer"
    envelope = seal(token, b, keys)
    assert s1.put_once(b, envelope) == "created"
    assert s2.put_once(b, envelope) == "already_present"
    loaded = s2.load_active(b.authority, now + 1)
    assert loaded.binding == b and loaded.envelope == envelope
    assert with_decrypted(loaded.envelope, loaded.binding, lambda v: v == token, keys)
    assert s2.put_once(replace(b, expires_at=now + 101), envelope) == "conflict"
    assert s1.load_active(b.authority, now + 100) is None
    assert s1.load_active(replace(b.authority, subject="auth0|foreign"), now + 1) is None
    assert s1.revoke_turn(replace(b.authority, subject="auth0|foreign"), now + 1) == 0

    # Deliberately collide grant/session/turn IDs across tenant AND project.
    foreign = replace(b, authority=replace(b.authority, tenant_id=str(uuid4())))
    other_project = replace(b, authority=replace(b.authority, project_id=str(uuid4())))
    e2 = seal(token, foreign, FakeKeys())
    e3 = seal(token, other_project, FakeKeys())
    assert s2.put_once(foreign, e2) == "created"
    assert s2.put_once(other_project, e3) == "created"
    assert s1.load_active(foreign.authority, now + 1).envelope == e2
    for auth in (b.authority, foreign.authority, other_project.authority):
        with scoped(c1, auth):
            # Intentionally no WHERE: real RLS, not a store predicate.
            rows = c1.execute("SELECT tenant_id::text, project_id::text, binding, envelope "
                              "FROM public.integration_turn_grants").fetchall()
            assert len(rows) == 1 and rows[0][:2] == (auth.tenant_id, auth.project_id)
            assert token.decode() not in json.dumps(rows[0][2])
            assert token not in bytes(rows[0][3])
        assert c1.execute("SELECT count(*) FROM public.integration_turn_grants").fetchone()[0] == 0
        settings = c1.execute("SELECT current_setting('leaf.integration_tenant_id', true), "
                              "current_setting('leaf.integration_project_id', true)").fetchone()
        assert all(value in (None, "") for value in settings)

    # WITH CHECK denies even direct cross-scope INSERT and UPDATE.
    with pytest.raises(psycopg.Error):
        with scoped(c1, b.authority):
            c1.execute("UPDATE public.integration_turn_grants SET tenant_id=%s", (foreign.authority.tenant_id,))
    denied = replace(foreign, grant_id=str(uuid4()),
                     authority=replace(foreign.authority, turn_id="denied-turn"))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with scoped(c1, b.authority):
            c1.execute("""INSERT INTO public.integration_turn_grants
                (tenant_id,project_id,session_id,turn_id,grant_id,binding,created_at,expires_at,envelope)
                VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)""",
                (denied.authority.tenant_id, denied.authority.project_id, denied.authority.session_id,
                 denied.authority.turn_id, denied.grant_id, json.dumps(denied.to_dict()),
                 denied.issued_at, denied.expires_at, e2))

    for sql in (
        "UPDATE public.integration_turn_grants SET expires_at=expires_at+1",
        "UPDATE public.integration_turn_grants SET binding=jsonb_set(binding,'{subject}','\"other\"')",
        "UPDATE public.integration_turn_grants SET envelope=decode('00','hex')",
    ):
        with pytest.raises(psycopg.Error):
            with scoped(c1, b.authority):
                c1.execute(sql)

    assert s1.revoke_turn(b.authority, now + 2) == 1
    assert s2.load_active(b.authority, now + 3) is None
    assert s2.revoke_turn(b.authority, now + 3) == 0
    assert s2.put_once(b, envelope) == "conflict"
    with scoped(c2, b.authority):
        tombstone = c2.execute("SELECT envelope,revoked_at,binding FROM public.integration_turn_grants").fetchone()
        assert tombstone[0] is None and tombstone[1] == now + 2 and tombstone[2] == b.to_dict()
    with pytest.raises(psycopg.Error):
        with scoped(c2, b.authority):
            c2.execute("UPDATE public.integration_turn_grants SET revoked_at=NULL, envelope=%s", (envelope,))

    sibling = replace(foreign, grant_id=str(uuid4()), expires_at=now + 99,
                      authority=replace(foreign.authority, turn_id="second-expired-turn"))
    assert s2.put_once(sibling, seal(token, sibling, FakeKeys())) == "created"
    scope = TenantProject(foreign.authority.tenant_id, foreign.authority.project_id)
    assert s1.purge_expired(scope, now + 101, 1) == 1
    with scoped(c2, foreign.authority):
        assert c2.execute("SELECT count(*) FROM public.integration_turn_grants WHERE envelope IS NOT NULL").fetchone()[0] == 1
    assert s1.purge_expired(scope, now + 101, 1) == 1
    with scoped(c2, foreign.authority):
        assert c2.execute("SELECT envelope FROM public.integration_turn_grants").fetchone()[0] is None
    assert s2.load_active(other_project.authority, now + 1) is not None

    # Owner/superuser is never a usable application identity.
    with pytest.raises(GrantStoreError, match="GRANT_UNSAFE_DATABASE"):
        IntegrationTurnGrantStore(_factory(owner)).load_active(b.authority, now + 1)
    from psycopg import sql
    role = c2.execute("SELECT current_user").fetchone()[0]
    owner.execute(sql.SQL("ALTER ROLE {} BYPASSRLS").format(sql.Identifier(role)))
    try:
        with pytest.raises(GrantStoreError, match="GRANT_UNSAFE_DATABASE"):
            s2.load_active(b.authority, now + 1)
    finally:
        owner.execute(sql.SQL("ALTER ROLE {} NOBYPASSRLS").format(sql.Identifier(role)))
    owner.execute("ALTER TABLE public.integration_turn_grants NO FORCE ROW LEVEL SECURITY")
    try:
        with pytest.raises(GrantStoreError, match="GRANT_UNSAFE_DATABASE"):
            s1.load_active(b.authority, now + 1)
    finally:
        owner.execute("ALTER TABLE public.integration_turn_grants FORCE ROW LEVEL SECURITY")
    owner.execute("ALTER TABLE public.integration_turn_grants DISABLE ROW LEVEL SECURITY")
    try:
        with pytest.raises(GrantStoreError, match="GRANT_UNSAFE_DATABASE"):
            s2.load_active(b.authority, now + 1)
    finally:
        owner.execute("ALTER TABLE public.integration_turn_grants ENABLE ROW LEVEL SECURITY")
    owner.execute("DROP POLICY integration_turn_grant_scope ON public.integration_turn_grants")
    with pytest.raises(GrantStoreError, match="GRANT_UNSAFE_DATABASE") as exc:
        s2.load_active(b.authority, now + 1)
    assert token.decode() not in str(exc.value) + caplog.text + str(capsys.readouterr())
