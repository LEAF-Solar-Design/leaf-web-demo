"""
Binary acceptance for UI wave 3 (backend), CONTRACT-ADDENDUM §15.

  Contract 2  tenant-repo registry fold precedence + entry resolution + the M1-authored
              `layer-bounding-boxes` run-with-matching-bbox (APS_LIVE=0), both in-process
              and end-to-end THROUGH THE REAL BROKER.
  Contract 3  /api/author top-level `source` + `static_scan` for the TEMPLATE path AND the
              HARNESS path (harness mocked with a local HTTP stub — no real SDK).
  Contract 4  nl-router `alternatives` + BUILD-INTENT boost (fixes the known miss).
  Contract 5a durable spine_ref terminal sync surviving a simulated restart (Neon).
  Contract 5b ops read-your-write: disable -> immediately reflected by GET /api/ops/tenants.
  Contract 5c richer job progress phase vocabulary + observable "executing".

Styles mirror tests/test_wave2.py + tests/test_ui_wave.py: in-process TestClient / pure-unit
for the hermetic pieces, the platform package under the `leaf_platform` alias (+ Neon) for
Contract 5a (SKIPPED-with-reason if the DB is unreachable), and a real broker+app subprocess
stack for the through-the-broker run, ops freshness, and observable progress.

Run:  cd server && python -m pytest tests/test_wave3.py -q
"""
from __future__ import annotations

# Cache the STDLIB `platform` module BEFORE anything puts PROJECT_ROOT on sys.path (the
# local `platform/` package otherwise shadows it and breaks attrs/jsonschema — mirrors the
# guard in server/broker.py).
import platform as _stdlib_platform  # noqa: E402
_stdlib_platform.python_implementation()

import http.server  # noqa: E402
import importlib  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402

import jsonschema  # noqa: E402
import pytest  # noqa: E402
import requests  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SERVER_DIR.parent
PLATFORM_DIR = PROJECT_ROOT / "platform"
TENANT_REPO_SRC = Path(os.environ.get("LEAF_TENANT_REPO_SRC", "C:/tmp/leaf-tenants/demo-tenant"))
INTAKE_PATH = PROJECT_ROOT / "data" / "rooftop_demo.intake.json"

# route the jobs SQLite DB to a throwaway dir BEFORE `jobs` is imported anywhere.
os.environ.setdefault("JOBS_DB", str(Path(tempfile.mkdtemp(prefix="wave3-jobs-")) / "jobs.db"))

if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

ENVELOPE_SCHEMA = json.loads((SERVER_DIR / "envelope_schema.json").read_text(encoding="utf-8"))

# Receipt oracle: data/nl_author_receipt.json's Panels bounding box (independent check).
PANELS_EXPECTED = {"min_x": 13940.333, "max_x": 21108.478, "min_y": 1125.414, "max_y": 4856.626}
PANELS_AREA = 26745868.641739998
PANELS_GEOMETRY_COUNT = 2345


def _assert_panels_bbox(env: dict) -> None:
    assert env["ok"] is True and env.get("degraded_mode") is False, env
    panels = [l for l in env["result"]["layers"] if l["layer"] == "Panels"]
    assert panels, env["result"]
    p = panels[0]
    for k, v in PANELS_EXPECTED.items():
        assert abs(p[k] - v) < 0.01, (k, p[k], v)
    assert abs(p["area"] - PANELS_AREA) < 1.0, p["area"]
    assert p["geometry_count"] == PANELS_GEOMETRY_COUNT, p["geometry_count"]


# =========================================================================== #
# Contract 2 — tenant-repo registry fold + entry resolution (in-process)
# =========================================================================== #
def test_C2_fold_off_by_default_is_byte_identical(monkeypatch):
    import deps
    monkeypatch.delenv("LEAF_TENANT_REPO", raising=False)
    assert deps.load_tenant_repo_tools() == []
    names = [t["name"] for t in deps.all_tools()]
    assert "layer-bounding-boxes" not in names  # tenant fold OFF -> not present


def test_C2_fold_precedence_tenant_over_engine_authored_over_both(monkeypatch):
    import deps
    monkeypatch.setenv("LEAF_TENANT_REPO", str(TENANT_REPO_SRC))
    monkeypatch.setattr(deps, "_AUTHORED", [])  # isolate authored layer for the assertion
    names = [t["name"] for t in deps.all_tools()]
    assert "layer-bounding-boxes" in names  # folded from the tenant repo

    # tenant `count-by-layer` (has an explicit entry) WINS over the engine registry's.
    cbl = deps.find_tool("count-by-layer")
    assert cbl.get("entry") == "tools/count-by-layer/tool.py"

    # authored_tools.json WINS over both: a same-named authored tool overrides the tenant one.
    sentinel = {"name": "layer-bounding-boxes", "engine_op": "x", "_which": "authored",
                "params": {"type": "object", "properties": {}}}
    monkeypatch.setattr(deps, "_AUTHORED", [sentinel])
    assert deps.find_tool("layer-bounding-boxes").get("_which") == "authored"


def test_C2_entry_resolves_against_tenant_root_absolute(monkeypatch):
    import deps
    from tool_loader import resolve_local_file
    monkeypatch.setenv("LEAF_TENANT_REPO", str(TENANT_REPO_SRC))
    monkeypatch.setattr(deps, "_AUTHORED", [])
    tool = deps.find_tool("layer-bounding-boxes")
    resolved = resolve_local_file(tool)
    assert resolved is not None
    assert resolved.is_absolute()
    assert resolved == (TENANT_REPO_SRC / "tools" / "layer-bounding-boxes" / "tool.py")
    # unset -> no tenant-root resolution (the entry alone does not resolve under server/)
    monkeypatch.delenv("LEAF_TENANT_REPO", raising=False)
    assert resolve_local_file({"name": "x", "entry": "tools/layer-bounding-boxes/tool.py"}) is None


def test_C2_run_matches_receipt_bbox_in_process(monkeypatch):
    import deps
    from tool_loader import run_tool_dynamic
    monkeypatch.setenv("LEAF_TENANT_REPO", str(TENANT_REPO_SRC))
    monkeypatch.setattr(deps, "_AUTHORED", [])
    tool = deps.find_tool("layer-bounding-boxes")
    intake = json.loads(INTAKE_PATH.read_text(encoding="utf-8"))
    env = run_tool_dynamic(tool, intake, {}, aps_live=False, da=None)
    _assert_panels_bbox(env)


# =========================================================================== #
# Contract 3 — /api/author source + static_scan (template AND mocked harness)
# =========================================================================== #
class _HarnessStubHandler(http.server.BaseHTTPRequestHandler):
    RESPONSE: dict = {}

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", 0) or 0)
        self.rfile.read(length)
        body = json.dumps(type(self).RESPONSE).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence
        return


@pytest.fixture
def harness_stub():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _HarnessStubHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}", _HarnessStubHandler
    finally:
        srv.shutdown()


def test_C3_author_template_source_and_static_scan(monkeypatch, tmp_path):
    import deps
    monkeypatch.delenv("LEAF_AUTHOR_HARNESS_URL", raising=False)
    # isolate the authored store so this never pollutes the shared authored_tools.json
    monkeypatch.setattr(deps, "AUTHORED_STORE", tmp_path / "authored.json")
    monkeypatch.setattr(deps, "_AUTHORED", [])

    from fastapi.testclient import TestClient
    from app import app
    c = TestClient(app)
    r = c.post("/api/author", json={"description": "count panels on layer Panels"})
    assert r.status_code == 200, r.text
    b = r.json()
    jsonschema.validate(b, ENVELOPE_SCHEMA)
    assert b["source"] == "template"
    assert isinstance(b["static_scan"], list)
    assert b["tool"]["provenance"]["static_scan"] == b["static_scan"]
    assert {"tool", "code", "preview"} <= set(b)
    # template path DID persist locally (into the isolated store)
    assert b["tool"]["name"] in [t["name"] for t in deps._AUTHORED]


def test_C3_author_harness_source_no_local_persist(monkeypatch, harness_stub, tmp_path):
    url, stub = harness_stub
    stub.RESPONSE = {
        "tool": {
            "name": "wave3-harness-probe", "version": "1.0.0", "kind": "script",
            "engine_op": "wave3_probe", "entry": "tools/wave3-harness-probe/tool.py",
            "params": {"type": "object", "properties": {}}, "returns": {"type": "object"},
            "capabilities": ["drawing.read"],
            "provenance": {"author": "agent", "created": "2026-07-18T00:00:00Z"},
        },
        "code": "def run(intake, params):\n    return ({'ok': True}, None)\n",
        "preview": "Tool authored via the Agent SDK (mock stub).",
    }
    import deps
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setattr(deps, "AUTHORED_STORE", tmp_path / "authored.json")
    monkeypatch.setattr(deps, "_AUTHORED", [])

    from fastapi.testclient import TestClient
    from app import app
    c = TestClient(app)
    r = c.post("/api/author", json={"description": "make a tool that probes"})
    assert r.status_code == 200, r.text
    b = r.json()
    jsonschema.validate(b, ENVELOPE_SCHEMA)
    assert b["source"] == "harness"
    assert isinstance(b["static_scan"], list)
    assert b["tool"]["name"] == "wave3-harness-probe"
    assert b["tool"]["provenance"]["static_scan"] == b["static_scan"]
    # harness tools are ALREADY registered in the tenant repo -> NOT persisted locally
    # (they surface in /api/tools via Contract 2's fold, not the authored store).
    assert "wave3-harness-probe" not in [t["name"] for t in deps._AUTHORED]


# =========================================================================== #
# Contract 4 — nl-router alternatives + build-intent boost (in-process)
# =========================================================================== #
from nl_router import LANE_BUILD, LANE_RUN, classify  # noqa: E402

_C4_CATALOG = [
    {"name": "count-by-layer", "engine_op": "count_by_layer",
     "description": "Counts every model-space entity per layer.",
     "params": {"type": "object", "properties": {}}},
    {"name": "count-panels", "engine_op": "count_by_layer",
     "description": "count panels on layer Panels",
     "params": {"type": "object", "properties": {"layer": {"type": "string"}}}},
    {"name": "measure-panel-area", "engine_op": "measure_area_by_layer",
     "description": "measure the panel area",
     "params": {"type": "object", "properties": {}}},
    {"name": "highlight-panels-near-edge", "engine_op": "highlight_near_edge",
     "description": "highlight panels near the edge",
     "params": {"type": "object", "properties": {"distance": {"type": "number"}}}},
]


def test_C4_build_boost_fixes_known_miss():
    text = "make a tool that counts panels smaller than 10 sqft"
    r = classify(text, _C4_CATALOG)
    assert r["lane"] == LANE_BUILD, r
    assert r["tool"] is None
    assert r["params"]["description"] == text


def test_C4_build_boost_other_verbs_route_build():
    for text in ("build a tool that counts panels under 10 sqft",
                 "create a tool to count panels below a threshold",
                 "author me a tool that tallies panels smaller than that"):
        assert classify(text, _C4_CATALOG)["lane"] == LANE_BUILD, text


def test_C4_explicit_build_honors_exact_name():
    # "count panels" essentially NAMES the count-panels tool exactly -> honour RUN.
    r = classify("count panels", _C4_CATALOG)
    assert r["lane"] == LANE_RUN and r["tool"] == "count-panels"


def test_C4_alternatives_shape_and_winner_excluded_on_run():
    r = classify("count panels per layer", _C4_CATALOG)
    assert r["lane"] == LANE_RUN
    alts = r["alternatives"]
    assert isinstance(alts, list) and len(alts) <= 3
    for a in alts:
        assert set(a.keys()) == {"tool", "confidence"}
        assert 0.0 <= a["confidence"] <= 1.0
        assert a["tool"] != r["tool"]  # the winner is never in its own alternatives


def test_C4_alternatives_empty_on_gibberish():
    r = classify("asdf qwer zxcvbn plorp", _C4_CATALOG)
    assert r["tool"] is None
    assert r["alternatives"] == []


def test_C4_alternatives_surface_partial_matches_on_build():
    r = classify("make a tool that counts panels smaller than 10 sqft", _C4_CATALOG)
    assert r["lane"] == LANE_BUILD
    assert isinstance(r["alternatives"], list) and len(r["alternatives"]) <= 3
    assert any(a["tool"] == "count-panels" for a in r["alternatives"])


def test_C4_endpoint_carries_alternatives():
    from fastapi.testclient import TestClient
    from app import app
    c = TestClient(app)
    r = c.post("/api/nl-prompt", json={"text": "count panels per layer"})
    assert r.status_code == 200, r.text
    b = r.json()
    jsonschema.validate(b, ENVELOPE_SCHEMA)
    assert "alternatives" in b and isinstance(b["alternatives"], list)
    assert b["error"] is None and b["degraded_mode"] is False


# =========================================================================== #
# Contract 5a — durable spine_ref terminal sync (Neon; skip if DB unreachable)
# =========================================================================== #
def _load_leaf_platform():
    if "leaf_platform" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "leaf_platform", PLATFORM_DIR / "__init__.py",
            submodule_search_locations=[str(PLATFORM_DIR)])
        mod = importlib.util.module_from_spec(spec)
        sys.modules["leaf_platform"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["leaf_platform"]


def _db_available() -> "tuple[bool, str]":
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


@requires_db
def test_C5a_durable_spine_ref_sync_survives_restart():
    import platform_link
    store = _pf_store()
    org = store.create_org("Wave3 Durable Org")
    project = store.create_project(org.org_id, "Wave3 Durable Project")
    spine_id = str(uuid.uuid4())

    platform_link._MAP.clear()
    platform_link.on_submit(spine_id, str(org.org_id), str(project.project_id),
                            "count-by-layer", {"n": 1})
    rows = store.list_jobs(org.org_id, project.project_id)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row.spine_ref == spine_id
    assert row.kind == "run" and row.tool_name == "count-by-layer"
    assert row.status == "queued"
    assert row.result is None

    # SIMULATE APP RESTART: reload the module so the in-process _MAP + memoized pool are gone.
    importlib.reload(platform_link)
    assert platform_link._MAP == {}

    # the terminal sync now finds the durable row by its unique spine_ref (no map needed).
    env = {"ok": True, "tool": "count-by-layer", "version": "1.0.0",
           "result": {"total": 2345}, "overlay": None, "timing_ms": 5,
           "cost": {"usd_est": 0.0123}, "error": None, "degraded_mode": False}
    platform_link.on_terminal(spine_id, "complete", env)

    after = store.get_job(org.org_id, row.job_id)
    assert after is not None
    assert after.status == "succeeded", after.status
    assert after.spine_ref == spine_id
    assert isinstance(after.result, dict) and after.result.get("ok") is True
    assert after.cost_usd is not None and abs(float(after.cost_usd) - 0.0123) < 1e-6


# =========================================================================== #
# Contract 5c — progress phase vocabulary (pure unit)
# =========================================================================== #
def test_C5c_progress_phase_vocabulary():
    import jobs
    assert jobs._progress_phase({"capabilities": ["drawing.read"]}, False) == "executing"
    assert jobs._progress_phase({"capabilities": ["drawing.read"]}, True) == "executing"
    assert jobs._progress_phase({"capabilities": ["drawing.write"]}, False) == "storing version"
    assert jobs._progress_phase({"capabilities": ["drawing.write"]}, True) == "extracting"
    assert jobs._progress_phase({}, False) == "executing"  # no capabilities -> read default


# =========================================================================== #
# subprocess stack (broker + app) with LEAF_TENANT_REPO -> through-the-broker tests
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
    tmp = tmp_path_factory.mktemp("wave3")
    # Run against a COPY of the tenant repo (never mutate the live one).
    tenant_copy = tmp / "tenant"
    shutil.copytree(TENANT_REPO_SRC, tenant_copy,
                    ignore=shutil.ignore_patterns(".git", "__pycache__"))
    store_dir = tmp / "drawings"
    ledger = tmp / "ledger.jsonl"
    broker_port, app_port = free_port(), free_port()
    common = {"APS_LIVE": "0", "LEAF_TENANT_REPO": str(tenant_copy)}
    broker = start_uvicorn("broker:app", broker_port,
                           {**common, "BROKER_LEDGER": ledger,
                            "BROKER_TENANTS": tmp / "tenants.json",
                            "LEAF_STORE_DIR": store_dir},
                           tmp / "broker.log")
    app = start_uvicorn("app:app", app_port,
                        {**common, "APS_CRED": "/nonexistent",
                         "BROKER_URL": f"http://127.0.0.1:{broker_port}",
                         "JOBS_DB": tmp / "jobs.db", "LEAF_STORE_DIR": store_dir,
                         "LEAF_USAGE_LEDGER": ledger},
                        tmp / "app.log")
    try:
        wait_ready(f"http://127.0.0.1:{broker_port}/broker/health", broker)
        wait_ready(f"http://127.0.0.1:{app_port}/api/health", app)
        # WARM-UP: the broker's FIRST /broker/run lazily loads the engine registry +
        # the 2345-polyline intake; under sustained parallel load that cold call can be
        # CPU-starved past the poll timeout (gate-runner flake follow-up). Force it now,
        # best-effort, so the timed assertions below run against a warm broker.
        try:
            requests.post(f"http://127.0.0.1:{app_port}/api/run?wait=1",
                          json={"tool": "count-by-layer", "params": {}, "dwg": "rooftop_demo"},
                          headers={"X-Tenant-Id": "warmup"}, timeout=120)
        except Exception:
            pass
        yield {"app": f"http://127.0.0.1:{app_port}",
               "broker": f"http://127.0.0.1:{broker_port}",
               "app_log": tmp / "app.log"}
    finally:
        stop(app)
        stop(broker)


def _h(tenant: str) -> dict:
    return {"X-Tenant-Id": tenant}


# --- Contract 2 (through the real broker) ---------------------------------- #
def test_C2_layer_bounding_boxes_in_api_tools(stack):
    r = requests.get(f"{stack['app']}/api/tools", timeout=15)
    assert r.status_code == 200, r.text
    names = {t["name"] for t in r.json()["tools"]}
    assert "layer-bounding-boxes" in names


def test_C2_run_through_broker_matches_receipt_bbox(stack):
    r = requests.post(f"{stack['app']}/api/run?wait=1",
                      json={"tool": "layer-bounding-boxes", "params": {}, "dwg": "rooftop_demo"},
                      headers=_h("wave3-c2"), timeout=60)
    assert r.status_code == 200, r.text
    _assert_panels_bbox(r.json())


# --- Contract 5b (ops read-your-write) ------------------------------------- #
def test_C5b_ops_disable_immediately_reflected(stack):
    tenant = "wave3-ops"
    qa = {"X-Internal-Role": "qa"}
    # baseline run so the tenant shows up in the ledger aggregation
    base = requests.post(f"{stack['app']}/api/run?wait=1",
                         json={"tool": "count-by-layer", "params": {}},
                         headers=_h(tenant), timeout=60)
    assert base.status_code == 200, base.text

    d = requests.post(f"{stack['app']}/api/ops/tenants/{tenant}/disable", headers=qa, timeout=15)
    assert d.status_code == 200 and d.json()["disabled"] is True, d.text

    # IMMEDIATELY (no sleep) reflected by the very next read (read-your-write, no /health cache)
    tl = requests.get(f"{stack['app']}/api/ops/tenants", headers=qa, timeout=15)
    assert tl.status_code == 200, tl.text
    jsonschema.validate(tl.json(), ENVELOPE_SCHEMA)
    row = {t["tenant_id"]: t for t in tl.json()["tenants"]}[tenant]
    assert row["disabled"] is True

    # re-enable and confirm immediate freshness the other direction (cleanup)
    e = requests.post(f"{stack['app']}/api/ops/tenants/{tenant}/enable", headers=qa, timeout=15)
    assert e.status_code == 200 and e.json()["disabled"] is False, e.text
    tl2 = requests.get(f"{stack['app']}/api/ops/tenants", headers=qa, timeout=15)
    row2 = {t["tenant_id"]: t for t in tl2.json()["tenants"]}[tenant]
    assert row2["disabled"] is False


# --- Contract 5c (observable "executing" progress) ------------------------- #
def test_C5c_executing_progress_observable(stack):
    # a slow read run holds `progress='executing'` during the broker sleep so a poller
    # sees more than status flips.
    r = requests.post(f"{stack['app']}/api/run",
                      json={"tool": "count-by-layer", "params": {"_qa_sleep_s": 1.5},
                            "dwg": "rooftop_demo"},
                      headers=_h("wave3-prog"), timeout=15)
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]

    seen_progress = set()
    seen_status = set()
    deadline = time.time() + 30
    rec = None
    while time.time() < deadline:
        rec = requests.get(f"{stack['app']}/api/jobs/{job_id}",
                           headers=_h("wave3-prog"), timeout=10).json()
        if rec.get("progress"):
            seen_progress.add(rec["progress"])
        seen_status.add(rec["status"])
        if rec["status"] in ("complete", "failed"):
            break
        time.sleep(0.05)
    assert rec is not None and rec["status"] == "complete", (seen_status, seen_progress)
    assert "executing" in seen_progress, f"progress phases seen: {sorted(seen_progress)}"
    assert "done" in seen_progress  # terminal progress still set
