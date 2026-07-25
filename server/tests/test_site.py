"""
Binary acceptance for the public-site backend (site-demo lane):

  * string-panels solve determinism (two runs -> identical strings + stats)
  * intake-hash stability
  * NEC 690.7 math spot-check (known inputs -> expected max modules)
  * /api/site/* answer 200 with NO auth header while LEAF_AUTH_LIVE=1
    (public-by-design, like /api/health) — and /api/jobs still 401s
  * ETag + If-None-Match -> 304
  * capabilities projection carries NO params_schema / provenance
  * cache paths (broker -> memory-cache -> file-cache) serve identical solves
  * broker down -> §10 BROKER_UNREACHABLE envelope, HTTP 502

All offline / in-process (TestClient): the broker HTTP hop is replaced by a
fake that executes the REAL tool body through tool_loader.run_tool_dynamic, so
the §3 envelope is genuine; the real-HTTP broker path is covered by the live
smoke (README run recipe). Run:  cd server && python -m pytest tests/test_site.py -q
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent

# Route the jobs SQLite DB to a throwaway dir BEFORE `jobs` is ever imported
# (jobs.py reads JOBS_DB at import time) — mirrors tests/test_ui_wave.py.
os.environ.setdefault("JOBS_DB", str(Path(tempfile.mkdtemp(prefix="site-jobs-")) / "jobs.db"))

if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import deps  # noqa: E402
import site_demo  # noqa: E402
import tool_loader  # noqa: E402
from broker_client import BrokerUnreachable  # noqa: E402


# `builtins` is a stdlib name, so import the tool module by file path instead
# of `import builtins.string_panels` (which would shadow-fight the stdlib).
def _load_string_panels():
    import importlib.util

    path = SERVER_DIR / "builtins" / "string_panels.py"
    spec = importlib.util.spec_from_file_location("string_panels_tool", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


string_panels = _load_string_panels()


@pytest.fixture(scope="module")
def intake():
    return deps.load_cached_intake()


@pytest.fixture()
def site_env(monkeypatch, tmp_path):
    """Fresh caches per test: file cache in tmp, in-process cache cleared."""
    monkeypatch.setenv("LEAF_SITE_CACHE_FILE", str(tmp_path / "site_demo_cache.json"))
    site_demo.clear_cache()
    yield tmp_path
    site_demo.clear_cache()


def _fake_broker(monkeypatch, intake):
    """Replace the HTTP hop with the REAL tool execution (genuine §3 envelope)."""
    calls = {"n": 0, "event_keys": []}

    def fake_run_via_broker(
            tenant_id, tool, params, dwg, aps_live, timeout_s=None,
            ledger_event_key=None):
        calls["n"] += 1
        calls["event_keys"].append(ledger_event_key)
        return tool_loader.run_tool_dynamic(tool, intake, params, aps_live=False, da=None)

    monkeypatch.setattr(site_demo.broker_client, "run_via_broker", fake_run_via_broker)
    return calls


def test_site_demo_run_identity_dedupes_cache_fill_and_allows_explicit_refresh(
        monkeypatch, intake, site_env):
    now = 1_800_000_123.0
    monkeypatch.setattr(site_demo, "_now_epoch", lambda: now)
    calls = _fake_broker(monkeypatch, intake)
    sha = site_demo.intake_sha256()
    first = site_demo.get_demo_solve()
    second = site_demo.get_demo_solve()
    assert first["solve_id"] == second["solve_id"]
    assert calls["n"] == 1
    prefix = (
        f"site-demo:{sha}:{site_demo.SITE_TOOL['version']}:"
        f"{site_demo.SITE_RUN_GENERATION}:"
    )
    window = site_demo._window_start(now)
    assert calls["event_keys"] == [prefix + f"window-{window}"]

    site_demo.get_demo_solve(refresh_id="operator-refresh-42")
    assert calls["n"] == 2
    assert calls["event_keys"][-1] == prefix + "operator-refresh-42"


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from envelopes import install_error_handlers
    from routers import jobs as jobs_router
    from routers import site as site_router

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(site_router.router)
    app.include_router(jobs_router.router)
    return TestClient(app, raise_server_exceptions=False)


# --------------------------------------------------------------------------- #
# 1. solve determinism — two runs over the same intake are byte-identical
# --------------------------------------------------------------------------- #
def test_solve_determinism(intake):
    r1 = string_panels.run(intake, {})
    r2 = string_panels.run(intake, {})
    assert json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)
    result, overlay = r1
    assert result["stats"]["panel_count"] == 2345
    assert result["stats"]["modules_stringed"] == 2345
    n = result["electrical"]["max_modules_per_string"]
    # bank-clustered routing: at least ceil(total/n) strings (each bank chunks
    # separately and contributes its own partial remainder), and no string may
    # exceed the NEC max
    assert result["stats"]["string_count"] >= math.ceil(2345 / n)
    assert max(s["modules"] for s in result["strings"]) <= n
    assert result["stats"]["bank_count"] >= 2, "sample roof has multiple banks"
    assert result["stats"]["worst_run_delta_basis"] in ("full-strings", "all-strings")
    # strings stay inside their bank: no segment longer than the sample roof's
    # smallest bank gap (the old router leaped whole roof sections)
    worst_seg_ft = max(
        (math.hypot(b[0] - a[0], b[1] - a[1]) / 12.0
         for s in result["strings"] for a, b in zip(s["pts"], s["pts"][1:])),
        default=0.0)
    assert worst_seg_ft < 100, f"string segment {worst_seg_ft:.0f} ft crosses a bank gap"
    assert overlay["items"][0]["pts"] == result["strings"][0]["pts"]
    # world coordinates: string points sit inside the intake's coordinate range
    xs = [pt[0] for s in result["strings"] for pt in s["pts"]]
    assert min(xs) > 1000, "string pts should be WORLD coords, not normalized"


# --------------------------------------------------------------------------- #
# 2. intake-hash stability
# --------------------------------------------------------------------------- #
def test_intake_hash_stability():
    h1, h2 = site_demo.intake_sha256(), site_demo.intake_sha256()
    assert h1 == h2
    assert h1 == hashlib.sha256(deps.DATA_FILE.read_bytes()).hexdigest()
    assert len(h1) == 64


# --------------------------------------------------------------------------- #
# 3. NEC 690.7 spot-check — known inputs -> expected max modules
# --------------------------------------------------------------------------- #
def test_nec_math_spot_check():
    # 50 V, -0.3 %/degC, -25 degC, 1000 V:
    #   worst voc = 50 * (1 + (-0.003)*(-50)) = 57.5 V -> floor(1000/57.5) = 17
    n, worst = string_panels.nec_max_modules(50.0, -0.3, -25.0, 1000.0)
    assert n == 17
    assert worst == pytest.approx(57.5)
    # marketing-demo defaults (operator-selected 2026-07-20): 48.5 V,
    # -0.27 %/degC, -25 degC, 1000 V -> 18
    d = string_panels.DEFAULTS
    n_def, worst_def = string_panels.nec_max_modules(
        d["voc"], d["temp_coeff_pct_per_c"], d["design_min_temp_c"],
        d["max_system_voltage"])
    assert n_def == 18
    assert 18 * worst_def <= d["max_system_voltage"]
    assert 19 * worst_def > d["max_system_voltage"]


# --------------------------------------------------------------------------- #
# 4. public endpoints answer with NO auth header while LEAF_AUTH_LIVE=1
# --------------------------------------------------------------------------- #
def test_site_endpoints_public_when_auth_live(monkeypatch, intake, site_env):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    _fake_broker(monkeypatch, intake)
    client = _client()

    r = client.get("/api/site/demo-solve")  # NO Authorization header
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["error"] is None
    solve = body["solve"]
    assert solve["solve_id"] == f"site-{solve['intake_sha256'][:8]}"
    assert solve["stats"]["panel_count"] == 2345
    assert solve["stats"]["drafting_hours"] == 25
    assert solve["stats"]["solve_minutes"] == 3
    assert solve["electrical"]["code_check"] == "NEC 690.7"
    assert solve["electrical"]["pass"] is True
    assert solve["receipt"]["ledger_line"] is True
    assert solve["receipt"]["degraded_mode"] is False
    assert isinstance(solve["receipt"]["timing_ms"], int)

    r2 = client.get("/api/site/capabilities")
    assert r2.status_code == 200, r2.text

    # the gated surface is UNCHANGED: /api/jobs without a token still 401s
    r3 = client.get("/api/jobs")
    assert r3.status_code == 401, r3.text


# --------------------------------------------------------------------------- #
# 5. ETag / If-None-Match -> 304 (+ exact cache headers)
# --------------------------------------------------------------------------- #
def test_etag_if_none_match_304(monkeypatch, intake, site_env):
    _fake_broker(monkeypatch, intake)
    client = _client()

    r = client.get("/api/site/demo-solve")
    assert r.status_code == 200
    etag = r.headers["ETag"]
    assert etag == site_demo.solve_etag(r.json()["solve"])
    assert r.headers["Cache-Control"] == "public, max-age=300, stale-while-revalidate=3600"

    r304 = client.get("/api/site/demo-solve", headers={"If-None-Match": etag})
    assert r304.status_code == 304
    assert r304.headers["ETag"] == etag
    assert r304.content == b""

    # a stale ETag still gets the full 200
    r200 = client.get("/api/site/demo-solve", headers={"If-None-Match": '"stale-0.0.0"'})
    assert r200.status_code == 200

    # capabilities honors If-None-Match the same way
    c1 = client.get("/api/site/capabilities")
    c304 = client.get("/api/site/capabilities",
                      headers={"If-None-Match": c1.headers["ETag"]})
    assert c304.status_code == 304


# --------------------------------------------------------------------------- #
# 6. capabilities projection: NO params_schema, NO provenance
# --------------------------------------------------------------------------- #
def test_capabilities_projection_is_stripped(site_env):
    client = _client()
    r = client.get("/api/site/capabilities")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["families"]
    assert "params_schema" not in r.text
    assert "provenance" not in r.text
    for fam in body["families"]:
        assert set(fam.keys()) == {"family_id", "label", "description", "tools"}
        for tool in fam["tools"]:
            assert set(tool.keys()) == {"name", "version", "description", "capabilities"}


# --------------------------------------------------------------------------- #
# 7. cache: broker -> memory-cache -> file-cache, identical solve every time
# --------------------------------------------------------------------------- #
def test_cache_paths_and_endpoint_determinism(monkeypatch, intake, site_env):
    calls = _fake_broker(monkeypatch, intake)
    client = _client()

    s1 = client.get("/api/site/demo-solve").json()["solve"]
    assert s1["receipt"]["path"] == "broker" and calls["n"] == 1
    s2 = client.get("/api/site/demo-solve").json()["solve"]
    assert s2["receipt"]["path"] == "memory-cache" and calls["n"] == 1

    site_demo.clear_cache()  # new process simulation: file cache must hit
    s3 = client.get("/api/site/demo-solve").json()["solve"]
    assert s3["receipt"]["path"] == "file-cache" and calls["n"] == 1

    for a, b in ((s1, s2), (s1, s3)):
        assert a["strings"] == b["strings"]
        assert a["stats"] == b["stats"]
        assert a["intake_sha256"] == b["intake_sha256"]


# --------------------------------------------------------------------------- #
# 8. broker down -> §10 error envelope, HTTP 502
# --------------------------------------------------------------------------- #
def test_broker_down_is_502_broker_unreachable(monkeypatch, site_env):
    def down(*a, **k):
        raise BrokerUnreachable("broker at http://127.0.0.1:0 unreachable: test")

    monkeypatch.setattr(site_demo.broker_client, "run_via_broker", down)
    client = _client()
    r = client.get("/api/site/demo-solve")
    assert r.status_code == 502, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["error_code"] == "BROKER_UNREACHABLE"
    assert body["error"]["retryable"] is True


def test_expiry_renews_once_and_rotates_etag(monkeypatch, intake, site_env):
    clock = {"now": 1_800_000_001.0}
    monkeypatch.setattr(site_demo, "_now_epoch", lambda: clock["now"])
    calls = _fake_broker(monkeypatch, intake)

    first = site_demo.get_demo_solve()
    first_etag = site_demo.solve_etag(first)
    clock["now"] += site_demo.SITE_SOLVE_REFRESH_SECONDS
    second = site_demo.get_demo_solve()

    assert calls["n"] == 2
    assert first["receipt"]["computed_at"] != second["receipt"]["computed_at"]
    assert first_etag != site_demo.solve_etag(second)
    assert calls["event_keys"][0] != calls["event_keys"][1]


def test_expired_file_survives_failed_renewal(monkeypatch, intake, site_env):
    clock = {"now": 1_800_000_001.0}
    monkeypatch.setattr(site_demo, "_now_epoch", lambda: clock["now"])
    _fake_broker(monkeypatch, intake)
    site_demo.get_demo_solve()
    cache_path = site_demo.cache_file()
    original = cache_path.read_bytes()

    site_demo.clear_cache()
    clock["now"] += site_demo.SITE_SOLVE_REFRESH_SECONDS

    def down(*args, **kwargs):
        raise BrokerUnreachable("broker unavailable during renewal")

    monkeypatch.setattr(site_demo.broker_client, "run_via_broker", down)
    response = _client().get("/api/site/demo-solve")

    assert response.status_code == 502
    assert response.json()["error"]["error_code"] == "BROKER_UNREACHABLE"
    assert cache_path.read_bytes() == original
    assert not list(cache_path.parent.glob(f".{cache_path.name}.*.tmp"))


def test_concurrent_callers_share_one_renewal(monkeypatch, intake, site_env):
    monkeypatch.setattr(site_demo, "_now_epoch", lambda: 1_800_000_001.0)
    started = threading.Event()
    release = threading.Event()
    call_lock = threading.Lock()
    calls = {"n": 0}

    def slow_broker(tenant_id, tool, params, dwg, aps_live, timeout_s=None,
                    ledger_event_key=None):
        with call_lock:
            calls["n"] += 1
        started.set()
        assert release.wait(timeout=5)
        return tool_loader.run_tool_dynamic(
            tool, intake, params, aps_live=False, da=None)

    monkeypatch.setattr(site_demo.broker_client, "run_via_broker", slow_broker)
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(site_demo.get_demo_solve) for _ in range(6)]
        assert started.wait(timeout=5)
        release.set()
        solves = [future.result(timeout=10) for future in futures]

    assert calls["n"] == 1
    assert sum(solve["receipt"]["path"] == "broker" for solve in solves) == 1
    assert all(solve["solve_id"] == solves[0]["solve_id"] for solve in solves)
