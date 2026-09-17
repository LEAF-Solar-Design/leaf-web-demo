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

        def execute(self, statement, params=None, **_kwargs):
            if isinstance(params, dict):
                placeholders = set(re.findall(r"%\(([a-z_][a-z0-9_]*)\)s", statement))
                assert placeholders <= set(params)
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
        "request_digest": "sha256:" + "b" * 64, "review_id": "review-1", "tenant_id": "tenant-a",
        "project_id": str(uuid.uuid4()), "source_revision": "source-1",
        "source_artifact_digest": "sha256:" + "a" * 64, "bundle_id": "com.leaf.app",
        "marketing_version": "1.0", "build_number": "1", "image_id": "ami-1",
        "image_digest": "sha256:" + "c" * 64, "host_id": "h-1",
        "instance_id": "i-1", "region": "us-east-2", "availability_zone": "us-east-2a",
        "instance_type": "mac2.metal", "minimum_allocation_hours": 24,
        "estimated_cost_usd": "28.80", "xcode_version": "26.3", "xcode_build": "17C529",
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
    # Keep the base receipt valid so each mutation reaches its intended gate.
    with pytest.raises(ios_ship.IosShipError):
        ios_ship._controller_receipt(_raw_controller_receipt(**{field: bad}))


_EC2_FIELDS = {
    "instance_id", "availability_zone", "instance_type", "minimum_allocation_hours",
    "estimated_cost_usd", "mac_instance_state", "dedicated_host_state",
}


def _v2_controller_receipt(kind="mac-mini"):
    value = _raw_controller_receipt(schema="leaf.ios-testflight-receipt.v2")
    ec2 = {name: value.pop(name) for name in _EC2_FIELDS}
    value["executor"] = {
        "kind": kind, "host": "mini.invalid" if kind == "mac-mini" else value["host_id"],
        "run_lock_released": True, "run_material_removed": True,
    }
    if kind == "ec2-mac":
        value["ec2"] = ec2
    else:
        value.update(host_id="mini.invalid", image_id=None, image_digest=None)
    return value


def _assert_receipt_error(value, code):
    with pytest.raises(ios_ship.IosShipError) as exc:
        ios_ship._controller_receipt(value)
    assert exc.value.code == code


def test_s2_row1_v1_normalizes_without_changing_ec2_values():
    """S2 row1: historical AWS proof is lifted into the common v2 shape."""
    raw = _raw_controller_receipt()
    clean = ios_ship._controller_receipt(raw)
    assert clean["schema"] == "leaf.ios-testflight-receipt.v2"
    assert clean["executor"] == {
        "kind": "ec2-mac", "host": raw["host_id"],
        "run_lock_released": True, "run_material_removed": True,
    }
    assert clean["ec2"] == {name: raw[name] for name in _EC2_FIELDS}
    assert not _EC2_FIELDS.intersection(clean)
    assert raw["schema"] == "leaf.ios-testflight-receipt.v1"


def test_s2_row2_v1_release_is_still_required():
    """S2 row2: legacy receipts do not bypass cleanup."""
    _assert_receipt_error(_raw_controller_receipt(dedicated_host_state="allocated"),
                          "terminal_proof_missing")


def test_s2_row3_mini_normalizes_without_ec2():
    """S2 row3: the synthetic mini receipt has no fictional EC2 object."""
    raw = _v2_controller_receipt()
    assert ios_ship._controller_receipt(raw) == raw
    assert "ec2" not in raw


@pytest.mark.parametrize("field", ["run_material_removed", "run_lock_released"])
@pytest.mark.parametrize("kind", ["mac-mini", "ec2-mac"])
def test_s2_row4_executor_cleanup_required(field, kind):
    """S2 row4: each executor flag is terminal proof for every kind."""
    raw = _v2_controller_receipt(kind)
    raw["executor"][field] = False
    _assert_receipt_error(raw, "terminal_proof_missing")


@pytest.mark.parametrize("claim", ["ec2", "mac_instance_state", "dedicated_host_state"])
@pytest.mark.parametrize("nested", [False, True])
def test_s2_row5_mini_ec2_claims_rejected_before_storage(monkeypatch, claim, nested):
    """S2 row5: no EC2 claim, even nested, can reach a database transaction."""
    raw = _v2_controller_receipt()
    target = raw["executor"] if nested else raw
    target[claim] = {} if claim == "ec2" else "terminated"
    def no_database():
        pytest.fail("invalid receipt reached storage")
    monkeypatch.setattr(ios_ship, "connection", no_database)
    with pytest.raises(ios_ship.IosShipError) as exc:
        ios_ship.record_provider_receipt(
            uuid.uuid4(), "tenant-a", raw["project_id"], uuid.uuid4(), "run-1", raw)
    assert exc.value.code == "invalid_provider_receipt"
    assert claim in str(exc.value)


def test_s2_row6_ec2_object_must_be_complete_and_terminal():
    """S2 row6: v2 EC2 keeps the complete legacy allocation and release proof."""
    raw = _v2_controller_receipt("ec2-mac")
    assert ios_ship._controller_receipt(raw) == raw
    absent = dict(raw)
    del absent["ec2"]
    _assert_receipt_error(absent, "invalid_provider_receipt")
    for field in _EC2_FIELDS:
        incomplete = {**raw, "ec2": dict(raw["ec2"])}
        del incomplete["ec2"][field]
        _assert_receipt_error(incomplete, "invalid_provider_receipt")
    for field in ("mac_instance_state", "dedicated_host_state"):
        nonterminal = {**raw, "ec2": {**raw["ec2"], field: "running"}}
        _assert_receipt_error(nonterminal, "terminal_proof_missing")


@pytest.mark.parametrize("field", ["image_id", "image_digest"])
def test_s2_row7_image_fields_are_kind_specific(field):
    """S2 row7: mini images are null; EC2 images retain string and digest checks."""
    mini = _v2_controller_receipt()
    assert ios_ship._controller_receipt(mini)[field] is None
    _assert_receipt_error({**mini, field: ""}, "invalid_provider_receipt")
    image_values = {"image_id": "ami-0123456789abcdef0",
                    "image_digest": "sha256:" + "c" * 64}
    _assert_receipt_error({**mini, field: image_values[field]}, "invalid_provider_receipt")
    _assert_receipt_error({**mini, **image_values}, "invalid_provider_receipt")
    ec2 = _v2_controller_receipt("ec2-mac")
    for bad in (None, ""):
        _assert_receipt_error({**ec2, field: bad}, "invalid_provider_receipt")
    _assert_receipt_error({**ec2, "image_digest": "bad"}, "invalid_provider_receipt")


@pytest.mark.parametrize("mutation", [
    "unknown_kind", "missing_executor", "non_bool", "extra", "executor_extra", "ec2_extra",
])
def test_s2_row8_v2_schema_remains_exact(mutation):
    """S2 row8: malformed executors and surplus fields fail closed."""
    raw = _v2_controller_receipt("ec2-mac" if mutation == "ec2_extra" else "mac-mini")
    if mutation == "unknown_kind":
        raw["executor"]["kind"] = "unknown"
    elif mutation == "missing_executor":
        del raw["executor"]
    elif mutation == "non_bool":
        raw["executor"]["run_lock_released"] = 1
    elif mutation == "executor_extra":
        raw["executor"]["extra"] = "unexpected"
    elif mutation == "ec2_extra":
        raw["ec2"]["extra"] = "unexpected"
    else:
        raw["extra"] = "unexpected"
    _assert_receipt_error(raw, "invalid_provider_receipt")
