"""Project catalog availability for non-Solar drawing tools, derived from their executor."""
from copy import deepcopy
import json
from pathlib import Path

import entitlements
from routers import jobs as jobs_router
import test_sip_r5_routes as routes
from test_sip_r5_routes import lane  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


def _registry_record(path, name):
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data["tools"] if isinstance(data, dict) else data
    return deepcopy(next(row for row in rows if row.get("name") == name))


COUNT = _registry_record(ROOT / "engine" / "registry.json", "count-by-layer")
DELETE = _registry_record(ROOT / "server" / "write_tools.json", "delete-marked-panel")


def _project_states(lane, *, version=True):
    lane.tools["count-by-layer"] = deepcopy(COUNT)
    lane.tools["delete-marked-panel"] = deepcopy(DELETE)
    query = {"project_id": str(lane.s.project), "drawing_id": str(lane.s.drawing)}
    if version:
        query["input_version_id"] = str(lane.s.parent.version_id)
    response = lane.client.get("/api/capabilities", params=query, headers=routes.headers(lane))
    return routes.states(response)


def test_real_records_match_the_run_route_predicate():
    assert jobs_router._is_project_read_tool(COUNT) is True
    assert jobs_router._is_project_read_tool(DELETE) is False


def _grant_run_read(monkeypatch):
    granted = entitlements.entitlements_for
    monkeypatch.setattr(entitlements, "entitlements_for",
                        lambda *args, **kwargs: {**granted(*args, **kwargs), "run_read": True})


def test_read_follows_the_run_route_entitlement(lane):
    # The lane's policy grants no run_read, which the run route would also refuse.
    state = _project_states(lane)["count-by-layer"]
    assert state["project_read"] is True and state["entitled"] is False
    assert state["runnable"] is False
    assert state["refusal_reasons"] == ["entitlement_required"]


def test_drawing_backed_project_read_is_runnable(lane, monkeypatch):
    _grant_run_read(monkeypatch)
    state = _project_states(lane)["count-by-layer"]
    assert state["project_read"] is True, state
    assert state["entitled"] is True, state
    assert all(state[key] is True for key in ("entitled", "engine_ready", "input_ready", "implemented"))
    assert state["runnable"] is True and state["refusal_reasons"] == []
    assert state["admission_checked"] is False and state["admission"] is None


def test_drawing_write_has_no_project_executor(lane):
    state = _project_states(lane)["delete-marked-panel"]
    assert state["project_read"] is False and state["runnable"] is False
    assert state["engine_ready"] is False
    assert state["refusal_reasons"] == ["project_adapter_unavailable"]


def test_read_without_exact_version_is_refused(lane, monkeypatch):
    _grant_run_read(monkeypatch)
    state = _project_states(lane, version=False)["count-by-layer"]
    assert state["project_read"] is False and state["runnable"] is False
    assert state["refusal_reasons"] == ["project_request_required"]


def test_solar_rows_keep_their_project_policy(lane):
    before = routes.states(lane.client.get("/api/capabilities", params={
        "project_id": str(lane.s.project), "drawing_id": str(lane.s.drawing),
        "input_version_id": str(lane.s.parent.version_id)}, headers=routes.headers(lane)))
    after = _project_states(lane)
    for name, state in before.items():
        assert "project_read" not in state
        assert after[name] == state


def test_catalog_read_writes_nothing(lane):
    before = deepcopy((lane.s.blobs, lane.s.versions, lane.s.fingerprints, lane.s.writes))
    _project_states(lane)
    assert (lane.s.blobs, lane.s.versions, lane.s.fingerprints, lane.s.writes) == before
    assert not lane.records
