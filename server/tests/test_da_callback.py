"""Callback-primary completion seam tests.

Run from ``server/``: ``python -m pytest tests/test_da_callback.py -q``.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import broker


@pytest.fixture(autouse=True)
def callback_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_CALLBACK_SECRET", "test-callback-secret")
    monkeypatch.setenv("JOBS_DB", str(tmp_path / "jobs.db"))
    monkeypatch.delenv("LEAF_CALLBACK_PRIMARY", raising=False)
    monkeypatch.delenv("LEAF_CALLBACK_URL", raising=False)
    monkeypatch.delenv("LEAF_CALLBACK_MAX_AGE_S", raising=False)
    assert broker._get_callbacks() is not None


def _signed(body: bytes, timestamp: str, nonce: str) -> str:
    callbacks = broker._get_callbacks()
    assert callbacks is not None
    return callbacks.sign_payload(body, timestamp, nonce)


def test_callback_signature_covers_timestamp_nonce_and_uses_compare_digest(monkeypatch):
    callbacks = broker._get_callbacks()
    assert callbacks is not None
    observed = []
    original = callbacks.hmac.compare_digest

    def spy(expected, supplied):
        observed.append((expected, supplied))
        return original(expected, supplied)

    monkeypatch.setattr(callbacks.hmac, "compare_digest", spy)
    body = b'{"job_id":"job-1","status":"success"}'
    timestamp = str(time.time())
    nonce = "nonce-1"
    signature = _signed(body, timestamp, nonce)
    assert callbacks.consume_callback(body, signature, timestamp, nonce)["ok"] is True
    # A captured body and signature cannot be replayed under a fresh nonce.
    assert callbacks.consume_callback(body, signature, timestamp, "fresh-nonce") == {
        "ok": False, "reason": "bad_signature"}
    assert observed, "signature verification must call hmac.compare_digest"


def test_invalid_or_missing_signature_fails_closed_without_creating_replay_state(tmp_path):
    body = b'{"job_id":"job-2","status":"success"}'
    response = TestClient(broker.app).post(
        "/da/callback", content=body,
        headers={"X-Leaf-Timestamp": str(time.time()), "X-Leaf-Nonce": "nonce-2"},
    )
    assert response.status_code == 401
    assert response.json()["ok"] is False
    assert response.json()["error"]["error_code"] == "BAD_PARAMS"
    assert not (tmp_path / "jobs.db").exists()


def test_poll_default_callback_flag_is_reserved_and_reaper_fallback(monkeypatch):
    class DA:
        def __init__(self):
            self.calls = []

        def run_tool(self, local, tool, params):
            self.calls.append("poll")
            return {"ok": True}

        def run_tool_callback(self, local, tool, params, *, callback_url):
            self.calls.append(("callback", callback_url))
            return {"ok": True}

        def cancel_workitem(self, workitem_id):
            self.calls.append(("reap", workitem_id))
            return {"cancelled": True}

    da = DA()
    monkeypatch.setattr(broker, "_get_da", lambda: da)
    assert broker._run_live_tool(da, "drawing.dwg", {"name": "tool"}, {}) == {"ok": True}
    assert da.calls == ["poll"]

    monkeypatch.setenv("LEAF_CALLBACK_PRIMARY", "1")
    monkeypatch.setenv("LEAF_CALLBACK_URL", "https://example.test/da/callback")
    with pytest.raises(
        broker.CallbackPrimaryUnavailable,
        match="callback-primary is reserved.*translation adapter",
    ):
        broker._run_live_tool(da, "drawing.dwg", {"name": "tool"}, {})
    # The reserved flag must neither submit through a future-looking adapter
    # method nor silently fall back to polling.
    assert da.calls == ["poll"]

    response = broker.broker_reap(broker.BrokerReapRequest(
        records=[{"status": "submitted", "workitem_id": "wi-1", "session_closed": True}],
        live=True,
    ))
    assert response.status_code == 200
    assert ("reap", "wi-1") in da.calls


def test_live_write_callback_flag_fails_before_aps_side_effects(monkeypatch, tmp_path):
    calls = []

    class DA:
        def signed_download_url(self, _key):
            calls.append("signed_download_url")
            return "https://example.test/input"

        def signed_upload_url(self, _key):
            calls.append("signed_upload_url")
            return "upload-key", "https://example.test/output"

        def activity_qualified(self, _name):
            calls.append("activity_qualified")
            return "owner.LeafWriteProbe+prod"

        def ensure_tool_activity(self, *_args, **_kwargs):
            calls.append("ensure_tool_activity")
            return {}

        def submit_workitem(self, *_args, **kwargs):
            calls.append(("submit_workitem", kwargs.get("poll")))
            return {"id": "wi-should-not-exist", "status": "success"}

        def run_tool(self, *_args, **_kwargs):
            calls.append("run_tool")
            return {"ok": True}

    tenant_id = "callback-reserved-write"
    monkeypatch.setenv("LEAF_CALLBACK_PRIMARY", "1")
    monkeypatch.setenv("LEAF_CALLBACK_URL", "https://example.test/da/callback")
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "drawings"))
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "broker-ledger.jsonl")
    monkeypatch.setattr(broker, "_cap_preflight", lambda *_args: None)
    monkeypatch.setattr(broker, "_run_quota_preflight", lambda *_args: None)
    monkeypatch.setattr(broker, "_resolve_live_dwg", lambda _dwg: tmp_path / "input.dwg")
    monkeypatch.setattr(broker, "_get_da", lambda: DA())

    backend = broker.write_loop.default_backend(aps_live=False)
    broker.write_loop.ensure_demo_drawing(
        backend, tenant_id, broker.write_loop.DEMO_DRAWING_ID)
    monkeypatch.setattr(
        broker.write_loop, "default_backend", lambda **_kwargs: backend)
    response = broker.broker_run(broker.BrokerRunRequest(
        tenant_id=tenant_id,
        tool={
            "name": "reserved-live-write",
            "capabilities": ["drawing.write"],
            "params_schema": {
                "type": "object",
                "properties": {"drawing_id": {"type": "string"}},
            },
        },
        params={"drawing_id": "demo"},
        dwg="rooftop_demo",
        aps_live=True,
    ))

    body = json.loads(response.body)
    assert response.status_code == 500, body
    assert body["error"]["error_code"] == "INTERNAL"
    assert body["error"]["retryable"] is False
    assert "callback-primary is reserved" in body["error"]["message"]
    assert calls == []


@pytest.mark.parametrize(
    ("capabilities", "params"),
    [
        (["drawing.read"], {}),
        (["drawing.write"], {"drawing_id": "demo"}),
    ],
    ids=("live-read", "live-write"),
)
def test_primary_flag_fails_closed_when_callback_module_is_unavailable(
        monkeypatch, tmp_path, capabilities, params):
    calls = []

    class DA:
        def run_tool(self, *_args, **_kwargs):
            calls.append("run_tool")
            return {"ok": True}

    def get_da():
        calls.append("get_da")
        return DA()

    def resolve_live_dwg(_dwg):
        calls.append("resolve_live_dwg")
        return tmp_path / "input.dwg"

    monkeypatch.setenv("LEAF_CALLBACK_PRIMARY", "1")
    monkeypatch.setenv("LEAF_CALLBACK_URL", "https://example.test/da/callback")
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "broker-ledger.jsonl")
    monkeypatch.setattr(broker, "_cap_preflight", lambda *_args: None)
    monkeypatch.setattr(broker, "_run_quota_preflight", lambda *_args: None)
    monkeypatch.setattr(broker, "_get_callbacks", lambda: None)
    monkeypatch.setattr(broker, "_get_da", get_da)
    monkeypatch.setattr(broker, "_resolve_live_dwg", resolve_live_dwg)

    response = broker.broker_run(broker.BrokerRunRequest(
        tenant_id="callback-module-unavailable",
        tool={
            "name": "module-unavailable-live-tool",
            "capabilities": capabilities,
            "params_schema": {
                "type": "object",
                "properties": {"drawing_id": {"type": "string"}},
            },
        },
        params=params,
        dwg="rooftop_demo",
        aps_live=True,
    ))

    body = json.loads(response.body)
    assert response.status_code == 500, body
    assert body["error"]["error_code"] == "INTERNAL"
    assert body["error"]["retryable"] is False
    assert "callback module is unavailable" in body["error"]["message"]
    assert calls == []


def test_missing_callback_module_preserves_default_polling(monkeypatch):
    class DA:
        def __init__(self):
            self.calls = []

        def run_tool(self, *_args, **_kwargs):
            self.calls.append("poll")
            return {"ok": True}

    da = DA()
    monkeypatch.delenv("LEAF_CALLBACK_PRIMARY", raising=False)
    monkeypatch.setattr(broker, "_get_callbacks", lambda: None)
    assert broker._run_live_tool(da, "drawing.dwg", {"name": "tool"}, {}) == {"ok": True}
    assert da.calls == ["poll"]


def test_nonce_replay_survives_a_fresh_replay_store_instance(tmp_path):
    callbacks = broker._get_callbacks()
    assert callbacks is not None
    body = b'{"job_id":"job-3","status":"success"}'
    timestamp = "1000"
    nonce = "nonce-3"
    signature = _signed(body, timestamp, nonce)
    db_path = tmp_path / "replays.db"
    first_store = callbacks.CallbackReplayStore(db_path)
    assert callbacks.consume_callback(body, signature, timestamp, nonce, now=1000.0,
                                      replay_store=first_store)["ok"] is True
    # A fresh object models a restarted broker process using the same durable DB.
    restarted_store = callbacks.CallbackReplayStore(db_path)
    assert callbacks.consume_callback(body, signature, timestamp, nonce, now=1000.0,
                                      replay_store=restarted_store) == {
        "ok": False, "reason": "replay"}


def test_callback_completes_the_durable_job_and_poll_duplicate_is_a_noop(monkeypatch, tmp_path):
    import jobs

    class InertExecutor:
        def submit(self, *args, **kwargs):
            return None

    if jobs._conn is not None:
        jobs._conn.close()
    monkeypatch.setattr(jobs, "DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(jobs, "_conn", None)
    monkeypatch.setattr(jobs, "_reaper_started", True)
    monkeypatch.setattr(jobs, "_executors", {"fast": InertExecutor(), "slow": InertExecutor()})
    tool = {"name": "callback-tool", "engine_op": "count_by_layer",
            "capabilities": ["drawing.read"]}
    job_id = jobs.submit_job("demo-tenant", tool, {}, "rooftop_demo", aps_live=True)
    assert jobs.claim_lease(job_id, "callback-worker") == 1

    body = json.dumps({"job_id": job_id, "status": "success", "result": {"answer": 42}}).encode()
    timestamp = str(time.time())
    nonce = "nonce-complete"
    response = TestClient(broker.app).post(
        "/da/callback", content=body,
        headers={"X-Leaf-Signature": _signed(body, timestamp, nonce),
                 "X-Leaf-Timestamp": timestamp, "X-Leaf-Nonce": nonce},
    )
    assert response.status_code == 200, response.text
    assert response.json()["completion"] == "applied"
    completed = jobs.get_job(job_id)
    assert completed is not None and completed["status"] == "complete"
    assert completed["result"]["answer"] == 42
    # The poll path uses the same jobs.complete_callback chokepoint. Its later,
    # identical terminal delivery is a no-op rather than a second transition.
    assert jobs.complete_callback(job_id, "complete", result_env=completed["result"],
                                  provenance=completed["provenance"]) == "duplicate"
    assert jobs.get_job(job_id)["status"] == "complete"


@pytest.mark.parametrize("missing", ["LEAF_CALLBACK_SECRET", "LEAF_CALLBACK_URL"])
def test_callback_primary_misconfiguration_fails_closed(monkeypatch, missing):
    class DA:
        def __init__(self):
            self.poll_calls = 0

        def run_tool(self, local, tool, params):
            self.poll_calls += 1
            return {"ok": True}

    da = DA()
    monkeypatch.setenv("LEAF_CALLBACK_PRIMARY", "1")
    monkeypatch.setenv("LEAF_CALLBACK_URL", "https://example.test/da/callback")
    if missing == "LEAF_CALLBACK_SECRET":
        monkeypatch.delenv(missing)
    else:
        monkeypatch.delenv(missing)
    with pytest.raises(broker.CallbackPrimaryConfigurationError, match=missing):
        broker._run_live_tool(da, "drawing.dwg", {"name": "tool"}, {})
    assert da.poll_calls == 0


def test_a_stale_attempts_callback_cannot_complete_a_newer_attempt(monkeypatch, tmp_path):
    """ROUND-10 FINDING. `_complete_callback_job` read the attempt off the JOB
    RECORD and stamped it onto the provenance, discarding the attempt the adapter
    had SIGNED. So a delayed attempt-1 envelope arriving after the lease was
    reclaimed and attempt 2 started was relabelled `attempt: 2` and used to
    complete attempt 2 — a receipt naming an attempt whose output it does not
    describe, and a signature over `attempt: 1` that no longer matches the claim
    the spine recorded.

    The signed attempt now decides applicability, and a disagreement is refused.
    """
    import jobs

    class InertExecutor:
        def submit(self, *args, **kwargs):
            return None

    if jobs._conn is not None:
        jobs._conn.close()
    monkeypatch.setattr(jobs, "DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(jobs, "_conn", None)
    monkeypatch.setattr(jobs, "_reaper_started", True)
    monkeypatch.setattr(jobs, "_executors", {"fast": InertExecutor(), "slow": InertExecutor()})
    tool = {"name": "callback-tool", "engine_op": "count_by_layer",
            "capabilities": ["drawing.read"]}
    job_id = jobs.submit_job("demo-tenant", tool, {}, "rooftop_demo", aps_live=True)
    assert jobs.claim_lease(job_id, "worker-1") == 1
    # The lease is reclaimed and a second attempt starts, exactly as happens when
    # attempt 1's lease expires: claim_lease's compare-and-set accepts a job whose
    # `lease_expires_at <= now`, so a later clock is all it takes.
    assert jobs.claim_lease(job_id, "worker-2", now=time.time() + 86_400) == 2
    assert int(jobs.get_job(job_id)["attempt"]) == 2

    def deliver(payload):
        body = json.dumps(payload).encode()
        timestamp = str(time.time())
        nonce = f"nonce-{payload.get('attempt')}-{payload['status']}"
        return TestClient(broker.app).post(
            "/da/callback", content=body,
            headers={"X-Leaf-Signature": _signed(body, timestamp, nonce),
                     "X-Leaf-Timestamp": timestamp, "X-Leaf-Nonce": nonce},
        )

    # Attempt 1's late success envelope must NOT close attempt 2.
    stale = deliver({"job_id": job_id, "status": "success", "attempt": 1,
                     "workitem_id": "wi-attempt-1", "result": {"answer": 1}})
    assert stale.status_code == 409, stale.text
    assert jobs.get_job(job_id)["status"] != "complete", (
        "a stale attempt's callback completed a newer attempt")

    # The failure branch carried no attempt at all, so it had the same hole.
    stale_failure = deliver({"job_id": job_id, "status": "failed", "attempt": 1,
                             "workitem_id": "wi-attempt-1", "message": "nope"})
    assert stale_failure.status_code == 409, stale_failure.text
    assert jobs.get_job(job_id)["status"] != "failed"

    # The CURRENT attempt's callback still completes it, and the receipt carries
    # the signed attempt and WorkItem rather than dropping them.
    current = deliver({"job_id": job_id, "status": "success", "attempt": 2,
                       "workitem_id": "wi-attempt-2", "result": {"answer": 42}})
    assert current.status_code == 200, current.text
    completed = jobs.get_job(job_id)
    assert completed["status"] == "complete"
    assert completed["result"]["answer"] == 42
    provenance = completed["provenance"]
    assert provenance["attempt"] == 2
    assert provenance["workitem_id"] == "wi-attempt-2", (
        "the signed WorkItem id must reach the receipt, or provenance cannot be "
        "reconciled against APS")


def test_a_callback_without_an_attempt_still_completes(monkeypatch, tmp_path):
    """The attempt binding is enforced when the callback SUPPLIES one. An emitter
    that sends no attempt (everything predating the L3.1 adapter) keeps working
    against the job's current attempt, so this is a tightening of the signed path
    and not a new required field on the wire."""
    import jobs

    class InertExecutor:
        def submit(self, *args, **kwargs):
            return None

    if jobs._conn is not None:
        jobs._conn.close()
    monkeypatch.setattr(jobs, "DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(jobs, "_conn", None)
    monkeypatch.setattr(jobs, "_reaper_started", True)
    monkeypatch.setattr(jobs, "_executors", {"fast": InertExecutor(), "slow": InertExecutor()})
    tool = {"name": "callback-tool", "engine_op": "count_by_layer",
            "capabilities": ["drawing.read"]}
    job_id = jobs.submit_job("demo-tenant", tool, {}, "rooftop_demo", aps_live=True)
    assert jobs.claim_lease(job_id, "worker-1") == 1

    body = json.dumps({"job_id": job_id, "status": "success", "result": {"answer": 7}}).encode()
    timestamp = str(time.time())
    nonce = "nonce-no-attempt"
    response = TestClient(broker.app).post(
        "/da/callback", content=body,
        headers={"X-Leaf-Signature": _signed(body, timestamp, nonce),
                 "X-Leaf-Timestamp": timestamp, "X-Leaf-Nonce": nonce},
    )
    assert response.status_code == 200, response.text
    completed = jobs.get_job(job_id)
    assert completed["status"] == "complete"
    assert completed["provenance"]["attempt"] == 1
    assert "workitem_id" not in completed["provenance"]
