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
    # The labels column travels as ONE json.dumps STRING: the legacy
    # insertAll API takes a JSON column as a JSON-encoded string and parses
    # it server-side; a dict fails the row with "labels is not a record"
    # (LIVE-proven on the first staging insert, 2026-08-04; this supersedes
    # review #420 round-1's dict theory, which was never live-verified).
    assert isinstance(row["labels"], str)
    labels = json.loads(row["labels"])
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
    assert json.loads(telemetry_sink._queue[-1]["labels"])["schema_version"] == "1"


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
    kept = [json.loads(r["labels"])["i"] for r in q]
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
    labels = json.loads(row["labels"])
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
    """With auth live and no credentials at all, exactly the pre-auth
    allowlist (the funnel trio, auth.completed whose failure branch never has
    a bearer, and client.exception which fires on pages that have no principal
    yet) is accepted; everything else drops silently (still 202)."""
    _enable_fake_sink(monkeypatch)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    c = _client()
    resp = _post(c, {"events": [
        {"event_name": "gate.choice", "labels": {"choice": "demo"}},
        {"event_name": "tour.started"},
        {"event_name": "auth.completed", "labels": {"ok": "false"}},
        {"event_name": "client.exception", "event_type": "exception"},
        {"event_name": "prompt.submitted"},   # NOT allowlisted pre-auth
    ]})
    assert resp.status_code == 202
    assert resp.json()["accepted"] == 4
    rows = list(telemetry_sink._queue)
    assert all(r["tenant_id"] == "anon" for r in rows)
    assert {r["event_name"] for r in rows} == {
        "gate.choice", "tour.started", "auth.completed", "client.exception"}


def test_ingest_anonymous_client_exception_lands_with_its_labels(monkeypatch):
    """A JS failure on a page with no principal — a marketing page, the
    sign-in gate, anything before a session exists — is exactly the failure
    nothing else can see: the server answered 200 and the browser died. It
    lands as event_type `exception` with SCHEMA-CONFORMING labels (this is the
    happy path; the filtering itself is the next test) and server-stamped
    anonymous identity."""
    _enable_fake_sink(monkeypatch)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    c = _client()
    resp = _post(c, {"events": [{
        "event_name": "client.exception",
        "event_type": "exception",
        "labels": {
            "source": "unhandledrejection",
            "message_class": "TypeError",
            "message_hash": "0002166136261234",
            "stack_hash": "0000884152034421",
            "route": "site",
            "ua_class": "chrome/desktop",
        },
    }]})
    assert resp.status_code == 202
    assert resp.json()["accepted"] == 1
    (row,) = list(telemetry_sink._queue)
    assert row["event_name"] == "client.exception"
    assert row["event_type"] == "exception"
    assert row["tenant_id"] == "anon"
    assert row["tenant_kind"] == "anon"
    labels = json.loads(row["labels"])
    assert labels["source"] == "unhandledrejection"
    assert labels["message_class"] == "TypeError"
    assert labels["route"] == "site"
    # Both digests are schema-conforming widths, so both survive.
    assert labels["message_hash"] == "0002166136261234"
    assert labels["stack_hash"] == "0000884152034421"
    assert labels["ingest"] == "client"


def test_ingest_anonymous_client_exception_labels_are_schema_filtered(monkeypatch):
    """The event's contract promises STRUCTURAL labels, and the browser half
    keeps that promise -- but the door is the open internet for this event.
    An anonymous POST may not smuggle free text into it: unknown keys are
    dropped, values that do not match their shape are dropped, and a class
    outside the allowlist degrades to "Other" rather than travelling."""
    _enable_fake_sink(monkeypatch)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    c = _client()
    resp = _post(c, {"events": [{
        "event_name": "client.exception",
        "event_type": "exception",
        "labels": {
            "source": "unhandledrejection",          # allowed enum
            "message_class": "AliceSmith",           # NOT a platform class
            "message_hash": "customerSecret",        # not a digest
            "stack_hash": "0000000000012345",        # a real 16-digit digest
            "route": "/app/alice/settings",          # not a scene name
            "ua_class": "alicesmith/desktop",        # shape-valid, NOT a real class
            "tour_step": "customer_secret",          # no longer in the schema
            "raw_secret": "sk-live-abcdef",          # invented key
        },
    }]})
    assert resp.status_code == 202
    assert resp.json()["accepted"] == 1
    labels = json.loads(list(telemetry_sink._queue)[0]["labels"])
    assert labels["source"] == "unhandledrejection"
    assert labels["stack_hash"] == "0000000000012345"
    # Degraded, dropped, dropped, dropped.
    assert labels["message_class"] == "Other"
    assert "message_hash" not in labels          # "customerSecret" is not a digest
    assert "route" not in labels                 # not a scene name
    assert "ua_class" not in labels              # not one of the twelve
    assert "tour_step" not in labels             # not in the schema at all
    assert "raw_secret" not in labels
    # Nothing the caller wrote as free text survives anywhere in the row.
    serialized = json.dumps(labels)
    assert "AliceSmith" not in serialized
    assert "customerSecret" not in serialized
    assert "alice" not in serialized
    assert "customer_secret" not in serialized
    assert "sk-live-abcdef" not in serialized


def test_ingest_client_exception_schema_binds_authed_callers_too(monkeypatch):
    """Authentication proves IDENTITY, not label provenance. A bearer holder
    or a self-mintable guest can POST free text exactly as a stranger can, and
    this event's contract says its labels are structural -- so the schema
    binds every caller. The cost, taken deliberately: a new label on THIS
    event needs a server release."""
    _enable_fake_sink(monkeypatch)
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    c = _client()
    resp = _post(c, {"events": [{
        "event_name": "client.exception",
        "event_type": "exception",
        "labels": {
            "message_class": "TypeError",
            "message_hash": "0000000000001234",
            "future_label": "owner@example.com",
        },
    }]}, headers={"X-Tenant-Id": "acme"})
    assert resp.status_code == 202
    labels = json.loads(list(telemetry_sink._queue)[0]["labels"])
    assert labels["message_class"] == "TypeError"
    assert labels["message_hash"] == "0000000000001234"
    assert "future_label" not in labels
    assert "owner@example.com" not in json.dumps(labels)


def test_component_stack_hash_accepts_both_client_versions(monkeypatch):
    """MIXED VERSIONS, which is the whole point of this rule.

    A browser tab opened before this release keeps running the OLD
    ErrorBoundary, which hashed the component stack with its own 32-bit
    shift-hash and sent `String(hash >>> 0)` -- 1 to 10 digits. A
    16-digit-only door accepts that row and silently STRIPS its only stack
    fingerprint, on exactly the event that reports crashes, for as long as any
    stale bundle is alive. Both widths are preserved. The widths in between,
    which no client version ever emitted, are not."""
    _enable_fake_sink(monkeypatch)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    c = _client()
    resp = _post(c, {"events": [
        # The pre-#537 client, still in the wild.
        {"event_name": "client.exception", "event_type": "exception",
         "labels": {"message_class": "TypeError", "component_stack_hash": "271828183"}},
        # This release.
        {"event_name": "client.exception", "event_type": "exception",
         "labels": {"message_class": "TypeError", "component_stack_hash": "0000000000012345"}},
        # Neither: no client emits eleven digits, so nothing is owed to it.
        {"event_name": "client.exception", "event_type": "exception",
         "labels": {"message_class": "TypeError", "component_stack_hash": "12345678901"}},
    ]})
    assert resp.status_code == 202
    assert resp.json()["accepted"] == 3
    legacy, current, bogus = (json.loads(r["labels"]) for r in telemetry_sink._queue)
    assert legacy["component_stack_hash"] == "271828183"
    assert current["component_stack_hash"] == "0000000000012345"
    assert "component_stack_hash" not in bogus
    # The row itself always lands: the label's loss was never visible.
    assert all(lb["message_class"] == "TypeError" for lb in (legacy, current, bogus))


def test_the_new_digests_do_not_get_the_legacy_width(monkeypatch):
    """The compat window is ONE label wide. `message_hash` and `stack_hash`
    ship for the first time in this release, so no stale client can emit them
    and a short decimal in either is a caller inventing one -- `5550142` is a
    phone number, which is the failure the fixed width exists to stop."""
    _enable_fake_sink(monkeypatch)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    c = _client()
    resp = _post(c, {"events": [{
        "event_name": "client.exception",
        "event_type": "exception",
        "labels": {
            "message_class": "TypeError",
            "message_hash": "5550142",
            "stack_hash": "5550142",
            "component_stack_hash": "5550142",
        },
    }]})
    assert resp.status_code == 202
    labels = json.loads(list(telemetry_sink._queue)[0]["labels"])
    assert "message_hash" not in labels
    assert "stack_hash" not in labels
    assert labels["component_stack_hash"] == "5550142"


def test_ingest_other_events_keep_the_generic_additive_contract(monkeypatch):
    """Only events that DECLARE a schema are filtered. Everything else keeps
    additive labels, so an ordinary new label still lands with no server
    release."""
    _enable_fake_sink(monkeypatch)
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    c = _client()
    resp = _post(c, {"events": [{
        "event_name": "prompt.submitted",
        "labels": {"input_kind": "typed", "brand_new_label": "kept"},
    }]}, headers={"X-Tenant-Id": "acme"})
    assert resp.status_code == 202
    labels = json.loads(list(telemetry_sink._queue)[0]["labels"])
    assert labels["brand_new_label"] == "kept"


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
    labels = json.loads(telemetry_sink._queue[-1]["labels"])
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


def test_bucket_sorted_shed_path_spares_the_most_exhausted(monkeypatch):
    """When no bucket is refillable-to-full, the sorted fallback sheds the
    least-restricted buckets and the exhausted one goes last (review #420
    round-3 nit)."""
    monkeypatch.setattr(telemetry_router, "_BUCKET_BURST", 2.0)
    assert telemetry_router._bucket_allows("ip:attacker", 2)  # now at 0 tokens
    import time as _t

    now = _t.monotonic()
    with telemetry_router._bucket_lock:
        for i in range(10_002):
            telemetry_router._buckets[f"ip:half{i}"] = [1.0, now]  # not full: pass 1 evicts none
    assert telemetry_router._bucket_allows("ip:new", 1)  # triggers sorted shed
    with telemetry_router._bucket_lock:
        assert len(telemetry_router._buckets) <= 10_001  # bounded again
        assert "ip:attacker" in telemetry_router._buckets  # exhausted survives
    assert not telemetry_router._bucket_allows("ip:attacker", 2)


def test_bucket_deny_path_also_sweeps(monkeypatch):
    """A flood of always-denied keys must not grow the dict without bound
    (review #420 round-3 warn 1)."""
    monkeypatch.setattr(telemetry_router, "_BUCKET_BURST", 2.0)
    for i in range(10_100):
        # cost above burst: every request denied, every request inserts.
        telemetry_router._bucket_allows(f"ip:deny{i}", 31)
    with telemetry_router._bucket_lock:
        assert len(telemetry_router._buckets) <= 10_001


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
    labels = json.loads(telemetry_sink._queue[-1]["labels"])
    clamped = float(labels["client_ts"])
    assert abs(_t.time() - clamped) <= telemetry_router.CLIENT_TS_CLAMP_S + 5
