from __future__ import annotations

import copy
import importlib.util
from contextlib import contextmanager
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "customization_incremental_reconcile",
    ROOT / "scripts" / "reconcile_customization_authority.py",
)
assert SPEC and SPEC.loader
RECONCILE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECONCILE)


def _empty():
    return {table: [] for table in RECONCILE.TABLE_COLUMNS}


def _row(table: str, identity: str):
    values = {column: f"{column}:{identity}" for column in RECONCILE.TABLE_COLUMNS[table]}
    if "version" in values:
        values["version"] = 1
    if "consumed" in values:
        values["consumed"] = 0
    return values


class _Connection:
    def __init__(self, snapshot):
        self.snapshot = copy.deepcopy(snapshot)
        self.statements: list[str] = []
        self.after_insert = None

    def execute(self, statement, values=()):
        self.statements.append(statement)
        if statement.startswith("SELECT pg_advisory_xact_lock"):
            return None
        assert statement.startswith("INSERT INTO ")
        table = statement.split()[2]
        columns = RECONCILE.TABLE_COLUMNS[table]
        self.snapshot[table].append(dict(zip(columns, values, strict=True)))
        if self.after_insert:
            self.after_insert(self.snapshot[table][-1])
        return None


class _Database:
    def __init__(self, snapshot):
        self.connection = _Connection(snapshot)
        self.transactions = 0

    def assert_schema_current(self):
        pass

    @contextmanager
    def transaction(self, *, isolation):
        assert isolation == "serializable"
        self.transactions += 1
        before = copy.deepcopy(self.connection.snapshot)
        try:
            yield self.connection
        except Exception:
            self.connection.snapshot = before
            raise


def _run(monkeypatch, source, target, *, database=None, mode="backfill"):
    database = database or _Database(target)
    monkeypatch.setattr(RECONCILE, "_ensure_source_schema", lambda _: None)
    monkeypatch.setattr(RECONCILE, "_sqlite_snapshot", lambda _: copy.deepcopy(source))
    monkeypatch.setattr(RECONCILE, "_platform_db", lambda: database)
    monkeypatch.setattr(
        RECONCILE,
        "_postgres_snapshot",
        lambda connection: copy.deepcopy(connection.snapshot),
    )
    return RECONCILE.reconcile(
        sqlite_path=Path("unused-by-fakes.db"), mode=mode
    ), database


def test_empty_authorities_are_idempotent(monkeypatch):
    receipt, database = _run(monkeypatch, _empty(), _empty())
    assert receipt["parity"] is True
    assert not [s for s in database.connection.statements if s.startswith("INSERT")]


def test_identical_nonempty_authorities_are_idempotent(monkeypatch):
    source = _empty()
    source["customization_change_sets"] = [
        _row("customization_change_sets", "same")
    ]
    receipt, database = _run(monkeypatch, source, source)
    assert receipt["parity"] is True
    assert not [s for s in database.connection.statements if s.startswith("INSERT")]


def test_strict_subset_inserts_only_missing_rows_in_table_order(monkeypatch):
    source = _empty()
    first_table, second_table = tuple(RECONCILE.TABLE_COLUMNS)[:2]
    source[first_table] = [_row(first_table, "present"), _row(first_table, "missing")]
    source[second_table] = [_row(second_table, "missing")]
    target = _empty()
    target[first_table] = [source[first_table][0]]

    receipt, database = _run(monkeypatch, source, target)

    assert receipt["parity"] is True
    inserts = [s for s in database.connection.statements if s.startswith("INSERT")]
    assert [statement.split()[2] for statement in inserts] == [first_table, second_table]
    assert database.connection.snapshot == source


def test_matching_primary_key_with_different_content_is_rejected(monkeypatch):
    source = _empty()
    table = "customization_change_sets"
    source[table] = [_row(table, "shared")]
    target = copy.deepcopy(source)
    target[table][0]["state"] = "different-secret-value"

    with pytest.raises(RuntimeError, match="conflicting row") as caught:
        _run(monkeypatch, source, target)
    assert "different-secret-value" not in str(caught.value)


def test_nullable_and_json_text_content_must_match_exactly(monkeypatch):
    source = _empty()
    table = "customization_confirmations"
    source[table] = [_row(table, "shared")]
    source[table][0]["payload_json"] = '{"a":1}'
    source[table][0]["signature"] = None
    target = copy.deepcopy(source)
    target[table][0]["payload_json"] = '{"a": 1}'

    with pytest.raises(RuntimeError, match="conflicting row"):
        _run(monkeypatch, source, target)


def test_target_only_row_is_preserved_as_prior_cutover_state(monkeypatch):
    source = _empty()
    target = _empty()
    table = "customization_change_sets"
    target[table] = [_row(table, "target-only-secret")]

    receipt, database = _run(monkeypatch, source, target)

    assert receipt["parity"] is False
    assert receipt["exact_equal"] is False
    assert receipt["source_incorporated"] is True
    assert receipt["target_only_counts"][table] == 1
    assert database.connection.snapshot == target
    assert not [s for s in database.connection.statements if s.startswith("INSERT")]


def test_parity_accepts_a_compatible_target_superset(monkeypatch):
    source = _empty()
    target = _empty()
    table = "customization_change_sets"
    target[table] = [_row(table, "retained")]

    receipt, _ = _run(monkeypatch, source, target, mode="parity")

    assert receipt["parity"] is False
    assert receipt["exact_equal"] is False
    assert receipt["source_incorporated"] is True


def test_parity_rejects_source_rows_missing_from_target(monkeypatch):
    source = _empty()
    table = "customization_change_sets"
    source[table] = [_row(table, "not-copied")]

    with pytest.raises(RuntimeError, match="parity failed"):
        _run(monkeypatch, source, _empty(), mode="parity")


@pytest.mark.parametrize("side", ["source", "target"])
def test_duplicate_primary_key_is_rejected(monkeypatch, side):
    source = _empty()
    table = "customization_change_sets"
    duplicate = _row(table, "duplicate-secret")
    source[table] = [duplicate, copy.deepcopy(duplicate)]
    target = copy.deepcopy(source) if side == "target" else _empty()
    if side == "target":
        source[table] = [duplicate]

    with pytest.raises(RuntimeError, match="duplicate primary key") as caught:
        _run(monkeypatch, source, target)
    assert "duplicate-secret" not in str(caught.value)


@pytest.mark.parametrize("side", ["source", "target"])
def test_null_primary_key_is_rejected(monkeypatch, side):
    source = _empty()
    table = "customization_change_sets"
    null_key = _row(table, "null-secret")
    null_key["change_set_id"] = None
    if side == "source":
        source[table] = [null_key]
        target = _empty()
    else:
        target = _empty()
        target[table] = [null_key]

    with pytest.raises(RuntimeError, match="ambiguous primary key") as caught:
        _run(monkeypatch, source, target)
    assert "null-secret" not in str(caught.value)


def test_incremental_backfill_is_repeat_idempotent(monkeypatch):
    source = _empty()
    table = "customization_change_sets"
    source[table] = [_row(table, "existing"), _row(table, "new")]
    target = _empty()
    target[table] = [source[table][0]]
    database = _Database(target)

    first, _ = _run(monkeypatch, source, target, database=database)
    first_insert_count = len(
        [s for s in database.connection.statements if s.startswith("INSERT")]
    )
    second, _ = _run(
        monkeypatch, source, database.connection.snapshot, database=database
    )

    assert first["digest"] == second["digest"]
    assert first_insert_count == 1
    assert len([s for s in database.connection.statements if s.startswith("INSERT")]) == 1
    assert database.transactions == 2


def test_post_insert_reread_must_reach_full_digest_parity(monkeypatch):
    source = _empty()
    table = "customization_change_sets"
    source[table] = [_row(table, "new")]
    database = _Database(_empty())
    database.connection.after_insert = lambda row: row.update(state="corrupted")

    with pytest.raises(RuntimeError, match="conflicting row"):
        _run(monkeypatch, source, _empty(), database=database)
    assert database.connection.snapshot == _empty()
