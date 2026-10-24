"""Leased worker integration proofs on independent SQLite/PostgreSQL handles."""
from datetime import timedelta
import threading
import time
from types import SimpleNamespace

import pytest

import emf_metrics
import lab_run_store as store
import lab_worker as worker
from lab_evaluate import RunCancelled, runtime_value
from test_lab_evaluate import case
from test_lab_run_store import authority  # noqa: F401 -- shared two-authority fixture


def eventually(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "worker did not reach expected state"


@pytest.fixture
def rig(authority, monkeypatch):
    graph, registry = case("operator-chain")
    monkeypatch.setattr(worker, "trusted_registry", lambda: registry)
    monkeypatch.setattr(worker, "lab_surface_enabled", lambda: True)
    prior_pause = store.paused()
    store.set_paused(False, "test", "test")
    metrics = []
    emit = SimpleNamespace(emit_lab_run_terminal=lambda state: metrics.append(("terminal", state)),
                           emit_lab_lease_takeover=lambda: metrics.append(("takeover", 1)),
                           emit_lab_queue_age_max=lambda age: metrics.append(("age", age)))
    artifact = {"graph": graph, "sha256": "a" * 64}
    artifacts = SimpleNamespace(get=lambda *args: artifact)
    env = {"LEAF_BUILD_REVISION": "abc1234", "LEAF_LAB_WORKER_IDLE_CLAIMS": "1"}
    workers = []

    def make(**kwargs):
        instance = worker.LabWorker(artifacts=artifacts, env=env, emit=emit,
                                    worker_id=authority if not workers else authority + "-second", **kwargs)
        workers.append(instance)
        return instance

    def create(run_id="run", build="abc1234"):
        return store.create(authority, "doc", run_id, 1, "a" * 64, worker.OPS_REVISION,
                            "b" * 64, "adapter-1", build)

    yield SimpleNamespace(tenant=authority, graph=graph, registry=registry, artifact=artifact,
                          env=env, metrics=metrics, make=make, create=create)
    for instance in workers:
        instance.stop()
    eventually(lambda: all(run.future is None or run.future.done()
                           for instance in workers for run in instance._runs.values()))
    store.set_paused(prior_pause, "", "test")


def events(rig, run_id="run"):
    return store.list_events(rig.tenant, run_id, after_seq=-1, limit=1000)


def terminal(rig, state="finished", run_id="run"):
    eventually(lambda: store.get(rig.tenant, run_id)["state"] == state)


def test_finishes_values_pins_and_status(rig):
    rig.create()
    instance = rig.make()
    instance.tick()
    terminal(rig)
    eventually(lambda: ("terminal", "finished") in rig.metrics)
    values = [event for event in events(rig) if event["kind"] == "value"]
    assert {event["block_id"] for event in values} == {block["id"] for block in rig.graph["blocks"]}
    assert len(values) == len(rig.graph["blocks"])
    assert all("data" in value for event in values for value in event["payload"].values())
    row = store.get(rig.tenant, "run")
    assert row["claimed_build_revision"] == "abc1234" and row["claimed_color"] == "active"
    status = next(row for row in store.worker_status() if row["worker_id"] == rig.tenant)
    assert status["build_revision"] == "abc1234" and status["color"] == "active"
    assert any(kind == "age" for kind, value in rig.metrics)


def test_second_connection_cancel_and_stream_under_two_seconds(rig, monkeypatch):
    entered = threading.Event()
    cancellation_observed = threading.Event()
    observed_at = []

    def evaluate(graph, registry, *, cancelled, on_block, **kwargs):
        on_block("first", {"result": runtime_value(7)})
        entered.set()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if cancelled():
                observed_at.append(time.monotonic())
                cancellation_observed.set()
                raise RunCancelled()
            time.sleep(0.01)
        raise AssertionError("cancellation not observed")

    monkeypatch.setattr(worker, "evaluate_graph", evaluate)
    rig.create()
    rig.make().tick()
    assert entered.wait(30)
    # All store calls use fresh database connections: this is the second handle.
    first = events(rig)
    assert first[0]["payload"]["result"]["data"] == 7
    assert store.request_cancel(rig.tenant, "run", "handle-B")
    requested_at = time.monotonic()
    assert cancellation_observed.wait(30)
    # Measure evaluator detection, excluding the later terminal database writes.
    assert observed_at[0] - requested_at < 2
    terminal(rig, "cancelled")
    tail = store.list_events(rig.tenant, "run", after_seq=first[-1]["seq"])
    assert [event["kind"] for event in tail] == ["cancelled"]
    eventually(lambda: ("terminal", "cancelled") in rig.metrics)


def test_takeover_skips_committed_values(rig, monkeypatch):
    rig.create()
    lease = store.claim(rig.tenant, "run", "old-worker", 1, build_revision="abc1234", color="active")
    block_id = rig.graph["blocks"][0]["id"]
    assert store.append_events(rig.tenant, "run", "old-worker", lease["fence"], [
        {"kind": "value", "block_id": block_id, "payload": {"result": {"data": 7}}}])
    later = store._now() + timedelta(seconds=2)
    monkeypatch.setattr(store, "_now", lambda: later)
    rig.make().tick()
    terminal(rig)
    rows = events(rig)
    assert [row["kind"] for row in rows].count("lease_takeover") == 1
    assert len([row for row in rows if row["kind"] == "value" and row["block_id"] == block_id]) == 1
    assert ("takeover", 1) in rig.metrics


@pytest.mark.parametrize("gate", ["rail_off", "worker_disabled", "paused"])
def test_off_suspends_once_stops_renewal_and_resumes_with_takeover(rig, monkeypatch, gate):
    entered = threading.Event()

    def evaluate(graph, registry, *, cancelled, **kwargs):
        entered.set()
        while not cancelled():
            time.sleep(0.01)
        raise RunCancelled()

    original = worker.evaluate_graph
    monkeypatch.setattr(worker, "evaluate_graph", evaluate)
    rig.create()
    instance = rig.make(lease_s=3)
    instance.tick()
    assert entered.wait(2)
    if gate == "rail_off":
        monkeypatch.setattr(worker, "lab_surface_enabled", lambda: False)
    elif gate == "worker_disabled":
        rig.env["LEAF_LAB_WORKER_DISABLED"] = "1"
    else:
        store.set_paused(True, "test", "test")
    instance.tick()
    expiry = store.get(rig.tenant, "run")["lease_expires_at"]
    rig.create("queued")
    instance.tick()
    assert store.get(rig.tenant, "queued")["state"] == "queued"
    rows = [row for row in events(rig) if row["kind"] == "suspended"]
    assert len(rows) == 1 and rows[0]["payload"] == {"reason": gate}
    eventually(lambda: all(run.future.done() for run in instance._runs.values()))
    assert store.get(rig.tenant, "run")["lease_expires_at"] == expiry
    assert store.get(rig.tenant, "run")["state"] == "running"
    monkeypatch.setattr(worker, "lab_surface_enabled", lambda: True)
    rig.env.pop("LEAF_LAB_WORKER_DISABLED", None)
    store.set_paused(False, "", "test")
    monkeypatch.setattr(worker, "evaluate_graph", original)
    later = store._now() + timedelta(seconds=4)
    monkeypatch.setattr(store, "_now", lambda: later)
    instance.tick()
    terminal(rig)
    assert any(row["kind"] == "lease_takeover" for row in events(rig))


def test_idle_requires_explicit_opt_in(rig):
    rig.create(build="def5678")
    rig.env.pop("LEAF_LAB_WORKER_IDLE_CLAIMS")
    instance = rig.make()
    instance.tick()
    assert store.get(rig.tenant, "run")["state"] == "queued"
    assert next(row for row in store.worker_status() if row["worker_id"] == rig.tenant)["color"] == "idle"
    rig.env["LEAF_LAB_WORKER_IDLE_CLAIMS"] = "1"
    instance.tick()
    terminal(rig)
    assert store.get(rig.tenant, "run")["claimed_color"] == "idle"


@pytest.mark.parametrize("reason", ["graph_pin_mismatch", "registry_changed", "lab_graph_host_refused",
                                  "lab_graph_effect_refused", "worker_error"])
def test_refusals_and_run_exception(rig, monkeypatch, reason):
    rig.create()
    if reason == "graph_pin_mismatch":
        rig.artifact["sha256"] = "c" * 64
    elif reason == "registry_changed":
        monkeypatch.setattr(store, "registry_digest", lambda: "changed")
    elif reason in {"lab_graph_host_refused", "lab_graph_effect_refused"}:
        import lab_evaluate
        monkeypatch.setattr(lab_evaluate.lab_registry, "lookup", lambda *args:
                            SimpleNamespace(residency="host" if reason == "lab_graph_host_refused" else "tenant",
                                            effect="pure" if reason == "lab_graph_host_refused" else "write"))
    else:
        def fail(*args, **kwargs):
            raise RuntimeError("secret must not be logged")
        monkeypatch.setattr(worker, "evaluate_graph", fail)
    rig.make().tick()
    terminal(rig, "failed")
    assert events(rig)[-1]["payload"] == {"reason": reason}
    eventually(lambda: ("terminal", "failed") in rig.metrics)


def test_run_cap_uses_injected_clock(rig, monkeypatch):
    rig.create()
    current = [0]
    original = worker.evaluate_graph

    def evaluate(*args, **kwargs):
        current[0] = 601
        return original(*args, **kwargs)

    monkeypatch.setattr(worker, "evaluate_graph", evaluate)
    rig.make(clock=lambda: current[0]).tick()
    terminal(rig, "failed")
    assert events(rig)[-1]["payload"] == {"reason": "run_wall_cap"}


def test_at_most_two_concurrent_runs(rig, monkeypatch):
    entered = []
    release = threading.Event()
    lock = threading.Lock()

    def evaluate(*args, cancelled, **kwargs):
        with lock:
            entered.append(threading.get_ident())
        while not release.wait(0.01):
            if cancelled():
                raise RunCancelled()

    monkeypatch.setattr(worker, "evaluate_graph", evaluate)
    for index in range(3):
        rig.create("run-" + str(index))
    instance = rig.make()
    try:
        instance.tick()
        eventually(lambda: len(entered) == 2)
        instance.tick()
        assert len(entered) == 2
        assert store.get(rig.tenant, "run-2")["state"] == "queued"
    finally:
        release.set()
    terminal(rig, run_id="run-0")
    terminal(rig, run_id="run-1")
    eventually(lambda: all(run.future.done() for run in instance._runs.values()))
    instance.tick()
    terminal(rig, run_id="run-2")


def test_lost_custody_stops_all_writes(rig, monkeypatch):
    entered = threading.Event()

    def evaluate(*args, cancelled, on_block, **kwargs):
        entered.set()
        while not cancelled():
            time.sleep(0.01)
        on_block("late", {"result": runtime_value(1)})

    monkeypatch.setattr(worker, "evaluate_graph", evaluate)
    monkeypatch.setattr(store, "heartbeat", lambda *args: False)
    rig.create()
    instance = rig.make(lease_s=0.15)
    instance.tick()
    assert entered.wait(2)
    eventually(lambda: all(run.future.done() for run in instance._runs.values()))
    assert events(rig) == []
    assert store.get(rig.tenant, "run")["state"] == "running"
    assert not any(kind == "terminal" for kind, value in rig.metrics)


def test_stop_suspends_within_two_seconds(rig, monkeypatch):
    entered = threading.Event()

    def evaluate(*args, cancelled, **kwargs):
        entered.set()
        while not cancelled():
            time.sleep(0.01)
        raise RunCancelled()

    monkeypatch.setattr(worker, "evaluate_graph", evaluate)
    rig.create()
    instance = rig.make().start()
    assert entered.wait(30)
    timeout_s = 2.0
    started = time.monotonic()
    instance.stop(timeout_s=timeout_s)
    assert time.monotonic() - started < timeout_s + 0.5
    eventually(lambda: any(row["kind"] == "suspended" for row in events(rig)))
    assert [row["kind"] for row in events(rig)] == ["suspended"]
    assert store.get(rig.tenant, "run")["state"] == "running"


def test_tick_exception_loop_continues_without_message(rig, monkeypatch, caplog):
    instance = rig.make(poll_s=0.01)
    calls = []
    completed = threading.Event()

    def tick():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("secret tick payload")
        completed.set()

    monkeypatch.setattr(instance, "tick", tick)
    instance.start()
    assert completed.wait(2)
    instance.stop()
    assert "RuntimeError" in caplog.text and "secret tick payload" not in caplog.text


@pytest.mark.parametrize("auth,single,mode,disabled,expected", [
    (False, "1", "postgres", "0", False),
    (True, "0", "sqlite", "0", False),
    (True, "1", "sqlite", "0", True),
    (True, "0", "postgres", "0", True),
    (True, "1", "postgres", "1", False),
])
def test_startup_guards(monkeypatch, auth, single, mode, disabled, expected):
    import app as application
    starts = []
    monkeypatch.setattr(application, "_lab_worker", None)
    monkeypatch.setattr(application.deps, "auth_live", lambda: auth)
    monkeypatch.setattr(store, "store_mode", lambda: mode)
    monkeypatch.setenv("LEAF_LAB_WORKER_SQLITE_SINGLE_PROCESS", single)
    monkeypatch.setenv("LEAF_LAB_WORKER_DISABLED", disabled)
    monkeypatch.setattr(worker, "LabWorker", lambda: SimpleNamespace(start=lambda: starts.append(1)))
    application.initialize_lab_worker()
    application.initialize_lab_worker()
    assert len(starts) == int(expected)


def test_startup_exception_is_contained(monkeypatch, capsys):
    import app as application
    monkeypatch.setattr(application, "_lab_worker", None)
    monkeypatch.setattr(application.deps, "auth_live", lambda: True)
    monkeypatch.setenv("LEAF_LAB_WORKER_DISABLED", "0")

    def fail():
        raise RuntimeError("secret startup payload")

    monkeypatch.setattr(store, "store_mode", fail)
    application.initialize_lab_worker()
    output = capsys.readouterr().err
    assert "RuntimeError" in output and "secret startup payload" not in output


def test_lab_emf_single_low_cardinality_dimension_sets(monkeypatch):
    docs = []
    monkeypatch.setattr(emf_metrics, "_DISABLED", False)
    monkeypatch.setattr(emf_metrics, "_write", docs.append)
    monkeypatch.setenv("LEAF_METRICS_ENVIRONMENT", "staging")
    for state in ("finished", "failed", "cancelled", "unbounded-run-id"):
        emf_metrics.emit_lab_run_terminal(state)
    emf_metrics.emit_lab_lease_takeover()
    emf_metrics.emit_lab_queue_age_max(17.5)
    emf_metrics.emit_lab_queue_age_max(float("nan"))
    assert len(docs) == 5
    for doc in docs:
        directives = doc["_aws"]["CloudWatchMetrics"]
        assert len(directives) == 1
        directive = directives[0]
        assert directive["Namespace"] == "Leaf/Platform/APS"
        dimensions = ["environment", "status"] if "LabRunTerminal" in doc else ["environment"]
        assert directive["Dimensions"] == [dimensions]
        assert doc["environment"] in emf_metrics.ENVIRONMENT_ALLOW
    assert docs[-1]["LabQueueAgeMax"] == 17.5


def test_lab_emf_is_best_effort(monkeypatch):
    monkeypatch.setattr(emf_metrics, "_DISABLED", False)

    def fail(*args):
        raise RuntimeError("sink unavailable")

    monkeypatch.setattr(emf_metrics, "_emit", fail)
    emf_metrics.emit_lab_run_terminal("finished")
    emf_metrics.emit_lab_lease_takeover()
    emf_metrics.emit_lab_queue_age_max(1)
