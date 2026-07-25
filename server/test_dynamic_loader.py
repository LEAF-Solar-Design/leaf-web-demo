"""
Binary acceptance for the dynamic tool loader (APS_LIVE=0, no operator).

Boots the real broker + app subprocesses (same harness style as
tests/test_backbone.py) and drives the HTTP surface end to end:

  * author a tool OUTSIDE the 3-op family ("list all layer names") -> its `code`
    is persisted to a real file under server/authored/ -> it appears in
    /api/tools -> /api/run returns a §3 envelope whose `result` is produced by
    THAT FILE (proven with a sentinel rewrite) and differs from count-by-layer.
  * the produced envelope schema-validates against engine/envelope_schema.json.
  * bad params -> ok:false BAD_PARAMS envelope, tool body never runs.
  * no OPS/OP_ALIASES/MIRRORS entry exists for the authored engine_op.
  * the 3 built-ins return the recorded byte-identical result/overlay.

Run:  cd server && python -m pytest test_dynamic_loader.py -q
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import jsonschema
import pytest
import requests

from _test_run_confirmation import confirmed_requests_payload

SERVER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SERVER_DIR.parent
ENGINE_SCHEMA = json.loads((PROJECT_ROOT / "engine" / "envelope_schema.json").read_text(encoding="utf-8"))
AUTHORED_DIR = SERVER_DIR / "authored"

FORBIDDEN_OPS = {"count_by_layer", "measure_area_by_layer", "measure_panel_area",
                 "highlight_near_edge", "highlight_panels_near_edge"}

CORE_KEYS = ("ok", "tool", "version", "result", "overlay", "timing_ms", "cost", "error")

# Recorded byte-identical baseline of the 3 built-ins (golden intake, params {}),
# captured BEFORE the dynamic-loader change.
BUILTIN_BASELINE = {
    "count-by-layer": {
        "result": {"counts": {"Panels": 2345}, "total": 2345},
        "overlay": None,
    },
    "measure-panel-area": {
        "result": {"avg_area": 2991.65, "count": 2345, "layer": "Panels",
                   "total_area": 7015420.07, "units": "drawing"},
        "overlay": None,
    },
    "highlight-panels-near-edge": {
        "result": {"bounds": {"maxx": 21108.478, "maxy": 4856.626,
                              "minx": 13940.333, "miny": 1125.414},
                   "layer": "Panels", "margin": 186.561, "matched": 215,
                   "total_on_layer": 2345},
        "overlay_handles": 215, "overlay_markers": 215, "first_handle": "93FC",
    },
}


# --------------------------------------------------------------------------- #
# harness
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
    tmp = tmp_path_factory.mktemp("dynloader")
    broker_port, app_port = free_port(), free_port()
    broker = start_uvicorn("broker:app", broker_port,
                           {"BROKER_LEDGER": tmp / "ledger.jsonl",
                            "BROKER_TENANTS": tmp / "tenants.json"},
                           tmp / "broker.log")
    app = start_uvicorn("app:app", app_port,
                        {"APS_LIVE": "0", "APS_CRED": "/nonexistent",
                         "BROKER_URL": f"http://127.0.0.1:{broker_port}",
                         "JOBS_DB": tmp / "jobs.db"},
                        tmp / "app.log")
    try:
        wait_ready(f"http://127.0.0.1:{broker_port}/broker/health", broker)
        wait_ready(f"http://127.0.0.1:{app_port}/api/health", app)
        # WARM-UP: the broker's FIRST /broker/run lazily loads the engine registry +
        # the 2345-polyline intake; under sustained parallel load that cold call can be
        # CPU-starved past the poll timeout (gate-runner flake follow-up). Force it now,
        # best-effort, so the timed assertions below run against a warm broker.
        try:
            app_url = f"http://127.0.0.1:{app_port}"
            headers = {"X-Tenant-Id": "warmup"}
            requests.post(f"http://127.0.0.1:{app_port}/api/run?wait=1",
                          json=confirmed_requests_payload(
                              app_url, "count-by-layer", dwg="rooftop_demo",
                              headers=headers),
                          headers=headers, timeout=120)
        except Exception:
            pass
        yield {"app": f"http://127.0.0.1:{app_port}", "broker": f"http://127.0.0.1:{broker_port}"}
    finally:
        stop(app)
        stop(broker)


def run_wait(stack, tool, params=None):
    r = requests.post(f"{stack['app']}/api/run?wait=1",
                      json=confirmed_requests_payload(
                          stack["app"], tool, params, "rooftop_demo"),
                      timeout=120)
    return r


def core_validates(env: dict) -> None:
    core = {k: env[k] for k in CORE_KEYS if k in env}
    jsonschema.validate(core, ENGINE_SCHEMA)  # raises on failure


# --------------------------------------------------------------------------- #
# ACCEPTANCE 7 (do it first): the 3 built-ins are byte-identical to baseline
# --------------------------------------------------------------------------- #
def test_builtins_byte_identical(stack):
    # count-by-layer
    e = run_wait(stack, "count-by-layer").json()
    assert e["ok"] is True
    assert e["result"] == BUILTIN_BASELINE["count-by-layer"]["result"]
    assert e["overlay"] == BUILTIN_BASELINE["count-by-layer"]["overlay"]
    core_validates(e)

    # measure-panel-area
    e = run_wait(stack, "measure-panel-area").json()
    assert e["ok"] is True
    assert e["result"] == BUILTIN_BASELINE["measure-panel-area"]["result"]
    assert e["overlay"] is None
    core_validates(e)

    # highlight-panels-near-edge
    e = run_wait(stack, "highlight-panels-near-edge").json()
    assert e["ok"] is True
    assert e["result"] == BUILTIN_BASELINE["highlight-panels-near-edge"]["result"]
    ov = e["overlay"]
    assert len(ov["highlight_handles"]) == BUILTIN_BASELINE["highlight-panels-near-edge"]["overlay_handles"]
    assert len(ov["markers"]) == BUILTIN_BASELINE["highlight-panels-near-edge"]["overlay_markers"]
    assert ov["highlight_handles"][0] == BUILTIN_BASELINE["highlight-panels-near-edge"]["first_handle"]
    core_validates(e)


# --------------------------------------------------------------------------- #
# ACCEPTANCE 1+2+3+8: author outside the family -> file persisted -> in /api/tools
# -> /api/run executes THAT FILE -> §3 envelope validates + differs from count
# --------------------------------------------------------------------------- #
def test_author_persist_and_run_from_file(stack):
    a = requests.post(f"{stack['app']}/api/author",
                      json={"description": "list all layer names in the drawing"},
                      timeout=15).json()
    assert set(("tool", "code", "preview")).issubset(a.keys())
    tool = a["tool"]
    engine_op = tool["engine_op"]
    entry = tool["entry"]

    # (1) engine_op AND entry are outside the pre-coded 3-op family
    assert engine_op not in FORBIDDEN_OPS, engine_op
    assert entry not in FORBIDDEN_OPS
    assert not any(op in str(entry) for op in FORBIDDEN_OPS), entry

    # (1) the code is persisted to the tenant-scoped file named by the returned
    # entry. The path must stay below server/authored and must not fall back to
    # the legacy flat store, which allowed two tenants to overwrite each other.
    relative_entry = Path(entry)
    assert not relative_entry.is_absolute()
    assert relative_entry.parts[0] == "authored"
    assert len(relative_entry.parts) == 3
    assert relative_entry.name == f"{tool['name']}.py"
    fpath = SERVER_DIR / relative_entry
    assert fpath.resolve().is_relative_to(AUTHORED_DIR.resolve())
    assert fpath.exists(), fpath
    assert "def run(" in fpath.read_text(encoding="utf-8")

    # (2) it appears in /api/tools by name
    names = {t["name"] for t in requests.get(f"{stack['app']}/api/tools", timeout=10).json()["tools"]}
    assert tool["name"] in names

    # (3) /api/run returns a §3 envelope whose result is produced by the FILE and
    #     differs from count-by-layer's output on the same golden intake
    e = run_wait(stack, tool["name"], {}).json()
    assert e["ok"] is True and e["error"] is None
    assert "layers" in e["result"] and "count" in e["result"]
    count_env = run_wait(stack, "count-by-layer").json()
    assert e["result"] != count_env["result"]
    core_validates(e)  # (8) schema-valid §3 envelope (engine/envelope_schema.json)

    # (3, strengthened) PROVE the PERSISTED FILE is what executes: overwrite it
    # with a sentinel body and re-run — the new bytes must appear in result.
    original = fpath.read_text(encoding="utf-8")
    try:
        fpath.write_text(
            'def run(intake, params):\n'
            '    return ({"sentinel": "FILE-IS-THE-TOOL-42", "layers": [], "count": 0}, None)\n',
            encoding="utf-8")
        e2 = run_wait(stack, tool["name"], {}).json()
        assert e2["ok"] is True
        assert e2["result"].get("sentinel") == "FILE-IS-THE-TOOL-42", e2["result"]
    finally:
        # restore the real list-layers body so the persisted tool stays meaningful
        fpath.write_text(original, encoding="utf-8")


# --------------------------------------------------------------------------- #
# ACCEPTANCE 6: bad params -> ok:false BAD_PARAMS envelope, body never runs
# --------------------------------------------------------------------------- #
def test_bad_params_pre_validation_gate(stack):
    a = requests.post(f"{stack['app']}/api/author",
                      json={"description": "list all layer names in the drawing"},
                      timeout=15).json()
    name = a["tool"]["name"]
    # prefix must be a string; sending a number violates the tool's own schema
    r = run_wait(stack, name, {"prefix": 123})
    env = r.json()
    assert env["ok"] is False
    assert env["error"]["error_code"] == "BAD_PARAMS", env["error"]
    # body never ran: no result payload leaked through
    assert env.get("result") in (None, {}), env.get("result")


# --------------------------------------------------------------------------- #
# ACCEPTANCE 4: no OPS/OP_ALIASES (tools_fallback) or MIRRORS (selfcheck) entry
#               for the authored engine_op — proving the FILE ran, not a handler
# --------------------------------------------------------------------------- #
def test_no_hardcoded_handler_for_authored_op(stack):
    a = requests.post(f"{stack['app']}/api/author",
                      json={"description": "list all layer names in the drawing"},
                      timeout=15).json()
    engine_op = a["tool"]["engine_op"]

    fb_src = (SERVER_DIR / "tools_fallback.py").read_text(encoding="utf-8")
    # isolate the OPS + OP_ALIASES literals and assert the authored op is absent
    ops_region = fb_src[fb_src.index("OPS = {"):fb_src.index("def resolve_op")]
    assert f'"{engine_op}"' not in ops_region, f"{engine_op} leaked into OPS/OP_ALIASES"

    sc_src = (PROJECT_ROOT / "engine" / "selfcheck.py").read_text(encoding="utf-8")
    mirrors_region = sc_src[sc_src.index("MIRRORS = {"):sc_src.index("def run_mock")]
    assert f'"{engine_op}"' not in mirrors_region, f"{engine_op} leaked into MIRRORS"
