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
  * `web-demo-gate` shells out to dispatch/run-local-ci.sh's demo-gate bucket,
    the adapter for web/'s golden-path oracles (this runner has no other web/
    test entry point). It needs a POSIX bash; see _bash().
  * the containerized harness smoke (census #13) is OPT-IN: it builds + boots
    the compose stack, so it runs only with LEAF_CONTAINER_SMOKE=1 and SKIPs
    (with reason) otherwise, or when Docker is unavailable (script exit 3).
  * a child that fails to SPAWN (OSError) or dies with no output is a FAIL row
    with an explicit note and retries like any red suite — never a runner
    crash that loses the scoreboard. Drill it with
    LEAF_GATE_FAULT_INJECT="<suite-id>:spawn" (first attempt only; see
    scripts/test_gate_runner.py, registered as gate-runner-selftest).
  * `--only` is REPEATABLE and unions its matches, a substring that matches no
    suite exits 2 rather than silently shrinking the run, and the scoreboard
    echoes the exact selection that produced it — so the printed result can be
    checked against the command that was typed.
  * a suite id registered twice exits 2 before anything runs. The id is the
    handle `--only` matches and the name of the child's log file, so a
    duplicate runs one test file twice and can answer to two different
    expected floors at once — a scoreboard that cannot say which floor the
    gate stands on.

USAGE
-----
    python scripts/run-all-gates.py              # run all, continue past failures (default)
    python scripts/run-all-gates.py --fail-fast  # stop at the first failing gate
    python scripts/run-all-gates.py --continue    # explicit default (run everything)
    python scripts/run-all-gates.py --only server # substring filter on suite ids
    python scripts/run-all-gates.py --only server-backbone --only harness-vitest
                                                 # repeatable: runs the UNION of both
    python scripts/run-all-gates.py --log-dir DIR # where per-suite logs land

EXIT CODE
---------
    0  iff every gate passed and every test-level skip was explicitly allowlisted
    2  nothing ran, so this is never a gate verdict: an --only substring matched
       no suite, or a suite id was registered more than once
    1  otherwise

Full per-suite output goes to <log-dir>/<suite>.log; only the scoreboard is
printed to stdout.

Pytest allowlists pin the exact reported reason. Vitest allowlists pin the
exact test file and skipped count because Vitest's default reporter does not
emit per-test skip reasons.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
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
WEB = REPO / "web"
AUTHORED_TOOLS = SERVER / "authored_tools.json"

# Defensive: never let the repo root shadow the stdlib `platform` module inside
# THIS process. The runner only shells out, so it needs no repo imports.
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != REPO]

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_PARKED_AGENT_ROUTER_REASON = re.escape(
    "PARKED at the 2026-07-21 merge resolution (spine x sessions-wire): "
    "this exercises a section-18 surface replaced by the section-2.1 lane "
    "(approvals resolve against session_store; site.py serves the reviewed "
    "builtin-only catalog + canned artifact). Restore at spine unification."
)


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
    expected: Optional[int] # minimum executed test count (None for pass/fail-only)
    # Pytest skips fail the gate unless their reported reason matches one of
    # these regular expressions. This keeps environmental exceptions explicit
    # instead of letting any skipped assertion count as a passing test.
    allowed_skip_reasons: tuple[str, ...] = ()
    # Vitest does not report skip reasons. Pin the exact skipped file and count
    # for each deliberate environment-gated integration group instead.
    allowed_vitest_skips: tuple[tuple[str, int], ...] = ()
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
            "-r", "s", "-p", "no:cacheprovider"]


def _npm() -> str:
    if os.name == "nt":
        return shutil.which("npm.cmd") or shutil.which("npm") or "npm.cmd"
    return shutil.which("npm") or "npm"


def _npx() -> str:
    if os.name == "nt":
        return shutil.which("npx.cmd") or shutil.which("npx") or "npx.cmd"
    return shutil.which("npx") or "npx"


# Standard Git for Windows install paths — the only bash on a Windows box that
# sees this repo at its Windows path.
_GIT_BASH_WIN = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
)


def _is_wsl_launcher(path: str) -> bool:
    r"""True for %SystemRoot%\System32\bash.exe — the WSL launcher, not a shell.

    It runs a Linux distro with a /mnt/c view of the tree, so a script started
    through it reports `uname -sm` as "Linux x86_64" and cannot execute the
    Windows node_modules bin shims. dispatch/run-local-ci.sh branches on exactly
    those two signals: it would conclude the deps are unusable and unpack
    web/vendor/node_modules-linux-x64.tar.gz OVER the Windows web/node_modules
    that the web-build / web-customization-check / web-staging-fixes-check
    suites need — one suite silently breaking three others.
    """
    try:
        system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        return Path(path).resolve().parent == system32.resolve()
    except OSError:
        return False


def _bash() -> str:
    """POSIX bash for `script` suites whose argv is a shell script.

    On Windows, shutil.which("bash") resolves to the System32 WSL launcher on
    any machine with WSL installed, so prefer Git Bash and never fall through to
    that shim: with no usable bash, return the canonical Git Bash path so the
    suite fails to spawn with a FAIL row naming the missing binary rather than
    running somewhere that corrupts the other suites' dependencies.
    """
    if os.name != "nt":
        return shutil.which("bash") or "bash"
    for candidate in _GIT_BASH_WIN:
        if Path(candidate).is_file():
            return candidate
    found = shutil.which("bash")
    return found if found and not _is_wsl_launcher(found) else _GIT_BASH_WIN[0]


def normalize_spawn_command(
    argv: List[str], *, os_name: Optional[str] = None,
    command_interpreter: Optional[str] = None,
) -> tuple[List[str] | str, bool, Optional[str]]:
    """Route Windows command shims through cmd.exe with Windows quoting."""
    normalized = [str(arg) for arg in argv]
    platform_name = os.name if os_name is None else os_name
    if platform_name != "nt" or Path(normalized[0]).suffix.lower() not in (".cmd", ".bat"):
        return normalized, False, None
    interpreter = command_interpreter or os.environ.get("COMSPEC") or "cmd.exe"
    return subprocess.list2cmdline(normalized), True, interpreter


def build_suites() -> List[Suite]:
    repo_name = REPO.name  # "leaf-web-demo"
    suites: List[Suite] = [
        # Executed-count floors: seventeen were re-baselined on 2026-07-25
        # because they sat BELOW their suite's real executed count. A low floor
        # is legal, coverage_verdict PASSes it with an "(executed-count drift:
        # ...)" note, which is exactly the hazard: such a suite can silently
        # lose tests down to the floor and still report green. Every
        # replacement is a MEASURED executed count (passed, skips excluded),
        # taken one file per pytest subprocess from the suite's registered cwd
        # and re-confirmed through this runner at --retry 0.
        # Two were deliberately left alone, each for a reason recorded at its
        # own Suite(...) below: `platform` (re-baselining it needs a live
        # Postgres, and the test-gate workflow is hermetic) and
        # `server-customization-adversarial` (Linux and Windows execute
        # different counts, so no single number is honest for both).
        # `platform` HAS since been re-baselined: 199 -> 223 (an interim bump of
        # exactly what one new file added) -> 234 -> 235, the last two each a
        # measured green count on a pristine database.
        # `server-customization-adversarial` is now the only
        # floor still pinned BELOW its CI executed count -- 17 is the Windows
        # count, Linux CI executes 19 and reports drift -- because no single
        # number is honest for both. Details at each Suite(...) below.
        # --- server/ (cwd=server): each file is its OWN pytest process --- #
        Suite("server-backbone", "server tests/test_backbone.py", "pytest", SERVER,
              _py_pytest("tests/test_backbone.py"), 15),
        Suite("server-dependency-health", "server tests/test_dependency_health.py", "pytest",
              SERVER, _py_pytest("tests/test_dependency_health.py"), 17),
        Suite("server-auth", "server test_auth.py", "pytest", SERVER,
              _py_pytest("test_auth.py"), 11),
        Suite("server-auth-envelope", "server tests/test_auth_envelope.py", "pytest", SERVER,
              _py_pytest("tests/test_auth_envelope.py"), 7),
        Suite("server-dynamic-loader", "server test_dynamic_loader.py", "pytest", SERVER,
              _py_pytest("test_dynamic_loader.py"), 4),
        Suite("server-write-loop", "server tests/test_write_loop.py", "pytest", SERVER,
              _py_pytest("tests/test_write_loop.py"), 21),
        Suite("server-nl-router", "server tests/test_nl_router.py", "pytest", SERVER,
              _py_pytest("tests/test_nl_router.py"), 18, reset_authored=True),
        Suite("server-ui-wave", "server tests/test_ui_wave.py", "pytest", SERVER,
              _py_pytest("tests/test_ui_wave.py"), 9),
        Suite("server-wave2", "server tests/test_wave2.py", "pytest", SERVER,
              _py_pytest("tests/test_wave2.py"), 6,
              allowed_skip_reasons=(r"platform DB unreachable: .+",)),
        # Floor 13 is the count that EXECUTES on the CI runner. It is NOT the
        # collected count and NOT the local count. 19 tests collect in both
        # environments (19 `def test_` functions, zero parametrize). CI run
        # 30161760318 reported `got 19, 6 skipped`, so CI executes 13. This dev
        # host executes 18 with 1 skip (`platform DB unreachable: DATABASE_URL
        # is not set`), measured 2026-07-25 from server/ via `python -m pytest
        # tests/test_wave3.py -q --color=no -r s -p no:cacheprovider`.
        # The 5-test gap is INFERRED, not confirmed: CI's per-test skip list is
        # not in the job log. Exactly 5 tests carry @requires_tenant_repo, whose
        # repo (default C:/tmp/leaf-tenants/demo-tenant) is present here and
        # absent on a clean runner, which accounts for 1 DB + 5 tenant = 6.
        # `expected` is a MIN across environments because the gate must pass
        # where it runs, and CI is the binding environment. PR #178 raised this
        # to the local 18 and CI red-failed: `executed-count regression:
        # expected >= 18, got 13`. A local `(executed-count drift: expected 13)`
        # note is this host overshooting the CI floor and is correct, not stale.
        # server-wave2 above uses the same convention: pinned 6, CI executes 6.
        Suite("server-wave3", "server tests/test_wave3.py", "pytest", SERVER,
              _py_pytest("tests/test_wave3.py"), 13,
              allowed_skip_reasons=(
                  r"platform DB unreachable: .+",
                  r"tenant tool repo absent at .+ \(set LEAF_TENANT_REPO_SRC to one\)",
              )),
        Suite("server-wave4", "server tests/test_wave4.py", "pytest", SERVER,
              _py_pytest("tests/test_wave4.py"), 9),
        Suite("server-wave5", "server tests/test_wave5.py", "pytest", SERVER,
              _py_pytest("tests/test_wave5.py"), 15),
        Suite("server-grant-admin-authority", "server tests/test_grant_admin_authority.py",
              "pytest", SERVER, _py_pytest("tests/test_grant_admin_authority.py"), 9),
        Suite("server-microvm", "server tests/test_hardening_2c_microvm.py", "pytest", SERVER,
              _py_pytest("tests/test_hardening_2c_microvm.py"), 14),
        Suite("server-broker-tenant-state", "server tests/test_broker_tenant_state.py", "pytest",
              SERVER, _py_pytest("tests/test_broker_tenant_state.py"), 12),
        # main's site-demo lane shipped WITHOUT a gate entry, so it only ever ran
        # by hand — same gap this branch closed for its own suites.
        Suite("server-site", "server tests/test_site.py", "pytest", SERVER,
              _py_pytest("tests/test_site.py"), 12),
        # --- conversational agent spine (CONTRACT-ADDENDUM section 18) --- #
        # Separate suites for the same reason as the waves above: the gate/ledger
        # suites share on-disk approval + audit state and the router suites toggle
        # dispatch-secret env, so one pytest process cross-contaminates them.
        Suite("server-agent-policy", "server tests/test_agent_policy.py", "pytest", SERVER,
              _py_pytest("tests/test_agent_policy.py"), 33),
        Suite("server-agent-gate", "server tests/test_agent_gate.py", "pytest", SERVER,
              _py_pytest("tests/test_agent_gate.py"), 55),
        # W14 admin self-edit lane (R7): branch-only platform-repo writes,
        # fundamental-path co-sign, landing handoff. Own process: it builds
        # real git repos and toggles the R7 rollout env.
        Suite("server-platform-customize", "server tests/test_platform_customize.py",
              "pytest", SERVER, _py_pytest("tests/test_platform_customize.py"), 41),
        Suite("server-agent-router", "server tests/test_agent_router.py", "pytest", SERVER,
              # Four section-18 tests are intentionally parked because the
              # section-2.1 lane replaced that surface. Pin the complete reason
              # and require all 26 active tests, so any different or additional
              # skip fails the gate and this debt cannot grow silently.
              _py_pytest("tests/test_agent_router.py"), 26,
              allowed_skip_reasons=(_PARKED_AGENT_ROUTER_REASON,)),
        Suite("server-sessions-router", "server tests/test_sessions_router.py", "pytest", SERVER,
              _py_pytest("tests/test_sessions_router.py"), 45),
        Suite("server-context-packet", "server tests/test_context_packet.py", "pytest", SERVER,
              _py_pytest("tests/test_context_packet.py"), 16),
        Suite("server-contract-freeze", "server tests/test_contract_freeze.py", "pytest", SERVER,
              _py_pytest("tests/test_contract_freeze.py"), 8),
        Suite("server-auth-vocab-freeze", "server tests/test_auth_vocab_freeze.py", "pytest",
              SERVER, _py_pytest("tests/test_auth_vocab_freeze.py"), 11),
        Suite("server-billing-tiers", "server tests/test_billing_tiers.py", "pytest", SERVER,
              _py_pytest("tests/test_billing_tiers.py"), 30),
        # The per-tenant DAILY AUTHORING cap. Shipped without a gate entry, the
        # same gap the site-demo lane above had: only the WEB half of this
        # feature (web-author-quota-gate) was registered, so every server-side
        # test of the cap only ever ran by hand. The cap is a precondition for
        # stranger-facing authoring, so it does not get to be unenforced in CI.
        Suite("server-author-quota", "server tests/test_author_quota.py", "pytest", SERVER,
              _py_pytest("tests/test_author_quota.py"), 57),
        Suite("server-job-lanes", "server tests/test_job_lanes.py", "pytest", SERVER,
              _py_pytest("tests/test_job_lanes.py"), 12),
        # One process per file (below) is exactly why the cross-file connection
        # leak this pins was invisible to CI: every file passed alone.
        Suite("server-jobs-connection-ownership",
              "server tests/test_jobs_connection_ownership.py", "pytest", SERVER,
              _py_pytest("tests/test_jobs_connection_ownership.py"), 12),
        Suite("server-agent-e2e", "server tests/test_agent_e2e.py", "pytest", SERVER,
              _py_pytest("tests/test_agent_e2e.py"), 4),
        # --- guest drawing uploads (CONTRACT-ADDENDUM section 19) --- #
        # One process per file, same isolation reasons as everything above:
        # these toggle LEAF_AUTH_LIVE / LEAF_GUEST_* env and share the guest
        # store + uploads staging dirs (isolated per-test via tmp_path).
        Suite("server-guest-uploads", "server tests/test_guest_uploads.py", "pytest", SERVER,
              _py_pytest("tests/test_guest_uploads.py"), 57),
        # The cross-process fence probe uses POSIX fcntl and therefore skips on
        # Windows operator boxes. Linux CI executes it. Keep the Windows run
        # honest with the exact measured floor for every portable test and an
        # allowlist for only that named deployment-contract skip.
        Suite("server-guest-fail-closed", "server tests/test_guest_fail_closed.py", "pytest",
              SERVER, _py_pytest("tests/test_guest_fail_closed.py"), 36,
              allowed_skip_reasons=(r"fcntl is a Linux deployment contract",)),
        Suite("server-guest-purge", "server tests/test_guest_purge.py", "pytest", SERVER,
              _py_pytest("tests/test_guest_purge.py"), 13),
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
              "pytest", SERVER, _py_pytest("tests/test_job_dwg_version_persist.py"), 6),
        Suite("server-job-migration-concurrent", "server tests/test_job_migration_concurrent.py",
              "pytest", SERVER, _py_pytest("tests/test_job_migration_concurrent.py"), 1),
        Suite("server-job-migration-thread-race", "server tests/test_job_migration_thread_race.py",
              "pytest", SERVER, _py_pytest("tests/test_job_migration_thread_race.py"), 1),
        Suite("server-jobs-reaper-start-race", "server tests/test_jobs_reaper_start_race.py",
              "pytest", SERVER, _py_pytest("tests/test_jobs_reaper_start_race.py"), 2),
        Suite("server-canonical-worker", "server tests/test_canonical_worker.py", "pytest",
              SERVER, _py_pytest("tests/test_canonical_worker.py"), 24),
        Suite("server-marathon-orchestration", "server tests/test_marathon_orchestration.py",
              "pytest", SERVER, _py_pytest("tests/test_marathon_orchestration.py"), 17),
        Suite("server-adapter-inverter", "server tests/test_inverter_placement_adapter.py",
              "pytest", SERVER, _py_pytest("tests/test_inverter_placement_adapter.py"), 1,
              allowed_skip_reasons=(
                  r"aws-inverter-placement source or its runtime deps \(numpy\) unavailable",
              )),
        Suite("server-adapter-combiner", "server tests/test_combiner_placement_adapter.py",
              "pytest", SERVER, _py_pytest("tests/test_combiner_placement_adapter.py"), 1,
              allowed_skip_reasons=(
                  r"aws-combiner-placement checkout is unavailable; adapter needs the real solver source",
              )),
        Suite("server-adapter-autofill", "server tests/test_autofill_adapter.py", "pytest",
              SERVER, _py_pytest("tests/test_autofill_adapter.py"), 13,
              allowed_skip_reasons=(
                  r"autofill-solver source absent, acknowledged via LEAF_AUTOFILL_SOLVER_ABSENT_OK=1",
              )),
        Suite("server-agent-approvals", "server tests/test_agent_approvals.py", "pytest",
              SERVER, _py_pytest("tests/test_agent_approvals.py"), 19),
        Suite("server-approval-consume", "server tests/test_approval_consume.py", "pytest",
              SERVER, _py_pytest("tests/test_approval_consume.py"), 20),
        Suite("server-drawings-bootstrap", "server tests/test_drawings_bootstrap.py", "pytest",
              SERVER, _py_pytest("tests/test_drawings_bootstrap.py"), 18),
        # NOT db_gated on purpose: this file's authority-selector and legacy-contract
        # tests need no database, and its DB-only tests skip themselves via
        # @requires_database. Gating the whole suite would hide the un-gated half on
        # a clean checkout. Four structural tests execute without a database; the
        # remaining race tests are named skips until DATABASE_URL is supplied.
        Suite("server-drawing-authority-postgres",
              "server tests/test_drawing_upload_authority_postgres.py", "pytest",
              SERVER, _py_pytest("tests/test_drawing_upload_authority_postgres.py"), 4,
              allowed_skip_reasons=(
                  r"PostgreSQL race test requires explicit DATABASE_URL",)),
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
              _py_pytest("tests/test_hardening_2b.py"), 15),
        # (test_hardening_2c_microvm.py is registered above as "server-microvm";
        # it was listed twice, running the same 14 tests for no added coverage.)
        Suite("server-hardening-3b", "server tests/test_hardening_3b.py", "pytest", SERVER,
              _py_pytest("tests/test_hardening_3b.py"), 14),
        # The opaque checkout capability's own unit acceptance. Separate from the
        # HTTP suites because the SUBJECT binding only exists with auth live, and
        # those suites run against the LEAF_AUTH_LIVE=0 header stub.
        Suite("server-checkout-capability", "server tests/test_checkout_capability.py",
              "pytest", SERVER, _py_pytest("tests/test_checkout_capability.py"), 29),
        # The legacy checkout's CROSS-PROCESS acceptance. Spawns real OS
        # processes, so it is the only suite that can catch the ownership bypass
        # a per-process threading lock cannot see. No skipif: `fcntl` is the
        # Linux path and `msvcrt` the Windows one, and both must execute.
        Suite("server-checkout-crossproc", "server tests/test_checkout_crossproc.py",
              "pytest", SERVER, _py_pytest("tests/test_checkout_crossproc.py"), 41),
        Suite("server-hardening-quota", "server tests/test_hardening_quota.py", "pytest",
              SERVER, _py_pytest("tests/test_hardening_quota.py"), 11),
        Suite("server-quota-shape", "server tests/test_quota_shape.py", "pytest", SERVER,
              _py_pytest("tests/test_quota_shape.py"), 12),
        Suite("server-session-store", "server tests/test_session_store.py", "pytest", SERVER,
              _py_pytest("tests/test_session_store.py"), 20),
        Suite("server-sessions-routes", "server tests/test_sessions_routes.py", "pytest",
              SERVER, _py_pytest("tests/test_sessions_routes.py"), 41),
        Suite("server-turn-runner", "server tests/test_turn_runner.py", "pytest", SERVER,
              _py_pytest("tests/test_turn_runner.py"), 24),
        # g1a canonical e2e self-skips without a reachable Postgres; gate it the
        # same way as the platform suite so the skip is visible, not silent.
        Suite("server-g1a-canonical-e2e", "server tests/test_g1a_canonical_e2e.py", "pytest",
              SERVER, _py_pytest("tests/test_g1a_canonical_e2e.py"), 1, db_gated=True),
        Suite("server-engine-registry-scripts", "server tests/test_engine_registry_scripts.py",
              "pytest", SERVER, _py_pytest("tests/test_engine_registry_scripts.py"), 5),
        # issue #29 red-suite registry (https://github.com/Evan-Haug/leaf-web-demo/issues/29):
        # all six now fixed-then-registered. test_sessions_e2e's measured "7 errors"
        # were purely its module `harness` fixture failing `npm run build` in a
        # worktree with no harness node_modules — NOT a code defect; it passes 9/9
        # once the harness is installed (the same harness the harness-vitest/tsc
        # suites require). It self-builds the harness on setup (~14s).
        Suite("server-sessions-e2e", "server tests/test_sessions_e2e.py", "pytest", SERVER,
              _py_pytest("tests/test_sessions_e2e.py"), 9),
        Suite("server-capabilities-promotion", "server tests/test_capabilities_promotion.py",
              "pytest", SERVER, _py_pytest("tests/test_capabilities_promotion.py"), 11),
        # The two cross-repo contract checks read the website validator out of a
        # SIBLING checkout that this deliberately hermetic workflow does not have,
        # so they skip on CI and execute only on an operator box that has both
        # repos. The suite's own helper documents that posture: the skip is
        # opt-out, and LEAF_CONTRACT_STRICT=1 turns it into a hard failure in a
        # job that HAS both checkouts.
        # KNOWN GAP, named rather than hidden: while this reason is allowlisted,
        # cross-repo drift between leaf-web-demo and leaf_website is NOT verified
        # by this gate. Closing it needs a job with both repos checked out and
        # LEAF_CONTRACT_STRICT=1, which is a credentials change beyond this suite.
        Suite("server-product-capability-catalog",
              "server tests/test_product_capability_availability.py",
              # Floor is 54, the count that executes WITHOUT the sibling repo. The
              # old 56 was measured on an operator box that has it, so CI executed
              # 54 and failed the floor even once the skip itself was allowlisted.
              # An operator box still runs 56 and reports upward drift, not a pass.
              "pytest", SERVER, _py_pytest("tests/test_product_capability_availability.py"), 54,
              allowed_skip_reasons=(
                  r"cannot read the website validator from origin/main: [\s\S]*"
                  r"Cross-repo contract drift is UNVERIFIED in this run\.",)),
        # --- broker keystone (census #4, 2026-07-22): test_broker_boundary's --- #
        # one red was a stale pre-§19 assertion (offline `dwg` no longer
        # ignored) — fixed and registered per the #29 fix-then-register rule.
        # The no-da-imports static invariant + §8 ledger-line schema freeze
        # gates ride the same lane.
        Suite("server-broker-boundary", "server tests/test_broker_boundary.py", "pytest",
              SERVER, _py_pytest("tests/test_broker_boundary.py"), 46),
        Suite("server-live-mutation-plan",
              "server tests/test_live_mutation_plan.py", "pytest", SERVER,
              _py_pytest("tests/test_live_mutation_plan.py"), 30),
        Suite("server-panel-transforms",
              "server tests/test_panel_transforms.py", "pytest", SERVER,
              _py_pytest("tests/test_panel_transforms.py"), 41),
        Suite("server-cat-litmus-offline",
              "server tests/test_cat_litmus_offline_e2e.py", "pytest", SERVER,
              _py_pytest("tests/test_cat_litmus_offline_e2e.py"), 1),
        Suite("server-authored-execution-live-gate",
              "server tests/test_authored_execution_live_gate.py", "pytest",
              SERVER, _py_pytest("tests/test_authored_execution_live_gate.py"), 13),
        Suite("server-authored-tenant-isolation",
              "server tests/test_authored_tenant_isolation.py", "pytest",
              SERVER, _py_pytest("tests/test_authored_tenant_isolation.py"), 5),
        Suite("server-wave2-trust-boundary",
              "server tests/test_wave2_trust_boundary.py", "pytest",
              SERVER, _py_pytest("tests/test_wave2_trust_boundary.py"), 13),
        Suite("server-no-da-imports", "server tests/test_no_da_imports_static.py", "pytest",
              SERVER, _py_pytest("tests/test_no_da_imports_static.py"), 8),
        Suite("server-broker-ledger-schema", "server tests/test_broker_ledger_schema_static.py",
              "pytest", SERVER, _py_pytest("tests/test_broker_ledger_schema_static.py"), 9),
        Suite("server-broker-ledger-runtime", "server tests/test_broker_ledger_schema_runtime.py",
              "pytest", SERVER, _py_pytest("tests/test_broker_ledger_schema_runtime.py"), 6),
        # Callback-primary is isolated: it changes completion selection and holds
        # a durable replay ledger, so it must not share another broker suite.
        Suite("server-da-callback", "server tests/test_da_callback.py", "pytest",
              SERVER, _py_pytest("tests/test_da_callback.py"), 15),
        Suite("server-aps-callback-adapter", "server tests/test_aps_callback_adapter.py",
              "pytest", SERVER, _py_pytest("tests/test_aps_callback_adapter.py"), 50),
        # --- modules that were registered in NO suite at all --- #
        # These 19 files existed in server/tests and ran nowhere: not in this
        # runner, not in any directory-target suite. A "*_postgres" name is not
        # a reason to leave one out -- none of them gate at module level, so
        # each carries offline structural tests that simply never ran. Every
        # floor below is the EXECUTED count measured on a host with no DATABASE_URL,
        # so the gate is honest about what it proves on a DB-less checkout, and
        # every environmental skip is named rather than tolerated.
        Suite("server-agent-metering-hook", "server tests/test_agent_metering_hook.py",
              "pytest", SERVER, _py_pytest("tests/test_agent_metering_hook.py"), 2),
        Suite("server-broker-migration-static", "server tests/test_broker_migration_static.py",
              "pytest", SERVER, _py_pytest("tests/test_broker_migration_static.py"), 1),
        Suite("server-canonical-worker-deploy-contract",
              "server tests/test_canonical_worker_deploy_contract.py", "pytest", SERVER,
              _py_pytest("tests/test_canonical_worker_deploy_contract.py"), 3),
        Suite("server-catalog-digest-boundary", "server tests/test_catalog_digest_boundary.py",
              "pytest", SERVER, _py_pytest("tests/test_catalog_digest_boundary.py"), 2),
        Suite("server-customization-store-scaling",
              "server tests/test_customization_store_scaling.py", "pytest", SERVER,
              _py_pytest("tests/test_customization_store_scaling.py"), 3),
        Suite("server-deployment-source-identity",
              "server tests/test_deployment_source_identity.py", "pytest", SERVER,
              _py_pytest("tests/test_deployment_source_identity.py"), 9),
        Suite("server-deployment-identity",
              "server tests/test_deployment_identity.py", "pytest", SERVER,
              _py_pytest("tests/test_deployment_identity.py"), 12),
        Suite("server-emf-metrics-stream", "server tests/test_emf_metrics_stream.py",
              "pytest", SERVER, _py_pytest("tests/test_emf_metrics_stream.py"), 1),
        # Floor 11 = 4 tests + the 7 parametrized cases of the unusable-reading
        # test. Every case is hermetic and unconditional (no skipif, no DB, no
        # subprocess), so the count is the same on every runner.
        Suite("server-submit-latency-metric",
              "server tests/test_submit_latency_metric.py", "pytest", SERVER,
              _py_pytest("tests/test_submit_latency_metric.py"), 11),
        Suite("server-ops-metrics", "server tests/test_ops_metrics.py", "pytest",
              SERVER, _py_pytest("tests/test_ops_metrics.py"), 13),
        # P2 telemetry (waves A + B). Floors are the measured local executed
        # counts on 2026-08-04; neither file was registered when it landed,
        # which made the whole telemetry suite invisible to PR CI (review
        # #426 round-2 blocker).
        Suite("server-telemetry", "server tests/test_telemetry.py", "pytest",
              SERVER, _py_pytest("tests/test_telemetry.py"), 20),
        Suite("server-telemetry-emits", "server tests/test_telemetry_emits.py", "pytest",
              SERVER, _py_pytest("tests/test_telemetry_emits.py"), 10),
        # Floor 13, re-measured 2026-07-27. The 12 was measured when this suite
        # was registered (bd4606c, 2026-07-24), a day before 5495b81 added
        # test_required_platform_rejects_missing_shared_mutation_fence. The floor
        # is a MINIMUM across runners, so it was checked on both before moving:
        # neither this file nor server/tests/conftest.py gates on OS, DATABASE_URL
        # or any other environment, and the Linux test-gate runner reports the
        # same 13 executed / 0 skipped as an operator box (runs 30251486524 and
        # 30250680397). Left at 12 the suite passed with a standing drift note,
        # so the next test added here could have vanished behind it unnoticed.
        Suite("server-platform-postgres-startup",
              "server tests/test_platform_postgres_startup.py", "pytest", SERVER,
              _py_pytest("tests/test_platform_postgres_startup.py"), 13),
        Suite("server-postgres-container-wiring",
              "server tests/test_postgres_container_wiring.py", "pytest", SERVER,
              _py_pytest("tests/test_postgres_container_wiring.py"), 7),
        # Offline restore coverage always runs. The one real PostgreSQL case is
        # separately enforced by upload-authority-postgres.yml and is the only
        # allowed skip on the hermetic test-gate runner.
        Suite("server-version-restore", "server tests/test_version_restore.py",
              "pytest", SERVER, _py_pytest("tests/test_version_restore.py"), 26,
              allowed_skip_reasons=(
                  r"PostgreSQL restore proof requires the EXPLICIT opt-in "
                  r"LEAF_RESTORE_PG_PROOF_DB \(a disposable database URL\)\. "
                  r"A generic ambient DATABASE_URL must never trigger this test: "
                  r"it applies every repository migration and leaves randomized "
                  r"manifest, version, and checkout rows behind, which would mutate "
                  r"a staging or production database whose URL happens to be in the "
                  r"environment\.",)),
        Suite("server-agent-gate-postgres", "server tests/test_agent_gate_postgres.py",
              "pytest", SERVER, _py_pytest("tests/test_agent_gate_postgres.py"), 14,
              allowed_skip_reasons=(r"DATABASE_URL is not set",)),
        Suite("server-agent-ops-postgres", "server tests/test_agent_ops_postgres.py",
              "pytest", SERVER, _py_pytest("tests/test_agent_ops_postgres.py"), 6,
              allowed_skip_reasons=(r"DATABASE_URL is not set",)),
        Suite("server-broker-pg-store", "server tests/test_broker_pg_store.py",
              "pytest", SERVER, _py_pytest("tests/test_broker_pg_store.py"), 28,
              allowed_skip_reasons=(r"DATABASE_URL is not configured",)),
        Suite("server-broker-usage-postgres", "server tests/test_broker_usage_postgres.py",
              "pytest", SERVER, _py_pytest("tests/test_broker_usage_postgres.py"), 4),
        Suite("server-guest-caps-postgres", "server tests/test_guest_caps_postgres.py",
              "pytest", SERVER, _py_pytest("tests/test_guest_caps_postgres.py"), 11,
              allowed_skip_reasons=(
                  r"DATABASE_URL is required for PostgreSQL concurrency tests",)),
        Suite("server-jobs-callbacks-postgres",
              "server tests/test_jobs_callbacks_postgres.py", "pytest", SERVER,
              _py_pytest("tests/test_jobs_callbacks_postgres.py"), 1,
              allowed_skip_reasons=(
                  r"DATABASE_URL is required for PostgreSQL job tests",)),
        # No allowed_skip_reasons ON PURPOSE. This suite proves the terminal write
        # and its platform mirror share one transaction, using a fake connection
        # rather than DATABASE_URL, so every test must execute on every runner. A
        # skip here means the atomicity guarantee went unchecked.
        Suite("server-jobs-terminal-mirror-atomic",
              "server tests/test_jobs_terminal_mirror_atomic.py", "pytest", SERVER,
              _py_pytest("tests/test_jobs_terminal_mirror_atomic.py"), 5),
        # The SQLite half of the same guarantee. No allowed_skip_reasons for the same
        # reason: the platform boundary is faked at _update_by_spine, never DATABASE_URL,
        # so every test must execute on every runner. A skip here means an undelivered
        # mirror could silently go back to being lost.
        Suite("server-jobs-terminal-mirror-durable",
              "server tests/test_jobs_terminal_mirror_durable.py", "pytest", SERVER,
              _py_pytest("tests/test_jobs_terminal_mirror_durable.py"), 14),
        # Floor 13, re-measured after main added four offline tests. It was 9 when
        # this suite was first registered; leaving it there would have let all
        # four of main's new tests disappear without reddening the gate.
        Suite("server-session-store-postgres", "server tests/test_session_store_postgres.py",
              "pytest", SERVER, _py_pytest("tests/test_session_store_postgres.py"), 13,
              allowed_skip_reasons=(
                  r"PostgreSQL integration test requires explicit DATABASE_URL",)),
        # The one module in the 19 with NO offline coverage: every test needs a
        # live DB. Registering it plain would report "1 skipped" as a PASS -- the
        # exact vacuous-green this gate exists to stop -- so it is db_gated and
        # reports an explicit SKIP row until a DB is reachable, then executes.
        Suite("server-ops-metrics-pg", "server tests/test_ops_metrics_pg.py", "pytest",
              SERVER, _py_pytest("tests/test_ops_metrics_pg.py"), 1, db_gated=True),
        Suite("server-postgres-authority-inventory",
              "server tests/test_postgres_authority_inventory_contract.py", "pytest",
              SERVER, _py_pytest("tests/test_postgres_authority_inventory_contract.py"), 6),
        # --- da/ (cwd=da) --- #
        Suite("da-store", "da test_store.py", "pytest", DA,
              _py_pytest("test_store.py"), 34),
        Suite("da-multitenant", "da test_multitenant.py", "pytest", DA,
              _py_pytest("test_multitenant.py"), 10),
        # Both are fully offline (no APS, no network) but were never registered,
        # so 11 tests sat outside the gate entirely.
        Suite("da-client-credentials", "da test_client_credentials.py", "pytest", DA,
              _py_pytest("test_client_credentials.py"), 6),
        Suite("da-extract-dxf-activity", "da test_extract_dxf_activity.py", "pytest", DA,
              _py_pytest("test_extract_dxf_activity.py"), 5),
        Suite("da-mutation-apply", "da test_mutation_apply.py", "pytest", DA,
              _py_pytest("test_mutation_apply.py"), 22),
        # Windows operator hosts run the non-billable AutoCAD engine canary.
        # Linux CI must still collect the suite and may skip only when the named
        # local AutoCAD runtime or tracked demo DWG is unavailable.
        Suite("da-mutation-apply-accoreconsole",
              "da test_mutation_apply_accoreconsole.py", "pytest", DA,
              _py_pytest("test_mutation_apply_accoreconsole.py"), 1,
              allowed_skip_reasons=(
                  r"local AutoCAD 2026 console and tracked demo DWG are required",
              )),
        # --- tenant customization control plane (one process per file) --- #
        Suite("server-customization-authority", "server customization authority", "pytest",
              SERVER, _py_pytest("tests/test_customization_authority.py"), 7),
        Suite("server-customization-store", "server customization store", "pytest",
              SERVER, _py_pytest("tests/test_customization_store.py"), 8),
        Suite("server-customization-reconcile", "server customization reconcile", "pytest",
              SERVER, _py_pytest("tests/test_customization_reconcile.py"), 8),
        Suite("server-customization-contract", "server customization contract freeze", "pytest",
              SERVER, _py_pytest("tests/test_customization_contract_freeze.py"), 8),
        Suite("server-customization-runtime", "server customization runtime", "pytest",
              SERVER, _py_pytest("tests/test_customization_runtime.py"), 26),
        Suite("server-customization-postgres-contract",
              "server customization PostgreSQL contract", "pytest",
              SERVER, _py_pytest("tests/test_customization_postgres_contract.py"), 9),
        # Holds both halves of a refusal: the cause reaches the operator log, and
        # never the tenant's response body.
        Suite("server-customization-refusal-observability",
              "server customization refusal observability", "pytest",
              SERVER, _py_pytest("tests/test_customization_refusal_observability.py"), 42),
        # The two OS-file-lock probes are skipif(fcntl is None): they EXECUTE on
        # the Linux CI runner and skip only on a Windows operator box. Named here
        # so a Windows run stays green without the fail-closed skip rule having to
        # tolerate an unnamed reason.
        # Floor is 17, the count that executes on Windows (19 collected, minus the
        # two fcntl-gated lock probes). Linux CI executes all 19 and reports drift.
        # The old floor of 5 let 12 tests vanish and still call the suite green.
        # RESIDUAL, deliberately not closed here: the allowlist is not
        # OS-conditional, so a Linux runner that somehow lacked fcntl would skip
        # the two lock probes and still clear 16. Catching that needs per-OS suite
        # config the runner does not have today.
        Suite("server-customization-adversarial", "server customization adversarial", "pytest",
              SERVER, _py_pytest("tests/test_customization_adversarial.py"), 17,
              allowed_skip_reasons=(r"POSIX advisory locking only",)),
        Suite("server-customization-publish-recovery", "server customization publish recovery", "pytest",
              SERVER, _py_pytest("tests/test_customization_publish_recovery.py"), 1),
        Suite("server-platform-release-policy", "server platform release policy", "pytest",
              SERVER, _py_pytest("tests/test_platform_release_policy.py"), 14),
        # --- platform (cwd=repo parent; DB-gated) --- #
        # Floor 236 is a MEASURED, GREEN, pristine-database baseline: 236
        # passed, 0 skipped, 0 failed, pytest exit 0, taken 2026-07-27 against
        # PostgreSQL 16.14 from this suite's registered cwd (the repo parent,
        # target <repo_name>/platform/tests) and reproduced on a second freshly
        # created database. It supersedes the 234 measured earlier the same day
        # (this branch adds exactly two tests), 199 (2026-07-25, PostgreSQL 17) and
        # the interim 223, which was only 199 plus the 24 cases one new file
        # added -- deliberately NOT a clean baseline, so it left an 11-test gap
        # between the floor and the real count. Losing 1 to 10 tests printed
        # only an "(executed-count drift)" note; losing exactly 11 printed
        # NOTHING, because coverage_verdict returns with no note once executed
        # == expected. That gap is now zero: the floor IS the count, so losing
        # any single test trips "executed-count regression" instead.
        # Collected and executed are both 236 because every skip path under
        # platform/tests is gated on the DB being unconfigured -- on a reachable
        # DB nothing skips, so a green run executes everything collected. The
        # empty skip allowlist does not prevent skips; it makes any skip FAIL
        # the suite, which is what keeps that property honest rather than
        # letting a future environment-gated skip erode the count quietly.
        # Re-baselining needs a PRISTINE database, not just a reachable one.
        # Several tests bind fixed external subjects (e.g. "auth0|1b-cross-org")
        # that are unique per tenant, so a second run against the same database
        # fails on the first run's rows instead of measuring anything.
        # The session timezone no longer changes this measurement. It used to:
        # off UTC, 4 test_signing.py cases failed `payload_mismatch` because
        # verify_signature re-derived signedAt as row["signed_at"].isoformat()
        # from a TIMESTAMPTZ, and the same instant renders differently under a
        # non-UTC session. That is fixed in signing.py (the re-derivation
        # normalizes to UTC first) and pinned by
        # test_signature_verifies_when_the_reader_session_is_not_utc, which
        # forces a non-UTC session itself rather than trusting the ambient one.
        # The WRITE side is canonicalized too: _now() converts a caller-supplied
        # `now=` to UTC, so the signed rendering cannot disagree with the one
        # verification re-derives; pinned by
        # test_signature_verifies_when_countersigned_with_a_non_utc_now.
        # 247 was measured both ways -- TimeZone=America/Chicago and UTC.
        # Raising this cannot red-fail CI: the suite is db_gated and the
        # test-gate workflow is hermetic, so run_suite returns SKIP with
        # "platform DB unreachable" before any executed-count check runs.
        Suite("platform", "platform/tests (Postgres)", "pytest", REPO_PARENT,
              _py_pytest(f"{repo_name}/platform/tests"), 247, db_gated=True),
        # Dependency-free *_static proofs must run even with NO Postgres: the
        # conftest's pytest_ignore_collect exempts them, so this un-gated suite
        # keeps them in the gate on a clean checkout.
        # This list must name EVERY platform/tests/*_static.py. The db_primitives
        # and db_readiness files were missing, and because the `platform` suite
        # above is db_gated they ran NOWHERE on a clean checkout -- 26 tests
        # outside the gate entirely, 24 of them dependency-free and 2 DB-gated
        # (both skips live in test_db_primitives_static.py). The schema proof is
        # also explicit here so this PR cannot ship its dependency-free
        # assertions ungated.
        # Explicit file targets, not the dir, so the COLLECTED count stays
        # invariant to DB presence. The floor below is the EXECUTED count on a
        # host with no DATABASE_URL: 124 collected minus the 2 DB-gated skips
        # named in allowed_skip_reasons = 122, measured on this tree
        # 2026-08-04. History of the floor: 96 (2026-07-28, 98 collected);
        # #256 and #401 then added 6 tests that only surfaced as a drift
        # note (#432 moved the floor to 102 for those);
        # test_overlay_store_static.py (20 dependency-free tests, added with
        # the T1 overlay lane) was registered NOWHERE until this entry picked
        # it up. gate-runner-selftest now pins this list against
        # glob("platform/tests/*_static.py") so the next *_static.py file
        # cannot silently run nowhere.
        Suite("platform-static", "platform/tests *_static (no DB)", "pytest", REPO_PARENT,
              _py_pytest(f"{repo_name}/platform/tests/test_ledger_static.py")
              + [f"{repo_name}/platform/tests/test_hashing_static.py",
                 f"{repo_name}/platform/tests/test_replay_static.py",
                 f"{repo_name}/platform/tests/test_evidence_freeze_static.py",
                 f"{repo_name}/platform/tests/test_db_primitives_static.py",
                 f"{repo_name}/platform/tests/test_db_readiness_static.py",
                 f"{repo_name}/platform/tests/test_db_schema_proof_static.py",
                 f"{repo_name}/platform/tests/test_overlay_store_static.py"], 122,
              allowed_skip_reasons=(
                  r"PostgreSQL integration test requires DATABASE_URL",)),
        # The committed replay fixture is dependency-free and catches hash or
        # replay drift before a PR reaches the GitHub simulator-gate workflow.
        Suite("platform-simgate-self-test", "platform simulator-gate self-test", "script",
              REPO_PARENT,
              [sys.executable, f"{repo_name}/platform/simgate/run.py", "--self-test"], None),
        # --- scripts (cwd=SCRIPTS_DIR) --- #
        # cwd=SCRIPTS_DIR, not REPO: `python -m pytest` puts the cwd on
        # sys.path, and the repo root would shadow the stdlib `platform`.
        # Registered per the #29 fix-then-register rule (shipped without a
        # gate entry; measured 1 passed on this tree 2026-07-23).
        Suite("build-platform-images-workflow",
              "scripts test_build_platform_images_workflow.py", "pytest",
              SCRIPTS_DIR, _py_pytest("test_build_platform_images_workflow.py"), 1),
        Suite("platform-release-manifest",
              "scripts test_platform_release_manifest.py", "pytest",
              SCRIPTS_DIR, _py_pytest("test_platform_release_manifest.py"), 21),
        Suite("production-web-release",
              "scripts test_production_web_release.py", "pytest",
              SCRIPTS_DIR, _py_pytest("test_production_web_release.py"), 9),
        # --- the gate runner's own spawn-failure/retry behavior (this file) --- #
        # Floor 47: the 29 measured 2026-07-28, plus the 18 sharding tests
        # (partition determinism, catalog fingerprint incl. toolchain-path
        # canonicalization, the glob pin on platform/tests/*_static.py, shard
        # CLI rejections, and the fan-in verifier's accept cases + refusal of
        # every corruption class incl. below-floor PASS and ungated SKIP),
        # measured on this tree 2026-08-04.
        Suite("gate-runner-selftest", "scripts test_gate_runner.py", "pytest",
              SCRIPTS_DIR, _py_pytest("test_gate_runner.py"), 47),
        Suite("public-host-contract", "scripts public host contract probe", "pytest",
              SCRIPTS_DIR, _py_pytest("test_public_host_probe.py"), 11),
        # W14 expand-contract migration gate: the pytest suite validates the
        # checker AND runs it over the real platform/migrations corpus, so a
        # contract-phase migration without its expand marker fails this gate
        # on every PR (test-gate.yml runs this runner).
        Suite("migration-expand-contract",
              "scripts test_migration_expand_contract_gate.py", "pytest",
              SCRIPTS_DIR, _py_pytest("test_migration_expand_contract_gate.py"), 12),
        # --- harness (cwd=harness) --- #
        Suite("harness-vitest", "harness npm test (vitest)", "vitest", HARNESS,
              [_npm(), "test"], 317,
              allowed_vitest_skips=(
                  ("test/tenantRepoLease.test.ts", 4),
                  ("test/harnessSchema.pg.test.ts", 1),
                  ("test/pgSessionStore.contract.test.ts", 5),
              )),
        # --- web unit (cwd=web) --- #
        # The web workspace had NO unit runner until the T1 overlay card; its
        # only specs were Playwright e2e, which this gate runs separately. A
        # review found the 14 card tests executing nowhere in CI, so the
        # operator-facing decision surface -- both controls, untrusted text,
        # style sinks -- was unverified on every PR.
        Suite("web-vitest", "web npm run test:unit (vitest)", "vitest", WEB,
              [_npm(), "run", "test:unit"], 35),
        Suite("harness-tsc-noemit", "harness npx tsc --noEmit", "tsc", HARNESS,
              [_npx(), "tsc", "--noEmit"], None),
        Suite("harness-tsc-build", "harness npx tsc -p tsconfig.build.json", "tsc", HARNESS,
              [_npx(), "tsc", "-p", "tsconfig.build.json"], None),
        Suite("harness-audit-high", "harness npm audit (high threshold)", "script", HARNESS,
              [_npm(), "audit", "--audit-level=high"], None),
        Suite("web-customization-check", "web customization static check", "script", WEB,
              [_npm(), "run", "check:customization"], None),
        Suite("web-customize-panel-check", "R7 self-edit panel boundary check", "script", WEB,
              [_npm(), "run", "check:customize-panel"], None),
        Suite("web-staging-fixes-check", "web staging fix regression checks", "script", WEB,
              [_npm(), "run", "check:staging-fixes"], None),
        # Named separately as well as inside check:staging-fixes, so a failure
        # reports as the checkout lock rather than as a generic staging-fix chain.
        Suite("web-checkout-identity-check", "web single-writer checkout lock identity", "script", WEB,
              [_npm(), "run", "check:checkout-identity"], None),
        Suite("web-deployed-acceptance-contract",
              "web deployed authored CAD acceptance contract", "script", WEB,
              [_npm(), "run", "check:deployed-acceptance-contract"], None),
        Suite("web-proof-receipt-contract",
              "web proof receipt contract", "script", WEB,
              [_npm(), "run", "check:proof-receipt"], None),
        # The seeded workbench id is the deployed acceptance driver's contract
        # with the surface: it seeds a server-canonical id and requires that
        # exact drawing to open.
        Suite("web-workbench-id-contract",
              "web live workbench drawing id rule", "script", WEB,
              [_npm(), "run", "check:workbench-id"], None),
        Suite("web-version-restore-proof",
              "web /app version restore browser proof", "script", WEB,
              [_npm(), "run", "proof:version-restore"], None),
        # The daily authoring cap refuses with 429 (CONTRACT-ADDENDUM §17). The
        # only thing standing between that refusal and a red "Couldn't author
        # the tool" is api.js tagging quota_kind=="daily_author"; nothing else
        # covered that, so a rename on either side would have shipped silently.
        Suite("web-author-quota-gate",
              "web daily authoring 429 renders a calm QuotaGate", "script", WEB,
              [_npm(), "run", "proof:author-quota-gate"], None),
        Suite("web-build", "web production build", "script", WEB,
              [_npm(), "run", "build"], None),
        # --- containerized harness smoke (census #13) — OPT-IN --- #
        # Builds + boots the real compose stack (broker+harness+app, mock agent)
        # and proves the authed app->harness hop, durable grant/tenant volumes,
        # and secret-free logs. Needs Docker + compose >= 2.24 and several minutes
        # on a cold image cache, so it only runs when LEAF_CONTAINER_SMOKE=1.
        Suite("harness-container-smoke", "harness container smoke (compose)", "script",
              REPO, [sys.executable, "scripts/harness-container-smoke.py"], None,
              opt_in_env="LEAF_CONTAINER_SMOKE", timeout_s=1800),
        # --- web presenter-kit demo gate --- #
        # This runner had ZERO references to web/, so web/'s golden-path demo
        # scripts (test/check_routes.mjs, test/check_integration.mjs,
        # scripts/check_author.mjs, check_writeloop.mjs, check_tourscript.mjs)
        # had no automated consumer at all. dispatch/run-local-ci.sh's demo-gate
        # bucket is the purpose-built adapter for exactly those oracles and was
        # never wired to anything; this row is that wiring. It also runs the vite
        # build with a >=2-JS-chunk assertion, the offline pre-flight, and the
        # authored-tool registry probe, so it is slower than a plain suite.
        Suite("web-demo-gate", "web dispatch/run-local-ci.sh --only demo-gate", "script",
              REPO, [_bash(), "dispatch/run-local-ci.sh", "--only", "demo-gate"], None,
              timeout_s=1200),
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
    "LEAF_CALLBACK_SECRET", "LEAF_CALLBACK_URL", "LEAF_CALLBACK_PRIMARY",
    "LEAF_CALLBACK_MAX_AGE_S",
    "LEAF_SANDBOX", "LEAF_SANDBOX_TIMEOUT_S", "LEAF_E2B_HELPER", "E2B_API_KEY",
    "LEAF_AUTHOR_SANDBOX_PROVIDER", "LEAF_TOOL_SANDBOX_PROVIDER",
    "LEAF_AUTHORED_EXECUTION", "E2B_API_KEY_FILE", "LEAF_SANDBOX_BROKER_HOST",
    # harness F5 caller-auth + agent-mock toggles: ambient values would 401 (or
    # fake out) the hermetic harness suites, which pin these per-test instead.
    "LEAF_HARNESS_AUTH", "LEAF_HARNESS_SECRET", "LEAF_AGENT_MOCK", "LEAF_GRANT_STORE",
    # gate-runner fault injection (see run_suite) must never leak into nested
    # runners or suite children.
    "LEAF_GATE_FAULT_INJECT",
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
    skip_reasons: list[tuple[int, str]] = []
    for line in t.splitlines():
        match = re.match(r"SKIPPED \[(\d+)\] .*?: (.+)$", line.strip())
        if match:
            skip_reasons.append((int(match.group(1)), match.group(2).strip()))
    return {"passed": passed, "failed": failed, "errors": errors,
            "skipped": skipped, "got": got, "skip_reasons": skip_reasons}


def coverage_verdict(c: dict, expected: Optional[int], passed: bool,
                     note: str) -> tuple[bool, str]:
    """Apply the executed-coverage rules shared by the pytest AND vitest paths.

    Both parsers report `got` as passed+failed+skipped, so both are vulnerable
    to the same lie, and only the pytest path was fixed the first time. One
    implementation, two call sites, so a future fix cannot land on one path and
    silently miss the other.

    Rule 1 -- a suite where EVERY test skipped asserted nothing, so it proves
    nothing. Reporting PASS there makes a green scoreboard mean "did not run",
    which is the one thing a merge gate must never say.

    Rule 2 -- `expected` is an EXECUTED-test floor, never a collected-test
    floor. A skipped test proves no assertion, so counting it toward the floor
    lets a suite trade real coverage for skips and stay green.
    """
    executed = c["got"] - c.get("skipped", 0)
    if passed and c["got"] and executed == 0:
        return False, (note + " " if note else "") + "ALL skipped: no coverage"
    if expected is not None and passed and executed < expected:
        return False, (note + " " if note else "") + \
            f"executed-count regression: expected >= {expected}, got {executed}"
    if expected is not None and passed and executed > expected:
        return True, (note + " " if note else "") + \
            f"(executed-count drift: expected {expected})"
    return passed, note


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
    skipped_files: list[tuple[str, int]] = []
    for output_line in t.splitlines():
        match = re.search(r"(\S+\.test\.ts).*?(\d+) skipped", output_line)
        if match:
            skipped_files.append((match.group(1).replace("\\", "/"), int(match.group(2))))
    return {"passed": passed, "failed": failed, "skipped": skipped, "got": got,
            "skipped_files": skipped_files}


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
    argv = [str(a) for a in suite.argv]
    # Fault-injection drill: LEAF_GATE_FAULT_INJECT="<suite-id>:spawn" points
    # this suite's FIRST attempt at a nonexistent binary, exercising the real
    # spawn-failure path end to end (attempt 2+ runs the real argv, so the
    # drill proves a transient spawn failure survives as a retried PASS).
    fault = os.environ.get("LEAF_GATE_FAULT_INJECT", "")
    if fault and attempt == 1 and fault == f"{suite.id}:spawn":
        argv = [argv[0] + ".fault-injected-missing.exe"] + argv[1:]
    spawn_command, use_shell, shell_executable = normalize_spawn_command(argv)
    spawn_err = ""
    with open(log_path, "w", encoding="utf-8", errors="replace") as logf:
        logf.write(f"$ (cwd={suite.cwd})\n$ {' '.join(argv)}\n"
                   f"$ attempt {attempt} @ {time.strftime('%Y-%m-%dT%H:%M:%S')}\n\n")
        logf.flush()
        try:
            proc = subprocess.run(
                spawn_command,
                cwd=str(suite.cwd), env={**clean_env(), **db_env},
                capture_output=True, text=True, timeout=suite.timeout_s,
                shell=use_shell, executable=shell_executable,
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
        except OSError as exc:
            # Spawn failure (missing binary, AV/file lock, process pressure).
            # This used to escape the try, kill the runner mid-gate, and lose
            # the scoreboard; report it as a FAIL row so the normal retry
            # applies to it exactly like a red suite.
            spawn_err = f"{type(exc).__name__}: {str(exc)[:160]}"
            out = f"[SPAWN FAILURE] {spawn_err}"
            rc = 127
        logf.write(out)
    seconds = time.perf_counter() - t0

    # Rows with no test output get an explicit hint so a bare nonzero exit is
    # diagnosable from the scoreboard alone: a spawn failure names its OS
    # error; an empty-output kill (observed 2026-07-23: an external sweeper
    # Stop-Processing gate pythons, which Git Bash surfaces as exit 127) is
    # called out as such instead of masquerading as a silent red suite.
    fail_hint = ""
    if spawn_err:
        fail_hint = f"spawn failure: {spawn_err}"
    elif rc != 0 and not out.strip():
        fail_hint = f"no output (exit {rc}): child failed to start or was killed externally"

    if suite.kind == "pytest":
        c = parse_pytest(out)
        passed = rc == 0 and c["failed"] == 0 and c["errors"] == 0
        note = pre_note
        if fail_hint and not passed:
            note = (note + " " if note else "") + fail_hint
        if c["skipped"]:
            note = (note + " " if note else "") + f"{c['skipped']} skipped"
            reported = sum(count for count, _ in c["skip_reasons"])
            unexpected = [reason for _, reason in c["skip_reasons"]
                          if not any(re.fullmatch(pattern, reason)
                                     for pattern in suite.allowed_skip_reasons)]
            if reported != c["skipped"]:
                passed = False
                note += (f"; skip details incomplete: pytest reported {c['skipped']} "
                         f"but named {reported}")
            elif unexpected:
                passed = False
                note += "; non-allowlisted skip: " + "; ".join(unexpected)
        passed, note = coverage_verdict(c, suite.expected, passed, note)
        return Result(suite, "PASS" if passed else "FAIL", str(c["got"]), seconds,
                      note=note.strip(), log_path=log_path, counts=c)

    if suite.kind == "vitest":
        c = parse_vitest(out)
        passed = rc == 0 and c["failed"] == 0
        note = f"{c['skipped']} skipped" if c.get("skipped") else ""
        if c.get("skipped"):
            actual = dict(c["skipped_files"])
            allowed = dict(suite.allowed_vitest_skips)
            reported = sum(actual.values())
            unexpected = {path: count for path, count in actual.items()
                          if allowed.get(path) != count}
            if reported != c["skipped"]:
                passed = False
                note += (f"; skip details incomplete: vitest reported {c['skipped']} "
                         f"but named {reported}")
            elif unexpected:
                passed = False
                note += f"; non-allowlisted vitest skip: {unexpected}"
        if fail_hint and not passed:
            note = (note + " " if note else "") + fail_hint
        # Same zero-coverage and executed-floor rules as pytest suites, through
        # the same helper: vitest's `got` counts skips too, so an all-skipped
        # run used to clear its floor here and report a vacuous PASS.
        passed, note = coverage_verdict(c, suite.expected, passed, note)
        return Result(suite, "PASS" if passed else "FAIL", str(c["got"]), seconds,
                      note=note.strip(), log_path=log_path, counts=c)

    if suite.kind == "script":
        # Optional scripts are skipped before spawn through opt_in_env. Once a
        # script is selected, an unavailable prerequisite is a failed gate.
        if rc == 3:
            last = next((ln for ln in out.strip().splitlines() if ln.strip()), "")
            return Result(suite, "FAIL", "err", seconds,
                          note=last[:120] or "required environment unavailable",
                          log_path=log_path, counts={})
        return Result(suite, "PASS" if rc == 0 else "FAIL",
                      "ok" if rc == 0 else "err", seconds,
                      note=("" if rc == 0 else (fail_hint or f"exit {rc}")),
                      log_path=log_path, counts={})

    # tsc: pass/fail on exit code only
    passed = rc == 0
    return Result(suite, "PASS" if passed else "FAIL",
                  "ok" if passed else "err", seconds,
                  note=("" if passed else (fail_hint or f"tsc exit {rc}")),
                  log_path=log_path, counts={})


def run_suite_guarded(suite: Suite, log_dir: Path, attempt: int) -> Result:
    """One suite must never take down the whole gate: any unexpected
    runner-side exception (parse bug, log-dir I/O, ...) becomes a FAIL row on
    the scoreboard instead of an uncaught crash that loses every remaining
    suite and the scoreboard itself."""
    try:
        return run_suite(suite, log_dir, attempt=attempt)
    except Exception as exc:  # noqa: BLE001 — the scoreboard is the contract
        return Result(suite, "FAIL", "err", 0.0,
                      note=f"runner error: {type(exc).__name__}: {str(exc)[:160]}")


# --------------------------------------------------------------------------- #
# catalog integrity
# --------------------------------------------------------------------------- #
def duplicate_suite_ids(suites: List[Suite]) -> List[str]:
    """Ids registered more than once, in first-registration order.

    The id is the runner's only handle on a suite: `--only` matches against it
    (select_suites), and each child's output goes to <log-dir>/<id>.log. So a
    second registration under a live id runs the same tests twice, answers to
    the same `--only`, and lets the second child's log overwrite the first.

    The two registrations can also carry different labels and different
    expected floors, and then the scoreboard prints two differently-named rows
    for one test file -- one flagged for executed-count drift against the stale
    floor, one clean against the current one -- with nothing saying which floor
    the gate actually stands on. That is not a verdict, so the runner refuses
    to produce one.

    Ordered by where each id was FIRST registered, so the printed list reads
    down the catalog in the same order as the file the reader has to go fix.
    Keying on the repeat instead would order by second occurrence: `alpha,
    beta, beta, alpha` would report beta before alpha.
    """
    first_seen: dict[str, int] = {}
    dupes: set[str] = set()
    for index, suite in enumerate(suites):
        if suite.id in first_seen:
            dupes.add(suite.id)
        else:
            first_seen[suite.id] = index
    return sorted(dupes, key=lambda sid: first_seen[sid])


# --------------------------------------------------------------------------- #
# --only selection
# --------------------------------------------------------------------------- #
NO_FILTER = "all suites (no --only filter)"


def select_suites(suites: List[Suite],
                  patterns: List[str]) -> tuple[List[Suite], List[str]]:
    """Union filter for `--only`: a suite is selected when its id contains ANY
    of the given substrings, in the runner's declared suite order.

    Returns (selected, dead_patterns). A pattern matching no suite is reported
    rather than absorbed. Both halves exist for the same reason: on 2026-07-25
    two sessions typed `--only a --only b`, argparse kept only `b`, and the run
    printed a perfectly truthful `1 PASS 0 FAIL 0 SKIP` for a set nobody asked
    for. Unioning fixes the flag; refusing to run on a dead substring stops a
    typo from re-opening the same gap one pattern at a time.
    """
    selected = [s for s in suites if any(p in s.id for p in patterns)]
    dead = [p for p in patterns if not any(p in s.id for s in suites)]
    return selected, dead


def describe_selection(patterns: List[str]) -> str:
    """The `--only` selection, rendered so the scoreboard can be read straight
    back against the command line that produced it."""
    if not patterns:
        return NO_FILTER
    flags = " ".join(f"--only {p}" for p in patterns)
    if len(patterns) == 1:
        return flags
    return f"{flags}  (union of {len(patterns)} substrings)"


# --------------------------------------------------------------------------- #
# sharding: deterministic partition + result files + fan-in verifier
#
# CI splits the catalog across N isolated runner checkouts (test-gate.yml
# matrix). Isolation is the point: suites share on-disk state inside one
# checkout (jobs.db, the versioned drawing store, authored_tools.json, broker
# ledgers — see the module docstring), so the suites of one shard still run
# STRICTLY SERIALLY, exactly as a full run does. Nothing about a suite's
# environment, retry, floor, or log changes under sharding; only WHICH suites
# a given invocation runs.
#
# The completeness proof lives in the fan-in, not in trust: every shard writes
# a result file carrying a fingerprint of the full catalog it partitioned, and
# --verify-shard-results recomputes the partition from ITS OWN checkout and
# refuses unless every shard ran exactly its assigned slice of the exact same
# catalog and every suite passed. A shard silently dropped, duplicated, run
# against a different tree, or run with a hand-typed --only cannot verify.
# --------------------------------------------------------------------------- #

# Measured wall seconds per suite (CI ubuntu-latest, runs 30922601347 /
# 30924090296, 2026-08-04) — scheduling weights for the partition, nothing
# more. Only suites >= ~3s are listed; the long tail defaults. A stale or
# missing weight only unbalances shard makespans, never correctness, but keep
# the heavy entries roughly honest or one shard quietly becomes the critical
# path. gate-runner-selftest pins every key here to a registered suite id so a
# rename cannot strand a weight.
_MEASURED_EST_S = {
    "web-version-restore-proof": 100.0,
    "server-checkout-crossproc": 60.0,
    "server-backbone": 50.0,
    "harness-vitest": 36.0,
    "server-sessions-router": 29.0,
    "web-author-quota-gate": 20.0,
    "server-turn-runner": 16.0,
    "server-write-loop": 12.0,
    "server-sessions-e2e": 12.0,
    "server-sessions-routes": 7.0,
    "server-wave5": 7.0,
    "server-hardening-3b": 6.6,
    "harness-tsc-noemit": 5.8,
    "server-wave3": 5.2,
    "server-platform-customize": 4.7,
    "server-guest-uploads": 4.6,
    "harness-tsc-build": 4.6,
    "server-microvm": 4.5,
    "server-dynamic-loader": 4.3,
    "web-demo-gate": 4.1,
    "web-build": 3.8,
    "server-wave2": 3.7,
    "server-guest-purge": 3.7,
    "server-customization-adversarial": 3.4,
    "server-ui-wave": 3.3,
    "server-wave4": 3.2,
    "server-agent-e2e": 3.2,
    "server-jobs-connection-ownership": 3.1,
}
_DEFAULT_EST_S = 2.0


def suite_weight(suite: Suite) -> float:
    return _MEASURED_EST_S.get(suite.id, _DEFAULT_EST_S)


def partition_suites(suites: List[Suite], shard_count: int) -> List[List[Suite]]:
    """Deterministic longest-processing-time partition of the catalog.

    Heaviest suite first (ties broken by id), each into the currently
    least-loaded shard (ties broken by lowest index). Every input on both
    sides of a tie is totally ordered, so the same catalog and count always
    produce the same partition — the property the fan-in verifier recomputes
    and stands on. Within a shard, suites run in catalog order, like a full
    run does.
    """
    if shard_count < 1:
        raise ValueError("shard_count must be >= 1")
    index_of = {s.id: i for i, s in enumerate(suites)}
    heaviest_first = sorted(suites, key=lambda s: (-suite_weight(s), s.id))
    loads = [0.0] * shard_count
    bins: List[List[Suite]] = [[] for _ in range(shard_count)]
    for suite in heaviest_first:
        target = min(range(shard_count), key=lambda k: (loads[k], k))
        bins[target].append(suite)
        loads[target] += suite_weight(suite)
    return [sorted(b, key=lambda s: index_of[s.id]) for b in bins]


def _fingerprint_argv(argv: List[str]) -> List[str]:
    """argv[0] is resolved from the ENVIRONMENT (sys.executable, shutil.which
    for npm/npx, Git Bash discovery), so its absolute path legitimately
    differs between jobs of one CI run: shard jobs run setup-node, the
    fan-in does not, and run 30938231420's fan-in resolved a different npm
    than every shard — refusing all eight as fingerprint mismatches. Hash
    the command IDENTITY (interpreter/tool token) instead of its path;
    positional targets and flags still hash raw."""
    if not argv:
        return []
    head = str(argv[0])
    if head == sys.executable:
        head = "<PYTHON>"
    else:
        # String-normalize both separator styles: on Linux, Path() is
        # PosixPath and does NOT split C:\...\npm.cmd at backslashes, so a
        # Windows-shaped path would escape canonicalization there while being
        # canonicalized on Windows — the fingerprint would then depend on the
        # OS that computed it (sol-critic #436 round 3, and the real
        # gate-shard-6 red on run 30940064231).
        name = head.replace("\\", "/").rsplit("/", 1)[-1].lower()
        for ext in (".cmd", ".exe"):
            if name.endswith(ext):
                name = name[: -len(ext)]
        if name in ("npm", "npx", "bash"):
            head = f"<{name.upper()}>"
    return [head] + [str(a) for a in argv[1:]]


def catalog_fingerprint(suites: List[Suite]) -> str:
    """SHA-256 over every field of every suite, so two runs agree on this
    value only when they partitioned the same catalog with the same floors,
    allowlists, and commands.

    cwd paths are hashed RAW, which binds the fingerprint to one checkout
    layout: an intra-run integrity token compared between the shard jobs and
    the fan-in of a single CI run (identical layout), or between local runs
    in one worktree — never across machines. Toolchain paths inside argv are
    canonicalized (see _fingerprint_argv) because they vary per JOB, not per
    catalog."""
    entries = [{
        "id": s.id, "label": s.label, "kind": s.kind, "cwd": str(s.cwd),
        "argv": _fingerprint_argv(s.argv), "expected": s.expected,
        "allowed_skip_reasons": list(s.allowed_skip_reasons),
        "allowed_vitest_skips": [list(pair) for pair in s.allowed_vitest_skips],
        "reset_authored": s.reset_authored, "db_gated": s.db_gated,
        "opt_in_env": s.opt_in_env, "timeout_s": s.timeout_s,
    } for s in suites]
    blob = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def executed_count(res: Result) -> Optional[int]:
    """Executed tests behind a result row: got minus skipped, the same
    quantity coverage_verdict compares floors against. None for rows that
    carry no test counts at all (tsc / script suites); 0 for suite-level
    SKIPs."""
    if res.status == "SKIP":
        return 0
    c = res.counts
    if not c or "got" not in c:
        return None
    try:
        return int(c["got"]) - int(c.get("skipped", 0))
    except (TypeError, ValueError):
        return None


def write_result_json(path: str, *, fingerprint: str, total: int,
                      shard_count: int, shard_index: int, selection: str,
                      suites: List[Suite], results: List[Result],
                      attempts_by_id: dict, wall: float) -> None:
    """One shard's machine-readable outcome, written even when suites failed
    so the fan-in can name the failure instead of reporting a missing shard."""
    entries = []
    for r in results:
        entries.append({
            "id": r.suite.id,
            "status": r.status,
            "got": r.got,
            "executed": executed_count(r),
            "expected": r.suite.expected,
            "attempts": attempts_by_id.get(r.suite.id, 1),
            "seconds": round(r.seconds, 1),
            "note": r.note,
        })
    payload = {
        "schema": 1,
        "catalog_fingerprint": fingerprint,
        "total_suites": total,
        "shard_count": shard_count,
        "shard_index": shard_index,
        "selection": selection,
        "suite_ids": [s.id for s in suites],
        "results": entries,
        "executed_total": sum(e["executed"] or 0 for e in entries),
        "any_fail": any(r.status == "FAIL" for r in results),
        "wall_seconds": round(wall, 1),
    }
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1), encoding="utf-8")


def verify_shard_results(results_dir: Path) -> int:
    """Fan-in: prove the shard set covered the exact catalog, exactly once,
    on the same tree, and everything passed. Exit 0 only on that proof.

    Every check here exists because its absence is a silent hole:
      * missing/duplicate shard  -> a slice of the catalog never ran
      * fingerprint mismatch     -> a shard ran a different catalog or tree
      * suite set != partition   -> someone ran a hand-typed subset
      * results != suite set     -> a shard stopped early (e.g. --fail-fast)
      * executed_total mismatch  -> the file's own arithmetic is broken
    """
    problems: List[str] = []
    parsed: List[tuple] = []
    for p in sorted(results_dir.rglob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("schema") == 1:
            parsed.append((p, data))
    if not parsed:
        print(f"no shard result files (schema 1) found under {results_dir}")
        return 1

    suites = build_suites()
    if duplicate_suite_ids(suites):
        print("catalog has duplicate suite ids; fix build_suites() first")
        return 1
    fingerprint = catalog_fingerprint(suites)
    suites_by_id = {s.id: s for s in suites}

    shard_counts = {d.get("shard_count") for _, d in parsed}
    if len(shard_counts) != 1 or not isinstance(next(iter(shard_counts)), int):
        problems.append(f"shard files disagree on shard_count: {sorted(map(str, shard_counts))}")
        shard_count = 0
    else:
        shard_count = next(iter(shard_counts))

    shards: dict = {}
    if shard_count >= 1:
        expected_partition = partition_suites(suites, shard_count)
        for path, d in parsed:
            i = d.get("shard_index")
            if not isinstance(i, int) or not (0 <= i < shard_count):
                problems.append(f"{path.name}: shard_index {i!r} outside 0..{shard_count - 1}")
                continue
            if i in shards:
                problems.append(f"shard {i}: reported more than once")
                continue
            shards[i] = d
            if d.get("catalog_fingerprint") != fingerprint:
                problems.append(
                    f"shard {i}: catalog fingerprint mismatch — it partitioned a "
                    f"different catalog (different tree, floors, or commands)")
            want_ids = [s.id for s in expected_partition[i]]
            if d.get("suite_ids") != want_ids:
                problems.append(
                    f"shard {i}: suite set differs from the deterministic "
                    f"partition for index {i}")
            # Shape first: a corrupt file must produce a NAMED problem, not an
            # uncaught exception that hides which shard was corrupt.
            raw_entries = d.get("results")
            if not isinstance(raw_entries, list):
                problems.append(f"shard {i}: results is not a list (corrupt result file)")
                raw_entries = []
            entries = [e for e in raw_entries if isinstance(e, dict)]
            if len(entries) != len(raw_entries):
                problems.append(
                    f"shard {i}: {len(raw_entries) - len(entries)} non-object "
                    f"result entr(ies) (corrupt result file)")
            result_ids = sorted(e.get("id", "") for e in entries)
            if result_ids != sorted(d.get("suite_ids", [])):
                problems.append(
                    f"shard {i}: results do not cover its suite set exactly "
                    f"(early stop, or a result for a suite it did not own)")
            claimed = d.get("executed_total")
            actual = sum((e.get("executed") or 0) for e in entries)
            if claimed != actual:
                problems.append(f"shard {i}: executed_total {claimed} != per-suite sum {actual}")
            # Statuses are an allowlist, not a denylist: rejecting only the
            # literal FAIL would let a corrupt file carrying any OTHER value
            # (say NOT_RUN) count as complete passing coverage — fail-open in
            # the acceptance instrument (sol-critic #436 round 1).
            unknown = [f"{e.get('id')}={e.get('status')!r}"
                       for e in entries
                       if e.get("status") not in ("PASS", "FAIL", "SKIP")]
            if unknown:
                problems.append(
                    f"shard {i}: unrecognized status(es): {', '.join(unknown)}")
            # A recognized status can still prove nothing ran (sol-critic #436
            # round 2): a fabricated PASS with executed 0 clears every other
            # check, and a fabricated suite-level SKIP is only legitimate for
            # suites that HAVE a suite-level skip gate. The verifier holds the
            # real catalog, so it re-checks both against the suite's own
            # configuration — the same quantities coverage_verdict enforced
            # inside the shard.
            for e in entries:
                suite = suites_by_id.get(e.get("id"))
                if suite is None:
                    continue  # already named by the suite-set checks
                status = e.get("status")
                executed = e.get("executed")
                if status == "PASS":
                    # type() is int, NOT isinstance: bool subclasses int, so a
                    # corrupt `executed: true` would satisfy isinstance and
                    # clear a floor of 1 (sol-critic #436 round 4). Negative
                    # counts are equally meaningless and refused.
                    if suite.expected is not None:
                        if (type(executed) is not int or executed < 0
                                or executed < suite.expected):
                            problems.append(
                                f"shard {i}: {suite.id} PASS with executed "
                                f"{executed!r} below its floor {suite.expected}")
                    elif suite.kind in ("pytest", "vitest") and (
                            type(executed) is not int or executed < 1):
                        problems.append(
                            f"shard {i}: {suite.id} PASS with no executed tests")
                elif status == "SKIP" and not (suite.db_gated or suite.opt_in_env):
                    problems.append(
                        f"shard {i}: {suite.id} SKIP but the suite has no "
                        f"suite-level skip gate")
            failed = [e.get("id") for e in entries if e.get("status") == "FAIL"]
            if failed:
                problems.append(f"shard {i}: FAILED suites: {', '.join(map(str, failed))}")
            elif d.get("any_fail"):
                problems.append(f"shard {i}: any_fail set without a FAIL row (corrupt result file)")
        missing = [i for i in range(shard_count) if i not in shards]
        if missing:
            problems.append(f"missing shard result(s): {missing}")

        covered = [sid for i in sorted(shards) for sid in shards[i].get("suite_ids", [])]
        catalog_ids = [s.id for s in suites]
        if not missing and sorted(covered) != sorted(catalog_ids):
            never_ran = sorted(set(catalog_ids) - set(covered))
            extras = sorted(set(covered) - set(catalog_ids))
            problems.append(
                f"union of shard suite sets != catalog "
                f"(never ran: {never_ran or '-'}; unknown: {extras or '-'})")

    def _dict_entries(d):
        raw = d.get("results")
        return [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []

    npass = sum(1 for d in shards.values() for e in _dict_entries(d)
                if e.get("status") == "PASS")
    nskip = sum(1 for d in shards.values() for e in _dict_entries(d)
                if e.get("status") == "SKIP")
    executed = sum(d.get("executed_total") or 0 for d in shards.values())
    slowest = max((d.get("wall_seconds") or 0 for d in shards.values()), default=0)
    print(f"shard fan-in: {len(shards)}/{shard_count or '?'} shards, "
          f"suites {npass} PASS {nskip} SKIP, executed {executed}, "
          f"slowest shard {slowest}s")
    if problems:
        for problem in problems:
            print(f"NOT PROVEN: {problem}")
        return 1
    print("PROVEN: every suite in the catalog ran exactly once on one catalog "
          "fingerprint and passed")
    return 0


# --------------------------------------------------------------------------- #
# scoreboard
# --------------------------------------------------------------------------- #
def print_scoreboard(results: List[Result], log_dir: Path, wall: float,
                     selection: str = NO_FILTER) -> None:
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
    total_skipped = sum(r.counts.get("skipped", 0) for r in results)
    print(f"  suites: {npass} PASS  {nfail} FAIL  {nskip} SKIP   "
          f"| test cases passed: {total_tests}  skipped: {total_skipped}   "
          f"| wall: {wall:.1f}s")
    # The counts above are only meaningful next to WHICH suites they counted.
    # Echo the selection so a scoreboard can be checked against the command that
    # produced it instead of being trusted to answer the question that was asked.
    print(f"  filter: {selection}")
    print(f"  logs:   {log_dir}")

    # A suite that only went green on a retry is NOT the same as a green suite,
    # and one annotated row inside a 90-row scoreboard is easy to miss. Call the
    # flakes out by name so "the gate passed" cannot quietly mean "the gate
    # passed the second time".
    flaked = [r.suite.id for r in results if r.status == "PASS" and "flaked" in r.note]
    if flaked:
        print(f"  FLAKED (passed only on retry, so this run's green is soft): "
              f"{', '.join(flaked)}")
    allskip = [r.suite.id for r in results if r.status == "SKIP" and r.got == "0"]
    if allskip:
        print(f"  NO COVERAGE (every test skipped, host artifact absent): "
              f"{', '.join(allskip)}")
    print("=" * len(line))


def main() -> int:
    ap = argparse.ArgumentParser(description="Leaf web demo full gate runner")
    ap.add_argument("--fail-fast", action="store_true",
                    help="stop at the first failing gate (default: run all)")
    ap.add_argument("--continue", dest="cont", action="store_true",
                    help="run every gate even if one fails (this is the default)")
    ap.add_argument("--only", action="append", default=None, metavar="SUBSTR",
                    help="only run suites whose id contains SUBSTR. REPEATABLE: each "
                         "occurrence adds to a union, so `--only a --only b` runs every "
                         "suite matching a OR b. Exits 2 if any SUBSTR matches no suite, "
                         "so a typo can never silently shrink the run.")
    ap.add_argument("--retry", type=int, default=1,
                    help="re-run a FAILED suite up to N more times before calling it "
                         "red (default 1). These suites boot real servers and can flake "
                         "under load; a flake that passes on retry is annotated, not "
                         "masked. Use --retry 0 to capture raw first-attempt results.")
    ap.add_argument("--log-dir", default=None,
                    help="directory for per-suite logs (default: C:/tmp/leaf-web-demo-gates)")
    ap.add_argument("--shard-count", type=int, default=1, metavar="N",
                    help="partition the catalog into N deterministic shards and run "
                         "only one of them (see --shard-index). Suites inside a shard "
                         "still run strictly serially; sharding changes WHICH suites "
                         "this invocation runs, never how any suite runs.")
    ap.add_argument("--shard-index", type=int, default=None, metavar="I",
                    help="0-based shard to run; required when --shard-count > 1. "
                         "Cannot combine with --only: shards must partition the FULL "
                         "catalog or the fan-in completeness proof is meaningless.")
    ap.add_argument("--result-json", default=None, metavar="PATH",
                    help="write this run's machine-readable outcome (catalog "
                         "fingerprint, per-suite verdicts, executed counts) for "
                         "--verify-shard-results to consume.")
    ap.add_argument("--verify-shard-results", default=None, metavar="DIR",
                    help="fan-in mode: read every shard result JSON under DIR "
                         "(recursively), recompute the partition from THIS checkout, "
                         "and exit 0 only when every shard ran exactly its slice of "
                         "the exact same catalog and every suite passed. Runs nothing.")
    args = ap.parse_args()

    if args.verify_shard_results:
        if (args.only or args.result_json or args.shard_count != 1
                or args.shard_index is not None):
            print("--verify-shard-results is a fan-in mode and takes no run flags "
                  "(--only/--shard-count/--shard-index/--result-json)")
            return 2
        return verify_shard_results(Path(args.verify_shard_results))

    if args.shard_count < 1:
        print(f"--shard-count must be >= 1 (got {args.shard_count})")
        return 2
    if args.shard_count > 1 and args.shard_index is None:
        print(f"--shard-count {args.shard_count} needs --shard-index (0..{args.shard_count - 1})")
        return 2
    if args.shard_index is not None:
        if not (0 <= args.shard_index < args.shard_count):
            print(f"--shard-index {args.shard_index} outside 0..{args.shard_count - 1}")
            return 2
        if args.only:
            print("--only cannot combine with sharding: shards must partition the "
                  "full catalog, or a suite could silently run nowhere")
            return 2

    default_logroot = Path("C:/tmp") if Path("C:/tmp").exists() else Path.cwd()
    log_dir = Path(args.log_dir) if args.log_dir else (default_logroot / "leaf-web-demo-gates")
    log_dir.mkdir(parents=True, exist_ok=True)

    suites = build_suites()
    # Before selection, not after: a duplicate breaks the catalog itself, so it
    # is still a broken run when `--only` happens to filter the duplicate away.
    # The `N of M` denominator the scoreboard echoes is already wrong.
    dupes = duplicate_suite_ids(suites)
    if dupes:
        for sid in dupes:
            print(f"suite id registered more than once: {sid!r}")
        print("nothing ran: every suite id must be unique. Remove the duplicate "
              "registration in build_suites().")
        return 2
    total = len(suites)
    # Over the FULL catalog, before any selection: every shard of one run must
    # report the same fingerprint, whatever slice it ran.
    fingerprint = catalog_fingerprint(suites)
    only: List[str] = args.only or []
    selection = describe_selection(only)
    if only:
        suites, dead = select_suites(suites, only)
        if dead:
            for pattern in dead:
                print(f"no suites match --only {pattern!r}")
            print(f"nothing ran: every --only substring must match at least one "
                  f"suite. Selection was: {selection}")
            return 2
        selection += f"  -> {len(suites)} of {total} suites"
    elif args.shard_index is not None:
        suites = partition_suites(suites, args.shard_count)[args.shard_index]
        selection = (f"--shard-count {args.shard_count} "
                     f"--shard-index {args.shard_index}"
                     f"  -> {len(suites)} of {total} suites")

    print(f"leaf-web-demo gate runner -- {len(suites)} suites, "
          f"separate processes, logs -> {log_dir}")
    print(f"  selection: {selection}")

    # Preserve the operator's authored_tools.json: the nl-router gate resets it to
    # clean (gitignored runtime pollution otherwise flakes NL routing), but we
    # restore the original after the whole run so nothing is destroyed as a side
    # effect. Captured only when a suite will actually touch it.
    will_reset = any(s.reset_authored for s in suites)
    orig_authored = (AUTHORED_TOOLS.read_bytes()
                     if (will_reset and AUTHORED_TOOLS.exists()) else None)
    authored_existed = AUTHORED_TOOLS.exists()

    results: List[Result] = []
    attempts_by_id: dict = {}
    wall0 = time.perf_counter()
    try:
      for suite in suites:
        print(f"  ... {suite.id:<22} ", end="", flush=True)
        res = run_suite_guarded(suite, log_dir, attempt=1)
        attempts = 1
        # Retry a FAILED integration suite (these boot real servers -> load
        # flakes), including spawn failures — a child that never started is
        # retried exactly like a red one.
        while (res.status == "FAIL" and attempts <= args.retry):
            attempts += 1
            prev_secs = res.seconds
            prev_note = res.note
            res = run_suite_guarded(suite, log_dir, attempt=attempts)
            res.seconds += prev_secs  # cumulative time spent on this row
            if res.status == "PASS":
                res.note = (f"flaked; passed on attempt {attempts}/{args.retry + 1}"
                            + (f" (prev: {prev_note})" if prev_note else "")
                            + (f" ({res.note})" if res.note else ""))
        if res.status == "FAIL" and attempts > 1:
            res.note = (f"FAIL after {attempts} attempts"
                        + (f" ({res.note})" if res.note else ""))
        attempts_by_id[suite.id] = attempts
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
    print_scoreboard(results, log_dir, wall, selection)

    if args.result_json:
        write_result_json(
            args.result_json, fingerprint=fingerprint, total=total,
            shard_count=args.shard_count,
            shard_index=args.shard_index if args.shard_index is not None else 0,
            selection=selection, suites=suites, results=results,
            attempts_by_id=attempts_by_id, wall=wall)

    # EXIT 0 iff every non-skipped gate passed.
    any_fail = any(r.status == "FAIL" for r in results)
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
