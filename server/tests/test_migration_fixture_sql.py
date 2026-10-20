"""Pure checks for the bounded PostgreSQL fixture migration translation."""
from pathlib import Path

import pytest

from migration_fixture_sql import fixture_grant_role, translate_migration_sql


MIGRATIONS = Path(__file__).resolve().parents[2] / "platform" / "migrations"
NAME = "0079_integration_turn_grants.sql"
SCHEMA = "demo_test_" + "a" * 32


def source():
    return (MIGRATIONS / NAME).read_text(encoding="utf-8")


def test_exact_mapping_preserves_every_other_byte():
    original = source()
    role = fixture_grant_role(SCHEMA)
    mapped = translate_migration_sql(NAME, original, SCHEMA)
    assert role == "demo_grant_" + "a" * 32
    assert len(role.encode("ascii")) < 63
    expected = original.replace("public.integration_turn_grants", SCHEMA + ".integration_turn_grants")
    expected = expected.replace("public.guard_integration_turn_grant", SCHEMA + ".guard_integration_turn_grant")
    expected = expected.replace("GRANT USAGE ON SCHEMA public", "GRANT USAGE ON SCHEMA " + SCHEMA)
    expected = expected.replace("leaf_integration_grant_app", role)
    assert mapped == expected
    assert mapped.count(SCHEMA + ".integration_turn_grants") == 8
    assert mapped.count(SCHEMA + ".guard_integration_turn_grant") == 2
    assert f"GRANT USAGE ON SCHEMA {SCHEMA} TO {role};" in mapped
    assert f"rolname='{role}'" in mapped
    assert f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS;" in mapped
    assert f"ALTER ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS;" in mapped
    assert f"FOR ALL TO {role}" in mapped
    assert f"GRANT SELECT, INSERT, UPDATE ON {SCHEMA}.integration_turn_grants TO {role};" in mapped
    assert "public." not in mapped
    assert "leaf_integration_grant_app" not in mapped


def test_two_fixtures_are_separate_and_deterministic():
    second_schema = "demo_test_" + "b" * 32
    first = translate_migration_sql(NAME, source(), SCHEMA)
    second = translate_migration_sql(NAME, source(), second_schema)
    assert first == translate_migration_sql(NAME, source(), SCHEMA)
    assert first != second
    assert SCHEMA not in second and fixture_grant_role(SCHEMA) not in second
    assert second_schema not in first and fixture_grant_role(second_schema) not in first
    assert first.replace(SCHEMA, second_schema).replace(
        fixture_grant_role(SCHEMA), fixture_grant_role(second_schema)) == second


@pytest.mark.parametrize("schema", [
    "public", "demo_test_", "demo_test_" + "a" * 31,
    "demo_test_" + "a" * 33, "demo_test_" + "A" * 32,
    "demo_test_" + "g" * 32, SCHEMA + "; DROP SCHEMA public CASCADE;",
    '"' + SCHEMA + '"', SCHEMA + "\n",
])
def test_unsafe_schema_is_refused(schema):
    with pytest.raises(ValueError, match="demo_test_"):
        translate_migration_sql(NAME, source(), schema)
    with pytest.raises(ValueError, match="demo_test_"):
        fixture_grant_role(schema)


def test_other_real_migrations_pass_unchanged():
    for migration in MIGRATIONS.glob("*.sql"):
        if migration.name != NAME:
            original = migration.read_text(encoding="utf-8")
            assert translate_migration_sql(migration.name, original, SCHEMA) == original
    assert translate_migration_sql("0080_other.sql", source(), SCHEMA) == source()


@pytest.mark.parametrize("token", [
    "public.integration_turn_grants", "public.guard_integration_turn_grant",
    "GRANT USAGE ON SCHEMA public", "leaf_integration_grant_app",
])
def test_missing_expected_token_is_refused(token):
    with pytest.raises(ValueError, match="expected .* occurrences"):
        translate_migration_sql(NAME, source().replace(token, "missing", 1), SCHEMA)


@pytest.mark.parametrize("extra", [
    "SELECT * FROM public.other;", 'SELECT * FROM "public".other;',
    "SELECT * FROM PUBLIC . other;", "-- leaf_integration_grant_app_extra",
])
def test_unmapped_production_reference_is_refused(extra):
    with pytest.raises(ValueError, match="unmapped production"):
        translate_migration_sql(NAME, source() + "\n" + extra, SCHEMA)


def test_security_clauses_survive_translation():
    mapped = translate_migration_sql(NAME, source(), SCHEMA)
    for clause in (
        f"ALTER TABLE {SCHEMA}.integration_turn_grants ENABLE ROW LEVEL SECURITY;",
        f"ALTER TABLE {SCHEMA}.integration_turn_grants FORCE ROW LEVEL SECURITY;",
        f"CREATE TRIGGER integration_turn_grant_immutable BEFORE UPDATE ON {SCHEMA}.integration_turn_grants",
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_integration_turn_grant();",
        "LANGUAGE plpgsql SET search_path = pg_catalog AS $$",
        "RAISE EXCEPTION 'GRANT_IMMUTABLE';",
        "IS DISTINCT FROM ROW(OLD.tenant_id, OLD.project_id, OLD.session_id, OLD.turn_id,",
        "OR (NEW.envelope IS NOT NULL AND NEW.envelope IS DISTINCT FROM OLD.envelope)",
        "OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at)",
        "CHECK (envelope IS NULL OR octet_length(envelope) BETWEEN 1 AND 32768)",
        "CHECK (revoked_at IS NULL OR envelope IS NULL)",
        "AND binding->>'purpose' = 'studio-integration-turn-grant'",
        "USING (tenant_id::text = current_setting('leaf.integration_tenant_id', true)",
        "WITH CHECK (tenant_id::text = current_setting('leaf.integration_tenant_id', true)",
        "AND project_id::text = current_setting('leaf.integration_project_id', true))",
        f"REVOKE ALL ON {SCHEMA}.integration_turn_grants FROM PUBLIC;",
    ):
        assert clause in mapped
