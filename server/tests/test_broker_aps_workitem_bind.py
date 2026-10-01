"""Live dispatch binds the APS WorkItem to its PostgreSQL job row (P-079).

`PostgresJobStore.bind_aps_workitem` is the dispatch prerequisite for a future
callback completion: it records which WorkItem the current live attempt owns.
The broker calls it from the run's `on_submitted` hook, once per attempt, with
the (attempt, lease owner) snapshot taken at dispatch. It is inert for
completion: polling stays the only mode while callback-primary is refused, so
a refused bind (stale attempt, wrong owner) changes nothing about the run.

Every test is offline. The store is a fake that models the conditional UPDATE;
no database and no APS client is touched.
"""
from __future__ import annotations

import functools
import json
import sys
import types
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import broker  # noqa: E402
import jobs  # noqa: E402


JOB_ID = "job-aps-bind-1"


class FakeJobStore:
    """Models get() and the guarded bind: current attempt, current lease owner,
    and a WorkItem already bound to this attempt is never replaced."""

    def __init__(self, attempt=1, owner="worker-a", status="running"):
        self.attempt = attempt
        self.owner = owner
        self.status = status
        self.bound_id = None
        self.bound_attempt = None
        self.calls = []

    def get(self, job_id):
        self.calls.append(("get", job_id))
        if job_id != JOB_ID:
            return None
        return {"job_id": job_id, "status": self.status, "attempt": self.attempt,
                "lease": {"owner": self.owner, "expires_at": 9e18} if self.owner else None}

    def bind_aps_workitem(self, job_id, attempt, worker_id, workitem_id):
        self.calls.append(("bind", job_id, attempt, worker_id, workitem_id))
        if (job_id != JOB_ID or attempt != self.attempt or worker_id != self.owner
                or self.status != "running"):
            return False
        if self.bound_attempt == attempt and self.bound_id != workitem_id:
            return False
        self.bound_id, self.bound_attempt = workitem_id, attempt
        return True

    def binds(self):
        return [c for c in self.calls if c[0] == "bind"]


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(broker, "ACTIVE_WORKITEMS_PATH", tmp_path / "active_workitems.jsonl")
    with broker._active_workitems_lock:
        broker._active_workitems.clear()
    yield
    with broker._active_workitems_lock:
        broker._active_workitems.clear()


@pytest.fixture
def store(monkeypatch):
    fake = FakeJobStore()
    monkeypatch.setattr(jobs, "_pg_store", fake)
    monkeypatch.setenv("LEAF_JOBS_STORE", "postgres")
    return fake


def _req(job_id=JOB_ID):
    return types.SimpleNamespace(job_id=job_id)


def test_postgres_store_binds_once_with_the_dispatch_identity(store):
    recorder = broker._submission_recorder(_req(), "run-1")
    assert isinstance(recorder, broker._ApsWorkitemBinder)

    recorder("wi-1")
    recorder("wi-2")  # a later WorkItem in the same attempt never rebinds

    assert store.binds() == [("bind", JOB_ID, 1, "worker-a", "wi-1")]
    assert recorder.bound is True
    assert store.bound_id == "wi-1"
    # The cancel correlation is recorded exactly as before: latest id, this run.
    assert broker._active_workitems[JOB_ID][:2] == ("wi-2", "run-1")


def test_each_attempt_binds_its_own_workitem(store):
    first = broker._submission_recorder(_req(), "run-1")
    first("wi-1")
    store.attempt, store.owner = 2, "worker-b"
    second = broker._submission_recorder(_req(), "run-2")
    second("wi-2")

    assert store.binds() == [("bind", JOB_ID, 1, "worker-a", "wi-1"),
                             ("bind", JOB_ID, 2, "worker-b", "wi-2")]
    assert first.bound is True and second.bound is True
    assert (store.bound_id, store.bound_attempt) == ("wi-2", 2)


def test_stale_attempt_is_refused_and_inert(store, capsys):
    recorder = broker._submission_recorder(_req(), "run-1")
    store.attempt, store.owner = 2, "worker-b"  # lease reclaimed before APS accepted

    recorder("wi-stale")

    assert store.binds() == [("bind", JOB_ID, 1, "worker-a", "wi-stale")]
    assert recorder.bound is False
    assert store.bound_id is None
    assert broker._active_workitems[JOB_ID][:2] == ("wi-stale", "run-1")
    assert "bind refused" in capsys.readouterr().err


def test_wrong_owner_is_refused_and_inert(store):
    recorder = broker._submission_recorder(_req(), "run-1")
    store.owner = "worker-other"

    recorder("wi-1")

    assert recorder.bound is False
    assert store.bound_id is None
    assert broker._active_workitems[JOB_ID][:2] == ("wi-1", "run-1")


def test_a_bind_error_never_reaches_the_poll(store, capsys):
    def boom(*_a, **_k):
        raise RuntimeError("database unavailable")

    store.bind_aps_workitem = boom
    recorder = broker._submission_recorder(_req(), "run-1")

    recorder("wi-1")  # must not raise into da/client's poll

    assert recorder.bound is False
    assert broker._active_workitems[JOB_ID][:2] == ("wi-1", "run-1")
    assert "bind failed" in capsys.readouterr().err


def test_empty_workitem_id_binds_nothing(store):
    recorder = broker._submission_recorder(_req(), "run-1")
    recorder(None)
    recorder("")
    assert store.binds() == []
    assert JOB_ID not in broker._active_workitems
    recorder("wi-1")
    assert store.binds() == [("bind", JOB_ID, 1, "worker-a", "wi-1")]


@pytest.mark.parametrize("mode", ["legacy", None, "bogus"])
def test_non_postgres_store_is_never_called(monkeypatch, mode):
    fake = FakeJobStore()
    monkeypatch.setattr(jobs, "_pg_store", fake)
    if mode is None:
        monkeypatch.delenv("LEAF_JOBS_STORE", raising=False)
    else:
        monkeypatch.setenv("LEAF_JOBS_STORE", mode)

    recorder = broker._submission_recorder(_req(), "run-1")
    assert isinstance(recorder, functools.partial)
    assert recorder.func is broker._record_active_workitem
    recorder("wi-1")

    assert fake.calls == []
    assert broker._active_workitems[JOB_ID][:2] == ("wi-1", "run-1")


@pytest.mark.parametrize("owner,status_attempt", [(None, 1), ("worker-a", 0)])
def test_no_live_lease_keeps_the_plain_recorder(store, owner, status_attempt):
    store.owner, store.attempt = owner, status_attempt
    recorder = broker._submission_recorder(_req(), "run-1")
    assert isinstance(recorder, functools.partial)
    recorder("wi-1")
    assert store.binds() == []


def test_unreadable_job_row_keeps_the_plain_recorder(store):
    def boom(_job_id):
        raise RuntimeError("database unavailable")

    store.get = boom
    recorder = broker._submission_recorder(_req(), "run-1")
    assert isinstance(recorder, functools.partial)
    recorder("wi-1")
    assert store.binds() == []


def test_no_job_id_has_no_callback_and_reads_nothing(store):
    assert broker._submission_recorder(_req(job_id=None), "run-1") is None
    assert store.calls == []


class FakeDa:
    def __init__(self):
        self.calls = []

    def run_tool(self, local, tool, params, on_submitted=None):
        self.calls.append((local, json.dumps(tool, sort_keys=True),
                           json.dumps(params, sort_keys=True), on_submitted is not None))
        on_submitted("wi-poll")
        return {"ok": True, "result": {"count": 3, "layers": ["A", "B"]},
                "workitem": {"id": "wi-poll", "status": "success"}}


@pytest.mark.parametrize("scenario", ["bound", "stale", "raises"])
def test_polling_result_is_byte_identical(monkeypatch, scenario):
    monkeypatch.delenv("LEAF_CALLBACK_PRIMARY", raising=False)
    monkeypatch.setattr(broker, "_get_callbacks", lambda: None)
    tool = {"name": "count", "engine_op": "count_by_layer"}
    params = {"layer": "A"}

    monkeypatch.setenv("LEAF_JOBS_STORE", "legacy")
    baseline_da = FakeDa()
    baseline = broker._run_live_tool(baseline_da, "local.dwg", tool, params,
                                     on_submitted=broker._submission_recorder(_req(), "run-1"))

    fake = FakeJobStore()
    if scenario == "stale":
        fake.owner = "worker-other"
    monkeypatch.setattr(jobs, "_pg_store", fake)
    monkeypatch.setenv("LEAF_JOBS_STORE", "postgres")
    recorder = broker._submission_recorder(_req(), "run-1")
    if scenario == "stale":
        fake.owner = "worker-a-reclaimed"
    if scenario == "raises":
        def boom(*_a, **_k):
            raise RuntimeError("database unavailable")
        fake.bind_aps_workitem = boom
    bound_da = FakeDa()
    result = broker._run_live_tool(bound_da, "local.dwg", tool, params, on_submitted=recorder)

    assert json.dumps(result, sort_keys=True) == json.dumps(baseline, sort_keys=True)
    assert bound_da.calls == baseline_da.calls
    assert recorder.bound is (scenario == "bound")
    if scenario != "raises":
        assert len(fake.binds()) == 1


def test_callback_primary_still_refuses_before_any_bind(monkeypatch, store):
    monkeypatch.setenv("LEAF_CALLBACK_PRIMARY", "1")
    monkeypatch.setattr(broker, "_get_callbacks", lambda: None)
    da = FakeDa()
    recorder = broker._submission_recorder(_req(), "run-1")
    with pytest.raises(broker.CallbackPrimaryUnavailable):
        broker._run_live_tool(da, "local.dwg", {"name": "count"}, {}, on_submitted=recorder)
    assert da.calls == []
    assert store.binds() == []
