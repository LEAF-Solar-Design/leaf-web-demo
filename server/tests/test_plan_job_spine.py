"""W4g browser plans use the durable job spine and its live broker boundary."""
from __future__ import annotations

import platform as _stdlib_platform
_stdlib_platform.python_implementation()

import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import broker_client  # noqa: E402
import jobs  # noqa: E402


class _Executor:
    def __init__(self):
        self.calls = []

    def submit(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))


@pytest.fixture
def spine(monkeypatch, tmp_path):
    jobs.reset_connection()
    monkeypatch.setenv("LEAF_JOBS_STORE", "legacy")
    monkeypatch.setattr(jobs, "DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(jobs, "ensure_started", lambda: None)
    monkeypatch.setattr(jobs, "_write_terminal_receipt", lambda *_a: None)
    monkeypatch.setattr(jobs, "_emit_job_terminal", lambda *_a: None)
    monkeypatch.setattr(jobs, "_emit_job_terminal_event", lambda *_a, **_k: None)
    for name in ("on_submit", "on_running", "forget"):
        monkeypatch.setattr(jobs.platform_link, name, lambda *_a, **_k: None)
    executors = {jobs.LANE_FAST: _Executor(), jobs.LANE_SLOW: _Executor()}
    monkeypatch.setattr(jobs, "_executors", executors)
    yield executors
    jobs.reset_connection()


@pytest.fixture
def plan():
    return {"drawing_id": "browser-drawing", "parent_version": 3,
            "mutations": {"delete": ["1A"]},
            "plan_sha256": "a" * 64, "source_sha256": "b" * 64}


def _submit(plan):
    return jobs.submit_plan_job(
        "tenant-plan", plan, plan["drawing_id"],
        checkout_holder="browser-tab", checkout_fence=7,
    )


def _execution(job_id):
    row = jobs._query("SELECT execution_json FROM jobs WHERE job_id = ?", (job_id,))[0]
    return json.loads(row["execution_json"])


def test_plan_submission_persists_full_execution_and_get_record(spine, plan):
    job_id = _submit(plan)
    record = jobs.get_job(job_id)
    assert record["tool"] == "cad-edit-plan"
    assert record["params"] == plan
    assert record["dwg_version"] == plan["parent_version"]
    assert record["status"] == "submitted"
    assert _execution(job_id) == {
        "tool": {"name": "cad-edit-plan", "version": "1.0.0",
                 "capabilities": ["drawing.write"], "kind": "plan"},
        "plan": plan, "aps_live": True, "dwg_version": 3,
        "checkout_holder": "browser-tab", "checkout_fence": 7,
    }
    assert not spine[jobs.LANE_FAST].calls
    fn, args, kwargs = spine[jobs.LANE_SLOW].calls[0]
    assert fn is jobs._run_job
    assert args == (job_id, "tenant-plan", jobs.PLAN_TOOL, plan,
                    "browser-drawing", True, 3, "browser-tab", 7)
    assert kwargs == {"plan": plan}
    assert jobs._progress_phase(jobs.PLAN_TOOL, True, plan) == "applying plan"


def test_plan_size_cap_covers_more_than_mutations(monkeypatch, spine, plan):
    # The mutations alone fit, but the full plan (including digests) does not.
    monkeypatch.setattr(jobs, "MAX_PARAMS_BYTES", 64)
    assert len(json.dumps(plan["mutations"]).encode("utf-8")) < 64
    with pytest.raises(HTTPException) as plan_error:
        _submit(plan)
    with pytest.raises(HTTPException) as tool_error:
        jobs.submit_job("tenant-plan", jobs.PLAN_TOOL, plan, "browser-drawing", True)
    assert plan_error.value.status_code == tool_error.value.status_code == 400
    assert plan_error.value.detail == tool_error.value.detail
    assert jobs.list_jobs("tenant-plan") == []
    assert not spine[jobs.LANE_SLOW].calls


def test_plan_worker_uses_plan_broker_and_completes(monkeypatch, spine, plan):
    calls = []
    envelope = {"ok": True, "tool": "cad-edit-plan", "version": "1.0.0",
                "result": {"version": 4, "activity_version": 12}, "error": None}

    def run_plan(*args, **kwargs):
        calls.append((args, kwargs))
        assert jobs.get_job(job_id)["progress"] == "applying plan"
        return envelope

    def generic(*_a, **_k):
        pytest.fail("a plan must never execute through the generic broker")

    monkeypatch.setattr(broker_client, "run_plan_via_broker", run_plan)
    monkeypatch.setattr(broker_client, "run_via_broker", generic)
    job_id = _submit(plan)
    fn, args, kwargs = spine[jobs.LANE_SLOW].calls[0]
    fn(*args, **kwargs)
    assert calls == [(("tenant-plan", plan, "browser-drawing"), {
        "timeout_s": jobs.job_max_s() + 30, "dwg_version": 3,
        "ledger_event_key": f"{job_id}:broker-run",
        "checkout_holder": "browser-tab", "checkout_fence": 7, "job_id": job_id,
    })]
    record = jobs.get_job(job_id)
    assert record["status"] == "complete"
    assert record["result"]["result"] == envelope["result"]
    assert record["provenance"] == {"attempt": 1, "execution_path": "cloud"}
    assert record["lease"] is None


def test_plan_recovery_preserves_plan_and_refuses_missing_context(spine, plan):
    job_id = _submit(plan)
    executor = spine[jobs.LANE_SLOW]
    initial = executor.calls.pop()
    assert jobs._redispatch_record(job_id) is True
    assert executor.calls.pop() == initial

    execution = _execution(job_id)
    del execution["plan"]
    jobs._exec("UPDATE jobs SET execution_json = ? WHERE job_id = ?",
               (json.dumps(execution), job_id))
    assert jobs._redispatch_record(job_id) is False
    assert not executor.calls
    record = jobs.get_job(job_id)
    assert record["status"] == "failed"
    assert record["error"]["error_code"] == "INTERNAL"
    assert "cannot recover job" in record["error"]["message"]


def test_plan_recovery_refuses_a_catalog_tool_hidden_in_execution(spine, plan):
    plan["parent_version"] = 1
    job_id = _submit(plan)
    spine[jobs.LANE_SLOW].calls.clear()
    catalog = json.loads((SERVER_DIR / "write_tools.json").read_text(encoding="utf-8"))
    tool = next(tool for tool in catalog["tools"] if tool["name"] == "delete-marked-panel")
    execution = {"tool": tool, "aps_live": False, "dwg_version": 1}
    jobs._exec("UPDATE jobs SET execution_json = ? WHERE job_id = ?",
               (json.dumps(execution), job_id))
    assert jobs.get_job(job_id)["tool"] == "cad-edit-plan"
    assert jobs._redispatch_record(job_id) is False
    assert all(not executor.calls for executor in spine.values())
    record = jobs.get_job(job_id)
    assert record["status"] == "failed"
    assert record["error"]["error_code"] == "INTERNAL"
    assert record["error"]["message"] == "cannot recover job: missing execution context"
