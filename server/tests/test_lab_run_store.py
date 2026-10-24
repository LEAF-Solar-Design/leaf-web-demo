"""The same lease, tenant, event and retention proofs on both authorities."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import json
import os
import threading
import uuid

import pytest

import lab_registry
import lab_run_store as store


@pytest.fixture(params=["sqlite", "postgres"])
def authority(request, monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "test")
    monkeypatch.setenv("LEAF_PLATFORM_POSTGRES_REQUIRED", "0")
    monkeypatch.setenv("LEAF_LAB_RUN_STORE", request.param)
    monkeypatch.setenv("LEAF_LAB_DB", str(tmp_path / "runs.db"))
    tenant = "lab-runs-test-" + uuid.uuid4().hex
    if request.param == "postgres":
        if not os.environ.get("DATABASE_URL"):
            pytest.skip("platform DB unreachable: DATABASE_URL unset")
        # A configured database must prove the suite: never turn a broken DSN,
        # migration or schema into a skip when PostgreSQL was supplied.
        monkeypatch.setenv("LEAF_LAB_RUN_STORE", "postgres_required")
        db = store.platform_link.platform_db()
        db.apply_migration()
        db.assert_schema_current()
    yield tenant
    if request.param == "postgres":
        try:
            store._run(tenant, lambda sql: sql.execute(
                "DELETE FROM lab_runs WHERE tenant_id LIKE ?", (tenant + "%",)), write=True)
            store._run("lab-worker-control", lambda sql: sql.execute(
                "DELETE FROM lab_worker_status WHERE worker_id=?", (tenant,)), write=True)
        finally:
            db.reset_pool()


def create(tenant, run_id="run", doc_id="doc"):
    return store.create(tenant, doc_id, run_id, 1, "a" * 64, "ops:1", "b" * 64, "adapter-1", "build_1")


def event(index, block="block"):
    return {"kind": "progress", "block_id": block, "payload": {"index": index}}


def claim(tenant, run_id, owner, ttl_s):
    return store.claim(tenant, run_id, owner, ttl_s, build_revision="worker-build", color="active")


def all_events(tenant, run_id):
    result = []
    cursor = -1
    while True:
        batch = store.list_events(tenant, run_id, after_seq=cursor, limit=1000)
        result.extend(batch)
        if len(batch) < 1000:
            return result
        cursor = batch[-1]["seq"]


def test_create_pins_and_rejects_bad_pins(authority):
    row = create(authority)
    expected = hashlib.sha256(json.dumps(sorted(lab_registry.REGISTRY), separators=(",", ":")).encode()).hexdigest()
    assert row["registry_digest"] == expected == store.registry_digest()
    assert row["graph_revision"] == 1 and row["graph_sha256"] == "a" * 64
    assert row["input_digest"] == "b" * 64
    assert (row["ops_revision"], row["adapter_revision"], row["build_revision"]) == ("ops:1", "adapter-1", "build_1")
    assert row["state"] == "queued" and row["next_seq"] == row["event_count"] == row["lease_fence"] == 0
    for field in ("graph_sha256", "ops_revision", "input_digest", "adapter_revision", "build_revision"):
        for bad in ("", "x" * 129, "space here", "newline\n", None):
            pins = dict(graph_revision=1, graph_sha256="a", ops_revision="ops", input_digest="in",
                        adapter_revision="adapter", build_revision="build")
            pins[field] = bad
            with pytest.raises(store.InvalidGraph):
                store.create(authority, "doc", "invalid", **pins)
    with pytest.raises(store.InvalidGraph):
        store.create(authority, "doc", "invalid", 1, "a", "ops", "in", "adapter", "build", registry_digest="bad")
    for revision in (True, 0, -1, "1"):
        with pytest.raises(store.InvalidGraph):
            store.create(authority, "doc", "invalid", revision, "a", "ops", "in", "adapter", "build")
    assert store.get(authority, "run") == row
    with pytest.raises(store.NotFound):
        store.get(authority, "invalid")


def test_two_connections_claim_exactly_once(authority):
    create(authority)
    barrier = threading.Barrier(2)

    def competing_claim(owner):
        barrier.wait(timeout=10)
        return claim(authority, "run", owner, 3600)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(competing_claim, ["worker-1", "worker-2"]))
    assert sum(result is False for result in results) == 1
    lease = next(result for result in results if result is not False)
    assert lease["fence"] == 1
    assert store.get(authority, "run")["lease_owner"] == lease["owner"]


def test_stale_fence_cannot_write_anything(authority, monkeypatch):
    create(authority)
    old = claim(authority, "run", "worker", 10)
    later = store._now() + timedelta(seconds=20)
    monkeypatch.setattr(store, "_now", lambda: later)
    new = claim(authority, "run", "worker", 60)
    assert new["fence"] > old["fence"]
    before = store.get(authority, "run")
    assert store.append_events(authority, "run", "worker", old["fence"], [event(0)]) is False
    assert store.heartbeat(authority, "run", "worker", old["fence"], 120) is False
    assert store.finish(authority, "run", "worker", old["fence"]) is False
    assert store.get(authority, "run") == before
    assert [row["kind"] for row in all_events(authority, "run")] == ["lease_takeover"]


def test_heartbeat_extends_only_holder(authority, monkeypatch):
    create(authority)
    lease = claim(authority, "run", "worker", 60)
    before = store.get(authority, "run")
    assert store.heartbeat(authority, "run", "other", lease["fence"], 120) is False
    assert store.get(authority, "run") == before
    later = store._now() + timedelta(seconds=10)
    monkeypatch.setattr(store, "_now", lambda: later)
    assert store.heartbeat(authority, "run", "worker", lease["fence"], 120) is True
    row = store.get(authority, "run")
    assert store._date(row["lease_expires_at"]) == later + timedelta(seconds=120)
    assert row["lease_fence"] == lease["fence"]


def test_expired_lease_refuses_writes_and_reclaims_higher(authority, monkeypatch):
    create(authority)
    lease = claim(authority, "run", "worker", 1)
    later = store._now() + timedelta(seconds=2)
    monkeypatch.setattr(store, "_now", lambda: later)
    assert store.heartbeat(authority, "run", "worker", lease["fence"], 30) is False
    assert store.append_events(authority, "run", "worker", lease["fence"], [event(0)]) is False
    assert store.finish(authority, "run", "worker", lease["fence"]) is False
    assert claim(authority, "run", "replacement", 60)["fence"] == lease["fence"] + 1


def test_two_connections_allocate_gap_free_events(authority):
    create(authority)
    lease = claim(authority, "run", "worker", 3600)
    barrier = threading.Barrier(2)

    def append(writer):
        barrier.wait(timeout=10)
        for batch in range(10):
            assert store.append_events(authority, "run", "worker", lease["fence"],
                                       [event(writer * 100 + batch * 10 + i) for i in range(10)]) is True

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(append, [0, 1]))
    events = all_events(authority, "run")
    assert [row["seq"] for row in events] == list(range(200))
    assert sorted(row["payload"]["index"] for row in events) == list(range(200))
    row = store.get(authority, "run")
    assert row["next_seq"] == row["event_count"] == 200


def test_cap_coalesces_latest_per_block_in_oldest_half(authority):
    create(authority)
    lease = claim(authority, "run", "worker", 3600)
    assert store.append_events(authority, "run", "worker", lease["fence"],
                               [event(i, "block-" + str(i % 10)) for i in range(10000)])
    events = all_events(authority, "run")
    assert [row["seq"] for row in events] == list(range(4990, 10001))
    assert [row["payload"]["index"] for row in events[:10]] == list(range(4990, 5000))
    assert events[-1]["kind"] == "coalesced" and events[-1]["payload"] == {"count": 4990}
    row = store.get(authority, "run")
    assert row["event_count"] == len(events) == 5011
    assert row["next_seq"] == 10001
    assert store.append_events(authority, "run", "worker", lease["fence"], [event(10001)])
    assert all_events(authority, "run")[-1]["seq"] == 10001


def test_uncoalescible_cap_rolls_back_batch(authority):
    create(authority)
    lease = claim(authority, "run", "worker", 3600)
    with pytest.raises(store.TooLarge):
        store.append_events(authority, "run", "worker", lease["fence"],
                            [event(i, "block-" + str(i)) for i in range(10001)])
    assert all_events(authority, "run") == []
    assert store.get(authority, "run")["next_seq"] == 0


def test_sweep_bounds_per_tenant_doc_and_event_age(authority, monkeypatch):
    now = store._now()
    old = now - timedelta(days=8)
    for tenant in (authority, authority + "-other"):
        for i in range(52):
            created = old + timedelta(seconds=i)
            monkeypatch.setattr(store, "_now", lambda: created)
            create(tenant, "run-" + str(i))
            if i < 51:
                lease = claim(tenant, "run-" + str(i), "worker", 3600)
                assert store.finish(tenant, "run-" + str(i), "worker", lease["fence"])
        create(tenant, "separate", "other-doc")
        lease = claim(tenant, "run-51", "worker", 3600)
        assert store.append_events(tenant, "run-51", "worker", lease["fence"], [event(0)])
        monkeypatch.setattr(store, "_now", lambda: now)
        lease = claim(tenant, "run-51", "worker", 3600)
        assert store.append_events(tenant, "run-51", "worker", lease["fence"], [event(1)])
        # The seven-day boundary itself survives (only strictly older expires).
        store._run(tenant, lambda sql: sql.execute(
            "UPDATE lab_run_events SET created_at=? WHERE tenant_id=? AND seq=2",
            ((now - timedelta(days=7)).isoformat(), tenant)), write=True)
    store.sweep(now)
    for tenant in (authority, authority + "-other"):
        runs = store._run(tenant, lambda sql: [dict(row) for row in sql.execute(
            "SELECT * FROM lab_runs WHERE tenant_id=?", (tenant,)).fetchall()])
        assert len(runs) == 51
        assert {row["run_id"] for row in runs} == {"separate"} | {"run-" + str(i) for i in range(2, 52)}
        assert [row["seq"] for row in all_events(tenant, "run-51")] == [1, 2]
        assert store.get(tenant, "run-51")["event_count"] == 2
        assert store.get(tenant, "run-51")["next_seq"] == 3


def test_tenant_boundary_for_every_operation(authority):
    create(authority)
    lease = claim(authority, "run", "worker", 3600)
    before = store.get(authority, "run")
    foreign = authority + "-other"
    for read in (store.get, store.list_events, store.cancel_requested):
        with pytest.raises(store.NotFound):
            read(foreign, "run")
    assert claim(foreign, "run", "worker", 60) is False
    assert store.heartbeat(foreign, "run", "worker", lease["fence"], 60) is False
    assert store.append_events(foreign, "run", "worker", lease["fence"], [event(0)]) is False
    assert store.finish(foreign, "run", "worker", lease["fence"]) is False
    with pytest.raises(store.NotFound):
        store.request_cancel(foreign, "run", "actor")
    assert store.get(authority, "run") == before
    create(foreign)
    assert claim(foreign, "run", "worker", 60)["fence"] == 1


def test_finished_run_cannot_reclaim_or_mutate_pins(authority):
    original = create(authority)
    lease = claim(authority, "run", "worker", 60)
    assert store.finish(authority, "run", "other", lease["fence"]) is False
    assert store.finish(authority, "run", "worker", lease["fence"])
    row = store.get(authority, "run")
    assert row["state"] == "finished" and row["finished_at"] is not None
    for key in ("graph_revision", "graph_sha256", "registry_digest", "ops_revision",
                "input_digest", "adapter_revision", "build_revision"):
        assert row[key] == original[key]
    assert claim(authority, "run", "worker", 60) is False
    assert store.append_events(authority, "run", "worker", lease["fence"], [event(0)]) is False


def test_failed_insert_rolls_back_sequence_allocation(authority, monkeypatch):
    create(authority)
    lease = claim(authority, "run", "worker", 60)
    insert = store._insert_event

    def fail_second(sql, tenant, run_id, seq, *args):
        if seq == 1:
            raise RuntimeError("injected event failure")
        return insert(sql, tenant, run_id, seq, *args)

    monkeypatch.setattr(store, "_insert_event", fail_second)
    with pytest.raises(RuntimeError, match="injected event failure"):
        store.append_events(authority, "run", "worker", lease["fence"], [event(0), event(1)])
    assert store.get(authority, "run")["next_seq"] == 0
    assert all_events(authority, "run") == []
    monkeypatch.setattr(store, "_insert_event", insert)
    assert store.append_events(authority, "run", "worker", lease["fence"], [event(0), event(1)])
    assert [row["seq"] for row in all_events(authority, "run")] == [0, 1]


@pytest.fixture
def local(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "test")
    monkeypatch.setenv("LEAF_PLATFORM_POSTGRES_REQUIRED", "0")
    monkeypatch.setenv("LEAF_LAB_RUN_STORE", "sqlite")
    path = tmp_path / "local.db"
    monkeypatch.setenv("LEAF_LAB_DB", str(path))
    return path


def test_queue_admission_cap_is_tenant_scoped(authority):
    for index in range(store.MAX_QUEUED_PER_TENANT):
        create(authority, "run-" + str(index))
    with pytest.raises(store.TooLarge, match="lab_run_queue_full"):
        create(authority, "overflow")
    create(authority + "-other")
    assert store.request_cancel(authority, "run-0", "actor")
    assert create(authority, "replacement")["state"] == "queued"


def test_concurrent_admission_cannot_exceed_queue_cap(authority):
    for index in range(store.MAX_QUEUED_PER_TENANT - 1):
        create(authority, "run-" + str(index))
    barrier = threading.Barrier(2)

    def submit(run_id):
        barrier.wait(timeout=10)
        try:
            create(authority, run_id)
            return True
        except store.TooLarge as exc:
            assert str(exc) == "lab_run_queue_full"
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, ["last-a", "last-b"])) == [False, True]


def test_claimable_round_robin_revisions_and_active_cap(authority, monkeypatch):
    now = store._now()
    monkeypatch.setattr(store, "_now", lambda: now)
    tenants = [authority, authority + "-other", authority + "-third"]
    # Equal timestamps and reverse insertion prove the run_id/tenant_id ties.
    for tenant in reversed(tenants):
        for index in reversed(range(2)):
            create(tenant, "run-" + str(index))
    unsupported = create(authority, "unsupported")
    store._run(authority, lambda sql: sql.execute(
        "UPDATE lab_runs SET ops_revision='future' WHERE tenant_id=? AND run_id=?",
        (authority, unsupported["run_id"])), write=True)

    def queued(limit=10000):
        return [row for row in store.claimable(limit, ops_revisions={"ops:1"})
                if row["tenant_id"] in tenants]

    before = queued()
    assert [(row["tenant_id"], row["run_id"]) for row in before] == [
        (tenant, "run-" + str(index)) for index in range(2) for tenant in tenants]
    assert len(store.claimable(2, ops_revisions={"ops:1"})) <= 2
    assert store.claimable(10, ops_revisions=set()) == []
    assert store.claimable(0, ops_revisions={"ops:1"}) == []
    assert store.get(authority, "run-0")["state"] == "queued"
    for index in range(4):
        run_id = "active-" + str(index)
        create(authority, run_id)
        assert claim(authority, run_id, "worker", 60)
    assert all(row["tenant_id"] != authority for row in queued())
    assert claim(authority, "run-0", "worker", 60) is False
    now += timedelta(seconds=61)
    assert any(row["tenant_id"] == authority for row in queued())
    assert claim(authority, "run-0", "worker", 60)


def test_revision_claim_takeover_and_committed_cursor(authority, monkeypatch):
    create(authority)
    old = store.claim(authority, "run", "old", 10, build_revision="build:old", color="idle")
    assert old["takeover"] is False and old["committed_seq"] == -1
    row = store.get(authority, "run")
    assert (row["claimed_build_revision"], row["claimed_color"]) == ("build:old", "idle")
    assert store.append_events(authority, "run", "old", old["fence"], [event(0), event(1)])
    later = store._now() + timedelta(seconds=11)
    monkeypatch.setattr(store, "_now", lambda: later)
    lease = store.claim(authority, "run", "new", 60, build_revision="build:new", color="active")
    assert lease["takeover"] is True and lease["committed_seq"] == 2
    events = all_events(authority, "run")
    assert [row["kind"] for row in events] == ["progress", "progress", "lease_takeover"]
    assert events[-1]["payload"] == {"previous_fence": old["fence"], "previous_owner": "old"}
    assert claim(authority, "run", "third", 60) is False
    assert store.append_events(authority, "run", "old", old["fence"], [event(2)]) is False
    assert all_events(authority, "run") == events
    assert store.append_events(authority, "run", "new", lease["fence"], [event(3)])
    assert [row["seq"] for row in store.list_events(authority, "run", after_seq=lease["committed_seq"])] == [3]
    row = store.get(authority, "run")
    assert (row["claimed_build_revision"], row["claimed_color"]) == ("build:new", "active")
    assert row["build_revision"] == "build_1"


def test_claim_requires_valid_build_and_color(authority):
    create(authority)
    with pytest.raises(TypeError):
        store.claim(authority, "run", "worker", 60)
    for build, color in (("space here", "active"), (None, "active"), ("build", "blue"), ("build", None)):
        with pytest.raises(store.InvalidGraph):
            store.claim(authority, "run", "worker", 60, build_revision=build, color=color)
    assert store.get(authority, "run")["state"] == "queued"


def test_takeover_event_failure_rolls_back_the_claim(authority, monkeypatch):
    create(authority)
    old = claim(authority, "run", "old", 1)
    assert store.append_events(authority, "run", "old", old["fence"], [event(0)])
    later = store._now() + timedelta(seconds=2)
    monkeypatch.setattr(store, "_now", lambda: later)
    before = store.get(authority, "run")
    insert = store._insert_event

    def fail_takeover(*args):
        raise RuntimeError("injected takeover failure")

    monkeypatch.setattr(store, "_insert_event", fail_takeover)
    with pytest.raises(RuntimeError, match="injected takeover failure"):
        claim(authority, "run", "new", 60)
    assert store.get(authority, "run") == before
    assert [row["kind"] for row in all_events(authority, "run")] == ["progress"]
    monkeypatch.setattr(store, "_insert_event", insert)
    lease = claim(authority, "run", "new", 60)
    assert lease["fence"] == old["fence"] + 1 and lease["committed_seq"] == 1
    assert [row["kind"] for row in all_events(authority, "run")] == ["progress", "lease_takeover"]


def test_cancel_running_queued_and_terminal(authority):
    create(authority)
    lease = claim(authority, "run", "worker", 60)
    assert store.cancel_requested(authority, "run") is False
    assert store.request_cancel(authority, "run", "actor@example.com") is True
    # Every API call opens a fresh connection; the holder sees committed cancellation.
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(store.cancel_requested, authority, "run").result() is True
    row = store.get(authority, "run")
    assert row["state"] == "running" and row["cancel_requested_by"] == "actor@example.com"
    assert store.request_cancel(authority, "run", "other") is False
    assert store.finish(authority, "run", "worker", lease["fence"], "cancelled")
    before = store.get(authority, "run")
    assert store.request_cancel(authority, "run", "other") is False
    assert store.get(authority, "run") == before
    create(authority, "queued")
    assert store.request_cancel(authority, "queued", "actor") is True
    row = store.get(authority, "queued")
    assert row["state"] == "cancelled" and row["finished_at"] is not None
    assert [row["kind"] for row in all_events(authority, "queued")] == ["cancelled"]
    assert claim(authority, "queued", "worker", 60) is False
    create(authority, "finished")
    lease = claim(authority, "finished", "worker", 60)
    assert store.finish(authority, "finished", "worker", lease["fence"])
    assert store.request_cancel(authority, "finished", "actor") is False
    assert store.cancel_requested(authority, "finished") is False


def test_cursor_reads_committed_events_on_second_connection(authority):
    create(authority)
    lease = claim(authority, "run", "worker", 60)
    assert store.append_events(authority, "run", "worker", lease["fence"], [event(0), event(1)])
    cursor = all_events(authority, "run")[-1]["seq"]
    assert store.append_events(authority, "run", "worker", lease["fence"], [event(2), event(3)])
    with ThreadPoolExecutor(max_workers=1) as pool:
        rows = pool.submit(store.list_events, authority, "run", after_seq=cursor, limit=1).result()
    assert [row["payload"]["index"] for row in rows] == [2]
    assert [row["payload"]["index"] for row in store.list_events(
        authority, "run", after_seq=rows[-1]["seq"])] == [3]
    assert [row["seq"] for row in store.list_events(authority, "run")] == [1, 2, 3]
    for limit in (0, 1001, True):
        with pytest.raises(ValueError):
            store.list_events(authority, "run", limit=limit)


def test_worker_status_upsert_and_age_filter(authority, monkeypatch):
    store.record_worker_status(authority, "build:1", "idle", 0, 3)
    row = next(row for row in store.worker_status() if row["worker_id"] == authority)
    assert (row["build_revision"], row["color"], row["active_runs"], row["queued_runs"]) == ("build:1", "idle", 0, 3)
    later = store._now() + timedelta(seconds=61)
    monkeypatch.setattr(store, "_now", lambda: later)
    assert not any(row["worker_id"] == authority for row in store.worker_status())
    store.record_worker_status(authority, "build:2", "active", 2, 1)
    rows = [row for row in store.worker_status() if row["worker_id"] == authority]
    assert len(rows) == 1
    assert (rows[0]["build_revision"], rows[0]["color"], rows[0]["active_runs"], rows[0]["queued_runs"]) == ("build:2", "active", 2, 1)
    assert store._date(rows[0]["heartbeat_at"]) == later


def test_pause_on_and_off(authority):
    original = store.paused()
    try:
        store.set_paused(True, "maintenance", "operator")
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(store.paused).result() is True
        row = store._run("lab-worker-control", lambda sql: dict(sql.execute(
            "SELECT * FROM lab_worker_control WHERE id='global'").fetchone()))
        assert row["reason"] == "maintenance" and row["updated_by"] == "operator"
        store.set_paused(False, "", "operator")
        assert store.paused() is False
    finally:
        store.set_paused(original, "", "operator")


def test_pause_defaults_to_false_on_empty_local_store(local):
    assert store.paused() is False


@pytest.mark.parametrize("runtime", ["staging", "production"])
def test_deployed_runtime_refuses_sqlite(local, monkeypatch, runtime):
    monkeypatch.setenv("LEAF_RUNTIME_ENV", runtime)
    with pytest.raises(store.StoreUnavailable, match="lab_postgres_required"):
        create("tenant")
    assert not local.exists()


def test_platform_required_refuses_sqlite(local, monkeypatch):
    monkeypatch.setenv("LEAF_PLATFORM_POSTGRES_REQUIRED", "1")
    with pytest.raises(store.StoreUnavailable, match="lab_postgres_required"):
        create("tenant")
    assert not local.exists()


@pytest.mark.parametrize("value", [None, "", " \t"])
def test_sqlite_requires_explicit_path(local, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("LEAF_LAB_DB")
    else:
        monkeypatch.setenv("LEAF_LAB_DB", value)
    with pytest.raises(store.StoreUnavailable, match="lab_db_unset"):
        create("tenant")
    assert not local.exists()


@pytest.mark.parametrize("value", ["/data/state", "/data/state/runs.db"])
def test_sqlite_forbids_durable_volume(local, monkeypatch, value):
    monkeypatch.setenv("LEAF_LAB_DB", value)
    with pytest.raises(store.StoreUnavailable, match="lab_sqlite_path_forbidden"):
        create("tenant")
    assert not local.exists()


def test_unknown_selector_refuses_persistence(local, monkeypatch):
    monkeypatch.setenv("LEAF_LAB_RUN_STORE", "postgre")
    with pytest.raises(store.StoreUnavailable, match="lab_run_store_invalid"):
        create("tenant")
    assert not local.exists()


def test_required_selector_never_falls_back_to_sqlite(local, monkeypatch):
    monkeypatch.setenv("LEAF_LAB_RUN_STORE", "postgres_required")

    def unavailable():
        raise store.StoreUnavailable("platform_unavailable")

    monkeypatch.setattr(store.platform_link, "platform_db", unavailable)
    with pytest.raises(store.StoreUnavailable, match="platform_unavailable"):
        create("tenant")
    assert not local.exists()
