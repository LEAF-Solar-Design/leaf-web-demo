"""
Binary acceptance for the agent-spine A3 lane (wire contract section 10 + the SSE
half of B1).

  Lane split   two ThreadPoolExecutor lanes in server/jobs.py: "fast"
               (JOB_WORKERS_FAST, default 8) for tools WITHOUT drawing.write on the
               mock path; "slow" (JOB_WORKERS_SLOW, default = legacy JOB_WORKERS)
               for drawing.write or live-APS runs. Selection at submit time from
               the resolved tool dict; env-tunable; unset env == today's write pool.
  SSE rewrite  GET /api/jobs/{job_id}/stream is an async generator (no pinned AnyIO
               worker thread) with the SAME wire format: one `data:` line per
               (status, progress) transition at the 0.5s cadence, closing after
               terminal; 404 envelope on unknown/cross-tenant job ids.

Styles mirror tests/test_ui_wave.py / tests/test_wave3.py: pure-unit for lane
selection + pool sizing, in-process TestClient with a monkeypatched broker_client
for the SSE event sequence (hermetic — no broker subprocess, no network).

Run:  cd server && python -m pytest tests/test_job_lanes.py -q
"""
from __future__ import annotations

# Cache the STDLIB `platform` module BEFORE anything puts PROJECT_ROOT on sys.path
# (the local `platform/` package otherwise shadows it — mirrors tests/test_wave3.py).
import platform as _stdlib_platform  # noqa: E402
_stdlib_platform.python_implementation()

import json  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent.parent

# route the jobs SQLite DB to a throwaway dir BEFORE `jobs` is imported anywhere
# (jobs.py reads JOBS_DB at import time — mirrors tests/test_wave3.py).
os.environ.setdefault("JOBS_DB", str(Path(tempfile.mkdtemp(prefix="lanes-jobs-")) / "jobs.db"))

if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import broker_client  # noqa: E402
import jobs  # noqa: E402

HDRS = {"X-Tenant-Id": "demo-tenant"}

READ_TOOL = {"name": "fake-read", "engine_op": "count_by_layer",
             "capabilities": ["drawing.read"], "params": {}}
WRITE_TOOL = {"name": "fake-write", "engine_op": "add_panel_row",
              "capabilities": ["drawing.read", "drawing.write"], "params": {}}


# =========================================================================== #
# lane selection (pure unit — wire contract section 10)
# =========================================================================== #
def test_lane_read_tool_mock_path_is_fast():
    assert jobs.lane_for(READ_TOOL, aps_live=False) == jobs.LANE_FAST


def test_lane_write_tool_is_slow():
    assert jobs.lane_for(WRITE_TOOL, aps_live=False) == jobs.LANE_SLOW


def test_lane_live_aps_is_slow_even_for_reads():
    assert jobs.lane_for(READ_TOOL, aps_live=True) == jobs.LANE_SLOW


def test_lane_missing_capabilities_defaults_fast():
    # a tool dict with no capabilities key (older authored tools) is a mock read
    assert jobs.lane_for({"name": "bare"}, aps_live=False) == jobs.LANE_FAST


# =========================================================================== #
# pool sizing (env-tunable; unset == today's write behavior)
# =========================================================================== #
def test_lane_workers_defaults(monkeypatch):
    monkeypatch.delenv("JOB_WORKERS_FAST", raising=False)
    monkeypatch.delenv("JOB_WORKERS_SLOW", raising=False)
    sizes = jobs.lane_workers()
    assert sizes[jobs.LANE_FAST] == 8
    # slow lane defaults to the legacy JOB_WORKERS pool size — writes unchanged
    assert sizes[jobs.LANE_SLOW] == jobs.MAX_WORKERS


def test_lane_workers_env_overrides(monkeypatch):
    monkeypatch.setenv("JOB_WORKERS_FAST", "2")
    monkeypatch.setenv("JOB_WORKERS_SLOW", "1")
    assert jobs.lane_workers() == {jobs.LANE_FAST: 2, jobs.LANE_SLOW: 1}


def test_ensure_started_builds_both_lane_executors(monkeypatch):
    monkeypatch.setenv("JOB_WORKERS_FAST", "3")
    monkeypatch.setenv("JOB_WORKERS_SLOW", "2")
    monkeypatch.setattr(jobs, "_executors", {})       # fresh topology, restored after
    monkeypatch.setattr(jobs, "_reaper_started", True)  # no extra reaper thread
    jobs.ensure_started()
    created = jobs._executors
    try:
        assert set(created) == {jobs.LANE_FAST, jobs.LANE_SLOW}
        assert created[jobs.LANE_FAST]._max_workers == 3
        assert created[jobs.LANE_SLOW]._max_workers == 2
        assert created[jobs.LANE_FAST]._thread_name_prefix == "jobworker-fast"
        assert created[jobs.LANE_SLOW]._thread_name_prefix == "jobworker-slow"
    finally:
        for ex in created.values():
            ex.shutdown(wait=False)


# =========================================================================== #
# submit-time routing (recording executors — no threads, no broker)
# =========================================================================== #
def test_jobs_db_creates_missing_parent_directory(monkeypatch, tmp_path):
    db_path = tmp_path / "missing" / "nested" / "jobs.db"
    monkeypatch.setattr(jobs, "DB_PATH", db_path)
    monkeypatch.setattr(jobs, "_conn", None)

    assert jobs.list_jobs("parent-create-test", limit=1) == []
    assert db_path.is_file()

    jobs._conn.close()


class _RecordingExecutor:
    def __init__(self):
        self.submitted = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))


def test_submit_routes_read_to_fast_and_write_to_slow(monkeypatch):
    fast, slow = _RecordingExecutor(), _RecordingExecutor()
    monkeypatch.setattr(jobs, "_executors", {jobs.LANE_FAST: fast, jobs.LANE_SLOW: slow})
    monkeypatch.setattr(jobs, "_reaper_started", True)

    read_id = jobs.submit_job("t-lanes", READ_TOOL, {}, "rooftop_demo", aps_live=False)
    write_id = jobs.submit_job("t-lanes", WRITE_TOOL, {}, "rooftop_demo", aps_live=False)
    live_id = jobs.submit_job("t-lanes", READ_TOOL, {}, "rooftop_demo", aps_live=True)

    assert [a[0] for (_, a, _k) in fast.submitted] == [read_id]
    assert [a[0] for (_, a, _k) in slow.submitted] == [write_id, live_id]
    # public API unchanged: the durable row exists regardless of lane
    assert jobs.get_job(read_id)["status"] == "submitted"
    assert jobs.get_job(write_id)["tool"] == "fake-write"


# =========================================================================== #
# SSE endpoint (async generator) — same event sequence for a completing job
# =========================================================================== #
def _client():
    from fastapi.testclient import TestClient
    from app import app
    return TestClient(app, raise_server_exceptions=False)


def _fake_broker_ok(tenant_id, tool, params, dwg, aps_live, timeout_s=0,
                    dwg_version=None):
    time.sleep(0.7)  # spans one 0.5s SSE tick so a pre-terminal event is observable
    return {"ok": True, "degraded_mode": False,
            "result": {"counts": {"Panels": 2345}}}


def test_sse_stream_yields_transition_sequence_until_complete(monkeypatch):
    monkeypatch.setattr(broker_client, "run_via_broker", _fake_broker_ok)
    c = _client()

    r = c.post("/api/run", json={"tool": "count-by-layer"}, headers=HDRS)
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    events = []
    with c.stream("GET", f"/api/jobs/{job_id}/stream", headers=HDRS) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))

    assert events, "no SSE events received"
    # identical wire format: one JSON object per transition, fixed key set
    for ev in events:
        assert set(ev) == {"job_id", "status", "progress", "elapsed_ms", "error"}
        assert ev["job_id"] == job_id
    # transition-only emission: no duplicate (status, progress) pairs
    keys = [(ev["status"], ev["progress"]) for ev in events]
    assert len(keys) == len(set(keys))
    # the 0.7s broker sleep spans a 0.5s tick precisely so a PRE-terminal
    # transition is observable — a stream that emits only the terminal frame
    # (skipping submitted/running) must fail here, not pass silently
    assert any(ev["status"] in ("submitted", "running") for ev in events[:-1]), (
        "no pre-terminal transition observed — the stream must emit each "
        "(status, progress) transition, not just the terminal frame")
    # stream closed after terminal, with the job's success visible
    assert events[-1]["status"] == "complete"
    assert events[-1]["error"] is None
    assert jobs.get_job(job_id)["result"]["ok"] is True


def test_sse_unknown_job_is_404_envelope():
    r = _client().get("/api/jobs/does-not-exist/stream", headers=HDRS)
    assert r.status_code == 404
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"


def test_wait_fallback_still_returns_final_envelope(monkeypatch):
    """?wait=1 (sync back-compat path) is untouched by the async SSE rewrite."""
    monkeypatch.setattr(
        broker_client, "run_via_broker",
        lambda *a, **k: {"ok": True, "degraded_mode": False,
                         "result": {"counts": {"Panels": 2345}}})
    r = _client().post("/api/run?wait=1", json={"tool": "count-by-layer"}, headers=HDRS)
    assert r.status_code == 200
    assert r.json()["ok"] is True
