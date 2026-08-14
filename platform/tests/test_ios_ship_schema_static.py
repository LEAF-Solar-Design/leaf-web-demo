"""Dependency-free proof that Wave D persistence is migration-backed."""
from __future__ import annotations

from contextlib import contextmanager
import re
import uuid

import pytest

from leaf_platform import db, ios_ship


TABLES = {
    "ios_ship_grants",
    "ios_ship_readiness",
    "ios_ship_revision_approvals",
    "ios_ship_executions",
    "ios_ship_receipts",
}


def test_every_ios_ship_table_is_created_by_the_shipped_migration():
    migration = (db._PKG_DIR / "migrations" / "0040_ios_ship_lane.sql").read_text(
        encoding="utf-8")
    created = set(re.findall(
        r"CREATE TABLE IF NOT EXISTS\s+([a-z_][a-z0-9_]*)", migration,
        flags=re.IGNORECASE))
    assert created == TABLES
    assert TABLES <= set(db._REQUIRED_COLUMNS)
    assert "ios_ship_receipts_immutable" in db._REQUIRED_TRIGGERS


def test_ios_ship_runtime_contains_no_schema_ddl_or_table_helper():
    source = (db._PKG_DIR / "ios_ship.py").read_text(encoding="utf-8")
    assert "_ensure_tables" not in source
    assert not re.search(r"\b(CREATE|ALTER|DROP)\s+(TABLE|INDEX|TRIGGER)\b", source,
                         flags=re.IGNORECASE)


def test_launch_admission_precedes_provider_dispatch_in_source():
    source = (db._PKG_DIR / "ios_ship.py").read_text(encoding="utf-8")
    launch = source[source.index("def launch_execution"):]
    job_insert = launch.index('INSERT INTO jobs')
    dispatch_call = launch.index('_dispatch_admitted(org, project, execution_id')
    assert job_insert < dispatch_call
    assert "pg_advisory_xact_lock" in source


def test_replay_resume_runs_only_after_admission_context_exits(monkeypatch):
    active = False
    execution_id = uuid.uuid4()
    org_id = uuid.uuid4()
    project_id = uuid.uuid4()
    approval_id = uuid.uuid4()

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, *_args, **_kwargs):
            return None

    class Connection:
        def cursor(self):
            return Cursor()

    @contextmanager
    def tracked_connection():
        nonlocal active
        active = True
        try:
            yield Connection()
        finally:
            active = False

    existing = {
        "execution_id": execution_id, "org_id": org_id, "project_id": project_id,
        "tenant_id": "tenant-a", "principal_id": str(uuid.uuid4()),
        "approval_id": approval_id, "revision": "r1", "source_revision": "83bbde1",
        "source_sha256": "a" * 64, "bundle_identifier": "com.leaf.soundbeam",
        "marketing_version": "1.2", "build_number": "19", "status": "queued",
        "failed_stage": None, "receipt_id": None, "created_at": None,
        "updated_at": None, "submission_fingerprint": "fingerprint",
    }
    monkeypatch.setattr(ios_ship, "connection", tracked_connection)
    monkeypatch.setattr(ios_ship, "_existing_execution", lambda *_args: existing)
    monkeypatch.setattr(ios_ship, "_grant_healthy", lambda *_args: None)
    monkeypatch.setattr(ios_ship, "_require_ship_principal", lambda *_args: None)
    monkeypatch.setattr(ios_ship, "_fingerprint", lambda *_args: "fingerprint")

    def resume(*_args):
        assert active is False
        return {"execution_id": str(execution_id), "status": "queued"}

    monkeypatch.setattr(ios_ship, "_resume_existing_execution", resume)
    result = ios_ship.launch_execution(
        org_id, "tenant-a", str(uuid.uuid4()), project_id, approval_id=approval_id,
            revision="r1", source_revision="83bbde1", source_sha256="a" * 64,
            bundle_identifier="com.leaf.soundbeam", marketing_version="1.2",
            build_number="19", app_color="primary", idempotency_key="same-key",
            dispatch=lambda _: {})
    assert result["execution_id"] == str(execution_id)


def test_new_provider_dispatch_runs_only_after_admission_context_exits(monkeypatch):
    active = False
    org_id, project_id, approval_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement, *_args, **_kwargs):
            self.statement = statement

        def fetchone(self):
            return {"exists": 1} if self.statement.startswith("SELECT 1 FROM projects") else None

    class Connection:
        def cursor(self):
            return Cursor()

    @contextmanager
    def tracked_connection():
        nonlocal active
        active = True
        try:
            yield Connection()
        finally:
            active = False

    approval = {
        "approval_id": str(approval_id), "revision": "r1", "source_revision": "83bbde1",
        "source_sha256": "a" * 64, "bundle_identifier": "com.leaf.soundbeam",
        "marketing_version": "1.2", "build_number": "19", "approved": True,
        "consumed_at": None, "consumed_execution_id": None,
    }
    monkeypatch.setattr(ios_ship, "connection", tracked_connection)
    monkeypatch.setattr(ios_ship, "_existing_execution", lambda *_args: None)
    monkeypatch.setattr(ios_ship, "_grant_healthy", lambda *_args: None)
    monkeypatch.setattr(ios_ship, "_require_ship_principal", lambda *_args: None)
    monkeypatch.setattr(ios_ship, "_approval_record", lambda *_args, **_kwargs: approval)

    def dispatch_admitted(*_args):
        assert active is False
        return {"execution_id": str(_args[2]), "status": "dispatched"}

    monkeypatch.setattr(ios_ship, "_dispatch_admitted", dispatch_admitted)
    result = ios_ship.launch_execution(
        org_id, "tenant-a", str(uuid.uuid4()), project_id, approval_id=approval_id,
            revision="r1", source_revision="83bbde1", source_sha256="a" * 64,
            bundle_identifier="com.leaf.soundbeam", marketing_version="1.2",
            build_number="19", app_color="primary", idempotency_key="new-key",
            dispatch=lambda _: {})
    assert result["status"] == "dispatched"


@pytest.mark.parametrize("result", [
    {"status": "succeeded", "stage": "UPLOADED"},
    {"status": "failed", "message": "provider refused the request"},
])
def test_dispatch_cannot_create_a_terminal_state_without_exact_receipt_or_stage(result):
    with pytest.raises(ios_ship.IosShipError) as exc:
        ios_ship._sanitize_dispatch_result(result)
    assert exc.value.code == "invalid_dispatch"


def test_dispatch_rejects_untrusted_provider_message_text():
    with pytest.raises(ios_ship.IosShipError) as exc:
        ios_ship._sanitize_dispatch_result({
            "status": "failed", "stage": "SIGNING_READY", "provider_run_id": "run-1",
            "message": "password is hunter2"})
    assert exc.value.code == "invalid_dispatch"


def test_production_composition_mounts_the_http_provider_once():
    source = (db._PKG_DIR.parent / "server" / "app.py").read_text(encoding="utf-8")
    assert source.count("def initialize_ios_ship_provider") == 1
    body = source[source.index("def initialize_ios_ship_provider"):]
    assert body.index("ProviderConfig.from_environment") < body.index("ios_ship.set_dispatch")
    assert "ios_ship_provider_router.set_config(config)" in body


def _raw_controller_receipt(**overrides):
    value = {
        "schema": "leaf.ios-testflight-receipt.v1", "run_id": "run-1",
        "request_digest": "b" * 64, "review_id": "review-1", "tenant_id": "tenant-a",
        "project_id": str(uuid.uuid4()), "source_revision": "source-1",
        "source_artifact_digest": "sha256:" + "a" * 64, "bundle_id": "com.leaf.app",
        "marketing_version": "1.0", "build_number": "1", "image_id": "ami-1",
        "image_digest": "sha256:" + "c" * 64, "host_id": "h-1",
        "instance_id": "i-1", "region": "us-east-2", "availability_zone": "us-east-2a",
        "instance_type": "mac2.metal", "minimum_allocation_hours": 24,
        "estimated_cost_usd": 28.8, "xcode_version": "26.3", "xcode_build": "17C529",
        "app_store_connect_app_id": "app-1", "app_store_connect_build_id": "build-1",
        "status": "VALID", "beta_group": "Internal Testers", "compliance_answered": True,
        "credentials_scrubbed": True, "mac_instance_state": "terminated",
        "dedicated_host_state": "released", "teardown_receipt_id": "teardown-1",
        "completed_at": "2026-08-13T16:20:00+00:00",
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize("field,bad", [
    ("status", "PROCESSING"), ("compliance_answered", False),
    ("credentials_scrubbed", False), ("mac_instance_state", "running"),
    ("dedicated_host_state", "allocated"), ("teardown_receipt_id", ""),
])
def test_controller_terminal_proofs_fail_closed_without_database(field, bad):
    with pytest.raises(ios_ship.IosShipError):
        ios_ship._controller_receipt(_raw_controller_receipt(**{field: bad}))
