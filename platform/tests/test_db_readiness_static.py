"""Dependency-light proofs for the PostgreSQL production configuration seam."""
import inspect
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

# The path a failure message below names, so a reviewer staring at a red CI
# job can jump straight to the pin instead of spelunking the traceback for a
# path relative to some other cwd (the platform suite runs from REPO_PARENT).
_THIS_FILE = "platform/tests/test_db_readiness_static.py"


def _assert_closed_world_pin(actual, expected, remediation):
    """Fail a hand-pinned "tail of main" check by naming the exact call site.

    These pins exist because a closed-world registry (the migration
    manifest's last entry, here) can drift silently past main: nothing else
    in the codebase notices when a new migration lands without this file's
    pin being bumped to match. A bare ``assert actual == expected`` makes a
    reviewer diff two opaque values and guess what to edit; naming the file,
    the exact line holding the stale pin, and what to change there turns that
    guess into a one-line fix.
    """
    caller = inspect.stack()[1]
    location = f"{_THIS_FILE}:{caller.lineno}"
    assert actual == expected, (
        f"{location} pins {expected!r} but found {actual!r}. {remediation}"
    )


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


def test_annotation_migration_is_in_the_unconditional_readiness_inventory():
    manifest_names = [item["name"] for item in db.migration_manifest()]
    inventory = json.loads(_AUTHORITY_INVENTORY_PATH.read_text(encoding="utf-8"))
    assert "0042_annotation_batches.sql" in manifest_names
    _assert_closed_world_pin(
        manifest_names[-1], "0054_arlo_lab_inputs.sql",
        "Update this pin to the new last migration filename once main adds "
        "one (and the migration_ids pin below to match).",
    )
    _assert_closed_world_pin(
        inventory["scope"]["migration_ids"][-1], "0054",
        "Update this pin (and authority-inventory.json's "
        "scope.migration_ids) to the new last migration id.",
    )
    assert "annotation_targets" in db._REQUIRED_COLUMNS
    assert "annotation_batches" in db._REQUIRED_COLUMNS
    assert "annotation_audit" in db._REQUIRED_COLUMNS
    catalog = db.required_catalog_for_selected_authorities({})
    assert "annotation_batches_target_fk" in catalog["constraints"]
    assert "annotation_batches_request_key_uq" in catalog["indexes"]


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
        "org_id", "project_id",
    }
    assert expected_constraints | {"app_session_requests_pkey"} <= {
        name for name in db._AUTHORITY_REQUIRED_CONSTRAINTS["sessions"]
        if name.startswith("app_session_requests_")
    }
    assert expected_indexes <= {
        name for name in db._AUTHORITY_REQUIRED_INDEXES["sessions"]
        if name.startswith("idx_app_session_requests_")
    }


def test_project_lifecycle_migration_unconditionally_declares_runtime_schema():
    migration = (
        db._PKG_DIR / "migrations" / "0038_project_lifecycle.sql"
    ).read_text(encoding="utf-8")
    expected_tables = {
        "project_member_bindings",
        "project_files",
        "project_lifecycle_receipts",
    }
    assert expected_tables == {
        line.split("IF NOT EXISTS ", 1)[1].split()[0]
        for line in migration.splitlines()
        if line.startswith("CREATE TABLE IF NOT EXISTS ")
    }
    expected_indexes = {
        "project_member_bindings_one_active",
        "idx_project_member_bindings_scope",
        "idx_project_files_scope",
        "idx_project_lifecycle_receipts_scope",
        "project_lifecycle_receipts_create_idempotency",
    }
    assert expected_indexes == {
        line.split("IF NOT EXISTS ", 1)[1].split()[0]
        for line in migration.splitlines()
        if "INDEX IF NOT EXISTS " in line
    }
    expected_constraints = {
        "identity_bindings_tenant_binding_unique",
        "project_member_bindings_project_fk",
        "project_member_bindings_binding_fk",
        "project_member_bindings_inviter_fk",
        "project_member_bindings_role_check",
        "project_member_bindings_status_check",
        "project_member_bindings_revocation_shape_check",
        "project_files_project_fk",
        "project_files_creator_fk",
        "project_files_path_check",
        "project_files_media_type_check",
        "project_files_content_sha256_check",
        "project_files_revision_check",
        "project_files_scope_path_unique",
        "project_lifecycle_receipts_project_fk",
        "project_lifecycle_receipts_actor_fk",
        "project_lifecycle_receipts_action_check",
        "project_lifecycle_receipts_idempotency_key_check",
        "project_lifecycle_receipts_input_digest_check",
        "project_lifecycle_receipts_idempotency_unique",
    }
    assert expected_constraints == {
        line.strip().split()[2 if line.strip().startswith("ADD CONSTRAINT") else 1]
        for line in migration.splitlines()
        if line.strip().startswith(("CONSTRAINT ", "ADD CONSTRAINT "))
    }
    assert db._REQUIRED_COLUMNS["project_member_bindings"] == {
        "membership_id", "org_id", "project_id", "binding_id", "role", "status",
        "invited_by_binding_id", "created_at", "revoked_at",
    }
    assert db._REQUIRED_COLUMNS["project_files"] == {
        "file_id", "org_id", "project_id", "path", "media_type", "content",
        "content_sha256", "revision", "created_by_binding_id", "created_at",
        "updated_at",
    }
    assert db._REQUIRED_COLUMNS["project_lifecycle_receipts"] == {
        "receipt_id", "org_id", "project_id", "action", "actor_binding_id",
        "idempotency_key", "input_digest", "result_json", "created_at",
    }
    assert expected_constraints <= set(db._REQUIRED_CONSTRAINTS)
    assert expected_indexes <= set(db._REQUIRED_INDEXES)
    assert db._REQUIRED_TRIGGERS["project_lifecycle_receipts_immutable"] == (
        db._catalog_contract(
            "project_lifecycle_receipts", "BEFORE DELETE OR UPDATE", "FOR EACH ROW",
            "EXECUTE FUNCTION leaf_reject_ledger_mutation()")
    )


def test_project_session_scope_migration_preserves_legacy_and_binds_projects():
    migration = (
        db._PKG_DIR / "migrations" / "0039_project_session_scope.sql"
    ).read_text(encoding="utf-8")
    expected_constraints = {
        "app_sessions_project_scope_shape_check",
        "app_sessions_project_tenant_check",
        "app_sessions_project_fk",
        "app_sessions_project_scope_unique",
        "app_session_requests_project_scope_shape_check",
        "app_session_requests_project_tenant_check",
        "app_session_requests_project_fk",
        "app_session_requests_session_project_fk",
    }
    assert expected_constraints == {
        line.strip().split()[2]
        for line in migration.splitlines()
        if line.strip().startswith("ADD CONSTRAINT app_session")
    }
    expected_indexes = {
        "idx_app_sessions_one_project",
        "idx_app_session_requests_project_scope",
    }
    assert expected_indexes == {
        line.split("IF NOT EXISTS ", 1)[1].split()[0]
        for line in migration.splitlines()
        if "INDEX IF NOT EXISTS idx_app_session" in line
    }
    assert "CHECK ((org_id IS NULL) = (project_id IS NULL))" in migration
    assert "'project:' || org_id::TEXT || ':' || project_id::TEXT" in migration
    assert "DROP" not in migration.upper()
    assert "REFERENCES projects(org_id, project_id) ON DELETE CASCADE" in migration
    assert (
        "REFERENCES app_sessions(session_id, org_id, project_id) ON DELETE CASCADE"
        in migration
    )
    assert {"org_id", "project_id"} <= db._AUTHORITY_REQUIRED_COLUMNS[
        "sessions"
    ]["app_sessions"]
    assert {"org_id", "project_id"} <= db._AUTHORITY_REQUIRED_COLUMNS[
        "sessions"
    ]["app_session_requests"]
    assert expected_constraints <= set(
        db._AUTHORITY_REQUIRED_CONSTRAINTS["sessions"]
    )
    assert "app_sessions_tenant_id_drawing_id_key" in (
        db._AUTHORITY_REQUIRED_CONSTRAINTS["sessions"]
    )
    assert expected_indexes <= set(db._AUTHORITY_REQUIRED_INDEXES["sessions"])


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


def test_manifest_tail_pin_drift_names_the_pin_line_and_remediation():
    """lwd#690 shape: main added a migration and this file's tail pin was not
    bumped to match it. Re-running that shard shape must not surface a bare
    ``AssertionError`` -- it must name this file, the exact line holding the
    stale pin, and how to fix it, or a reviewer has to reverse-engineer the
    fix from two opaque diffed values.
    """
    with pytest.raises(AssertionError) as exc_info:
        _assert_closed_world_pin(
            "0051_next_migration.sql", "0050_unit_economics.sql",
            "Update this pin to the new last migration filename once main "
            "adds one.",
        )
    message = str(exc_info.value)
    assert re.search(rf"{re.escape(_THIS_FILE)}:\d+", message), message
    assert "0051_next_migration.sql" in message
    assert "0050_unit_economics.sql" in message
    assert (
        "Update this pin to the new last migration filename once main "
        "adds one." in message
    )


def test_migration_id_tail_pin_drift_names_the_pin_line_and_remediation():
    """The migration_ids counterpart of the pin above must fail the same way:
    naming this file, the drifted line, and the exact remediation."""
    with pytest.raises(AssertionError) as exc_info:
        _assert_closed_world_pin(
            "0051", "0050",
            "Update this pin (and authority-inventory.json's "
            "scope.migration_ids) to the new last migration id.",
        )
    message = str(exc_info.value)
    assert re.search(rf"{re.escape(_THIS_FILE)}:\d+", message), message
    assert "'0051'" in message
    assert "'0050'" in message
    assert (
        "Update this pin (and authority-inventory.json's "
        "scope.migration_ids) to the new last migration id." in message
    )
