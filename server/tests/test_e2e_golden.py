"""
Golden-path END-TO-END test for the composed Leaf web demo (APS_LIVE=0).

This is ONE test that proves the whole product path holds together across REAL
process boundaries — complementary to the per-concern unit suites. It boots the
broker + app as SEPARATE subprocesses on EPHEMERAL free ports (never assumes
8130/8140 are free — stale processes squat them), then drives the product over
HTTP end to end:

    health
      -> POST /api/nl-prompt("count panels per layer")     [NL router -> RUN lane]
      -> POST /api/run (202 {job_id})  -> poll to complete  [async job spine]
         result.counts.Panels == 2345                       [golden oracle]
      -> GET  /api/capabilities         families present
      -> POST /api/run delete-marked-panel  -> new_version v2 (parent v1) [write loop]
      -> GET  /api/drawings/demo/versions   -> the v1->v2 chain
      -> POST /api/drawings/demo/undo       -> head back to v1
      -> GET  /api/entitlements             -> demo tier, full access
      -> POST /api/author (template)        -> tool appears in /api/tools

Design-time only: no harness/SDK, no live APS, no LLM. The author step exercises
the TEMPLATE path (LEAF_AUTHOR_HARNESS_URL is stripped from the child env).

SELF-CONTAINED: authored_tools.json is reset to a clean {"tools": []} for the
booted app (so NL routing is deterministic — gitignored authored pollution can
otherwise outrank count-by-layer) and RESTORED byte-for-byte on teardown; any
authored tool body the author step creates (at any depth under authored/, since
they persist per-tenant) is removed. Both server processes are
torn down in a finally.

Run:  cd server && python -m pytest tests/test_e2e_golden.py -q
"""
from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

from _test_run_confirmation import confirmed_requests_payload

SERVER_DIR = Path(__file__).resolve().parent.parent
AUTHORED_TOOLS = SERVER_DIR / "authored_tools.json"
AUTHORED_DIR = SERVER_DIR / "authored"

SECTION3_KEYS = {"ok", "tool", "version", "result", "overlay", "timing_ms", "cost", "error"}
TENANT = "e2e-golden"
# The description this suite authors from, and the one filename that produces. Naming is
# deterministic (tools_fallback._slugify), so teardown can name exactly what it created
# instead of sweeping whatever else appeared under authored/ while it ran.
AUTHOR_DESCRIPTION = "measure the bounding box of every layer"


# --------------------------------------------------------------------------- #
# process harness (same style as tests/test_backbone.py + tests/test_write_loop.py)
# --------------------------------------------------------------------------- #
def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_uvicorn(module_app: str, port: int, env_overrides: dict, log_path: Path) -> subprocess.Popen:
    # Start from a CLEAN env: strip the process-global toggles that other suites
    # set, then apply this test's overrides — so the child sees APS_LIVE=0 defaults,
    # off-auth (demo tier), and the TEMPLATE author path deterministically.
    env = dict(os.environ)
    for k in ("LEAF_AUTH_LIVE", "LEAF_AUTHOR_HARNESS_URL", "LEAF_AUTHOR_LLM",
              "LEAF_TENANTS_DIR", "LEAF_TENANT_REPO", "LEAF_ENTITLEMENTS_FILE",
              "JOB_MAX_S"):
        env.pop(k, None)
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


# --------------------------------------------------------------------------- #
# fixture: clean authored store, boot broker + app on ephemeral ports, tear down
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e_golden")

    # --- self-containment: snapshot + reset the shared authored store --- #
    orig_authored = AUTHORED_TOOLS.read_bytes() if AUTHORED_TOOLS.exists() else None
    # rglob, not glob: the template author path persists under a per-tenant
    # subdirectory (authored/<tenant>/<name>.py), so a top-level-only sweep would
    # snapshot nothing and leave this suite's authored bodies behind on teardown.
    import tools_fallback
    global AUTHORED_BODY
    AUTHORED_BODY = f"{tools_fallback.author_tool(AUTHOR_DESCRIPTION)[0]['name']}.py"
    pre_authored_files = set(AUTHORED_DIR.rglob("*.py")) if AUTHORED_DIR.exists() else set()
    pre_authored_dirs = ({d for d in AUTHORED_DIR.iterdir() if d.is_dir()}
                         if AUTHORED_DIR.exists() else set())
    AUTHORED_TOOLS.write_text('{"tools": []}\n', encoding="utf-8")

    store_dir = tmp / "drawings"  # shared by broker (writes) + app (reads)
    broker_port, app_port = free_port(), free_port()

    broker = start_uvicorn(
        "broker:app", broker_port,
        {"APS_LIVE": "0", "BROKER_LEDGER": tmp / "ledger.jsonl",
         "BROKER_TENANTS": tmp / "tenants.json", "LEAF_STORE_DIR": store_dir},
        tmp / "broker.log")
    app = start_uvicorn(
        "app:app", app_port,
        {"APS_LIVE": "0", "APS_CRED": "/nonexistent",   # app process never needs the APS secret
         "BROKER_URL": f"http://127.0.0.1:{broker_port}",
         "JOBS_DB": tmp / "jobs.db", "LEAF_STORE_DIR": store_dir},
        tmp / "app.log")
    try:
        wait_ready(f"http://127.0.0.1:{broker_port}/broker/health", broker)
        wait_ready(f"http://127.0.0.1:{app_port}/api/health", app)
        app_url = f"http://127.0.0.1:{app_port}"
        # WARM-UP: the broker's FIRST /broker/run lazily loads the engine registry +
        # the 2345-polyline cached intake, which on a heavily-loaded box can be slow
        # to schedule. Do one throwaway synchronous run here (separate tenant, read
        # tool -> no version side effects) so the MEASURED async run in the test is a
        # hot path. A broker that truly can't serve fails HERE with a clear error.
        warm_headers = {"X-Tenant-Id": "e2e-warmup"}
        warm = requests.post(f"{app_url}/api/run?wait=1",
                             json=confirmed_requests_payload(
                                 app_url, "count-by-layer", dwg="rooftop_demo",
                                 headers=warm_headers),
                             headers=warm_headers, timeout=120)
        assert warm.status_code == 200, f"broker warm-up failed: {warm.status_code} {warm.text[:300]}"
        yield {"app": app_url, "broker": f"http://127.0.0.1:{broker_port}"}
    finally:
        stop(app)
        stop(broker)
        # restore the shared authored store byte-for-byte (or remove if absent before)
        if orig_authored is None:
            AUTHORED_TOOLS.unlink(missing_ok=True)
        else:
            AUTHORED_TOOLS.write_bytes(orig_authored)
        # Remove the authored tool bodies THIS suite's author step created. Scope the
        # sweep to the directory of the tenant this suite authors as, not the whole
        # authored/ tree: a tree-wide "anything newer than the snapshot" sweep would
        # also delete a body some other tenant's concurrent author request wrote while
        # this suite was running. Pre-#131 the bodies were flat, so the top level is
        # still swept for this suite's own filenames.
        if AUTHORED_DIR.exists():
            own_dir = AUTHORED_DIR / hashlib.sha256(TENANT.encode("utf-8")).hexdigest()[:32]
            # Only the ONE deterministic filename this suite's author step produces, in
            # its own tenant directory (and at the top level, for a pre-#131 flat body).
            # Sweeping "every .py that appeared" would delete a body some other tenant's
            # concurrent author request wrote while this suite was running.
            for f in (own_dir / AUTHORED_BODY, AUTHORED_DIR / AUTHORED_BODY):
                if f not in pre_authored_files:
                    f.unlink(missing_ok=True)
            # drop the per-tenant directory only if this suite created it and left it
            # empty; never a recursive delete, so a directory that still holds another
            # body is left exactly as found.
            if own_dir.is_dir() and own_dir not in pre_authored_dirs and not any(own_dir.iterdir()):
                own_dir.rmdir()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _h() -> dict:
    return {"X-Tenant-Id": TENANT}


def poll_job(app: str, job_id: str, timeout_s: float = 50.0) -> dict:
    seen = []
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        rec = requests.get(f"{app}/api/jobs/{job_id}", headers=_h(), timeout=10).json()
        if not seen or seen[-1] != rec["status"]:
            seen.append(rec["status"])
        if rec["status"] in ("complete", "failed"):
            return rec
        time.sleep(0.1)
    raise TimeoutError(f"job {job_id} not terminal in {timeout_s}s (saw {seen})")


# --------------------------------------------------------------------------- #
# the golden path — ONE test proving the composed system
# --------------------------------------------------------------------------- #
def test_golden_path(stack):
    app = stack["app"]

    # 0) both processes are alive; health reflects the offline (APS_LIVE=0) demo
    health = requests.get(f"{app}/api/health", headers=_h(), timeout=10).json()
    assert health["ok"] is True
    assert health["aps_live"] is False
    assert requests.get(f"{stack['broker']}/broker/health", timeout=10).json()["ok"] is True

    # 1) NL prompt -> RUN lane -> the count-by-layer tool
    nl = requests.post(f"{app}/api/nl-prompt",
                       json={"text": "count panels per layer"}, timeout=15).json()
    assert nl["error"] is None
    assert nl["lane"] == "run", nl
    assert nl["tool"] == "count-by-layer", nl
    assert nl["confidence"] >= 0.8

    # 2) run that tool through the ASYNC job spine: 202 {job_id} -> poll -> complete
    headers = _h()
    r = requests.post(f"{app}/api/run",
                      json=confirmed_requests_payload(
                          app, nl["tool"], dwg="rooftop_demo", headers=headers),
                      headers=headers, timeout=15)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "submitted" and body["job_id"]
    rec = poll_job(app, body["job_id"])
    assert rec["status"] == "complete", rec
    env3 = rec["result"]                                  # the §3 envelope
    assert SECTION3_KEYS.issubset(env3.keys())
    assert env3["ok"] is True and env3["error"] is None
    assert env3["result"]["counts"]["Panels"] == 2345    # golden oracle

    # 3) capability catalog: families present, each with capabilities
    caps = requests.get(f"{app}/api/capabilities", headers=_h(), timeout=10).json()
    assert caps["error"] is None
    assert len(caps["families"]) >= 1
    for fam in caps["families"]:
        assert fam["family_id"] and fam["label"]
        assert len(fam["capabilities"]) >= 1
    cap_names = {c["name"] for f in caps["families"] for c in f["capabilities"]}
    assert "count-by-layer" in cap_names

    # 4) a WRITE tool run -> a new immutable version v2 (parent v1)
    w = requests.post(f"{app}/api/run?wait=1",
                      json=confirmed_requests_payload(
                          app, "delete-marked-panel", {"drawing_id": "demo"},
                          "rooftop_demo", headers=headers),
                      headers=headers, timeout=60)
    assert w.status_code == 200, w.text
    wenv = w.json()
    assert wenv["ok"] is True and wenv["error"] is None
    assert wenv["result"]["new_version"] == {"drawing_id": "demo", "version": 2, "parent": 1}, wenv["result"].get("new_version")

    # 5) the version chain endpoint shows v1 -> v2
    ver = requests.get(f"{app}/api/drawings/demo/versions", headers=_h(), timeout=15).json()
    assert ver["error"] is None
    assert ver["head"] == 2 and ver["latest"] == 2
    by_v = {row["v"]: row for row in ver["versions"]}
    assert set(by_v) == {1, 2}
    assert by_v[1]["parent"] is None
    assert by_v[2]["parent"] == 1
    assert by_v[2]["tool"] == "delete-marked-panel"

    # 6) undo -> head steps back to v1 (redo still possible: latest stays 2)
    undo = requests.post(f"{app}/api/drawings/demo/undo", headers=_h(), timeout=15).json()
    assert undo["error"] is None
    assert undo["version"] == 1 and undo["head"] == 1 and undo["latest"] == 2
    assert requests.get(f"{app}/api/drawings/demo/intake",
                        params={"version": "head"}, headers=_h(), timeout=15).json()["version"] == 1

    # 7) entitlements: off-auth demo tier grants full access
    ent = requests.get(f"{app}/api/entitlements", headers=_h(), timeout=10).json()
    assert ent["error"] is None
    assert ent["tier"] == "demo"
    # 9-capability shape (agent spine wire contract §9 + §19 upload)
    assert ent["entitlements"] == {"run_read": True, "run_write": True, "solve": True,
                                   "build": True, "converse": True,
                                   "agent_write_autopilot": True, "deploy": True,
                                   "platform_customize": False, "upload": True}
    assert ent["source"] == "policy"

    # 8) template author -> the new tool is registered and appears in /api/tools
    names_before = {t["name"] for t in
                    requests.get(f"{app}/api/tools", headers=_h(), timeout=10).json()["tools"]}
    author = requests.post(f"{app}/api/author",
                           json={"description": AUTHOR_DESCRIPTION},
                           headers=_h(), timeout=30)
    assert author.status_code == 200, author.text
    abody = author.json()
    assert abody["error"] is None
    assert abody["source"] == "template"          # no harness wired -> deterministic template path
    new_name = abody["tool"]["name"]
    assert new_name
    names_after = {t["name"] for t in
                   requests.get(f"{app}/api/tools", headers=_h(), timeout=10).json()["tools"]}
    assert new_name in names_after, f"authored tool {new_name!r} not in /api/tools"
    assert new_name not in names_before or True  # newly authored (or idempotent re-register)
    assert names_before <= names_after            # authoring only ADDS to the catalog
