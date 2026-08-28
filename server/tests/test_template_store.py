"""Immutable versioned template store (card C2-1R, migration 0049).

Two tiers, matching test_conversation_model.py's convention:

* Static tests always run: the migration file's immutability contract
  (UNIQUE arbiter, CHECK constraints, the UPDATE/DELETE-rejecting trigger,
  expand-only shape), the module's INSERT-only write path (no UPDATE anywhere),
  the read-through wiring, publish validation, and every pin surface advanced
  to 0049 in this same change (the #710 lesson: a migration that advances the
  inventory without every pin is a red main).

* PostgreSQL tests require DATABASE_URL and skip cleanly otherwise: publish,
  duplicate-publish refusal, database-enforced immutability (UPDATE and DELETE
  both rejected by the 0049 trigger), read-through authority over the
  registry, and digest fail-closed on a corrupted row.
"""
from __future__ import annotations

import inspect
import json
import os
import re
import sys
import uuid
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SERVER_DIR.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import templates  # noqa: E402

requires_database = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="PostgreSQL template store test requires explicit DATABASE_URL",
)

MIGRATION = PROJECT_ROOT / "platform" / "migrations" / "0049_solar_template.sql"


# --- static: the migration's immutability contract -------------------------- #

def test_migration_0049_exists_and_is_expand_only() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS template_versions" in sql
    for forbidden in ("DROP TABLE", "DROP COLUMN", "ALTER COLUMN", "TRUNCATE"):
        assert forbidden not in sql.upper(), forbidden


def test_migration_0049_unique_arbiter_matches_the_module() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "UNIQUE (template_id, version)" in sql
    src = inspect.getsource(templates.publish_template_version)
    assert "ON CONFLICT" not in src.upper(), (
        "publish must be INSERT-only: a duplicate surfaces the UNIQUE "
        "violation as TemplateVersionConflictError, never a conflict arm")


def test_migration_0049_rejects_update_and_delete_by_trigger() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "BEFORE UPDATE OR DELETE ON template_versions" in sql
    assert "RAISE EXCEPTION" in sql


def test_migration_0049_provenance_is_mandatory() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "published_by      TEXT NOT NULL" in sql.replace("  ", "  ")
    assert "provenance_source" in sql
    assert "'seed', 'operator_publish'" in sql


# --- static: the module write path ------------------------------------------ #

def test_module_has_no_update_or_delete_statement_on_the_store() -> None:
    src = inspect.getsource(templates)
    for match in re.finditer(r'"(UPDATE|DELETE)[^"]*"', src, re.IGNORECASE):
        assert templates.STORE_TABLE not in match.group(0), match.group(0)
    for function in (
        templates.publish_template_version,
        templates._read_through_stored_version,
    ):
        src = inspect.getsource(function)
        assert "SELECT set_config('statement_timeout', %s, true)" in src
        assert "SET LOCAL statement_timeout = %s" not in src


def test_resolve_version_reads_through_the_store_only_behind_the_flag() -> None:
    src = inspect.getsource(templates.resolve_version)
    assert "solar_template_beta_enabled()" in src
    assert "_database_configured()" in src
    assert "_read_through_stored_version" in src


def test_publish_validation_fails_closed() -> None:
    good = {"array_type": "rooftop"}
    with pytest.raises(ValueError):
        templates._validate_publish_inputs("", "1.0.0", good, "op", "seed")
    with pytest.raises(ValueError):
        templates._validate_publish_inputs("t", "", good, "op", "seed")
    with pytest.raises(ValueError):
        templates._validate_publish_inputs("t", "1.0.0", good, "", "seed")
    with pytest.raises(ValueError):
        templates._validate_publish_inputs("t", "1.0.0", good, "op", "invented")
    with pytest.raises(ValueError):
        templates._validate_publish_inputs("t", "1.0.0", "not-a-dict", "op", "seed")
    big = {"k": "x" * (templates.MAX_TEMPLATE_CONTENT_BYTES + 1)}
    with pytest.raises(ValueError):
        templates._validate_publish_inputs("t", "1.0.0", big, "op", "seed")
    digest = templates._validate_publish_inputs("t", "1.0.0", good, "op", "seed")
    assert digest == templates.content_digest(good)


def test_registry_fallback_untouched_when_flag_off(monkeypatch) -> None:
    monkeypatch.delenv(templates.FLAG_SOLAR_TEMPLATE_BETA, raising=False)
    calls = {"count": 0}

    def _spy(*_a, **_k):
        calls["count"] += 1
        raise AssertionError("store must not be consulted with the flag off")

    monkeypatch.setattr(templates, "_read_through_stored_version", _spy)
    resolved = templates.resolve_version("rooftop-standard-string", None)
    assert resolved.version == "1.1.0"
    assert calls["count"] == 0


# --- static: every pin surface advanced to the head in this same change ----- #
# The 0049 card wrote these four mirrors so a migration could never land with
# half its pins moved. They did their job: the 0050 live-project-guard card
# tripped all four. Renamed off the number so the NEXT card edits values, not
# identifiers.

def test_authority_inventory_advanced_to_the_head() -> None:
    inventory = json.loads(
        (PROJECT_ROOT / "platform" / "authority-inventory.json")
        .read_text(encoding="utf-8"))
    ids = inventory["scope"]["migration_ids"]
    assert ids[-1] == "0050"
    assert ids == [f"{n:04d}" for n in range(1, 51)]
    files = sorted(
        p.name.split("_", 1)[0]
        for p in (PROJECT_ROOT / "platform" / "migrations").glob("*.sql"))
    assert files == ids


def test_conversation_model_pin_advanced() -> None:
    src = (SERVER_DIR / "tests" / "test_conversation_model.py").read_text(
        encoding="utf-8")
    assert 'migration_ids[-1] == "0050"' in src
    assert "range(1, 51)" in src


def test_readiness_pins_advanced() -> None:
    src = (PROJECT_ROOT / "platform" / "tests" / "test_db_readiness_static.py"
           ).read_text(encoding="utf-8")
    # The FILENAME, not the bare number. That file also holds synthetic "0049"
    # and "0050" literals inside two negative pin-drift tests, so a bare-number
    # assertion passed whether or not the real pin had moved -- vacuous.
    assert "0050_live_project_guard.sql" in src


def test_postgres_contract_expected_migrations_advanced() -> None:
    src = (SERVER_DIR / "tests" /
           "test_postgres_authority_inventory_contract.py").read_text(
        encoding="utf-8")
    assert "range(1, 51)" in src


def test_db_registers_template_versions_columns() -> None:
    src = (PROJECT_ROOT / "platform" / "db.py").read_text(encoding="utf-8")
    assert '"template_versions"' in src
    for col in ("version_id", "template_id", "version", "content",
                "content_sha256", "published_by", "provenance_source",
                "created_at"):
        assert col in src


# --- PostgreSQL: the store's live behavior ---------------------------------- #

def _unique_template_id() -> str:
    return f"c21r-test-{uuid.uuid4().hex[:12]}"


@requires_database
def test_publish_then_resolve_reads_the_stored_row(monkeypatch) -> None:
    monkeypatch.setenv(templates.FLAG_SOLAR_TEMPLATE_BETA, "1")
    template_id = _unique_template_id()
    content = {"array_type": "rooftop", "default_tilt_deg": 22}
    receipt = templates.publish_template_version(
        template_id, "1.0.0", content,
        published_by="c21r-test", provenance_source="operator_publish")
    assert receipt["content_digest"] == templates.content_digest(content)
    resolved = templates.resolve_version(template_id, "1.0.0")
    assert resolved.content == content
    assert resolved.digest == receipt["content_digest"]
    latest = templates.resolve_version(template_id, None)
    assert latest.version == "1.0.0"


@requires_database
def test_duplicate_publish_is_refused_never_overwritten(monkeypatch) -> None:
    monkeypatch.setenv(templates.FLAG_SOLAR_TEMPLATE_BETA, "1")
    template_id = _unique_template_id()
    first = {"array_type": "rooftop", "n": 1}
    templates.publish_template_version(
        template_id, "1.0.0", first,
        published_by="c21r-test", provenance_source="operator_publish")
    with pytest.raises(templates.TemplateVersionConflictError):
        templates.publish_template_version(
            template_id, "1.0.0", {"array_type": "rooftop", "n": 2},
            published_by="c21r-test", provenance_source="operator_publish")
    resolved = templates.resolve_version(template_id, "1.0.0")
    assert resolved.content == first


@requires_database
def test_database_rejects_update_and_delete(monkeypatch) -> None:
    monkeypatch.setenv(templates.FLAG_SOLAR_TEMPLATE_BETA, "1")
    template_id = _unique_template_id()
    templates.publish_template_version(
        template_id, "1.0.0", {"array_type": "rooftop"},
        published_by="c21r-test", provenance_source="operator_publish")
    db = templates.platform_db()
    import psycopg
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        with db.transaction() as conn:
            conn.execute(
                f"UPDATE {templates.STORE_TABLE} SET content = %s"
                " WHERE template_id = %s",
                (templates.Jsonb({"tampered": True})
                 if hasattr(templates, "Jsonb") else '{"tampered": true}',
                 template_id))
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        with db.transaction() as conn:
            conn.execute(
                f"DELETE FROM {templates.STORE_TABLE} WHERE template_id = %s",
                (template_id,))


@requires_database
def test_store_wins_over_registry_for_published_versions(monkeypatch) -> None:
    monkeypatch.setenv(templates.FLAG_SOLAR_TEMPLATE_BETA, "1")
    content = {"array_type": "rooftop", "authoritative": True}
    templates.publish_template_version(
        "rooftop-standard-string", f"9.{uuid.uuid4().hex[:6]}.0", content,
        published_by="c21r-test", provenance_source="operator_publish")
    latest = templates.resolve_version("rooftop-standard-string", None)
    assert latest.content.get("authoritative") is True, (
        "the store's newest published version must win over the registry's "
        "latest when the flag is on")
