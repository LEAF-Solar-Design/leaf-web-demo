"""Offline contracts for the APS-submitted, ledger-not-written crash window."""
import json
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import broker
import broker_pg_store
import broker_admission_reconciler as reconciler
import test_broker_pg_store as pg_fixtures
from envelopes import ErrorCode
from test_broker_admission_reconciler import FakeAps, environment as pg_environment


JOB_ID = "12345678-1234-1234-1234-123456789abc"
EVENT_KEY = f"{JOB_ID}:broker-run"
WORKITEM_ID = "workitem-crash-window"
TENANT = "tenant-a"


class SimulatedCrash(BaseException):
    """Escape the broker's Exception handlers without killing the test process."""


@pytest.fixture
def offline_run(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "ACTIVE_WORKITEMS_PATH", tmp_path / "workitems.jsonl")
    monkeypatch.setattr(broker, "_active_workitems", {})
    monkeypatch.setattr(broker, "_sidecar_lines", 0)
    monkeypatch.setattr(broker, "_aps_bind_store", lambda: None)
    monkeypatch.setattr(broker, "_get_reaper", lambda: None)
    monkeypatch.setattr(broker, "_get_da", lambda: pytest.fail("real APS client requested"))
    import requests
    monkeypatch.setattr(requests.sessions.Session, "request",
                        lambda *a, **k: pytest.fail("network access forbidden"))
    monkeypatch.setattr(broker, "_tenant_tier", lambda tenant: "test")
    monkeypatch.setattr(broker, "_get_usage", lambda: SimpleNamespace(
        DEFAULT_EST_USD=0.02, cap_for=lambda tenant: 1000,
        daily_run_limit_for=lambda tier: 1000))

    def execute(req, tool, engine_op, t0, entry, *, quota_reserved,
                admission, run_token):
        broker._start_admitted_execution(req, admission)
        on_submitted = broker._submission_recorder(req, run_token)
        on_submitted(WORKITEM_ID)
        # Supply the failure envelope needed to reach complete_run in finally.
        # BaseException here would instead trigger "no terminal result" there.
        raise RuntimeError("fake poll interrupted after APS submission")

    monkeypatch.setattr(broker, "_execute", execute)
    return broker.BrokerRunRequest(
        tenant_id=TENANT, job_id=JOB_ID, ledger_event_key=EVENT_KEY,
        tool={"name": "crash-window", "engine_op": "count"}, aps_live=True)


def _crash(monkeypatch, request, store=None):
    def interrupted_write(*args, **kwargs):
        raise SimulatedCrash("process lost before ledger write")

    # Restore the real writer before recovery or duplicate-completion checks.
    with monkeypatch.context() as crash_patch:
        if store is None:
            crash_patch.setattr(broker, "_ledger_append", interrupted_write)
        else:
            crash_patch.setattr(store, "complete_run", interrupted_write)
        with pytest.raises(SimulatedCrash):
            broker._broker_run(request)
    assert reconciler.read_sidecar_correlations(broker.ACTIVE_WORKITEMS_PATH) == {
        JOB_ID: WORKITEM_ID}


@pytest.fixture
def crashed_postgres(pg_environment, offline_run, monkeypatch):
    conn, store = pg_environment
    _crash(monkeypatch, offline_run, store)
    assert conn.admissions[EVENT_KEY]["state"] == "executing"
    assert conn.admissions[EVENT_KEY]["reserved_usd"] == 0.02
    assert conn.ledger == {}
    return conn, store


def _reconcile(monkeypatch, store, status):
    # The shared SQL fake reports 60 seconds regardless of execution_started_at.
    # Age its returned rows past the real default without sleeping or changing it.
    list_executing = store.list_executing

    def aged_rows(limit=100):
        return [dict(row, age_seconds=reconciler.DEFAULT_MIN_AGE_S + 1)
                for row in list_executing(limit)]

    alarms = []
    client = FakeAps({"status": status})
    with monkeypatch.context() as age_patch:
        age_patch.setattr(store, "list_executing", aged_rows)
        summary = reconciler.reconcile_once(aps_client=client, alarm=alarms.append)
    assert summary["alarmed"] == alarms
    return summary, alarms, client


def test_legacy_mode_crash_window_loses_the_line_known_residual(offline_run, monkeypatch):
    """Documented legacy residual: _broker_run_request, broker.py:3142, loses
    the line when _ledger_append crashes. BaseException in execution alone
    would still run finally and write; the crash therefore lands at the writer.
    """
    monkeypatch.setenv("LEAF_BROKER_STORE", "legacy")
    _crash(monkeypatch, offline_run)
    lines = (broker.LEDGER_PATH.read_text(encoding="utf-8").splitlines()
             if broker.LEDGER_PATH.exists() else [])
    assert lines == []


def test_postgres_mode_crash_leaves_executing_admission_and_no_row(crashed_postgres):
    """Crash at _broker_run_request, broker.py:3132, before complete_run writes.
    Finally runs even for BaseException, so the writer raises SimulatedCrash;
    a fake poll failure first supplies the terminal envelope it requires.
    """
    conn, _store = crashed_postgres
    assert conn.admissions[EVENT_KEY]["state"] == "executing"
    assert conn.admissions[EVENT_KEY]["reserved_usd"] == 0.02
    assert EVENT_KEY not in conn.ledger
    assert reconciler.read_sidecar_correlations(broker.ACTIVE_WORKITEMS_PATH)[JOB_ID] == WORKITEM_ID


def test_reconcile_terminal_failure_writes_exactly_one_row(crashed_postgres, monkeypatch):
    conn, store = crashed_postgres
    assert "failedInstructions" in reconciler._TERMINAL_FAILURES
    summary, alarms, client = _reconcile(monkeypatch, store, "failedInstructions")
    assert len(summary["resolved"]) == 1
    assert alarms == []
    assert client.calls == [WORKITEM_ID]
    assert list(conn.ledger) == [EVENT_KEY]
    assert conn.ledger[EVENT_KEY]["usd_est"] is None
    assert conn.ledger[EVENT_KEY]["status"] == "WORKITEM_FAILED"
    assert conn.admissions[EVENT_KEY]["state"] == "terminal"
    assert conn.admissions[EVENT_KEY]["reserved_usd"] == 0


def test_reconcile_is_idempotent_on_second_pass(crashed_postgres, monkeypatch):
    conn, store = crashed_postgres
    first, alarms, _client = _reconcile(monkeypatch, store, "failedInstructions")
    assert len(first["resolved"]) == 1 and alarms == []
    original = dict(conn.ledger[EVENT_KEY])
    second, alarms, client = _reconcile(monkeypatch, store, "failedInstructions")
    assert second["resolved"] == []
    assert alarms == client.calls == []
    assert conn.ledger == {EVENT_KEY: original}
    assert len(conn.audits) == 1


def test_reconcile_success_alarms_and_writes_nothing(crashed_postgres, monkeypatch):
    """Pin the pre-write poll-failure path with WorkItem correlation retained.

    APS reports success during recovery, after the broker's poll failed.
    """
    conn, store = crashed_postgres
    for _ in range(2):
        summary, alarms, client = _reconcile(monkeypatch, store, "success")
        assert summary["resolved"] == []
        assert [alarm["reason"] for alarm in alarms] == ["aps_succeeded_needs_operator"]
        assert alarms[0]["event_key"] == EVENT_KEY
        assert alarms[0]["tenant_id"] == TENANT
        assert client.calls == [WORKITEM_ID]
        assert conn.ledger == {}
        assert conn.admissions[EVENT_KEY]["state"] == "executing"
        assert conn.admissions[EVENT_KEY]["reserved_usd"] == 0.02


@pytest.mark.parametrize("point", [
    "before_intent_write", "after_intent_update_before_commit",
    "after_intent_commit_before_ack", "after_intent_commit_before_close",
    "after_close_before_settle", "after_ledger_insert", "after_terminal_update",
    "after_slot_release", "after_recovery_audit_insert",
    "after_settlement_commit_before_ack", "before_response_delivery",
])
def test_paid_success_terminal_intent_crash_matrix_recovers_exactly_once(
        pg_environment, offline_run, monkeypatch, point):
    conn, store = pg_environment
    monkeypatch.setattr(broker, "_execute", _execute_paid_success)
    captured = {}
    stage = store.stage_success_intent
    settle = store.settle_success_intent
    def crash(*a, **kw):
        raise SimulatedCrash(point)
    def staging(key, tenant, token, intent):
        captured.update(deepcopy(intent))
        if point == "before_intent_write":
            crash()
        if point == "after_intent_commit_before_ack":
            store._db.lose_ack = True
        return stage(key, tenant, token, intent)
    def settling(*a, **kw):
        if point == "after_settlement_commit_before_ack":
            store._db.lose_ack = True
        return settle(*a, **kw)
    writes = {
        "after_intent_update_before_commit": "terminal_intent_json =",
        "after_ledger_insert": "INSERT INTO broker_usage_ledger",
        "after_terminal_update": "SET state = 'terminal'",
        "after_slot_release": "UPDATE broker_aps_slots",
    }
    conn.fail_after = writes.get(point)
    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(store, "stage_success_intent", staging)
        crash_patch.setattr(store, "settle_success_intent", settling)
        if point == "after_intent_commit_before_close":
            crash_patch.setattr(broker, "_drop_active_workitem", crash)
        if point in {"after_close_before_settle", "after_recovery_audit_insert"}:
            crash_patch.setattr(store, "settle_success_intent", crash)
        if point == "before_response_delivery":
            crash_patch.setattr(broker, "_emit_aps_metric", crash)
        with pytest.raises((SimulatedCrash, pg_fixtures._TransactionCrash)):
            broker._broker_run(offline_run)
    conn.fail_after = None
    precommit = point in {"before_intent_write", "after_intent_update_before_commit"}
    completed = point in {"after_settlement_commit_before_ack", "before_response_delivery"}
    row = conn.admissions[EVENT_KEY]
    assert row["terminal_intent_json"] == (None if precommit else captured)
    if not completed:
        assert row["state"] == "executing" and row["reserved_usd"] == 0.02
        assert conn.ledger == {}
        assert conn.slots[EVENT_KEY]["state"] == "held"
    if precommit or point in {"after_intent_commit_before_ack", "after_intent_commit_before_close"}:
        assert broker.active_workitem_for(JOB_ID) == WORKITEM_ID
        assert reconciler.read_sidecar_correlations(broker.ACTIVE_WORKITEMS_PATH)[JOB_ID] == WORKITEM_ID
    else:
        assert broker.active_workitem_for(JOB_ID) is None
        assert JOB_ID not in reconciler.read_sidecar_correlations(broker.ACTIVE_WORKITEMS_PATH)
    # Recreate the process-facing store and discard all correlation memory.
    broker._active_workitems.clear()
    monkeypatch.setattr(reconciler, "_QUEUE_POSITIONS", {})
    monkeypatch.setattr(reconciler, "_QUEUE_COUNTER", 0)
    restarted = broker_pg_store.PostgresBrokerStore(store._db)
    monkeypatch.setattr(broker, "_pg_store", restarted)
    def forbidden(*a, **kw):
        pytest.fail("intent recovery touched external execution or correlation")
    monkeypatch.setattr(broker, "_execute", forbidden)
    monkeypatch.setattr(broker, "_get_da", forbidden)
    monkeypatch.setattr(broker, "_get_reaper", forbidden)
    client = SimpleNamespace(get_workitem_status=forbidden, cancel_workitem=forbidden)
    if precommit:
        # Without committed evidence APS success remains an operator case.
        summary, alarms, aps = _reconcile(monkeypatch, restarted, "success")
        assert summary["resolved"] == [] and conn.ledger == {}
        assert alarms[0]["reason"] == "aps_succeeded_needs_operator"
        return
    if point == "after_recovery_audit_insert":
        conn.fail_after = "INSERT INTO broker_admission_resolution_audit"
        before = deepcopy(vars(conn))
        with pytest.raises(pg_fixtures._TransactionCrash):
            reconciler.reconcile_once(aps_client=client, correlation=forbidden, min_age_s=0)
        assert vars(conn) == before
        conn.fail_after = None
    for _ in range(2):
        summary = reconciler.reconcile_once(aps_client=client, correlation=forbidden, min_age_s=0)
        assert summary["alarmed"] == []
    pg_fixtures._assert_intent_settled(conn, captured, audits=0 if completed else 1)
    before = deepcopy(vars(conn))
    response = broker._broker_run(offline_run)
    assert response.status_code == captured["http_status"]
    assert json.loads(response.body) == captured["result"]
    assert vars(conn) == before


def _execute_paid_success(req, tool, engine_op, t0, entry, *, quota_reserved,
                          admission, run_token):
    broker._start_admitted_execution(req, admission)
    broker._submission_recorder(req, run_token)(WORKITEM_ID)
    entry.update(engine_seconds=1.0, usd_est=0.02)
    return {"ok": True, "degraded_mode": False, "result": {"paid": "original"},
            "cost": {"engine_seconds": 1.0, "usd_est": 0.02}}, 200


def test_failed_intent_stage_never_closes_or_reaps_successful_workitem(
        pg_environment, offline_run, monkeypatch):
    conn, store = pg_environment
    monkeypatch.setattr(broker, "_execute", _execute_paid_success)
    def fail(*a, **kw):
        raise RuntimeError("staging unavailable")
    def forbidden(*a, **kw):
        pytest.fail("successful evidence was replaced or cleaned up")
    monkeypatch.setattr(store, "stage_success_intent", fail)
    monkeypatch.setattr(store, "complete_run", forbidden)
    monkeypatch.setattr(broker, "_reap_or_disown_own_workitem", forbidden)
    with pytest.raises(RuntimeError, match="staging unavailable"):
        broker._broker_run(offline_run)
    assert broker.active_workitem_for(JOB_ID) == WORKITEM_ID
    assert conn.admissions[EVENT_KEY]["reserved_usd"] == 0.02 and conn.ledger == {}


@pytest.mark.parametrize("reason", ["null_cost", "missing_owned_correlation"])
@pytest.mark.parametrize("write_fails", [False, True])
def test_ineligible_success_commits_before_correlation_close(
        pg_environment, offline_run, monkeypatch, reason, write_fails):
    conn, store = pg_environment
    def execute(*args, **kwargs):
        result, status = _execute_paid_success(*args, **kwargs)
        if reason == "null_cost":
            args[4].update(engine_seconds=None, usd_est=None)
            result["cost"] = None
        else:
            # Another invocation owns this correlation; this one cannot borrow it.
            with broker._active_workitems_lock:
                broker._active_workitems[JOB_ID] = (WORKITEM_ID, "other-owner", 1)
        return result, status
    monkeypatch.setattr(broker, "_execute", execute)
    complete = store.complete_run
    drop = broker._drop_active_workitem
    events = []
    def writing(*a, **kw):
        events.append("complete")
        assert broker.active_workitem_for(JOB_ID) == WORKITEM_ID
        if write_fails:
            raise SimulatedCrash()
        return complete(*a, **kw)
    def closing(*a, **kw):
        events.append("close")
        assert conn.admissions[EVENT_KEY]["state"] == "terminal"
        return drop(*a, **kw)
    monkeypatch.setattr(store, "complete_run", writing)
    monkeypatch.setattr(broker, "_drop_active_workitem", closing)
    if write_fails:
        with pytest.raises(SimulatedCrash):
            broker._broker_run(offline_run)
        assert events == ["complete"] and broker.active_workitem_for(JOB_ID) == WORKITEM_ID
    else:
        broker._broker_run(offline_run)
        assert events == ["complete", "close"]
        if reason == "missing_owned_correlation":
            assert broker.active_workitem_for(JOB_ID) == WORKITEM_ID


def test_duplicate_delivery_cannot_close_intent_owners_workitem(
        pg_environment, offline_run, monkeypatch):
    conn, store = pg_environment
    owner = pg_fixtures._admit(store, EVENT_KEY, request_fingerprint=broker._broker_request_fingerprint(offline_run))
    store.mark_execution_started(EVENT_KEY, TENANT, owner["lease_token"],
                                  aps_live=True, max_concurrency=2, slot_lease_seconds=60)
    broker._submission_recorder(offline_run, "original-owner")(WORKITEM_ID)
    def forbidden(*a, **kw):
        pytest.fail("duplicate attempted work or intent staging")
    monkeypatch.setattr(store, "stage_success_intent", forbidden)
    monkeypatch.setattr(broker, "_execute", forbidden)
    response = broker._broker_run(offline_run)
    assert json.loads(response.body)["error"]["error_code"] == ErrorCode.TURN_IN_PROGRESS
    assert broker.active_workitem_for(JOB_ID) == WORKITEM_ID
    assert conn.ledger == {} and conn.admissions[EVENT_KEY]["terminal_intent_json"] is None


def test_complete_run_duplicate_event_key_is_a_single_row(pg_environment):
    conn, store = pg_environment
    admission = pg_fixtures._admit(store, EVENT_KEY)
    assert admission["status"] == "acquired"
    entry = dict(pg_fixtures._entry(), job_id=JOB_ID)
    for _ in range(2):
        store.complete_run(EVENT_KEY, TENANT, admission["lease_token"], entry, {"ok": True}, 200)
    assert list(conn.ledger) == [EVENT_KEY]
    assert store.aggregate_usage(TENANT)["total"] == {"runs": 1, "usd_est": 0.02}
    assert store.aggregate_usage("other-tenant")["total"]["runs"] == 0


def test_fallback_event_key_is_a_distinct_row_by_design(pg_environment):
    """Pin current behaviour of jobs.py's run/fallback keys, not a verdict."""
    conn, store = pg_environment
    keys = [EVENT_KEY, f"{JOB_ID}:broker-fallback"]
    for key in keys:
        admission = pg_fixtures._admit(store, key)
        assert admission["status"] == "acquired"
        store.complete_run(key, TENANT, admission["lease_token"],
                           dict(pg_fixtures._entry(), job_id=JOB_ID), {"ok": True}, 200)
    assert set(conn.ledger) == set(keys)
    assert store.aggregate_usage(TENANT)["total"] == {"runs": 2, "usd_est": 0.04}


def test_postgres_mode_rejects_run_without_event_key(pg_environment, offline_run):
    conn, _store = pg_environment
    offline_run.ledger_event_key = None
    response = broker._broker_run(offline_run)
    assert response.status_code == 400
    assert json.loads(response.body)["error"]["error_code"] == "BAD_PARAMS"
    assert conn.admissions == {}
    assert conn.ledger == {}
    assert not broker.ACTIVE_WORKITEMS_PATH.exists()
