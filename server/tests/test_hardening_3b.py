"""
Binary acceptance for lane 3B — the single-writer checkout lock's MUTATING routes
(APS_LIVE=0, no operator, no APS).

Boots the real broker + app subprocesses (same harness style as
tests/test_write_loop.py) against an ISOLATED filesystem store (LEAF_STORE_DIR in
tmp) and drives the new endpoints end to end over HTTP:

  * POST   /api/drawings/{id}/checkout {holder?, ttl_s?}  -> take the lock
  * DELETE /api/drawings/{id}/checkout?holder=<h>          -> release the lock

Covers the acceptance matrix:
  1. acquire on a free drawing -> 200 + checkout record (holder/acquired/expires);
  2. a SECOND holder acquiring the held lock -> 409 not-acquired (locked_by X);
     (the lock is per-tenant+drawing; `holder` is the session id, so a second
     HOLDER on the same tenant/drawing is the true single-writer conflict — two
     distinct tenants have separate manifests and correctly never collide);
  3. the same holder re-acquiring REFRESHES (200, not a conflict);
  4. release by the holder -> 200 + cleared state; GET /versions shows it cleared;
  5. release by a NON-holder -> 403 and the lock is left intact;
  6. a read tool run (count-by-layer) is UNAFFECTED by a held lock;
  7. an EXPIRED lock is re-acquirable by a different holder;
  8. the default holder is the tenant id (bodyless POST / no-holder DELETE);
  9. releasing when nothing is held is an idempotent 200.

Each test uses its OWN X-Tenant-Id so its bootstrapped `demo` drawing (and its
manifest lock) is isolated — the tests are order-independent.

Run:  cd server && python -m pytest tests/test_hardening_3b.py tests/test_write_loop.py -q
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

SERVER_DIR = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# process harness (mirrors tests/test_write_loop.py)
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
    tmp = tmp_path_factory.mktemp("hardening3b")
    store_dir = tmp / "drawings"           # shared by broker (writes) + app (reads/lock)
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


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _h(tenant: str) -> dict:
    return {"X-Tenant-Id": tenant}


def acquire(stack, tenant, drawing="demo", holder=None, ttl_s=None):
    body = {}
    if holder is not None:
        body["holder"] = holder
    if ttl_s is not None:
        body["ttl_s"] = ttl_s
    return requests.post(f"{stack['app']}/api/drawings/{drawing}/checkout",
                         json=body if body else None, headers=_h(tenant), timeout=30)


def release(stack, tenant, drawing="demo", holder=None):
    params = {"holder": holder} if holder is not None else None
    return requests.delete(f"{stack['app']}/api/drawings/{drawing}/checkout",
                           params=params, headers=_h(tenant), timeout=30)


def versions(stack, tenant, drawing="demo"):
    return requests.get(f"{stack['app']}/api/drawings/{drawing}/versions",
                        headers=_h(tenant), timeout=30)


def run_wait(stack, tool, params=None, tenant="wl"):
    headers = _h(tenant)
    tools = requests.get(f"{stack['app']}/api/tools", headers=headers, timeout=30).json()["tools"]
    catalog_digest = next(item["catalog_digest"] for item in tools if item["name"] == tool)
    return requests.post(f"{stack['app']}/api/run?wait=1",
                         json={"tool": tool, "params": params or {}, "dwg": "rooftop_demo",
                               "catalog_digest": catalog_digest},
                         headers=headers, timeout=120)


def _envelope_ok(body: dict) -> None:
    # §10: every response carries error + degraded_mode
    assert "error" in body and "degraded_mode" in body, body


# --------------------------------------------------------------------------- #
# 1. acquire on a free drawing -> 200 + checkout record
# --------------------------------------------------------------------------- #
def test_acquire_on_free_drawing(stack):
    t = "co-free"
    r = acquire(stack, t, holder="sess-a", ttl_s=3600)
    assert r.status_code == 200, r.text
    body = r.json()
    _envelope_ok(body)
    assert body["error"] is None
    assert body["acquired"] is True
    assert body["holder"] == "sess-a"
    co = body["checkout"]
    assert co["holder"] == "sess-a"
    assert co["acquired"] and co["expires"]            # timestamps present

    # the read-only versions surface now reflects the held lock
    v = versions(stack, t).json()
    assert v["checkout"]["holder"] == "sess-a"


# --------------------------------------------------------------------------- #
# 2. a second HOLDER acquiring the held lock -> 409 not-acquired (locked_by X)
# --------------------------------------------------------------------------- #
def test_second_holder_conflict_409(stack):
    t = "co-conflict"
    assert acquire(stack, t, holder="alice").status_code == 200

    r = acquire(stack, t, holder="bob")
    assert r.status_code == 409, r.text
    body = r.json()
    _envelope_ok(body)
    assert body["acquired"] is False
    assert body["locked_by"] == "alice"
    assert body["checkout"]["holder"] == "alice"       # bob sees who holds it
    assert body["error"]["error_code"] == "BAD_PARAMS"
    assert body["error"]["retryable"] is True

    # the lock was NOT stolen — still alice's
    assert versions(stack, t).json()["checkout"]["holder"] == "alice"


# --------------------------------------------------------------------------- #
# 3. the same holder re-acquiring REFRESHES (not a conflict)
# --------------------------------------------------------------------------- #
def test_same_holder_reacquire_refreshes(stack):
    t = "co-refresh"
    first = acquire(stack, t, holder="alice", ttl_s=10).json()
    time.sleep(1.1)
    second = acquire(stack, t, holder="alice", ttl_s=3600)
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["acquired"] is True
    # the lease was refreshed -> a strictly later expiry than the first grab
    assert body["checkout"]["expires"] > first["checkout"]["expires"]


# --------------------------------------------------------------------------- #
# 4. release by the holder -> 200 + cleared state
# --------------------------------------------------------------------------- #
def test_release_by_holder_clears(stack):
    t = "co-release"
    assert acquire(stack, t, holder="alice").status_code == 200

    r = release(stack, t, holder="alice")
    assert r.status_code == 200, r.text
    body = r.json()
    _envelope_ok(body)
    assert body["released"] is True
    assert body["checkout"] is None

    # versions confirms the lock is gone
    assert versions(stack, t).json()["checkout"] is None


# --------------------------------------------------------------------------- #
# 5. release by a NON-holder -> 403 and the lock is left intact
# --------------------------------------------------------------------------- #
def test_release_by_non_holder_forbidden(stack):
    t = "co-nonholder"
    assert acquire(stack, t, holder="alice").status_code == 200

    r = release(stack, t, holder="mallory")
    assert r.status_code == 403, r.text
    body = r.json()
    _envelope_ok(body)
    assert body["error"]["error_code"] == "BAD_PARAMS"

    # the active lock is untouched — still alice's
    assert versions(stack, t).json()["checkout"]["holder"] == "alice"


# --------------------------------------------------------------------------- #
# 6. a read tool run is UNAFFECTED by a held lock
# --------------------------------------------------------------------------- #
def test_read_tool_unaffected_by_lock(stack):
    t = "co-read"
    assert acquire(stack, t, holder="alice").status_code == 200      # lock held

    e = run_wait(stack, "count-by-layer", {}, tenant=t).json()
    assert e["ok"] is True
    assert e["result"]["counts"]["Panels"] == 2345                   # golden read, lock ignored
    assert "new_version" not in e["result"]                          # read never versions

    # and the lock is still held afterwards (the read did not touch it)
    assert versions(stack, t).json()["checkout"]["holder"] == "alice"


# --------------------------------------------------------------------------- #
# 7. an EXPIRED lock is re-acquirable by a different holder
# --------------------------------------------------------------------------- #
def test_expired_lock_is_reacquirable(stack):
    t = "co-expire"
    assert acquire(stack, t, holder="alice", ttl_s=1).status_code == 200
    time.sleep(1.3)                                                  # let alice's lease expire

    r = acquire(stack, t, holder="bob")
    assert r.status_code == 200, r.text                             # expired lock is free
    body = r.json()
    assert body["acquired"] is True
    assert body["checkout"]["holder"] == "bob"
    assert versions(stack, t).json()["checkout"]["holder"] == "bob"


# --------------------------------------------------------------------------- #
# 8. the default holder is the tenant id (bodyless POST / no-holder DELETE)
# --------------------------------------------------------------------------- #
def test_default_holder_is_tenant(stack):
    t = "co-default"
    r = acquire(stack, t)                                           # no body -> holder defaults to tenant
    assert r.status_code == 200, r.text
    assert r.json()["checkout"]["holder"] == t

    # a no-holder release uses the same default (tenant) -> succeeds
    rel = release(stack, t)
    assert rel.status_code == 200, rel.text
    assert rel.json()["released"] is True
    assert rel.json()["checkout"] is None


# --------------------------------------------------------------------------- #
# 9. releasing when nothing is held is an idempotent 200
# --------------------------------------------------------------------------- #
def test_release_when_free_is_idempotent(stack):
    t = "co-idem"
    # bootstrap the demo drawing (a versions read) with NO lock held
    assert versions(stack, t).json()["checkout"] is None

    r = release(stack, t)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["released"] is False                               # nothing was held
    assert body["checkout"] is None
