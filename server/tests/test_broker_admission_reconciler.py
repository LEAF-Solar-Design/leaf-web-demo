"""Admission recovery contracts using the real resolver and PostgreSQL store."""
from copy import deepcopy
from decimal import Decimal
from fractions import Fraction
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, call
import uuid

import pytest
import requests
from fastapi import HTTPException
from fastapi.testclient import TestClient

import broker
import broker_admission_reconciler as reconciler
import broker_pg_store
import test_broker_pg_store as pg_fixtures


JOB_ID = "12345678-1234-1234-1234-123456789abc"
EVENT_KEY = f"{JOB_ID}:broker-run"
NOW = 1753222000.0
FAILURES = [
    "failedDownload", "failedInstructions", "failedUpload",
    "failedLimitDataSize", "failedLimitProcessingTime", "cancelled",
]


class FakeAps:
    def __init__(self, answer=None, error=None):
        self.answer = {"status": "failedInstructions"} if answer is None else answer
        self.error = error
        self.calls = []

    def get_workitem_status(self, workitem_id):
        self.calls.append(workitem_id)
        if self.error is not None:
            raise self.error
        return self.answer


@pytest.fixture
def environment(monkeypatch):
    db = pg_fixtures._Db()
    store = broker_pg_store.PostgresBrokerStore(db)
    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    monkeypatch.delenv("LEAF_BROKER_RECONCILER_ALARM_ONLY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(broker, "_pg_store", store)
    monkeypatch.setattr(reconciler, "_QUEUE_POSITIONS", {})
    monkeypatch.setattr(reconciler, "_QUEUE_COUNTER", 0)
    return db.pool.conn, store


def start(store, key=EVENT_KEY, *, live=True, tenant="tenant-a"):
    admission = pg_fixtures._admit(
        store, key, tenant, aps_live=live, daily_limit=1000, spend_cap=1000)
    assert admission["status"] == "acquired"
    assert store.mark_execution_started(
        key, tenant, admission["lease_token"], aps_live=live,
        max_concurrency=1000, slot_lease_seconds=60)


def snapshot(conn):
    # Includes admissions, ledger, slots and audit rows, even their absence.
    return deepcopy(vars(conn))


def tick(client, alarms, **kw):
    options = {"correlation": lambda job_id: "workitem-123", "min_age_s": 60}
    options.update(kw)
    return reconciler.reconcile_once(
        aps_client=client, alarm=alarms.append, now=NOW, **options)


def assert_alarm_without_writes(conn, before, summary, alarms, reason, key=EVENT_KEY):
    assert snapshot(conn) == before
    assert summary["resolved"] == []
    assert summary["alarmed"] == alarms == [{
        "event": "broker_admission_reconcile_alarm", "event_key": key,
        "tenant_id": "tenant-a", "reason": reason, "age_seconds": 60.0,
    }]


@pytest.mark.parametrize("status", FAILURES)
def test_aps_failed_workitem_resolves_as_verified_workitem_failed_with_null_cost(
        environment, status):
    conn, store = environment
    start(store)
    client, alarms = FakeAps({"status": status}), []
    summary = tick(client, alarms)
    assert alarms == []
    assert client.calls == ["workitem-123"]
    assert summary["resolved"] == [{
        "event_key": EVENT_KEY, "tenant_id": "tenant-a",
        "resolution": "verified_terminal",
        "evidence_ref": f"aps-workitem:workitem-123:{status}",
    }]
    admission = conn.admissions[EVENT_KEY]
    assert admission["state"] == "terminal"
    assert admission["reserved_usd"] == 0
    assert admission["http_status"] == 502
    result = admission["result_json"]
    if isinstance(result, str):
        result = json.loads(result)
    assert result == broker.err_envelope(
        broker.ErrorCode.WORKITEM_FAILED,
        f"WorkItem workitem-123 status={status}", retryable=False)
    ledger = conn.ledger[EVENT_KEY]
    for key, value in {
        "ts": NOW, "tenant_id": "tenant-a", "tool": None,
        "engine_op": "", "aps_endpoint": broker.APS_ENDPOINT,
        "aps_live": True, "engine_seconds": None, "usd_est": None,
        "status": "WORKITEM_FAILED", "job_id": JOB_ID,
    }.items():
        assert ledger[key] == value
    assert conn.slots[EVENT_KEY]["state"] == "released"
    assert len(conn.audits) == 1
    assert conn.audits[0]["operator_id"] == "reconciler"
    assert conn.audits[0]["resolution"] == "verified_terminal"


def test_aps_success_alarms_and_writes_nothing(environment):
    conn, store = environment
    start(store)
    before, alarms = snapshot(conn), []
    summary = tick(FakeAps({"status": "success"}), alarms)
    assert_alarm_without_writes(
        conn, before, summary, alarms, "aps_succeeded_needs_operator")


@pytest.mark.parametrize("answer", [
    {"status": "pending"}, {"status": "inprogress"}, {},
    {"status": "unknown"}, {"status": None}, {"status": []},
])
def test_non_terminal_aps_status_alarms_and_writes_nothing(environment, answer):
    conn, store = environment
    start(store)
    before, alarms = snapshot(conn), []
    summary = tick(FakeAps(answer), alarms)
    assert_alarm_without_writes(conn, before, summary, alarms, "aps_not_terminal")


@pytest.mark.parametrize("error", [
    requests.HTTPError("404"), requests.Timeout("timeout"), ValueError("bad JSON"),
])
def test_aps_client_error_or_timeout_alarms_and_writes_nothing(environment, error):
    conn, store = environment
    start(store)
    before, alarms = snapshot(conn), []
    summary = tick(FakeAps(error=error), alarms)
    assert_alarm_without_writes(conn, before, summary, alarms, "aps_status_unreadable")


def test_missing_workitem_correlation_alarms_and_writes_nothing(environment):
    conn, store = environment
    start(store)
    before, alarms, client = snapshot(conn), [], FakeAps()
    summary = tick(client, alarms, correlation=lambda job_id: None)
    assert client.calls == []
    assert_alarm_without_writes(conn, before, summary, alarms, "no_workitem_correlation")


@pytest.mark.parametrize("key", ["unbound", f"{JOB_ID}:broker-other", EVENT_KEY + "\nextra"])
def test_event_key_not_bound_to_a_job_alarms_and_writes_nothing(environment, key):
    conn, store = environment
    start(store, key)
    before, alarms, client = snapshot(conn), [], FakeAps()
    summary = tick(client, alarms)
    assert client.calls == []
    assert_alarm_without_writes(conn, before, summary, alarms, "event_key_not_job_bound", key)


def test_two_executing_admissions_for_one_job_alarm_both(environment):
    conn, store = environment
    start(store)
    fallback = f"{JOB_ID}:broker-fallback"
    start(store, fallback)
    before, alarms, client = snapshot(conn), [], FakeAps()
    summary = tick(client, alarms)
    assert snapshot(conn) == before
    assert summary["resolved"] == []
    assert len(alarms) == 2
    assert {record["event_key"] for record in alarms} == {EVENT_KEY, fallback}
    assert {record["reason"] for record in alarms} == {"ambiguous_job_admissions"}
    assert client.calls == []


@pytest.mark.parametrize("live", [True, False])
def test_admission_younger_than_min_age_is_skipped_without_aps_call(environment, live):
    conn, store = environment
    start(store, live=live)
    before, alarms, client = snapshot(conn), [], FakeAps()
    summary = tick(client, alarms, min_age_s=reconciler.DEFAULT_MIN_AGE_S)
    assert snapshot(conn) == before
    assert summary == {"mode": "postgres", "checked": 0, "skipped_young": 1,
                       "resolved": [], "alarmed": []}
    assert alarms == client.calls == []


def test_stale_non_live_admission_resolves_confirmed_failed_no_charge(environment):
    conn, store = environment
    start(store, "non-live-run", live=False)
    alarms, client = [], FakeAps()
    summary = tick(client, alarms)
    assert summary["resolved"] == [{
        "event_key": "non-live-run", "tenant_id": "tenant-a",
        "resolution": "confirmed_failed_no_charge",
        "evidence_ref": "non-live-admission:non-live-run",
    }]
    assert alarms == client.calls == []
    assert conn.admissions["non-live-run"]["state"] == "terminal"
    assert conn.admissions["non-live-run"]["accounted_work"] is False
    assert conn.ledger["non-live-run"]["status"] == "RECONCILED_FAILED_NO_CHARGE"
    assert conn.ledger["non-live-run"]["usd_est"] is None
    assert conn.audits[0]["operator_id"] == "reconciler"


@pytest.mark.parametrize("live", [True, False])
def test_resolution_goes_through_the_admin_resolve_path_as_reconciler(
        environment, monkeypatch, live):
    conn, store = environment
    start(store, live=live)
    real_resolve = broker.resolve_executing_admission
    calls = []

    def recording_resolve(key, request):
        calls.append((key, request))
        return real_resolve(key, request)

    monkeypatch.setattr(broker, "resolve_executing_admission", recording_resolve)
    alarms = []
    tick(FakeAps(), alarms)
    assert alarms == []
    assert len(calls) == 1
    key, request = calls[0]
    assert key == EVENT_KEY
    assert isinstance(request, broker.BrokerAdmissionResolution)
    assert request.operator_id == "reconciler"
    assert len(request.reason) >= 16
    assert request.confirmation == f"RESOLVE tenant-a {EVENT_KEY} {request.resolution}"
    assert conn.admissions[EVENT_KEY]["state"] == "terminal"


def test_reconciler_never_deletes_releases_leases_or_resubmits(environment, monkeypatch):
    conn, store = environment
    start(store)
    keys = set(conn.admissions)

    def forbidden(*args, **kwargs):
        pytest.fail("reconciler attempted an execution or lease operation")

    for name in ("admit_run", "mark_execution_started", "complete_run", "release_lease"):
        monkeypatch.setattr(store, name, forbidden, raising=False)
    monkeypatch.setattr(broker, "_get_da", forbidden)
    monkeypatch.setattr(broker, "_replay_persisted_workitems", forbidden)
    alarms = []
    summary = tick(FakeAps(), alarms)
    assert alarms == []
    assert len(summary["resolved"]) == 1
    assert set(conn.admissions) == keys
    assert conn.slots[EVENT_KEY]["state"] == "released"
    # The unchanged fake rejects unknown SQL, including deletes. Only the
    # resolver's atomic terminal transition can release the known APS slot.
    assert len(conn.audits) == len(conn.ledger) == 1


@pytest.mark.parametrize("limit", [0, 2, reconciler.MAX_APS_CHECKS_PER_TICK])
def test_aps_status_checks_per_tick_are_bounded(environment, limit):
    conn, store = environment
    for _ in range(22):
        start(store, f"{uuid.uuid4()}:broker-run")
    before, alarms, client = snapshot(conn), [], FakeAps({"status": "pending"})
    summary = tick(client, alarms, max_aps_checks=limit)
    assert snapshot(conn) == before
    assert len(client.calls) == len(alarms) == limit
    assert summary["resolved"] == []


def test_reconciler_rotates_so_no_admission_starves(environment):
    conn, store = environment
    job_ids = [str(uuid.uuid4()) for _ in range(21)]
    for job_id in job_ids:
        start(store, f"{job_id}:broker-run")
    failed_job = job_ids[-1]
    failed_key = f"{failed_job}:broker-run"
    calls = []

    def get_status(workitem_id):
        calls.append(workitem_id)
        return {"status": "failedInstructions" if workitem_id == failed_job else "success"}

    client = SimpleNamespace(get_workitem_status=get_status)
    before, alarms = snapshot(conn), []
    first = tick(client, alarms, correlation=lambda job_id: job_id)
    assert calls == job_ids[:20]
    assert first["resolved"] == []
    assert snapshot(conn) == before
    assert len(alarms) == 20
    assert {record["reason"] for record in alarms} == {"aps_succeeded_needs_operator"}

    calls.clear()
    alarms.clear()
    second = tick(client, alarms, correlation=lambda job_id: job_id)
    assert calls == [failed_job] + job_ids[:19]
    assert len(calls) <= 20
    assert second["resolved"] == [{
        "event_key": failed_key, "tenant_id": "tenant-a",
        "resolution": "verified_terminal",
        "evidence_ref": f"aps-workitem:{failed_job}:failedInstructions",
    }]
    assert conn.admissions[failed_key]["state"] == "terminal"
    assert conn.ledger[failed_key]["job_id"] == failed_job
    assert len(conn.audits) == 1
    assert all(conn.admissions[f"{job_id}:broker-run"]["state"] == "executing"
               for job_id in job_ids[:-1])


def test_reconciler_arrivals_do_not_starve_a_waiting_admission(environment):
    conn, store = environment
    waiting_jobs = [JOB_ID] + [str(uuid.uuid4()) for _ in range(19)]
    for job_id in waiting_jobs:
        start(store, f"{job_id}:broker-run")
    client, alarms = FakeAps({"status": "pending"}), []
    before = snapshot(conn)
    tick(client, alarms, correlation=lambda job_id: job_id)
    assert client.calls == waiting_jobs
    assert len(client.calls) <= 20
    assert snapshot(conn) == before

    expected_jobs = waiting_jobs
    for _ in range(2):
        arrivals = [str(uuid.uuid4()) for _ in range(20)]
        for job_id in arrivals:
            start(store, f"{job_id}:broker-run")
        before = snapshot(conn)
        client.calls.clear()
        alarms.clear()
        summary = tick(client, alarms, correlation=lambda job_id: job_id)
        assert client.calls == expected_jobs
        assert len(client.calls) <= 20
        assert len(alarms) == 20
        assert {record["reason"] for record in alarms} == {"aps_not_terminal"}
        assert summary["resolved"] == []
        assert snapshot(conn) == before
        expected_jobs = arrivals


def test_reconciler_queue_map_is_pruned_to_current_rows(environment, monkeypatch):
    conn, store = environment
    job_ids = [str(uuid.uuid4()) for _ in range(3)]
    for job_id in job_ids:
        start(store, f"{job_id}:broker-run")
    client, alarms = FakeAps({"status": "pending"}), []
    before = snapshot(conn)
    tick(client, alarms, correlation=lambda job_id: job_id)
    rows = store.list_executing(100)
    assert set(reconciler._QUEUE_POSITIONS) == {row["event_key"] for row in rows}

    current_rows = rows[1:]
    monkeypatch.setattr(store, "list_executing", lambda limit: current_rows)
    tick(client, alarms, correlation=lambda job_id: job_id)
    assert set(reconciler._QUEUE_POSITIONS) == {
        row["event_key"] for row in current_rows
    }
    current_rows.clear()
    tick(client, alarms, correlation=lambda job_id: job_id)
    assert reconciler._QUEUE_POSITIONS == {}
    assert snapshot(conn) == before


def test_reconciler_default_budget_is_twenty(environment):
    conn, store = environment
    for _ in range(25):
        start(store, f"{uuid.uuid4()}:broker-run")
    before, alarms, client = snapshot(conn), [], FakeAps({"status": "pending"})
    summary = tick(client, alarms)
    assert len(client.calls) == 20
    assert len(alarms) == 20
    assert summary["resolved"] == []
    assert snapshot(conn) == before


@pytest.mark.parametrize("live", [True, False])
def test_alarm_only_kill_switch_writes_nothing(environment, monkeypatch, live):
    conn, store = environment
    start(store, live=live)
    monkeypatch.setenv("LEAF_BROKER_RECONCILER_ALARM_ONLY", "1")
    before, alarms = snapshot(conn), []
    summary = tick(FakeAps(), alarms)
    assert_alarm_without_writes(conn, before, summary, alarms, "alarm_only_mode")


def test_legacy_store_mode_is_a_noop(monkeypatch):
    monkeypatch.delenv("LEAF_BROKER_STORE", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("legacy tick touched a dependency")

    monkeypatch.setattr(broker, "_postgres_store", forbidden)
    client = SimpleNamespace(get_workitem_status=forbidden)
    assert reconciler.reconcile_once(
        aps_client=client, correlation=forbidden, alarm=forbidden,
    ) == {"mode": "legacy", "checked": 0}


def test_sidecar_reader_keeps_last_event_per_job_and_rejects_oversized_file(tmp_path):
    path = tmp_path / "active.jsonl"
    records = [
        {"event": "open", "job_id": "a", "workitem_id": "old", "ts": 99},
        {"event": "open", "job_id": "b", "workitem_id": "gone", "ts": 99},
        {"event": "close", "job_id": "a", "workitem_id": "old", "ts": 1},
        {"event": "open", "job_id": "a", "workitem_id": "new", "ts": 0},
        {"event": "close", "job_id": "b", "workitem_id": None, "ts": 0},
    ]
    path.write_text("\n".join(json.dumps(row) for row in records), encoding="utf-8")
    before = path.read_bytes()
    assert reconciler.read_sidecar_correlations(path) == {"a": "new"}
    assert path.read_bytes() == before
    with path.open("wb") as stream:
        stream.truncate(16 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="16 MiB"):
        reconciler.read_sidecar_correlations(path)
    path.write_text('{"event":', encoding="utf-8")
    with pytest.raises(ValueError):
        reconciler.read_sidecar_correlations(path)


@pytest.mark.parametrize("error", [HTTPException(409, "already settled"), RuntimeError("conflict")])
def test_rejected_resolution_alarms_and_continues(environment, monkeypatch, error):
    conn, store = environment
    start(store, live=False)
    start(store, "second", live=False)
    real_resolve = broker.resolve_executing_admission
    before = deepcopy(conn.admissions[EVENT_KEY])

    def reject_first(key, request):
        if key == EVENT_KEY:
            raise error
        return real_resolve(key, request)

    monkeypatch.setattr(broker, "resolve_executing_admission", reject_first)
    alarms = []
    summary = tick(FakeAps(), alarms)
    assert [record["reason"] for record in alarms] == ["resolve_rejected"]
    assert conn.admissions[EVENT_KEY] == before
    assert EVENT_KEY not in conn.ledger
    assert summary["resolved"][0]["event_key"] == "second"
    assert conn.admissions["second"]["state"] == "terminal"


@pytest.mark.parametrize("content", ['{"event":', '[]', '{"event":"open"}'])
def test_unreadable_sidecar_alarms_without_partial_correlation(
        environment, monkeypatch, tmp_path, content):
    conn, store = environment
    start(store)
    path = tmp_path / "active.jsonl"
    valid = {"event": "open", "job_id": JOB_ID, "workitem_id": "old", "ts": 1}
    path.write_text(json.dumps(valid) + "\n" + content, encoding="utf-8")
    monkeypatch.setattr(broker, "active_workitem_for", lambda job_id: None)
    monkeypatch.setattr(broker, "ACTIVE_WORKITEMS_PATH", path)
    before, alarms, client = snapshot(conn), [], FakeAps()
    summary = tick(client, alarms, correlation=None)
    assert_alarm_without_writes(conn, before, summary, alarms, "sidecar_unreadable")
    assert client.calls == []


def test_reconciler_does_not_trust_memory_after_a_corrupt_replay(
        environment, monkeypatch, tmp_path):
    conn, store = environment
    start(store)
    path = tmp_path / "active.jsonl"
    valid = {"event": "open", "job_id": JOB_ID, "workitem_id": "old", "ts": 1}
    path.write_text(json.dumps(valid) + '\n{"event":', encoding="utf-8")
    before_bytes = path.read_bytes()
    memory = Mock(return_value="old")
    read_sidecar = Mock(wraps=reconciler.read_sidecar_correlations)
    monkeypatch.setattr(broker, "active_workitem_for", memory)
    monkeypatch.setattr(broker, "ACTIVE_WORKITEMS_PATH", path)
    monkeypatch.setattr(reconciler, "read_sidecar_correlations", read_sidecar)
    before, alarms, client = snapshot(conn), [], FakeAps()
    summary = tick(client, alarms, correlation=None)
    assert_alarm_without_writes(conn, before, summary, alarms, "sidecar_unreadable")
    assert client.calls == []
    read_sidecar.assert_called_once_with(path)
    memory.assert_not_called()
    assert path.read_bytes() == before_bytes


@pytest.mark.parametrize("sidecar_workitem", ["new", None])
def test_reconciler_refuses_a_memory_correlation_the_sidecar_does_not_prove(
        environment, monkeypatch, tmp_path, sidecar_workitem):
    conn, store = environment
    start(store)
    path = tmp_path / "active.jsonl"
    records = [{"event": "open", "job_id": JOB_ID, "workitem_id": "old", "ts": 1}]
    records.append({
        "event": "close" if sidecar_workitem is None else "open",
        "job_id": JOB_ID, "workitem_id": sidecar_workitem, "ts": 2,
    })
    path.write_text("\n".join(json.dumps(row) for row in records), encoding="utf-8")
    before_bytes = path.read_bytes()
    monkeypatch.setattr(broker, "active_workitem_for", lambda job_id: "old")
    monkeypatch.setattr(broker, "ACTIVE_WORKITEMS_PATH", path)
    before, alarms, client = snapshot(conn), [], FakeAps()
    summary = tick(client, alarms, correlation=None)
    assert_alarm_without_writes(conn, before, summary, alarms, "correlation_unproven")
    assert client.calls == []
    assert path.read_bytes() == before_bytes


@pytest.mark.parametrize("in_memory", [None, "disk-workitem", "memory-workitem"])
def test_default_correlation_reads_memory_then_sidecar(
        environment, monkeypatch, tmp_path, in_memory):
    conn, store = environment
    start(store)
    path = tmp_path / "active.jsonl"
    path.write_text(json.dumps({
        "event": "open", "job_id": JOB_ID, "workitem_id": "disk-workitem", "ts": 1,
    }) + "\n", encoding="utf-8")
    before_bytes = path.read_bytes()
    monkeypatch.setattr(broker, "ACTIVE_WORKITEMS_PATH", path)
    monkeypatch.setattr(broker, "active_workitem_for", lambda job_id: in_memory)
    before, alarms, client = snapshot(conn), [], FakeAps({"status": "pending"})
    summary = tick(client, alarms, correlation=None)
    assert path.read_bytes() == before_bytes
    if in_memory == "memory-workitem":
        assert client.calls == []
        assert_alarm_without_writes(conn, before, summary, alarms, "correlation_unproven")
    else:
        assert client.calls == ["disk-workitem"]
        assert_alarm_without_writes(conn, before, summary, alarms, "aps_not_terminal")


@pytest.mark.parametrize("status_code", [200, 302, 404, 500])
def test_production_status_client_makes_one_bounded_authenticated_get(monkeypatch, status_code):
    response = Mock(status_code=status_code)
    response.json.return_value = {"status": "pending"}
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.HTTPError("status error")
    get = Mock(return_value=response)
    monkeypatch.setattr(reconciler.requests, "get", get)
    monkeypatch.setattr(broker, "_get_da", lambda: SimpleNamespace(
        DA="https://example.test/da", _auth_headers=lambda: {"Authorization": "test-only"}))
    client = reconciler.ApsWorkitemStatusClient()
    if status_code == 200:
        assert client.get_workitem_status("workitem-123") == {"status": "pending"}
    else:
        with pytest.raises(requests.HTTPError):
            client.get_workitem_status("workitem-123")
    get.assert_called_once_with(
        "https://example.test/da/workitems/workitem-123",
        headers={"Authorization": "test-only"}, timeout=10, allow_redirects=False)
    response.close.assert_called_once_with()


def test_run_forever_waits_interruptibly_and_does_not_run_after_stop(monkeypatch):
    run = Mock()
    monkeypatch.setattr(reconciler, "reconcile_once", run)
    stop = Mock()
    stop.is_set.return_value = False
    stop.wait.return_value = True
    client = FakeAps()
    reconciler.run_forever(60, stop, aps_client=client)
    run.assert_called_once_with(aps_client=client)
    stop.wait.assert_called_once_with(60)
    run.reset_mock()
    stop.is_set.return_value = True
    reconciler.run_forever(60, stop, aps_client=client)
    run.assert_not_called()


@pytest.fixture
def arming_environment(environment, monkeypatch):
    for name in (
        reconciler.ARM_ENV, reconciler.INTERVAL_ENV, reconciler.MAX_APS_CHECKS_ENV,
        "LEAF_DRAWING_STORE", "LEAF_JOBS_STORE", "LEAF_CALLBACK_REPLAY_STORE",
        "LEAF_RUNTIME_ENV", "LEAF_AUTHORED_EXECUTION",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(reconciler, "_RUNNING", None)
    return environment


def _reconciler_threads():
    return [thread for thread in threading.enumerate()
            if thread.name == reconciler.THREAD_NAME and thread.is_alive()]


@pytest.mark.parametrize("environ, expected, error", [
    pytest.param({}, None, None, id="off-empty"),
    pytest.param({reconciler.ARM_ENV: "0"}, None, None, id="off-zero"),
    pytest.param({reconciler.ARM_ENV: "  "}, None, None, id="off-blank"),
    pytest.param({reconciler.ARM_ENV: "0", reconciler.INTERVAL_ENV: "5"},
                 None, None, id="off-ignores-knobs"),
    pytest.param({reconciler.ARM_ENV: "1"},
                 {"interval_s": 300, "max_aps_checks": 20}, None, id="defaults"),
    pytest.param({reconciler.ARM_ENV: " 1 ", reconciler.INTERVAL_ENV: "60",
                  reconciler.MAX_APS_CHECKS_ENV: "1"},
                 {"interval_s": 60, "max_aps_checks": 1}, None, id="low-bounds"),
    pytest.param({reconciler.ARM_ENV: "1", reconciler.INTERVAL_ENV: "3600",
                  reconciler.MAX_APS_CHECKS_ENV: "20"},
                 {"interval_s": 3600, "max_aps_checks": 20}, None, id="high-bounds"),
    pytest.param({reconciler.ARM_ENV: "1", reconciler.INTERVAL_ENV: "",
                  reconciler.MAX_APS_CHECKS_ENV: " "},
                 {"interval_s": 300, "max_aps_checks": 20}, None, id="blank-knobs"),
    pytest.param({reconciler.ARM_ENV: "true"}, None,
                 "LEAF_BROKER_RECONCILER must be unset", id="arm-true"),
    pytest.param({reconciler.ARM_ENV: "1", reconciler.INTERVAL_ENV: "59"}, None,
                 "LEAF_BROKER_RECONCILER_INTERVAL_S must be", id="int-59"),
    pytest.param({reconciler.ARM_ENV: "1", reconciler.INTERVAL_ENV: "3601"}, None,
                 "LEAF_BROKER_RECONCILER_INTERVAL_S must be", id="int-3601"),
    pytest.param({reconciler.ARM_ENV: "1", reconciler.INTERVAL_ENV: "300.5"}, None,
                 "LEAF_BROKER_RECONCILER_INTERVAL_S must be", id="int-float"),
    pytest.param({reconciler.ARM_ENV: "1", reconciler.INTERVAL_ENV: "1e3"}, None,
                 "LEAF_BROKER_RECONCILER_INTERVAL_S must be", id="int-exp"),
    pytest.param({reconciler.ARM_ENV: "1", reconciler.INTERVAL_ENV: "nan"}, None,
                 "LEAF_BROKER_RECONCILER_INTERVAL_S must be", id="int-nan"),
    pytest.param({reconciler.ARM_ENV: "1", reconciler.INTERVAL_ENV: "\uff13\uff10\uff10"}, None,
                 "LEAF_BROKER_RECONCILER_INTERVAL_S must be", id="int-fullwidth"),
    pytest.param({reconciler.ARM_ENV: "1", reconciler.MAX_APS_CHECKS_ENV: "0"}, None,
                 "LEAF_BROKER_RECONCILER_MAX_APS_CHECKS must be", id="max-0"),
    pytest.param({reconciler.ARM_ENV: "1", reconciler.MAX_APS_CHECKS_ENV: "21"}, None,
                 "LEAF_BROKER_RECONCILER_MAX_APS_CHECKS must be", id="max-21"),
    pytest.param({reconciler.ARM_ENV: "1", reconciler.MAX_APS_CHECKS_ENV: "-1"}, None,
                 "LEAF_BROKER_RECONCILER_MAX_APS_CHECKS must be", id="max-neg"),
])
def test_reconciler_config_parser_accepts_only_bounded_integers(environ, expected, error):
    before = dict(environ)
    if error is not None:
        with pytest.raises(RuntimeError, match=error):
            reconciler.load_config(environ)
    else:
        assert reconciler.load_config(environ) == expected
    assert environ == before


def test_lifespan_arms_reconciler_and_resolves_a_stale_admission(
        arming_environment, monkeypatch):
    conn, store = arming_environment
    monkeypatch.setenv(reconciler.ARM_ENV, "1")
    start(store, "non-live-run", live=False)
    list_executing = store.list_executing

    def stale_rows(limit):
        return [dict(row, age_seconds=7200.0) for row in list_executing(limit)]

    monkeypatch.setattr(store, "list_executing", stale_rows)
    lines = []
    monkeypatch.setattr(reconciler, "_emit_line", lines.append)
    with TestClient(broker.app) as client:
        assert client.get("/broker/health").status_code == 200
        deadline = time.monotonic() + 5
        while not any(line.startswith("TICK ") for line in lines):
            remaining = deadline - time.monotonic()
            assert remaining > 0, lines
            time.sleep(min(0.01, remaining))
    assert lines == [
        'ARMED {"alarm_only": false, "interval_s": 300, "list_limit": 100, '
        '"max_aps_checks": 20, "min_age_s": 3600}',
        'RESOLVED {"event_key": "non-live-run", '
        '"evidence_ref": "non-live-admission:non-live-run", '
        '"resolution": "confirmed_failed_no_charge", "tenant_id": "tenant-a"}',
        'TICK {"alarmed": 0, "checked": 1, "resolved": 1, "skipped_young": 0}',
        'STOPPED {"thread": "leaf-broker-reconciler"}',
    ]
    assert conn.admissions["non-live-run"]["state"] == "terminal"
    assert len(conn.audits) == 1
    assert conn.audits[0]["operator_id"] == "reconciler"
    assert _reconciler_threads() == []


@pytest.mark.parametrize("arm", [None, "0"], ids=["unset", "zero"])
def test_lifespan_without_flag_starts_no_reconciler(arming_environment, monkeypatch, arm):
    conn, store = arming_environment
    if arm is not None:
        monkeypatch.setenv(reconciler.ARM_ENV, arm)
    start(store, "non-live-run", live=False)
    lines = []
    monkeypatch.setattr(reconciler, "_emit_line", lines.append)
    monkeypatch.setattr(reconciler, "start_background", pytest.fail)
    with TestClient(broker.app) as client:
        assert client.get("/broker/health").status_code == 200
        assert _reconciler_threads() == []
    assert lines == []
    assert conn.admissions["non-live-run"]["state"] == "executing"


@pytest.mark.parametrize("environ, error", [
    ({reconciler.ARM_ENV: "yes"}, "LEAF_BROKER_RECONCILER must be unset"),
    ({reconciler.ARM_ENV: "1", reconciler.INTERVAL_ENV: "59"},
     "LEAF_BROKER_RECONCILER_INTERVAL_S must be"),
    ({reconciler.ARM_ENV: "1", reconciler.MAX_APS_CHECKS_ENV: "21"},
     "LEAF_BROKER_RECONCILER_MAX_APS_CHECKS must be"),
    ({reconciler.ARM_ENV: "1", "LEAF_BROKER_STORE": "legacy"},
     "requires LEAF_BROKER_STORE=postgres"),
], ids=["arm", "interval", "batch", "legacy"])
def test_lifespan_refuses_to_start_on_malformed_reconciler_config(
        arming_environment, monkeypatch, environ, error):
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=error):
        with TestClient(broker.app):
            pytest.fail("broker started with invalid reconciler configuration")
    assert _reconciler_threads() == []


def test_armed_loop_survives_a_failing_tick_and_logs_one_line_per_action(monkeypatch):
    stop = Mock()
    stop.is_set.return_value = False
    stop.wait.side_effect = [False, True]
    client = FakeAps()
    run = Mock(side_effect=[RuntimeError("db down"), {
        "mode": "postgres", "checked": 2, "skipped_young": 1,
        "resolved": [{"event_key": "k", "tenant_id": "t",
                      "resolution": "confirmed_failed_no_charge",
                      "evidence_ref": "non-live-admission:k"}],
        "alarmed": [{"reason": "aps_not_terminal"}],
    }])
    monkeypatch.setattr(reconciler, "reconcile_once", run)
    lines = []
    monkeypatch.setattr(reconciler, "_emit_line", lines.append)
    reconciler._serve(stop, interval_s=300, max_aps_checks=7, aps_client=client)
    assert lines == [
        'TICK_FAILED {"error": "RuntimeError"}',
        'RESOLVED {"event_key": "k", "evidence_ref": "non-live-admission:k", '
        '"resolution": "confirmed_failed_no_charge", "tenant_id": "t"}',
        'TICK {"alarmed": 1, "checked": 2, "resolved": 1, "skipped_young": 1}',
        'STOPPED {"thread": "leaf-broker-reconciler"}',
    ]
    assert run.call_args_list == [
        call(aps_client=client, max_aps_checks=7),
        call(aps_client=client, max_aps_checks=7),
    ]
    assert stop.wait.call_args_list == [call(300), call(300)]
    assert all("db down" not in line for line in lines)


@pytest.mark.parametrize("age", [Decimal("7200.0"), Fraction(7200, 1)],
                         ids=["postgres-decimal", "other-number"])
@pytest.mark.parametrize("fail_alarm", [False, True], ids=["normal", "sink-failure"])
def test_armed_tick_keeps_resolution_lines_after_a_numeric_alarm(
        arming_environment, monkeypatch, age, fail_alarm):
    conn, store = arming_environment
    start(store, "non-live-run", live=False)
    start(store, "unbound-live-run", live=True)
    list_executing = store.list_executing
    monkeypatch.setattr(store, "list_executing", lambda limit: [
        dict(row, age_seconds=age) for row in list_executing(limit)])
    lines = []

    def emit(text):
        if fail_alarm and text.startswith("ALARM "):
            raise OSError("private sink details")
        lines.append(text)

    monkeypatch.setattr(reconciler, "_emit_line", emit)
    stop = Mock()
    stop.is_set.return_value = False
    stop.wait.return_value = True
    client = FakeAps()
    reconciler._serve(stop, interval_s=300, max_aps_checks=7, aps_client=client)
    alarm_line = (
        'ALARM {"event": "broker_admission_reconcile_alarm", '
        '"event_key": "unbound-live-run", "tenant_id": "tenant-a", '
        '"reason": "event_key_not_job_bound", "age_seconds": 7200.0}')
    assert lines == [
        'TICK_FAILED {"error": "OSError"}' if fail_alarm else alarm_line,
        'RESOLVED {"event_key": "non-live-run", '
        '"evidence_ref": "non-live-admission:non-live-run", '
        '"resolution": "confirmed_failed_no_charge", "tenant_id": "tenant-a"}',
        'TICK {"alarmed": 1, "checked": 2, "resolved": 1, "skipped_young": 0}',
        'STOPPED {"thread": "leaf-broker-reconciler"}',
    ]
    if not fail_alarm:
        assert not any(line.startswith("TICK_FAILED ") for line in lines)
    assert all("private sink details" not in line for line in lines)
    assert conn.admissions["non-live-run"]["state"] == "terminal"
    assert conn.admissions["unbound-live-run"]["state"] == "executing"
    assert len(conn.audits) == 1
    assert conn.audits[0]["operator_id"] == "reconciler"
    assert client.calls == []


@pytest.mark.parametrize("value", [
    Decimal("Infinity"), Decimal("-Infinity"), Decimal("NaN"),
    float("inf"), float("-inf"), float("nan"),
], ids=["decimal-inf", "decimal-neg-inf", "decimal-nan",
        "float-inf", "float-neg-inf", "float-nan"])
def test_log_json_normalizes_nested_non_finite_numbers(value):
    payload = reconciler._json_record({
        "nested": [{"age_seconds": value}, (value,)], "live": True,
    })
    assert json.loads(payload, parse_constant=lambda token: pytest.fail(token)) == {
        "nested": [{"age_seconds": None}, [None]], "live": True,
    }


def test_armed_tick_emits_strict_json_for_non_finite_alarm_ages(
        arming_environment, monkeypatch):
    conn, store = arming_environment
    start(store, "unbound-live-run", live=True)
    start(store)
    before = snapshot(conn)
    rows = [dict(row, age_seconds=Decimal("Infinity")
                 if row["event_key"] == "unbound-live-run" else Decimal("7200.0"))
            for row in store.list_executing(100)]
    monkeypatch.setattr(store, "list_executing", lambda limit: rows)
    monkeypatch.setattr(reconciler, "read_sidecar_correlations",
                        lambda path: {JOB_ID: "workitem-123"})
    monkeypatch.setattr(broker, "active_workitem_for", lambda job_id: None)

    def alarm_with_nan(workitem_id):
        # NaN cannot pass the age comparison. Supply it after eligibility so
        # this exercises the real alarm path without changing that decision.
        next(row for row in rows if row["event_key"] == EVENT_KEY)[
            "age_seconds"] = Decimal("NaN")
        return {"status": "success"}

    client = SimpleNamespace(get_workitem_status=Mock(side_effect=alarm_with_nan))
    stop = Mock()
    stop.is_set.return_value = False
    stop.wait.return_value = True
    lines = []
    monkeypatch.setattr(reconciler, "_emit_line", lines.append)
    reconciler._serve(stop, interval_s=300, max_aps_checks=7, aps_client=client)
    records = [(kind, json.loads(payload, parse_constant=lambda token: pytest.fail(token)))
               for kind, payload in (line.split(" ", 1) for line in lines)]
    assert [kind for kind, record in records] == ["ALARM", "ALARM", "TICK", "STOPPED"]
    assert {record["event_key"]: (record["reason"], record["age_seconds"])
            for kind, record in records if kind == "ALARM"} == {
        "unbound-live-run": ("event_key_not_job_bound", None),
        EVENT_KEY: ("aps_succeeded_needs_operator", None),
    }
    assert records[-2:] == [
        ("TICK", {"alarmed": 2, "checked": 2, "resolved": 0, "skipped_young": 0}),
        ("STOPPED", {"thread": reconciler.THREAD_NAME}),
    ]
    client.get_workitem_status.assert_called_once_with("workitem-123")
    assert snapshot(conn) == before


def test_cli_once_emits_strict_json_for_decimal_alarm_age(
        environment, monkeypatch, capsys):
    conn, store = environment
    start(store, "unbound-live-run", live=True)
    before = snapshot(conn)
    rows = [dict(row, age_seconds=Decimal("7200.0"))
            for row in store.list_executing(100)]
    monkeypatch.setattr(store, "list_executing", lambda limit: rows)
    client = FakeAps()
    monkeypatch.setattr(reconciler, "ApsWorkitemStatusClient", lambda: client)
    monkeypatch.setattr(reconciler.sys, "argv", ["broker_admission_reconciler", "--once"])
    assert reconciler.main() == 0
    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 1
    summary = json.loads(captured.out, parse_constant=lambda token: pytest.fail(token))
    assert summary == {
        "mode": "postgres", "checked": 1, "skipped_young": 0, "resolved": [],
        "alarmed": [{"event": "broker_admission_reconcile_alarm",
                     "event_key": "unbound-live-run", "tenant_id": "tenant-a",
                     "reason": "event_key_not_job_bound", "age_seconds": 7200.0}],
    }
    assert isinstance(summary["alarmed"][0]["age_seconds"], float)
    alarm_lines = captured.err.splitlines()
    assert len(alarm_lines) == 1
    prefix = "[leaf-broker-reconciler] ALARM "
    assert alarm_lines[0].startswith(prefix)
    assert json.loads(alarm_lines[0][len(prefix):],
                      parse_constant=lambda token: pytest.fail(token)) == summary["alarmed"][0]
    assert client.calls == []
    assert snapshot(conn) == before


def test_armed_loop_survives_a_malformed_summary(monkeypatch):
    stop = Mock()
    stop.is_set.return_value = False
    stop.wait.side_effect = [False, True]
    client = FakeAps()
    run = Mock(side_effect=[None, {
        "mode": "postgres", "checked": Decimal("2"),
        "skipped_young": Fraction(1, 1), "resolved": [], "alarmed": [],
    }])
    monkeypatch.setattr(reconciler, "reconcile_once", run)
    lines = []
    monkeypatch.setattr(reconciler, "_emit_line", lines.append)
    reconciler._serve(stop, interval_s=300, max_aps_checks=7, aps_client=client)
    assert lines == [
        'TICK_FAILED {"error": "AttributeError"}',
        'TICK {"alarmed": 0, "checked": 2.0, "resolved": 0, "skipped_young": 1.0}',
        'STOPPED {"thread": "leaf-broker-reconciler"}',
    ]
    assert run.call_args_list == [
        call(aps_client=client, max_aps_checks=7),
        call(aps_client=client, max_aps_checks=7),
    ]
    assert stop.wait.call_args_list == [call(300), call(300)]


def test_bad_resolution_log_does_not_drop_other_completed_actions(monkeypatch):
    stop = Mock()
    stop.is_set.return_value = False
    stop.wait.return_value = True
    monkeypatch.setattr(reconciler, "reconcile_once", Mock(return_value={
        "checked": 3, "skipped_young": 0, "alarmed": [],
        "resolved": [{"event_key": "first"}, {"bad": object()},
                     {"event_key": "last", "live": False, "age": Decimal("7200.0")}],
    }))
    lines = []
    monkeypatch.setattr(reconciler, "_emit_line", lines.append)
    reconciler._serve(stop, interval_s=300, max_aps_checks=7, aps_client=FakeAps())
    assert lines == [
        'RESOLVED {"event_key": "first"}',
        'TICK_FAILED {"error": "TypeError"}',
        'RESOLVED {"age": 7200.0, "event_key": "last", "live": false}',
        'TICK {"alarmed": 0, "checked": 3, "resolved": 3, "skipped_young": 0}',
        'STOPPED {"thread": "leaf-broker-reconciler"}',
    ]


def test_concurrent_starts_arm_exactly_one_loop(arming_environment, monkeypatch):
    callers_ready = threading.Barrier(3, timeout=2)
    constructors_ready = threading.Barrier(2, timeout=0.5)
    handles, errors, lines = [None, None], [], []
    run = Mock(return_value={"mode": "postgres", "checked": 0, "skipped_young": 0,
                             "resolved": [], "alarmed": []})
    monkeypatch.setattr(reconciler, "reconcile_once", run)
    monkeypatch.setattr(reconciler, "_emit_line", lines.append)

    def make_client():
        # Pause after the liveness check and before publishing _RUNNING. With
        # the lock only one caller gets here; without it both cross this barrier.
        try:
            constructors_ready.wait()
        except threading.BrokenBarrierError:
            pass
        return FakeAps()

    monkeypatch.setattr(reconciler, "ApsWorkitemStatusClient", make_client)

    def start_racing(index):
        try:
            callers_ready.wait()
            handles[index] = reconciler.start_background({reconciler.ARM_ENV: "1"})
        except Exception as exc:
            errors.append(exc)

    callers = [threading.Thread(target=start_racing, args=(index,)) for index in range(2)]
    try:
        for caller in callers:
            caller.start()
        callers_ready.wait()
        deadline = time.monotonic() + 3
        for caller in callers:
            caller.join(max(0, deadline - time.monotonic()))
        assert not any(caller.is_alive() for caller in callers)
        assert errors == []
        armed = [handle for handle in handles if handle is not None]
        assert len(armed) == 1
        assert _reconciler_threads() == [armed[0].thread]
        assert sum(line.startswith("ARMED ") for line in lines) == 1
        assert lines.count('NOT_ARMED {"reason": "already_running"}') == 1
    finally:
        callers_ready.abort()
        constructors_ready.abort()
        for caller in callers:
            if caller.ident is not None:
                caller.join(2)
        for handle in handles:
            if handle is not None:
                assert handle.stop(timeout=2) is True
    assert _reconciler_threads() == []


def test_second_start_in_one_process_does_not_start_a_second_loop(
        arming_environment, monkeypatch):
    run = Mock(return_value={"mode": "postgres", "checked": 0, "skipped_young": 0,
                             "resolved": [], "alarmed": []})
    monkeypatch.setattr(reconciler, "reconcile_once", run)
    lines = []
    monkeypatch.setattr(reconciler, "_emit_line", lines.append)
    first = second = third = None
    try:
        first = reconciler.start_background({reconciler.ARM_ENV: "1"}, aps_client=FakeAps())
        assert isinstance(first, reconciler.ReconcilerHandle)
        assert first.thread.daemon is True
        second = reconciler.start_background({reconciler.ARM_ENV: "1"}, aps_client=FakeAps())
        assert second is None
        assert 'NOT_ARMED {"reason": "already_running"}' in lines
        assert _reconciler_threads() == [first.thread]
        assert first.stop() is True
        third = reconciler.start_background({reconciler.ARM_ENV: "1"}, aps_client=FakeAps())
        assert isinstance(third, reconciler.ReconcilerHandle)
        assert third.stop() is True
    finally:
        for handle in (first, second, third):
            if handle is not None:
                handle.stop(timeout=5)
    assert _reconciler_threads() == []


def test_stop_is_bounded_when_a_tick_hangs(arming_environment, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    thread_errors = []
    monkeypatch.setattr(threading, "excepthook", thread_errors.append)

    def hang(*, aps_client, max_aps_checks):
        entered.set()
        release.wait(5)
        return {"mode": "postgres", "checked": 0, "skipped_young": 0,
                "resolved": [], "alarmed": []}

    run = Mock(side_effect=hang)
    monkeypatch.setattr(reconciler, "reconcile_once", run)
    lines = []
    monkeypatch.setattr(reconciler, "_emit_line", lines.append)
    handle = second = None
    try:
        handle = reconciler.start_background({reconciler.ARM_ENV: "1"}, aps_client=FakeAps())
        assert isinstance(handle, reconciler.ReconcilerHandle)
        assert entered.wait(5)
        assert handle.stop(timeout=0.2) is False
        assert 'STOP_TIMEOUT {"join_timeout_s": 0.2}' in lines
        second = reconciler.start_background({reconciler.ARM_ENV: "1"}, aps_client=FakeAps())
        assert second is None
        assert 'NOT_ARMED {"reason": "already_running"}' in lines
    finally:
        release.set()
        for current in (handle, second):
            if current is not None:
                current.stop(timeout=5)
    assert not handle.thread.is_alive()
    assert lines[-2:] == [
        'TICK {"alarmed": 0, "checked": 0, "resolved": 0, "skipped_young": 0}',
        'STOPPED {"thread": "leaf-broker-reconciler"}',
    ]
    assert not any(line.startswith("TICK_FAILED ") for line in lines)
    assert run.call_count == 1
    assert thread_errors == []
    assert _reconciler_threads() == []
