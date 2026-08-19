"""Tests for the immutable versioned Solar CAD template store (0047).

Structural checks (migration text, expand-only gate, flag gating, module
shape) always run. Everything that touches PostgreSQL requires an explicit
DATABASE_URL and skips cleanly otherwise, matching the convention in
test_drawing_upload_authority_postgres.py and test_conversation_model.py.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SERVER_DIR.parent
MIGRATION_PATH = (
    PROJECT_ROOT / "platform" / "migrations" / "0047_solar_template.sql"
)
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import templates  # noqa: E402
import deps  # noqa: E402

requires_database = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="PostgreSQL template store test requires explicit DATABASE_URL",
)


# --- structural: migration text, expand-only, immutability, flag ----------- #

def test_migration_file_is_a_fresh_immutable_versioned_table() -> None:
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS solar_template_versions" in sql
    assert "REFERENCES orgs(org_id) ON DELETE CASCADE" in sql
    assert "REFERENCES identity_bindings(platform_tenant_id, binding_id)" in sql
    # Mandatory provenance columns: not null by construction (no DEFAULT NULL
    # / no "DEFAULT" escape hatch), so a row cannot exist without them.
    for column in ("content_sha256", "source", "published_by_binding_id"):
        assert f"{column}" in sql
    assert "content_sha256           TEXT NOT NULL" in sql
    assert "source                   TEXT NOT NULL" in sql
    assert "published_by_binding_id  UUID NOT NULL" in sql
    # No backfill: this is a wholly new table, so a populated snapshot must
    # gain zero template rows and orphan nothing.
    assert "INSERT INTO solar_template_versions" not in sql
    # The immutability trigger: no UPDATE or DELETE path exists in the
    # database layer either.
    assert "BEFORE UPDATE OR DELETE ON solar_template_versions" in sql
    assert "leaf_reject_ledger_mutation" in sql
    # Unique (org, template_key, version): two publishers cannot land the
    # same version number for the same template.
    assert "solar_template_versions_scope_version_unique" in sql
    assert "UNIQUE (org_id, template_key, version)" in sql


def test_migration_is_expand_only() -> None:
    """Runs the real expand-contract gate's own detectors (comment/string
    stripped, exactly as scripts/migration_expand_contract_gate.py matches)
    against this one file, so prose in a comment can never produce a false
    positive the way a bare substring search would."""
    scripts_dir = str(PROJECT_ROOT / "scripts")
    added = scripts_dir not in sys.path
    if added:
        sys.path.insert(0, scripts_dir)
    try:
        import migration_expand_contract_gate as gate
    finally:
        if added:
            sys.path.remove(scripts_dir)

    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    stripped = gate.strip_sql_noise(sql)
    hits = [label for label, pattern in gate.CONTRACT_PATTERNS
            if pattern.search(stripped)]
    assert hits == []

    violations = gate.check_migrations(PROJECT_ROOT / "platform" / "migrations")
    assert violations == []


def test_migration_advances_authority_inventory_by_exactly_one() -> None:
    inventory = json.loads(
        (PROJECT_ROOT / "platform" / "authority-inventory.json")
        .read_text(encoding="utf-8")
    )
    migration_ids = inventory["scope"]["migration_ids"]
    assert migration_ids[-1] == "0047"
    assert migration_ids == [f"{n:04d}" for n in range(1, 48)]

    migration_files = sorted(
        p.name.split("_", 1)[0]
        for p in (PROJECT_ROOT / "platform" / "migrations").glob("*.sql")
    )
    assert migration_files == migration_ids


def test_no_update_path_exists_anywhere_in_the_store_module() -> None:
    """Structural proof that publishing a version can never mutate a prior
    row: the module's own source contains no UPDATE statement against
    solar_template_versions, and its public API exposes no update/delete
    function at all -- only publish (INSERT) and read functions."""
    source = (SERVER_DIR / "templates.py").read_text(encoding="utf-8")
    assert "UPDATE solar_template_versions" not in source
    assert "cur.execute(\n            \"UPDATE" not in source
    assert not any(
        name.startswith(("update_", "edit_", "mutate_", "delete_"))
        for name in dir(templates)
    )
    public_functions = {
        name for name in dir(templates)
        if not name.startswith("_") and callable(getattr(templates, name))
    }
    assert {"publish_version", "list_versions", "get_version",
            "get_latest_version"} <= public_functions


def test_solar_template_beta_flag_is_off_by_default_and_reads_case_insensitively(
    monkeypatch,
) -> None:
    monkeypatch.delenv(templates.FLAG_SOLAR_TEMPLATE_BETA, raising=False)
    assert templates.solar_template_beta_enabled() is False
    monkeypatch.setenv(templates.FLAG_SOLAR_TEMPLATE_BETA, "TRUE")
    assert templates.solar_template_beta_enabled() is True
    monkeypatch.setenv(templates.FLAG_SOLAR_TEMPLATE_BETA, "0")
    assert templates.solar_template_beta_enabled() is False


def test_publish_version_refuses_to_write_when_flag_is_off(monkeypatch) -> None:
    monkeypatch.delenv(templates.FLAG_SOLAR_TEMPLATE_BETA, raising=False)
    with pytest.raises(templates.TemplateBetaDisabled):
        templates.publish_version(
            str(uuid.uuid4()), "roof-array", {"panels": 12}, str(uuid.uuid4()),
        )


def test_content_digest_is_deterministic_regardless_of_key_order() -> None:
    a = templates.content_digest({"panels": 12, "tilt": 20})
    b = templates.content_digest({"tilt": 20, "panels": 12})
    assert a == b
    assert len(a) == 64
    assert all(c in "0123456789abcdef" for c in a)


# --- DB: populated-snapshot apply, immutability, provenance, rollback ------ #

@pytest.fixture
def pg(monkeypatch):
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("PostgreSQL template store test requires DATABASE_URL")
    db = templates.platform_db()
    db.apply_migration()
    monkeypatch.setenv(templates.FLAG_SOLAR_TEMPLATE_BETA, "1")
    yield db
    db.reset_pool()


def _seed_org(store: Any) -> tuple[str, str]:
    org = store.create_org_with_identity(
        f"tmpl-test-org-{uuid.uuid4().hex[:8]}", "auth0", f"auth0|{uuid.uuid4().hex}",
    )
    with templates.platform_db().cursor() as cur:
        cur.execute(
            "SELECT binding_id FROM identity_bindings"
            " WHERE platform_tenant_id = %s LIMIT 1",
            (str(org.org_id),),
        )
        binding_id = str(cur.fetchone()["binding_id"])
    return str(org.org_id), binding_id


@requires_database
def test_migration_applies_cleanly_on_populated_snapshot_with_zero_orphans(pg) -> None:
    store = templates.platform_store()
    org_id, binding_id = _seed_org(store)

    version = templates.publish_version(
        org_id, "roof-array", {"panels": 12}, binding_id,
    )

    with pg.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM orgs WHERE org_id = %s", (org_id,))
        orgs_before = cur.fetchone()["n"]
        cur.execute(
            "SELECT COUNT(*) AS n FROM solar_template_versions WHERE org_id = %s",
            (org_id,),
        )
        versions_before = cur.fetchone()["n"]

    # Re-apply every migration, including 0047, over this now-populated org.
    # Idempotent (CREATE TABLE IF NOT EXISTS, ledger-tracked skip), so this is
    # exactly the "applies cleanly on a populated snapshot" proof.
    pg.apply_migration()

    with pg.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM orgs WHERE org_id = %s", (org_id,))
        assert cur.fetchone()["n"] == orgs_before
        cur.execute(
            "SELECT COUNT(*) AS n FROM solar_template_versions WHERE org_id = %s",
            (org_id,),
        )
        assert cur.fetchone()["n"] == versions_before

    assert version["version"] == 1
    assert version["org_id"] == org_id


@requires_database
def test_publishing_a_new_version_never_mutates_a_prior_one(pg) -> None:
    store = templates.platform_store()
    org_id, binding_id = _seed_org(store)

    v1 = templates.publish_version(
        org_id, "roof-array", {"panels": 12}, binding_id,
        source="author", provenance_note="first cut",
    )
    v2 = templates.publish_version(
        org_id, "roof-array", {"panels": 20}, binding_id,
        source="author", provenance_note="revised layout",
    )

    assert v1["version"] == 1
    assert v2["version"] == 2
    assert v1["template_version_id"] != v2["template_version_id"]

    # The row read back for v1 is byte-identical to what was returned at
    # publish time: publishing v2 touched nothing about v1's row.
    reread_v1 = templates.get_version(org_id, "roof-array", 1)
    assert reread_v1 == v1
    assert reread_v1["content"] == {"panels": 12}
    assert reread_v1["content_sha256"] == templates.content_digest({"panels": 12})

    latest = templates.get_latest_version(org_id, "roof-array")
    assert latest == v2

    all_versions = templates.list_versions(org_id, "roof-array")
    assert [row["version"] for row in all_versions] == [1, 2]


@requires_database
def test_immutable_trigger_rejects_update_and_delete(pg) -> None:
    import psycopg

    store = templates.platform_store()
    org_id, binding_id = _seed_org(store)
    version = templates.publish_version(
        org_id, "inverter-string", {"strings": 4}, binding_id,
    )

    with pytest.raises(psycopg.Error) as update_exc:
        with pg.cursor() as cur:
            cur.execute(
                "UPDATE solar_template_versions SET content = %s"
                " WHERE template_version_id = %s",
                (json.dumps({"strings": 999}), version["template_version_id"]),
            )
    assert "immutable" in str(update_exc.value).lower()

    with pytest.raises(psycopg.Error) as delete_exc:
        with pg.cursor() as cur:
            cur.execute(
                "DELETE FROM solar_template_versions WHERE template_version_id = %s",
                (version["template_version_id"],),
            )
    assert "immutable" in str(delete_exc.value).lower()

    # Neither attempt changed the row: it reads back exactly as published.
    unchanged = templates.get_version(org_id, "inverter-string", 1)
    assert unchanged == version


@requires_database
def test_provenance_fields_are_mandatory_at_the_database_boundary(pg) -> None:
    import psycopg

    store = templates.platform_store()
    org_id, binding_id = _seed_org(store)

    with pytest.raises(psycopg.Error):
        with pg.cursor() as cur:
            cur.execute(
                "INSERT INTO solar_template_versions"
                " (template_version_id, org_id, template_key, version, content,"
                "  content_sha256, source, published_by_binding_id)"
                " VALUES (%s, %s, 'no-provenance', 1, '{}'::jsonb, %s, NULL, %s)",
                (str(uuid.uuid4()), org_id,
                 templates.content_digest({}), binding_id),
            )
    with pytest.raises(psycopg.Error):
        with pg.cursor() as cur:
            cur.execute(
                "INSERT INTO solar_template_versions"
                " (template_version_id, org_id, template_key, version, content,"
                "  content_sha256, source, published_by_binding_id)"
                " VALUES (%s, %s, 'no-publisher', 1, '{}'::jsonb, %s, 'author', NULL)",
                (str(uuid.uuid4()), org_id, templates.content_digest({})),
            )

    assert templates.list_versions(org_id, "no-provenance") == []
    assert templates.list_versions(org_id, "no-publisher") == []


@requires_database
def test_concurrent_publish_of_the_same_next_version_collides_not_overwrites(pg) -> None:
    """Defense in depth: even a caller that bypasses `publish_version`'s own
    MAX(version)+1 subquery and races to insert version 1 twice for the same
    template must collide on the unique constraint, never silently overwrite
    the winner's row."""
    import psycopg

    store = templates.platform_store()
    org_id, binding_id = _seed_org(store)
    templates.publish_version(org_id, "racer", {"n": 1}, binding_id)

    with pytest.raises(psycopg.Error):
        with pg.cursor() as cur:
            cur.execute(
                "INSERT INTO solar_template_versions"
                " (template_version_id, org_id, template_key, version, content,"
                "  content_sha256, source, published_by_binding_id)"
                " VALUES (%s, %s, 'racer', 1, '{}'::jsonb, %s, 'author', %s)",
                (str(uuid.uuid4()), org_id, templates.content_digest({}), binding_id),
            )

    versions = templates.list_versions(org_id, "racer")
    assert len(versions) == 1
    assert versions[0]["content"] == {"n": 1}


@requires_database
def test_rollback_assertion_restores_prior_schema(pg) -> None:
    """A transactional-DDL proof, independent of the shared ledger: apply
    0047's exact statements inside one explicit transaction, confirm the
    table becomes visible, then ROLLBACK and confirm it is gone -- the prior
    schema is restored without any DROP-based down-migration (which the
    expand-only gate forbids)."""
    schema = f"tmpl_rollback_{uuid.uuid4().hex[:12]}"
    sql = MIGRATION_PATH.read_text(encoding="utf-8")

    with pg.get_pool().connection() as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        try:
            conn.execute(f"SET search_path TO {schema}")
            conn.execute("CREATE TABLE orgs (org_id UUID PRIMARY KEY)")
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
                    f"SELECT to_regclass('{schema}.solar_template_versions') AS t"
                ).fetchone()
                assert visible["t"] is not None
                raise _RollbackSentinel()
        except _RollbackSentinel:
            pass
        finally:
            after = conn.execute(
                f"SELECT to_regclass('{schema}.solar_template_versions') AS t"
            ).fetchone()
            assert after["t"] is None, "rollback must restore the prior schema"
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
            conn.commit()


class _RollbackSentinel(Exception):
    """Raised inside the transaction block above to trigger a psycopg ROLLBACK."""


# --- DB: the API path -- flag-off negative control, own-org publish ------- #

class _Tenant(str):
    subject: str = ""
    org_id: str = ""


@requires_database
def test_template_api_path_flag_off_writes_none_and_flag_on_publishes_to_own_org(
    pg,
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    store = templates.platform_store()
    subject = f"auth0|{uuid.uuid4().hex}"
    org2 = store.create_org_with_identity(
        f"tmpl-api-org-{uuid.uuid4().hex[:8]}", "auth0", subject,
    )
    with pg.cursor() as cur:
        cur.execute(
            "SELECT binding_id FROM identity_bindings WHERE platform_tenant_id = %s",
            (str(org2.org_id),),
        )
        api_binding_id = str(cur.fetchone()["binding_id"])

    tenant = _Tenant(str(org2.org_id))
    tenant.subject = subject
    tenant.org_id = str(org2.org_id)

    app = FastAPI()
    app.include_router(templates.router)
    app.dependency_overrides[deps.require_active_tenant] = lambda: tenant
    client = TestClient(app)

    # Flag off: publish must 404 and write nothing.
    os.environ.pop(templates.FLAG_SOLAR_TEMPLATE_BETA, None)
    resp = client.post(
        "/api/templates",
        json={"template_key": "roof-array", "content": {"panels": 8}},
    )
    assert resp.status_code == 404
    assert templates.list_versions(str(org2.org_id), "roof-array") == []

    # Flag on: publishes into the tenant's own resolved org, never a
    # client-supplied one (the request body carries no org_id field at all).
    os.environ[templates.FLAG_SOLAR_TEMPLATE_BETA] = "1"
    resp = client.post(
        "/api/templates",
        json={"template_key": "roof-array", "content": {"panels": 8}},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["org_id"] == str(org2.org_id)
    assert body["version"] == 1
    assert body["published_by_binding_id"] == api_binding_id

    resp = client.get("/api/templates/roof-array/versions")
    assert resp.status_code == 200
    assert len(resp.json()["versions"]) == 1

    resp = client.get("/api/templates/roof-array/versions/1")
    assert resp.status_code == 200
    assert resp.json()["content"] == {"panels": 8}

    resp = client.get("/api/templates/roof-array/versions/99")
    assert resp.status_code == 404
