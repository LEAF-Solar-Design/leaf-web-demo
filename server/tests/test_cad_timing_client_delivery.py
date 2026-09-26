"""First terminal result delivery timing for the CAD job execution lane."""
from __future__ import annotations

import copy
import os
import sys
import uuid
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import broker_client  # noqa: E402
import jobs  # noqa: E402
import write_loop  # noqa: E402
from routers import jobs as jobs_router  # noqa: E402


TOOL = {"name": "cad-delivery-test", "engine_op": "count_by_layer",
        "capabilities": ["drawing.read"]}


class RecordingExecutor:
    def submit(self, *args, **kwargs):
        pass


@pytest.fixture(autouse=True)
def isolated_jobs(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_JOBS_STORE", "legacy")
    monkeypatch.setattr(jobs, "DB_PATH", tmp_path / "cad-delivery.db")
    monkeypatch.setattr(jobs, "_conn", None)
    monkeypatch.setattr(jobs, "_reaper_started", True)
    monkeypatch.setattr(jobs, "_executors", {
        jobs.LANE_FAST: RecordingExecutor(), jobs.LANE_SLOW: RecordingExecutor(),
    })
    monkeypatch.setattr(jobs.platform_link, "on_submit", lambda *a, **k: None)
    monkeypatch.setattr(jobs.platform_link, "on_running", lambda *a, **k: None)
    monkeypatch.setattr(jobs.platform_link, "on_terminal", lambda *a, **k: None)
    monkeypatch.setattr(jobs_router.deps, "auth_live", lambda: False)
    yield
    jobs.reset_connection()


def _timing():
    return {
        "contract": "leaf.cad-timing.v1",
        "total_ms": 42,
        "spans_ms": {"engine": 6, "image_pull": None, "client_delivery": None},
        "provider_accounted_ms": 6,
        "unavailable_spans": ["image_pull", "client_delivery"],
    }


def _finished_job(monkeypatch, *, cad=True):
    job_id = jobs.submit_job("tenant-cad", TOOL, {}, "demo", True)
    envelope = {"ok": True, "result": {"new_version": {"version": 2}}}
    if cad:
        envelope["execution_provenance"] = {"cad_timing": _timing()}
    monkeypatch.setattr(broker_client, "run_via_broker", lambda *a, **k: envelope)
    jobs._run_job(job_id, "tenant-cad", TOOL, {}, "demo", True)
    assert jobs.get_job(job_id)["status"] == "complete"
    return job_id


def test_apply_client_delivery_fills_span_and_drops_unavailable():
    timing = _timing()
    original = copy.deepcopy(timing)
    result = write_loop.apply_client_delivery(timing, 100.0, 100.125)
    assert result is not timing
    assert result["spans_ms"] is not timing["spans_ms"]
    assert result["spans_ms"]["client_delivery"] == 125
    assert result["unavailable_spans"] == ["image_pull"]
    assert result["spans_ms"]["image_pull"] is None
    assert result["total_ms"] == 42
    assert timing == original
    assert write_loop.apply_client_delivery(timing, 100, 100)["spans_ms"]["client_delivery"] == 0


def test_apply_client_delivery_leaves_null_on_bad_or_negative_input():
    for finished, delivered in [
        (None, 101), (100, None), ("100", 101), (100, "101"),
        (False, 101), (100, True), (float("nan"), 101), (100, float("nan")),
        (float("inf"), 101), (100, float("inf")), (100, float("-inf")), (101, 100),
    ]:
        timing = _timing()
        result = write_loop.apply_client_delivery(timing, finished, delivered)
        assert result == timing
        assert result is not timing
        assert result["spans_ms"]["client_delivery"] is None
        assert "client_delivery" in result["unavailable_spans"]
    for timing in [dict(_timing(), contract="other"),
                   {"contract": "leaf.cad-timing.v1"},
                   dict(_timing(), spans_ms=None)]:
        result = write_loop.apply_client_delivery(timing, 100, 101)
        assert result == timing
        assert result is not timing
    for timing in (None, [], "bad"):
        assert write_loop.apply_client_delivery(timing, 100, 101) == {}


def test_first_terminal_read_stamps_delivery_once(monkeypatch):
    job_id = _finished_job(monkeypatch)
    before = jobs.get_job(job_id)
    assert "client_delivered_at" not in before["provenance"]
    assert jobs.record_first_delivery(job_id, now=100.0) == 100.0
    assert jobs.record_first_delivery(job_id, now=200.0) == 100.0
    after = jobs.get_job(job_id)
    assert after["provenance"] == dict(before["provenance"], client_delivered_at=100.0)
    assert after["result"] == before["result"]
    assert jobs.record_first_delivery("missing-job", now=100.0) is None
    failed_id = jobs.submit_job("tenant-cad", TOOL, {}, "demo", False)
    jobs._exec("UPDATE jobs SET status = 'failed' WHERE job_id = ?", (failed_id,))
    assert jobs.record_first_delivery(failed_id, now=100.0) == 100.0
    assert jobs.record_first_delivery(failed_id, now=200.0) == 100.0


def test_nonterminal_job_is_not_stamped():
    job_id = jobs.submit_job("tenant-cad", TOOL, {}, "demo", False)
    for status in ("submitted", "running"):
        jobs._exec("UPDATE jobs SET status = ? WHERE job_id = ?", (status, job_id))
        before = jobs.get_job(job_id)
        assert jobs.record_first_delivery(job_id, now=100.0) is None
        assert jobs.get_job(job_id) == before
        assert jobs_router._with_client_delivery(before) is before


def test_get_job_route_serves_client_delivery(monkeypatch):
    job_id = _finished_job(monkeypatch)
    original = jobs.get_job(job_id)
    snapshot = copy.deepcopy(original)
    assert jobs_router.get_job(job_id, "other-tenant").status_code == 404
    assert "client_delivered_at" not in jobs.get_job(job_id)["provenance"]
    with monkeypatch.context() as denied:
        response = jobs_router.JSONResponse(status_code=403, content={})
        denied.setattr(jobs_router, "_access_error", lambda *a, **k: response)
        assert jobs_router.get_job(job_id, "tenant-cad") is response
    assert "client_delivered_at" not in jobs.get_job(job_id)["provenance"]
    monkeypatch.setattr(jobs_router, "_job_for_tenant", lambda *a: original)
    first = jobs_router.get_job(job_id, "tenant-cad")
    second = jobs_router.get_job(job_id, "tenant-cad")
    first_timing = first["result"]["execution_provenance"]["cad_timing"]
    second_timing = second["result"]["execution_provenance"]["cad_timing"]
    span = first_timing["spans_ms"]["client_delivery"]
    assert isinstance(span, int) and span >= 0
    assert second_timing["spans_ms"]["client_delivery"] == span
    assert "client_delivery" not in first_timing["unavailable_spans"]
    assert first_timing["spans_ms"]["image_pull"] is None
    assert "image_pull" in first_timing["unavailable_spans"]
    assert first["provenance"]["cad_timing"] == first_timing
    assert original == snapshot
    assert jobs.get_job(job_id)["provenance"]["cad_timing"] == _timing()


def test_get_job_route_does_not_stamp_jobs_without_cad_timing(monkeypatch):
    job_id = _finished_job(monkeypatch, cad=False)
    before = jobs.get_job(job_id)
    assert jobs_router._with_client_delivery(before) is before
    result = jobs_router.get_job(job_id, "tenant-cad")
    assert "client_delivered_at" not in result["provenance"]
    assert jobs.get_job(job_id) == before


@pytest.fixture
def postgres_authority(monkeypatch, isolated_jobs):
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL is required for PostgreSQL job tests")
    from job_pg_store import _db

    db = _db()
    db.apply_migration()
    monkeypatch.setenv("LEAF_JOBS_STORE", "postgres")
    return db


def test_postgres_record_first_delivery_is_idempotent(postgres_authority):
    db = postgres_authority
    job_id = jobs.submit_job(
        "tenant-cad-" + uuid.uuid4().hex, TOOL, {}, "demo", False,
        project_id="project-" + uuid.uuid4().hex,
        idempotency_key="delivery-" + uuid.uuid4().hex,
        authority_mode="postgres_canonical",
    )
    try:
        assert jobs.record_first_delivery(job_id, now=100.0) is None
        assert jobs.record_first_delivery("missing-" + uuid.uuid4().hex, now=100.0) is None
        with db.transaction() as conn:
            conn.execute(
                "UPDATE async_jobs SET status = 'complete', provenance_json = "
                "'{\"execution_path\": \"cloud\"}'::jsonb WHERE job_id = %s", (job_id,),
            )
        assert jobs.record_first_delivery(job_id, now=100.0) == 100.0
        assert jobs.record_first_delivery(job_id, now=200.0) == 100.0
        assert jobs.get_job(job_id)["provenance"] == {
            "execution_path": "cloud", "client_delivered_at": 100.0,
        }
    finally:
        with db.transaction() as conn:
            conn.execute("DELETE FROM async_jobs WHERE job_id = %s", (job_id,))
