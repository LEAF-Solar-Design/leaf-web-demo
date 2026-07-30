"""Expand-contract migration gate — self-tests + the real-corpus check.

Run:  cd scripts && python -m pytest test_migration_expand_contract_gate.py -q
(cwd=scripts on purpose: the repo root would shadow the stdlib `platform`.)
"""
from __future__ import annotations

from pathlib import Path

import migration_expand_contract_gate as gate


def _dir(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "migrations"
    root.mkdir(parents=True)
    for name, body in files.items():
        (root / name).write_text(body, encoding="utf-8")
    return root


def test_real_corpus_passes():
    """The shipped platform/migrations tree must always clear its own gate."""
    assert gate.check_migrations(gate.DEFAULT_MIGRATIONS_DIR) == []


def test_additive_migration_passes(tmp_path):
    root = _dir(tmp_path, {"0023_widgets.sql": (
        "CREATE TABLE widgets (id UUID PRIMARY KEY);\n"
        "ALTER TABLE orgs ADD COLUMN IF NOT EXISTS widget_id UUID;\n"
        "CREATE INDEX IF NOT EXISTS idx_w ON widgets (id);\n")})
    assert gate.check_migrations(root) == []


def test_contract_without_marker_fails(tmp_path):
    root = _dir(tmp_path, {"0023_cleanup.sql": "ALTER TABLE orgs DROP COLUMN legacy;\n"})
    violations = gate.check_migrations(root)
    assert len(violations) == 1
    assert "DROP" in violations[0]
    assert "expand-contract" in violations[0]


def test_contract_with_earlier_marker_passes(tmp_path):
    root = _dir(tmp_path, {
        "0017_expand.sql": "ALTER TABLE orgs ADD COLUMN IF NOT EXISTS v2 TEXT;\n",
        "0023_contract.sql": (
            "-- expand-contract: contract-of=0017\n"
            "ALTER TABLE orgs DROP COLUMN v1;\n"),
    })
    assert gate.check_migrations(root) == []


def test_marker_must_reference_earlier_existing_migration(tmp_path):
    root = _dir(tmp_path, {"0023_contract.sql": (
        "-- expand-contract: contract-of=0030\n"
        "ALTER TABLE orgs DROP COLUMN v1;\n")})
    violations = gate.check_migrations(root)
    assert any("EARLIER" in v for v in violations)

    root2 = _dir(tmp_path / "b", {"0023_contract.sql": (
        "-- expand-contract: contract-of=0011\n"
        "ALTER TABLE orgs DROP COLUMN v1;\n")})
    violations2 = gate.check_migrations(root2)
    assert any("does not exist" in v for v in violations2)


def test_block_comments_are_token_separators_not_glue(tmp_path):
    """Round 3, finding 1: `DROP/**/TRIGGER` is valid SQL — stripping the
    comment must leave a separator, not fuse the tokens past the detector."""
    for i, sql in enumerate([
            "DROP/**/TRIGGER trg ON t;",
            "ALTER TYPE e RENAME/**/VALUE 'a' TO 'b';",
            "DROP/* sneaky */TABLE users;",
    ]):
        root = _dir(tmp_path / f"glue{i}", {"0023_x.sql": sql + "\n"})
        violations = gate.check_migrations(root)
        assert violations and "marker" in violations[0], sql


def test_comments_and_strings_never_trip_the_gate(tmp_path):
    root = _dir(tmp_path, {"0023_notes.sql": (
        "-- This note mentions DROP TABLE and RENAME COLUMN in prose.\n"
        "/* also TRUNCATE and DELETE FROM in a block comment */\n"
        "INSERT INTO notes (body) VALUES ('do not DROP TABLE users');\n")})
    assert gate.check_migrations(root) == []


def test_every_contract_pattern_is_detected(tmp_path):
    statements = [
        "DROP TABLE old;",
        "ALTER TABLE t DROP COLUMN c;",
        "DROP INDEX idx;",
        "DROP VIEW v;",
        "ALTER TABLE t DROP CONSTRAINT fk;",
        "ALTER TABLE t RENAME TO t2;",
        "ALTER TABLE t RENAME COLUMN a TO b;",
        "ALTER TABLE t ALTER COLUMN c TYPE BIGINT;",
        "ALTER TABLE t ALTER COLUMN c SET DATA TYPE BIGINT;",
        "ALTER TABLE t ALTER c TYPE BIGINT;",  # PG allows omitting COLUMN
        "ALTER TABLE t ALTER COLUMN c SET NOT NULL;",
        "TRUNCATE t;",
        "DELETE FROM t WHERE stale;",
        "DROP MATERIALIZED VIEW mv;",
        "DROP SCHEMA old_schema;",
        "DROP SEQUENCE seq;",
        "DROP TYPE old_enum;",
        "DROP FUNCTION fn(int);",
        "DROP TRIGGER trg ON t;",
        "DROP POLICY pol ON t;",
        "DROP PROCEDURE proc(int);",
        "ALTER TYPE status_enum RENAME VALUE 'old' TO 'new';",
        "REVOKE SELECT ON t FROM app_role;",
    ]
    for i, sql in enumerate(statements):
        root = _dir(tmp_path / f"case{i}", {"0023_x.sql": sql + "\n"})
        violations = gate.check_migrations(root)
        assert violations and "marker" in violations[0], sql


def test_add_column_not_null_default_is_additive(tmp_path):
    root = _dir(tmp_path, {"0023_add.sql": (
        "ALTER TABLE orgs ADD COLUMN flag BOOLEAN NOT NULL DEFAULT FALSE;\n"
        "ALTER TABLE orgs ALTER COLUMN flag DROP NOT NULL;\n"
        "ALTER TABLE orgs ALTER COLUMN flag SET DEFAULT TRUE;\n")})
    assert gate.check_migrations(root) == []


def test_grandfathered_numbers_are_exempt_and_frozen(tmp_path):
    root = _dir(tmp_path, {"0004_old.sql": "DROP TABLE ancient;\n"})
    assert gate.check_migrations(root) == []
    # the set is exactly 0001..0022 — growing it is how the gate dies quietly
    assert gate.GRANDFATHERED == frozenset(range(1, 23))


def test_bad_filenames_and_duplicates_fail(tmp_path):
    root = _dir(tmp_path, {
        "23_short.sql": "CREATE TABLE a (id INT);\n",
        "0024_Upper.sql": "CREATE TABLE b (id INT);\n",
    })
    violations = gate.check_migrations(root)
    assert len(violations) == 2

    dup = _dir(tmp_path / "dup", {
        "0023_one.sql": "CREATE TABLE a (id INT);\n",
        "0023_two.sql": "CREATE TABLE b (id INT);\n",
    })
    assert any("duplicate" in v for v in gate.check_migrations(dup))


def test_cli_exit_codes(tmp_path, capsys):
    good = _dir(tmp_path / "good", {"0023_ok.sql": "CREATE TABLE a (id INT);\n"})
    assert gate.main(["--migrations-dir", str(good)]) == 0
    assert "PASS" in capsys.readouterr().out
    bad = _dir(tmp_path / "bad", {"0023_bad.sql": "DROP TABLE a;\n"})
    assert gate.main(["--migrations-dir", str(bad)]) == 1
    assert "FAIL" in capsys.readouterr().out
