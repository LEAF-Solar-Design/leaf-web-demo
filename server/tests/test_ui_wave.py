"""
Binary acceptance for UI wave 1 (backend): spend meter, tier echo, version
history, and the bodyless tab-close beacon. All offline, APS_LIVE=0 — no live
APS, no LLM, no Auth0.

Two styles, mirroring the existing suites:
  * TestClient (in-process, minimal apps like test_auth._client) for the
    ledger-backed /api/usage read, the tenant_echo tier unit, and the bodyless
    /api/jobs/{id}/close check;
  * a real broker+app subprocess stack (same harness as tests/test_write_loop)
    for the version-history chain after two real writes.

Run:  cd server && python -m pytest tests/test_ui_wave.py -q
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest
import requests

SERVER_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SERVER_DIR.parent

# Route the jobs SQLite DB to a throwaway dir BEFORE `jobs` is ever imported
# (jobs.py reads JOBS_DB at import time) so the /close test never litters server/.
os.environ.setdefault("JOBS_DB", str(Path(tempfile.mkdtemp(prefix="uiwave-jobs-")) / "jobs.db"))

if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from _test_readiness import wait_ready  # noqa: E402
from _test_run_confirmation import confirmed_requests_payload  # noqa: E402

# §10 envelope schema (the app's, matching test_backbone).
ENVELOPE_SCHEMA = json.loads((SERVER_DIR / "envelope_schema.json").read_text(encoding="utf-8"))

WRITE_TOOL = "delete-marked-panel"


def _validate_envelope(body: dict) -> None:
    jsonschema.validate(body, ENVELOPE_SCHEMA)
    assert body["error"] is None
    assert body["degraded_mode"] is False


def _usage_mod():
    import deps  # noqa: PLC0415
    return deps._load_module_from(deps.DA_DIR / "usage.py", "leaf_usage")


# --------------------------------------------------------------------------- #
# in-process minimal apps
# --------------------------------------------------------------------------- #
def _usage_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from envelopes import install_error_handlers
    from routers import usage as usage_router

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(usage_router.router)
    return TestClient(app, raise_server_exceptions=False)


def _jobs_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from envelopes import install_error_handlers
    from routers import jobs as jobs_router

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(jobs_router.router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _clean_usage_env(monkeypatch):
    """Every test starts with caps OFF and no ambient ledger override."""
    for k in ("LEAF_TENANT_CAP_USD", "LEAF_USAGE_CAPS", "LEAF_USAGE_CAPS_FILE",
              "LEAF_USAGE_LEDGER", "BROKER_LEDGER", "LEAF_AUTH_LIVE"):
        monkeypatch.delenv(k, raising=False)
    yield


# --------------------------------------------------------------------------- #
# 1. /api/usage — zeros on a missing/empty ledger, never an error
# --------------------------------------------------------------------------- #
def test_usage_zeros_on_empty_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_USAGE_LEDGER", str(tmp_path / "nope.jsonl"))  # does not exist
    r = _usage_client().get("/api/usage", headers={"X-Tenant-Id": "demo-tenant"})
    assert r.status_code == 200, r.text
    body = r.json()
    _validate_envelope(body)
    assert body["tenant_id"] == "demo-tenant"
    assert body["today"] == {"runs": 0, "usd_est": 0.0}
    assert body["total"] == {"runs": 0, "usd_est": 0.0}
    assert body["cap"] == {"usd_cap": None, "remaining": None, "enabled": False}
    assert isinstance(body["updated_at"], str) and body["updated_at"]

    # an EMPTY file (exists, no lines) is also zeros, not an error
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("LEAF_USAGE_LEDGER", str(empty))
    b2 = _usage_client().get("/api/usage", headers={"X-Tenant-Id": "demo-tenant"}).json()
    _validate_envelope(b2)
    assert b2["total"] == {"runs": 0, "usd_est": 0.0}


# --------------------------------------------------------------------------- #
# 2. /api/usage — aggregates a synthetic ledger: today/total split + isolation
# --------------------------------------------------------------------------- #
def _write_ledger(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_usage_aggregates_today_total_and_isolation(tmp_path, monkeypatch):
    now = time.time()
    old = now - 3 * 86400  # definitely a prior UTC day
    ledger = tmp_path / "broker_ledger.jsonl"
    _write_ledger(ledger, [
        # tenant A: two today (0.01 + 0.02), one old (0.03)  -> total 3/$0.06, today 2/$0.03
        {"ts": now, "tenant_id": "A", "tool": "count-by-layer", "usd_est": 0.01, "status": "ok"},
        {"ts": now, "tenant_id": "A", "tool": "count-by-layer", "usd_est": 0.02, "status": "ok"},
        {"ts": old, "tenant_id": "A", "tool": "count-by-layer", "usd_est": 0.03, "status": "ok"},
        # a denied pre-flight line for A today must NOT count (no run, no spend)
        {"ts": now, "tenant_id": "A", "tool": "count-by-layer", "usd_est": None,
         "status": "quota_exceeded"},
        # a mock run with usd_est=null still counts as a run, adds $0
        {"ts": now, "tenant_id": "A", "tool": "count-by-layer", "usd_est": None, "status": "ok"},
        # tenant B: one today (0.05) — must be invisible to A
        {"ts": now, "tenant_id": "B", "tool": "count-by-layer", "usd_est": 0.05, "status": "ok"},
    ])
    monkeypatch.setenv("LEAF_USAGE_LEDGER", str(ledger))

    a = _usage_client().get("/api/usage", headers={"X-Tenant-Id": "A"}).json()
    _validate_envelope(a)
    assert a["tenant_id"] == "A"
    # 3 spend-bearing today runs? no: today runs = 2 usd-lines + 1 null-usd ok line = 3
    assert a["today"] == {"runs": 3, "usd_est": 0.03}, a["today"]
    # total = 3 today-counting + 1 old = 4 runs; spend 0.06
    assert a["total"] == {"runs": 4, "usd_est": 0.06}, a["total"]
    assert a["cap"] == {"usd_cap": None, "remaining": None, "enabled": False}

    # per-tenant isolation: B's $0.05 never leaks into A
    b = _usage_client().get("/api/usage", headers={"X-Tenant-Id": "B"}).json()
    assert b["total"] == {"runs": 1, "usd_est": 0.05}, b["total"]

    # total.usd_est must equal the authoritative spent_from_broker_ledger
    um = _usage_mod()
    assert a["total"]["usd_est"] == um.spent_from_broker_ledger("A", ledger)


def test_usage_deterministic_split_via_aggregate_helper(tmp_path):
    """Pin `now_ts` so the today/total boundary is race-free (the HTTP test uses
    real now)."""
    day = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc).timestamp()
    prev = datetime(2026, 7, 17, 23, 0, tzinfo=timezone.utc).timestamp()
    ledger = tmp_path / "l.jsonl"
    _write_ledger(ledger, [
        {"ts": day, "tenant_id": "A", "usd_est": 0.10, "status": "ok"},
        {"ts": prev, "tenant_id": "A", "usd_est": 0.20, "status": "ok"},
    ])
    um = _usage_mod()
    agg = um.aggregate_usage("A", ledger, now_ts=day)
    assert agg == {"today": {"runs": 1, "usd_est": 0.1},
                   "total": {"runs": 2, "usd_est": 0.3}}


def test_usage_cap_enabled_reports_remaining(tmp_path, monkeypatch):
    now = time.time()
    ledger = tmp_path / "l.jsonl"
    _write_ledger(ledger, [
        {"ts": now, "tenant_id": "capped", "usd_est": 0.40, "status": "ok"},
    ])
    monkeypatch.setenv("LEAF_USAGE_LEDGER", str(ledger))
    monkeypatch.setenv("LEAF_TENANT_CAP_USD", "1.00")
    body = _usage_client().get("/api/usage", headers={"X-Tenant-Id": "capped"}).json()
    _validate_envelope(body)
    assert body["cap"]["enabled"] is True
    assert body["cap"]["usd_cap"] == 1.0
    assert body["cap"]["remaining"] == 0.6  # 1.00 - 0.40


# --------------------------------------------------------------------------- #
# 3. tenant_echo tier unit (CONTRACT 2): TenantContext -> tier present; str -> unchanged
# --------------------------------------------------------------------------- #
def test_tenant_echo_adds_tier_for_tenant_context():
    import deps

    ctx = deps.TenantContext("org_acme", org_id="org_acme", tier="hosted_pro", workspace="/w")
    out = deps.tenant_echo({"intake": 1}, ctx)
    assert out["intake"] == 1
    assert out["tenant_id"] == "org_acme"
    assert out["org_id"] == "org_acme"
    assert out["tier"] == "hosted_pro"


def test_tenant_echo_offauth_str_is_byte_identical():
    import deps

    body = {"job_id": "j1", "status": "submitted"}
    out = deps.tenant_echo(body, "demo-tenant")  # plain str (off-auth)
    assert out == body
    assert "tier" not in out and "tenant_id" not in out and "org_id" not in out


# --------------------------------------------------------------------------- #
# 4. POST /api/jobs/{id}/close accepts a bodyless POST (sendBeacon parity)
# --------------------------------------------------------------------------- #
def test_close_accepts_bodyless_post():
    client = _jobs_client()
    # sendBeacon cannot set json content-type / custom headers and may send an
    # empty text/plain body — this must NOT 422 on request-body validation.
    r_none = client.post("/api/jobs/no-such-job/close")
    assert r_none.status_code != 422, r_none.text
    assert r_none.status_code == 404  # unknown job -> reached the handler, structured 404
    assert r_none.json()["error"]["error_code"] == "BAD_PARAMS"

    r_beacon = client.post("/api/jobs/no-such-job/close", content=b"",
                           headers={"content-type": "text/plain;charset=UTF-8"})
    assert r_beacon.status_code != 422, r_beacon.text
    assert r_beacon.status_code == 404


# --------------------------------------------------------------------------- #
# subprocess stack (broker + app) for the version-history chain — mirrors
# tests/test_write_loop.py exactly (isolated LEAF_STORE_DIR in tmp).
# --------------------------------------------------------------------------- #
def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_uvicorn(module_app: str, port: int, env_overrides: dict, log_path: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({k: str(v) for k, v in env_overrides.items()})
    log = open(log_path, "ab")
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", module_app, "--port", str(port), "--host", "127.0.0.1"],
        cwd=str(SERVER_DIR), env=env, stdout=log, stderr=log,
    )


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("uiwave")
    store_dir = tmp / "drawings"
    broker_port, app_port = free_port(), free_port()
    broker = start_uvicorn("broker:app", broker_port,
                           {"BROKER_LEDGER": tmp / "ledger.jsonl",
                            "BROKER_TENANTS": tmp / "tenants.json",
                            "APS_LIVE": "0",
                            "LEAF_STORE_DIR": store_dir},
                           tmp / "broker.log")
    app = start_uvicorn("app:app", app_port,
                        {"APS_LIVE": "0", "APS_CRED": "/nonexistent",
                         "BROKER_URL": f"http://127.0.0.1:{broker_port}",
                         "JOBS_DB": tmp / "jobs.db",
                         "LEAF_STORE_DIR": store_dir},
                        tmp / "app.log")
    try:
        wait_ready(f"http://127.0.0.1:{broker_port}/broker/health", broker,
                   log_path=tmp / "broker.log")
        wait_ready(f"http://127.0.0.1:{app_port}/api/health", app,
                   log_path=tmp / "app.log")
        yield {"app": f"http://127.0.0.1:{app_port}", "broker": f"http://127.0.0.1:{broker_port}"}
    finally:
        stop(app)
        stop(broker)


def _h(tenant: str) -> dict:
    return {"X-Tenant-Id": tenant}


def _run_wait(stack, tool, params, tenant):
    headers = _h(tenant)
    return requests.post(f"{stack['app']}/api/run?wait=1",
                         json=confirmed_requests_payload(
                             stack["app"], tool, params, "rooftop_demo",
                             headers=headers),
                         headers=headers, timeout=120)


# --------------------------------------------------------------------------- #
# 5. GET /api/drawings/{id}/versions — full chain after two writes
# --------------------------------------------------------------------------- #
def test_versions_chain_after_two_writes(stack):
    t = "uiw-versions"
    a = _run_wait(stack, WRITE_TOOL, {"drawing_id": "demo"}, t)
    assert a.status_code == 200, a.text
    assert a.json()["result"]["new_version"] == {"drawing_id": "demo", "version": 2, "parent": 1}
    b = _run_wait(stack, WRITE_TOOL, {"drawing_id": "demo"}, t)
    assert b.json()["result"]["new_version"] == {"drawing_id": "demo", "version": 3, "parent": 2}

    r = requests.get(f"{stack['app']}/api/drawings/demo/versions", headers=_h(t), timeout=30)
    assert r.status_code == 200, r.text
    body = r.json()
    _validate_envelope(body)
    assert body["drawing_id"] == "demo"
    assert body["head"] == 3 and body["latest"] == 3

    versions = body["versions"]
    assert [e["v"] for e in versions] == [1, 2, 3]
    assert [e["parent"] for e in versions] == [None, 1, 2]   # parent links form the chain
    # every row carries the manifest fields (optional ones may be null)
    for e in versions:
        assert set(e.keys()) == {"v", "parent", "created", "bytes", "sha256",
                                 "tool", "workitem_id", "note"}
        assert isinstance(e["created"], str) and e["created"]
        assert isinstance(e["bytes"], int)
        assert isinstance(e["sha256"], str) and len(e["sha256"]) == 64
    # the two write versions record the authoring tool; v1 (initial ingest) has none
    assert versions[0]["tool"] is None
    assert versions[1]["tool"] == WRITE_TOOL and versions[2]["tool"] == WRITE_TOOL


def test_versions_unknown_drawing_404(stack):
    # Contract change (gap-fill QW-C, 2026-07-21): slug-valid first-seen ids
    # auto-bootstrap now; only MALFORMED ids 404. Assert both halves.
    t = "uiw-unknown"
    r = requests.get(f"{stack['app']}/api/drawings/UPPERCASE/versions",
                     headers=_h(t), timeout=30)
    assert r.status_code == 404, r.text
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"
