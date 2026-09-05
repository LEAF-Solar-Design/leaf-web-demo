"""W4g browser plans enter only the broker's server-owned live-write route."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import broker  # noqa: E402
import mutation_apply  # noqa: E402 - write_loop adds da/ to sys.path
import write_loop  # noqa: E402


@pytest.fixture
def body():
    return {
        "tenant_id": "tenant-plan", "dwg": "browser-drawing", "dwg_version": 3,
        "plan": {"drawing_id": "browser-drawing", "parent_version": 3,
                 "mutations": {"delete": ["1A"]}, "plan_sha256": "a" * 64,
                 "source_sha256": "b" * 64},
        "checkout_holder": "browser-tab", "checkout_fence": 7,
        "ledger_event_key": "plan-job:broker-run", "job_id": "plan-job",
    }


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    monkeypatch.setenv("LEAF_BROKER_STORE", "legacy")
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
    monkeypatch.delenv("LEAF_BROKER_SECRET", raising=False)
    monkeypatch.delenv("LEAF_DRAWING_MUTATIONS_FENCE_FILE", raising=False)
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "ACTIVE_WORKITEMS_PATH", tmp_path / "workitems.jsonl")
    monkeypatch.setattr(broker, "_active_workitems", {})
    monkeypatch.setattr(broker, "_sidecar_lines", 0)
    monkeypatch.setattr(broker, "_plan_readiness_cache", None)
    monkeypatch.setattr(broker, "_emit_aps_metric", lambda *_a: None)
    monkeypatch.setattr(broker, "tenant_disabled", lambda _t: False)
    monkeypatch.setattr(broker, "_tenant_tier", lambda _t: "demo")
    monkeypatch.setattr(broker, "_cap_preflight", lambda *_a: None)
    monkeypatch.setattr(broker, "_run_quota_preflight", lambda *_a: None)
    monkeypatch.setattr(broker, "_require_supported_live_completion_mode", lambda: None)
    monkeypatch.setattr(mutation_apply, "readiness", lambda: {
        "ready": True, "mismatches": [], "activity": {"alias": "prod", "version": 12},
    })

    def forbidden(*_a, **_k):
        pytest.fail("unexpected execution or APS client load")

    monkeypatch.setattr(broker, "_get_da", forbidden)
    monkeypatch.setattr(write_loop, "run_data_plan_live", forbidden)
    monkeypatch.setattr(write_loop, "run_write_mock", forbidden)
    return TestClient(broker.app)


@pytest.mark.parametrize("invalid", [
    "extra", "plan-extra", "digest", "source-digest", "drawing", "parent", "version",
])
def test_plan_endpoint_rejects_invalid_schema_and_version(client, body, invalid):
    if invalid == "extra":
        body["tool"] = {"name": "caller-code"}
    elif invalid == "plan-extra":
        body["plan"]["test_source"] = "caller-code"
    elif invalid == "digest":
        body["plan"]["plan_sha256"] = "x" * 64
    elif invalid == "source-digest":
        body["plan"]["source_sha256"] = "a" * 63
    elif invalid == "drawing":
        body["plan"]["drawing_id"] = "../drawing"
    elif invalid == "parent":
        body["plan"]["parent_version"] = 0
    else:
        body["dwg_version"] = 4
    response = client.post("/broker/run-plan", json=body)
    assert response.status_code == (400 if invalid == "version" else 422)
    assert response.json()["ok"] is False
    assert response.json()["error"]["error_code"] == "BAD_PARAMS"


@pytest.mark.parametrize("failure", ["readiness", "readiness-exception", "no-da", "no-run-tool"])
def test_plan_endpoint_fails_closed_without_ready_activity_or_da(
    monkeypatch, client, body, failure,
):
    if failure == "readiness":
        monkeypatch.setattr(mutation_apply, "readiness", lambda: {
            "ready": False, "mismatches": ["x"],
        })
    elif failure == "readiness-exception":
        def unavailable():
            raise RuntimeError("activity lookup failed")
        monkeypatch.setattr(mutation_apply, "readiness", unavailable)
    else:
        monkeypatch.setattr(broker, "_get_da", lambda: None if failure == "no-da" else object())
    response = client.post("/broker/run-plan", json=body)
    assert response.status_code == 503
    envelope = response.json()
    assert envelope["error"]["error_code"] == "APS_UNAVAILABLE"
    assert envelope["error"]["retryable"] is True
    message = envelope["error"]["message"]
    if failure.startswith("readiness"):
        assert "mutation Activity not ready:" in message
        assert ("x" if failure == "readiness" else "activity lookup failed") in message
    else:
        assert "there is no degraded writer for a data plan" in message
        assert envelope["result"]["activity_version"] == 12


@pytest.mark.parametrize("store_mode", ["legacy", "postgres"])
def test_plan_endpoint_passes_identity_and_records_activity_once(
    monkeypatch, client, body, store_mode,
):
    da = SimpleNamespace(run_tool=lambda *_a, **_k: None)
    backend = object()
    calls = []
    admissions, starts, completions = [], [], []

    class Store:
        def admit_run(self, *args, **kwargs):
            admissions.append((args, kwargs))
            return {"status": "acquired", "lease_token": "lease-plan"}

        def mark_execution_started(self, *args, **kwargs):
            starts.append((args, kwargs))
            return True

        def complete_run(self, *args):
            completions.append(args)

    if store_mode == "postgres":
        monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
        monkeypatch.setattr(broker, "_postgres_store", lambda: Store())
        monkeypatch.setattr(broker, "_get_usage", lambda: SimpleNamespace(
            DEFAULT_EST_USD=0.1, cap_for=lambda _t: 2.0,
            daily_run_limit_for=lambda _tier: 20,
        ))

    def get_backend(**kwargs):
        assert kwargs == {"aps_live": True, "da": da}
        return backend

    def run_plan(plan, tenant, **kwargs):
        calls.append((plan, tenant, kwargs))
        assert kwargs["ledger_entry"]["activity_version"] == 12
        kwargs["ledger_entry"].update(engine_seconds=1.0, usd_est=0.01)
        kwargs["on_submitted"]("workitem-plan")
        assert broker.active_workitem_for(body["job_id"]) == "workitem-plan"
        return ({"ok": True, "tool": "cad-edit-plan", "version": "1.0.0",
                 "result": {"version": 4}, "overlay": None, "timing_ms": 1,
                 "cost": {"engine_seconds": 1.0, "usd_est": 0.01},
                 "error": None, "degraded_mode": False}, 200)

    monkeypatch.setattr(broker, "_get_da", lambda: da)
    monkeypatch.setattr(write_loop, "default_backend", get_backend)
    monkeypatch.setattr(write_loop, "run_data_plan_live", run_plan)
    # No caller code loads, so a deployed posture does not close this route.
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "production")
    monkeypatch.setenv("LEAF_AUTHORED_EXECUTION", "0")
    response = client.post("/broker/run-plan", json=body)
    assert response.status_code == 200
    assert response.json()["result"] == {"version": 4, "activity_version": 12}
    assert len(calls) == 1
    plan, tenant, kwargs = calls[0]
    assert plan == body["plan"] and tenant == body["tenant_id"]
    assert kwargs["backend"] is backend and kwargs["da"] is da
    assert kwargs["holder"] == body["checkout_holder"]
    assert kwargs["fence"] == body["checkout_fence"]
    assert broker.active_workitem_for(body["job_id"]) is None
    if store_mode == "legacy":
        entries = [json.loads(line) for line in broker.LEDGER_PATH.read_text().splitlines()]
        assert len(entries) == 1
        entry = entries[0]
    else:
        assert len(admissions) == len(starts) == len(completions) == 1
        assert admissions[0][0] == (body["ledger_event_key"], body["tenant_id"])
        assert admissions[0][1]["aps_live"] is True
        assert admissions[0][1]["estimated_usd"] == 0.1
        assert admissions[0][1]["spend_cap"] == 2.0
        assert admissions[0][1]["daily_limit"] == 20
        assert starts[0][1]["aps_live"] is True
        event_key, tenant_id, token, entry, envelope, status = completions[0]
        assert (event_key, tenant_id, token) == (
            body["ledger_event_key"], body["tenant_id"], "lease-plan")
        assert envelope == response.json() and status == 200
    assert entry["tool"] == "cad-edit-plan"
    assert entry["aps_live"] is True
    assert entry["activity_version"] == 12
    assert entry["status"] == "ok"


def test_generic_broker_refuses_reserved_plan_tool(client, body):
    response = client.post("/broker/run", json={
        "tenant_id": body["tenant_id"], "tool": broker.PLAN_TOOL,
        "params": body["plan"], "dwg": body["dwg"], "aps_live": True,
    })
    assert response.status_code == 400
    assert response.json()["error"]["error_code"] == "BAD_PARAMS"
    assert response.json()["error"]["message"] == "reserved tool name"


@pytest.mark.parametrize("ready", [True, False])
def test_plan_readiness_cache_bounds_activity_calls(monkeypatch, client, ready):
    now, calls = [100.0], []
    result = {"ready": ready, "mismatches": [] if ready else ["x"],
              "activity": {"alias": "prod", "version": 12}}

    def readiness():
        calls.append(now[0])
        return result

    monkeypatch.setattr(broker.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(mutation_apply, "readiness", readiness)
    assert broker._plan_activity_ready() == (ready, result)
    now[0] += 59
    assert broker._plan_activity_ready() == (ready, result)
    assert calls == [100.0]
    now[0] += 1
    assert broker._plan_activity_ready() == (ready, result)
    assert calls == [100.0, 160.0]
