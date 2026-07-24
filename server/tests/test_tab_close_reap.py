"""Closing a browser tab must actually cancel the APS WorkItem it abandoned.

THE GAP THIS PINS. POST /api/jobs/{id}/close (the tab-close beacon) marked the
job row terminal and stopped there. The WorkItem kept running on APS and kept
BILLING to completion, because nothing correlated a job_id with a WorkItem id:
the id lives only inside the broker's blocking poll, and the app side — which
holds no APS credential — never learns it. `orphan_lease_records()` hardcoded
`workitem_id: None` and had no callers at all.

The wiring under test, end to end:
    submit_workitem(on_submitted=...)         -> id observable before polling
    broker_run(job_id=...)                    -> _active_workitems[job_id] = id
    /broker/reap {job_id, session_closed}     -> id resolved, cancel issued
    jobs._reap_orphans_once()                 -> fires that reap, closed rows only

Every test is offline: the live APS client is never resolved (`_get_da` is
monkeypatched or returns None under pytest) and no cancel leaves the process.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import broker  # noqa: E402
import broker_client  # noqa: E402
import jobs  # noqa: E402


TOOL = {"name": "reap-tool", "engine_op": "count_by_layer",
        "capabilities": ["drawing.read"]}


class RecordingExecutor:
    def __init__(self, run=False):
        self.calls = []
        self.run = run

    def submit(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))
        if self.run:
            fn(*args, **kwargs)


@pytest.fixture(autouse=True)
def isolated_jobs(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "DB_PATH", tmp_path / "reap.db")
    monkeypatch.setattr(jobs, "_conn", None)
    monkeypatch.setattr(jobs, "_reaper_started", True)
    monkeypatch.setattr(jobs, "_executors", {jobs.LANE_FAST: RecordingExecutor(),
                                             jobs.LANE_SLOW: RecordingExecutor()})
    monkeypatch.setattr(jobs.platform_link, "on_submit", lambda *a, **k: None)
    monkeypatch.setattr(jobs.platform_link, "on_running", lambda *a, **k: None)
    monkeypatch.setattr(jobs.platform_link, "on_terminal", lambda *a, **k: None)
    yield
    if jobs._conn is not None:
        jobs._conn.close()


@pytest.fixture(autouse=True)
def clean_registry():
    """No test may inherit or leak an in-flight WorkItem mapping."""
    with broker._active_workitems_lock:
        broker._active_workitems.clear()
    yield
    with broker._active_workitems_lock:
        broker._active_workitems.clear()


def _submit(*, aps_live=False):
    return jobs.submit_job("tenant-reap", TOOL, {}, "demo", aps_live)


# --------------------------------------------------------------------------- #
# jobs -> broker: the reap is fired for CLOSED rows and ONLY closed rows
# --------------------------------------------------------------------------- #
def test_tab_close_reaps_exactly_once_with_the_job_correlation(monkeypatch):
    job_id = _submit()
    assert jobs.claim_lease(job_id, "worker-1") == 1
    assert jobs.mark_job_closed(job_id) is True

    sent = []
    monkeypatch.setattr(broker_client, "reap_via_broker",
                        lambda records, **_kw: sent.append(records) or {"ok": True})

    assert jobs._reap_orphans_once() == 1

    assert len(sent) == 1, "closed row must trigger exactly one reap"
    (record,) = sent[0]
    assert record["job_id"] == job_id
    assert record["tenant_id"] == "tenant-reap"
    assert record["session_closed"] is True
    assert record["status"] == "inprogress"      # sweep() only reaps in-flight rows
    assert record["workitem_id"] is None         # the broker resolves it
    assert jobs.get_job(job_id)["status"] == "failed"


def test_heartbeat_stale_row_is_redispatched_and_never_reaped(monkeypatch):
    """A stale row gets REDISPATCHED, and the previous worker may still be in
    flight — cancelling by job_id there could kill the WorkItem the retry is
    about to adopt. Only a closed tab is race-free."""
    job_id = _submit()
    assert jobs.claim_lease(job_id, "dead-owner") == 1
    jobs._exec("UPDATE jobs SET lease_expires_at = 0 WHERE job_id = ?", (job_id,))

    sent = []
    monkeypatch.setattr(broker_client, "reap_via_broker",
                        lambda records, **_kw: sent.append(records) or {"ok": True})
    monkeypatch.setattr(jobs, "_executors", {jobs.LANE_FAST: RecordingExecutor(run=True),
                                             jobs.LANE_SLOW: RecordingExecutor(run=True)})
    monkeypatch.setattr(broker_client, "run_via_broker", lambda *a, **k: {"ok": True})

    assert jobs._reap_orphans_once() == 1
    assert sent == [], "heartbeat-stale rows must NOT be reaped"
    assert jobs.get_job(job_id)["status"] == "complete"


def test_unreachable_broker_still_leaves_the_job_terminal(monkeypatch, capsys):
    """Pre-existing contract: failing to cancel remote compute must not un-finish
    the local row. It must not be silent either — money is still burning."""
    job_id = _submit()
    assert jobs.claim_lease(job_id, "worker-1") == 1
    assert jobs.mark_job_closed(job_id) is True

    def _down(_records, **_kw):
        raise broker_client.BrokerUnreachable("broker at http://127.0.0.1:8140 unreachable")

    monkeypatch.setattr(broker_client, "reap_via_broker", _down)

    assert jobs._reap_orphans_once() == 1
    assert jobs.get_job(job_id)["status"] == "failed"
    assert "tab-close reap failed" in capsys.readouterr().err


def test_the_cloud_run_sends_its_job_id_so_correlation_can_exist(monkeypatch):
    """Without job_id on /broker/run the registry is never written and the whole
    reap path is inert."""
    job_id = _submit(aps_live=True)
    seen = {}

    def _run(*_a, **kw):
        seen.update(kw)
        return {"ok": True, "tool": "reap-tool", "version": "1.0.0", "result": {},
                "overlay": None, "timing_ms": 1, "cost": None, "error": None}

    monkeypatch.setattr(broker_client, "run_via_broker", _run)
    jobs._run_job(job_id, "tenant-reap", TOOL, {}, "demo", True)

    assert seen.get("job_id") == job_id


# --------------------------------------------------------------------------- #
# broker: the registry is populated while the run is live, evicted when it ends
# --------------------------------------------------------------------------- #
def _live_da(monkeypatch, *, mid_poll_probe):
    """A stand-in live client that reports its WorkItem id then 'polls'."""

    class _DA:
        @staticmethod
        def run_tool(local, tool, params, on_submitted=None):
            if on_submitted is not None:
                on_submitted("wi-broker-1")
            mid_poll_probe.append(broker.active_workitem_for("job-live"))
            return {"ok": True, "tool": tool.get("name"), "version": "1.0.0",
                    "result": {}, "overlay": None, "timing_ms": 1, "cost": None,
                    "error": None, "degraded_mode": False}

    monkeypatch.setattr(broker, "_get_da", lambda: _DA())
    monkeypatch.setattr(broker, "_live_script_is_nonempty", lambda *a, **k: True)
    monkeypatch.setattr(broker, "_resolve_live_dwg", lambda *a, **k: "demo.dwg")


def test_registry_holds_the_workitem_mid_run_and_is_empty_after(monkeypatch, tmp_path):
    mid_poll = []
    _live_da(monkeypatch, mid_poll_probe=mid_poll)
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")

    client = TestClient(broker.app)
    resp = client.post("/broker/run", json={
        "tenant_id": "tenant-reap", "tool": TOOL, "params": {},
        "dwg": "rooftop_demo", "aps_live": True, "job_id": "job-live"})

    assert resp.status_code == 200
    assert mid_poll == ["wi-broker-1"], "id must be registered before polling returns"
    assert broker.active_workitem_for("job-live") is None, "entry must not outlive the run"


def test_registry_is_evicted_even_when_the_run_fails(monkeypatch, tmp_path):
    """Eviction lives in a `finally`, so a failed/timed-out run cannot leak."""
    class _DA:
        @staticmethod
        def run_tool(local, tool, params, on_submitted=None):
            if on_submitted is not None:
                on_submitted("wi-broker-2")
            raise RuntimeError("WorkItem blew up")

    monkeypatch.setattr(broker, "_get_da", lambda: _DA())
    monkeypatch.setattr(broker, "_live_script_is_nonempty", lambda *a, **k: True)
    monkeypatch.setattr(broker, "_resolve_live_dwg", lambda *a, **k: "demo.dwg")
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")

    client = TestClient(broker.app)
    resp = client.post("/broker/run", json={
        "tenant_id": "tenant-reap", "tool": TOOL, "params": {},
        "dwg": "rooftop_demo", "aps_live": True, "job_id": "job-boom"})

    assert resp.status_code >= 400
    assert broker.active_workitem_for("job-boom") is None


def test_a_run_without_job_id_registers_nothing(monkeypatch, tmp_path):
    """Byte-for-byte compatibility for every existing caller."""
    mid_poll = []
    _live_da(monkeypatch, mid_poll_probe=mid_poll)
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")

    client = TestClient(broker.app)
    resp = client.post("/broker/run", json={
        "tenant_id": "tenant-reap", "tool": TOOL, "params": {},
        "dwg": "rooftop_demo", "aps_live": True})

    assert resp.status_code == 200
    assert mid_poll == [None]
    with broker._active_workitems_lock:
        assert broker._active_workitems == {}


# --------------------------------------------------------------------------- #
# broker: /broker/reap resolves job_id -> workitem_id and cancels it
# --------------------------------------------------------------------------- #
@pytest.fixture
def stub_cancels(monkeypatch):
    """Force the inert StubCancelClient and hand back its record of cancels."""
    reaper = broker._get_reaper()
    stub = reaper.StubCancelClient()
    monkeypatch.setattr(reaper, "cancel_client_for", lambda **_kw: stub)
    return stub


def test_reap_by_job_id_alone_cancels_the_resolved_workitem(stub_cancels):
    """The app can only ever send {job_id, session_closed}. That must be enough."""
    broker._record_active_workitem("job-abandoned", "wi-abandoned")

    resp = TestClient(broker.app).post("/broker/reap", json={
        "records": [{"job_id": "job-abandoned", "session_closed": True,
                     "status": "inprogress"}]})

    assert resp.status_code == 200
    body = resp.json()
    stub = stub_cancels
    assert stub.cancelled == ["wi-abandoned"]
    assert body["reaped"] == ["wi-abandoned"]
    assert body["count"] == 1
    assert body["live"] is False, "APS_LIVE unset -> no live DA cancel"
    assert broker.active_workitem_for("job-abandoned") is None, "resolved entry is dropped"


def test_reap_resolves_through_the_real_factory_and_stays_non_live(monkeypatch):
    """Same path with NOTHING stubbed: APS_LIVE unset must yield the inert client,
    and the resolved id must still come back in `reaped`."""
    monkeypatch.delenv("APS_LIVE", raising=False)
    monkeypatch.delenv("BROKER_REAP_LIVE", raising=False)
    broker._record_active_workitem("job-real-factory", "wi-real-factory")

    resp = TestClient(broker.app).post("/broker/reap", json={
        "records": [{"job_id": "job-real-factory", "session_closed": True,
                     "status": "inprogress"}]})

    assert resp.status_code == 200
    body = resp.json()
    assert body["reaped"] == ["wi-real-factory"]
    assert body["live"] is False


def test_reap_prefers_an_explicit_workitem_id_over_the_registry(stub_cancels):
    broker._record_active_workitem("job-x", "wi-registry")

    resp = TestClient(broker.app).post("/broker/reap", json={
        "records": [{"job_id": "job-x", "workitem_id": "wi-explicit",
                     "session_closed": True, "status": "inprogress"}]})

    assert resp.status_code == 200
    assert stub_cancels.cancelled == ["wi-explicit"]


def test_reap_of_an_unknown_job_cancels_nothing_and_does_not_raise(stub_cancels):
    resp = TestClient(broker.app).post("/broker/reap", json={
        "records": [{"job_id": "job-never-seen", "session_closed": True,
                     "status": "inprogress"}]})

    assert resp.status_code == 200
    assert stub_cancels.cancelled == []


def test_reap_leaves_a_healthy_run_alone_and_keeps_its_id(stub_cancels):
    """A healthy row in the batch must be neither cancelled nor forgotten. If
    resolving evicted it, the job would still be running with nothing left to
    cancel when its tab actually closes."""
    broker._record_active_workitem("job-healthy", "wi-healthy")

    resp = TestClient(broker.app).post("/broker/reap", json={
        "records": [{"job_id": "job-healthy", "status": "inprogress"}]})

    assert resp.status_code == 200
    assert stub_cancels.cancelled == [], "not an orphan: no close signal, no expired lease"
    assert broker.active_workitem_for("job-healthy") == "wi-healthy"


def test_reaping_the_same_job_twice_cancels_it_once(stub_cancels):
    """The sweep is idempotent from the registry's side: once cancelled, the
    correlation is gone, so a repeated beacon cannot re-cancel."""
    broker._record_active_workitem("job-dup", "wi-dup")
    client = TestClient(broker.app)
    payload = {"records": [{"job_id": "job-dup", "session_closed": True,
                            "status": "inprogress"}]}

    assert client.post("/broker/reap", json=payload).status_code == 200
    assert client.post("/broker/reap", json=payload).status_code == 200
    assert stub_cancels.cancelled == ["wi-dup"]


# --------------------------------------------------------------------------- #
# client: the app-side sender
# --------------------------------------------------------------------------- #
def test_reap_via_broker_posts_records_with_broker_auth(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True, "reaped": [], "count": 0}

    def _post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers, timeout=timeout)
        return _Resp()

    monkeypatch.setenv("LEAF_BROKER_SECRET", "s3cret")
    monkeypatch.setattr(broker_client.requests, "post", _post)

    records = [{"job_id": "j1", "session_closed": True}]
    assert broker_client.reap_via_broker(records)["ok"] is True
    assert captured["url"].endswith("/broker/reap")
    assert captured["json"] == {"records": records}
    assert captured["headers"] == {"X-Broker-Secret": "s3cret"}


def test_reap_via_broker_raises_broker_unreachable_on_transport_failure(monkeypatch):
    import requests as _requests

    def _boom(*_a, **_k):
        raise _requests.ConnectionError("refused")

    monkeypatch.setattr(broker_client.requests, "post", _boom)
    with pytest.raises(broker_client.BrokerUnreachable):
        broker_client.reap_via_broker([{"job_id": "j1", "session_closed": True}])
