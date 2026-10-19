"""Independent-connection proofs for the complete durable demo mint transaction."""
from concurrent.futures import ThreadPoolExecutor
import os
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
import uuid

from fastapi import HTTPException
import pytest

# Reuse the isolated schema and full migration chain, never shared table truncation.
from test_demo_sessions_postgres import postgres_registry, request
import demo_sessions as demo


real_loader = demo._db

TABLES = ("demo_mint_counters", "demo_sessions", "orgs", "identity_bindings",
          "projects", "project_authority_modes", "project_member_bindings", "broker_tenants")
STATEMENTS = ("demo_mint_counters", "demo_mint_counters", "demo_sessions", "orgs",
              "identity_bindings", "projects", "project_authority_modes",
              "project_member_bindings", "broker_tenants")


def _schema_url(database_url: str, schema: str) -> str:
    parts = urlsplit(database_url)
    options = urlencode({"options": "-csearch_path=" + schema})
    query = parts.query + "&" + options if parts.query else options
    return urlunsplit(parts._replace(query=query))


def test_schema_url_preserves_url_and_adds_search_path():
    db = real_loader()
    schema = "demo_test_schema"
    for scheme in ("postgres", "postgresql"):
        for query in ("", "sslmode=require&application_name=demo%20test&tag=one&tag=two"):
            database_url = f"{scheme}://demo:p%40ss@localhost:5433/demo_db"
            if query:
                database_url += "?" + query
            url = _schema_url(database_url, schema)
            original, scoped = urlsplit(database_url), urlsplit(url)
            assert scoped.scheme == original.scheme
            assert scoped._replace(query=original.query) == original
            assert parse_qs(scoped.query) == {
                **parse_qs(original.query), "options": ["-csearch_path=" + schema]}
            db.validate_database_url(url)


def snapshot(state):
    with state.connect() as conn:
        return {table: sorted((dict(row) for row in conn.execute("SELECT * FROM " + table)),
                              key=repr) for table in TABLES}


def joined(state, minted):
    """Assert the exact relation and every org/project reference, not just totals."""
    record_id = minted.token.split(".")[0]
    with state.connect() as conn:
        rows = conn.execute(
            "SELECT s.*, o.name AS org_name, o.tier, o.account_class, "
            "b.binding_id, b.platform_user_id, b.external_authority, b.external_subject, "
            "b.role AS binding_role, p.project_id, p.name AS project_name, "
            "a.authority_mode, a.selected_by, m.membership_id, m.role AS member_role, "
            "m.invited_by_binding_id, t.tier AS broker_tier, t.disabled "
            "FROM demo_sessions s JOIN orgs o ON o.org_id = s.org_id "
            "JOIN identity_bindings b ON b.platform_tenant_id = o.org_id "
            "JOIN projects p ON p.org_id = o.org_id "
            "JOIN project_authority_modes a ON (a.org_id, a.project_id) = (o.org_id, p.project_id) "
            "JOIN project_member_bindings m ON (m.org_id, m.project_id, m.binding_id) = "
            "(o.org_id, p.project_id, b.binding_id) "
            "JOIN broker_tenants t ON t.tenant_id = s.tenant_id WHERE s.id = %s",
            (record_id,)).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["org_name"] == "Leaf demo " + row["id"].hex[:8]
        assert row["subject"] == row["external_subject"] == "demo:" + record_id
        assert row["tenant_id"] == "demo-" + record_id
        assert row["tier"] == row["broker_tier"] == "demo_live"
        assert row["account_class"] == "demo"
        assert row["external_authority"] == "leaf-demo"
        assert row["binding_role"] == "read_only"
        assert row["project_name"] == "Sample rooftop"
        assert row["authority_mode"] == "postgres_canonical"
        assert row["selected_by"] == "server"
        assert row["member_role"] == "editor"
        assert row["invited_by_binding_id"] == row["binding_id"]
        assert row["disabled"] is False
        from psycopg import sql
        references = conn.execute(
            "SELECT c.table_name, c.column_name FROM information_schema.columns c "
            "JOIN information_schema.tables t ON "
            "(t.table_schema, t.table_name) = (c.table_schema, c.table_name) "
            "WHERE c.table_schema = %s AND t.table_type = 'BASE TABLE' AND c.column_name IN "
            "('org_id', 'platform_tenant_id', 'project_id')", (state.schema,)).fetchall()
        expected_org = {"demo_sessions", "orgs", "projects", "project_authority_modes",
                        "project_member_bindings", "identity_bindings"}
        expected_project = {"projects", "project_authority_modes", "project_member_bindings"}
        for ref in references:
            project = ref["column_name"] == "project_id"
            count = conn.execute(sql.SQL("SELECT count(*) AS n FROM {} WHERE {}::text = %s").format(
                sql.Identifier(ref["table_name"]), sql.Identifier(ref["column_name"])),
                (str(row["project_id"] if project else row["org_id"]),)).fetchone()["n"]
            assert count == int(ref["table_name"] in (
                expected_project if project else expected_org)), ref
        return row


def test_exact_tenant_relation_and_generated_ids(postgres_registry, monkeypatch):
    original = demo.provision_demo_tenant
    results = []

    def capture(conn, session):
        tenant = original(conn, session)
        results.append(tenant)
        return tenant

    monkeypatch.setattr(demo, "provision_demo_tenant", capture)
    row = joined(postgres_registry, demo.mint(request("192.0.2.1")))
    tenant, = results
    assert tenant.session_id == row["id"]
    for field in tenant._fields[1:]:
        assert getattr(tenant, field) == row[field]


@pytest.mark.parametrize("failure_after", range(1, 11))
def test_every_mint_statement_rolls_back(postgres_registry, monkeypatch, failure_after):
    state = postgres_registry
    demo.mint(request("192.0.2.1"))
    before = snapshot(state)
    original = demo._db().run_transaction
    seen = []

    class FailingConnection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, statement, params):
            result = self.conn.execute(statement, params)
            seen.append(statement.split("INSERT INTO ")[1].split()[0])
            if len(seen) == failure_after:
                raise RuntimeError("injected after statement")
            return result

    def run(operation, **kwargs):
        def fail(conn):
            result = operation(FailingConnection(conn))
            if failure_after == 10:
                raise RuntimeError("injected before callback return")
            return result
        return original(fail, **kwargs)

    monkeypatch.setattr(demo, "_db", lambda: SimpleNamespace(run_transaction=run))
    with pytest.raises(HTTPException) as error:
        demo.mint(request("192.0.2.2"))
    assert error.value.status_code == 503
    assert seen == list(STATEMENTS[:min(failure_after, 9)])
    assert snapshot(state) == before


@pytest.mark.parametrize("cap", [100, 7])
def test_twenty_independent_mints_and_concurrent_refusal(postgres_registry, monkeypatch, cap):
    state = postgres_registry
    monkeypatch.setenv("LEAF_DEMO_PER_IP_DAILY_CAP", "100")
    monkeypatch.setenv("LEAF_DEMO_GLOBAL_DAILY_CAP", str(cap))
    state.barrier = threading.Barrier(20)

    def attempt(index):
        try:
            return demo.mint(request(f"198.51.100.{index + 1}"))
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=20) as pool:
        outcomes = list(pool.map(attempt, range(20)))
    state.barrier = None
    successes = [item for item in outcomes if isinstance(item, demo.MintedSession)]
    expected = min(cap, 20)
    assert len(successes) == expected, outcomes
    assert outcomes.count(429) == 20 - expected
    assert len(state.backend_ids) == 20
    rows = [joined(state, item) for item in successes]
    for key in ("id", "org_id", "binding_id", "platform_user_id", "project_id",
                "membership_id", "tenant_id"):
        assert len({row[key] for row in rows}) == expected
    contents = snapshot(state)
    for table in TABLES[1:]:
        assert len(contents[table]) == expected, table
    counters = {row["scope"]: row["count"] for row in contents["demo_mint_counters"]}
    assert counters.pop("global") == expected
    assert len(counters) == expected
    assert set(counters.values()) == {1}


def test_real_shared_loader_mint_round_trip(postgres_registry, monkeypatch):
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    db = real_loader()
    db.reset_pool()
    database_url = os.environ["DATABASE_URL"]
    schema = "demo_loader_test_" + uuid.uuid4().hex
    url = _schema_url(database_url, schema)
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        monkeypatch.setattr(db, "get_database_url", lambda: url)
        monkeypatch.setattr(demo, "_db", real_loader)
        db.apply_migration()
        minted = demo.mint(request("192.0.2.1"))
        record = demo.verify(minted.token)
        state = SimpleNamespace(schema=schema, connect=lambda: psycopg.connect(
            url, row_factory=dict_row))
        assert record.id == joined(state, minted)["id"]
    finally:
        db.reset_pool()
        with psycopg.connect(database_url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_disabled_mint_never_calls_database_or_provisioning(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("disabled demo reached database or provisioning")

    monkeypatch.setenv("LEAF_DEMO_LIVE_ENABLED", "0")
    monkeypatch.setattr(demo, "_db", forbidden)
    monkeypatch.setattr(demo, "provision_demo_tenant", forbidden)
    with pytest.raises(HTTPException) as error:
        demo.mint(request("192.0.2.1"))
    assert error.value.status_code == 404
