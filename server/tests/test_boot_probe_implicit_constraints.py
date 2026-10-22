"""Offline coverage for catalog defects explained by pending table creation."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "platform" / "migrations"
PENDING = sorted(path.name for path in MIGRATIONS.glob("007[0-8]_*.sql"))
RELATIONS = {
    "demo_mint_counters_pkey": "demo_mint_counters",
    "demo_sessions_pkey": "demo_sessions",
    "demo_sessions_subject_key": "demo_sessions",
    "demo_sessions_tenant_id_key": "demo_sessions",
    "engine_change_card_reads_card_id_fkey": "engine_change_card_reads",
    "engine_change_card_reads_pkey": "engine_change_card_reads",
    "engine_change_cards_operation_id_key": "engine_change_cards",
    "engine_change_cards_pkey": "engine_change_cards",
    "engine_change_cards_state_check": "engine_change_cards",
}
DEFECTS = sorted(f"{name}:missing-or-wrong-relation" for name in RELATIONS)
CATALOG = {"constraints": {
    name: {"relation": relation} for name, relation in RELATIONS.items()
}}
ABSENT = set(RELATIONS.values())


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location(
        "implicit_constraint_boot_probe", ROOT / "server" / "boot_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_migrations_dir", lambda: MIGRATIONS)
    return module


def production_status():
    return {"ok": False, "missing_migrations": list(PENDING),
            "migration_hash_drift": [], "invalid_constraints": list(DEFECTS)}


def fake_database(monkeypatch, catalog=CATALOG, absent=ABSENT):
    db = SimpleNamespace(
        required_catalog_for_selected_authorities=MagicMock(return_value=catalog),
        get_pool=MagicMock(),
    )
    cursor = db.get_pool.return_value.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [{"name": name} for name in sorted(absent)]
    monkeypatch.setitem(sys.modules, "platform_link", SimpleNamespace(platform_db=lambda: db))
    return db, cursor


@pytest.mark.parametrize("absent,expected", [
    (ABSENT, []), (set(), DEFECTS),
], ids=["production-relations-absent", "production-relations-present"])
def test_exact_production_case(probe, absent, expected):
    assert len(PENDING) == 9
    assert probe._unexplained_defects(
        production_status(), PENDING, catalog=CATALOG, absent_relations=absent) == expected


def test_absent_relation_without_pending_creation(probe):
    pending = ["0070_aps_completion_receipts.sql"]
    assert probe._unexplained_defects(
        production_status(), pending, catalog=CATALOG, absent_relations=ABSENT) == DEFECTS


@pytest.mark.parametrize("kind,reason", [
    ("constraints", "not-validated"), ("indexes", "not-valid-ready"),
    ("triggers", "disabled"), ("constraints", "definition-mismatch"),
    ("constraints", "unknown-reason"),
])
def test_other_reasons_stay_unexplained(probe, kind, reason):
    entry = f"implicit_object:{reason}"
    assert probe._unexplained_defects(
        {f"invalid_{kind}": [entry]}, PENDING,
        catalog={kind: {"implicit_object": {"relation": "demo_sessions"}}},
        absent_relations=ABSENT) == [entry]


@pytest.mark.parametrize("kind", ["constraints", "indexes", "triggers"])
def test_all_supported_catalog_kinds(probe, kind):
    assert probe._unexplained_defects(
        {f"invalid_{kind}": ["implicit_object:missing-or-wrong-relation"]}, PENDING,
        catalog={kind: {"implicit_object": {"relation": "demo_sessions"}}},
        absent_relations=ABSENT) == []


def test_missing_catalog_entry_and_other_lists_stay_unexplained(probe):
    assert probe._unexplained_defects(
        production_status(), PENDING, catalog={}, absent_relations=ABSENT) == DEFECTS
    entry = "implicit_object:missing-or-wrong-relation"
    assert probe._unexplained_defects(
        {"missing_indexes": [entry]}, PENDING,
        catalog={"indexes": {"implicit_object": {"relation": "demo_sessions"}}},
        absent_relations=ABSENT) == [entry]


@pytest.mark.parametrize("lookup", ["catalog", "absent"])
def test_injected_lookup_errors_fail_closed(probe, lookup):
    catalog, absent = CATALOG, ABSENT
    broken = MagicMock()
    if lookup == "catalog":
        broken.get.side_effect = RuntimeError("private-lookup-details")
        catalog = broken
    else:
        broken.__contains__.side_effect = RuntimeError("private-lookup-details")
        absent = broken
    assert probe._unexplained_defects(
        production_status(), PENDING, catalog=catalog, absent_relations=absent) == DEFECTS


def test_name_in_pending_text_still_explains_defect(probe):
    entry = "demo_sessions_subject_check:not-validated"
    assert probe._unexplained_defects(
        {"invalid_constraints": [entry]}, PENDING) == []


@pytest.mark.parametrize("sql,explained", [
    ("CREATE TABLE demo_sessions (id uuid);", True),
    ('create table if not exists public."DEMO_SESSIONS" (id uuid);', True),
    ('CREATE TABLE "public" . "demo_sessions" (id uuid);', True),
    ("CREATE TABLE demo_sessions_archive (id uuid);", False),
    ('CREATE TABLE "demo_sessions_archive" (id uuid);', False),
    ("CREATE TABLE other.demo_sessions (id uuid);", False),
    ("CREATE TABLE demo_sessions.other (id uuid);", False),
    ("CREATE TABLE demo_sessions$archive (id uuid);", False),
    ("ALTER TABLE demo_sessions ADD COLUMN id uuid;", False),
])
def test_create_table_matching(probe, monkeypatch, tmp_path, sql, explained):
    (tmp_path / "pending.sql").write_text(sql, encoding="utf-8")
    monkeypatch.setattr(probe, "_migrations_dir", lambda: tmp_path)
    entry = "implicit_object:missing-or-wrong-relation"
    assert probe._unexplained_defects(
        {"invalid_constraints": [entry]}, ["pending.sql"],
        catalog={"constraints": {"implicit_object": {"relation": "demo_sessions"}}},
        absent_relations={"demo_sessions"}) == ([] if explained else [entry])


def test_columns_keep_existing_rule(probe):
    assert probe._unexplained_defects(
        {"missing": {"demo_sessions": ["absent_column"]}}, PENDING,
        catalog=CATALOG, absent_relations=ABSENT) == ["demo_sessions.absent_column"]


def test_absence_query_is_one_batch_for_distinct_candidates(probe, monkeypatch):
    db, cursor = fake_database(monkeypatch)
    catalog, absent = probe._catalog_relation_absence(production_status())
    assert catalog == CATALOG
    assert absent == ABSENT
    db.required_catalog_for_selected_authorities.assert_called_once()
    cursor.execute.assert_called_once_with(
        "SELECT name FROM unnest(%(names)s::text[]) AS name "
        "WHERE to_regclass(quote_ident(current_schema()) || '.' || "
        "quote_ident(name)) IS NULL", {"names": sorted(ABSENT)})


@pytest.mark.parametrize("status", [
    {}, {"invalid_constraints": ["demo_sessions_pkey:not-validated"]},
    {"missing_indexes": ["some_index:missing-or-wrong-relation"]},
])
def test_no_lookups_without_candidates(probe, monkeypatch, status):
    db, _ = fake_database(monkeypatch)
    assert probe._catalog_relation_absence(status) == ({}, set())
    db.required_catalog_for_selected_authorities.assert_not_called()
    db.get_pool.assert_not_called()


def test_no_query_for_unknown_catalog_names(probe, monkeypatch):
    db, _ = fake_database(monkeypatch, catalog={})
    assert probe._catalog_relation_absence(production_status()) == ({}, set())
    db.get_pool.assert_not_called()


@pytest.mark.parametrize("failure", ["none", "catalog", "absent", "fetch"])
def test_run_production_case_and_lookup_failures(probe, monkeypatch, capsys, failure):
    monkeypatch.setattr(probe, "_CHECKS", ())
    db, cursor = fake_database(monkeypatch)

    def unavailable(*args, **kwargs):
        print("private-lookup-details")
        raise RuntimeError("private-lookup-details")

    if failure == "catalog":
        db.required_catalog_for_selected_authorities.side_effect = unavailable
    elif failure == "absent":
        cursor.execute.side_effect = unavailable
    elif failure == "fetch":
        cursor.fetchall.side_effect = unavailable
    expected = 0 if failure == "none" else 10
    assert probe.run({}, production_status) == expected
    output = capsys.readouterr()
    assert output.err == ""
    assert len(output.out.splitlines()) == 1
    assert "private-lookup-details" not in output.out
    payload = json.loads(output.out)
    assert payload["exit"] == expected
    assert payload["ok"] is (expected == 0)
    assert payload["schema"]["unexplained"] == ([] if expected == 0 else DEFECTS)
    assert payload["schema"]["pending"] == PENDING
    assert payload["failures"] == ([] if expected == 0 else [
        {"check": "schema_status", "error": "startup validation failed"}])


def test_lookup_failure_preserves_name_in_text_rule(probe, monkeypatch, capsys):
    monkeypatch.setattr(probe, "_CHECKS", ())
    db, _ = fake_database(monkeypatch)
    db.required_catalog_for_selected_authorities.side_effect = RuntimeError("private-details")
    status = production_status()
    status["invalid_constraints"] = [
        "demo_sessions_subject_check:missing-or-wrong-relation"]
    assert probe.run({}, lambda: status) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"]["unexplained"] == []
