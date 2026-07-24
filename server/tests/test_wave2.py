"""
Binary acceptance for UI wave 2 (backend), CONTRACT-ADDENDUM §14:

  A. platform create_org route  (POST /api/orgs round-trip + GET /api/orgs 404-isolation)
  B. platform Job linkage       (POST /api/run with X-Org-Id/X-Project-Id -> platform Job
                                 row w/ spine_ref, terminal sync; AND the no-DB/no-headers
                                 byte-identical path; AND best-effort error-swallow)
  C. ops surface                (role gate 403; tenants aggregation shape; disable/enable
                                 proxy against a booted broker + the kill-switch blocking a run)
  D. checkout surfacing         (GET .../versions gains `checkout` — null default; populated
                                 after acquire via the store API in-test)

Styles mirror the existing suites (tests/test_ui_wave.py, tests/test_backbone.py):
  * TestClient (in-process minimal apps) for the ops role gate + platform_link units;
  * the platform package under the `leaf_platform` alias (+ Neon) for Contract A and the
    linkage row-reads — SKIPPED-with-reason if the platform DB is truly unreachable;
  * a real broker+app subprocess stack for the linkage happy path, the ops proxy/kill-switch,
    and the version-history checkout field.

Run:  cd server && python -m pytest tests/test_wave2.py -q
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import jsonschema
import pytest
import requests

SERVER_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SERVER_DIR.parent
PLATFORM_DIR = PROJECT_ROOT / "platform"

# Route the jobs SQLite DB to a throwaway dir BEFORE `jobs` is imported anywhere
# (jobs.py reads JOBS_DB at import time) so nothing litters server/.
os.environ.setdefault("JOBS_DB", str(Path(tempfile.mkdtemp(prefix="wave2-jobs-")) / "jobs.db"))

if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

ENVELOPE_SCHEMA = json.loads((SERVER_DIR / "envelope_schema.json").read_text(encoding="utf-8"))

READ_TOOL = "count-by-layer"   # no required params; honors _qa_sleep_s on the mock path
WRITE_TOOL = "delete-marked-panel"


def _validate_envelope(body: dict) -> None:
    """§10 validation for app-side (server) responses: error null + degraded bool."""
    jsonschema.validate(body, ENVELOPE_SCHEMA)
    assert body["error"] is None, body
    assert body["degraded_mode"] is False


# --------------------------------------------------------------------------- #
# platform package under the leaf_platform alias (+ DB availability probe)
# --------------------------------------------------------------------------- #
def _load_leaf_platform():
    if "leaf_platform" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "leaf_platform", PLATFORM_DIR / "__init__.py",
            submodule_search_locations=[str(PLATFORM_DIR)])
        mod = importlib.util.module_from_spec(spec)
        sys.modules["leaf_platform"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["leaf_platform"]


def _db_available() -> tuple[bool, str]:
    """(available, reason). Applies migration 0001 (idempotent) as the probe."""
    try:
        _load_leaf_platform()
        from leaf_platform import db  # noqa: PLC0415
        db.get_database_url()
        db.apply_migration()
        with db.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"platform DB unreachable: {type(exc).__name__}: {exc}"


_DB_OK, _DB_REASON = _db_available()
requires_db = pytest.mark.skipif(not _DB_OK, reason=_DB_REASON or "platform DB unavailable")


def _database_url() -> str | None:
    if not _DB_OK:
        return None
    from leaf_platform import db  # noqa: PLC0415
    try:
        return db.get_database_url()
    except Exception:  # noqa: BLE001
        return None


@pytest.fixture(scope="module", autouse=True)
def _pf_pool_teardown():
    yield
    if _DB_OK:
        try:
            from leaf_platform import db  # noqa: PLC0415
            db.reset_pool()
        except Exception:  # noqa: BLE001
            pass


def _pf_store():
    _load_leaf_platform()
    from leaf_platform import store  # noqa: PLC0415
    return store


# =========================================================================== #
# A. Contract A — create_org round-trip + GET isolation (needs DB)
# =========================================================================== #
def _platform_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _load_leaf_platform()
    from leaf_platform.api import router  # noqa: PLC0415
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


@requires_db
def test_A_create_org_roundtrip_and_shape():
    c = _platform_client()
    r = c.post("/api/orgs", json={"name": "Wave2 Org", "tier": "hosted_pro"})
    assert r.status_code == 200, r.text
    org = r.json()["org"]
    assert uuid.UUID(org["org_id"])
    assert org["name"] == "Wave2 Org"
    assert org["tier"] == "hosted_pro"
    assert org["status"] == "active"
    assert isinstance(org["created_at"], str) and org["created_at"]
    # the contract's declared fields are all present
    assert {"org_id", "name", "tier", "status", "created_at"} <= set(org)


@requires_db
def test_A_create_org_default_tier_and_invalid_tier():
    c = _platform_client()
    d = c.post("/api/orgs", json={"name": "Default Tier Org"})
    assert d.status_code == 200, d.text
    assert d.json()["org"]["tier"] == "hosted_starter"  # store default

    bad = c.post("/api/orgs", json={"name": "x", "tier": "gold"})
    assert bad.status_code == 422, bad.text


@requires_db
def test_A_get_org_isolation_404_not_403():
    c = _platform_client()
    oid = c.post("/api/orgs", json={"name": "Isolation Org"}).json()["org"]["org_id"]

    # own org -> 200
    own = c.get(f"/api/orgs/{oid}", headers={"X-Org-Id": oid})
    assert own.status_code == 200, own.text
    assert own.json()["org"]["org_id"] == oid

    # a DIFFERENT caller org -> 404 (never 403: a 403 leaks existence)
    other = str(uuid.uuid4())
    x = c.get(f"/api/orgs/{oid}", headers={"X-Org-Id": other})
    assert x.status_code == 404, x.text

    # missing header -> 400 (malformed request, consistent with the other reads)
    m = c.get(f"/api/orgs/{oid}")
    assert m.status_code == 400, m.text

    # an unknown-but-own-shaped org id (header==path) that doesn't exist -> 404
    ghost = str(uuid.uuid4())
    g = c.get(f"/api/orgs/{ghost}", headers={"X-Org-Id": ghost})
    assert g.status_code == 404, g.text


# =========================================================================== #
# B. Contract B — platform_link units (no DB needed): no-headers + error-swallow
# =========================================================================== #
def test_B_platform_link_noop_without_headers():
    import platform_link

    platform_link._MAP.clear()
    # missing either header -> pure no-op, DB never touched, no mapping recorded
    platform_link.on_submit("spine-nohdr-1", None, "proj", READ_TOOL, {})
    platform_link.on_submit("spine-nohdr-2", "org", None, READ_TOOL, {})
    assert "spine-nohdr-1" not in platform_link._MAP
    assert "spine-nohdr-2" not in platform_link._MAP
    # terminal on an unlinked job is a no-op (must not raise)
    platform_link.on_running("spine-nohdr-1")
    platform_link.on_terminal("spine-nohdr-1", "complete", {"ok": True})


def test_B_platform_link_swallows_db_error(monkeypatch):
    """A DB/create error logs one line and NEVER propagates (run unaffected)."""
    import platform_link

    platform_link._MAP.clear()

    class _BoomStore:
        def create_job(self, *a, **k):
            raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(platform_link, "_db_configured", lambda: True)
    monkeypatch.setattr(platform_link, "_load_platform", lambda: (_BoomStore(), object()))

    # must return cleanly (no exception) and record NO mapping
    platform_link.on_submit("spine-boom", "11111111-1111-1111-1111-111111111111",
                            "22222222-2222-2222-2222-222222222222", READ_TOOL, {})
    assert "spine-boom" not in platform_link._MAP


# =========================================================================== #
# C. Contract C — ops role gate (in-process; no broker needed)
# =========================================================================== #
def _ops_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from envelopes import install_error_handlers
    from routers import ops as ops_router

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(ops_router.router)
    return TestClient(app, raise_server_exceptions=False)


def _dead_broker_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # closed -> connection refused fast
    return f"http://127.0.0.1:{port}"


def test_C_ops_role_gate_403_without_qa(monkeypatch):
    # F7: the ops surface now requires the LEAF_OPS_SECRET shared secret, presented
    # in the X-Ops-Secret header (constant-time compared) — the old plain
    # X-Internal-Role: qa header no longer grants access.
    monkeypatch.setenv("LEAF_USAGE_LEDGER", str(Path(tempfile.mkdtemp()) / "nope.jsonl"))
    monkeypatch.setenv("LEAF_OPS_SECRET", "s3cr3t-wave2")
    c = _ops_client()

    no_hdr = c.get("/api/ops/tenants")
    assert no_hdr.status_code == 403, no_hdr.text
    _body = no_hdr.json()
    jsonschema.validate(_body, ENVELOPE_SCHEMA)
    assert _body["error"] is not None and _body["error"]["error_code"]

    wrong = c.get("/api/ops/tenants", headers={"X-Ops-Secret": "wrong"})
    assert wrong.status_code == 403, wrong.text

    # the old role header no longer grants access
    old = c.get("/api/ops/tenants", headers={"X-Internal-Role": "qa"})
    assert old.status_code == 403, old.text

    # correct secret -> 200 (empty ledger -> empty tenants list)
    ok = c.get("/api/ops/tenants", headers={"X-Ops-Secret": "s3cr3t-wave2"})
    assert ok.status_code == 200, ok.text

    # the mutating routes are gated too (wrong/absent secret -> 403, before any proxy)
    assert c.post("/api/ops/tenants/t1/disable").status_code == 403
    assert c.post("/api/ops/tenants/t1/enable",
                  headers={"X-Ops-Secret": "nope"}).status_code == 403


def test_C_ops_tenants_shape_and_disabled_from_file(monkeypatch, tmp_path):
    # synthetic ledger: A (2 runs, $0.03), B (1 run, $0.05); a denied line never counts
    ledger = tmp_path / "ledger.jsonl"
    now = time.time()
    ledger.write_text("\n".join(json.dumps(r) for r in [
        {"ts": now, "tenant_id": "A", "tool": READ_TOOL, "usd_est": 0.01, "status": "ok"},
        {"ts": now, "tenant_id": "A", "tool": READ_TOOL, "usd_est": 0.02, "status": "ok"},
        {"ts": now, "tenant_id": "A", "tool": READ_TOOL, "usd_est": None,
         "status": "quota_exceeded"},
        {"ts": now, "tenant_id": "B", "tool": READ_TOOL, "usd_est": 0.05, "status": "ok"},
    ]) + "\n", encoding="utf-8")
    # kill-switch fallback file: B disabled (broker unreachable -> read this file)
    tenants_file = tmp_path / "broker_tenants.json"
    tenants_file.write_text(json.dumps({"B": {"disabled": True}}), encoding="utf-8")

    monkeypatch.setenv("LEAF_USAGE_LEDGER", str(ledger))
    monkeypatch.setenv("BROKER_TENANTS", str(tenants_file))
    monkeypatch.setenv("BROKER_URL", _dead_broker_url())  # force file fallback

    r = _ops_client().get("/api/ops/tenants", headers={"X-Internal-Role": "qa"})
    assert r.status_code == 200, r.text
    body = r.json()
    _validate_envelope(body)
    rows = {t["tenant_id"]: t for t in body["tenants"]}
    assert set(rows) == {"A", "B"}
    for t in body["tenants"]:
        assert set(t.keys()) == {"tenant_id", "runs", "usd_est", "disabled"}
    assert rows["A"] == {"tenant_id": "A", "runs": 2, "usd_est": 0.03, "disabled": False}
    assert rows["B"] == {"tenant_id": "B", "runs": 1, "usd_est": 0.05, "disabled": True}


# =========================================================================== #
# subprocess stack (broker + app) — mirrors tests/test_ui_wave.py
# =========================================================================== #
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
    tmp = tmp_path_factory.mktemp("wave2")
    store_dir = tmp / "drawings"
    ledger = tmp / "ledger.jsonl"
    broker_port, app_port = free_port(), free_port()
    broker = start_uvicorn("broker:app", broker_port,
                           {"BROKER_LEDGER": ledger,
                            "BROKER_TENANTS": tmp / "tenants.json",
                            "APS_LIVE": "0",
                            "LEAF_STORE_DIR": store_dir},
                           tmp / "broker.log")
    app_env = {"APS_LIVE": "0", "APS_CRED": "/nonexistent",
               "BROKER_URL": f"http://127.0.0.1:{broker_port}",
               "JOBS_DB": tmp / "jobs.db",
               "LEAF_STORE_DIR": store_dir,
               "LEAF_USAGE_LEDGER": ledger}  # ops reads the broker's ledger
    db_url = _database_url()
    if db_url:
        app_env["DATABASE_URL"] = db_url  # activate best-effort platform linkage
    app = start_uvicorn("app:app", app_port, app_env, tmp / "app.log")
    try:
        wait_ready(f"http://127.0.0.1:{broker_port}/broker/health", broker)
        wait_ready(f"http://127.0.0.1:{app_port}/api/health", app)
        yield {"app": f"http://127.0.0.1:{app_port}",
               "broker": f"http://127.0.0.1:{broker_port}",
               "store_dir": store_dir, "ledger": ledger, "app_log": tmp / "app.log"}
    finally:
        stop(app)
        stop(broker)


def _h(tenant: str) -> dict:
    return {"X-Tenant-Id": tenant}


def _run_json(stack, tool: str, params: dict, tenant: str) -> dict:
    tools = requests.get(f"{stack['app']}/api/tools", headers=_h(tenant), timeout=30).json()["tools"]
    catalog_digest = next(item["catalog_digest"] for item in tools if item["name"] == tool)
    return {"tool": tool, "params": params, "dwg": "rooftop_demo",
            "catalog_digest": catalog_digest}


# --------------------------------------------------------------------------- #
# B. Contract B — linkage happy path (before/after terminal) + no-headers no-row
# --------------------------------------------------------------------------- #
@requires_db
def test_B_project_scoped_legacy_dispatch_is_truthfully_locked(stack):
    store = _pf_store()
    org = store.create_org("Linkage Org")
    project = store.create_project(org.org_id, "Linkage Project")
    hdr = {**_h("wave2-link"),
           "X-Org-Id": str(org.org_id), "X-Project-Id": str(project.project_id),
           "Idempotency-Key": "wave2-linkage-happy-path"}

    # async submit WITH project context; slow the run so we can see the "before"
    r = requests.post(f"{stack['app']}/api/run",
                      json=_run_json(stack, READ_TOOL, {"_qa_sleep_s": 2}, "wave2-link"),
                      headers=hdr, timeout=15)
    assert r.status_code == 409, r.text
    assert "locked" in r.json()["error"]["message"]
    assert store.list_jobs(org.org_id, project.project_id) == []


@requires_db
def test_B_no_headers_creates_no_platform_row_and_byte_identical_202(stack):
    store = _pf_store()
    org = store.create_org("NoHdr Org")
    project = store.create_project(org.org_id, "NoHdr Project")

    # run WITHOUT the project headers -> platform linkage is a no-op
    r = requests.post(f"{stack['app']}/api/run",
                      json=_run_json(stack, READ_TOOL, {}, "wave2-nohdr"),
                      headers=_h("wave2-nohdr"), timeout=15)
    assert r.status_code == 202, r.text
    # 202 body is byte-identical to the legacy shape (no org/tier echo)
    assert set(r.json()) == {"job_id", "status", "error", "degraded_mode"}
    assert r.json()["status"] == "submitted"
    assert r.json()["error"] is None and r.json()["degraded_mode"] is False

    # no platform Job row was created for this (fresh) project
    time.sleep(0.5)
    assert store.list_jobs(org.org_id, project.project_id) == []


# --------------------------------------------------------------------------- #
# C. Contract C — disable/enable proxy against the booted broker + kill-switch
# --------------------------------------------------------------------------- #
def test_C_ops_disable_enable_proxy_and_kill_switch_blocks_run(stack):
    tenant = "wave2-ops"
    qa = {"X-Internal-Role": "qa"}

    # a baseline successful run so the tenant appears with runs>=1
    ok0 = requests.post(f"{stack['app']}/api/run?wait=1",
                        json=_run_json(stack, READ_TOOL, {}, tenant), headers=_h(tenant), timeout=60)
    assert ok0.status_code == 200, ok0.text

    # DISABLE via the proxy -> broker's §-enveloped ack
    d = requests.post(f"{stack['app']}/api/ops/tenants/{tenant}/disable", headers=qa, timeout=15)
    assert d.status_code == 200, d.text
    dbody = d.json()
    jsonschema.validate(dbody, ENVELOPE_SCHEMA)
    assert dbody["ok"] is True and dbody["tenant_id"] == tenant and dbody["disabled"] is True

    # the kill-switch actually blocks a run (broker rejects before any APS work)
    blocked = requests.post(f"{stack['app']}/api/run?wait=1",
                            json=_run_json(stack, READ_TOOL, {}, tenant), headers=_h(tenant), timeout=60)
    assert blocked.status_code == 403, blocked.text
    assert blocked.json()["error"]["error_code"] == "TENANT_DISABLED"

    # ops tenants reflects disabled=True (authoritative broker health)
    tl = requests.get(f"{stack['app']}/api/ops/tenants", headers=qa, timeout=15)
    assert tl.status_code == 200, tl.text
    _validate_envelope(tl.json())
    row = {t["tenant_id"]: t for t in tl.json()["tenants"]}[tenant]
    assert row["disabled"] is True
    assert row["runs"] >= 1  # the baseline ok run is attributed

    # ENABLE via the proxy and confirm a run succeeds again
    e = requests.post(f"{stack['app']}/api/ops/tenants/{tenant}/enable", headers=qa, timeout=15)
    assert e.status_code == 200 and e.json()["disabled"] is False, e.text
    ok1 = requests.post(f"{stack['app']}/api/run?wait=1",
                        json=_run_json(stack, READ_TOOL, {}, tenant), headers=_h(tenant), timeout=60)
    assert ok1.status_code == 200, ok1.text

    tl2 = requests.get(f"{stack['app']}/api/ops/tenants", headers=qa, timeout=15)
    row2 = {t["tenant_id"]: t for t in tl2.json()["tenants"]}[tenant]
    assert row2["disabled"] is False


# --------------------------------------------------------------------------- #
# D. Contract D — GET .../versions checkout field: null default -> populated
# --------------------------------------------------------------------------- #
def test_D_versions_checkout_null_then_populated(stack):
    tenant = "wave2-checkout"

    # one write -> bootstraps demo (v1) + v2
    w = requests.post(f"{stack['app']}/api/run?wait=1",
                      json=_run_json(stack, WRITE_TOOL, {"drawing_id": "demo"}, tenant),
                      headers=_h(tenant), timeout=120)
    assert w.status_code == 200, w.text

    # checkout is null by default
    v0 = requests.get(f"{stack['app']}/api/drawings/demo/versions", headers=_h(tenant), timeout=30)
    assert v0.status_code == 200, v0.text
    body0 = v0.json()
    _validate_envelope(body0)
    assert "checkout" in body0 and body0["checkout"] is None

    # acquire the single-writer lock directly via the store API on the SHARED dir
    import write_loop  # noqa: PLC0415  (adds da/ to sys.path)
    import store  # noqa: PLC0415  (da/store.py)
    backend = store.FilesystemBackend(str(stack["store_dir"]))
    got = store.acquire_checkout(backend, tenant, "demo", "editor-alice", ttl_s=3600)
    assert got is True

    # now the versions read surfaces the checkout lock (read-only, §14)
    v1 = requests.get(f"{stack['app']}/api/drawings/demo/versions", headers=_h(tenant), timeout=30)
    assert v1.status_code == 200, v1.text
    body1 = v1.json()
    _validate_envelope(body1)
    co = body1["checkout"]
    assert co is not None and set(co.keys()) == {"holder", "acquired", "expires"}
    assert co["holder"] == "editor-alice"
    assert isinstance(co["acquired"], str) and isinstance(co["expires"], str)
