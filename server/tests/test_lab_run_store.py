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
                "DELETE FROM lab_runs WHERE tenant_id IN (?,?)", (tenant, tenant + "-other")), write=True)
        finally:
            db.reset_pool()


def create(tenant, run_id="run", doc_id="doc"):
    return store.create(tenant, doc_id, run_id, 1, "a" * 64, "ops:1", "b" * 64, "adapter-1", "build_1")


def event(index, block="block"):
    return {"kind": "progress", "block_id": block, "payload": {"index": index}}


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

    def claim(owner):
        barrier.wait(timeout=10)
        return store.claim(authority, "run", owner, 3600)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["worker-1", "worker-2"]))
    assert sum(result is False for result in results) == 1
    lease = next(result for result in results if result is not False)
    assert lease["fence"] == 1
    assert store.get(authority, "run")["lease_owner"] == lease["owner"]


def test_stale_fence_cannot_write_anything(authority, monkeypatch):
    create(authority)
    old = store.claim(authority, "run", "worker", 10)
    later = store._now() + timedelta(seconds=20)
    monkeypatch.setattr(store, "_now", lambda: later)
    new = store.claim(authority, "run", "worker", 60)
    assert new["fence"] > old["fence"]
    before = store.get(authority, "run")
    assert store.append_events(authority, "run", "worker", old["fence"], [event(0)]) is False
    assert store.heartbeat(authority, "run", "worker", old["fence"], 120) is False
    assert store.finish(authority, "run", "worker", old["fence"]) is False
    assert store.get(authority, "run") == before
    assert store.list_events(authority, "run") == []


def test_heartbeat_extends_only_holder(authority, monkeypatch):
    create(authority)
    lease = store.claim(authority, "run", "worker", 60)
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
    lease = store.claim(authority, "run", "worker", 1)
    later = store._now() + timedelta(seconds=2)
    monkeypatch.setattr(store, "_now", lambda: later)
    assert store.heartbeat(authority, "run", "worker", lease["fence"], 30) is False
    assert store.append_events(authority, "run", "worker", lease["fence"], [event(0)]) is False
    assert store.finish(authority, "run", "worker", lease["fence"]) is False
    assert store.claim(authority, "run", "replacement", 60)["fence"] == lease["fence"] + 1


def test_two_connections_allocate_gap_free_events(authority):
    create(authority)
    lease = store.claim(authority, "run", "worker", 3600)
    barrier = threading.Barrier(2)

    def append(writer):
        barrier.wait(timeout=10)
        for batch in range(10):
            assert store.append_events(authority, "run", "worker", lease["fence"],
                                       [event(writer * 100 + batch * 10 + i) for i in range(10)]) is True

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(append, [0, 1]))
    events = store.list_events(authority, "run")
    assert [row["seq"] for row in events] == list(range(200))
    assert sorted(row["payload"]["index"] for row in events) == list(range(200))
    row = store.get(authority, "run")
    assert row["next_seq"] == row["event_count"] == 200


def test_cap_coalesces_latest_per_block_in_oldest_half(authority):
    create(authority)
    lease = store.claim(authority, "run", "worker", 3600)
    assert store.append_events(authority, "run", "worker", lease["fence"],
                               [event(i, "block-" + str(i % 10)) for i in range(10000)])
    events = store.list_events(authority, "run")
    assert [row["seq"] for row in events] == list(range(4990, 10001))
    assert [row["payload"]["index"] for row in events[:10]] == list(range(4990, 5000))
    assert events[-1]["kind"] == "coalesced" and events[-1]["payload"] == {"count": 4990}
    row = store.get(authority, "run")
    assert row["event_count"] == len(events) == 5011
    assert row["next_seq"] == 10001
    assert store.append_events(authority, "run", "worker", lease["fence"], [event(10001)])
    assert store.list_events(authority, "run")[-1]["seq"] == 10001


def test_uncoalescible_cap_rolls_back_batch(authority):
    create(authority)
    lease = store.claim(authority, "run", "worker", 3600)
    with pytest.raises(store.TooLarge):
        store.append_events(authority, "run", "worker", lease["fence"],
                            [event(i, "block-" + str(i)) for i in range(10001)])
    assert store.list_events(authority, "run") == []
    assert store.get(authority, "run")["next_seq"] == 0


def test_sweep_bounds_per_tenant_doc_and_event_age(authority, monkeypatch):
    now = store._now()
    old = now - timedelta(days=8)
    for tenant in (authority, authority + "-other"):
        for i in range(52):
            created = old + timedelta(seconds=i)
            monkeypatch.setattr(store, "_now", lambda: created)
            create(tenant, "run-" + str(i))
        create(tenant, "separate", "other-doc")
        lease = store.claim(tenant, "run-51", "worker", 3600)
        assert store.append_events(tenant, "run-51", "worker", lease["fence"], [event(0)])
        monkeypatch.setattr(store, "_now", lambda: now)
        lease = store.claim(tenant, "run-51", "worker", 3600)
        assert store.append_events(tenant, "run-51", "worker", lease["fence"], [event(1)])
        # The seven-day boundary itself survives (only strictly older expires).
        store._run(tenant, lambda sql: sql.execute(
            "UPDATE lab_run_events SET created_at=? WHERE tenant_id=? AND seq=1",
            ((now - timedelta(days=7)).isoformat(), tenant)), write=True)
    store.sweep(now)
    for tenant in (authority, authority + "-other"):
        runs = store._run(tenant, lambda sql: [dict(row) for row in sql.execute(
            "SELECT * FROM lab_runs WHERE tenant_id=?", (tenant,)).fetchall()])
        assert len(runs) == 51
        assert {row["run_id"] for row in runs} == {"separate"} | {"run-" + str(i) for i in range(2, 52)}
        assert [row["seq"] for row in store.list_events(tenant, "run-51")] == [1]
        assert store.get(tenant, "run-51")["event_count"] == 1
        assert store.get(tenant, "run-51")["next_seq"] == 2


def test_tenant_boundary_for_every_operation(authority):
    create(authority)
    lease = store.claim(authority, "run", "worker", 3600)
    before = store.get(authority, "run")
    foreign = authority + "-other"
    for read in (store.get, store.list_events):
        with pytest.raises(store.NotFound):
            read(foreign, "run")
    assert store.claim(foreign, "run", "worker", 60) is False
    assert store.heartbeat(foreign, "run", "worker", lease["fence"], 60) is False
    assert store.append_events(foreign, "run", "worker", lease["fence"], [event(0)]) is False
    assert store.finish(foreign, "run", "worker", lease["fence"]) is False
    assert store.get(authority, "run") == before
    create(foreign)
    assert store.claim(foreign, "run", "worker", 60)["fence"] == 1


def test_finished_run_cannot_reclaim_or_mutate_pins(authority):
    original = create(authority)
    lease = store.claim(authority, "run", "worker", 60)
    assert store.finish(authority, "run", "other", lease["fence"]) is False
    assert store.finish(authority, "run", "worker", lease["fence"])
    row = store.get(authority, "run")
    assert row["state"] == "finished" and row["finished_at"] is not None
    for key in ("graph_revision", "graph_sha256", "registry_digest", "ops_revision",
                "input_digest", "adapter_revision", "build_revision"):
        assert row[key] == original[key]
    assert store.claim(authority, "run", "worker", 60) is False
    assert store.append_events(authority, "run", "worker", lease["fence"], [event(0)]) is False


def test_failed_insert_rolls_back_sequence_allocation(authority, monkeypatch):
    create(authority)
    lease = store.claim(authority, "run", "worker", 60)
    insert = store._insert_event

    def fail_second(sql, tenant, run_id, seq, *args):
        if seq == 1:
            raise RuntimeError("injected event failure")
        return insert(sql, tenant, run_id, seq, *args)

    monkeypatch.setattr(store, "_insert_event", fail_second)
    with pytest.raises(RuntimeError, match="injected event failure"):
        store.append_events(authority, "run", "worker", lease["fence"], [event(0), event(1)])
    assert store.get(authority, "run")["next_seq"] == 0
    assert store.list_events(authority, "run") == []
    monkeypatch.setattr(store, "_insert_event", insert)
    assert store.append_events(authority, "run", "worker", lease["fence"], [event(0), event(1)])
    assert [row["seq"] for row in store.list_events(authority, "run")] == [0, 1]


@pytest.fixture
def local(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "test")
    monkeypatch.setenv("LEAF_PLATFORM_POSTGRES_REQUIRED", "0")
    monkeypatch.setenv("LEAF_LAB_RUN_STORE", "sqlite")
    path = tmp_path / "local.db"
    monkeypatch.setenv("LEAF_LAB_DB", str(path))
    return path


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
