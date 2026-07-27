"""
SubmitLatency (CloudWatch EMF) — the production-side half of the POST /api/run
202 contract.

`contract/CONTRACT.md` §7 documents POST /api/run as returning "HTTP 202
immediately (<200 ms)". That is a p-latency claim on real traffic, and no test
that runs on `ubuntu-latest` can prove it: tests/test_backbone.py
`test_1b_submit_cost_is_independent_of_execution_time` bounds the DIFFERENCE OF
MEANS between a 4s job and a 0s job precisely because no single-sample threshold
fits that host's tail. So the absolute bound is alarmed on in production, from
`emf_metrics.emit_submit_latency`, and what this file gates is the emission: that
it happens on a successful submit, that its dimension set and unit are the ones
the alarm queries, and that the value CANNOT contain execution time.

The execution-independence test spends no timing budget at all. Its broker double
parks inside the run until this test releases an Event, so the job's execution
duration is chosen by the test rather than raced against the host.

Run:  cd server && python -m pytest tests/test_submit_latency_metric.py -q
"""
from __future__ import annotations

# Cache the STDLIB `platform` module BEFORE anything puts PROJECT_ROOT on
# sys.path (the local `platform/` package otherwise shadows it — mirrors
# tests/test_job_lanes.py).
import platform as _stdlib_platform  # noqa: E402
_stdlib_platform.python_implementation()

import os  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Dict, List, Optional  # noqa: E402

import pytest  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent.parent

os.environ.setdefault(
    "JOBS_DB", str(Path(tempfile.mkdtemp(prefix="submitlat-jobs-")) / "jobs.db"))

if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import broker_client  # noqa: E402
import emf_metrics  # noqa: E402
import jobs  # noqa: E402
from _test_run_confirmation import confirmed_client_payload  # noqa: E402

HDRS = {"X-Tenant-Id": "demo-tenant"}
TOOL = "count-by-layer"

# The job is held open by an Event, not by a sleep, so this is only the ceiling
# the double refuses to hang past if a test fails before releasing it.
GATE_CEILING_S = 30.0
# How long the test deliberately keeps the job executing before releasing it.
# `elapsed_ms` on the finished job is therefore at least this, which is what the
# emitted submit value is compared against. Nothing here races the host: a
# submit that really did cost more than this is a contract violation, not noise.
HOLD_S = 1.0
DRAIN_TIMEOUT_S = 30.0


@pytest.fixture
def captured(monkeypatch) -> List[Dict[str, Any]]:
    """Every EMF document the process writes during the test, in order.

    Patching `_write` rather than reading stderr keeps the assertion on the
    document itself and works regardless of which thread emitted it (the job
    worker emits JobTerminal from a pool thread).
    """
    docs: List[Dict[str, Any]] = []
    monkeypatch.setattr(emf_metrics, "_DISABLED", False)
    monkeypatch.setattr(emf_metrics, "_write", docs.append)
    return docs


def _submit_latency_docs(docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [d for d in docs if "SubmitLatency" in d]


def _only_submit_latency(docs: List[Dict[str, Any]]) -> Dict[str, Any]:
    found = _submit_latency_docs(docs)
    assert len(found) == 1, (
        f"expected exactly one SubmitLatency document, got {len(found)}: {found}")
    return found[0]


def _client():
    from fastapi.testclient import TestClient
    from app import app
    return TestClient(app, raise_server_exceptions=False)


def _drain(job_id: str) -> Dict[str, Any]:
    deadline = time.time() + DRAIN_TIMEOUT_S
    rec = jobs.get_job(job_id)
    while rec is not None and rec["status"] not in jobs.TERMINAL \
            and time.time() < deadline:
        time.sleep(0.05)
        rec = jobs.get_job(job_id)
    assert rec is not None, f"job {job_id} vanished"
    assert rec["status"] in jobs.TERMINAL, (
        f"job {job_id} never reached terminal: {rec['status']}")
    return rec


# =========================================================================== #
# emitter shape — what the alarm actually queries
# =========================================================================== #
def test_submit_latency_publishes_milliseconds_under_environment_and_aps_live(
        captured, monkeypatch):
    monkeypatch.setenv("LEAF_METRICS_ENVIRONMENT", "production")
    emf_metrics.emit_submit_latency(
        11.5, aps_live=True, tenant_id="acme", tool="panelize", job_id="job-abc")

    doc = _only_submit_latency(captured)
    directives = doc["_aws"]["CloudWatchMetrics"]
    # ONE directive with ONE dimension set: a metric published under two sets
    # doubles when a consumer sums across them (the module docstring's rule).
    # Adding `environment` EXTENDED the existing set rather than adding a second
    # one, precisely so that rule still holds.
    assert len(directives) == 1
    assert directives[0]["Namespace"] == "Leaf/Platform/APS"
    assert directives[0]["Dimensions"] == [["environment", "aps_live"]]
    assert directives[0]["Metrics"] == [
        {"Name": "SubmitLatency", "Unit": "Milliseconds"}]
    assert doc["SubmitLatency"] == 11.5
    # Both dimensions the p99 alarm scopes to, as strings (CloudWatch requires
    # a dimension value to be a string).
    assert doc["environment"] == "production"
    assert doc["aps_live"] == "true"
    # High-cardinality attribution rides as LOG fields, never as dimensions.
    assert doc["tenant_id"] == "acme"
    assert doc["tool"] == "panelize"
    assert doc["job_id"] == "job-abc"
    assert "tenant_id" not in directives[0]["Dimensions"][0]


# --------------------------------------------------------------------------- #
# the `environment` dimension — this namespace is shared by staging and
# production, so the value has to be right or an alarm watches the wrong fleet
# --------------------------------------------------------------------------- #
def test_environment_dimension_distinguishes_the_two_deployments(
        captured, monkeypatch):
    """The whole point: two deployments must land on two different series."""
    monkeypatch.setenv("LEAF_METRICS_ENVIRONMENT", "production")
    emf_metrics.emit_submit_latency(1.0, aps_live=True)
    monkeypatch.setenv("LEAF_METRICS_ENVIRONMENT", "staging")
    emf_metrics.emit_submit_latency(2.0, aps_live=True)

    docs = _submit_latency_docs(captured)
    assert [d["environment"] for d in docs] == ["production", "staging"]
    # Same metric, same aps_live, different series. An alarm scoped to
    # environment="production" sees the first and not the second.
    assert {d["aps_live"] for d in docs} == {"true"}


def test_environment_is_not_read_from_leaf_runtime_env(captured, monkeypatch):
    """`LEAF_RUNTIME_ENV` must NOT be the source.

    Staging's app and broker both run LEAF_RUNTIME_ENV=production (it selects the
    fail-closed posture, not the deployment). If this emitter ever read it, every
    staging submit would publish as production and the dimension would recreate
    the confusion it was added to remove.
    """
    monkeypatch.delenv("LEAF_METRICS_ENVIRONMENT", raising=False)
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "production")
    emf_metrics.emit_submit_latency(1.0, aps_live=True)

    assert _only_submit_latency(captured)["environment"] == "unset"


def test_unconfigured_environment_publishes_unset_not_a_missing_dimension(
        captured, monkeypatch):
    """An unconfigured deployment must stay VISIBLE.

    Dropping the dimension would remove the series from an environment-scoped
    alarm's view with no symptom anywhere, which is the failure this dimension
    exists to prevent, not a form of it.
    """
    monkeypatch.delenv("LEAF_METRICS_ENVIRONMENT", raising=False)
    emf_metrics.emit_submit_latency(1.0, aps_live=True)

    doc = _only_submit_latency(captured)
    assert doc["environment"] == "unset"
    assert doc["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [
        ["environment", "aps_live"]]


@pytest.mark.parametrize(
    "raw,expected",
    [("production", "production"), ("staging", "staging"),
     ("  Production  ", "production"), ("PRODUCTION", "production"),
     ("", "unset"), ("   ", "unset"),
     ("prod", "other"), ("dev", "other"), ("production-2", "other")])
def test_environment_value_is_bounded(captured, monkeypatch, raw, expected):
    """The value becomes a CloudWatch dimension, so it is clamped like `status`.

    A typo in a task definition must not be able to mint an unbounded series,
    and must not be silently dropped either: it lands on "other", which is
    visible and alarmable.
    """
    monkeypatch.setenv("LEAF_METRICS_ENVIRONMENT", raw)
    emf_metrics.emit_submit_latency(1.0, aps_live=True)

    assert _only_submit_latency(captured)["environment"] == expected


@pytest.mark.parametrize(
    "bad", [float("nan"), float("inf"), float("-inf"), -1.0, True, "12", None])
def test_submit_latency_drops_an_unusable_reading(captured, bad):
    """A broken clock must publish NOTHING rather than a fast sample.

    Flooring a failed measurement to 0 would pull the p99 down at exactly the
    moment the alarm has to be trusted, so an unusable value is dropped.
    """
    emf_metrics.emit_submit_latency(bad, aps_live=True)
    assert _submit_latency_docs(captured) == []


# =========================================================================== #
# emission on the real submit path
# =========================================================================== #
def test_api_run_202_emits_submit_latency(captured, monkeypatch):
    monkeypatch.setattr(
        broker_client, "run_via_broker",
        lambda *a, **k: {"ok": True, "degraded_mode": False,
                         "result": {"counts": {"Panels": 2345}}})
    c = _client()
    payload = confirmed_client_payload(c, TOOL, {}, "rooftop_demo", headers=HDRS)

    captured.clear()  # the catalog GET above emits nothing, but be explicit
    r = c.post("/api/run", json=payload, headers=HDRS)
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    doc = _only_submit_latency(captured)
    assert doc["job_id"] == job_id
    assert doc["tool"] == TOOL
    assert doc["tenant_id"] == "demo-tenant"
    assert doc["SubmitLatency"] > 0.0
    _drain(job_id)


def test_submit_latency_excludes_execution_time(captured, monkeypatch):
    """A job that executes for a long time must not inflate its submit metric.

    No timing budget is spent. The broker double parks inside the run until this
    test sets `gate`, so the job's execution duration is the test's choice, and
    the emitted value is compared against the duration the APP itself recorded.
    """
    gate = threading.Event()
    seen_params: List[Optional[float]] = []

    def _parked_broker(tenant_id, tool, params, dwg, aps_live, timeout_s=0,
                       **_extra):
        # The payload carries the repo's real QA latency hook; this double honors
        # it as "run long", with the gate as the release so nothing is raced.
        seen_params.append(params.get("_qa_sleep_s"))
        gate.wait(timeout=GATE_CEILING_S)
        return {"ok": True, "degraded_mode": False,
                "result": {"counts": {"Panels": 2345}}}

    monkeypatch.setattr(broker_client, "run_via_broker", _parked_broker)
    c = _client()
    payload = confirmed_client_payload(
        c, TOOL, {"_qa_sleep_s": 4.0}, "rooftop_demo", headers=HDRS)

    captured.clear()
    try:
        started = time.perf_counter()
        r = c.post("/api/run", json=payload, headers=HDRS)
        post_wall_ms = (time.perf_counter() - started) * 1000.0
        assert r.status_code == 202
        job_id = r.json()["job_id"]

        doc = _only_submit_latency(captured)
        # (1) The metric exists while the worker is still inside the broker call:
        # it cannot have returned, because `gate` is only set in the `finally`
        # below. So emission provably did not wait for execution.
        assert jobs.get_job(job_id)["status"] != "complete"
        # (2) The value is bounded by the request the CLIENT observed, so the
        # measured window is a subinterval of the POST, not of the job.
        assert doc["SubmitLatency"] <= post_wall_ms

        # Keep the job executing for a known stretch, then let it finish, and
        # compare against the app's own record of how long it ran.
        time.sleep(HOLD_S)
    finally:
        gate.set()
    rec = _drain(job_id)

    assert seen_params == [4.0], (
        f"the run did not carry the _qa_sleep_s hook: {seen_params}")
    assert rec["status"] == "complete"
    assert rec["elapsed_ms"] >= HOLD_S * 1000.0, (
        "the job did not actually execute for the held duration; the comparison "
        f"below would be vacuous (elapsed_ms={rec['elapsed_ms']})")
    assert doc["SubmitLatency"] < rec["elapsed_ms"], (
        f"submit ({doc['SubmitLatency']}ms) is not below the job's own execution "
        f"time ({rec['elapsed_ms']}ms) — the submit path is timing execution")
    # Still exactly one SubmitLatency for the whole lifecycle: the terminal path
    # emits JobTerminal, not a second submit sample.
    assert len(_submit_latency_docs(captured)) == 1
    assert any("JobTerminal" in d for d in captured)


def test_wait_path_emits_no_submit_latency(captured, monkeypatch):
    """`?wait=1` blocks on execution and returns 200, not 202.

    Timing it would put engine seconds into a submit gauge and recreate the
    coupling test_1b exists to forbid, so that population is not sampled.
    """
    monkeypatch.setattr(
        broker_client, "run_via_broker",
        lambda *a, **k: {"ok": True, "degraded_mode": False,
                         "result": {"counts": {"Panels": 2345}}})
    c = _client()
    payload = confirmed_client_payload(c, TOOL, {}, "rooftop_demo", headers=HDRS)

    captured.clear()
    r = c.post("/api/run?wait=1", json=payload, headers=HDRS)
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert _submit_latency_docs(captured) == []
