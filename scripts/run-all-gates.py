#!/usr/bin/env python3
"""
Leaf web demo — full test-orchestration gate runner (CI-ready).

WHY THIS EXISTS
---------------
The suites in this repo CANNOT share one pytest process. They cross-contaminate:

  * auth suites toggle LEAF_AUTH_LIVE (a process-global env flag);
  * write / job / drawing suites share on-disk state (jobs.db, the versioned
    drawing store, authored_tools.json, broker ledger/tenants);
  * every suite boots its own broker+app on ephemeral ports — combined, their
    module-scoped fixtures collide;
  * the repo contains a `platform/` package that SHADOWS the stdlib `platform`
    module whenever the repo root lands on sys.path, so the platform suite has to
    run from a DIFFERENT cwd with its own shadow-defusing conftest.

So this runner launches EACH suite in its OWN subprocess, with the correct cwd
and a cleaned env, captures pass/fail + counts, and prints ONE scoreboard.

Special handling, all documented on the scoreboard:
  * `authored_tools.json` is RESET to {"tools": []} before the nl_router suite
    (gitignored runtime pollution otherwise makes NL routing flaky). The prior
    file is backed up into the log dir first.
  * the platform suite needs a reachable Postgres (DATABASE_URL, or
    platform/.env.local). If it is unreachable the suite is SKIPPED with a
    reason rather than reported red.
  * harness `npm test` + `npx tsc --noEmit` + `npx tsc -p tsconfig.build.json`
    are included.
  * the containerized harness smoke (census #13) is OPT-IN: it builds + boots
    the compose stack, so it runs only with LEAF_CONTAINER_SMOKE=1 and SKIPs
    (with reason) otherwise, or when Docker is unavailable (script exit 3).

USAGE
-----
    python scripts/run-all-gates.py              # run all, continue past failures (default)
    python scripts/run-all-gates.py --fail-fast  # stop at the first failing gate
    python scripts/run-all-gates.py --continue    # explicit default (run everything)
    python scripts/run-all-gates.py --only server # substring filter on suite ids
    python scripts/run-all-gates.py --log-dir DIR # where per-suite logs land

EXIT CODE
---------
    0  iff every non-skipped gate passed
    1  otherwise

Full per-suite output goes to <log-dir>/<suite>.log; only the scoreboard is
printed to stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO = SCRIPTS_DIR.parent               # .../leaf-web-demo
REPO_PARENT = REPO.parent               # cwd the platform suite runs from (e.g. C:/tmp)
SERVER = REPO / "server"
DA = REPO / "da"
HARNESS = REPO / "harness"
AUTHORED_TOOLS = SERVER / "authored_tools.json"

# Defensive: never let the repo root shadow the stdlib `platform` module inside
# THIS process. The runner only shells out, so it needs no repo imports.
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != REPO]

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(s: str) -> str:
    return _ANSI.sub("", s)


# --------------------------------------------------------------------------- #
# suite model
# --------------------------------------------------------------------------- #
@dataclass
class Suite:
    id: str                 # short id (scoreboard row + log filename)
    label: str              # human label
    kind: str               # pytest | vitest | tsc
    cwd: Path
    argv: List[str]         # command argv (python -m pytest ... / npm ... / npx ...)
    expected: Optional[int] # expected test count (None for tsc: pass/fail only)
    reset_authored: bool = False   # reset authored_tools.json before this suite
    db_gated: bool = False         # SKIP unless the platform DB is reachable
    opt_in_env: str = ""           # SKIP unless this env flag is truthy (opt-in suite)
    timeout_s: int = 900           # per-attempt subprocess timeout


@dataclass
class Result:
    suite: Suite
    status: str             # PASS | FAIL | SKIP
    got: str                # got count (or "-"/"skip")
    seconds: float
    note: str = ""
    log_path: Optional[Path] = None
    counts: dict = field(default_factory=dict)


def _py_pytest(target: str) -> List[str]:
    return [sys.executable, "-m", "pytest", target, "-q", "--color=no",
            "-p", "no:cacheprovider"]


def _npm() -> str:
    return shutil.which("npm") or shutil.which("npm.cmd") or "npm"


def _npx() -> str:
    return shutil.which("npx") or shutil.which("npx.cmd") or "npx"


def build_suites() -> List[Suite]:
    repo_name = REPO.name  # "leaf-web-demo"
    suites: List[Suite] = [
        # --- server/ (cwd=server): each file is its OWN pytest process --- #
        Suite("server-backbone", "server tests/test_backbone.py", "pytest", SERVER,
              _py_pytest("tests/test_backbone.py"), 10),
        Suite("server-auth", "server test_auth.py", "pytest", SERVER,
              _py_pytest("test_auth.py"), 11),
        Suite("server-dynamic-loader", "server test_dynamic_loader.py", "pytest", SERVER,
              _py_pytest("test_dynamic_loader.py"), 4),
        Suite("server-write-loop", "server tests/test_write_loop.py", "pytest", SERVER,
              _py_pytest("tests/test_write_loop.py"), 8),
        Suite("server-nl-router", "server tests/test_nl_router.py", "pytest", SERVER,
              _py_pytest("tests/test_nl_router.py"), 17, reset_authored=True),
        Suite("server-ui-wave", "server tests/test_ui_wave.py", "pytest", SERVER,
              _py_pytest("tests/test_ui_wave.py"), 9),
        Suite("server-wave2", "server tests/test_wave2.py", "pytest", SERVER,
              _py_pytest("tests/test_wave2.py"), 11),
        Suite("server-wave3", "server tests/test_wave3.py", "pytest", SERVER,
              _py_pytest("tests/test_wave3.py"), 19),
        Suite("server-wave4", "server tests/test_wave4.py", "pytest", SERVER,
              _py_pytest("tests/test_wave4.py"), 9),
        Suite("server-wave5", "server tests/test_wave5.py", "pytest", SERVER,
              _py_pytest("tests/test_wave5.py"), 14),
        Suite("server-microvm", "server tests/test_hardening_2c_microvm.py", "pytest", SERVER,
              _py_pytest("tests/test_hardening_2c_microvm.py"), 11),
        Suite("server-broker-tenant-state", "server tests/test_broker_tenant_state.py", "pytest",
              SERVER, _py_pytest("tests/test_broker_tenant_state.py"), 11),
        # main's site-demo lane shipped WITHOUT a gate entry, so it only ever ran
        # by hand — same gap this branch closed for its own suites.
        Suite("server-site", "server tests/test_site.py", "pytest", SERVER,
              _py_pytest("tests/test_site.py"), 8),
        # --- conversational agent spine (CONTRACT-ADDENDUM section 18) --- #
        # Separate suites for the same reason as the waves above: the gate/ledger
        # suites share on-disk approval + audit state and the router suites toggle
        # dispatch-secret env, so one pytest process cross-contaminates them.
        Suite("server-agent-policy", "server tests/test_agent_policy.py", "pytest", SERVER,
              _py_pytest("tests/test_agent_policy.py"), 22),
        Suite("server-agent-gate", "server tests/test_agent_gate.py", "pytest", SERVER,
              _py_pytest("tests/test_agent_gate.py"), 30),
        Suite("server-agent-router", "server tests/test_agent_router.py", "pytest", SERVER,
              _py_pytest("tests/test_agent_router.py"), 23),
        Suite("server-sessions-router", "server tests/test_sessions_router.py", "pytest", SERVER,
              _py_pytest("tests/test_sessions_router.py"), 25),
        Suite("server-context-packet", "server tests/test_context_packet.py", "pytest", SERVER,
              _py_pytest("tests/test_context_packet.py"), 16),
        Suite("server-contract-freeze", "server tests/test_contract_freeze.py", "pytest", SERVER,
              _py_pytest("tests/test_contract_freeze.py"), 8),
        Suite("server-auth-vocab-freeze", "server tests/test_auth_vocab_freeze.py", "pytest",
              SERVER, _py_pytest("tests/test_auth_vocab_freeze.py"), 8),
        Suite("server-billing-tiers", "server tests/test_billing_tiers.py", "pytest", SERVER,
              _py_pytest("tests/test_billing_tiers.py"), 30),
        Suite("server-job-lanes", "server tests/test_job_lanes.py", "pytest", SERVER,
              _py_pytest("tests/test_job_lanes.py"), 11),
        Suite("server-agent-e2e", "server tests/test_agent_e2e.py", "pytest", SERVER,
              _py_pytest("tests/test_agent_e2e.py"), 4),
        # --- guest drawing uploads (CONTRACT-ADDENDUM section 19) --- #
        # One process per file, same isolation reasons as everything above:
        # these toggle LEAF_AUTH_LIVE / LEAF_GUEST_* env and share the guest
        # store + uploads staging dirs (isolated per-test via tmp_path).
        Suite("server-guest-uploads", "server tests/test_guest_uploads.py", "pytest", SERVER,
              _py_pytest("tests/test_guest_uploads.py"), 33),
        Suite("server-guest-fail-closed", "server tests/test_guest_fail_closed.py", "pytest",
              SERVER, _py_pytest("tests/test_guest_fail_closed.py"), 11),
        Suite("server-guest-purge", "server tests/test_guest_purge.py", "pytest", SERVER,
              _py_pytest("tests/test_guest_purge.py"), 9),
        Suite("server-guest-session-auth", "server tests/test_guest_session_auth.py", "pytest",
              SERVER, _py_pytest("tests/test_guest_session_auth.py"), 14),
        Suite("server-broker-upload-resolver", "server tests/test_broker_upload_resolver.py",
              "pytest", SERVER, _py_pytest("tests/test_broker_upload_resolver.py"), 22),
        # --- the golden-path composed e2e (this runner's sibling deliverable) --- #
        Suite("server-e2e-golden", "server tests/test_e2e_golden.py", "pytest", SERVER,
              _py_pytest("tests/test_e2e_golden.py"), 1),
        # --- registration sweep 2026-07-22 (census #17: suites shipped without --- #
        # --- gate entries; every count below measured one-process-per-file)   --- #
        Suite("server-catalog-version-pin", "server tests/test_catalog_and_version_pin.py",
              "pytest", SERVER, _py_pytest("tests/test_catalog_and_version_pin.py"), 21),
        Suite("server-live-lsp-resolution", "server tests/test_live_lsp_resolution.py",
              "pytest", SERVER, _py_pytest("tests/test_live_lsp_resolution.py"), 2),
        Suite("server-job-dwg-version", "server tests/test_job_dwg_version_persist.py",
              "pytest", SERVER, _py_pytest("tests/test_job_dwg_version_persist.py"), 5),
        Suite("server-canonical-worker", "server tests/test_canonical_worker.py", "pytest",
              SERVER, _py_pytest("tests/test_canonical_worker.py"), 10),
        Suite("server-marathon-orchestration", "server tests/test_marathon_orchestration.py",
              "pytest", SERVER, _py_pytest("tests/test_marathon_orchestration.py"), 15),
        Suite("server-adapter-inverter", "server tests/test_inverter_placement_adapter.py",
              "pytest", SERVER, _py_pytest("tests/test_inverter_placement_adapter.py"), 3),
        Suite("server-adapter-combiner", "server tests/test_combiner_placement_adapter.py",
              "pytest", SERVER, _py_pytest("tests/test_combiner_placement_adapter.py"), 3),
        Suite("server-adapter-autofill", "server tests/test_autofill_adapter.py", "pytest",
              SERVER, _py_pytest("tests/test_autofill_adapter.py"), 2),
        Suite("server-agent-approvals", "server tests/test_agent_approvals.py", "pytest",
              SERVER, _py_pytest("tests/test_agent_approvals.py"), 10),
        Suite("server-approval-consume", "server tests/test_approval_consume.py", "pytest",
              SERVER, _py_pytest("tests/test_approval_consume.py"), 13),
        Suite("server-drawings-bootstrap", "server tests/test_drawings_bootstrap.py", "pytest",
              SERVER, _py_pytest("tests/test_drawings_bootstrap.py"), 16),
        Suite("server-entitlements", "server tests/test_entitlements.py", "pytest", SERVER,
              _py_pytest("tests/test_entitlements.py"), 26),
        Suite("server-policy-unavailable-paths", "server tests/test_policy_unavailable_paths.py",
              "pytest", SERVER, _py_pytest("tests/test_policy_unavailable_paths.py"), 2),
        Suite("server-entitlements-converse", "server tests/test_entitlements_converse.py",
              "pytest", SERVER, _py_pytest("tests/test_entitlements_converse.py"), 6),
        Suite("server-hardening-1c", "server tests/test_hardening_1c.py", "pytest", SERVER,
              _py_pytest("tests/test_hardening_1c.py"), 12),
        Suite("server-hardening-1f", "server test_hardening_1f.py", "pytest", SERVER,
              _py_pytest("test_hardening_1f.py"), 8),
        Suite("server-hardening-2b", "server tests/test_hardening_2b.py", "pytest", SERVER,
              _py_pytest("tests/test_hardening_2b.py"), 13),
        Suite("server-hardening-3b", "server tests/test_hardening_3b.py", "pytest", SERVER,
              _py_pytest("tests/test_hardening_3b.py"), 9),
        Suite("server-hardening-quota", "server tests/test_hardening_quota.py", "pytest",
              SERVER, _py_pytest("tests/test_hardening_quota.py"), 11),
        Suite("server-quota-shape", "server tests/test_quota_shape.py", "pytest", SERVER,
              _py_pytest("tests/test_quota_shape.py"), 12),
        Suite("server-session-store", "server tests/test_session_store.py", "pytest", SERVER,
              _py_pytest("tests/test_session_store.py"), 19),
        Suite("server-sessions-routes", "server tests/test_sessions_routes.py", "pytest",
              SERVER, _py_pytest("tests/test_sessions_routes.py"), 33),
        Suite("server-turn-runner", "server tests/test_turn_runner.py", "pytest", SERVER,
              _py_pytest("tests/test_turn_runner.py"), 12),
        # g1a canonical e2e self-skips without a reachable Postgres; gate it the
        # same way as the platform suite so the skip is visible, not silent.
        Suite("server-g1a-canonical-e2e", "server tests/test_g1a_canonical_e2e.py", "pytest",
              SERVER, _py_pytest("tests/test_g1a_canonical_e2e.py"), 1, db_gated=True),
        # NOT registered (red at measurement 2026-07-22, one process per file,
        # pre-existing on main). Durable tracker with the fix-then-register
        # rule: https://github.com/Evan-Haug/leaf-web-demo/issues/29
        # tests/test_capabilities_promotion.py (4F/7P),
        # tests/test_engine_registry_scripts.py (1F/3P),
        # tests/test_sessions_e2e.py (7 errors/2P).
        # --- broker keystone (census #4, 2026-07-22): test_broker_boundary's --- #
        # one red was a stale pre-§19 assertion (offline `dwg` no longer
        # ignored) — fixed and registered per the #29 fix-then-register rule.
        # The no-da-imports static invariant + §8 ledger-line schema freeze
        # gates ride the same lane.
        Suite("server-broker-boundary", "server tests/test_broker_boundary.py", "pytest",
              SERVER, _py_pytest("tests/test_broker_boundary.py"), 43),
        Suite("server-no-da-imports", "server tests/test_no_da_imports_static.py", "pytest",
              SERVER, _py_pytest("tests/test_no_da_imports_static.py"), 8),
        Suite("server-broker-ledger-schema", "server tests/test_broker_ledger_schema_static.py",
              "pytest", SERVER, _py_pytest("tests/test_broker_ledger_schema_static.py"), 9),
        Suite("server-broker-ledger-runtime", "server tests/test_broker_ledger_schema_runtime.py",
              "pytest", SERVER, _py_pytest("tests/test_broker_ledger_schema_runtime.py"), 6),
        # --- da/ (cwd=da) --- #
        Suite("da-store", "da test_store.py", "pytest", DA,
              _py_pytest("test_store.py"), 14),
        Suite("da-multitenant", "da test_multitenant.py", "pytest", DA,
              _py_pytest("test_multitenant.py"), 5),
        # --- platform (cwd=repo parent; DB-gated) --- #
        # Expected 118 = the full DB-configured collection, measured on this
        # tree 2026-07-22 via `DATABASE_URL=... pytest --collect-only -q
        # platform/tests` (the conftest ignore-hook only prunes when NO
        # DATABASE_URL is set, so with a DB every module collects, not just the
        # *_static.py proofs).
        Suite("platform", "platform/tests (Postgres)", "pytest", REPO_PARENT,
              _py_pytest(f"{repo_name}/platform/tests"), 145, db_gated=True),
        # Dependency-free *_static proofs must run even with NO Postgres: the
        # conftest's pytest_ignore_collect exempts them, so this un-gated suite
        # keeps them in the gate on a clean checkout. Explicit file targets, not
        # the dir, so the collected count (26) is invariant to DB presence.
        Suite("platform-static", "platform/tests *_static (no DB)", "pytest", REPO_PARENT,
              _py_pytest(f"{repo_name}/platform/tests/test_ledger_static.py")
              + [f"{repo_name}/platform/tests/test_hashing_static.py",
                 f"{repo_name}/platform/tests/test_replay_static.py"], 26),
        # The committed replay fixture is dependency-free and catches hash or
        # replay drift before a PR reaches the GitHub simulator-gate workflow.
        Suite("platform-simgate-self-test", "platform simulator-gate self-test", "script",
              REPO_PARENT,
              [sys.executable, f"{repo_name}/platform/simgate/run.py", "--self-test"], None),
        # --- harness (cwd=harness) --- #
        Suite("harness-vitest", "harness npm test (vitest)", "vitest", HARNESS,
              [_npm(), "test"], 63),
        Suite("harness-tsc-noemit", "harness npx tsc --noEmit", "tsc", HARNESS,
              [_npx(), "tsc", "--noEmit"], None),
        Suite("harness-tsc-build", "harness npx tsc -p tsconfig.build.json", "tsc", HARNESS,
              [_npx(), "tsc", "-p", "tsconfig.build.json"], None),
        # --- containerized harness smoke (census #13) — OPT-IN --- #
        # Builds + boots the real compose stack (broker+harness+app, mock agent)
        # and proves the authed app->harness hop, durable grant/tenant volumes,
        # and secret-free logs. Needs Docker + compose >= 2.24 and several minutes
        # on a cold image cache, so it only runs when LEAF_CONTAINER_SMOKE=1.
        Suite("harness-container-smoke", "harness container smoke (compose)", "script",
              REPO, [sys.executable, "scripts/harness-container-smoke.py"], None,
              opt_in_env="LEAF_CONTAINER_SMOKE", timeout_s=1800),
    ]
    return suites


# --------------------------------------------------------------------------- #
# env hygiene: strip cross-contaminating toggles so each suite sees defaults
# --------------------------------------------------------------------------- #
_ENV_DENYLIST = (
    "LEAF_AUTH_LIVE", "APS_LIVE", "APS_CRED", "JOBS_DB", "SESSIONS_DB", "JOB_MAX_S",
    "LEAF_STORE_DIR", "LEAF_ENTITLEMENTS_FILE", "LEAF_AUTHOR_HARNESS_URL",
    "LEAF_CONVERSE_HARNESS_URL",
    "LEAF_AUTHOR_LLM", "LEAF_TENANTS_DIR", "LEAF_TENANT_REPO", "BROKER_URL",
    "BROKER_LEDGER", "BROKER_TENANTS",
    "LEAF_SANDBOX", "LEAF_SANDBOX_TIMEOUT_S", "LEAF_E2B_HELPER", "E2B_API_KEY",
    # harness F5 caller-auth + agent-mock toggles: ambient values would 401 (or
    # fake out) the hermetic harness suites, which pin these per-test instead.
    "LEAF_HARNESS_AUTH", "LEAF_HARNESS_SECRET", "LEAF_AGENT_MOCK", "LEAF_GRANT_STORE",
)


def clean_env() -> dict:
    env = dict(os.environ)
    for k in _ENV_DENYLIST:
        env.pop(k, None)
    env["PY_COLORS"] = "0"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


# --------------------------------------------------------------------------- #
# platform DB reachability probe (runs from REPO_PARENT to dodge the shadow)
# --------------------------------------------------------------------------- #
_PROBE = r"""
import sys
from pathlib import Path
dsn = None
import os
dsn = os.environ.get("DATABASE_URL")
if not dsn:
    envf = Path(sys.argv[1])
    if envf.exists():
        for line in envf.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                dsn = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
if not dsn:
    print("NO_DSN"); sys.exit(3)
try:
    import psycopg
    with psycopg.connect(dsn, connect_timeout=8) as c:
        with c.cursor() as cur:
            cur.execute("select 1"); cur.fetchone()
    print("REACHABLE"); sys.exit(0)
except Exception as e:
    print("UNREACHABLE: %s: %s" % (type(e).__name__, str(e)[:160])); sys.exit(1)
"""


def _dsn_from_env_local() -> str:
    """DATABASE_URL from platform/.env.local ('' when absent) — the SAME file,
    key, and quoting rules the probe applies, so probe verdict and injected DSN
    can never disagree."""
    envf = REPO / "platform" / ".env.local"
    if not envf.exists():
        return ""
    for line in envf.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def probe_platform_db() -> tuple[bool, str, str]:
    """(reachable, message, dsn). The dsn is returned so db_gated suites can
    receive it as DATABASE_URL: the probe accepts a file-only DSN
    (platform/.env.local), but suites that gate themselves on the ENV VAR
    (server/tests/test_g1a_canonical_e2e.py's skipif) would then silently skip
    inside a green suite — probe REACHABLE must mean the suite actually runs."""
    envf = REPO / "platform" / ".env.local"
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE, str(envf)],
            cwd=str(REPO_PARENT), env=clean_env(),
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False, "probe timed out (>30s)", ""
    out = (proc.stdout + proc.stderr).strip().splitlines()
    msg = out[-1] if out else f"probe exit {proc.returncode}"
    dsn = os.environ.get("DATABASE_URL") or _dsn_from_env_local()
    return proc.returncode == 0, msg, dsn


# --------------------------------------------------------------------------- #
# count parsing
# --------------------------------------------------------------------------- #
def _n(word: str, text: str) -> int:
    m = re.findall(rf"(\d+) {word}", text)
    return int(m[-1]) if m else 0


def parse_pytest(text: str) -> dict:
    t = strip_ansi(text)
    # isolate the pytest summary line (last line carrying a count keyword)
    summary = ""
    for line in reversed(t.splitlines()):
        if re.search(r"\d+ (passed|failed|error|skipped|deselected|xfailed|xpassed)", line):
            summary = line
            break
    if not summary:
        if "no tests ran" in t:
            return {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "got": 0}
        summary = t
    passed = _n("passed", summary)
    failed = _n("failed", summary)
    errors = _n("error", summary)   # matches "error" and "errors"
    skipped = _n("skipped", summary)
    xfailed = _n("xfailed", summary)
    xpassed = _n("xpassed", summary)
    got = passed + failed + errors + skipped + xfailed + xpassed
    return {"passed": passed, "failed": failed, "errors": errors,
            "skipped": skipped, "got": got}


def parse_vitest(text: str) -> dict:
    t = strip_ansi(text)
    line = ""
    for ln in t.splitlines():
        s = ln.strip()
        if s.startswith("Tests ") and ("passed" in s or "failed" in s):
            line = s
    if not line:
        # fall back to any line with "N passed" near "Tests"
        line = t
    passed = _n("passed", line)
    failed = _n("failed", line)
    skipped = _n("skipped", line)
    got = passed + failed + skipped
    return {"passed": passed, "failed": failed, "skipped": skipped, "got": got}


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #
def reset_authored_tools(log_dir: Path) -> str:
    """Reset server/authored_tools.json to a clean {"tools": []}, backing up the
    prior content into the log dir first. Returns a human note."""
    backup = log_dir / "authored_tools.json.bak"
    if AUTHORED_TOOLS.exists():
        backup.write_bytes(AUTHORED_TOOLS.read_bytes())
        note = f"reset authored_tools.json (backup: {backup})"
    else:
        note = "authored_tools.json absent; wrote clean {\"tools\": []}"
    AUTHORED_TOOLS.write_text('{"tools": []}\n', encoding="utf-8")
    return note


def run_suite(suite: Suite, log_dir: Path, attempt: int = 1) -> Result:
    log_path = log_dir / (f"{suite.id}.log" if attempt == 1
                          else f"{suite.id}.retry{attempt - 1}.log")

    # DB-gated suites: probe first, SKIP-with-reason when unreachable. On
    # REACHABLE, inject the resolved DSN as DATABASE_URL for the child: the
    # probe accepts a file-only DSN (platform/.env.local), and a suite that
    # skipif-gates on the env var must RUN in that case, not green-skip.
    db_env: dict = {}
    if suite.db_gated:
        ok, msg, dsn = probe_platform_db()
        if not ok:
            return Result(suite, "SKIP", "skip", 0.0,
                          note=f"platform DB unreachable ({msg})", log_path=None)
        if dsn:
            db_env["DATABASE_URL"] = dsn

    # Opt-in suites: SKIP-with-reason unless their env flag is truthy.
    if suite.opt_in_env and os.environ.get(suite.opt_in_env, "").strip().lower() \
            not in ("1", "true", "yes", "on"):
        return Result(suite, "SKIP", "skip", 0.0,
                      note=f"opt-in: set {suite.opt_in_env}=1 (needs Docker)",
                      log_path=None)

    pre_note = ""
    if suite.reset_authored:
        pre_note = reset_authored_tools(log_dir)

    t0 = time.perf_counter()
    with open(log_path, "w", encoding="utf-8", errors="replace") as logf:
        logf.write(f"$ (cwd={suite.cwd})\n$ {' '.join(str(a) for a in suite.argv)}\n\n")
        logf.flush()
        try:
            proc = subprocess.run(
                [str(a) for a in suite.argv],
                cwd=str(suite.cwd), env={**clean_env(), **db_env},
                capture_output=True, text=True, timeout=suite.timeout_s,
                # text=True without an explicit encoding decodes with the system
                # ANSI codepage (cp1252 here), and vitest/tsc emit UTF-8 box and
                # quote glyphs. A byte outside cp1252 killed the reader thread,
                # left proc.stdout as None, and crashed the whole runner mid-gate
                # — the acceptance instrument itself failing on output encoding.
                encoding="utf-8", errors="replace",
            )
            out = (proc.stdout or "") + "\n" + (proc.stderr or "")
            rc = proc.returncode
        except subprocess.TimeoutExpired as exc:
            out = ((exc.stdout or "") + "\n" + (exc.stderr or "")
                   + f"\n[TIMEOUT >{suite.timeout_s}s]")
            rc = 124
        logf.write(out)
    seconds = time.perf_counter() - t0

    if suite.kind == "pytest":
        c = parse_pytest(out)
        passed = rc == 0 and c["failed"] == 0 and c["errors"] == 0
        note = pre_note
        if c["skipped"]:
            note = (note + " " if note else "") + f"{c['skipped']} skipped"
        # Expected counts are a FLOOR: fewer tests than registered means the
        # suite silently lost coverage (deselected file, import skip, renamed
        # module) even when everything that ran was green. Growth is fine and
        # only noted.
        if suite.expected is not None and passed and c["got"] < suite.expected:
            passed = False
            note = (note + " " if note else "") + \
                f"count regression: expected >= {suite.expected}, got {c['got']}"
        elif suite.expected is not None and c["got"] > suite.expected and passed:
            note = (note + " " if note else "") + f"(count drift: expected {suite.expected})"
        return Result(suite, "PASS" if passed else "FAIL", str(c["got"]), seconds,
                      note=note.strip(), log_path=log_path, counts=c)

    if suite.kind == "vitest":
        c = parse_vitest(out)
        passed = rc == 0 and c["failed"] == 0
        note = f"{c['skipped']} skipped" if c.get("skipped") else ""
        # Same floor rule as pytest suites.
        if suite.expected is not None and passed and c["got"] < suite.expected:
            passed = False
            note = (note + " " if note else "") + \
                f"count regression: expected >= {suite.expected}, got {c['got']}"
        return Result(suite, "PASS" if passed else "FAIL", str(c["got"]), seconds,
                      note=note.strip(), log_path=log_path, counts=c)

    if suite.kind == "script":
        # standalone smoke script: exit 0 PASS; exit 3 = environment unavailable
        # (e.g. no Docker) -> SKIP with reason; anything else FAIL.
        if rc == 3:
            last = next((ln for ln in out.strip().splitlines() if ln.strip()), "")
            return Result(suite, "SKIP", "skip", seconds,
                          note=last[:120] or "environment unavailable",
                          log_path=log_path, counts={})
        return Result(suite, "PASS" if rc == 0 else "FAIL",
                      "ok" if rc == 0 else "err", seconds,
                      note=("" if rc == 0 else f"exit {rc}"),
                      log_path=log_path, counts={})

    # tsc: pass/fail on exit code only
    passed = rc == 0
    return Result(suite, "PASS" if passed else "FAIL",
                  "ok" if passed else "err", seconds,
                  note=("" if passed else f"tsc exit {rc}"),
                  log_path=log_path, counts={})


# --------------------------------------------------------------------------- #
# scoreboard
# --------------------------------------------------------------------------- #
def print_scoreboard(results: List[Result], log_dir: Path, wall: float) -> None:
    rows = []
    for r in results:
        exp = "-" if r.suite.expected is None else str(r.suite.expected)
        rows.append((r.suite.label, exp, r.got, r.status, f"{r.seconds:5.1f}", r.note))

    headers = ("SUITE", "EXP", "GOT", "RESULT", "SECS", "NOTE")
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    widths[0] = min(widths[0], 40)

    def fmt(row) -> str:
        cells = []
        for i, cell in enumerate(row):
            cell = str(cell)
            if i in (1, 2, 3, 4):
                cells.append(cell.rjust(widths[i]))
            else:
                cells.append(cell.ljust(widths[i]))
        return "  ".join(cells).rstrip()

    line = "-" * (sum(widths) + 2 * (len(headers) - 1))
    print()
    print("=" * len(line))
    print("  LEAF WEB DEMO -- GATE SCOREBOARD")
    print("=" * len(line))
    print(fmt(headers))
    print(line)
    for row in rows:
        print(fmt(row))
    print(line)

    npass = sum(1 for r in results if r.status == "PASS")
    nfail = sum(1 for r in results if r.status == "FAIL")
    nskip = sum(1 for r in results if r.status == "SKIP")
    total_tests = sum(r.counts.get("passed", 0) for r in results)
    print(f"  suites: {npass} PASS  {nfail} FAIL  {nskip} SKIP   "
          f"| test cases passed: {total_tests}   | wall: {wall:.1f}s")
    print(f"  logs:   {log_dir}")
    print("=" * len(line))


def main() -> int:
    ap = argparse.ArgumentParser(description="Leaf web demo full gate runner")
    ap.add_argument("--fail-fast", action="store_true",
                    help="stop at the first failing gate (default: run all)")
    ap.add_argument("--continue", dest="cont", action="store_true",
                    help="run every gate even if one fails (this is the default)")
    ap.add_argument("--only", default=None,
                    help="only run suites whose id contains this substring")
    ap.add_argument("--retry", type=int, default=1,
                    help="re-run a FAILED suite up to N more times before calling it "
                         "red (default 1). These suites boot real servers and can flake "
                         "under load; a flake that passes on retry is annotated, not "
                         "masked. Use --retry 0 to capture raw first-attempt results.")
    ap.add_argument("--log-dir", default=None,
                    help="directory for per-suite logs (default: C:/tmp/leaf-web-demo-gates)")
    args = ap.parse_args()

    default_logroot = Path("C:/tmp") if Path("C:/tmp").exists() else Path.cwd()
    log_dir = Path(args.log_dir) if args.log_dir else (default_logroot / "leaf-web-demo-gates")
    log_dir.mkdir(parents=True, exist_ok=True)

    suites = build_suites()
    if args.only:
        suites = [s for s in suites if args.only in s.id]
        if not suites:
            print(f"no suites match --only {args.only!r}")
            return 2

    print(f"leaf-web-demo gate runner -- {len(suites)} suites, "
          f"separate processes, logs -> {log_dir}")

    # Preserve the operator's authored_tools.json: the nl-router gate resets it to
    # clean (gitignored runtime pollution otherwise flakes NL routing), but we
    # restore the original after the whole run so nothing is destroyed as a side
    # effect. Captured only when a suite will actually touch it.
    will_reset = any(s.reset_authored for s in suites)
    orig_authored = (AUTHORED_TOOLS.read_bytes()
                     if (will_reset and AUTHORED_TOOLS.exists()) else None)
    authored_existed = AUTHORED_TOOLS.exists()

    results: List[Result] = []
    wall0 = time.perf_counter()
    try:
      for suite in suites:
        print(f"  ... {suite.id:<22} ", end="", flush=True)
        res = run_suite(suite, log_dir, attempt=1)
        attempts = 1
        # Retry a FAILED integration suite (these boot real servers -> load flakes).
        while (res.status == "FAIL" and attempts <= args.retry):
            attempts += 1
            prev_secs = res.seconds
            res = run_suite(suite, log_dir, attempt=attempts)
            res.seconds += prev_secs  # cumulative time spent on this row
            if res.status == "PASS":
                res.note = (f"flaked; passed on attempt {attempts}/{args.retry + 1}"
                            + (f" ({res.note})" if res.note else ""))
        if res.status == "FAIL" and attempts > 1:
            res.note = (f"FAIL after {attempts} attempts"
                        + (f" ({res.note})" if res.note else ""))
        results.append(res)
        tail = f"{res.got:>4}  {res.seconds:5.1f}s"
        if res.note:
            tail += f"  {res.note}"
        print(f"{res.status:<4} {tail}")
        if res.status == "FAIL" and args.fail_fast:
            print(f"  --fail-fast: stopping after {suite.id}")
            break
    finally:
        # Restore authored_tools.json to the operator's pre-run content (or remove
        # it if it did not exist before) so the gate run leaves no side effect.
        if orig_authored is not None:
            AUTHORED_TOOLS.write_bytes(orig_authored)
        elif will_reset and not authored_existed:
            AUTHORED_TOOLS.unlink(missing_ok=True)

    wall = time.perf_counter() - wall0
    print_scoreboard(results, log_dir, wall)

    # EXIT 0 iff every non-skipped gate passed.
    any_fail = any(r.status == "FAIL" for r in results)
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
