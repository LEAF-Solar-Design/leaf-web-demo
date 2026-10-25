"""Resumption, tenancy and transport bounds with deterministic clocks and both stores."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest
from starlette.requests import Request

import deps
import lab_run_store as store
from routers import lab_router, lab_stream as stream
from test_lab_run_store import authority, create  # noqa: F401 -- PostgreSQL twin


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    async def sleep(self, seconds):
        self.value += seconds


@pytest.fixture
def rig(authority, monkeypatch):
    monkeypatch.setenv("LEAF_LAB_SURFACE_ENABLED", "1")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.delenv("LEAF_ENTITLEMENTS_FILE", raising=False)
    context = deps.TenantContext(authority, tier="hosted_pro", subject=authority)
    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority",
                        lambda subject: (authority, "hosted_pro"))
    app = FastAPI()
    app.include_router(lab_router.router)
    app.dependency_overrides[deps.require_tenant] = lambda: context
    create(authority)
    clock = Clock()
    stamp = datetime.now(timezone.utc)
    monkeypatch.setattr(store, "_now", lambda: stamp + timedelta(seconds=clock.value))
    yield SimpleNamespace(tenant=authority, context=context, app=app, clock=clock, stamp=stamp)
    store._run(authority, lambda sql: sql.execute(
        "DELETE FROM lab_stream_leases WHERE tenant_id=?", (authority,)), write=True)


def events(rig, items, run_id="run"):
    lease = store.claim(rig.tenant, run_id, "worker", 600, build_revision="build", color="active")
    assert store.append_events(rig.tenant, run_id, "worker", lease["fence"], items)


def value(index, block="block"):
    return {"kind": "value", "block_id": block, "payload": {"out": index}}


def collect(rig, *, cursors=None, stop=1.25, callback=None):
    async def consume():
        signal = asyncio.Event()
        stream._streams.add(signal)
        store.acquire_stream(rig.tenant, rig.tenant, "stream", 45)

        async def sleep(seconds):
            await rig.clock.sleep(seconds)
            if callback:
                await callback(signal)
            if rig.clock.value >= stop:
                signal.set()

        return [frame async for frame in stream.event_stream(
            rig.context, cursors or {"run": -1}, rig.tenant, "stream", signal,
            clock=rig.clock, sleep=sleep,
            wall_clock=lambda: rig.stamp.timestamp() + rig.clock.value + .125)]

    return asyncio.run(consume())


def frames(output, kind):
    return [json.loads(frame.split("data: ", 1)[1]) for frame in output
            if "event: " + kind + "\n" in frame]


@pytest.mark.parametrize("credential", ["token", "jwt", "ticket", "access_token"])
def test_query_credentials_refused(rig, credential):
    with TestClient(rig.app) as client:
        response = client.get(f"/api/lab/stream/runs?runs=run&{credential}=private")
    assert response.status_code == 400
    assert response.json()["detail"] == "lab_stream_query_credential"


def test_authorization_required_and_foreign_run_hidden(rig):
    with TestClient(rig.app) as client:
        assert client.get("/api/lab/stream/runs?runs=missing").status_code == 404
        create(rig.tenant + "-foreign", "foreign")
        assert client.get("/api/lab/stream/runs?runs=foreign").status_code == 404
        rig.app.dependency_overrides.clear()
        assert client.get("/api/lab/stream/runs?runs=run").status_code == 401
        assert client.get("/api/lab/stream/runs?runs=run&token=private").status_code == 401


@pytest.mark.parametrize("runs,last", [
    ("", None), ("a,a", None), (",".join(str(i) for i in range(9)), None),
    ("a", "a:bad"), ("a", "a:-2"), ("a", "b:1"), ("a", "a:1,a:2"),
])
def test_invalid_cursors(runs, last):
    with pytest.raises(HTTPException):
        stream._cursors(runs, -1, last)


def test_resume_and_multiplex(rig):
    create(rig.tenant, "other")
    events(rig, [value(1), value(2), value(3)])
    events(rig, [value("other-run")], run_id="other")
    assert stream._cursors("run,other", 1, "run:2") == {"run": 2, "other": 1}
    assert stream._cursors("run", -1, None) == {"run": -1}
    output = collect(rig, cursors={"run": 1, "other": -1})
    assert [(row["run_id"], row["seq"]) for row in frames(output, "run")] == [("run", 2), ("other", 0)]
    assert "id: run:2\n" in output[0]


def test_fetch_route_cursors_and_snapshot_path(rig, monkeypatch):
    events(rig, [value(0), value(1), value(2)])
    original = stream.event_stream

    def accelerated(tenant, cursors, principal, stream_id, signal, **kwargs):
        async def sleep(seconds):
            signal.set()
        return original(tenant, cursors, principal, stream_id, signal,
                        clock=rig.clock, sleep=sleep,
                        wall_clock=lambda: rig.stamp.timestamp() + .125,
                        lease=kwargs.get("lease"))

    monkeypatch.setattr(stream, "event_stream", accelerated)
    with TestClient(rig.app) as client:
        response = client.get("/api/lab/stream/runs?runs=run&after_seq=0",
                              headers={"Authorization": "Bearer header-only", "Last-Event-ID": "run:1"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "id: run:2\n" in response.text and "id: run:1\n" not in response.text
        assert client.get("/api/lab/runs/run").json()["run_id"] == "run"
    assert store.acquire_stream(rig.tenant, rig.tenant, "after-route", 45)


def test_disconnect_releases(rig):
    async def consume():
        signal = asyncio.Event()
        stream._streams.add(signal)
        store.acquire_stream(rig.tenant, rig.tenant, "stream", 45)

        class Disconnected:
            async def is_disconnected(self):
                return True

        output = [frame async for frame in stream.event_stream(
            rig.context, {"run": -1}, rig.tenant, "stream", signal,
            request=Disconnected(), clock=rig.clock, sleep=rig.clock.sleep)]
        assert output == [] and signal not in stream._streams

    asyncio.run(consume())
    assert store.acquire_stream(rig.tenant, rig.tenant, "stream", 45)


def test_cancelled_consumer_releases(rig):
    events(rig, [value(1)])

    async def consume():
        signal = asyncio.Event()
        stream._streams.add(signal)
        store.acquire_stream(rig.tenant, rig.tenant, "stream", 45)
        generator = stream.event_stream(rig.context, {"run": -1}, rig.tenant, "stream", signal,
                                        clock=rig.clock, sleep=rig.clock.sleep)
        assert (await anext(generator)).startswith("id: run:0")
        await generator.aclose()
        assert signal not in stream._streams

    asyncio.run(consume())
    assert store.acquire_stream(rig.tenant, rig.tenant, "stream", 45)


def test_gap_then_public_events(rig, monkeypatch):
    monkeypatch.setattr(store, "MAX_EVENTS", 6)
    events(rig, [value(index) for index in range(6)])
    output = collect(rig)
    assert frames(output, "gap") == [{"run_id": "run", "from": 0, "to": 1}]
    assert output[0].startswith("event: gap")
    assert frames(output, "run")[0]["seq"] == 2
    assert any(row["kind"] == "coalesced" for row in frames(output, "run"))


@pytest.mark.parametrize("failure, lookup_error", [
    ("binding_changed", None),
    ("binding_unavailable", HTTPException(503, "unavailable")),
    ("binding_unavailable", OSError("unavailable")),
])
def test_binding_rechecked_each_tick(rig, monkeypatch, failure, lookup_error):
    calls = []
    def resolve(subject):
        calls.append(rig.clock.value)
        if rig.clock.value < 1:
            return rig.tenant, "hosted_pro"
        if lookup_error is not None:
            raise lookup_error
        return "another-tenant", "hosted_pro"
    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority", resolve)
    output = collect(rig, stop=3)
    assert frames(output, "close") == [{"reason": failure}]
    assert calls == [0, 1]
    assert store.acquire_stream(rig.tenant, rig.tenant, "stream", 45)


@pytest.mark.parametrize("context", ["auth-off", "verified-demo", "no-subject"])
def test_contexts_keep_identity(rig, monkeypatch, context):
    if context == "auth-off":
        monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    elif context == "verified-demo":
        rig.context.verified_demo = True
    else:
        rig.context.subject = None
    def forbidden(subject):
        pytest.fail("stable identity invoked binding lookup")
    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority", forbidden)
    assert frames(collect(rig), "close") == [{"reason": "drain"}]


def test_heartbeat_health_source_age_and_lifetime(rig):
    events(rig, [{"kind": "value", "block_id": "block", "payload": {"as_of": "old-source"}}])
    output = collect(rig, stop=301)
    assert frames(output, "close") == [{"reason": "max_age"}]
    assert output.count(": hb\n\n") == 19
    assert frames(output, "health")[0] == {"lag_ms": 15125.0}
    assert frames(output, "run")[0]["payload"] == {"as_of": "old-source"}


def test_shutdown_drain_releases(rig):
    async def callback(signal):
        await stream.drain()
        assert signal.is_set()
    assert frames(collect(rig, callback=callback), "close") == [{"reason": "drain"}]
    assert not stream._streams


def test_store_cap_release_expiry_and_cross_tenant(rig):
    for index in range(4):
        assert store.acquire_stream(rig.tenant, rig.tenant, str(index), 45)
    with pytest.raises(store.TooLarge, match="lab_stream_limit"):
        store.acquire_stream(rig.tenant + "-moved", rig.tenant, "fifth", 45)
    with TestClient(rig.app) as client:
        response = client.get("/api/lab/stream/runs?runs=run")
        assert response.status_code == 409
        assert response.json()["detail"] == "lab_stream_limit"
    store.release_stream(rig.tenant, rig.tenant, "0")
    assert store.acquire_stream(rig.tenant, rig.tenant, "replacement", 45)
    assert not store.renew_stream(rig.tenant + "-foreign", rig.tenant, "replacement", 45)
    assert store.renew_stream(rig.tenant, rig.tenant, "replacement", 60)
    rig.clock.value = 61
    assert not store.renew_stream(rig.tenant, rig.tenant, "replacement", 45)
    assert store.acquire_stream(rig.tenant, rig.tenant, "expired-replacement", 45)


def test_sixty_values_coalesce_and_control_passes(rig):
    events(rig, [value(i) for i in range(60)] + [
        {"kind": "lease_takeover", "payload": {"private": "secret"}},
    ])
    output = frames(collect(rig), "run")
    values = [row for row in output if row["kind"] == "value"]
    assert len(values) == 51
    assert values[-1]["coalesced"] == 10
    assert values[-1]["payload"] == {"out": 59}
    assert [row["payload"] for row in output if row["kind"] == "lease_takeover"] == [{}]


def test_coalesced_values_precede_finished_in_sequence(rig):
    events(rig, [value(i) for i in range(60)] + [{"kind": "finished"}])
    output = collect(rig)
    rows = frames(output, "run")
    expected = list(range(50)) + [59, 60]
    assert [row["seq"] for row in rows] == expected
    assert [frame.splitlines()[0] for frame in output if frame.startswith("id: ")] == [
        f"id: run:{seq}" for seq in expected]
    assert rows[-2]["coalesced"] == 10
    assert rows[-2]["payload"] == {"out": 59}
    assert rows[-1]["kind"] == "finished"


def test_resume_after_fifty_values_does_not_skip_deferred_values(rig):
    events(rig, [value(i) for i in range(60)] + [{"kind": "finished"}])

    async def disconnect():
        signal = asyncio.Event()
        store.acquire_stream(rig.tenant, rig.tenant, "stream", 45)
        generator = stream.event_stream(
            rig.context, {"run": -1}, rig.tenant, "stream", signal,
            clock=rig.clock, sleep=rig.clock.sleep)
        try:
            return [await anext(generator) for _ in range(50)]
        finally:
            await generator.aclose()

    first = asyncio.run(disconnect())
    assert [row["seq"] for row in frames(first, "run")] == list(range(50))
    last_id = first[-1].splitlines()[0].removeprefix("id: ")
    resumed = frames(collect(rig, cursors=stream._cursors("run", -1, last_id)), "run")
    assert [row["seq"] for row in resumed] == list(range(50, 61))
    assert resumed[-2]["payload"] == {"out": 59}
    assert resumed[-1]["kind"] == "finished"


def test_pending_distinct_blocks_flush_in_order_within_allowance(rig):
    events(rig, [value(i, str(i)) for i in range(170)] + [{"kind": "finished"}])
    output = frames(collect(rig, stop=.75), "run")
    assert [row["seq"] for row in output] == list(range(50))
    for stop, expected_end in ((1.75, 100), (2.75, 150), (3.75, 171)):
        rig.clock.value = 0
        output = frames(collect(rig, stop=stop), "run")
        assert [row["seq"] for row in output] == list(range(expected_end))
        assert all(row["coalesced"] == 1 for row in output[50:min(expected_end, 170)])
    assert output[-1]["kind"] == "finished"


def test_disconnect_before_body_starts_releases_response_lease(rig, monkeypatch):
    started = []

    async def body(*args, **kwargs):
        started.append(True)
        yield "unexpected"

    monkeypatch.setattr(stream, "event_stream", body)

    async def disconnect():
        for index in range(3):
            store.acquire_stream(rig.tenant, rig.tenant, str(index), 45)
        scope = {"type": "http", "asgi": {"spec_version": "2.0"},
                 "query_string": b"runs=run", "headers": []}
        response = await stream.runs(Request(scope), runs="run", after_seq=-1, tenant=rig.context)
        signal = response.lease.signal
        with pytest.raises(store.TooLarge):
            store.acquire_stream(rig.tenant, rig.tenant, "fifth", 45)

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            assert message["type"] == "http.response.start"
            await asyncio.Event().wait()

        await response(scope, receive, send)
        assert not started
        assert signal not in stream._streams
        assert store.acquire_stream(rig.tenant, rig.tenant, "fifth", 45)

    asyncio.run(disconnect())


@pytest.mark.parametrize("failure", [False, True])
def test_response_and_generator_release_exactly_once(rig, monkeypatch, failure):
    released = []
    original = store.release_stream

    def release(*args):
        released.append(args)
        return original(*args)

    monkeypatch.setattr(store, "release_stream", release)

    async def consume():
        scope = {"type": "http", "asgi": {"spec_version": "2.4"},
                 "query_string": b"runs=run", "headers": []}
        response = await stream.runs(Request(scope), runs="run", after_seq=-1, tenant=rig.context)
        response.lease.signal.set()

        async def receive():
            await asyncio.Event().wait()

        async def send(message):
            if failure and message["type"] == "http.response.body":
                raise RuntimeError("send failed")

        try:
            if failure:
                with pytest.raises(RuntimeError, match="send failed"):
                    await response(scope, receive, send)
            else:
                await response(scope, receive, send)
        finally:
            await response.body_iterator.aclose()
        await response.lease.release()
        assert response.lease.signal not in stream._streams
        assert released == [(rig.tenant, rig.tenant, response.lease.stream_id)]

    asyncio.run(consume())


def test_oversize_value_truncated(rig):
    events(rig, [value("x" * (64 * 1024))])
    payload = frames(collect(rig), "run")[0]["payload"]
    assert payload["truncated"] is True and payload["size"] > 64 * 1024


def test_byte_budget_defers_without_losing_events(rig):
    events(rig, [value("x" * 60000, str(i)) for i in range(60)])
    early = frames(collect(rig, stop=.75), "run")
    assert sum(len(stream._frame("run", row, f'run:{row["seq"]}').encode()) for row in early) <= stream.STREAM_BYTES
    assert 0 < len(early) < 50
    rig.clock.value = 0
    assert len(frames(collect(rig, stop=2.25), "run")) == 60


def test_run_disappears_mid_stream(rig, monkeypatch):
    def missing(*args, **kwargs):
        raise store.NotFound("private")
    monkeypatch.setattr(store, "list_events", missing)
    assert frames(collect(rig), "close") == [{"reason": "run_missing", "run_id": "run"}]


def test_latency_summary_has_counts_without_payload(rig, caplog):
    events(rig, [value("payload-secret")])
    with caplog.at_level(logging.INFO, logger=stream.__name__):
        collect(rig)
    line = next(record.getMessage() for record in caplog.records
                if record.getMessage().startswith("lab_stream_closed "))
    summary = json.loads(line.split(" ", 1)[1])
    assert summary["runs"]["run"] == {"count": 1, "p50_ms": 125.0, "p95_ms": 125.0}
    assert "payload-secret" not in line and "payload" not in line
