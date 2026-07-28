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
