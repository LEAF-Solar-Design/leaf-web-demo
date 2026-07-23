"""Guest upload cap authority tests.

Mocked tests always run. Fleet concurrency tests use DATABASE_URL and skip in
the normal database-free server test environment.
"""
from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import guest_uploads

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_memory_is_default_and_postgres_is_explicit(monkeypatch):
    monkeypatch.delenv("LEAF_GUEST_CAP_STORE", raising=False)
    assert guest_uploads._guest_cap_store() == "memory"
    monkeypatch.setenv("LEAF_GUEST_CAP_STORE", "postgres")
    assert guest_uploads._guest_cap_store() == "postgres"


def test_guest_cap_migration_matches_shared_counter_contract():
    sql = (PROJECT_ROOT / "platform" / "migrations" /
           "0015_guest_caps.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS guest_upload_counters" in sql
    for column in ("namespace", "counter_key", "value", "updated_at"):
        assert column in sql
    assert "PRIMARY KEY (namespace, counter_key)" in sql


def test_postgres_ip_key_is_versioned_keyed_hmac(monkeypatch):
    monkeypatch.setattr(guest_uploads, "_rate_day", lambda: "test-day")
    monkeypatch.setenv("LEAF_GUEST_CAP_HMAC_SECRET", "operator-secret-one")
    first = guest_uploads._postgres_counter_keys("192.0.2.20")[1]
    monkeypatch.setenv("LEAF_GUEST_CAP_HMAC_SECRET", "operator-secret-two")
    second = guest_uploads._postgres_counter_keys("192.0.2.20")[1]
    assert first.startswith("test-day:h1:")
    assert first != second
    assert "192.0.2.20" not in first


def test_postgres_ip_key_requires_operator_secret(monkeypatch):
    monkeypatch.delenv("LEAF_GUEST_CAP_HMAC_SECRET", raising=False)
    monkeypatch.delenv("LEAF_GUEST_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="HMAC_SECRET"):
        guest_uploads._postgres_counter_keys("192.0.2.21")


def test_invalid_store_mode_fails_closed(monkeypatch):
    monkeypatch.setenv("LEAF_GUEST_CAP_STORE", "typo")
    with pytest.raises(RuntimeError, match="LEAF_GUEST_CAP_STORE"):
        guest_uploads.check_and_count_guest_upload("192.0.2.1")
    with pytest.raises(RuntimeError, match="LEAF_GUEST_CAP_STORE"):
        guest_uploads.refund_guest_upload("192.0.2.1")


def test_postgres_error_does_not_fall_back_to_memory(monkeypatch):
    monkeypatch.setenv("LEAF_GUEST_CAP_STORE", "postgres")
    guest_uploads._reset_rate_state()
    monkeypatch.setattr(
        guest_uploads, "_postgres_check_and_count",
        lambda _ip: (_ for _ in ()).throw(RuntimeError("database down")),
    )
    with pytest.raises(RuntimeError, match="database down"):
        guest_uploads.check_and_count_guest_upload("192.0.2.2")
    assert guest_uploads._RATE_STATE["total"] == 0


class _FakeDatabase:
    def __init__(self):
        self.rolled_back = False

    def run_transaction(self, operation, **_kwargs):
        try:
            return operation(object())
        except Exception:
            self.rolled_back = True
            raise


class _FakeCounter:
    def __init__(self, rejected_scope):
        self.rejected_scope = rejected_scope
        self.calls = []

    def consume_in_transaction(self, _conn, *, namespace, key, limit):
        self.calls.append((namespace, key, limit))
        scope = "ip" if namespace == "guest_upload_ip" else "global"
        return SimpleNamespace(accepted=scope != self.rejected_scope)


@pytest.mark.parametrize(
    ("rejected_scope", "expected_calls"),
    [("ip", ["guest_upload_ip"]), ("global", [
        "guest_upload_ip", "guest_upload_global"])],
)
def test_cap_rejection_rolls_back_the_whole_charge(
    monkeypatch, rejected_scope, expected_calls,
):
    database = _FakeDatabase()
    counter = _FakeCounter(rejected_scope)
    monkeypatch.setattr(
        guest_uploads, "_load_platform_counters",
        lambda: (database, object),
    )
    monkeypatch.setattr(guest_uploads, "_postgres_counter", lambda: counter)
    monkeypatch.setattr(guest_uploads, "_rate_day", lambda: "test-day")
    monkeypatch.setenv("LEAF_GUEST_CAP_HMAC_SECRET", "test-secret")

    assert guest_uploads._postgres_check_and_count("192.0.2.3") == rejected_scope
    assert database.rolled_back is True
    assert [call[0] for call in counter.calls] == expected_calls
    assert "192.0.2.3" not in repr(counter.calls)


class _RecordingConnection:
    def __init__(self):
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append((" ".join(str(query).split()), dict(params or {})))
        return SimpleNamespace(fetchone=lambda: None)


class _RecordingDatabase:
    def __init__(self):
        self.connection = _RecordingConnection()

    def run_transaction(self, operation, **_kwargs):
        return operation(self.connection)


def test_refund_uses_exact_charge_day_across_utc_rollover(monkeypatch):
    database = _RecordingDatabase()
    counter = _FakeCounter(rejected_scope=None)
    monkeypatch.setattr(
        guest_uploads, "_load_platform_counters",
        lambda: (database, object),
    )
    monkeypatch.setattr(guest_uploads, "_postgres_counter", lambda: counter)
    monkeypatch.setenv("LEAF_GUEST_CAP_HMAC_SECRET", "rollover-secret")
    monkeypatch.setattr(guest_uploads, "_rate_day", lambda: "2026-07-23")

    assert guest_uploads._postgres_check_and_count("192.0.2.30") is None
    charged_receipt = guest_uploads._PG_CHARGE_RECEIPT.get()
    monkeypatch.setattr(guest_uploads, "_rate_day", lambda: "2026-07-24")
    guest_uploads._postgres_refund("192.0.2.30")

    refund_params = [
        params for query, params in database.connection.calls
        if query.startswith("UPDATE guest_upload_counters")
    ]
    assert charged_receipt is not None
    assert refund_params[0]["key"] == charged_receipt[1]
    assert refund_params[1]["key"] == "2026-07-23"
    assert all("2026-07-24" not in params["key"] for params in refund_params)


def test_refund_database_failure_keeps_slot_and_receipt(monkeypatch):
    receipt = ("2026-07-23", "2026-07-23:h1:digest")
    guest_uploads._PG_CHARGE_RECEIPT.set(receipt)

    class BrokenDatabase:
        @staticmethod
        def run_transaction(_operation, **_kwargs):
            raise RuntimeError("refund database down")

    monkeypatch.setattr(
        guest_uploads, "_load_platform_counters",
        lambda: (BrokenDatabase, object),
    )
    with pytest.raises(RuntimeError, match="refund database down"):
        guest_uploads._postgres_refund("192.0.2.31")
    assert guest_uploads._PG_CHARGE_RECEIPT.get() == receipt
    guest_uploads._PG_CHARGE_RECEIPT.set(None)


def test_counter_cleanup_is_bounded(monkeypatch):
    connection = _RecordingConnection()
    monkeypatch.setenv("LEAF_GUEST_CAP_RETENTION_DAYS", "8")
    guest_uploads._cleanup_old_postgres_counters(connection)
    query, params = connection.calls[0]
    assert "LIMIT 100" in query
    assert params["cutoff"].tzinfo is not None


def _database_modules():
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL is required for PostgreSQL concurrency tests")
    db, _counter_type = guest_uploads._load_platform_counters()
    db.apply_migration(
        PROJECT_ROOT / "platform" / "migrations" / "0015_guest_caps.sql")
    return db


def _clear_test_day(db, day):
    with db.cursor() as cur:
        cur.execute(
            "DELETE FROM guest_upload_counters "
            "WHERE counter_key = %(day)s OR counter_key LIKE %(prefix)s",
            {"day": day, "prefix": f"{day}:%"},
        )


def test_two_writers_share_per_ip_limit(monkeypatch):
    db = _database_modules()
    day = f"test-{uuid.uuid4().hex}"
    monkeypatch.setenv("LEAF_GUEST_CAP_STORE", "postgres")
    monkeypatch.setenv("LEAF_GUEST_UPLOADS_PER_IP_PER_DAY", "1")
    monkeypatch.setenv("LEAF_GUEST_UPLOADS_PER_DAY", "10")
    monkeypatch.setenv("LEAF_GUEST_CAP_HMAC_SECRET", uuid.uuid4().hex)
    monkeypatch.setattr(guest_uploads, "_rate_day", lambda: day)
    _clear_test_day(db, day)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                guest_uploads.check_and_count_guest_upload,
                ["198.51.100.8", "198.51.100.8"],
            ))
        assert results.count(None) == 1
        assert results.count("ip") == 1

    finally:
        _clear_test_day(db, day)


def test_two_writers_share_global_limit(monkeypatch):
    db = _database_modules()
    day = f"test-{uuid.uuid4().hex}"
    monkeypatch.setenv("LEAF_GUEST_CAP_STORE", "postgres")
    monkeypatch.setenv("LEAF_GUEST_UPLOADS_PER_IP_PER_DAY", "10")
    monkeypatch.setenv("LEAF_GUEST_UPLOADS_PER_DAY", "1")
    monkeypatch.setenv("LEAF_GUEST_CAP_HMAC_SECRET", uuid.uuid4().hex)
    monkeypatch.setattr(guest_uploads, "_rate_day", lambda: day)
    _clear_test_day(db, day)
    try:
        ips = ["198.51.100.9", "198.51.100.10"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                guest_uploads.check_and_count_guest_upload,
                ips,
            ))
        assert results.count(None) == 1
        assert results.count("global") == 1
        losing_ip = ips[results.index("global")]
        losing_key = guest_uploads._postgres_counter_keys(losing_ip)[1]
        with db.cursor() as cur:
            cur.execute(
                "SELECT value FROM guest_upload_counters "
                "WHERE namespace = 'guest_upload_ip' "
                "AND counter_key = %(key)s",
                {"key": losing_key},
            )
            assert cur.fetchone() is None, (
                "global rejection must roll back the losing per-IP charge")
    finally:
        _clear_test_day(db, day)
