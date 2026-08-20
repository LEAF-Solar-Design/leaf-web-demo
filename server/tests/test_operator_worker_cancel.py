"""OP-1, the server-authorized exact-worker cancellation boundary."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import deps
import operator_deps
import operator_worker_dispatch as dispatch
from routers import operator_worker as route


class _Operator:
    subject = "auth0|operator-a"
    role = "operator"
    role_revision = 7


def _app(operator=None, tenant="tenant-a"):
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[operator_deps.require_operator] = lambda: operator or _Operator()
    app.dependency_overrides[deps.require_tenant] = lambda: tenant
    return app


def _binding(**overrides):
    value = {
        "worker_id": "worker-a", "run_id": "run-a", "status": "running",
        "active": True, "owner_subject": "auth0|operator-a",
        "tenant_id": "tenant-a", "role_revision": 7,
    }
    value.update(overrides)
    return value


def _payload(**overrides):
    value = {"worker_id": "worker-a", "run_id": "run-a"}
    value.update(overrides)
    return value


def _client(monkeypatch, binding=None, receipt=None, operator=None, tenant="tenant-a"):
    stop_calls = []
    monkeypatch.setattr(dispatch, "_resolve_active_worker_run",
                        lambda *args: binding or _binding())

    def stop(*args):
        stop_calls.append(args)
        return receipt or {"worker_id": "worker-a", "run_id": "run-a", "status": "cancelled"}

    monkeypatch.setattr(dispatch, "_stop_exact_worker_run", stop)
    return TestClient(_app(operator, tenant)), stop_calls


def test_missing_or_non_admin_request_never_reaches_stop(monkeypatch):
    client, stops = _client(monkeypatch)
    assert client.post("/api/operator/worker/cancel", json={"worker_id": "worker-a"}).status_code == 422
    assert stops == []

    class _ReadOnly(_Operator):
        role = "read_only"

    client, stops = _client(monkeypatch, operator=_ReadOnly())
    response = client.post("/api/operator/worker/cancel", json=_payload())
    assert response.status_code == 404
    assert stops == []


@pytest.mark.parametrize("binding", [
    _binding(active=False),
    _binding(status="completed", active=False),
], ids=["stale", "completed"])
def test_stale_or_completed_run_is_refused_before_stop(monkeypatch, binding):
    client, stops = _client(monkeypatch, binding=binding)
    response = client.post("/api/operator/worker/cancel", json=_payload())
    assert response.status_code == 409
    assert response.json()["detail"] == "worker_run_not_active"
    assert stops == []


@pytest.mark.parametrize("binding, tenant", [
    (_binding(owner_subject="auth0|operator-b"), "tenant-a"),
    (_binding(tenant_id="tenant-b"), "tenant-a"),
    (_binding(worker_id="worker-b"), "tenant-a"),
    (_binding(run_id="run-b"), "tenant-a"),
], ids=["different-owner", "cross-tenant", "forged-worker", "forged-run"])
def test_mismatched_identity_is_refused_before_stop(monkeypatch, binding, tenant):
    client, stops = _client(monkeypatch, binding=binding, tenant=tenant)
    response = client.post("/api/operator/worker/cancel", json=_payload())
    assert response.status_code == 404
    assert response.json()["detail"] == "worker_run_not_found"
    assert stops == []


def test_authorized_request_stops_one_exact_active_worker_run(monkeypatch):
    client, stops = _client(monkeypatch)
    response = client.post("/api/operator/worker/cancel", json=_payload())
    assert response.status_code == 200
    assert response.json() == {"worker_id": "worker-a", "run_id": "run-a", "status": "cancelled"}
    assert len(stops) == 1
    _ctx, tenant_id, worker_id, run_id = stops[0]
    assert (tenant_id, worker_id, run_id) == ("tenant-a", "worker-a", "run-a")


def test_forged_terminal_receipt_is_not_reported_as_a_cancel(monkeypatch):
    client, stops = _client(
        monkeypatch,
        receipt={"worker_id": "worker-a", "run_id": "run-a", "status": "completed"},
    )
    response = client.post("/api/operator/worker/cancel", json=_payload())
    assert response.status_code == 502
    assert response.json()["detail"] == "worker_stop_receipt_invalid"
    assert len(stops) == 1
