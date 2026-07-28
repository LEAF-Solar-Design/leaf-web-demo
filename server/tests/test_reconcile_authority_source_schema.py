"""The authority reconcile script must self-initialize its SQLite source.

The staging authored-execution activation failed live on 2026-07-28 with
"SQLite customization source is incomplete: customization_publication_requests"
because the app's SQLite store creates tables lazily (and migrates legacy
shapes) only on first touch, while scripts/reconcile_customization_authority.py
snapshotted the file read-only and refused any store missing a table. These
tests pin the fix: the script runs the store's own idempotent
``initialize()`` before snapshotting, so a never-touched store, a legacy
store, and a brand-new environment all read as complete.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _script():
    name = "reconcile_customization_authority_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / "reconcile_customization_authority.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_missing_file_becomes_a_complete_empty_snapshot(tmp_path):
    script = _script()
    path = tmp_path / "state" / "customization.db"
    assert not path.exists()
    script._ensure_source_schema(path)
    snapshot = script._sqlite_snapshot(path)
    assert set(snapshot) == set(script.TABLE_COLUMNS)
    assert all(rows == [] for rows in snapshot.values())


def test_store_missing_one_table_is_repaired_not_refused(tmp_path):
    script = _script()
    path = tmp_path / "customization.db"
    script._ensure_source_schema(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE customization_publication_requests")
    # Without the ensure step this is exactly the live failure shape.
    try:
        script._sqlite_snapshot(path)
    except RuntimeError as error:
        assert "customization_publication_requests" in str(error)
    else:  # pragma: no cover - guards against silently weakened snapshot
        raise AssertionError("snapshot accepted an incomplete source")
    script._ensure_source_schema(path)
    snapshot = script._sqlite_snapshot(path)
    assert snapshot["customization_publication_requests"] == []


def test_reconcile_ensures_schema_before_snapshotting(tmp_path, monkeypatch):
    """Deleting the ensure call from reconcile() must fail THIS test.

    reconcile() is exercised for real up to the PostgreSQL boundary: the
    platform-db loader is replaced with a sentinel so the test proves the
    ordering (ensure, then snapshot, then database) without a live Postgres.
    """
    script = _script()
    path = tmp_path / "state" / "customization.db"
    assert not path.exists()

    class _Sentinel(RuntimeError):
        pass

    calls: list[str] = []
    real_ensure = script._ensure_source_schema
    real_snapshot = script._sqlite_snapshot

    def recording_ensure(target):
        calls.append("ensure")
        return real_ensure(target)

    def recording_snapshot(target):
        calls.append("snapshot")
        return real_snapshot(target)

    def no_database():
        calls.append("database")
        raise _Sentinel("stop before PostgreSQL")

    monkeypatch.setattr(script, "_ensure_source_schema", recording_ensure)
    monkeypatch.setattr(script, "_sqlite_snapshot", recording_snapshot)
    monkeypatch.setattr(script, "_platform_db", no_database)

    try:
        script.reconcile(sqlite_path=path, mode="backfill")
    except _Sentinel:
        pass
    else:  # pragma: no cover - reconcile must reach the database boundary
        raise AssertionError("reconcile() never reached the database boundary")
    # The never-touched source was initialized BEFORE the snapshot read it;
    # without the ensure, the snapshot raises "incomplete" (or FileNotFound)
    # and this ordering is never recorded.
    assert calls == ["ensure", "snapshot", "database"]
    assert path.exists()


def test_ensure_is_idempotent_and_preserves_rows(tmp_path):
    script = _script()
    path = tmp_path / "customization.db"
    script._ensure_source_schema(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO customization_publication_requests "
            "(tenant_id, change_set_id, confirmation_id, status, reason_code,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("t1", "c1", "conf1", "approved", None, "2026-07-28T00:00:00Z",
             "2026-07-28T00:00:00Z"),
        )
    script._ensure_source_schema(path)
    snapshot = script._sqlite_snapshot(path)
    rows = snapshot["customization_publication_requests"]
    assert len(rows) == 1
    assert rows[0]["tenant_id"] == "t1"
    assert rows[0]["change_set_id"] == "c1"


def test_locked_source_is_retried_then_fails_loud(tmp_path):
    """A busy database must not turn a transient lock into a failed activation.

    The activation task runs against the same EFS file the live app serves
    from, so SQLite write locks are expected. Bounded retry converts the
    common case into success; an unrelenting lock still fails loud rather
    than silently proceeding to snapshot an unmigrated store.
    """
    script = _script()
    path = tmp_path / "customization.db"
    slept: list[float] = []

    calls = {"n": 0}
    real_ensure_store = None

    class _Flaky:
        def __init__(self, target):
            self.target = target

        def initialize(self):
            calls["n"] += 1
            if calls["n"] < 3:
                raise sqlite3.OperationalError("database is locked")
            _real_store(self.target).initialize()

    import customization_store as store_module

    _real_store = store_module.SQLiteCustomizationStore
    store_module.SQLiteCustomizationStore = _Flaky
    try:
        script._ensure_source_schema(path, sleep=slept.append)
    finally:
        store_module.SQLiteCustomizationStore = _real_store
    assert calls["n"] == 3
    assert slept == [script._ENSURE_BACKOFF_SECONDS] * 2
    assert path.exists()

    calls["n"] = 0
    slept.clear()

    class _AlwaysLocked:
        def __init__(self, target):
            pass

        def initialize(self):
            calls["n"] += 1
            raise sqlite3.OperationalError("database is locked")

    store_module.SQLiteCustomizationStore = _AlwaysLocked
    try:
        try:
            script._ensure_source_schema(
                tmp_path / "other.db", attempts=3, sleep=slept.append
            )
        except RuntimeError as error:
            assert "stayed locked after 3" in str(error)
        else:  # pragma: no cover - the bound must fail loud
            raise AssertionError("an unrelenting lock did not fail loud")
    finally:
        store_module.SQLiteCustomizationStore = _real_store
    assert calls["n"] == 3
    assert len(slept) == 2


def test_non_lock_operational_error_is_not_retried(tmp_path):
    script = _script()
    import customization_store as store_module

    calls = {"n": 0}
    _real_store = store_module.SQLiteCustomizationStore

    class _Corrupt:
        def __init__(self, target):
            pass

        def initialize(self):
            calls["n"] += 1
            raise sqlite3.OperationalError("file is not a database")

    store_module.SQLiteCustomizationStore = _Corrupt
    try:
        try:
            script._ensure_source_schema(tmp_path / "bad.db", sleep=lambda _: None)
        except sqlite3.OperationalError as error:
            assert "not a database" in str(error)
        else:  # pragma: no cover
            raise AssertionError("a non-lock error was swallowed")
    finally:
        store_module.SQLiteCustomizationStore = _real_store
    assert calls["n"] == 1
