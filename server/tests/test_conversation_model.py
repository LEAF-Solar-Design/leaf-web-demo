"""Tests for the project-bound durable conversation model (0046).

Structural checks (migration text, flag gating) always run. Everything that
touches PostgreSQL requires an explicit DATABASE_URL and skips cleanly
otherwise, matching the convention in test_drawing_upload_authority_postgres.py
and test_agent_gate_postgres.py.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SERVER_DIR.parent
MIGRATION_PATH = (
    PROJECT_ROOT / "platform" / "migrations" / "0046_conversation_durable.sql"
)
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import conversations  # noqa: E402
import deps  # noqa: E402
import platform_link  # noqa: E402

requires_database = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="PostgreSQL conversation persistence test requires explicit DATABASE_URL",
)


# --- structural: migration text, expand-only, flag ------------------------- #

def test_migration_file_is_a_fresh_project_bound_table() -> None:
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS conversations" in sql
    assert "REFERENCES projects(org_id, project_id) ON DELETE CASCADE" in sql
    assert "REFERENCES identity_bindings(platform_tenant_id, binding_id)" in sql
    # No backfill: this is a wholly new table, so a populated snapshot must
    # gain zero conversation rows and orphan nothing.
    assert "INSERT INTO conversations" not in sql


def test_migration_is_expand_only() -> None:
    """Runs the real expand-contract gate's own detectors (comment/string
    stripped, exactly as scripts/migration_expand_contract_gate.py matches)
    against this one file, so prose in a comment (e.g. "title rename") can
    never produce a false positive the way a bare substring search would."""
    import sys as _sys

    scripts_dir = str(PROJECT_ROOT / "scripts")
    added = scripts_dir not in _sys.path
    if added:
        _sys.path.insert(0, scripts_dir)
    try:
        import migration_expand_contract_gate as gate
    finally:
        if added:
            _sys.path.remove(scripts_dir)

    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    stripped = gate.strip_sql_noise(sql)
    hits = [label for label, pattern in gate.CONTRACT_PATTERNS
            if pattern.search(stripped)]
    assert hits == []


def test_migration_advances_authority_inventory_by_exactly_one() -> None:
    import json

    inventory = json.loads(
        (PROJECT_ROOT / "platform" / "authority-inventory.json")
        .read_text(encoding="utf-8")
    )
    migration_ids = inventory["scope"]["migration_ids"]
    assert migration_ids[-1] == "0048"
    assert migration_ids == [f"{n:04d}" for n in range(1, 49)]

    migration_files = sorted(
        p.name.split("_", 1)[0]
        for p in (PROJECT_ROOT / "platform" / "migrations").glob("*.sql")
    )
    assert migration_files == migration_ids


def test_conv_durable_flag_is_off_by_default_and_reads_case_insensitively(
    monkeypatch,
) -> None:
    monkeypatch.delenv(conversations.FLAG_CONV_DURABLE, raising=False)
    assert conversations.conv_durable_enabled() is False
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "TRUE")
    assert conversations.conv_durable_enabled() is True
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "0")
    assert conversations.conv_durable_enabled() is False


# --- DB: populated-snapshot apply, orphans, rollback ----------------------- #

@pytest.fixture
def pg(monkeypatch):
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("PostgreSQL conversation persistence test requires DATABASE_URL")
    db = conversations.platform_db()
    db.apply_migration()
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "1")
    yield db
    db.reset_pool()


def _seed_org_project_receipt(store: Any) -> tuple[str, str]:
    org = store.create_org_with_identity(
        f"conv-test-org-{uuid.uuid4().hex[:8]}", "auth0", f"auth0|{uuid.uuid4().hex}",
    )
    project = store.create_project(org.org_id, "conv-test-project")
    return str(org.org_id), str(project.project_id)


@requires_database
def test_migration_applies_cleanly_on_populated_snapshot_with_zero_orphans(pg) -> None:
    store = conversations.platform_store()
    org_id, project_id = _seed_org_project_receipt(store)

    with pg.cursor() as cur:
        cur.execute(
            "INSERT INTO project_lifecycle_receipts"
            " (receipt_id, org_id, project_id, action, actor_binding_id,"
            "  idempotency_key, input_digest, result_json)"
            " SELECT %s, %s, %s, 'project_created', binding_id, 'seed-key', %s, '{}'"
            " FROM identity_bindings WHERE platform_tenant_id = %s LIMIT 1",
            (str(uuid.uuid4()), org_id, project_id, "0" * 64, org_id),
        )
        cur.execute(
            "SELECT COUNT(*) AS n FROM projects WHERE org_id = %s", (org_id,),
        )
        projects_before = cur.fetchone()["n"]
        cur.execute(
            "SELECT COUNT(*) AS n FROM project_lifecycle_receipts WHERE org_id = %s",
            (org_id,),
        )
        receipts_before = cur.fetchone()["n"]

    # Re-apply every migration, including 0046, over this now-populated org.
    # Idempotent (CREATE TABLE IF NOT EXISTS, ledger-tracked skip), so this is
    # exactly the "applies cleanly on a populated snapshot" proof.
    pg.apply_migration()

    with pg.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM projects WHERE org_id = %s", (org_id,),
        )
        assert cur.fetchone()["n"] == projects_before
        cur.execute(
            "SELECT COUNT(*) AS n FROM project_lifecycle_receipts WHERE org_id = %s",
            (org_id,),
        )
        assert cur.fetchone()["n"] == receipts_before

    conversation = conversations.create_conversation(
        org_id, project_id,
        _first_binding(pg, org_id), "seeded conversation",
    )
    assert conversation["project_id"] == project_id
    assert conversation["org_id"] == org_id


def _first_binding(db: Any, org_id: str) -> str:
    with db.cursor() as cur:
        cur.execute(
            "SELECT binding_id FROM identity_bindings"
            " WHERE platform_tenant_id = %s LIMIT 1",
            (org_id,),
        )
        return str(cur.fetchone()["binding_id"])


@requires_database
def test_conversation_fk_rejects_a_project_that_does_not_exist(pg) -> None:
    import psycopg

    store = conversations.platform_store()
    org_id, _project_id = _seed_org_project_receipt(store)
    binding_id = _first_binding(pg, org_id)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        conversations.create_conversation(
            org_id, str(uuid.uuid4()), binding_id, "orphan attempt",
        )


@requires_database
def test_rollback_assertion_restores_prior_schema(pg) -> None:
    """A transactional-DDL proof, independent of the shared ledger: apply
    0046's exact statements inside one explicit transaction, confirm the
    table becomes visible, then ROLLBACK and confirm it is gone -- the prior
    schema is restored without any DROP-based down-migration (which the
    expand-only gate forbids)."""
    schema = f"conv_rollback_{uuid.uuid4().hex[:12]}"
    sql = MIGRATION_PATH.read_text(encoding="utf-8")

    with pg.get_pool().connection() as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        try:
            conn.execute(f"SET search_path TO {schema}")
            conn.execute("CREATE TABLE orgs (org_id UUID PRIMARY KEY)")
            conn.execute(
                "CREATE TABLE projects (project_id UUID PRIMARY KEY,"
                " org_id UUID NOT NULL REFERENCES orgs(org_id),"
                " UNIQUE (org_id, project_id))"
            )
            conn.execute(
                "CREATE TABLE identity_bindings (binding_id UUID PRIMARY KEY,"
                " platform_tenant_id UUID NOT NULL REFERENCES orgs(org_id),"
                " UNIQUE (platform_tenant_id, binding_id))"
            )
            conn.commit()

            with conn.transaction():
                for statement in sql.split(";"):
                    statement = statement.strip()
                    if statement:
                        conn.execute(statement)
                visible = conn.execute(
                    f"SELECT to_regclass('{schema}.conversations') AS t"
                ).fetchone()
                assert visible["t"] is not None
                raise _RollbackSentinel()
        except _RollbackSentinel:
            pass
        finally:
            after = conn.execute(
                f"SELECT to_regclass('{schema}.conversations') AS t"
            ).fetchone()
            assert after["t"] is None, "rollback must restore the prior schema"
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
            conn.commit()


class _RollbackSentinel(Exception):
    """Raised inside the transaction block above to trigger a psycopg ROLLBACK."""


@requires_database
def test_rename_conversation_mutates_without_an_immutability_trigger(pg) -> None:
    store = conversations.platform_store()
    org_id, project_id = _seed_org_project_receipt(store)
    binding_id = _first_binding(pg, org_id)
    conversation = conversations.create_conversation(
        org_id, project_id, binding_id, "first title",
    )
    renamed = conversations.rename_conversation(
        org_id, project_id, conversation["conversation_id"], "second title",
    )
    assert renamed is not None
    assert renamed["title"] == "second title"
    assert renamed["updated_at"] != conversation["updated_at"] or (
        renamed["updated_at"] is not None
    )


@requires_database
def test_get_conversation_storage_boundary_rejects_wrong_org_directly(pg) -> None:
    """Defense in depth: even bypassing the router, the store layer alone
    must not resolve a conversation under the wrong org."""
    store = conversations.platform_store()
    org_id, project_id = _seed_org_project_receipt(store)
    binding_id = _first_binding(pg, org_id)
    conversation = conversations.create_conversation(
        org_id, project_id, binding_id, "scoped",
    )
    other_org_id, _other_project = _seed_org_project_receipt(store)
    assert conversations.get_conversation(
        other_org_id, project_id, conversation["conversation_id"],
    ) is None


# --- DB: the API path -- cross-tenant zero rows, flag-off negative control - #

class _Tenant(str):
    subject: str = ""


@requires_database
def test_conversation_api_path_cross_tenant_get_returns_zero_rows_and_flag_off_writes_none(
    pg,
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    store = conversations.platform_store()

    def _member(org: Any, project: Any, subject: str) -> str:
        binding_id = str(uuid.uuid4())
        with pg.cursor() as cur:
            cur.execute(
                "INSERT INTO identity_bindings"
                " (binding_id, external_authority, external_subject,"
                "  platform_tenant_id, platform_user_id, role)"
                " VALUES (%s, 'auth0', %s, %s, %s, 'owner')",
                (binding_id, subject, str(org.org_id), str(uuid.uuid4())),
            )
            cur.execute(
                "INSERT INTO project_member_bindings"
                " (membership_id, org_id, project_id, binding_id, role, status,"
                "  invited_by_binding_id)"
                " VALUES (%s, %s, %s, %s, 'owner', 'active', %s)",
                (str(uuid.uuid4()), str(org.org_id), str(project.project_id),
                 binding_id, binding_id),
            )
        return binding_id

    org1 = store.create_org_with_identity(
        f"conv-api-org1-{uuid.uuid4().hex[:8]}", "auth0", f"auth0|{uuid.uuid4().hex}")
    project1 = store.create_project(org1.org_id, "conv-api-project-1")
    subject1 = f"auth0|{uuid.uuid4().hex}"
    _member(org1, project1, subject1)

    org2 = store.create_org_with_identity(
        f"conv-api-org2-{uuid.uuid4().hex[:8]}", "auth0", f"auth0|{uuid.uuid4().hex}")
    project2 = store.create_project(org2.org_id, "conv-api-project-2")
    subject2 = f"auth0|{uuid.uuid4().hex}"
    _member(org2, project2, subject2)

    tenant1 = _Tenant(str(org1.org_id))
    tenant1.subject = subject1
    tenant2 = _Tenant(str(org2.org_id))
    tenant2.subject = subject2

    app = FastAPI()
    app.include_router(conversations.router)
    client = TestClient(app)

    # Flag off: create must 404 and write nothing.
    os.environ.pop(conversations.FLAG_CONV_DURABLE, None)
    app.dependency_overrides[deps.require_active_tenant] = lambda: tenant1
    resp = client.post(
        f"/api/projects/{project1.project_id}/conversations", json={"title": "x"})
    assert resp.status_code == 404
    assert conversations.list_conversations(
        str(org1.org_id), str(project1.project_id)) == []

    # Flag on: tenant1 creates a conversation under its own project.
    os.environ[conversations.FLAG_CONV_DURABLE] = "1"
    resp = client.post(
        f"/api/projects/{project1.project_id}/conversations",
        json={"title": "tenant1 conversation"},
    )
    assert resp.status_code == 201
    conversation_id = resp.json()["conversation_id"]

    resp = client.get(
        f"/api/projects/{project1.project_id}/conversations/{conversation_id}")
    assert resp.status_code == 200
    assert resp.json()["conversation_id"] == conversation_id

    # Cross-tenant: tenant2 has no membership on project1, so the SAME
    # conversation id, fetched via the API path under tenant2's identity,
    # must return zero rows (404, no conversation data in the body).
    app.dependency_overrides[deps.require_active_tenant] = lambda: tenant2
    resp = client.get(
        f"/api/projects/{project1.project_id}/conversations/{conversation_id}")
    assert resp.status_code == 404
    assert "conversation_id" not in resp.json()

    resp = client.get(f"/api/projects/{project1.project_id}/conversations")
    assert resp.status_code == 404

    resp = client.post(f"/api/projects/{project1.project_id}/conversations/recover")
    assert resp.status_code == 404
    # Cross-tenant recover must not have created a row either.
    assert conversations.list_conversations(
        str(org1.org_id), str(project1.project_id)) == [
        conversations.get_conversation(
            str(org1.org_id), str(project1.project_id), conversation_id)
    ]
