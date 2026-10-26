"""Unsettled worker exits emit one allowlisted signal without changing custody."""
import json
from types import SimpleNamespace
from uuid import UUID

import pytest

import solar_project_jobs as service


PREFIX = "LEAF_GRAPH_JOB_EVENT "
SECRET = "SECRET-MARKER-W1C24"
JOB_ID = UUID("a45e574a-0000-4000-8000-000000000001")
OWNER = "canonical-worker:404"
TOOL = "solar-settings"


def failure(reason=None, *, code=None):
    exc = RuntimeError(SECRET)
    if reason is not None:
        exc.reason_code = reason
    if code is not None:
        exc.code = code
    return exc


@pytest.fixture
def worker(monkeypatch):
    state = SimpleNamespace(
        job={"job_id": JOB_ID, "attempt": 3, "params": {"secret": SECRET},
             "execution_context": {"secret": SECRET}},
        prepared=object(), prepare_error=None, persist_error=None, complete_error=None,
        fail_outcome="failed", complete_outcome=("applied", None), lost=False, calls=[],
        now=100.0, renewals=0, last_renewal_at=None)
    monkeypatch.setattr(service.time, "monotonic", lambda: state.now)

    def claim(owner, *, tool_name, lease_seconds):
        assert owner == OWNER
        assert tool_name in (TOOL, service.solve.TOOL_NAME)
        assert lease_seconds == 30.0
        state.calls.append("claim")
        return state.job

    def heartbeat(*args, **kwargs):
        return True

    def fail(job_id, owner, attempt, error):
        assert (job_id, owner, attempt) == (JOB_ID, OWNER, 3)
        assert error == service._preparation_error(state.prepare_error)
        state.calls.append("fail")
        if state.persist_error is not None:
            raise state.persist_error
        return state.fail_outcome

    def prepare(job, **kwargs):
        assert job is state.job
        state.calls.append("prepare")
        state.now += 2.0
        if state.prepare_error is not None:
            raise state.prepare_error
        return state.prepared

    def complete(job_id, owner, attempt, prepared, **kwargs):
        assert (job_id, owner, attempt) == (JOB_ID, OWNER, 3)
        assert prepared is state.prepared
        state.calls.append("complete")
        state.now += 3.0
        if state.complete_error is not None:
            raise state.complete_error
        return state.complete_outcome

    jobs = SimpleNamespace(claim_project_graph_job=claim, heartbeat_project_graph_job=heartbeat,
                           fail_project_graph_job=fail, complete_project_solve_job=complete)

    class Guard:
        def __init__(self, *args, **kwargs):
            assert args == (jobs, JOB_ID, OWNER, 3, 30.0)

        @property
        def lost(self):
            return state.lost

        def start(self):
            state.calls.append("start")

        def close(self):
            state.calls.append("close")
            state.now += 0.5
            self.renewals = state.renewals
            self.last_renewal_at = state.last_renewal_at

    monkeypatch.setattr(service, "_jobs", lambda: jobs)
    monkeypatch.setattr(service, "GraphLeaseGuard", Guard)
    monkeypatch.setattr(service.platform_link, "platform_db", lambda: SimpleNamespace(
        run_transaction=lambda operation: operation(object())))
    monkeypatch.setattr(service, "prepare_project_graph_job", prepare)
    monkeypatch.setattr(service.solve, "prepare_job", lambda job, guard: prepare(job))
    monkeypatch.setattr(service, "complete_project_graph_job", complete)
    return state


def assert_event(capsys, phase, *, tool=TOOL, exception=None, reason=None, outcome=None,
                 renewals=0, last_renewal_age_ms=2500.0):
    captured = capsys.readouterr()
    assert captured.out == ""
    assert SECRET not in captured.err
    lines = captured.err.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(PREFIX)
    expected = {"event": "graph_job_unsettled", "phase": phase, "job_id": str(JOB_ID),
                "tool_name": tool, "attempt": 3, "worker_id": OWNER,
                "exception": exception, "reason_code": reason, "outcome": outcome,
                "prepare_ms": 2000.0,
                "complete_ms": 3000.0 if phase.startswith("complete_") else None,
                "renewals": renewals, "last_renewal_age_ms": last_renewal_age_ms}
    assert json.loads(lines[0][len(PREFIX):]) == expected
    assert captured.err == PREFIX + json.dumps(expected, separators=(",", ":"), sort_keys=True) + "\n"


@pytest.mark.parametrize("reason,code,expected", [
    ("SIP_R1_CHECKOUT_STALE", None, "SIP_R1_CHECKOUT_STALE"),
    (None, None, None),
    (None, "STORE_UNAVAILABLE", "STORE_UNAVAILABLE"),
    (42, "IGNORED_FALLBACK", None),
])
def test_fail_persist_error(worker, capsys, reason, code, expected):
    worker.prepare_error = ValueError(SECRET)
    worker.persist_error = failure(reason, code=code)
    assert service.run_once(OWNER, tool_name=TOOL) is True
    assert_event(capsys, "fail_persist_error", exception="RuntimeError", reason=expected)
    assert worker.calls == ["claim", "start", "prepare", "close", "fail"]


@pytest.mark.parametrize("outcome", ["not_owner", "missing", "conflict"])
def test_fail_not_settled(worker, capsys, outcome):
    worker.prepare_error = failure()
    worker.fail_outcome = outcome
    assert service.run_once(OWNER, tool_name=TOOL) is True
    assert_event(capsys, "fail_not_settled", outcome=outcome)
    assert worker.calls == ["claim", "start", "prepare", "close", "fail"]


@pytest.mark.parametrize("preparation", ["raised", "returned", "none"])
def test_lease_lost(worker, capsys, preparation):
    worker.lost = True
    if preparation == "raised":
        worker.prepare_error = failure()
    elif preparation == "none":
        worker.prepared = None
    assert service.run_once(OWNER, tool_name=TOOL) is True
    assert_event(capsys, "lease_lost")
    assert worker.calls == ["claim", "start", "prepare", "close"]


def test_prepared_none(worker, capsys):
    worker.prepared = None
    assert service.run_once(OWNER, tool_name=TOOL) is True
    assert_event(capsys, "prepared_none")
    assert worker.calls == ["claim", "start", "prepare", "close"]


@pytest.mark.parametrize("tool", [TOOL, service.solve.TOOL_NAME])
@pytest.mark.parametrize("reason,code,expected", [
    ("SIP_R1_CHECKOUT_STALE", None, "SIP_R1_CHECKOUT_STALE"),
    (None, None, None),
    (None, "STORE_UNAVAILABLE", "STORE_UNAVAILABLE"),
    (42, "IGNORED_FALLBACK", None),
])
def test_complete_error(worker, capsys, tool, reason, code, expected):
    worker.complete_error = failure(reason, code=code)
    assert service.run_once(OWNER, tool_name=tool) is True
    assert_event(capsys, "complete_error", tool=tool, exception="RuntimeError", reason=expected)
    assert worker.calls == ["claim", "start", "prepare", "close", "complete"]


@pytest.mark.parametrize("tool", [TOOL, service.solve.TOOL_NAME])
@pytest.mark.parametrize("outcome", [("not_owner", None), ["missing", None], ("conflict", None)])
@pytest.mark.parametrize("renewals", [0, 2])
def test_complete_not_settled(worker, capsys, tool, outcome, renewals):
    worker.complete_outcome = outcome
    worker.renewals = renewals
    worker.last_renewal_at = 101.0 if renewals else None
    assert service.run_once(OWNER, tool_name=tool) is True
    assert_event(capsys, "complete_not_settled", tool=tool, outcome=outcome[0],
                 renewals=renewals, last_renewal_age_ms=1500.0 if renewals else 2500.0)
    assert worker.calls == ["claim", "start", "prepare", "close", "complete"]


def test_no_claim(worker, capsys):
    worker.job = None
    assert service.run_once(OWNER, tool_name=TOOL) is False
    assert capsys.readouterr() == ("", "")
    assert worker.calls == ["claim"]


@pytest.mark.parametrize("outcome", ["failed", "retry", "duplicate"])
def test_fail_settled_is_silent(worker, capsys, outcome):
    worker.prepare_error = failure()
    worker.fail_outcome = outcome
    assert service.run_once(OWNER, tool_name=TOOL) is True
    assert capsys.readouterr() == ("", "")
    assert worker.calls == ["claim", "start", "prepare", "close", "fail"]


@pytest.mark.parametrize("outcome", [None, True, ("not_owner", None)])
def test_fail_other_shape_is_unsettled(worker, capsys, outcome):
    worker.prepare_error = failure()
    worker.fail_outcome = outcome
    assert service.run_once(OWNER, tool_name=TOOL) is True
    assert_event(capsys, "fail_not_settled")
    assert worker.calls == ["claim", "start", "prepare", "close", "fail"]


@pytest.mark.parametrize("tool", [TOOL, service.solve.TOOL_NAME])
@pytest.mark.parametrize("outcome", [("applied", None), ["duplicate", None]])
def test_complete_settled_is_silent(worker, capsys, tool, outcome):
    worker.complete_outcome = outcome
    assert service.run_once(OWNER, tool_name=tool) is True
    assert capsys.readouterr() == ("", "")
    assert worker.calls == ["claim", "start", "prepare", "close", "complete"]


@pytest.mark.parametrize("tool", [TOOL, service.solve.TOOL_NAME])
@pytest.mark.parametrize("outcome,expected", [
    (None, None), (True, None), ("not_owner", "not_owner"),
    (("not_owner",), None), (("not_owner", None, None), None),
    ((42, None), None), ({"outcome": "not_owner"}, None),
    (("applied",), None), ([1, 2], None),
])
def test_complete_other_shape_is_unsettled(worker, capsys, tool, outcome, expected):
    worker.complete_outcome = outcome
    assert service.run_once(OWNER, tool_name=tool) is True
    assert_event(capsys, "complete_not_settled", tool=tool, outcome=expected)
    assert worker.calls == ["claim", "start", "prepare", "close", "complete"]


@pytest.mark.parametrize("broken", [None, "write", "flush"])
def test_event_is_flushed_and_stream_errors_are_swallowed(worker, monkeypatch, broken):
    worker.prepared = None
    calls = []

    class Stream:
        def write(self, line):
            calls.append("write")
            assert line.startswith(PREFIX) and line.endswith("\n")
            if broken == "write":
                raise OSError(SECRET)

        def flush(self):
            calls.append("flush")
            if broken == "flush":
                raise OSError(SECRET)

    monkeypatch.setattr(service.sys, "stderr", Stream())
    assert service.run_once(OWNER, tool_name=TOOL) is True
    assert calls == (["write"] if broken == "write" else ["write", "flush"])


@pytest.mark.parametrize("failure_mode", ["refused", "raised", "callback"])
def test_guard_records_only_successful_renewals(monkeypatch, failure_mode):
    now = [100.0]
    monkeypatch.setattr(service.time, "monotonic", lambda: now[0])
    calls = []

    def heartbeat(*args, **kwargs):
        calls.append((args, kwargs))
        now[0] += 1.0
        if len(calls) == 3:
            if failure_mode == "raised":
                raise ConnectionError(SECRET)
            return failure_mode == "callback"
        return True

    def on_renew():
        if len(calls) == 3:
            raise ConnectionError(SECRET)

    guard = service.GraphLeaseGuard(SimpleNamespace(heartbeat_project_graph_job=heartbeat),
        JOB_ID, OWNER, 3, 30.0, on_renew=on_renew)
    assert guard.renewals == 0 and guard.last_renewal_at is None
    guard._stop = SimpleNamespace(wait=lambda interval: False)
    guard._renew()
    assert guard.lost
    assert calls == [((JOB_ID, OWNER, 3), {"lease_seconds": 30.0})] * 3
    assert guard.renewals == (3 if failure_mode == "callback" else 2)
    assert guard.last_renewal_at == (103.0 if failure_mode == "callback" else 102.0)
