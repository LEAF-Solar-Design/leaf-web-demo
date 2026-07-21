#!/usr/bin/env python3
"""
start-leaf.py — one command that boots the Leaf platform for LOCAL DEV.

    python scripts/start-leaf.py                 # broker + app + web (mock APS)
    python scripts/start-leaf.py --with-harness  # + the real Agent-SDK Build sidecar
    python scripts/start-leaf.py --no-web         # backend only (broker + app)

What it starts (all at APS_LIVE=0 — no cloud, no secrets):

    broker   127.0.0.1:8140   sole APS-credential holder, ledger, kill-switch  (server/broker.py)
    app      127.0.0.1:8130   tenant-facing FastAPI: jobs spine, catalog,      (server/app.py)
                              write loop, NL router, usage/ops/tenant surfaces
    web      127.0.0.1:5175   Vite/React/three.js dev server (VITE_MOCK=0 ->    (web/)
                              talks to the live app)
    harness  127.0.0.1:8150   (optional) real Agent-SDK author loop, Build lane (harness/dist/scripts/serve.js)

If a default port is already taken (a stale squatter from a previous run is a
real gotcha), the next free port is chosen automatically and every dependent URL
is re-wired to match. Readiness is confirmed by hitting each service's health
endpoint; the URLs are printed once the stack is green. Ctrl-C tears the whole
tree down — no orphaned python/node left on your ports.

Pure standard library. Launch children with THIS interpreter (sys.executable) so
the app/broker run on the same Python that has fastapi/uvicorn installed.

Contract sources: server/CONTRACT-ADDENDUM.md (ports, envs), server/app.py:109,
server/broker.py (uvicorn bind), harness/scripts/serve.ts (HARNESS_PORT),
web/vite.config.js (5175), deploy/README.md (env table).
"""
from __future__ import annotations

import argparse
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---- Layout -----------------------------------------------------------------
REPO = Path(__file__).resolve().parent.parent
SERVER_DIR = REPO / "server"
WEB_DIR = REPO / "web"
HARNESS_DIR = REPO / "harness"
HARNESS_ENTRY = HARNESS_DIR / "dist" / "scripts" / "serve.js"

IS_WIN = os.name == "nt"

# ---- Defaults (env-overridable, mirrored from the contract) -----------------
DEFAULT_BROKER_PORT = 8140   # server/broker.py  (BROKER_PORT)
DEFAULT_APP_PORT = 8130      # server/app.py     (APP_PORT)
DEFAULT_HARNESS_PORT = 8150  # harness serve.ts  (HARNESS_PORT)
DEFAULT_WEB_PORT = 5175      # web/vite.config.js


# ---- Small terminal helpers -------------------------------------------------
def say(msg: str) -> None:
    print(f"[start-leaf] {msg}", flush=True)


def is_port_free(port: str | int) -> bool:
    """A port is free if we can bind 127.0.0.1:<port> right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", int(port)))
            return True
        except OSError:
            return False


def pick_port(name: str, preferred: int, taken: set[int]) -> int:
    """Return the preferred port if free, else the next free one (skipping ports
    already claimed for this run). Warn loudly when we move off the default so a
    stale squatter never silently breaks the wiring."""
    if preferred not in taken and is_port_free(preferred):
        taken.add(preferred)
        return preferred
    say(f"WARNING: {name} default port {preferred} is busy (stale squatter?) — finding a free one")
    for candidate in range(preferred + 1, preferred + 200):
        if candidate not in taken and is_port_free(candidate):
            taken.add(candidate)
            say(f"         {name} -> {candidate}")
            return candidate
    raise SystemExit(f"could not find a free port for {name} near {preferred}")


def http_ok(url: str, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 500  # any real HTTP answer means it's up
    except urllib.error.HTTPError as exc:
        return 200 <= exc.code < 500  # a 4xx still proves the server is listening
    except Exception:
        return False


def wait_healthy(name: str, url: str, deadline_s: float = 40.0) -> bool:
    """Poll a health URL until it answers or we time out."""
    start = time.time()
    while time.time() - start < deadline_s:
        if http_ok(url):
            say(f"OK   {name:<8} healthy  ({url})")
            return True
        time.sleep(0.5)
    say(f"WARN {name:<8} did not report healthy within {int(deadline_s)}s ({url})")
    return False


# ---- Child-process management ----------------------------------------------
class Child:
    def __init__(self, name: str, proc: subprocess.Popen):
        self.name = name
        self.proc = proc


_children: list[Child] = []

# Windows Job Object (kill-on-close) — the bulletproof orphan guard. Every child
# is assigned to this job; when the launcher exits FOR ANY REASON (clean Ctrl-C,
# a crash, or a hard kill that skips our Python cleanup), Windows terminates the
# whole job tree. This is what guarantees "no orphaned python/node on your ports".
_win_job = None


def _make_win_job():
    """Create a kill-on-close Job Object. Returns a HANDLE or None on any failure."""
    global _win_job
    if not IS_WIN:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                ctypes.c_void_p, ctypes.c_uint32]
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        ok = k32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info))  # 9 = ExtendedLimitInformation
        if not ok:
            return None
        _win_job = (k32, job)
        say("orphan guard armed (Windows job object, kill-on-close)")
        return _win_job
    except Exception as exc:  # ctypes/OS edge — fall back to taskkill-only
        say(f"note: job-object guard unavailable ({exc}); relying on taskkill cleanup")
        return None


def _assign_to_job(proc: subprocess.Popen) -> None:
    if not _win_job:
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32, job = _win_job
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.AssignProcessToJobObject(job, int(proc._handle))
    except Exception:
        pass  # graceful taskkill still covers cleanup


def spawn(name: str, cmd: list[str], cwd: Path, env: dict) -> Child:
    say(f"start {name}: {' '.join(str(c) for c in cmd)}  (cwd={cwd.name})")
    creationflags = 0
    if IS_WIN:
        # New process group so a console Ctrl-C does NOT race us to the children;
        # we orchestrate the teardown ourselves via taskkill /T.
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    kwargs: dict = {"cwd": str(cwd), "env": env, "creationflags": creationflags}
    if not IS_WIN:
        kwargs["start_new_session"] = True  # own process group for killpg
    proc = subprocess.Popen([str(c) for c in cmd], **kwargs)
    _assign_to_job(proc)  # Windows kill-on-close job guard (no-op elsewhere)
    child = Child(name, proc)
    _children.append(child)
    return child


def kill_child(child: Child) -> None:
    proc = child.proc
    if proc.poll() is not None:
        return
    try:
        if IS_WIN:
            # /T kills the whole tree (npm -> node -> vite; node -> git worker).
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def cleanup() -> None:
    if not _children:
        return
    say("shutting down (Ctrl-C) - stopping child processes ...")
    for child in reversed(_children):
        kill_child(child)
    # Brief confirm — kept short so the whole teardown fits inside Windows'
    # console-control grace window (the job-object guard is the backstop).
    deadline = time.time() + 2.5
    for child in reversed(_children):
        while child.proc.poll() is None and time.time() < deadline:
            time.sleep(0.1)
        state = "stopped" if child.proc.poll() is not None else "signaled"
        say(f"  {child.name:<8} {state}")
    say("done.")


# ---- Node / npm resolution --------------------------------------------------
def resolve_exe(name: str) -> str | None:
    """shutil.which finds the .cmd shim on Windows (npm.cmd) and the bare exe elsewhere."""
    return shutil.which(name)


# ---- Main -------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Boot the Leaf platform for local dev (broker + app + web, APS_LIVE=0).",
    )
    ap.add_argument("--no-web", action="store_true", help="skip the Vite web dev server (backend only)")
    ap.add_argument("--with-harness", action="store_true",
                    help="also boot the compiled Agent-SDK harness sidecar (Build lane)")
    ap.add_argument("--broker-port", type=int, default=int(os.environ.get("BROKER_PORT", DEFAULT_BROKER_PORT)))
    ap.add_argument("--app-port", type=int, default=int(os.environ.get("APP_PORT", DEFAULT_APP_PORT)))
    ap.add_argument("--harness-port", type=int, default=int(os.environ.get("HARNESS_PORT", DEFAULT_HARNESS_PORT)))
    ap.add_argument("--web-port", type=int, default=DEFAULT_WEB_PORT)
    args = ap.parse_args()

    # Sanity: the two backend entrypoints must exist.
    for entry in (SERVER_DIR / "app.py", SERVER_DIR / "broker.py"):
        if not entry.exists():
            say(f"ERROR: missing {entry} — run this from inside the leaf-web-demo repo.")
            return 2

    want_web = not args.no_web
    want_harness = args.with_harness

    if want_harness and not HARNESS_ENTRY.exists():
        say(f"ERROR: --with-harness needs the compiled sidecar at {HARNESS_ENTRY}")
        say("       build it first:  cd harness && npm install && npm run build")
        return 2

    npm = resolve_exe("npm") if want_web else None
    if want_web and not npm:
        say("WARNING: npm not found on PATH — skipping the web dev server (backend still boots).")
        want_web = False

    # ---- Allocate ports up front so every dependent URL is wired correctly ---
    taken: set[int] = set()
    broker_port = pick_port("broker", args.broker_port, taken)
    app_port = pick_port("app", args.app_port, taken)
    harness_port = pick_port("harness", args.harness_port, taken) if want_harness else None
    web_port = pick_port("web", args.web_port, taken) if want_web else None

    broker_url = f"http://127.0.0.1:{broker_port}"
    app_url = f"http://127.0.0.1:{app_port}"
    harness_url = f"http://127.0.0.1:{harness_port}" if harness_port else None
    web_url = f"http://127.0.0.1:{web_port}" if web_port else None

    base_env = os.environ.copy()
    base_env["APS_LIVE"] = "0"  # local dev is always mock — no cloud, no secrets

    # Converse back-edge secret (wire contract §0): the harness authenticates to
    # the app with X-Dispatch-Secret. Unset in EITHER child => /internal/agent/gate
    # 401s and every spine tool is denied while all four services report healthy —
    # a silent dead end. Mint one ephemeral value per boot and give it to BOTH
    # children so a clean environment works; an operator-set secret always wins.
    dispatch_secret = os.environ.get("LEAF_APP_DISPATCH_SECRET", "").strip()
    ephemeral_dispatch = want_harness and not dispatch_secret
    if ephemeral_dispatch:
        dispatch_secret = secrets.token_urlsafe(32)

    _make_win_job()  # arm the kill-on-close guard before any child is spawned

    say("=" * 70)
    say("Leaf platform — local dev boot (APS_LIVE=0, no secrets)")
    say("=" * 70)

    # ---- 1) broker (sole credential holder; binds 127.0.0.1) -----------------
    broker_env = dict(base_env, BROKER_PORT=str(broker_port))
    spawn("broker", [sys.executable, "broker.py"], SERVER_DIR, broker_env)

    # ---- 2) harness sidecar (optional Build lane) ----------------------------
    if want_harness:
        node = resolve_exe("node") or "node"
        harness_env = dict(
            base_env,
            HARNESS_PORT=str(harness_port),
            BROKER_URL=broker_url,
            LEAF_REPO_ROOT=str(REPO),
            # Converse back-edge: the harness must call the SAME app this launcher
            # boots even when the app moved off its default port (stale squatter).
            LEAF_APP_URL=app_url,
            LEAF_APP_DISPATCH_SECRET=dispatch_secret,
        )
        spawn("harness", [node, str(HARNESS_ENTRY)], HARNESS_DIR, harness_env)

    # ---- 3) app (talks to broker over HTTP; never holds the APS credential) --
    app_env = dict(base_env, APP_PORT=str(app_port), BROKER_URL=broker_url)
    if harness_url:
        app_env["LEAF_AUTHOR_HARNESS_URL"] = harness_url
        app_env["LEAF_CONVERSE_HARNESS_URL"] = harness_url
        app_env["LEAF_APP_DISPATCH_SECRET"] = dispatch_secret
    spawn("app", [sys.executable, "app.py"], SERVER_DIR, app_env)

    # ---- 4) web (Vite dev; VITE_MOCK=0 so the browser hits the live app) -----
    if want_web:
        web_env = dict(base_env, VITE_MOCK="0", VITE_API_BASE=app_url)
        # Bind Vite to IPv4 explicitly: its default `localhost` resolves to IPv6
        # (::1) on Windows, so a 127.0.0.1 health check (and the URL we print)
        # would miss it. Same IPv4/IPv6 trap deploy/README.md flags for nginx.
        spawn(
            "web",
            [npm, "run", "dev", "--", "--host", "127.0.0.1", "--port", str(web_port), "--strictPort"],
            WEB_DIR, web_env,
        )

    # ---- Readiness (health endpoints) ---------------------------------------
    say("-" * 70)
    say("waiting for services to report healthy ...")
    wait_healthy("broker", f"{broker_url}/broker/health")
    if want_harness:
        wait_healthy("harness", f"{harness_url}/health")
    wait_healthy("app", f"{app_url}/api/health")
    if want_web:
        wait_healthy("web", f"{web_url}/")

    # ---- Summary banner ------------------------------------------------------
    say("=" * 70)
    say("Leaf is up. Open:")
    if want_web:
        say(f"    Web UI     {web_url}     <- open this")
    say(f"    App API    {app_url}/api/health")
    say(f"    Broker     {broker_url}/broker/health   (internal; app talks to it)")
    if want_harness:
        say(f"    Harness    {harness_url}/health          (Build lane sidecar)")
        if ephemeral_dispatch:
            say("    Converse back edge ENABLED with an ephemeral dev secret")
            say("          (regenerated every boot; set LEAF_APP_DISPATCH_SECRET to pin it).")
        else:
            say("    Converse back edge ENABLED with the LEAF_APP_DISPATCH_SECRET from your env.")
        say("    NOTE: authoring a tool needs a linked Claude grant per tenant")
        say("          (POST /api/tenant/claude-grant); without it /api/author")
        say("          returns a calm GRANT_REQUIRED (401) — no LLM credit spent.")
    say("")
    say("Everything runs at APS_LIVE=0 (pure-python results from real geometry).")
    say("Press Ctrl-C to stop the whole stack.")
    say("=" * 70)

    # ---- Graceful-stop signals -----------------------------------------------
    # Route every stop signal to a flag the monitor loop polls (short sleeps), so
    # teardown starts within ~0.2s regardless of platform signal-delivery quirks:
    #   Ctrl-C (SIGINT / CTRL_C_EVENT), CTRL_BREAK_EVENT (SIGBREAK, Windows),
    #   and SIGTERM (POSIX). The job-object guard is the backstop if we're killed
    #   before cleanup runs at all.
    stop = {"requested": False}

    def _request_stop(_signum=None, _frame=None):
        stop["requested"] = True

    signal.signal(signal.SIGINT, _request_stop)
    if IS_WIN:
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, _request_stop)
    else:
        signal.signal(signal.SIGTERM, _request_stop)

    # ---- Monitor loop: block until a stop signal, watching for a core crash --
    core = {"broker", "app"}
    rc = 0
    try:
        while not stop["requested"]:
            time.sleep(0.2)
            for child in list(_children):
                if child.proc.poll() is not None:
                    code = child.proc.returncode
                    if child.name in core:
                        say(f"ERROR: {child.name} exited unexpectedly (code {code}) - tearing down.")
                        rc = 1
                        stop["requested"] = True
                        break
                    say(f"NOTE: {child.name} exited (code {code}); leaving the rest running.")
                    _children.remove(child)
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
