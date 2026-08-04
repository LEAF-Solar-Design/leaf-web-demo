"""Contract tests for the P2 product-event layer: telemetry_sink + the
/api/telemetry ingest door.

The sink is tested WITHOUT the BigQuery SDK (the default test environment):
its honest disabled state, never-raise contract, and queue/label mechanics
via monkeypatched internals. The router is tested for the properties that
matter: always-202, identity server-stamping, the pre-auth allowlist, and
caps. No test talks to GCP.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import telemetry_sink
from routers import telemetry as telemetry_router


@pytest.fixture(autouse=True)
def _reset_sink(monkeypatch):
    # Each test starts with fresh module state, mutated under the sink's own
    # locks so a lingering flusher thread from another test cannot race the
    # reset.
    with telemetry_sink._wake:
        telemetry_sink._queue.clear()
        for k in telemetry_sink._stats:
            telemetry_sink._stats[k] = 0
    telemetry_sink._created_tables.clear()
    telemetry_sink._noted.clear()
    telemetry_sink._sdk_checked = None
    with telemetry_router._bucket_lock:
        telemetry_router._buckets.clear()
    yield


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(telemetry_router.router)
    return TestClient(app, raise_server_exceptions=True)


def _enable_fake_sink(monkeypatch):
    """Make the sink 'enabled' without the SDK: bypass disabled_reason and the
    flusher (events stay in the queue for inspection)."""
    monkeypatch.setattr(telemetry_sink, "disabled_reason", lambda: None)
    monkeypatch.setattr(telemetry_sink, "_ensure_flusher", lambda: None)


# --------------------------------------------------------------------------- #
# sink
# --------------------------------------------------------------------------- #

def test_sink_disabled_without_sdk_or_creds_and_never_raises():
    # Default test env: no SDK/creds -> honest disabled reason, emit -> False.
    assert telemetry_sink.disabled_reason() is not None
    ok = telemetry_sink.emit(
        "job.terminal", tenant_id="t", tenant_kind="account", session_id="s")
    assert ok is False
    assert telemetry_sink.stats()["enqueued"] == 0


def test_sink_kill_switch_wins(monkeypatch):
    monkeypatch.setenv("LEAF_TELEMETRY_DISABLED", "1")
    assert "kill switch" in (telemetry_sink.disabled_reason() or "")


def test_sink_enqueues_row_with_stringified_labels(monkeypatch):
    _enable_fake_sink(monkeypatch)
    monkeypatch.setenv("LEAF_METRICS_ENVIRONMENT", "staging")
    ok = telemetry_sink.emit(
        "job.terminal",
        tenant_id="acme",
        tenant_kind="account",
        session_id="server",
        labels={"job_id": "j1", "attempts": 2, "usd_est": 0.01, "none_dropped": None},
    )
    assert ok is True
    row = telemetry_sink._queue[-1]
    assert row["event_name"] == "job.terminal"
    assert row["tenant_id"] == "acme"
    assert row["environment"] == "staging"
    labels = row["labels"]
    # The labels column is a DICT (a JSON OBJECT on the wire): a serialized
    # string would break every JSON_VALUE(labels.x) query downstream
    # (review #420 round-1 blocker 1).
    assert isinstance(labels, dict)
    # ALL values strings; None dropped; schema_version stamped by the server
    # even if a caller supplied one.
    assert labels["attempts"] == "2"
    assert labels["usd_est"] == "0.01"
    assert labels["schema_version"] == "1"
    assert "none_dropped" not in labels


def test_sink_schema_version_is_server_authority(monkeypatch):
    _enable_fake_sink(monkeypatch)
    telemetry_sink.emit(
        "a.b", tenant_id="t", tenant_kind="account", session_id="s",
        labels={"schema_version": "999"})
    assert telemetry_sink._queue[-1]["labels"]["schema_version"] == "1"


def test_ensure_table_failure_is_not_cached_as_created(monkeypatch):
    """A transient create failure must RAISE into the batch-drop handler and
    leave the day uncached so the next batch retries (review #420 round-1
    blocker 4)."""
    class _FailingClient:
        project = "p"

        def create_table(self, table, exists_ok=False):
            raise RuntimeError("transient permission blip")

        def insert_rows_json(self, *a, **kw):  # pragma: no cover - unreached
            return []

    monkeypatch.setattr(telemetry_sink, "_get_client", lambda: _FailingClient())
    # _ensure_table imports the SDK for schema objects; fake just enough.
    import types as _types
    import sys as _sys

    class _SchemaField:
        def __init__(self, *a, **kw):
            pass

    class _Table:
        def __init__(self, *a, **kw):
            pass

    fake_bq = _types.SimpleNamespace(SchemaField=_SchemaField, Table=_Table)
    fake_cloud = _types.SimpleNamespace(bigquery=fake_bq)
    monkeypatch.setitem(_sys.modules, "google", _types.SimpleNamespace(cloud=fake_cloud))
    monkeypatch.setitem(_sys.modules, "google.cloud", fake_cloud)
    monkeypatch.setitem(_sys.modules, "google.cloud.bigquery", fake_bq)

    telemetry_sink._flush_batch([{"event_name": "a.b"}])
    assert telemetry_sink._created_tables == set()
    assert telemetry_sink.stats()["dropped_flush"] == 1


def test_sink_rejects_unknown_event_type(monkeypatch):
    _enable_fake_sink(monkeypatch)
    ok = telemetry_sink.emit(
        "x.y", event_type="nonsense",
        tenant_id="t", tenant_kind="account", session_id="s")
    assert ok is False


def test_sink_drops_oldest_on_overflow(monkeypatch):
    _enable_fake_sink(monkeypatch)
    monkeypatch.setattr(telemetry_sink, "_QUEUE_MAX", 3)
    # deque maxlen is fixed at import; emulate with a fresh bounded deque.
    from collections import deque

    monkeypatch.setattr(telemetry_sink, "_queue", deque(maxlen=3))
    for i in range(5):
        telemetry_sink.emit(
            f"a.b{i}" if False else "a.b",  # same name; label carries i
            tenant_id="t", tenant_kind="account", session_id="s",
            labels={"i": i},
        )
    q = list(telemetry_sink._queue)
    assert len(q) == 3
    kept = [r["labels"]["i"] for r in q]
    assert kept == ["2", "3", "4"]  # oldest dropped, newest kept
    assert telemetry_sink.stats()["dropped_overflow"] == 2


# --------------------------------------------------------------------------- #
# ingest router
# --------------------------------------------------------------------------- #

def _post(client, body, headers=None):
    return client.post("/api/telemetry", json=body, headers=headers or {})


def test_ingest_always_202_even_for_garbage(monkeypatch):
    _enable_fake_sink(monkeypatch)
    c = _client()
    for payload in ({}, {"events": "nope"}, {"events": [{"event_name": "BAD NAME"}]}):
        resp = _post(c, payload)
        assert resp.status_code == 202
        assert resp.json() == {"accepted": 0}


def test_ingest_oversize_body_dropped_as_202(monkeypatch):
    _enable_fake_sink(monkeypatch)
    c = _client()
    big = {"events": [{"event_name": "a.b", "labels": {"x": "y" * 40000}}]}
    resp = _post(c, big)
    assert resp.status_code == 202
    assert resp.json() == {"accepted": 0}


def test_ingest_identity_is_server_stamped_and_reserved_labels_stripped(monkeypatch):
    """Identity comes ONLY from the resolved principal, and reserved
    envelope/identity keys smuggled as labels are DROPPED entirely
    (review #420 round-1 blocker 3)."""
    _enable_fake_sink(monkeypatch)
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    c = _client()
    resp = _post(
        c,
        {"session_id": "browser-1",
         "events": [{"event_name": "prompt.submitted",
                     "labels": {"tenant_id": "victim", "user_email": "x@y.z",
                                "schema_version": "999", "environment": "prod",
                                "input_kind": "typed"}}]},
        headers={"X-Tenant-Id": "acme"},
    )
    assert resp.status_code == 202
    assert resp.json()["accepted"] == 1
    row = telemetry_sink._queue[-1]
    assert row["tenant_id"] == "acme"          # from the resolved principal
    assert row["session_id"] == "browser-1"
    labels = row["labels"]
    assert labels["input_kind"] == "typed"
    for smuggled in ("tenant_id", "user_email", "environment"):
        assert smuggled not in labels
    assert labels["schema_version"] == "1"     # server authority, not "999"


def test_ingest_malformed_raw_json_is_202_zero(monkeypatch):
    _enable_fake_sink(monkeypatch)
    c = _client()
    resp = c.post("/api/telemetry", content=b"\x00not json",
                  headers={"Content-Type": "application/json"})
    assert resp.status_code == 202
    assert resp.json() == {"accepted": 0}


def test_ingest_bucket_costs_events_not_requests(monkeypatch):
    """One anonymous request with N allowlisted events spends N tokens
    (review #420 round-1 warn 5)."""
    _enable_fake_sink(monkeypatch)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setattr(telemetry_router, "_BUCKET_BURST", 5.0)
    c = _client()
    resp = _post(c, {"events": [{"event_name": "tour.started"}] * 4})
    assert resp.json()["accepted"] == 4
    # 1 token left; a 2-event request must drop entirely.
    resp = _post(c, {"events": [{"event_name": "tour.started"}] * 2})
    assert resp.json()["accepted"] == 0


def test_ingest_caps_events_per_body(monkeypatch):
    _enable_fake_sink(monkeypatch)
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    c = _client()
    events = [{"event_name": "a.b"} for _ in range(80)]
    resp = _post(c, {"events": events}, headers={"X-Tenant-Id": "acme"})
    assert resp.status_code == 202
    assert resp.json()["accepted"] == telemetry_router.MAX_EVENTS


def test_ingest_anonymous_allowlist_only(monkeypatch):
    """With auth live and no credentials at all, exactly the pre-auth trio is
    accepted; everything else drops silently (still 202)."""
    _enable_fake_sink(monkeypatch)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    c = _client()
    resp = _post(c, {"events": [
        {"event_name": "gate.choice", "labels": {"choice": "demo"}},
        {"event_name": "tour.started"},
        {"event_name": "prompt.submitted"},   # NOT allowlisted pre-auth
    ]})
    assert resp.status_code == 202
    assert resp.json()["accepted"] == 2
    rows = list(telemetry_sink._queue)
    assert all(r["tenant_id"] == "anon" for r in rows)
    assert {r["event_name"] for r in rows} == {"gate.choice", "tour.started"}


def test_ingest_anonymous_bucket_exhaustion_drops_silently(monkeypatch):
    _enable_fake_sink(monkeypatch)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setattr(telemetry_router, "_BUCKET_BURST", 2.0)
    c = _client()
    accepted = 0
    for _ in range(5):
        resp = _post(c, {"events": [{"event_name": "tour.started"}]})
        assert resp.status_code == 202
        accepted += resp.json()["accepted"]
    assert accepted == 2  # burst of 2, then the bucket drops silently


def test_ingest_nested_values_are_bounded_on_the_wire(monkeypatch):
    """Objects/arrays are serialized THEN capped: no client value may exceed
    MAX_LABEL_VALUE_LEN on the wire (review #420 round-2 blocker 1)."""
    _enable_fake_sink(monkeypatch)
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    c = _client()
    nested = {"deep": ["x" * 300, {"more": "y" * 300}]}
    resp = _post(
        c,
        {"events": [{"event_name": "a.b", "labels": {"blob": nested, "n": 12345}}]},
        headers={"X-Tenant-Id": "acme"},
    )
    assert resp.json()["accepted"] == 1
    labels = telemetry_sink._queue[-1]["labels"]
    assert len(labels["blob"]) <= telemetry_router.MAX_LABEL_VALUE_LEN
    assert labels["n"] == "12345"


def test_bucket_sweep_never_resets_an_exhausted_bucket(monkeypatch):
    """At the dict cap, only would-be-full buckets are evicted; a spent
    bucket survives the sweep still spent (review #420 round-2 warn 2)."""
    monkeypatch.setattr(telemetry_router, "_BUCKET_BURST", 2.0)
    # Exhaust one attacker bucket.
    assert telemetry_router._bucket_allows("ip:attacker", 2)
    assert not telemetry_router._bucket_allows("ip:attacker", 1)
    # Force the dict over the cap with idle full buckets.
    import time as _t

    now = _t.monotonic()
    with telemetry_router._bucket_lock:
        for i in range(10_001):
            telemetry_router._buckets[f"ip:idle{i}"] = [2.0, now]
    # A new caller triggers the sweep; the attacker bucket must survive spent.
    assert telemetry_router._bucket_allows("ip:new", 1)
    assert not telemetry_router._bucket_allows("ip:attacker", 1)


def test_ingest_client_ts_clamped_into_labels(monkeypatch):
    _enable_fake_sink(monkeypatch)
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    import time as _t

    c = _client()
    ancient = _t.time() - 10 * 24 * 3600
    resp = _post(
        c,
        {"events": [{"event_name": "a.b", "client_ts": ancient}]},
        headers={"X-Tenant-Id": "acme"},
    )
    assert resp.json()["accepted"] == 1
    labels = telemetry_sink._queue[-1]["labels"]
    clamped = float(labels["client_ts"])
    assert abs(_t.time() - clamped) <= telemetry_router.CLIENT_TS_CLAMP_S + 5
