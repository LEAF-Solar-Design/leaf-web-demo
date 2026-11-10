"""Expand-contract migration gate — self-tests + the real-corpus check.

Run:  cd scripts && python -m pytest test_migration_expand_contract_gate.py -q
(cwd=scripts on purpose: the repo root would shadow the stdlib `platform`.)
"""
from __future__ import annotations

from pathlib import Path

import pytest

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


@pytest.mark.parametrize("revoke", [
    "REVOKE ALL ON public.widgets FROM PUBLIC;",
    "REVOKE ALL PRIVILEGES ON TABLE public.widgets FROM PUBLIC;",
    "revoke all on table PUBLIC.WIDGETS from public;",
    "REVOKE/**/ALL ON public.widgets FROM PUBLIC;",
])
def test_new_table_privilege_initialization_passes(tmp_path, revoke):
    root = _dir(tmp_path, {"0023_widgets.sql": (
        "-- CREATE TABLE public.other (id int);\n"
        "CREATE TABLE public.widgets (id INT);\n" + revoke)})
    assert gate.check_migrations(root) == []


@pytest.mark.parametrize("sql", [
    "REVOKE ALL ON public.widgets FROM PUBLIC;",
    "REVOKE ALL ON public.widgets FROM PUBLIC; CREATE TABLE public.widgets (id INT);",
    "CREATE TABLE IF NOT EXISTS public.widgets (id INT); REVOKE ALL ON public.widgets FROM PUBLIC;",
    "CREATE TABLE public.other (id INT); REVOKE ALL ON public.widgets FROM PUBLIC;",
    "CREATE TABLE private.widgets (id INT); REVOKE ALL ON public.widgets FROM PUBLIC;",
    "DO $$ BEGIN CREATE TABLE public.widgets (id INT); END $$; REVOKE ALL ON public.widgets FROM PUBLIC;",
    "DO $body$ BEGIN; CREATE TABLE public.widgets (id INT); END $body$; REVOKE ALL ON public.widgets FROM PUBLIC;",
    "CREATE FUNCTION f() RETURNS void LANGUAGE SQL BEGIN ATOMIC SELECT 1; CREATE TABLE public.widgets (id INT); END; REVOKE ALL ON public.widgets FROM PUBLIC;",
    "-- CREATE TABLE public.widgets (id INT);\nREVOKE ALL ON public.widgets FROM PUBLIC;",
    "SELECT 'CREATE TABLE public.widgets (id INT);'; REVOKE ALL ON public.widgets FROM PUBLIC;",
    'CREATE TABLE public."widgets" (id INT); REVOKE ALL ON public.widgets FROM PUBLIC;',
    'CREATE TABLE public.widgets (id INT); REVOKE ALL ON public."widgets" FROM PUBLIC;',
    "CREATE TABLE widgets (id INT); REVOKE ALL ON widgets FROM PUBLIC;",
    "CREATE TABLE public.widgets (id INT); REVOKE SELECT ON public.widgets FROM PUBLIC;",
    "CREATE TABLE public.widgets (id INT); REVOKE ALL ON public.widgets FROM app_role;",
    "CREATE TABLE public.widgets (id INT); REVOKE ALL ON public.widgets FROM PUBLIC, app_role;",
    "CREATE TABLE public.widgets (id INT); REVOKE ALL ON public.widgets FROM PUBLIC CASCADE;",
    "CREATE TABLE public.widgets (id INT); REVOKE ALL ON public.widgets FROM PUBLIC",
    "CREATE TABLE public.widgets (id INT); REVOKE ALL ON public.widgets, public.other FROM PUBLIC;",
    "CREATE TABLE public.widgets (id INT); REVOKE ALL ON SEQUENCE public.widgets FROM PUBLIC;",
])
def test_other_revokes_still_require_marker(tmp_path, sql):
    root = _dir(tmp_path, {"0023_widgets.sql": sql})
    violations = gate.check_migrations(root)
    assert len(violations) == 1
    assert "REVOKE" in violations[0]


@pytest.mark.parametrize("other,label", [
    ("DROP TABLE public.legacy;", "DROP"),
    ("REVOKE ALL ON public.legacy FROM PUBLIC;", "REVOKE"),
    ("REVOKE SELECT ON public.widgets FROM app_role;", "REVOKE"),
])
def test_initialization_does_not_exempt_other_contracts(tmp_path, other, label):
    root = _dir(tmp_path, {"0023_widgets.sql": (
        "CREATE TABLE public.widgets (id INT);\n"
        "REVOKE ALL ON public.widgets FROM PUBLIC;\n" + other)})
    violations = gate.check_migrations(root)
    assert len(violations) == 1
    assert f"[{label}]" in violations[0]


def test_table_created_in_earlier_migration_is_not_initialization(tmp_path):
    root = _dir(tmp_path, {
        "0023_widgets.sql": "CREATE TABLE public.widgets (id INT);",
        "0024_privileges.sql": "REVOKE ALL ON public.widgets FROM PUBLIC;",
    })
    violations = gate.check_migrations(root)
    assert len(violations) == 1
    assert "0024_privileges.sql" in violations[0]
    assert "REVOKE" in violations[0]


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
            # PostgreSQL block comments NEST — the whole outer comment is one
            # separator (round 4 finding).
            "DROP/* outer /* inner */ NOT */TRIGGER trg ON t;",
            "ALTER TABLE t ALTER/* o /* i */ garbage */c TYPE bigint;",
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


def test_bad_filenames_fail(tmp_path):
    root = _dir(tmp_path, {
        "23_short.sql": "CREATE TABLE a (id INT);\n",
        "0024_Upper.sql": "CREATE TABLE b (id INT);\n",
    })
    violations = gate.check_migrations(root)
    assert len(violations) == 2


@pytest.mark.parametrize("destructive", [None, "0023_one.sql", "0023_two.sql"])
def test_same_prefix_migrations_validate_each_body(tmp_path, destructive):
    files = {
        "0023_one.sql": "CREATE TABLE a (id INT);\n",
        "0023_two.sql": "CREATE TABLE b (id INT);\n",
    }
    if destructive is not None:
        files[destructive] = "DROP TABLE existing;\n"
    violations = gate.check_migrations(_dir(tmp_path, files))
    if destructive is None:
        assert violations == []
    else:
        assert len(violations) == 1
        assert violations[0].startswith(destructive + ":")
        assert "[DROP]" in violations[0] and "without an" in violations[0]


def test_cli_exit_codes(tmp_path, capsys):
    good = _dir(tmp_path / "good", {"0023_ok.sql": "CREATE TABLE a (id INT);\n"})
    assert gate.main(["--migrations-dir", str(good)]) == 0
    assert "PASS" in capsys.readouterr().out
    bad = _dir(tmp_path / "bad", {"0023_bad.sql": "DROP TABLE a;\n"})
    assert gate.main(["--migrations-dir", str(bad)]) == 1
    assert "FAIL" in capsys.readouterr().out


# --- widening a value-list CHECK is additive ---------------------------------

_BASE = ("CREATE TABLE IF NOT EXISTS receipts (\n  id UUID PRIMARY KEY,\n  action TEXT NOT NULL,\n"
         "  CONSTRAINT receipts_action_check\n    CHECK (action IN (\n      'created', 'reset'\n    )),\n"
         "  CONSTRAINT receipts_key_check CHECK (char_length(action) BETWEEN 1 AND 200)\n);\n")
_WIDEN = ("ALTER TABLE receipts DROP CONSTRAINT IF EXISTS receipts_action_check;\n"
          "ALTER TABLE receipts ADD CONSTRAINT receipts_action_check CHECK (action IN (\n"
          "  'created', 'reset',\n  'renamed'\n));\n")


def _widening(tmp_path, change, base=_BASE):
    return gate.check_migrations(_dir(tmp_path, {
        "0038_receipts.sql": base, "0103_change.sql": change}))


def test_widening_a_value_list_check_needs_no_marker(tmp_path):
    assert _widening(tmp_path, "-- adds one action\n" + _WIDEN) == []
    assert _widening(tmp_path / "lower", _WIDEN.lower()) == []


def test_widening_twice_is_grounded_in_the_latest_definition(tmp_path):
    again = _WIDEN.replace("'renamed'", "'renamed', 'archived'")
    root = _dir(tmp_path, {"0038_receipts.sql": _BASE, "0103_change.sql": _WIDEN, "0104_more.sql": again})
    assert gate.check_migrations(root) == []
    # 0104 dropping what 0103 added is a narrowing, judged against 0103, not 0038.
    narrowed = _dir(tmp_path / "n", {"0038_receipts.sql": _BASE, "0103_change.sql": _WIDEN,
                                     "0104_less.sql": _WIDEN.replace(",\n  'renamed'", "")})
    assert [v.split(":")[0] for v in gate.check_migrations(narrowed)] == ["0104_less.sql"]


@pytest.mark.parametrize("change", [
    # a literal removed
    _WIDEN.replace("'created', ", ""),
    # same count, one literal replaced
    _WIDEN.replace("'reset'", "'restarted'"),
    # the drop alone
    "ALTER TABLE receipts DROP CONSTRAINT IF EXISTS receipts_action_check;\n",
    # re-added under another name
    _WIDEN.replace("ADD CONSTRAINT receipts_action_check", "ADD CONSTRAINT receipts_action_check2"),
    # re-added on another table
    _WIDEN.replace("ALTER TABLE receipts ADD", "ALTER TABLE other ADD"),
    # another column
    _WIDEN.replace("CHECK (action IN", "CHECK (kind IN"),
    # a statement between the pair
    _WIDEN.replace(";\nALTER TABLE receipts ADD", ";\nSELECT 1;\nALTER TABLE receipts ADD"),
    # not a value list
    _WIDEN.split(";\n")[0] + ";\nALTER TABLE receipts ADD CONSTRAINT receipts_action_check CHECK (action <> '');\n",
    # a different constraint that was never a value list
    _WIDEN.replace("receipts_action_check", "receipts_key_check"),
    # a constraint no earlier migration defines
    _WIDEN.replace("receipts_action_check", "receipts_unknown_check"),
    # the widening pair does not excuse another drop in the file
    _WIDEN + "ALTER TABLE receipts DROP COLUMN action;\n",
    _WIDEN + "DROP TABLE receipts;\n",
    # a duplicated literal is not a clean list
    _WIDEN.replace("'renamed'", "'renamed', 'created'"),
    # tagged dollar bodies are not read, so nothing is excused
    "DO $b$ BEGIN NULL; END $b$;\n" + _WIDEN,
])
def test_anything_but_a_pure_widening_still_needs_a_marker(tmp_path, change):
    violations = _widening(tmp_path, change)
    assert len(violations) == 1 and violations[0].startswith("0103_change.sql")
    assert "DROP" in violations[0]


def test_an_unrecognized_touch_forgets_the_prior_definition(tmp_path):
    # 0050 re-shapes the constraint in a form the gate does not read, so 0103
    # can no longer be proved a widening of what the database holds.
    reshaped = ("-- expand-contract: contract-of=0038\n"
                "ALTER TABLE receipts DROP CONSTRAINT receipts_action_check;\n"
                "ALTER TABLE receipts ADD CONSTRAINT receipts_action_check CHECK (action <> 'x');\n")
    root = _dir(tmp_path, {"0038_receipts.sql": _BASE, "0050_reshape.sql": reshaped, "0103_change.sql": _WIDEN})
    assert [v.split(":")[0] for v in gate.check_migrations(root)] == ["0103_change.sql"]


def test_widening_with_a_marker_or_in_the_defining_file_behaves_as_before(tmp_path):
    marked = "-- expand-contract: contract-of=0038\n" + _WIDEN.replace("'created', ", "")
    assert _widening(tmp_path, marked) == []
    same_file = _dir(tmp_path / "same", {"0103_all.sql": _BASE + _WIDEN})
    assert gate.check_migrations(same_file) == []


def test_real_rename_receipt_migration_is_the_recognized_widening():
    text = (gate.DEFAULT_MIGRATIONS_DIR / "0103_project_rename_receipt.sql").read_text(encoding="utf-8")
    assert "DROP CONSTRAINT IF EXISTS project_lifecycle_receipts_action_check" in text
    assert "contract-of" not in text


@pytest.mark.parametrize("between", [
    # A second CREATE TABLE IF NOT EXISTS is a no-op: its text is not the live constraint.
    "CREATE TABLE IF NOT EXISTS receipts (id UUID, action TEXT,\n"
    "  CONSTRAINT receipts_action_check CHECK (action IN ('created')));\n",
    # The table was dropped (with its marker) and no longer holds the tracked constraint.
    "-- expand-contract: contract-of=0038\nDROP TABLE receipts;\n",
    # A rename of the column or the table breaks the tracked identity.
    "-- expand-contract: contract-of=0038\nALTER TABLE receipts RENAME COLUMN action TO kind;\n",
])
def test_a_table_reshaped_in_between_forgets_its_constraints(tmp_path, between):
    root = _dir(tmp_path, {"0038_receipts.sql": _BASE, "0050_between.sql": between, "0103_change.sql": _WIDEN})
    assert [v.split(":")[0] for v in gate.check_migrations(root)] == ["0103_change.sql"]
