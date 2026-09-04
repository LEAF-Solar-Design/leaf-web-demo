"""
GET /api/builds (slice 11a): tenant-scoped, bounded, fail closed, three
sources behind one shape. In-process TestClient over a minimal app that mounts
only the builds router (the test_ui_wave pattern), a throwaway SQLite jobs
store, a fake fleet gateway opener, and a tmp runs directory for the fold lane.

Run:  cd server && python -m pytest tests/test_builds_route.py -q
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

os.environ.setdefault("JOBS_DB", str(Path(tempfile.mkdtemp(prefix="builds-jobs-")) / "jobs.db"))

import build_queue  # noqa: E402
import build_receipts  # noqa: E402
import fleet_gateway_client  # noqa: E402
import jobs  # noqa: E402

TENANT = "demo-tenant"
OTHER = "other-tenant"


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from envelopes import install_error_handlers
    from routers import builds as builds_router

    monkeypatch.setattr(jobs, "DB_PATH", tmp_path / "jobs.db")
    jobs.reset_connection()
    monkeypatch.setenv("LEAF_BUILD_RECEIPTS_DIR", str(tmp_path / "receipts"))
    monkeypatch.delenv(fleet_gateway_client.URL_ENV, raising=False)
    monkeypatch.delenv(fleet_gateway_client.TOKEN_ENV, raising=False)
    monkeypatch.delenv("LEAF_MARATHON_RUNS_DIR", raising=False)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(builds_router.router)
    yield TestClient(app)
    jobs.reset_connection()


def _insert(job_id, tenant, status, *, tool="count-by-layer", created_at=None, elapsed_ms=None,
            result=None, error=None, provenance=None):
    now = created_at if created_at is not None else time.time()
    with jobs._lock:
        conn = jobs._db()
        conn.execute(
            "INSERT INTO jobs (job_id, tenant_id, tool, params_json, dwg, status, progress, created_at, "
            "started_at, updated_at, finished_at, elapsed_ms, result_json, error_json, provenance_json, attempt) "
            "VALUES (?, ?, ?, '{}', 'cat-panels', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (job_id, tenant, tool, status, "done" if status == "complete" else status, now, now, now,
             now + 1 if status in ("complete", "failed") else None, elapsed_ms,
             json.dumps(result) if result else None, json.dumps(error) if error else None,
             json.dumps(provenance) if provenance else None),
        )
        conn.commit()


def _get(client, tenant=TENANT, **params):
    return client.get("/api/builds", params=params, headers={"X-Tenant-Id": tenant})


def test_broker_lane_is_tenant_scoped_and_carries_the_terminal_receipt(client):
    _insert("j-mine-done", TENANT, "complete", created_at=1725400000, elapsed_ms=4200,
            result={"ok": True, "cost": {"usd_est": 0.0042}},
            provenance={"attempt": 1, "execution_path": "local", "fallback": False})
    _insert("j-mine-run", TENANT, "running", created_at=1725400100)
    _insert("j-theirs", OTHER, "complete", created_at=1725400200)
    # the receipt the terminal callback would have written, beside the record
    build_receipts.write_terminal_receipt(jobs.get_job("j-mine-done"))

    response = _get(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["error"] is None and body["degraded_mode"] is False
    ids = [b["id"] for b in body["builds"]]
    assert ids == ["j-mine-run", "j-mine-done"], "newest first, other tenant absent"
    assert all(b["lane"] == "broker" for b in body["builds"])
    done = body["builds"][1]
    assert done["state"] == "done"
    assert done["terminal"] == {"verified": True, "promoted": False}
    assert done["status"] == {"word": "complete", "tint": "ok", "detail": "$0.0042"}
    assert done["receipts"] == [{"kind": "terminal", "ref": "receipts/j-mine-done/receipt.json", "at": 1725400001000}]
    running = body["builds"][0]
    assert running["state"] == "running" and running["terminal"] == {"verified": False, "promoted": False}
    assert running["receipts"] == [] and running["actions"] == ["cancel"]
    assert body["sources"] == {"broker": "jobs-store", "fleet": "unconfigured", "fold": "unconfigured"}
    assert body["warnings"] == ["fleet: gateway not configured"]
    assert body["dropped"] == {"broker": 0, "fleet": 0, "fold": 0}
    for record in body["builds"]:
        build_queue.validate_record(record)

    theirs = _get(client, tenant=OTHER).json()
    assert [b["id"] for b in theirs["builds"]] == ["j-theirs"]


def test_limit_is_clamped_and_applies_to_the_merged_list(client):
    for i in range(5):
        _insert(f"j-{i}", TENANT, "complete", created_at=1725400000 + i)
    assert len(_get(client, limit=2).json()["builds"]) == 2
    assert _get(client, limit=2).json()["limit"] == 2
    assert _get(client, limit=0).json()["limit"] == 1
    assert _get(client, limit=10_000).json()["limit"] == 200
    assert _get(client, limit="abc").status_code == 422


class _FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fleet_lane_reads_with_the_platform_credential_and_degrades_to_empty(client, monkeypatch):
    monkeypatch.setenv(fleet_gateway_client.URL_ENV, "https://fleet.example.test/gw/")
    monkeypatch.setenv(fleet_gateway_client.TOKEN_ENV, "platform-secret-token")
    seen = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["timeout"] = timeout
        return _FakeResponse(json.dumps({"tasks": [
            {"task_id": "t-1", "title": "Ship it", "owner": "session:claude-7", "state": "active",
             "created_at": 1725400000, "last_evidence_at": 1725400600},
            {"task_id": "t-2", "title": "Done it", "state": "complete", "created_at": 1725399000,
             "receipts": [{"kind": "gate-proof", "ref": "gate-proof-abc"}]},
            {"task_id": "t-3", "title": "Odd", "state": "paused"},
            "not a row",
        ]}).encode("utf-8"))

    monkeypatch.setattr(fleet_gateway_client.urllib.request, "urlopen", opener)
    body = _get(client, limit=10).json()
    assert seen["url"] == "https://fleet.example.test/gw/tasks?tenant=demo-tenant&limit=10"
    assert seen["auth"] == "Bearer platform-secret-token"
    assert seen["timeout"] == fleet_gateway_client.FLEET_TIMEOUT_S
    fleet = [b for b in body["builds"] if b["lane"] == "fleet"]
    assert [b["id"] for b in fleet] == ["t-1", "t-2"]
    assert fleet[0]["requested_by"] == "session:claude-7"
    assert fleet[1]["terminal"] == {"verified": True, "promoted": False}
    assert body["sources"]["fleet"] == "gateway"
    assert body["dropped"]["fleet"] == 1
    assert body["warnings"] == []
    # the credential never reaches the body
    assert "platform-secret-token" not in json.dumps(body)

    def refusing(request, timeout):
        raise fleet_gateway_client.urllib.error.URLError("connection refused")

    monkeypatch.setattr(fleet_gateway_client.urllib.request, "urlopen", refusing)
    degraded = _get(client).json()
    assert [b for b in degraded["builds"] if b["lane"] == "fleet"] == []
    assert degraded["sources"]["fleet"] == "unavailable"
    assert degraded["warnings"] == ["fleet: gateway unreachable (URLError)"]
    assert "platform-secret-token" not in json.dumps(degraded)


def test_fleet_client_bounds_the_body_and_refuses_bad_shapes(monkeypatch):
    monkeypatch.setenv(fleet_gateway_client.URL_ENV, "https://fleet.example.test")
    monkeypatch.delenv(fleet_gateway_client.TOKEN_ENV, raising=False)

    def big(request, timeout):
        return _FakeResponse(b"{" + b" " * (fleet_gateway_client.MAX_BODY_BYTES + 5) + b"}")

    with pytest.raises(fleet_gateway_client.FleetGatewayUnavailable, match="over bound"):
        fleet_gateway_client.list_tasks("t", 5, opener=big)

    def not_json(request, timeout):
        return _FakeResponse(b"<html>")

    with pytest.raises(fleet_gateway_client.FleetGatewayUnavailable, match="not JSON"):
        fleet_gateway_client.list_tasks("t", 5, opener=not_json)

    def wrong_shape(request, timeout):
        return _FakeResponse(b'{"rows": []}')

    with pytest.raises(fleet_gateway_client.FleetGatewayUnavailable, match="no tasks list"):
        fleet_gateway_client.list_tasks("t", 5, opener=wrong_shape)

    def unauthorized(request, timeout):
        raise fleet_gateway_client.urllib.error.HTTPError(request.full_url, 401, "nope", {}, None)

    with pytest.raises(fleet_gateway_client.FleetGatewayUnavailable, match="answered 401"):
        fleet_gateway_client.list_tasks("t", 5, opener=unauthorized)

    monkeypatch.setenv(fleet_gateway_client.URL_ENV, "ftp://fleet")
    with pytest.raises(fleet_gateway_client.FleetGatewayUnavailable, match="not http"):
        fleet_gateway_client.list_tasks("t", 5)

    monkeypatch.delenv(fleet_gateway_client.URL_ENV, raising=False)
    with pytest.raises(fleet_gateway_client.FleetGatewayUnavailable, match="not configured"):
        fleet_gateway_client.list_tasks("t", 5)


def _write_run(root, tenant, run_id, state, manifest=None, promotion=None):
    run_dir = root / tenant / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    if manifest is not None:
        (run_dir / "run-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if promotion is not None:
        (run_dir / "promotion.json").write_text(json.dumps(promotion), encoding="utf-8")
    return run_dir


def test_fold_lane_reads_only_the_tenants_runs_and_skips_malformed_state(client, monkeypatch, tmp_path):
    root = tmp_path / "runs"
    monkeypatch.setenv("LEAF_MARATHON_RUNS_DIR", str(root))
    _write_run(root, TENANT, "run-verified", {
        "run_id": "run-verified", "rounds": 3, "spent_usd": 2.5, "mission_complete": True,
        "milestones": {"a": {"status": "done", "verified_at": "2026-09-04T00:00:00Z"}},
    }, manifest={"title": "Sheets page", "requested_by": "operator", "started_at": 1725400000},
       promotion={"promotion_stage": {"status": "promoted", "ref": "janitor/stage-3"}})
    _write_run(root, TENANT, "run-running", {
        "run_id": "run-running", "rounds": 1, "spent_usd": 0.2, "mission_complete": False,
        "round_in_progress": {"milestone": "a", "round": 2, "attempt": 1},
        "milestones": {"a": {"status": "running"}},
    })
    broken = root / TENANT / "run-broken"
    broken.mkdir(parents=True)
    (broken / "state.json").write_text("{not json", encoding="utf-8")
    _write_run(root, OTHER, "run-theirs", {"run_id": "run-theirs", "rounds": 1})

    body = _get(client).json()
    fold = {b["id"]: b for b in body["builds"] if b["lane"] == "fold"}
    assert set(fold) == {"run-verified", "run-running"}
    verified = fold["run-verified"]
    assert verified["state"] == "done"
    assert verified["title"] == "Sheets page" and verified["requested_by"] == "operator"
    assert verified["terminal"] == {"verified": True, "promoted": True}
    assert [r["kind"] for r in verified["receipts"]] == ["verification", "promotion"]
    assert verified["actions"] == []
    assert verified["started"] == 1725400000000
    running = fold["run-running"]
    assert running["state"] == "running" and running["status"]["word"] == "round 2"
    assert running["actions"] == ["cancel"]
    assert body["sources"]["fold"] == "runs-dir"
    # Counted, not named per-run (marathon_runs.list_runs): one aggregate
    # warning, not one string per malformed directory.
    assert "fold: 1 run(s) skipped (state.json missing, oversized or malformed)" in body["warnings"]
    assert "run-theirs" not in json.dumps(body)
    for record in body["builds"]:
        build_queue.validate_record(record)


def test_fold_lane_refuses_a_tenant_id_that_is_not_a_plain_token(monkeypatch, tmp_path):
    import marathon_runs  # noqa: PLC0415
    monkeypatch.setenv("LEAF_MARATHON_RUNS_DIR", str(tmp_path))
    runs, warnings = marathon_runs.list_runs("../escape", 5)
    assert runs == [] and warnings == ["fold: tenant id is not a plain token"]
    runs, warnings = marathon_runs.list_runs("nobody", 5)
    assert runs == [] and warnings == []
    monkeypatch.delenv("LEAF_MARATHON_RUNS_DIR", raising=False)
    assert marathon_runs.list_runs("demo-tenant", 5) == ([], [])


def test_a_receipt_is_written_when_a_terminal_callback_is_applied(client, tmp_path):
    """The hook in jobs.complete_callback: an applied terminal outcome leaves
    receipt.json beside the record, and /api/builds reads it back."""
    _insert("j-live", TENANT, "running", created_at=1725400000)
    with jobs._lock:
        conn = jobs._db()
        conn.execute("UPDATE jobs SET lease_owner = 'w1', lease_expires_at = ?, attempt = 1 WHERE job_id = 'j-live'",
                     (time.time() + 60,))
        conn.commit()
    outcome = jobs.complete_callback(
        "j-live", "complete", worker_id="w1",
        result_env={"ok": True, "tool": "count-by-layer", "result": {}, "execution_provenance": {
            "attempt": 1, "execution_path": "local", "fallback": False}},
        provenance={"attempt": 1, "execution_path": "local", "fallback": False})
    assert outcome == "applied"
    receipt = build_receipts.read_terminal_receipt("j-live")
    assert receipt is not None and receipt["status"] == "complete" and receipt["job_id"] == "j-live"
    body = _get(client).json()
    live = next(b for b in body["builds"] if b["id"] == "j-live")
    assert live["receipts"] == [{"kind": "terminal", "ref": "receipts/j-live/receipt.json",
                                 "at": build_queue.to_epoch_ms(receipt["finished_at"])}]
    assert live["terminal"] == {"verified": True, "promoted": False}
