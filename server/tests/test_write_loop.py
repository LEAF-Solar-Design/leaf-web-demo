"""
Binary acceptance for the M2 write loop (APS_LIVE=0, no operator, no APS).

Boots the real broker + app subprocesses (same harness style as
tests/test_backbone.py) against an ISOLATED filesystem store (LEAF_STORE_DIR in
tmp) and drives the write loop end to end over HTTP:

  * a registered drawing.write tool run creates v2 with parent v1, and its §3
    envelope carries result.new_version = {drawing_id, version, parent};
  * GET /api/drawings/{id}/intake serves head, then pinned versions;
  * POST .../undo -> head=v1 (redo available); POST .../redo -> head=v2;
  * read tools (count-by-layer) are unaffected.

Each test uses its OWN X-Tenant-Id so its bootstrapped `demo` drawing is
isolated — the tests are order-independent.

Run:  cd server && python -m pytest tests/test_write_loop.py -q
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import jsonschema
import pytest
import requests

from _test_run_confirmation import confirmed_requests_payload

SERVER_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SERVER_DIR.parent
ENGINE_SCHEMA = json.loads((PROJECT_ROOT / "engine" / "envelope_schema.json").read_text(encoding="utf-8"))
CORE_KEYS = ("ok", "tool", "version", "result", "overlay", "timing_ms", "cost", "error")

WRITE_TOOL = "delete-marked-panel"


# --------------------------------------------------------------------------- #
# process harness
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


def wait_ready(url: str, proc: subprocess.Popen, timeout_s: float = 30.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server process exited early (rc={proc.returncode})")
        try:
            if requests.get(url, timeout=2).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.25)
    raise TimeoutError(f"server at {url} not ready in {timeout_s}s")


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
    tmp = tmp_path_factory.mktemp("writeloop")
    store_dir = tmp / "drawings"           # shared by broker (writes) + app (reads)
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
        wait_ready(f"http://127.0.0.1:{broker_port}/broker/health", broker)
        wait_ready(f"http://127.0.0.1:{app_port}/api/health", app)
        yield {"app": f"http://127.0.0.1:{app_port}", "broker": f"http://127.0.0.1:{broker_port}"}
    finally:
        stop(app)
        stop(broker)


def _h(tenant: str) -> dict:
    return {"X-Tenant-Id": tenant}


def run_wait(stack, tool, params=None, tenant="wl"):
    headers = _h(tenant)
    r = requests.post(f"{stack['app']}/api/run?wait=1",
                      json=confirmed_requests_payload(
                          stack["app"], tool, params, "rooftop_demo",
                          headers=headers),
                      headers=headers, timeout=120)
    return r


def get_intake(stack, drawing_id, version, tenant):
    return requests.get(f"{stack['app']}/api/drawings/{drawing_id}/intake",
                        params={"version": version}, headers=_h(tenant), timeout=30)


def core_validates(env: dict) -> None:
    core = {k: env[k] for k in CORE_KEYS if k in env}
    jsonschema.validate(core, ENGINE_SCHEMA)


# --------------------------------------------------------------------------- #
# 1. write run creates v2 with parent v1; envelope carries result.new_version
# --------------------------------------------------------------------------- #
def test_write_creates_v2_with_parent_v1_and_new_version(stack):
    t = "wl-write"
    r = run_wait(stack, WRITE_TOOL, {"drawing_id": "demo"}, tenant=t)
    assert r.status_code == 200, r.text
    env = r.json()
    assert env["ok"] is True and env["error"] is None
    core_validates(env)  # still a valid §3 envelope
    nv = env["result"]["new_version"]
    assert nv == {"drawing_id": "demo", "version": 2, "parent": 1}, nv
    # the write actually declared a delete + a marker add
    assert env["result"]["deleted_handle"]
    assert env["result"]["added_marker_handle"]
    assert env["result"]["mutations"]["removed"] == [env["result"]["deleted_handle"]]


# --------------------------------------------------------------------------- #
# 2. intake endpoint serves head, then pinned versions
# --------------------------------------------------------------------------- #
def test_intake_serves_head_then_pinned_versions(stack):
    t = "wl-intake"
    assert run_wait(stack, WRITE_TOOL, {"drawing_id": "demo"}, tenant=t).status_code == 200

    head = get_intake(stack, "demo", "head", t).json()
    assert head["version"] == 2 and head["head"] == 2 and head["latest"] == 2
    assert head["error"] is None and head["degraded_mode"] is False   # §10 envelope
    assert "polylines" in head["intake"]
    assert "LEAF_WRITE_PROBE" in head["intake"]["layers"]             # the mock add

    v1 = get_intake(stack, "demo", "1", t).json()
    assert v1["version"] == 1 and v1["head"] == 2 and v1["latest"] == 2
    assert "LEAF_WRITE_PROBE" not in v1["intake"]["layers"]           # v1 predates the add
    # v1 has one MORE original panel than head (head deleted one, added a marker)
    assert len(v1["intake"]["polylines"]) == len(head["intake"]["polylines"])

    # v1 is the cached rooftop demo intake -> 2345 polylines
    assert len(v1["intake"]["polylines"]) == 2345


# --------------------------------------------------------------------------- #
# 3. undo -> v1 (redo available); redo -> v2
# --------------------------------------------------------------------------- #
def test_undo_then_redo_round_trip(stack):
    t = "wl-undo"
    assert run_wait(stack, WRITE_TOOL, {"drawing_id": "demo"}, tenant=t).status_code == 200

    u = requests.post(f"{stack['app']}/api/drawings/demo/undo", headers=_h(t), timeout=30).json()
    assert u["version"] == 1 and u["head"] == 1 and u["latest"] == 2   # redo still possible (latest=2)
    assert "LEAF_WRITE_PROBE" not in u["intake"]["layers"]

    # head now serves v1
    assert get_intake(stack, "demo", "head", t).json()["version"] == 1

    red = requests.post(f"{stack['app']}/api/drawings/demo/redo", headers=_h(t), timeout=30).json()
    assert red["version"] == 2 and red["head"] == 2 and red["latest"] == 2
    assert "LEAF_WRITE_PROBE" in red["intake"]["layers"]
    assert get_intake(stack, "demo", "head", t).json()["version"] == 2


def test_undo_at_root_is_a_clean_error(stack):
    t = "wl-root"
    # bootstrap demo (v1) via an intake read, then undo at the root
    assert get_intake(stack, "demo", "head", t).json()["version"] == 1
    r = requests.post(f"{stack['app']}/api/drawings/demo/undo", headers=_h(t), timeout=30)
    assert r.status_code == 400
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"


def test_redo_without_undo_is_a_clean_error(stack):
    t = "wl-noredo"
    assert run_wait(stack, WRITE_TOOL, {"drawing_id": "demo"}, tenant=t).status_code == 200
    # head == latest == 2 -> nothing to redo
    r = requests.post(f"{stack['app']}/api/drawings/demo/redo", headers=_h(t), timeout=30)
    assert r.status_code == 400
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"


# --------------------------------------------------------------------------- #
# 4. two writes chain v1 -> v2 -> v3; undo walks back one at a time
# --------------------------------------------------------------------------- #
def test_two_writes_chain_and_stepwise_undo(stack):
    t = "wl-chain"
    a = run_wait(stack, WRITE_TOOL, {"drawing_id": "demo"}, tenant=t).json()
    assert a["result"]["new_version"] == {"drawing_id": "demo", "version": 2, "parent": 1}
    b = run_wait(stack, WRITE_TOOL, {"drawing_id": "demo"}, tenant=t).json()
    assert b["result"]["new_version"] == {"drawing_id": "demo", "version": 3, "parent": 2}

    u1 = requests.post(f"{stack['app']}/api/drawings/demo/undo", headers=_h(t), timeout=30).json()
    assert u1["head"] == 2 and u1["latest"] == 3
    u2 = requests.post(f"{stack['app']}/api/drawings/demo/undo", headers=_h(t), timeout=30).json()
    assert u2["head"] == 1 and u2["latest"] == 3
    r1 = requests.post(f"{stack['app']}/api/drawings/demo/redo", headers=_h(t), timeout=30).json()
    assert r1["head"] == 2 and r1["latest"] == 3


# --------------------------------------------------------------------------- #
# 5. read tools unaffected: count-by-layer still returns the golden counts and
#    NEVER creates a version; the write tool is registered in /api/tools.
# --------------------------------------------------------------------------- #
def test_read_tools_unaffected(stack):
    t = "wl-read"
    e = run_wait(stack, "count-by-layer", {}, tenant=t).json()
    assert e["ok"] is True
    assert e["result"]["counts"]["Panels"] == 2345
    assert "new_version" not in e["result"]                # read tool never versions
    core_validates(e)

    # a read tool did NOT bootstrap/mutate a 'demo' drawing for this tenant
    r = get_intake(stack, "demo", "head", t)               # bootstraps on demand -> v1 only
    assert r.json()["latest"] == 1

    names = {x["name"] for x in requests.get(f"{stack['app']}/api/tools", timeout=10).json()["tools"]}
    assert WRITE_TOOL in names
    assert {"count-by-layer", "measure-panel-area", "highlight-panels-near-edge"} <= names


# --------------------------------------------------------------------------- #
# 6. unknown drawing -> clean 404 (no crash, structured envelope)
# --------------------------------------------------------------------------- #
def test_unknown_drawing_intake_404(stack):
    # Contract change (gap-fill QW-C, 2026-07-21): a slug-valid first-seen id
    # now auto-bootstraps (200) instead of 404ing — 'no-such-drawing' is a
    # legitimate new drawing. The 404 contract survives for MALFORMED ids,
    # which is what this test now asserts (id from test_drawings_bootstrap's
    # BAD_IDS set).
    t = "wl-unknown"
    r = get_intake(stack, "UPPERCASE", "head", t)
    assert r.status_code == 404
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"
