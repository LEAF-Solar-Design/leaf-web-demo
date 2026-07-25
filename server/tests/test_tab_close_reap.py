"""Closing a browser tab must actually cancel the APS WorkItem it abandoned.

THE GAP THIS PINS. POST /api/jobs/{id}/close (the tab-close beacon) marked the
job row terminal and stopped there. The WorkItem kept running on APS and kept
BILLING to completion, because nothing correlated a job_id with a WorkItem id:
the id lives only inside the broker's blocking poll, and the app side (which
holds no APS credential) never learns it. `orphan_lease_records()` hardcoded
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

import json
import sqlite3
import sys
import time
import types
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
def clean_registry(monkeypatch, tmp_path):
    """No test may inherit or leak an in-flight WorkItem mapping, and none may
    write the durable sidecar into the repo."""
    monkeypatch.setattr(broker, "ACTIVE_WORKITEMS_PATH",
                        tmp_path / "active_workitems.jsonl")
    monkeypatch.setattr(jobs, "PENDING_REAPS_PATH",
                        tmp_path / "pending_reaps.jsonl")
    with broker._active_workitems_lock:
        broker._active_workitems.clear()
    with jobs._pending_reaps_lock:
        jobs._pending_reaps.clear()
    yield
    with broker._active_workitems_lock:
        broker._active_workitems.clear()
    with jobs._pending_reaps_lock:
        jobs._pending_reaps.clear()


class _RecordingCancelClient:
    """A cancel client that is NOT the inert stub, so the broker treats its
    outcome as a genuine cancel (as it would a real DACancelClient)."""

    def __init__(self, succeeds: bool = True) -> None:
        self.cancelled: list = []
        self._succeeds = succeeds

    def cancel(self, workitem_id: str) -> dict:
        self.cancelled.append(workitem_id)
        return {"workitem_id": workitem_id, "cancelled": self._succeeds}


@pytest.fixture
def live_cancels(monkeypatch):
    """A cancel that really reaches APS and succeeds."""
    reaper = broker._get_reaper()
    client = _RecordingCancelClient(succeeds=True)
    monkeypatch.setattr(reaper, "cancel_client_for", lambda **_kw: client)
    return client


@pytest.fixture
def failing_cancels(monkeypatch):
    """A cancel that reaches APS and is REFUSED."""
    reaper = broker._get_reaper()
    client = _RecordingCancelClient(succeeds=False)
    monkeypatch.setattr(reaper, "cancel_client_for", lambda **_kw: client)
    return client


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


def test_close_arriving_after_the_run_finished_is_a_clean_no_op(monkeypatch):
    """A tab can close at any moment, including after the WorkItem is already
    done. The beacon must not error, and the sweep must not reap a terminal row
    -- cancelling there would issue a DELETE against work APS already finished.
    """
    job_id = _submit(aps_live=True)
    assert jobs.claim_lease(job_id, "worker-1") == 1
    assert jobs.complete_callback(
        job_id, "complete",
        result_env={"ok": True, "tool": "reap-tool"},
        worker_id="worker-1",
        provenance={"attempt": 1, "execution_path": "cloud"}) == "applied"
    assert jobs.get_job(job_id)["status"] == "complete"

    sent = []
    monkeypatch.setattr(broker_client, "reap_via_broker",
                        lambda records, **_kw: sent.append(records) or {"ok": True})

    assert jobs.mark_job_closed(job_id) is False, "terminal row cannot be closed"
    assert jobs._reap_orphans_once() == 0, "a finished row is not reclaimable"
    assert sent == [], "no cancel may be issued against a completed WorkItem"
    assert jobs.get_job(job_id)["status"] == "complete", "close must not un-finish it"


def test_close_racing_a_just_finished_run_resolves_to_no_workitem(stub_cancels):
    """The narrow race: the row is still 'running' when the beacon lands, but the
    broker's `finally` has already evicted the correlation. The reap must resolve
    to no id and cancel NOTHING, rather than raising or cancelling a stale id."""
    broker._record_active_workitem("job-just-finished", "wi-just-finished")
    assert broker._drop_active_workitem("job-just-finished") == "wi-just-finished"

    resp = TestClient(broker.app).post("/broker/reap", json={
        "records": [{"job_id": "job-just-finished", "session_closed": True,
                     "status": "inprogress"}]})

    assert resp.status_code == 200
    assert stub_cancels.cancelled == [], "the run already ended: nothing to cancel"
    assert resp.json()["reaped"] == [None], "row swept, but no id was cancelled"


def test_heartbeat_stale_row_is_redispatched_and_never_reaped(monkeypatch):
    """A stale row gets REDISPATCHED, and the previous worker may still be in
    flight, so cancelling by job_id there could kill the WorkItem the retry is
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
    the local row. It must not be silent either: money is still burning."""
    job_id = _submit()
    assert jobs.claim_lease(job_id, "worker-1") == 1
    assert jobs.mark_job_closed(job_id) is True

    def _down(_records, **_kw):
        raise broker_client.BrokerUnreachable("broker at http://127.0.0.1:8140 unreachable")

    monkeypatch.setattr(broker_client, "reap_via_broker", _down)

    assert jobs._reap_orphans_once() == 1
    assert jobs.get_job(job_id)["status"] == "failed"
    assert "tab-close reap failed" in capsys.readouterr().err


def test_row_get_tolerates_both_store_row_shapes():
    """The two stores hand the same sweep different row shapes: sqlite selects
    whole rows, the PostgreSQL `reclaimable` projection is job_id+progress only.
    A missing column raises IndexError on sqlite3.Row and KeyError on a dict --
    neither may escape and kill the sweep."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (job_id TEXT, tenant_id TEXT)")
    conn.execute("INSERT INTO t VALUES ('j1', 'tenant-a')")
    sqlite_row = conn.execute("SELECT * FROM t").fetchone()
    conn.close()

    assert jobs._row_get(sqlite_row, "tenant_id") == "tenant-a"
    assert jobs._row_get(sqlite_row, "nope") is None
    assert jobs._row_get({"job_id": "j1"}, "tenant_id") is None


def test_reap_payload_survives_a_missing_tenant(monkeypatch):
    """PostgreSQL's projection carries no tenant_id. The broker correlates on
    job_id and never reads it, so a null tenant must not break the send."""
    sent = []
    monkeypatch.setattr(broker_client, "reap_via_broker",
                        lambda records, **_kw: sent.append(records) or {"ok": True})

    jobs._cancel_remote_workitem("job-no-tenant", None)

    assert sent == [[{"job_id": "job-no-tenant", "tenant_id": None,
                      "status": "inprogress", "workitem_id": None,
                      "session_closed": True}]]


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


def test_a_run_that_dies_mid_poll_keeps_its_workitem_reapable(monkeypatch, tmp_path):
    """A raise mid-poll ends the RUN, not necessarily the WorkItem: it was
    submitted and nothing issued a DELETE. Closing the correlation here would
    make something that is still billing permanently unaddressable, so the entry
    is retained -- disowned, so the dead run can no longer evict it, and still
    reapable by a later beacon. Bounded by the replay TTL and compaction.
    """
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
    assert broker.active_workitem_for("job-boom") == "wi-broker-2", (
        "the run died, but its WorkItem may still be running and billing")
    with broker._active_workitems_lock:
        assert broker._active_workitems["job-boom"][1] is None, (
            "the dead run must no longer own it")


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


def test_a_live_write_registers_its_workitem_too(monkeypatch, tmp_path):
    """A live WRITE submits and polls its own WorkItem exactly like a read does,
    so an abandoned write burns money the same way. It was the one live branch
    with no correlation at all -- a closed tab could never cancel it."""
    import store
    import write_loop

    backend = store.InMemoryBackend()
    vkey = store.drawing_version_key("tenant-reap", "d1", 1)
    backend.put(vkey, b"DWGBYTES")
    backend.put(store.manifest_key("tenant-reap", "d1"), json.dumps({
        "schema": 1, "tenant_id": "tenant-reap", "drawing_id": "d1",
        "head": 1, "latest": 1, "versions": [{"v": 1, "key": vkey}],
        "checkout": None}).encode())

    seen = {}

    def _fake_submit(activity_id, arguments, dry_run=False, poll=True,
                     tenant_id=None, on_submitted=None):
        seen["offered"] = on_submitted is not None
        if on_submitted is not None:
            on_submitted("wi-write-1")
        # Mid-poll: this is exactly when an abandoned write must be cancellable.
        seen["mid_run"] = broker.active_workitem_for("job-write")
        return {"status": "failed", "id": "wi-write-1"}   # stop before upload

    da = types.SimpleNamespace(
        submit_workitem=_fake_submit,
        activity_qualified=lambda name: f"nick.{name}",
        signed_download_url=lambda key: "https://example.invalid/in",
        signed_upload_url=lambda key: ("upload-key", "https://example.invalid/out"),
        upload_object=lambda *a, **k: None,
        finalize_upload=lambda *a, **k: None,
    )

    req = broker.BrokerRunRequest(tenant_id="tenant-reap", tool={"name": "w"},
                                  params={}, dwg="d1", aps_live=True,
                                  job_id="job-write")

    env, status_code = write_loop.run_write_live(
        {"name": "w"}, {"drawing_id": "d1"}, "tenant-reap",
        backend=backend, da=da, t0=0.0,
        on_submitted=broker._submission_recorder(req, "write-token"))

    assert env["ok"] is False and status_code >= 400, "the stub WorkItem failed"
    assert seen.get("offered") is True, "the write path must offer the hook"
    assert seen.get("mid_run") == "wi-write-1", (
        "the write's WorkItem must be reapable while it is still polling")


def test_a_failed_reap_is_retried_on_the_next_sweep(monkeypatch):
    """The row is already terminal when the reap fires, so the sweep will never
    select it again. One broker blip must not be the end of the attempt."""
    job_id = _submit()
    assert jobs.claim_lease(job_id, "worker-1") == 1
    assert jobs.mark_job_closed(job_id) is True

    attempts = []

    def _down(records, **_kw):
        attempts.append(records)
        raise broker_client.BrokerUnreachable("broker unreachable")

    monkeypatch.setattr(broker_client, "reap_via_broker", _down)
    assert jobs._reap_orphans_once() == 1
    assert len(attempts) == 1
    assert job_id in jobs._pending_reaps, "a failed reap must be queued, not lost"

    # Broker comes back; the next sweep delivers it even though the row is
    # terminal and is no longer selected.
    delivered = []
    monkeypatch.setattr(broker_client, "reap_via_broker",
                        lambda records, **_kw: delivered.append(records) or {"ok": True})

    assert jobs._reap_orphans_once() == 0, "no reclaimable rows left"
    assert len(delivered) == 1, "the queued reap was retried"
    assert delivered[0][0]["job_id"] == job_id
    assert job_id not in jobs._pending_reaps, "a delivered reap is dequeued"


def test_a_non_200_reap_response_is_not_read_as_success(monkeypatch):
    """A 401/500/503 still carries a JSON body. Returning it would report a
    cancel that never happened and drop the job from the retry queue."""
    class _Resp:
        status_code = 503

        @staticmethod
        def json():
            return {"ok": False, "detail": "broker reconciling"}

    monkeypatch.setattr(broker_client.requests, "post",
                        lambda *a, **k: _Resp())

    with pytest.raises(broker_client.BrokerReapRejected):
        broker_client.reap_via_broker([{"job_id": "j1", "session_closed": True}])


def test_an_accepted_but_uncancelled_reap_is_queued_for_retry(monkeypatch):
    """HTTP 200 means the broker took the request, not that APS stopped billing.
    Only cancelled_jobs says the DELETE actually succeeded."""
    job_id = _submit()
    assert jobs.claim_lease(job_id, "worker-1") == 1
    assert jobs.mark_job_closed(job_id) is True

    monkeypatch.setattr(broker_client, "reap_via_broker",
                        lambda records, **_kw: {"ok": True, "live": True,
                                                "cancelled_jobs": [], "count": 1})

    assert jobs._reap_orphans_once() == 1
    assert job_id in jobs._pending_reaps, (
        "live reaping was on and the cancel did not land: try again")


def test_reaping_off_does_not_queue_every_closed_tab_forever(monkeypatch):
    """With live reaping disabled no cancel was ever going to happen, so retrying
    would grow the queue with work that can never succeed."""
    job_id = _submit()
    assert jobs.claim_lease(job_id, "worker-1") == 1
    assert jobs.mark_job_closed(job_id) is True

    monkeypatch.setattr(broker_client, "reap_via_broker",
                        lambda records, **_kw: {"ok": True, "live": False,
                                                "cancelled_jobs": [], "count": 1})

    assert jobs._reap_orphans_once() == 1
    assert job_id not in jobs._pending_reaps


def test_the_retry_batch_stops_at_the_first_unreachable_broker(monkeypatch):
    """The retry runs INSIDE the sweep. A broker that is down is down for all of
    them, so grinding through the whole queue would stall the tabs closing now."""
    for i in range(5):
        jobs._remember_pending_reap(f"job-{i}", "tenant-reap")

    attempts = []

    def _down(records, **_kw):
        attempts.append(records[0]["job_id"])
        raise broker_client.BrokerUnreachable("down")

    monkeypatch.setattr(broker_client, "reap_via_broker", _down)
    jobs._retry_pending_reaps()

    assert len(attempts) == 1, f"must abandon the batch after one failure, tried {attempts}"


def test_the_retry_batch_is_capped_per_sweep(monkeypatch):
    monkeypatch.setattr(jobs, "PENDING_REAP_BATCH", 3)
    for i in range(10):
        jobs._remember_pending_reap(f"job-{i}", "tenant-reap")

    attempts = []
    monkeypatch.setattr(broker_client, "reap_via_broker",
                        lambda records, **_kw: attempts.append(records) or {"ok": True})

    jobs._retry_pending_reaps()
    assert len(attempts) == 3


def test_a_live_broker_that_names_no_cancelled_jobs_fails_closed(monkeypatch):
    """An older broker mid rolling-update reports `live` but has no
    cancelled_jobs key. It has NOT told us the WorkItem stopped, and treating
    that as done is how the billing quietly continues."""
    job_id = _submit()
    assert jobs.claim_lease(job_id, "worker-1") == 1
    assert jobs.mark_job_closed(job_id) is True

    monkeypatch.setattr(broker_client, "reap_via_broker",
                        lambda records, **_kw: {"ok": True, "live": True, "count": 1})

    assert jobs._reap_orphans_once() == 1
    assert job_id in jobs._pending_reaps, "unacknowledged means unfinished"


def test_a_stuck_job_rotates_and_cannot_starve_the_queue(monkeypatch):
    """Taking the first N entries every sweep without rotation lets an
    unresolvable job sit at the head forever while later jobs are never tried."""
    monkeypatch.setattr(jobs, "PENDING_REAP_BATCH", 2)
    for i in range(5):
        jobs._remember_pending_reap(f"job-{i}", "tenant-reap")

    tried = []

    def _accepted_but_never_cancelled(records, **_kw):
        tried.append(records[0]["job_id"])
        return {"ok": True, "live": True, "cancelled_jobs": []}

    monkeypatch.setattr(broker_client, "reap_via_broker", _accepted_but_never_cancelled)

    for _ in range(3):
        jobs._retry_pending_reaps()

    assert len(set(tried)) > 2, (
        f"every sweep tried the same head entries: {tried}")


def test_a_reap_that_never_succeeds_is_given_up_on_loudly(monkeypatch, capsys):
    """Retrying forever starves the jobs behind it. Give up, but say so."""
    monkeypatch.setattr(jobs, "PENDING_REAP_MAX_ATTEMPTS", 3)
    for _ in range(3):
        jobs._remember_pending_reap("job-stuck", "tenant-reap")
    assert jobs._pending_reaps["job-stuck"]["attempts"] == 3, "still trying"

    jobs._remember_pending_reap("job-stuck", "tenant-reap")   # attempt 4 of 3

    assert "job-stuck" not in jobs._pending_reaps
    assert "giving up on the tab-close reap" in capsys.readouterr().err


def test_the_pending_queue_survives_an_app_restart(monkeypatch, tmp_path):
    """The row is terminal when the reap fails, so the sweep never selects it
    again: this queue is the only remaining signal that a WorkItem needs
    cancelling. Losing it on restart loses the cancel."""
    monkeypatch.setattr(jobs, "PENDING_REAPS_PATH", tmp_path / "pending_reaps.jsonl")
    jobs._remember_pending_reap("job-persisted", "tenant-reap")

    restored = jobs._load_pending_reaps()

    assert restored["job-persisted"]["tenant_id"] == "tenant-reap"
    assert restored["job-persisted"]["attempts"] == 1

    jobs._forget_pending_reap("job-persisted")
    assert jobs._load_pending_reaps() == {}


def test_the_pending_reap_queue_is_bounded(monkeypatch, capsys):
    """A long outage must not grow the queue without limit."""
    monkeypatch.setattr(jobs, "PENDING_REAP_MAX", 2)
    for i in range(4):
        jobs._remember_pending_reap(f"job-{i}", "tenant-reap")

    assert len(jobs._pending_reaps) == 2
    assert "pending-reap queue full" in capsys.readouterr().err


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
    assert broker.active_workitem_for("job-abandoned") == "wi-abandoned", (
        "the inert stub cancelled NOTHING on APS, so the id must be kept: it is "
        "the only thing that can stop the billing once reaping is enabled")


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


def test_reaping_the_same_job_twice_cancels_it_once(live_cancels):
    """The sweep is idempotent from the registry's side: once a cancel really
    succeeds the correlation is gone, so a repeated beacon cannot re-cancel."""
    broker._record_active_workitem("job-dup", "wi-dup")
    client = TestClient(broker.app)
    payload = {"records": [{"job_id": "job-dup", "session_closed": True,
                            "status": "inprogress"}]}

    assert client.post("/broker/reap", json=payload).status_code == 200
    assert client.post("/broker/reap", json=payload).status_code == 200
    assert live_cancels.cancelled == ["wi-dup"]
    assert broker.active_workitem_for("job-dup") is None


def test_a_refused_cancel_keeps_the_correlation_for_a_later_retry(failing_cancels):
    """sweep() marks a row reaped whatever the cancel returned. Evicting on that
    alone would throw the id away after a FAILED DELETE, and the id is the only
    thing that can stop the billing."""
    broker._record_active_workitem("job-refused", "wi-refused")

    resp = TestClient(broker.app).post("/broker/reap", json={
        "records": [{"job_id": "job-refused", "session_closed": True,
                     "status": "inprogress"}]})

    assert resp.status_code == 200
    assert failing_cancels.cancelled == ["wi-refused"]
    assert broker.active_workitem_for("job-refused") == "wi-refused", (
        "the DELETE was refused: keep the id so a later sweep can try again")


def test_a_duplicate_delivery_cannot_evict_the_live_runs_correlation(monkeypatch, tmp_path):
    """A redelivery of the same job POSTs the same job_id, is rejected before it
    runs anything, and then falls into the SAME `finally` as a real run. Without
    ownership it would evict the in-flight run's correlation and leave a live,
    billing WorkItem permanently unreapable."""
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    broker._record_active_workitem("job-inflight", "wi-inflight",
                                   run_token="token-of-the-real-run")

    # The duplicate never reaches _execute, so it registers nothing; its own
    # token owns nothing.
    broker._drop_active_workitem("job-inflight", "token-of-the-duplicate")

    assert broker.active_workitem_for("job-inflight") == "wi-inflight", (
        "a duplicate delivery must not evict the running job's WorkItem id")

    # The run that actually owns it still evicts on its way out.
    assert broker._drop_active_workitem(
        "job-inflight", "token-of-the-real-run") == "wi-inflight"
    assert broker.active_workitem_for("job-inflight") is None


def test_correlations_are_recoverable_after_a_broker_restart(monkeypatch, tmp_path):
    """The WorkItem outlives the broker process -- APS keeps running and billing
    it -- so a restart must not be what makes an abandoned run uncancellable."""
    sidecar = tmp_path / "active_workitems.jsonl"
    monkeypatch.setattr(broker, "ACTIVE_WORKITEMS_PATH", sidecar)

    broker._record_active_workitem("job-survivor", "wi-survivor", run_token="t1")
    broker._record_active_workitem("job-finished", "wi-finished", run_token="t2")
    broker._drop_active_workitem("job-finished", "t2")

    replayed = broker._replay_persisted_workitems()

    assert replayed["job-survivor"][0] == "wi-survivor"
    assert replayed["job-survivor"][1] is None, (
        "replayed entries are UNOWNED: the process that owned them is gone")
    assert "job-finished" not in replayed, "a closed correlation must not come back"


def test_a_redelivery_cannot_erase_a_correlation_recovered_from_disk():
    """Restart recovery is only worth anything if the recovered entry survives.
    A replayed entry has no owner, so a redelivery -- which registers nothing but
    still runs the same `finally` -- must not be able to delete it."""
    with broker._active_workitems_lock:
        broker._active_workitems["job-recovered"] = ("wi-recovered", None, time.time())

    assert broker._drop_active_workitem("job-recovered", "some-new-run-token") is None
    assert broker.active_workitem_for("job-recovered") == "wi-recovered"

    # A successful cancel still clears it (the reap passes no run token).
    assert broker._drop_active_workitem(
        "job-recovered", expected_workitem_id="wi-recovered") == "wi-recovered"


def test_a_stale_replayed_correlation_is_aged_out(monkeypatch, tmp_path):
    """A WorkItem cannot outlive the 900s poll ceiling, so an hour-old entry names
    finished work. Keeping it would grow the recovered set across every restart."""
    sidecar = tmp_path / "active_workitems.jsonl"
    monkeypatch.setattr(broker, "ACTIVE_WORKITEMS_PATH", sidecar)
    old = time.time() - (broker.ACTIVE_WORKITEM_TTL_S + 60)
    sidecar.write_text(
        json.dumps({"event": "open", "job_id": "job-old",
                    "workitem_id": "wi-old", "ts": old}) + "\n"
        + json.dumps({"event": "open", "job_id": "job-fresh",
                      "workitem_id": "wi-fresh", "ts": time.time()}) + "\n",
        encoding="utf-8")

    replayed = broker._replay_persisted_workitems()

    assert "job-old" not in replayed, "an hour-old correlation is certainly finished"
    assert replayed["job-fresh"][0] == "wi-fresh"


def test_a_cancel_racing_a_new_run_is_not_acknowledged(live_cancels):
    """The old WorkItem really was cancelled, but a newer run has already
    installed a fresh correlation for the same job. Acknowledging the JOB there
    tells the app to stop retrying while the NEW WorkItem is still billing."""
    broker._record_active_workitem("job-race", "wi-new", run_token="new-run")

    resp = TestClient(broker.app).post("/broker/reap", json={
        "records": [{"job_id": "job-race", "workitem_id": "wi-old",
                     "session_closed": True, "status": "inprogress"}]})

    assert resp.status_code == 200
    body = resp.json()
    assert live_cancels.cancelled == ["wi-old"], "the old WorkItem was cancelled"
    assert body["cancelled_jobs"] == [], (
        "the job is NOT settled: wi-new is still in flight")
    assert broker.active_workitem_for("job-race") == "wi-new"


def test_a_plain_successful_cancel_is_acknowledged(live_cancels):
    """The ordinary case must still acknowledge, or the app retries forever."""
    broker._record_active_workitem("job-plain", "wi-plain", run_token="r")

    resp = TestClient(broker.app).post("/broker/reap", json={
        "records": [{"job_id": "job-plain", "session_closed": True,
                     "status": "inprogress"}]})

    assert resp.json()["cancelled_jobs"] == ["job-plain"]
    assert broker.active_workitem_for("job-plain") is None


def test_a_replacement_registered_mid_settle_is_not_acknowledged_away():
    """The interleaving, not just the ordering: a replacement run can register
    AFTER the eviction and BEFORE the 'is anything left?' question. Deciding
    those separately acknowledges the job while the NEW WorkItem still bills, so
    the two must happen under one lock."""
    broker._record_active_workitem("job-tocto", "wi-old", run_token="old-run")

    # Settling the id we cancelled is what clears it...
    assert broker._settle_cancelled_workitem("job-tocto", "wi-old") is True
    assert broker.active_workitem_for("job-tocto") is None

    # ...and a replacement present at settle time is never acknowledged.
    broker._record_active_workitem("job-tocto", "wi-new", run_token="new-run")
    assert broker._settle_cancelled_workitem("job-tocto", "wi-old") is False
    assert broker.active_workitem_for("job-tocto") == "wi-new"


def test_a_timed_out_poll_keeps_its_workitem_reapable(monkeypatch, tmp_path):
    """da/client's poll RETURNS when its ceiling expires and issues no DELETE,
    so the WorkItem can still be running and billing. A run ending that way must
    not persist a close for it -- nothing could ever address it again."""
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")

    class _DA:
        @staticmethod
        def run_tool(local, tool, params, on_submitted=None):
            if on_submitted is not None:
                on_submitted("wi-still-running")
            # What run_tool returns when the WorkItem never reached success.
            return {"ok": False, "tool": tool.get("name"), "version": "1.0.0",
                    "result": {}, "overlay": None, "timing_ms": 1, "cost": None,
                    "error": {"error_code": "workitem_failed",
                              "message": "poll ceiling reached", "retryable": True},
                    "degraded_mode": False}

    monkeypatch.setattr(broker, "_get_da", lambda: _DA())
    monkeypatch.setattr(broker, "_live_script_is_nonempty", lambda *a, **k: True)
    monkeypatch.setattr(broker, "_resolve_live_dwg", lambda *a, **k: "demo.dwg")

    TestClient(broker.app).post("/broker/run", json={
        "tenant_id": "tenant-reap", "tool": TOOL, "params": {},
        "dwg": "rooftop_demo", "aps_live": True, "job_id": "job-timedout"})

    assert broker.active_workitem_for("job-timedout") == "wi-still-running", (
        "the run ended but the WorkItem may not have: keep it cancellable")


def test_a_disowned_correlation_is_still_reapable(live_cancels):
    """Disowning must not make it unreachable -- that is the whole point."""
    broker._record_active_workitem("job-disowned", "wi-disowned", run_token="r")
    assert broker._disown_active_workitem("job-disowned", "r") is True

    resp = TestClient(broker.app).post("/broker/reap", json={
        "records": [{"job_id": "job-disowned", "session_closed": True,
                     "status": "inprogress"}]})

    assert resp.status_code == 200
    assert live_cancels.cancelled == ["wi-disowned"]
    assert resp.json()["cancelled_jobs"] == ["job-disowned"]


def test_a_successful_run_still_drops_its_correlation(monkeypatch, tmp_path):
    """The ordinary path must not start leaking entries."""
    mid_poll = []
    _live_da(monkeypatch, mid_poll_probe=mid_poll)
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")

    resp = TestClient(broker.app).post("/broker/run", json={
        "tenant_id": "tenant-reap", "tool": TOOL, "params": {},
        "dwg": "rooftop_demo", "aps_live": True, "job_id": "job-live"})

    assert resp.status_code == 200
    assert broker.active_workitem_for("job-live") is None


def test_the_replay_ttl_outlasts_the_poll_ceiling():
    """_poll_workitem returns when its 900s ceiling expires and issues no DELETE,
    so a WorkItem can still be executing long after this process stopped watching
    it. A TTL at or near that ceiling would discard a live correlation."""
    assert broker.ACTIVE_WORKITEM_TTL_S >= 86400


def test_a_successful_cancel_does_not_evict_a_newer_correlation():
    """A new run can install a fresh correlation for the same job while the
    DELETE for the old one is still in flight. Dropping by job_id alone would
    throw away the NEW, live WorkItem's only means of cancellation."""
    broker._record_active_workitem("job-recycled", "wi-new", run_token="new-run")

    dropped = broker._drop_active_workitem("job-recycled",
                                           expected_workitem_id="wi-old")

    assert dropped is None
    assert broker.active_workitem_for("job-recycled") == "wi-new"


def test_the_sidecar_is_written_under_the_registry_lock(monkeypatch, tmp_path):
    """Appending outside the lock lets two threads persist in the opposite order
    from the order they mutated memory, so a `close` can land before its `open`
    and a restart resurrects a finished WorkItem."""
    monkeypatch.setattr(broker, "ACTIVE_WORKITEMS_PATH", tmp_path / "a.jsonl")
    held = []

    real_persist = broker._persist_workitem_event_locked

    def _checking_persist(event, job_id, workitem_id):
        held.append(broker._active_workitems_lock.locked())
        return real_persist(event, job_id, workitem_id)

    monkeypatch.setattr(broker, "_persist_workitem_event_locked", _checking_persist)

    broker._record_active_workitem("job-ordered", "wi-ordered", run_token="t")
    broker._drop_active_workitem("job-ordered", "t")

    assert held == [True, True], "both appends must happen under the registry lock"


def test_the_sidecar_compacts_at_runtime_not_only_at_import(monkeypatch, tmp_path):
    """A long-lived broker would otherwise cross the bound once and grow forever."""
    sidecar = tmp_path / "active_workitems.jsonl"
    monkeypatch.setattr(broker, "ACTIVE_WORKITEMS_PATH", sidecar)
    monkeypatch.setattr(broker, "_ACTIVE_WORKITEMS_COMPACT_LINES", 4)
    monkeypatch.setattr(broker, "_sidecar_lines", 0)

    for i in range(6):
        broker._record_active_workitem(f"job-{i}", f"wi-{i}", run_token="t")
        broker._drop_active_workitem(f"job-{i}", "t")

    lines = [ln for ln in sidecar.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) <= 4, f"sidecar must be compacted at runtime, got {len(lines)} lines"


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
