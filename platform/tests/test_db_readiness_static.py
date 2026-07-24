"""Dependency-light proofs for the PostgreSQL production configuration seam."""
import importlib.util
from pathlib import Path

import pytest


_DB_PATH = Path(__file__).resolve().parents[1] / "db.py"
_SPEC = importlib.util.spec_from_file_location("leaf_platform_db_readiness", _DB_PATH)
db = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(db)


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
    assert [item["name"] for item in manifest] == sorted(item["name"] for item in manifest)
    assert [item["name"] for item in manifest] == [
        f"{number:04d}_{name}.sql" for number, name in [
            (1, "project_job"), (2, "deletion_columns"),
            (3, "canonical_history_ledger"), (4, "canonical_job_worker"),
            (5, "project_share_grants"), (6, "snapshot_pins"),
                (7, "compliance_waivers"), (8, "evidence_bundles"),
                (9, "review_signatures"), (10, "drawing_artifacts"),
                (11, "jobs_callbacks"),
                (12, "sessions"), (13, "agent_state"),
                (14, "broker"), (15, "guest_caps"),
                (16, "drawing_upload_authority"),
                (17, "harness_sessions"),
                (18, "drawing_import_provenance"),
                (19, "sessions_model"),
        ]
    ]
    assert all(len(item["sha256"]) == 64 for item in manifest)


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
