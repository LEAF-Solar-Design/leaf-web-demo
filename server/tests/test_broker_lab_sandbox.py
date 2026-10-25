"""Lab broker containment runs before existing execution branches."""
import pytest
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

import broker
import tool_loader


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    monkeypatch.delenv("LEAF_BROKER_SECRET", raising=False)
    calls = []
    def execute(req):
        calls.append(req)
        return JSONResponse({"ok": True, "result": 7})
    monkeypatch.setattr(broker, "_broker_run", execute)
    with TestClient(broker.app) as http:
        yield http, calls


def body():
    return {"tenant_id": "tenant-a", "tool": {"name": "lab-read", "capabilities": ["drawing.read"],
            "lab_execution": {"run_id": "run", "block_id": "node", "attempt": 1}}, "aps_live": False}


@pytest.mark.parametrize("tier", ["off", "invalid", "subprocess", None])
def test_sandbox_closed(client, monkeypatch, tier):
    monkeypatch.setattr(tool_loader, "_sandbox_tier", lambda: tier)
    http, calls = client
    response = http.post("/broker/run", json=body())
    assert response.status_code == 503
    assert response.json()["error"]["reason_code"] == "lab_sandbox_unavailable"
    assert not calls


def test_unset_provider_closed(client, monkeypatch):
    for name in ("LEAF_TOOL_SANDBOX_PROVIDER", "LEAF_SANDBOX", "LEAF_TOOL_SANDBOX"):
        monkeypatch.delenv(name, raising=False)
    http, calls = client
    response = http.post("/broker/run", json=body())
    assert response.status_code == 503
    assert response.json()["error"]["reason_code"] == "lab_sandbox_unavailable"
    assert not calls


def test_microvm_dispatch_and_request_time_config(client, monkeypatch):
    monkeypatch.setenv("LEAF_TOOL_SANDBOX_PROVIDER", "e2b")
    http, calls = client
    response = http.post("/broker/run", json=body())
    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0].tool == body()["tool"]
    monkeypatch.setenv("LEAF_TOOL_SANDBOX_PROVIDER", "invalid")
    assert http.post("/broker/run", json=body()).status_code == 503
    assert len(calls) == 1


def test_aps_refused(client):
    http, calls = client
    response = http.post("/broker/run", json={**body(), "aps_live": True})
    assert response.status_code == 403
    assert response.json()["error"]["reason_code"] == "lab_aps_refused"
    assert not calls


@pytest.mark.parametrize("capability", ["drawing.write", "drawing.write.child", "solve", "solve.child",
                                      "cloud-proposal", "cloud-proposal.child"])
def test_write_refused(client, capability):
    http, calls = client
    request = body()
    request["tool"]["capabilities"] = [capability]
    response = http.post("/broker/run", json=request)
    assert response.status_code == 403
    assert response.json()["error"]["reason_code"] == "lab_write_refused"
    assert not calls


@pytest.mark.parametrize("marker", [None, "run", {}, {"run_id": 1, "block_id": "node", "attempt": 1},
                                  {"run_id": "run", "block_id": None, "attempt": 1},
                                  {"run_id": "run", "block_id": "node", "attempt": True}])
def test_marker_invalid(client, marker):
    http, calls = client
    request = body()
    request["tool"]["lab_execution"] = marker
    response = http.post("/broker/run", json=request)
    assert response.status_code == 422
    assert response.json()["error"]["reason_code"] == "lab_execution_invalid"
    assert not calls


def test_plan_refused(client, monkeypatch):
    http, calls = client
    monkeypatch.setattr(broker, "_broker_run_plan", lambda req: pytest.fail("lab plan dispatched"))
    response = http.post("/broker/run-plan", json={
        "tenant_id": "tenant-a", "tool": body()["tool"], "dwg": "drawing", "dwg_version": 1,
        "plan": {"drawing_id": "drawing", "parent_version": 1, "mutations": {},
                 "plan_sha256": "a" * 64, "source_sha256": "b" * 64}})
    assert response.status_code == 422
    assert not calls


def test_unmarked_unchanged(client, monkeypatch):
    monkeypatch.setattr(tool_loader, "_sandbox_tier", lambda: pytest.fail("non-lab sandbox check"))
    http, calls = client
    request = body()
    del request["tool"]["lab_execution"]
    response = http.post("/broker/run", json=request)
    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0].tool == request["tool"]
