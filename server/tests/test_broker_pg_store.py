"""Focused PostgreSQL broker admission and accounting contracts."""
from __future__ import annotations

import os
import platform as _stdlib_platform
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_stdlib_platform.python_implementation()

SERVER_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SERVER_DIR.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import broker  # noqa: E402
import broker_pg_store  # noqa: E402
import deps  # noqa: E402


class _Result:
    def __init__(self, one=None, many=None):
        self._one = one
        self._many = many or []

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._many


class _Connection:
    def __init__(self):
        self.tenants = {}
        self.admissions = {}
        self.ledger = {}
        self.slots = {}

    def execute(self, sql, params=None):
        params = params or {}
        if sql.startswith("SELECT pg_advisory_xact_lock"):
            return _Result({"pg_advisory_xact_lock": None})
        if sql.startswith("SELECT table_name, column_name"):
            columns = {
                "broker_tenants": {
                    "tenant_id", "disabled", "tier", "updated_at",
                },
                "broker_run_admissions": {
                    "event_key", "tenant_id", "request_fingerprint", "state",
                    "lease_token", "lease_expires_at", "aps_live",
                    "accounted_work", "reserved_usd", "execution_started_at",
                    "result_json", "http_status", "created_at", "updated_at",
                    "terminal_at",
                },
                "broker_usage_ledger": {
                    "event_key", "ts", "tenant_id", "tool", "engine_op",
                    "aps_endpoint", "aps_live", "engine_seconds", "usd_est",
                    "status", "inserted_at",
                },
                "broker_aps_slots": {
                    "event_key", "tenant_id", "state", "acquired_at",
                    "lease_expires_at", "released_at", "release_reason",
                },
                "broker_admission_resolution_audit": {
                    "audit_id", "event_key", "tenant_id", "resolution",
                    "operator_id", "reason", "evidence_ref", "prior_state",
                    "terminal_status", "created_at",
                },
                "drawing_store_manifests": {
                    "tenant_id", "drawing_id", "head", "latest",
                    "checkout_holder", "checkout_fence",
                    "checkout_acquired_at", "checkout_expires_at",
                    "created_at", "updated_at",
                },
                "drawing_store_versions": {
                    "tenant_id", "drawing_id", "version", "parent_version",
                    "object_key", "byte_count", "content_sha256",
                    "workitem_id", "tool", "note", "state",
                    "reservation_token", "reservation_expires_at",
                    "created_at", "ready_at",
                },
                "drawing_upload_attempts": {
                    "tenant_id", "drawing_id", "attempt", "marker", "status",
                    "retention_expires_at", "extraction_owner",
                    "extraction_fence", "extraction_expires_at",
                    "purge_owner", "purge_fence", "purge_expires_at",
                    "updated_at",
                },
            }
            return _Result(many=[
                {"table_name": table, "column_name": column}
                for table in params["tables"]
                for column in columns.get(table, set())
            ])
        if sql.startswith("SELECT conname FROM pg_constraint"):
            return _Result(many=[
                {"conname": name} for name in params["constraints"]
            ])
        if sql.startswith("SELECT tgname FROM pg_trigger"):
            return _Result(many=[
                {"tgname": name} for name in params["triggers"]
            ])
        if "aps-concurrency-slots" in sql:
            return _Result({"pg_advisory_xact_lock": None})
        if sql.startswith("SELECT tenant_id, disabled"):
            return _Result(self.tenants.get(params["tenant_id"]))
        if sql.startswith("INSERT INTO broker_tenants"):
            current = self.tenants.get(params["tenant_id"], {
                "tenant_id": params["tenant_id"], "tier": None,
            })
            current.update({"disabled": params["disabled"], "updated_at": 1.0})
            self.tenants[params["tenant_id"]] = current
            return _Result()
        if sql.startswith("SELECT tenant_id FROM broker_tenants"):
            return _Result(many=[
                {"tenant_id": tenant_id}
                for tenant_id, rec in sorted(self.tenants.items())
                if rec["disabled"]
            ])
        if sql.startswith("SELECT event_key, tenant_id, request_fingerprint"):
            rec = self.admissions.get(params["event_key"])
            return _Result(dict(rec) if rec else None)
        if sql.startswith("SELECT COALESCE(SUM(usd_est)"):
            total = sum(
                float(rec["usd_est"] or 0)
                for rec in self.ledger.values()
                if rec["tenant_id"] == params["tenant_id"]
                and rec["status"] not in {"quota_exceeded", "TENANT_DISABLED"}
            )
            return _Result({"spent": total})
        if sql.startswith("SELECT COALESCE(SUM(reserved_usd)"):
            total = sum(
                rec["reserved_usd"]
                for rec in self.admissions.values()
                if rec["tenant_id"] == params["tenant_id"]
                and (
                    rec["state"] == "executing"
                    or (rec["state"] == "leased" and rec["lease_active"])
                )
            )
            return _Result({"reserved": total})
        if sql.startswith("SELECT COUNT(*) FILTER"):
            included = [
                rec for rec in self.ledger.values()
                if rec["tenant_id"] == params["tenant_id"]
                and rec["status"] not in {"quota_exceeded", "TENANT_DISABLED"}
            ]
            total = sum(float(rec["usd_est"] or 0) for rec in included)
            return _Result({
                "total_runs": len(included), "total_usd": total,
                "today_runs": len(included), "today_usd": total,
            })
        if sql.startswith("SELECT DISTINCT tenant_id"):
            return _Result(many=[
                {"tenant_id": tenant}
                for tenant in sorted({
                    rec["tenant_id"] for rec in self.ledger.values()
                })
            ])
        if sql.startswith("SELECT COUNT(*) AS used FROM broker_run_admissions"):
            used = sum(
                1 for rec in self.admissions.values()
                if rec["tenant_id"] == params["tenant_id"]
                and rec["aps_live"]
                and (
                    rec["accounted_work"]
                    or (rec["state"] == "leased" and rec["lease_active"])
                )
            )
            return _Result({"used": used})
        if sql.startswith("SELECT COUNT(*) AS used FROM broker_usage_ledger"):
            used = sum(
                1 for rec in self.ledger.values()
                if rec["tenant_id"] == params["tenant_id"]
                and rec["aps_live"]
                and rec["status"] not in {
                    "quota_exceeded", "TENANT_DISABLED",
                    "RECONCILED_FAILED_NO_CHARGE",
                }
            )
            return _Result({"used": used})
        if sql.startswith("INSERT INTO broker_run_admissions"):
            self.admissions[params["event_key"]] = {
                "event_key": params["event_key"],
                "tenant_id": params["tenant_id"],
                "request_fingerprint": params["request_fingerprint"],
                "state": "leased",
                "lease_token": params["token"],
                "lease_expires_at": 1,
                "lease_active": True,
                "aps_live": params["aps_live"],
                "accounted_work": False,
                "reserved_usd": params["reserved_usd"],
                "execution_started_at": None,
                "result_json": None,
                "http_status": None,
            }
            return _Result()
        if sql.startswith("UPDATE broker_run_admissions SET lease_token"):
            rec = self.admissions[params["event_key"]]
            rec.update({
                "lease_token": params["token"],
                "lease_active": True,
                "aps_live": params["aps_live"],
                "reserved_usd": params["reserved_usd"],
            })
            return _Result({"event_key": params["event_key"]})
        if sql.startswith(
                "UPDATE broker_run_admissions SET lease_expires_at = NOW()"):
            rec = self.admissions.get(params["event_key"])
            if rec and rec["lease_token"] == params["lease_token"]:
                rec["lease_active"] = False
            return _Result()
        if sql.startswith("UPDATE broker_run_admissions SET state = 'executing'"):
            rec = self.admissions.get(params["event_key"])
            if (
                rec is None or rec["tenant_id"] != params["tenant_id"]
                or rec["lease_token"] != params["lease_token"]
                or rec["state"] != "leased" or not rec["lease_active"]
            ):
                return _Result()
            rec.update({
                "state": "executing", "lease_active": False,
                "lease_expires_at": None, "execution_started_at": 1,
                "accounted_work": params["aps_live"],
            })
            return _Result({"event_key": params["event_key"]})
        if sql.startswith("SELECT COUNT(*) AS held FROM broker_aps_slots"):
            return _Result({
                "held": sum(1 for slot in self.slots.values()
                            if slot["state"] == "held"),
            })
        if sql.startswith("INSERT INTO broker_aps_slots"):
            self.slots[params["event_key"]] = {
                "event_key": params["event_key"],
                "tenant_id": params["tenant_id"],
                "state": "held",
                "lease_expires_at": 1,
                "slot_stuck": False,
            }
            return _Result()
        if sql.startswith("UPDATE broker_aps_slots SET state = 'released'"):
            slot = self.slots.get(params["event_key"])
            if slot and slot["state"] == "held":
                slot["state"] = "released"
            return _Result()
        if sql.startswith("SELECT tenant_id, state, lease_token"):
            rec = self.admissions.get(params["event_key"])
            return _Result(dict(rec) if rec else None)
        if sql.startswith("INSERT INTO broker_usage_ledger"):
            self.ledger.setdefault(params["event_key"], dict(params))
            return _Result()
        if sql.startswith("UPDATE broker_run_admissions SET state = 'terminal'"):
            rec = self.admissions.get(params["event_key"])
            owned = rec is not None and rec["tenant_id"] == params["tenant_id"]
            if "lease_token" in params:
                owned = (
                    owned and rec["lease_token"] == params["lease_token"]
                    and rec["state"] in {"leased", "executing"}
                )
            else:
                owned = owned and rec["state"] == "executing"
            if not owned:
                return _Result()
            rec.update({
                "state": "terminal", "lease_active": False,
                "lease_expires_at": None, "reserved_usd": 0,
                "result_json": params["result_json"],
                "http_status": params["http_status"],
            })
            if params.get("resolution") == "confirmed_failed_no_charge":
                rec["accounted_work"] = False
            return _Result({"event_key": params["event_key"]})
        if sql.startswith("SELECT COALESCE(SUM"):
            raise AssertionError(f"unexpected aggregate SQL: {sql}")
        if sql.startswith("SELECT COUNT"):
            return _Result({"used": 0})
        if sql.startswith(
                "SELECT admission.tenant_id, admission.event_key, "
                "admission.request_fingerprint, admission.aps_live"):
            rows = [
                dict(rec) for rec in self.admissions.values()
                if rec["state"] == "executing"
            ][:params["limit"]]
            for rec in rows:
                rec["age_seconds"] = 60.0
                slot = self.slots.get(rec["event_key"])
                rec["slot_lease_expires_at"] = (
                    slot["lease_expires_at"] if slot else None)
                rec["slot_stuck"] = bool(slot and slot["slot_stuck"])
            return _Result(many=rows)
        if sql.startswith(
                "SELECT admission.tenant_id, admission.event_key, "
                "admission.request_fingerprint, admission.state"):
            rec = self.admissions.get(params["event_key"])
            if rec is None or rec["tenant_id"] != params["tenant_id"]:
                return _Result()
            out = dict(rec)
            out["age_seconds"] = 60.0
            slot = self.slots.get(rec["event_key"])
            out["slot_state"] = slot["state"] if slot else None
            out["slot_lease_expires_at"] = (
                slot["lease_expires_at"] if slot else None)
            out["slot_stuck"] = bool(slot and slot["slot_stuck"])
            return _Result(out)
        if sql.startswith("SELECT audit_id, resolution"):
            rows = [
                audit for audit in getattr(self, "audits", [])
                if audit["event_key"] == params["event_key"]
                and audit["tenant_id"] == params["tenant_id"]
            ]
            return _Result(many=rows)
        if sql.startswith("SELECT tenant_id, state FROM broker_run_admissions"):
            rec = self.admissions.get(params["event_key"])
            return _Result(dict(rec) if rec else None)
        if sql.startswith("INSERT INTO broker_admission_resolution_audit"):
            if not hasattr(self, "audits"):
                self.audits = []
            self.audits.append(dict(params, prior_state="executing"))
            return _Result()
        raise AssertionError(f"unexpected SQL: {sql}")


class _ConnectionContext:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_args):
        return False


class _Pool:
    def __init__(self):
        self.conn = _Connection()

    def connection(self):
        return _ConnectionContext(self.conn)


class _Db:
    def __init__(self):
        self.pool = _Pool()
        self.lock = threading.RLock()

    def get_pool(self):
        return self.pool

    def run_transaction(self, operation, **_kwargs):
        with self.lock:
            return operation(self.pool.conn)

    def assert_schema_current(self):
        return {"ok": True}

    def migration_manifest(self):
        return [
            {"name": "0014_broker.sql", "sha256": "test"},
            {"name": "0016_drawing_upload_authority.sql", "sha256": "test"},
        ]


def _entry(tenant_id="tenant-a", usd_est=0.02):
    return {
        "ts": 1753222000.0, "tenant_id": tenant_id, "tool": "count",
        "engine_op": "count",
        "aps_endpoint": "https://developer.api.autodesk.com",
        "aps_live": True, "engine_seconds": 1.0, "usd_est": usd_est,
        "status": "ok",
    }


def _admit(store, key, tenant="tenant-a", **overrides):
    options = {
        "aps_live": True, "estimated_usd": 0.02,
        "spend_cap": 1.0, "daily_limit": 20,
        "request_fingerprint": "a" * 64,
    }
    options.update(overrides)
    return store.admit_run(key, tenant, **options)


def test_postgres_kill_state_is_visible_to_each_store_instance():
    db = _Db()
    writer = broker_pg_store.PostgresBrokerStore(db)
    reader = broker_pg_store.PostgresBrokerStore(db)
    assert reader.tenant("tenant-a") is None
    reader.validate_schema()
    writer.set_tenant_disabled("tenant-a", True)
    assert reader.tenant("tenant-a")["disabled"] is True
    assert reader.disabled_tenant_ids() == ["tenant-a"]
    writer.set_tenant_disabled("tenant-a", False)
    assert reader.tenant("tenant-a")["disabled"] is False


def test_schema_readiness_rejects_missing_critical_constraint(monkeypatch):
    db = _Db()
    original = db.pool.conn.execute

    def incomplete(sql, params=None):
        if sql.startswith("SELECT conname FROM pg_constraint"):
            return _Result(many=[])
        return original(sql, params)

    monkeypatch.setattr(db.pool.conn, "execute", incomplete)
    with pytest.raises(RuntimeError, match="constraints"):
        broker_pg_store.PostgresBrokerStore(db).validate_schema()


def test_drawing_schema_readiness_checks_0016_catalog():
    db = _Db()
    store = broker_pg_store.PostgresBrokerStore(db)
    store.validate_drawing_schema()


def test_broker_startup_rejects_drawing_selector_typo(monkeypatch):
    monkeypatch.setenv("LEAF_BROKER_STORE", "legacy")
    monkeypatch.setenv("LEAF_DRAWING_STORE", "typo")
    with pytest.raises(RuntimeError, match="LEAF_DRAWING_STORE"):
        with TestClient(broker.app):
            pytest.fail("broker health became reachable with invalid selector")


def test_broker_startup_rejects_incomplete_0016_before_health(monkeypatch):
    class _Incomplete:
        def validate_drawing_schema(self):
            raise RuntimeError(
                "PostgreSQL drawing schema is incomplete: missing 0016")

    monkeypatch.setenv("LEAF_BROKER_STORE", "legacy")
    monkeypatch.setenv("LEAF_DRAWING_STORE", "postgres")
    monkeypatch.setattr(broker, "_pg_store", _Incomplete())
    with pytest.raises(RuntimeError, match="missing 0016"):
        with TestClient(broker.app):
            pytest.fail("broker health became reachable with incomplete 0016")


def test_broker_health_waits_for_drawing_schema_readiness(monkeypatch):
    calls = []

    class _Ready:
        def validate_drawing_schema(self):
            calls.append("drawing-ready")

    monkeypatch.setenv("LEAF_BROKER_STORE", "legacy")
    monkeypatch.setenv("LEAF_DRAWING_STORE", "postgres")
    monkeypatch.setattr(broker, "_pg_store", _Ready())
    with TestClient(broker.app) as client:
        assert client.get("/broker/health").status_code == 200
    assert calls == ["drawing-ready"]


def test_distinct_keys_reserve_spend_atomically():
    store = broker_pg_store.PostgresBrokerStore(_Db())
    assert _admit(store, "run-1", spend_cap=0.03)["status"] == "acquired"
    denied = _admit(store, "run-2", spend_cap=0.03)
    assert denied["status"] == "spend_quota"
    assert denied["reserved"] == 0.02
    assert denied["lease_token"]


def test_concurrent_distinct_keys_cannot_both_cross_spend_cap():
    store = broker_pg_store.PostgresBrokerStore(_Db())
    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(
            lambda key: _admit(store, key, spend_cap=0.03),
            ("run-1", "run-2"),
        ))
    assert sorted(decision["status"] for decision in decisions) == [
        "acquired", "spend_quota",
    ]


def test_distinct_keys_reserve_daily_slot_atomically():
    store = broker_pg_store.PostgresBrokerStore(_Db())
    assert _admit(store, "run-1", daily_limit=1)["status"] == "acquired"
    denied = _admit(store, "run-2", daily_limit=1)
    assert denied["status"] == "daily_quota"
    assert denied["used"] == 1
    assert denied["limit"] == 1
    assert denied["lease_token"]


def test_expired_unstarted_lease_reclaims_but_execution_never_does():
    db = _Db()
    first = broker_pg_store.PostgresBrokerStore(db)
    second = broker_pg_store.PostgresBrokerStore(db)
    original = _admit(first, "run-1")
    db.pool.conn.admissions["run-1"]["lease_active"] = False
    reclaimed = _admit(second, "run-1")
    assert reclaimed["status"] == "acquired"
    assert reclaimed["reclaimed"] is True
    second.mark_execution_started("run-1", "tenant-a", reclaimed["lease_token"])
    assert _admit(first, "run-1")["status"] == "executing"


def test_fleet_live_slot_ceiling_survives_broker_death_until_resolution():
    db = _Db()
    first = broker_pg_store.PostgresBrokerStore(db)
    second = broker_pg_store.PostgresBrokerStore(db)
    one = _admit(first, "live-1")
    two = _admit(second, "live-2")
    assert first.mark_execution_started(
        "live-1", "tenant-a", one["lease_token"],
        aps_live=True, max_concurrency=1, slot_lease_seconds=60,
    ) is True
    assert second.mark_execution_started(
        "live-2", "tenant-a", two["lease_token"],
        aps_live=True, max_concurrency=1, slot_lease_seconds=60,
    ) is False
    # Expiry is an operator alarm, never permission to risk a duplicate paid run.
    db.pool.conn.slots["live-1"]["slot_stuck"] = True
    db.pool.conn.slots["live-1"]["lease_expires_at"] = 0
    two_retry = _admit(second, "live-2")
    assert two_retry["status"] == "acquired"
    assert second.mark_execution_started(
        "live-2", "tenant-a", two_retry["lease_token"],
        aps_live=True, max_concurrency=1, slot_lease_seconds=60,
    ) is False
    listed = first.list_executing()
    assert listed[0]["event_key"] == "live-1"
    assert listed[0]["slot_stuck"] is True
    first.complete_run(
        "live-1", "tenant-a", one["lease_token"],
        _entry(), {"ok": True, "error": None, "degraded_mode": False}, 200,
    )
    assert db.pool.conn.slots["live-1"]["state"] == "released"
    two_final = _admit(second, "live-2")
    assert two_final["status"] == "acquired"
    assert second.mark_execution_started(
        "live-2", "tenant-a", two_final["lease_token"],
        aps_live=True, max_concurrency=1, slot_lease_seconds=60,
    ) is True


def test_mock_execution_never_acquires_aps_slot():
    db = _Db()
    store = broker_pg_store.PostgresBrokerStore(db)
    for key in ("mock-1", "mock-2"):
        admitted = _admit(store, key, aps_live=False)
        assert store.mark_execution_started(
            key, "tenant-a", admitted["lease_token"],
            aps_live=False, max_concurrency=1,
        ) is True
    assert db.pool.conn.slots == {}


def test_terminal_numbers_cannot_reduce_recorded_spend():
    db = _Db()
    store = broker_pg_store.PostgresBrokerStore(db)
    first = _admit(store, "positive")
    store.complete_run(
        "positive", "tenant-a", first["lease_token"],
        _entry(usd_est=0.02),
        {"ok": True, "error": None, "degraded_mode": False},
        200,
    )
    assert store.spent_usd("tenant-a") == 0.02

    for key, invalid in (
        ("negative", -0.01),
        ("nan", float("nan")),
        ("infinity", float("inf")),
    ):
        admitted = _admit(store, key)
        with pytest.raises(ValueError, match="finite and nonnegative"):
            store.complete_run(
                key, "tenant-a", admitted["lease_token"],
                _entry(usd_est=invalid),
                {"ok": True, "error": None, "degraded_mode": False},
                200,
            )
        assert store.spent_usd("tenant-a") == 0.02


def test_postgres_extract_replays_after_first_response_is_lost(
        monkeypatch, tmp_path):
    db = _Db()
    store = broker_pg_store.PostgresBrokerStore(db)
    calls = []

    class _Da:
        def extract(self, path):
            calls.append(path)
            return {"polylines": [{"layer": "A"}]}

    drawing = tmp_path / "drawing.dwg"
    drawing.write_bytes(b"DWG")
    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    monkeypatch.setattr(broker, "_pg_store", store)
    monkeypatch.setattr(broker, "_get_da", lambda: _Da())
    monkeypatch.setattr(broker, "_resolve_live_dwg", lambda _dwg: drawing)
    request = broker.BrokerExtractRequest(
        tenant_id="tenant-a",
        dwg="drawing",
        ledger_event_key="stable-extract",
    )

    first = broker.broker_extract(request)
    assert first.status_code == 200
    # Simulate losing the first HTTP response and retrying the durable key.
    replay = broker.broker_extract(request)
    assert replay.status_code == 200
    assert replay.body == first.body
    assert calls == [str(drawing)]
    assert db.pool.conn.admissions["stable-extract"]["state"] == "terminal"
    assert db.pool.conn.slots["stable-extract"]["state"] == "released"
    assert store.daily_live_run_count("tenant-a") == 1


def test_postgres_extract_requires_durable_key_and_respects_global_slot(
        monkeypatch, tmp_path):
    db = _Db()
    store = broker_pg_store.PostgresBrokerStore(db)
    drawing = tmp_path / "drawing.dwg"
    drawing.write_bytes(b"DWG")
    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    monkeypatch.setenv("APS_MAX_CONCURRENCY", "1")
    monkeypatch.setattr(broker, "_pg_store", store)
    monkeypatch.setattr(broker, "_resolve_live_dwg", lambda _dwg: drawing)
    monkeypatch.setattr(broker, "_get_da", lambda: type(
        "_Da", (), {"extract": lambda _self, _path: {"polylines": []}})())

    missing = broker.broker_extract(broker.BrokerExtractRequest(
        tenant_id="tenant-a", dwg="drawing"))
    assert missing.status_code == 400

    held = _admit(store, "held-live")
    assert store.mark_execution_started(
        "held-live", "tenant-a", held["lease_token"],
        aps_live=True, max_concurrency=1, slot_lease_seconds=60,
    )
    blocked = broker.broker_extract(broker.BrokerExtractRequest(
        tenant_id="tenant-a", dwg="drawing",
        ledger_event_key="blocked-extract",
    ))
    assert blocked.status_code == 502
    assert db.pool.conn.admissions["blocked-extract"]["state"] == "leased"
    assert db.pool.conn.admissions["blocked-extract"]["lease_active"] is False
    assert "blocked-extract" not in db.pool.conn.slots


def test_terminal_result_and_ledger_replay_once():
    db = _Db()
    first = broker_pg_store.PostgresBrokerStore(db)
    second = broker_pg_store.PostgresBrokerStore(db)
    admitted = _admit(first, "run-1")
    first.mark_execution_started("run-1", "tenant-a", admitted["lease_token"])
    result = {"ok": True, "error": None, "degraded_mode": False}
    first.complete_run(
        "run-1", "tenant-a", admitted["lease_token"],
        _entry(), result, 200,
    )
    replay = _admit(second, "run-1")
    assert replay == {"status": "replay", "result": result, "http_status": 200}
    assert len(db.pool.conn.ledger) == 1


def test_postgres_usage_aggregate_matches_frozen_shape_and_rounding():
    db = _Db()
    store = broker_pg_store.PostgresBrokerStore(db)
    db.pool.conn.ledger = {
        "one": _entry("tenant-a", 0.1234567),
        "two": dict(_entry("tenant-a", 0.0000006), status="ok"),
        "denied": dict(_entry("tenant-a", None), status="quota_exceeded"),
        "other": _entry("tenant-b", 9.0),
    }
    assert store.aggregate_usage("tenant-a") == {
        "today": {"runs": 2, "usd_est": 0.123457},
        "total": {"runs": 2, "usd_est": 0.123457},
    }
    assert store.usage_tenant_ids() == ["tenant-a", "tenant-b"]


def test_cross_tenant_collision_is_a_result_not_a_write():
    db = _Db()
    store = broker_pg_store.PostgresBrokerStore(db)
    assert _admit(store, "shared", "tenant-a")["status"] == "acquired"
    assert _admit(store, "shared", "tenant-b") == {"status": "collision"}
    assert db.pool.conn.ledger == {}


def test_same_tenant_key_cannot_replay_or_reclaim_different_input():
    store = broker_pg_store.PostgresBrokerStore(_Db())
    assert _admit(store, "shared")["status"] == "acquired"
    changed = _admit(store, "shared", request_fingerprint="b" * 64)
    assert changed == {"status": "mismatch"}


def test_store_uses_tenant_transaction_lock_and_irreversible_marker():
    source = Path(broker_pg_store.__file__).read_text(encoding="utf-8")
    assert "pg_advisory_xact_lock(hashtextextended" in source
    assert "hashtextextended('event:' || %(event_key)s" in source
    assert "isolation=\"serializable\"" in source
    assert "state = 'executing'" in source
    assert "lease_expires_at = NULL, execution_started_at = NOW()" in source


def test_broker_postgres_mode_reads_kill_and_tier_at_call_time(monkeypatch):
    db = _Db()
    store = broker_pg_store.PostgresBrokerStore(db)
    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    monkeypatch.setattr(broker, "_pg_store", store)
    assert broker.tenant_disabled("tenant-a") is False
    store.set_tenant_disabled("tenant-a", True)
    assert broker.tenant_disabled("tenant-a") is True
    db.pool.conn.tenants["tenant-a"]["tier"] = "hosted_pro"
    assert broker._provisioned_tier("tenant-a") == "hosted_pro"


def test_backedge_tier_uses_same_postgres_authority(monkeypatch):
    db = _Db()
    store = broker_pg_store.PostgresBrokerStore(db)
    db.pool.conn.tenants["tenant-a"] = {
        "tenant_id": "tenant-a", "disabled": False,
        "tier": "hosted_starter", "updated_at": 1.0,
    }
    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    monkeypatch.setattr(broker_pg_store, "get_store", lambda: store)
    assert deps.backedge_tier("tenant-a") == "hosted_starter"
    assert deps.backedge_tier("unknown") is None


def test_legacy_remains_default(monkeypatch, tmp_path):
    monkeypatch.delenv("LEAF_BROKER_STORE", raising=False)
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    broker._ledger_append(_entry(), "ignored-in-legacy")
    assert len(broker.LEDGER_PATH.read_text(encoding="utf-8").splitlines()) == 1


def test_postgres_mode_requires_stable_key_before_execution(monkeypatch):
    store = broker_pg_store.PostgresBrokerStore(_Db())
    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    monkeypatch.setattr(broker, "_pg_store", store)
    monkeypatch.setattr(
        broker, "_execute",
        lambda *_args, **_kwargs: pytest.fail("missing key reached execution"),
    )
    response = broker.broker_run(broker.BrokerRunRequest(
        tenant_id="tenant-a", tool={"name": "count", "engine_op": "count"},
    ))
    assert response.status_code == 400
    assert store._db.pool.conn.admissions == {}
    assert store._db.pool.conn.ledger == {}


def test_broker_terminal_replay_and_cross_tenant_collision_do_not_reexecute(
        monkeypatch):
    db = _Db()
    store = broker_pg_store.PostgresBrokerStore(db)
    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    monkeypatch.delenv("LEAF_TENANT_CAP_USD", raising=False)
    monkeypatch.setattr(broker, "_pg_store", store)
    calls = []

    def execute(req, _tool, _engine_op, _t0, _entry, **kwargs):
        calls.append(req.tenant_id)
        broker._start_admitted_execution(req, kwargs["admission"])
        return {
            "ok": True, "tool": "count", "version": "1.0.0",
            "result": {"count": 1}, "overlay": None, "timing_ms": 1,
            "cost": None, "error": None, "degraded_mode": False,
        }, 200

    monkeypatch.setattr(broker, "_execute", execute)
    request = broker.BrokerRunRequest(
        tenant_id="tenant-a",
        tool={"name": "count", "engine_op": "count"},
        ledger_event_key="stable-run",
    )
    assert broker.broker_run(request).status_code == 200
    assert broker.broker_run(request).status_code == 200
    collision = broker.broker_run(broker.BrokerRunRequest(
        tenant_id="tenant-b",
        tool={"name": "count", "engine_op": "count"},
        ledger_event_key="stable-run",
    ))
    assert collision.status_code == 400
    assert calls == ["tenant-a"]
    assert len(db.pool.conn.ledger) == 1
    assert db.pool.conn.ledger["stable-run"]["tenant_id"] == "tenant-a"


def test_broker_quota_denial_is_ledgered_once_and_replayed(monkeypatch):
    db = _Db()
    store = broker_pg_store.PostgresBrokerStore(db)
    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    monkeypatch.setenv("LEAF_TENANT_CAP_USD", "0")
    monkeypatch.setattr(broker, "_pg_store", store)
    monkeypatch.setattr(
        broker, "_execute",
        lambda *_args, **_kwargs: pytest.fail("over-cap request reached execution"),
    )
    request = broker.BrokerRunRequest(
        tenant_id="tenant-a",
        tool={"name": "count", "engine_op": "count"},
        ledger_event_key="over-cap",
    )
    first = broker.broker_run(request)
    second = broker.broker_run(request)
    assert first.status_code == second.status_code == 402
    assert len(db.pool.conn.ledger) == 1
    assert db.pool.conn.ledger["over-cap"]["status"] == "quota_exceeded"
    assert db.pool.conn.admissions["over-cap"]["state"] == "terminal"


def _executing_store(key="stuck-run"):
    db = _Db()
    store = broker_pg_store.PostgresBrokerStore(db)
    admitted = _admit(store, key)
    store.mark_execution_started(key, "tenant-a", admitted["lease_token"])
    return db, store


def test_operator_resolution_is_atomic_audited_and_never_reexecutes():
    db, store = _executing_store()
    listed = store.list_executing()
    assert listed[0]["event_key"] == "stuck-run"
    assert listed[0]["request_fingerprint"] == "a" * 64
    before = store.admission_status("stuck-run", "tenant-a")
    assert before["state"] == "executing"
    assert before["reserved_usd"] == 0.02

    entry = _entry(usd_est=None)
    entry["status"] = "RECONCILED_FAILED_NO_CHARGE"
    result = {
        "ok": False, "error": {
            "error_code": "WORKITEM_FAILED",
            "message": "APS confirms no accepted work", "retryable": False,
        }, "degraded_mode": False,
    }
    outcome = store.reconcile_executing(
        "stuck-run", "tenant-a",
        resolution="confirmed_failed_no_charge",
        operator_id="ops@example.test",
        reason="APS work item search returned no accepted work",
        evidence_ref="cw://evidence/no-workitem-123",
        entry=entry, result=result, http_status=502,
    )
    assert outcome["state"] == "terminal"
    assert db.pool.conn.ledger["stuck-run"]["usd_est"] is None
    after = store.admission_status("stuck-run", "tenant-a")
    assert after["state"] == "terminal"
    assert after["accounted_work"] is False
    assert store.daily_live_run_count("tenant-a") == 0
    assert after["resolution_audit"][0]["evidence_ref"] == (
        "cw://evidence/no-workitem-123")
    with pytest.raises(RuntimeError, match="not 'executing'"):
        store.reconcile_executing(
            "stuck-run", "tenant-a",
            resolution="confirmed_failed_no_charge",
            operator_id="ops@example.test",
            reason="attempted duplicate operator resolution",
            evidence_ref="cw://evidence/duplicate",
            entry=entry, result=result, http_status=502,
        )


def test_reconciliation_endpoint_requires_both_secrets_and_exact_confirmation(
        monkeypatch):
    db, store = _executing_store("verified-run")
    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    monkeypatch.setenv("LEAF_BROKER_SECRET", "broker-secret")
    monkeypatch.setenv("LEAF_BROKER_RECONCILE_SECRET", "reconcile-secret")
    monkeypatch.setattr(broker, "_pg_store", store)
    client = TestClient(broker.app)
    base_headers = {"X-Broker-Secret": "broker-secret"}
    url = "/broker/admin/admissions/verified-run/resolve"
    body = {
        "tenant_id": "tenant-a",
        "resolution": "verified_terminal",
        "operator_id": "ops@example.test",
        "reason": "APS terminal receipt independently verified by operator",
        "evidence_ref": "aps://workitems/workitem-123",
        "confirmation": "wrong",
        "result": {"ok": True, "error": None, "degraded_mode": False},
        "http_status": 200,
        "ledger_entry": _entry(),
    }
    assert client.get(
        "/broker/admin/admissions/executing", headers=base_headers).status_code == 401
    headers = {
        **base_headers, "X-Broker-Reconcile-Secret": "reconcile-secret",
    }
    assert client.post(url, headers=headers, json=body).status_code == 400
    assert db.pool.conn.admissions["verified-run"]["state"] == "executing"

    body["confirmation"] = (
        "RESOLVE tenant-a verified-run verified_terminal")
    # A verified terminal envelope must carry cost evidence, even when null.
    assert client.post(url, headers=headers, json=body).status_code == 400
    body["result"]["cost"] = {
        "engine_seconds": 1.0,
        "usd_est": 0.02,
    }
    body["ledger_entry"]["usd_est"] = -0.01
    assert client.post(url, headers=headers, json=body).status_code == 400
    body["ledger_entry"]["usd_est"] = 0.02
    body["ledger_entry"]["engine_seconds"] = -0.01
    assert client.post(url, headers=headers, json=body).status_code == 400
    body["ledger_entry"]["engine_seconds"] = 0.1
    # Ledger and result cost cannot disagree or omit one component.
    assert client.post(url, headers=headers, json=body).status_code == 400
    body["result"]["cost"]["engine_seconds"] = 0.1
    saved_usd = body["result"]["cost"].pop("usd_est")
    assert client.post(url, headers=headers, json=body).status_code == 400
    body["result"]["cost"]["usd_est"] = saved_usd + 0.01
    assert client.post(url, headers=headers, json=body).status_code == 400
    body["result"]["cost"]["usd_est"] = saved_usd
    body["result"]["cost"] = None
    assert client.post(url, headers=headers, json=body).status_code == 400
    body["result"]["cost"] = {
        "engine_seconds": 0.1,
        "usd_est": 0.02,
    }
    body["ledger_entry"]["status"] = "quota_exceeded"
    assert client.post(url, headers=headers, json=body).status_code == 400
    body["ledger_entry"]["status"] = "ok"
    body["confirmation"] = (
        "RESOLVE tenant-a verified-run verified_terminal")
    response = client.post(url, headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert db.pool.conn.admissions["verified-run"]["state"] == "terminal"
    status = client.get(
        "/broker/admin/admissions/verified-run",
        params={"tenant_id": "tenant-a"},
        headers=headers,
    )
    assert status.status_code == 200
    audit = status.json()["admission"]["resolution_audit"][0]
    assert audit["operator_id"] == "ops@example.test"
    assert audit["evidence_ref"] == "aps://workitems/workitem-123"


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL is not configured",
)
def test_postgres_migration_two_writer_admission_and_replay():
    db = broker_pg_store._load_db()
    # Startup readiness is intentionally global, so integration tests apply the
    # same full one-shot chain used by staging before validating broker schema.
    db.apply_migration()
    first = broker_pg_store.PostgresBrokerStore(db)
    second = broker_pg_store.PostgresBrokerStore(db)
    first.validate_schema()
    first.validate_drawing_schema()
    tenant_id = f"broker-test-{uuid.uuid4()}"
    event_key = f"broker-event-{uuid.uuid4()}"
    first.set_tenant_disabled(tenant_id, True)
    assert second.tenant(tenant_id)["disabled"] is True
    admitted = _admit(first, event_key, tenant_id)
    assert admitted["status"] == "acquired"
    assert _admit(second, event_key, tenant_id)["status"] == "leased"
    first.mark_execution_started(event_key, tenant_id, admitted["lease_token"])
    first.complete_run(
        event_key, tenant_id, admitted["lease_token"],
        _entry(tenant_id), {"ok": True, "error": None, "degraded_mode": False}, 200,
    )
    assert _admit(second, event_key, tenant_id)["status"] == "replay"
    aggregate = second.aggregate_usage(tenant_id)
    assert aggregate["total"] == {"runs": 1, "usd_est": 0.02}
    assert tenant_id in second.usage_tenant_ids()

    from psycopg.errors import CheckViolation

    for suffix, invalid in (
        ("negative", -0.01),
        ("nan", float("nan")),
        ("infinity", float("inf")),
    ):
        invalid_key = f"{event_key}-{suffix}"
        _admit(first, invalid_key, tenant_id)
        with pytest.raises(CheckViolation):
            with db.get_pool().connection() as conn:
                conn.execute(
                    "INSERT INTO broker_usage_ledger "
                    "(event_key, ts, tenant_id, tool, engine_op, aps_endpoint, "
                    "aps_live, engine_seconds, usd_est, status) VALUES "
                    "(%(event_key)s, 1, %(tenant_id)s, 'test', 'test', "
                    "'https://example.test', TRUE, 1, %(usd_est)s, 'ok')",
                    {
                        "event_key": invalid_key,
                        "tenant_id": tenant_id,
                        "usd_est": invalid,
                    },
                )
        assert second.spent_usd(tenant_id) == 0.02

    slot_one, slot_two = f"{event_key}-slot-1", f"{event_key}-slot-2"
    admitted_one = _admit(first, slot_one, tenant_id)
    admitted_two = _admit(second, slot_two, tenant_id)
    assert first.mark_execution_started(
        slot_one, tenant_id, admitted_one["lease_token"],
        aps_live=True, max_concurrency=1, slot_lease_seconds=60,
    ) is True
    # Simulate the first broker dying after irreversible APS submission. The
    # deadline becomes an operator alarm, never an automatic capacity release.
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE broker_aps_slots SET lease_expires_at = NOW() - INTERVAL '1 second' "
            "WHERE event_key = %(event_key)s",
            {"event_key": slot_one},
        )
    assert second.mark_execution_started(
        slot_two, tenant_id, admitted_two["lease_token"],
        aps_live=True, max_concurrency=1, slot_lease_seconds=60,
    ) is False
    stuck = {
        row["event_key"]: row for row in second.list_executing()
    }
    assert stuck[slot_one]["slot_stuck"] is True
    first.complete_run(
        slot_one, tenant_id, admitted_one["lease_token"],
        _entry(tenant_id), {"ok": True, "error": None, "degraded_mode": False}, 200,
    )
    reclaimed_two = _admit(second, slot_two, tenant_id)
    assert reclaimed_two["status"] == "acquired"
    assert second.mark_execution_started(
        slot_two, tenant_id, reclaimed_two["lease_token"],
        aps_live=True, max_concurrency=1, slot_lease_seconds=60,
    ) is True
    second.complete_run(
        slot_two, tenant_id, reclaimed_two["lease_token"],
        _entry(tenant_id), {"ok": True, "error": None, "degraded_mode": False}, 200,
    )
