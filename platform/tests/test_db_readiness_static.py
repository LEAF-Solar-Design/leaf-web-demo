"""Dependency-light proofs for the PostgreSQL production configuration seam."""
import json
import importlib.util
from pathlib import Path
import re

import pytest


_DB_PATH = Path(__file__).resolve().parents[1] / "db.py"
_AUTHORITY_INVENTORY_PATH = _DB_PATH.with_name("authority-inventory.json")
_SPEC = importlib.util.spec_from_file_location("leaf_platform_db_readiness", _DB_PATH)
db = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(db)


def _assert_manifest_matches_inventory(manifest_names, migration_ids):
    assert isinstance(migration_ids, list)
    assert migration_ids
    assert all(
        isinstance(migration_id, str)
        and re.fullmatch(r"[0-9]{4}", migration_id)
        for migration_id in migration_ids
    )
    assert len(migration_ids) == len(set(migration_ids))
    assert migration_ids == sorted(migration_ids)
    assert migration_ids == [
        f"{number:04d}" for number in range(1, len(migration_ids) + 1)
    ]
    assert [name.split("_", 1)[0] for name in manifest_names] == migration_ids


@pytest.mark.parametrize("url", [
    "sqlite:///tmp/leaf.db",
    "postgresql:///leaf",
    "postgresql://db.example.com",
    "not a url",
])
def test_database_url_rejects_non_postgres_or_incomplete_urls(url):
    with pytest.raises(RuntimeError):
        db.validate_database_url(url)


@pytest.mark.parametrize("url", [
    "postgresql://leaf@db.internal:5432/leaf",
    "postgres://leaf:secret@db.internal/leaf?sslmode=require",
])
def test_database_url_accepts_postgres_without_returning_or_logging_it(url):
    assert db.validate_database_url(url) is None


def test_migration_manifest_is_ordered_complete_and_credential_free():
    manifest = db.migration_manifest()
    manifest_names = [item["name"] for item in manifest]
    inventory = json.loads(_AUTHORITY_INVENTORY_PATH.read_text(encoding="utf-8"))
    assert manifest_names == sorted(manifest_names)
    _assert_manifest_matches_inventory(
        manifest_names, inventory["scope"]["migration_ids"])
    assert all(
        re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
        for item in manifest
    )


@pytest.mark.parametrize(
    ("manifest_names", "migration_ids"),
    [
        (["0001_one.sql", "0002_two.sql"], ["0001"]),
        (["0001_one.sql"], ["0001", "0002"]),
        (["0001_one.sql", "0002_two.sql"], ["0001", "0001"]),
        (["0001_one.sql", "0002_two.sql"], ["0002", "0001"]),
        (["0001_one.sql", "0003_three.sql"], ["0001", "0003"]),
    ],
    ids=["missing", "extra", "duplicate", "reordered", "noncontiguous"],
)
def test_migration_inventory_refuses_incomplete_or_ambiguous_ids(
    manifest_names, migration_ids,
):
    with pytest.raises(AssertionError):
        _assert_manifest_matches_inventory(manifest_names, migration_ids)


def test_request_journal_migration_unconditionally_declares_runtime_schema():
    migration = (
        db._PKG_DIR / "migrations" / "0035_session_request_journal.sql"
    ).read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS app_session_requests" in migration
    expected_indexes = {
        "idx_app_session_requests_one_executing",
        "idx_app_session_requests_one_queued",
        "idx_app_session_requests_recovery",
        "idx_app_session_requests_scope",
    }
    assert expected_indexes == {
        line.split("IF NOT EXISTS ", 1)[1].split()[0]
        for line in migration.splitlines()
        if "INDEX IF NOT EXISTS idx_" in line
    }
    expected_constraints = {
        "app_session_requests_session_id_fkey",
        "app_session_requests_payload_digest_check",
        "app_session_requests_state_check",
        "app_session_requests_response_status_check",
        "app_session_requests_queued_payload_present_check",
        "app_session_requests_nonqueued_payload_absent_check",
        "app_session_requests_executing_lease_shape_check",
        "app_session_requests_terminal_result_shape_check",
    }
    assert expected_constraints == {
        line.strip().split()[1]
        for line in migration.splitlines()
        if line.strip().startswith("CONSTRAINT app_session_requests_")
    }
    assert db._AUTHORITY_REQUIRED_COLUMNS["sessions"][
        "app_session_requests"
    ] == {
        "request_id", "tenant_id", "drawing_id", "session_id",
        "principal_key", "payload_digest", "recoverable_json", "state",
        "turn_id", "lease_owner", "lease_expires_at", "response_status",
        "response_json", "created_at", "updated_at", "terminal_at",
    }
    assert expected_constraints | {"app_session_requests_pkey"} <= {
        name for name in db._AUTHORITY_REQUIRED_CONSTRAINTS["sessions"]
        if name.startswith("app_session_requests_")
    }
    assert expected_indexes == {
        name for name in db._AUTHORITY_REQUIRED_INDEXES["sessions"]
        if name.startswith("idx_app_session_requests_")
    }


def test_customization_authority_migration_follows_tenant_mcp_journal():
    manifest = [item["name"] for item in db.migration_manifest()]
    # 0031 follows 0030 immediately (intent preserved even as later
    # migrations, e.g. 0032 operator control plane, append after it).
    i30 = manifest.index("0030_tenant_mcp_approvals.sql")
    assert manifest[i30 + 1] == "0031_customization_stage_authority.sql"
    migration = (
        db._PKG_DIR / "migrations" / "0031_customization_stage_authority.sql"
    ).read_text(encoding="utf-8")
    assert "ALTER TABLE customization_change_sets" in migration
    assert "authority_session_id TEXT" in migration
    assert "authority_turn_id TEXT" in migration
    assert {"authority_session_id", "authority_turn_id"} <= (
        db._AUTHORITY_REQUIRED_COLUMNS["customization"][
            "customization_change_sets"
        ]
    )


def test_drawing_import_schema_is_required_before_startup():
    assert {"provenance", "idempotency_key", "import_fingerprint"} <= (
        db._REQUIRED_COLUMNS["drawing_versions"]
    )
    assert {"tenant_id", "drawing_id", "attempt", "status", "marker"} <= (
        db._REQUIRED_COLUMNS["drawing_upload_attempts"]
    )
    assert {
        "tenant_id", "drawing_id", "version", "state", "object_key",
        "byte_count", "content_sha256",
    } <= db._REQUIRED_COLUMNS["drawing_store_versions"]
    assert {"binding_id", "platform_tenant_id", "role", "status"} <= (
        db._REQUIRED_COLUMNS["identity_bindings"]
    )
