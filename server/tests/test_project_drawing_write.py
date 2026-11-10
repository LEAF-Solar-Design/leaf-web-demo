"""C01 project drawing.write producer through /api/run on a private PostgreSQL project.

Real topology: the genuine registered delete-marked-panel builtin, an imported
drawing-backed project, the canonical project editor writer and checkout store.
"""
from copy import deepcopy
import json
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import deps
import entitlements
import project_drawing_write as producer
import solar_project_context as project_context
from envelopes import install_error_handlers
from routers import jobs as route
from routers import project_drawings
from test_lab_corpus_executor import private_pg  # noqa: F401  job-private PG16 supply
from test_project_drawing_editor import checkout, lane, versions  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


def _record(path, name):
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data["tools"] if isinstance(data, dict) else data
    return deepcopy(next(row for row in rows if row.get("name") == name))


DELETE = _record(ROOT / "server" / "write_tools.json", "delete-marked-panel")
COUNT = _record(ROOT / "engine" / "registry.json", "count-by-layer")


@pytest.fixture
def run(lane, monkeypatch):
    # hosted_starter grants run_read and run_write; the synthetic org's tier is
    # granted the same two capabilities, and nothing else changes.
    granted = entitlements.entitlements_for
    monkeypatch.setattr(entitlements, "entitlements_for", lambda *args, **kwargs: {
        **granted(*args, **kwargs), "run_read": True, "run_write": True})
    monkeypatch.setattr(deps, "backedge_run_identity", lambda tenant, *_: tenant)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(route.router)
    app.include_router(project_drawings.router)
    app.dependency_overrides[deps.require_tenant] = lambda: lane.tenant
    app.dependency_overrides[deps.require_active_tenant] = lambda: lane.tenant
    with TestClient(app) as http:
        def post(tool=DELETE, *, version=None, params=None, key=None, cap=None, org=None,
                 digest=True):
            row = deps.find_tool(tool["name"], str(lane.tenant))
            body = {"tool": tool["name"], "params": params or {},
                    "dwg": str(version or lane.parent.version_id)}
            if digest:
                body["catalog_digest"] = deps.catalog_tool_digest(row)
            headers = {"X-Org-Id": str(org or lane.org), "X-Project-Id": str(lane.project),
                       "Idempotency-Key": key or str(uuid4())}
            if cap is not None:
                headers["X-Checkout-Capability"] = cap
            return http.post("/api/run", json=body, headers=headers)
        yield post


def _intake(lane, version_id):
    return project_context.resolve_context(lane.tenant, lane.project, UUID(str(version_id)),
                                           drawing_id=lane.drawing).intake


def _panels(intake):
    return [row["handle"] for row in intake["polylines"] if row.get("layer") == "Panels"]


def test_only_the_server_owned_builtin_is_a_producer():
    assert producer.producer_entry(DELETE) == "builtins/delete_marked_panel.py"
    assert producer.producer_entry(COUNT) is None
    assert producer.producer_entry({**DELETE, "entry": "builtins/count_by_layer.py"}) is None
    assert producer.producer_entry({**DELETE, "name": "authored-write"}) is None
    assert producer.producer_entry({**DELETE, "capabilities": ["drawing.write", "drawing.read"]}) is None


def test_reviewed_write_commits_one_immutable_version_and_counts_it(lane, run):
    assert deps.find_tool("delete-marked-panel", str(lane.tenant))["entry"] == DELETE["entry"]
    cap, key = checkout(lane), str(uuid4())
    before = _intake(lane, lane.parent.version_id)
    assert len(_panels(before)) == 74
    response = run(key=key, cap=cap)
    assert response.status_code == 200, response.text
    body = response.json()
    child = body["version"]
    assert body["ok"] is True and child["seq"] == 2
    assert child["project_id"] == str(lane.project) and child["drawing_id"] == str(lane.drawing)
    assert body["result"]["deleted_handle"] == "A100"
    assert body["producer"]["schema"] == producer.SCHEMA
    assert body["producer"]["tool"] == "delete-marked-panel"
    assert body["producer"]["input_version_id"] == str(lane.parent.version_id)
    assert body["project_context"]["input_version_id"] == str(lane.parent.version_id)
    assert len(versions(lane)) == 2
    after = _intake(lane, child["version_id"])
    assert "A100" not in _panels(after) and len(_panels(after)) == 73
    assert [row["layer"] for row in after["polylines"]].count("LEAF_WRITE_PROBE") == 1
    assert after["texts"] == before["texts"]
    # The prior version is immutable and still reads its original drawing.
    assert _intake(lane, lane.parent.version_id) == before
    # An exact replay of the confirmation returns the same version, no second write.
    replay = run(key=key, cap=cap)
    assert replay.status_code == 200 and replay.json()["version"] == child
    # Count through the existing project read endpoint; reads create no version.
    counted = run(COUNT, version=child["version_id"])
    assert counted.status_code == 200, counted.text
    assert counted.json()["result"]["counts"]["Panels"] == 73
    assert counted.json()["result"]["counts"]["LEAF_WRITE_PROBE"] == 1
    original = run(COUNT, version=lane.parent.version_id)
    assert original.json()["result"]["counts"]["Panels"] == 74
    assert len(versions(lane)) == 2


def test_changed_params_under_one_confirmation_are_refused(lane, run):
    cap, key = checkout(lane), str(uuid4())
    assert run(key=key, cap=cap).status_code == 200
    changed = run(key=key, cap=cap, params={"handle": "A101"})
    assert changed.status_code == 409, changed.text
    assert len(versions(lane)) == 2


def test_stale_parent_is_refused_by_compare_and_set(lane, run):
    cap = checkout(lane)
    assert run(cap=cap).status_code == 200
    stale = run(cap=cap, params={"handle": "A101"})
    assert stale.status_code == 409, stale.text
    assert len(versions(lane)) == 2


def test_absent_and_foreign_checkout_do_not_publish(lane, run):
    assert run().status_code == 409
    checkout(lane)
    assert run(cap="lco1." + "0" * 64).status_code == 403
    assert len(versions(lane)) == 1


def test_cross_tenant_and_foreign_version_do_not_publish(lane, run):
    cap = checkout(lane)
    foreign = run(cap=cap, org=uuid4())
    assert foreign.status_code in (403, 409), foreign.text
    assert run(cap=cap, version=uuid4()).status_code in (404, 409)
    assert len(versions(lane)) == 1


def test_missing_confirmation_and_missing_target_do_not_publish(lane, run):
    cap = checkout(lane)
    unconfirmed = run(cap=cap, digest=False)
    assert unconfirmed.status_code == 409, unconfirmed.text
    missing = run(cap=cap, params={"handle": "NOPE"})
    assert missing.status_code == 422, missing.text
    assert missing.json()["reason_code"] == "PROJECT_WRITE_TARGET_NOT_FOUND"
    assert len(versions(lane)) == 1
