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
  * `harness-audit-high` (kind="npm-audit") tells a real lockfile advisory
    apart from an npmjs.org registry outage: exit 0 is PASS, exit 1 with a
    parseable advisory JSON report is FAIL, and a registry/transport error is
    UNAVAILABLE — retried up to AUDIT_MAX_ATTEMPTS times with backoff INSIDE
    this one suite (never re-entering the outage through the generic
    --retry), never counted as a shard failure, and called out on the
    scoreboard with a "NOT PROVEN BY AUDIT" line. See run_npm_audit_suite for
    the classification (a transport signal requires line-adjacency to its own
    marker — never a bare 5xx digit anywhere in the output) and
    verify_shard_results for what an UNAVAILABLE suite costs the fan-in: never
    a free PROVEN, only a lockfile-blob-sha transitivity proof against a prior
    PASSED audit, or a FAIL naming exactly that.

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
        # `server-customization-adversarial` was for a while the only
        # floor still pinned BELOW its CI executed count -- 17 is the Windows
        # count, Linux CI executes 19 and reports drift -- because no single
        # number is honest for both. Details at each Suite(...) below.
        #
        # Second re-baseline, 2026-08-13: THIRTY-EIGHT more floors had drifted
        # below their executed counts, so the 2026-07-25 pass held for about
        # three weeks. That is the shape of the problem -- a floor decays every
        # time a PR adds a test to an already-registered file and does not
        # touch this line, and nothing turns red when it happens. Same method as
        # above: one file per pytest subprocess from the suite's registered cwd,
        # then re-confirmed through this runner at --retry 0 (wall 713s).
        #   server-agent-approvals 19 -> 24     server-agent-gate 55 -> 57
        #   server-auth 11 -> 14                server-auth-vocab-freeze 11 -> 18
        #   server-author-quota 57 -> 64        server-canonical-worker 24 -> 25
        #   server-authored-execution-live-gate 13 -> 25
        #   server-catalog-version-pin 21 -> 23 server-contract-freeze 8 -> 14
        #   server-customization-async-stage 47 -> 51
        #   server-customization-postgres-contract 9 -> 11
        #   server-customization-runtime 26 -> 50
        #   server-customization-store 8 -> 11  server-dependency-health 17 -> 20
        #   server-deployment-source-identity 9 -> 14
        #   server-drawings-bootstrap 18 -> 23  server-engine-registry-scripts 5 -> 7
        #   server-guest-purge 13 -> 14         server-guest-uploads 57 -> 61
        #   server-hardening-1c 12 -> 57        server-hardening-2b 15 -> 17
        #   server-live-mutation-plan 30 -> 34  server-marathon-orchestration 17 -> 18
        #   server-mcp-gateway-authority 21 -> 55  server-microvm 14 -> 20
        #   server-ops-metrics 13 -> 22         server-overlay-registry 55 -> 56
        #   server-overlay-routes-stream 8 -> 11   server-platform-customize 41 -> 95
        #   server-postgres-container-wiring 48 -> 49
        #   server-session-annex-store 27 -> 28 server-session-store 20 -> 24
        #   server-sessions-router 45 -> 46     server-sessions-routes 41 -> 43
        #   server-submit-latency-metric 11 -> 23  server-turn-runner 24 -> 62
        #   server-wave2-trust-boundary 13 -> 14   server-wave5 15 -> 21
        # EIGHT other suites also execute above their floors and were left
        # alone, because for them the floor is a CI MINIMUM and the local count
        # is the wrong input: each either declares allowed_skip_reasons, is
        # db_gated, or carries a skipif/pytest.skip/importorskip that fires in a
        # different environment. They are server-wave3 (the recorded case:
        # raising 13 to the local count red-failed CI at 13 executed),
        # server-agent-gate-postgres, server-broker-upload-resolver,
        # server-customization-adversarial, server-drawing-authority-postgres,
        # server-guest-fail-closed, server-product-capability-catalog and
        # server-session-store-postgres. The 38 raised above are the complement:
        # each ran with ZERO skips and carries no conditional-skip construct
        # anywhere in its file, and server/tests/conftest.py has no skip
        # machinery either, so there is no environment where they execute fewer.
        # (`server-customization-adversarial`'s own 17-vs-19 note is now stale
        # in its numbers -- Windows measured 21 executed, 2 skipped, on
        # 2026-08-13 -- but not in its conclusion, so its floor still stands.)
        # --- server/ (cwd=server): each file is its OWN pytest process --- #
        Suite("server-backbone", "server tests/test_backbone.py", "pytest", SERVER,
              _py_pytest("tests/test_backbone.py"), 15),
        Suite("server-campaigns", "server tests/test_campaigns_router.py", "pytest", SERVER,
              _py_pytest("tests/test_campaigns_router.py"), 8),
        Suite("server-dependency-health", "server tests/test_dependency_health.py", "pytest",
              SERVER, _py_pytest("tests/test_dependency_health.py"), 20),
        Suite("server-auth", "server test_auth.py", "pytest", SERVER,
              _py_pytest("test_auth.py"), 14),
        Suite("server-auth-envelope", "server tests/test_auth_envelope.py", "pytest", SERVER,
              _py_pytest("tests/test_auth_envelope.py"), 7),
        Suite("server-mcp-gateway-authority",
              "server tests/test_mcp_gateway_authority.py", "pytest", SERVER,
              _py_pytest("tests/test_mcp_gateway_authority.py"), 55),
        Suite("server-mcp-staging-probe",
              "server tests/test_mcp_staging_probe.py", "pytest", SERVER,
              _py_pytest("tests/test_mcp_staging_probe.py"), 24),
        Suite("server-mcp-tenant-registry",
              "server tests/test_tenant_mcp.py", "pytest", SERVER,
              _py_pytest("tests/test_tenant_mcp.py"), 33,
              allowed_skip_reasons=(r"Windows has no POSIX mode bits to assert on",)),
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
              _py_pytest("tests/test_wave5.py"), 21),
        # Card F-7 tripwire (mushy_fold vendor pin) — shipped with PR #474 but
        # never registered here, so it only ever ran by hand until this row.
        # Standardization slice 7b extended it in the same PR that gave it a
        # gate row for the first time.
        Suite("server-vendor", "server tests/test_mushy_fold_vendor_disposition.py",
              "pytest", SERVER,
              _py_pytest("tests/test_mushy_fold_vendor_disposition.py"), 4),
        # standardization slice 7b: the surface-config overlay fold + route.
        Suite("server-surface-config", "server tests/test_surface_config.py", "pytest",
              SERVER, _py_pytest("tests/test_surface_config.py"), 10),
        Suite("server-grant-admin-authority", "server tests/test_grant_admin_authority.py",
              "pytest", SERVER, _py_pytest("tests/test_grant_admin_authority.py"), 9),
        Suite("server-microvm", "server tests/test_hardening_2c_microvm.py", "pytest", SERVER,
              _py_pytest("tests/test_hardening_2c_microvm.py"), 20),
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
              _py_pytest("tests/test_agent_gate.py"), 57),
        # W14 admin self-edit lane (R7): branch-only platform-repo writes,
        # fundamental-path co-sign, landing handoff. Own process: it builds
        # real git repos and toggles the R7 rollout env.
        Suite("server-platform-customize", "server tests/test_platform_customize.py",
              "pytest", SERVER, _py_pytest("tests/test_platform_customize.py"), 95),
        Suite("server-agent-router", "server tests/test_agent_router.py", "pytest", SERVER,
              # Four section-18 tests are intentionally parked because the
              # section-2.1 lane replaced that surface. Pin the complete reason
              # and require all 26 active tests, so any different or additional
              # skip fails the gate and this debt cannot grow silently.
              _py_pytest("tests/test_agent_router.py"), 26,
              allowed_skip_reasons=(_PARKED_AGENT_ROUTER_REASON,)),
        Suite("server-sessions-router", "server tests/test_sessions_router.py", "pytest", SERVER,
              _py_pytest("tests/test_sessions_router.py"), 46),
        Suite("server-context-packet", "server tests/test_context_packet.py", "pytest", SERVER,
              # 16 -> 17 on 2026-09-02: the packet's `checkout` now nulls an
              # ELAPSED lease, pinned in both states. Nothing here skips, so the
              # local count IS the CI count.
              _py_pytest("tests/test_context_packet.py"), 17),
        Suite("server-contract-freeze", "server tests/test_contract_freeze.py", "pytest", SERVER,
              _py_pytest("tests/test_contract_freeze.py"), 14),
        Suite("server-auth-vocab-freeze", "server tests/test_auth_vocab_freeze.py", "pytest",
              SERVER, _py_pytest("tests/test_auth_vocab_freeze.py"), 18),
        Suite("server-billing-tiers", "server tests/test_billing_tiers.py", "pytest", SERVER,
              _py_pytest("tests/test_billing_tiers.py"), 30),
        # The per-tenant DAILY AUTHORING cap. Shipped without a gate entry, the
        # same gap the site-demo lane above had: only the WEB half of this
        # feature (web-author-quota-gate) was registered, so every server-side
        # test of the cap only ever ran by hand. The cap is a precondition for
        # stranger-facing authoring, so it does not get to be unenforced in CI.
        Suite("server-author-quota", "server tests/test_author_quota.py", "pytest", SERVER,
              _py_pytest("tests/test_author_quota.py"), 64),
        Suite("server-job-lanes", "server tests/test_job_lanes.py", "pytest", SERVER,
              _py_pytest("tests/test_job_lanes.py"), 12),
        # The T1 overlay lane's SERVER half. Only its platform/ half was
        # registered (platform-static picked up test_overlay_store_static.py);
        # these five files, 115 tests, ran nowhere in CI from the day the lane
        # shipped. That is how two live-only defects reached staging under a
        # green gate: the back-edge allowlist gap and the stdlib-platform
        # import shadow, both found by a real chat turn, neither by CI.
        # Floors are the executed counts measured on this tree 2026-08-04 with
        # no DATABASE_URL; every one of these files is dependency-free.
        # Live-APS catalog authority (2026-08-10): none of these four files was
        # registered, so the seam that gates real-USD APS routing (deps
        # provenance fold, router consumer, aps_live projection, generation
        # pins) ran nowhere in CI -- the same silent-gap failure mode the
        # comment above records. Floors are collected counts on this tree;
        # all four files are dependency-free (monkeypatch + tmp stores only).
        Suite("server-exact-write-pins", "server tests/test_exact_write_pins.py", "pytest",
              SERVER, _py_pytest("tests/test_exact_write_pins.py"), 11),
        Suite("server-catalog-read-fallback", "server tests/test_catalog_read_fallback.py",
              "pytest", SERVER, _py_pytest("tests/test_catalog_read_fallback.py"), 21),
        Suite("server-instant-catalog-metadata",
              "server tests/test_instant_catalog_metadata.py", "pytest", SERVER,
              _py_pytest("tests/test_instant_catalog_metadata.py"), 16),
        Suite("server-catalog-source-provenance",
              "server tests/test_catalog_tool_source_provenance.py", "pytest", SERVER,
              _py_pytest("tests/test_catalog_tool_source_provenance.py"), 24),
        Suite("server-overlay-propose", "server tests/test_overlay_propose.py", "pytest",
              SERVER, _py_pytest("tests/test_overlay_propose.py"), 13),
        Suite("server-overlay-decision", "server tests/test_overlay_decision.py", "pytest",
              SERVER, _py_pytest("tests/test_overlay_decision.py"), 21),
        Suite("server-overlay-routes-stream", "server tests/test_overlay_routes_stream.py",
              "pytest", SERVER, _py_pytest("tests/test_overlay_routes_stream.py"), 11),
        Suite("server-overlay-stream", "server tests/test_overlay_stream.py", "pytest",
              SERVER, _py_pytest("tests/test_overlay_stream.py"), 18),
        Suite("server-overlay-registry", "server tests/test_overlay_registry.py", "pytest",
              SERVER, _py_pytest("tests/test_overlay_registry.py"), 56),
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
              _py_pytest("tests/test_guest_uploads.py"), 61),
        # The APS-free DWG read lane (dwg2dxf -> dxf_intake) + engine toggle.
        # FIVE real-binary tests run wherever their tools are installed (the app
        # container ships them; see deploy/Dockerfile.app) and skip with these
        # exact allowlisted reasons everywhere else. The second reason covers the
        # two CAGE tests, which need util-linux's prlimit/setpriv as well as
        # dwg2dxf; the third covers the two seccomp-specific real tests, which
        # additionally need the compiled syscall-denylist file
        # deploy/Dockerfile.app bakes in — the cage and its seccomp filter are
        # what bound a memory-corruption exploit in the parser, so none of these
        # five may be allowed to skip for any OTHER reason.
        # Floor 25, not 22: 30 tests, of which exactly those 5 are host-gated, so
        # 25 is the count that must run EVERYWHERE. Raising it is what makes a
        # silently-lost cage or seccomp test a red gate rather than a smaller
        # green number.
        Suite("server-dwg-local-extract", "server tests/test_dwg_local_extract.py", "pytest",
              SERVER, _py_pytest("tests/test_dwg_local_extract.py"), 25,
              allowed_skip_reasons=(
                  r"dwg2dxf binary not installed on this host",
                  r"the real dwg2dxf plus util-linux prlimit/setpriv are not "
                  r"installed on this host",
                  r"needs the real app image: dwg2dxf, prlimit/setpriv, AND the "
                  r"compiled seccomp filter deploy/Dockerfile.app bakes in",
              )),
        # The cross-process fence probe uses POSIX fcntl and therefore skips on
        # Windows operator boxes. Linux CI executes it. Keep the Windows run
        # honest with the exact measured floor for every portable test and an
        # allowlist for only that named deployment-contract skip.
        Suite("server-guest-fail-closed", "server tests/test_guest_fail_closed.py", "pytest",
              SERVER, _py_pytest("tests/test_guest_fail_closed.py"), 36,
              allowed_skip_reasons=(r"fcntl is a Linux deployment contract",)),
        Suite("server-guest-purge", "server tests/test_guest_purge.py", "pytest", SERVER,
              _py_pytest("tests/test_guest_purge.py"), 14),
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
              "pytest", SERVER, _py_pytest("tests/test_catalog_and_version_pin.py"), 23),
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
              SERVER, _py_pytest("tests/test_canonical_worker.py"), 25),
        Suite("server-marathon-orchestration", "server tests/test_marathon_orchestration.py",
              "pytest", SERVER, _py_pytest("tests/test_marathon_orchestration.py"), 18),
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
              SERVER, _py_pytest("tests/test_agent_approvals.py"), 24),
        Suite("server-approval-consume", "server tests/test_approval_consume.py", "pytest",
              SERVER, _py_pytest("tests/test_approval_consume.py"), 20),
        Suite("server-drawings-bootstrap", "server tests/test_drawings_bootstrap.py", "pytest",
              SERVER, _py_pytest("tests/test_drawings_bootstrap.py"), 23),
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
              _py_pytest("tests/test_hardening_1c.py"), 57),
        Suite("server-hardening-1f", "server test_hardening_1f.py", "pytest", SERVER,
              _py_pytest("test_hardening_1f.py"), 8),
        Suite("server-hardening-2b", "server tests/test_hardening_2b.py", "pytest", SERVER,
              # 17 -> 19 on 2026-08-17: the sandbox child's loader path is now
              # DERIVED from this interpreter (it died rc=127 in ld.so on the
              # CodeBuild image), so an inherited LD_LIBRARY_PATH being scrubbed
              # AND the child still booting are each their own named test.
              _py_pytest("tests/test_hardening_2b.py"), 19),
        # (test_hardening_2c_microvm.py is registered above as "server-microvm";
        # it was listed twice, running the same 14 tests for no added coverage.)
        Suite("server-hardening-3b", "server tests/test_hardening_3b.py", "pytest", SERVER,
              # 14 -> 15 on 2026-09-02: matrix item 7 gained its read-side twin
              # (GET /versions withholds an elapsed lease). Nothing in this suite
              # skips, so the local count IS the CI count.
              _py_pytest("tests/test_hardening_3b.py"), 15),
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
              _py_pytest("tests/test_session_store.py"), 24),
        Suite("server-sessions-routes", "server tests/test_sessions_routes.py", "pytest",
              SERVER, _py_pytest("tests/test_sessions_routes.py"), 43),
        Suite("server-turn-runner", "server tests/test_turn_runner.py", "pytest", SERVER,
              _py_pytest("tests/test_turn_runner.py"), 62),
        # g1a canonical e2e self-skips without a reachable Postgres; gate it the
        # same way as the platform suite so the skip is visible, not silent.
        Suite("server-g1a-canonical-e2e", "server tests/test_g1a_canonical_e2e.py", "pytest",
              SERVER, _py_pytest("tests/test_g1a_canonical_e2e.py"), 1, db_gated=True),
        Suite("server-engine-registry-scripts", "server tests/test_engine_registry_scripts.py",
              "pytest", SERVER, _py_pytest("tests/test_engine_registry_scripts.py"), 7),
        # kind:appbundle activity spec + LINE/TEXT intake (2026-09-01): generic platform
        # capabilities, dependency-free.
        Suite("server-appbundle-activity-spec", "server tests/test_appbundle_activity_spec.py",
              "pytest", SERVER, _py_pytest("tests/test_appbundle_activity_spec.py"), 7),
        Suite("server-dxf-intake-lines-text", "server tests/test_dxf_intake_lines_text.py",
              "pytest", SERVER, _py_pytest("tests/test_dxf_intake_lines_text.py"), 4),
        # W4g engine reach (2026-09-04): the intake -> DXF synthesizer (the
        # pinned inverse of dxf_intake), the drawing DXF route with its three
        # fail-closed legs, and the browser-edited version write-back (card
        # F-3, which had no row until this slice). Dependency-free, no skips.
        Suite("server-intake-dxf", "server tests/test_intake_dxf.py", "pytest", SERVER,
              _py_pytest("tests/test_intake_dxf.py"), 19),
        Suite("server-drawing-dxf-route", "server tests/test_drawing_dxf_route.py", "pytest", SERVER,
              _py_pytest("tests/test_drawing_dxf_route.py"), 10),
        Suite("server-save-edited-version", "server tests/test_save_edited_version.py", "pytest", SERVER,
              _py_pytest("tests/test_save_edited_version.py"), 9),
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
        # Floor 59, re-measured 2026-08-06, the third of the stale ones #490
        # started on. It sat at 46 while the suite executed 59, so 13 could have
        # vanished behind an "(executed-count drift: ...)" note and still reported
        # green. 59 is safe on every runner for the same reason 41 is above: 39
        # test functions plus three `parametrize` decorators over literal lists,
        # and no skipif, pytest.skip, importorskip, or platform branching anywhere
        # in the file, so no environment collects or executes fewer.
        Suite("server-broker-boundary", "server tests/test_broker_boundary.py", "pytest",
              SERVER, _py_pytest("tests/test_broker_boundary.py"), 59),
        Suite("server-live-mutation-plan",
              "server tests/test_live_mutation_plan.py", "pytest", SERVER,
              _py_pytest("tests/test_live_mutation_plan.py"), 34),
        # W4g-3a: the contract v2 (the browser engine's saves through the same
        # closed plan), 42 rows over literal parametrize lists, no skip gates.
        Suite("server-mutation-contract-v2",
              "server tests/test_mutation_contract_v2.py", "pytest", SERVER,
              _py_pytest("tests/test_mutation_contract_v2.py"), 42),
        Suite("server-panel-transforms",
              "server tests/test_panel_transforms.py", "pytest", SERVER,
              _py_pytest("tests/test_panel_transforms.py"), 41),
        Suite("server-cat-litmus-offline",
              "server tests/test_cat_litmus_offline_e2e.py", "pytest", SERVER,
              _py_pytest("tests/test_cat_litmus_offline_e2e.py"), 1),
        Suite("server-authored-execution-live-gate",
              "server tests/test_authored_execution_live_gate.py", "pytest",
              SERVER, _py_pytest("tests/test_authored_execution_live_gate.py"), 25),
        Suite("server-authored-tenant-isolation",
              "server tests/test_authored_tenant_isolation.py", "pytest",
              SERVER, _py_pytest("tests/test_authored_tenant_isolation.py"), 5),
        Suite("server-wave2-trust-boundary",
              "server tests/test_wave2_trust_boundary.py", "pytest",
              SERVER, _py_pytest("tests/test_wave2_trust_boundary.py"), 14),
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
              _py_pytest("tests/test_deployment_source_identity.py"), 14),
        Suite("server-deployment-identity",
              "server tests/test_deployment_identity.py", "pytest", SERVER,
              _py_pytest("tests/test_deployment_identity.py"), 12),
        # Registered WITH the file, per the #29 fix-then-register rule: an
        # unregistered suite runs nowhere, and a deploy-safety gate that runs
        # nowhere is exactly the advisory this one exists to replace. Floor 52 =
        # 6 unparametrized tests + 4 parametrizations over the 9 evaluable
        # task-local state paths (36) + 1 selector test over the single
        # selector-guarded path + 9 over the hand-declared defaults the
        # expression evaluator cannot read. Measured on this tree 2026-08-13.
        # The floor moves with the ledger: each new evaluable path adds 4, each
        # hand-declared one adds 1.
        Suite("server-task-local-state-authority",
              "server tests/test_task_local_state_authority_gate.py", "pytest", SERVER,
              _py_pytest("tests/test_task_local_state_authority_gate.py"), 52),
        Suite("server-emf-metrics-stream", "server tests/test_emf_metrics_stream.py",
              "pytest", SERVER, _py_pytest("tests/test_emf_metrics_stream.py"), 1),
        # Every case is hermetic and unconditional (no skipif, no DB, no
        # subprocess), so the count is the same on every runner.
        # 11 -> 23 on 2026-08-13, MEASURED. 11 was 4 tests + the 7 parametrized
        # cases of the unusable-reading test, correct when it was written at 5
        # test defs and one parametrize; #233 grew the file to 9 defs and two
        # parametrizes for the environment dimension and left the floor alone,
        # so 12 of the 23 cases could have vanished under a green gate.
        Suite("server-submit-latency-metric",
              "server tests/test_submit_latency_metric.py", "pytest", SERVER,
              _py_pytest("tests/test_submit_latency_metric.py"), 23),
        Suite("server-ops-metrics", "server tests/test_ops_metrics.py", "pytest",
              SERVER, _py_pytest("tests/test_ops_metrics.py"), 22),
        Suite("server-unit-economics-ops",
              "server tests/test_unit_economics_ops.py", "pytest", SERVER,
              _py_pytest("tests/test_unit_economics_ops.py"), 4),
        # Ops usage scoreboard (M6). Registered WITH the file, per the #29
        # fix-then-register rule. Both files are hermetic -- every authority is
        # monkeypatched, no DB, no subprocess, no skipif -- so these floors are
        # the exact count on every runner, not a cross-environment minimum.
        # Measured on this tree 2026-09-01: 4 and 3.
        # test_ops_operator_boundary.py had NEVER been registered, so the
        # browser-credential boundary it guards ran nowhere in PR CI; it is
        # registered here because the scoreboard join changed the row shape it
        # pins, and an unpinned shape is how the drift began.
        Suite("server-ops-usage-scoreboard",
              "server tests/test_ops_usage_scoreboard.py", "pytest", SERVER,
              _py_pytest("tests/test_ops_usage_scoreboard.py"), 4),
        Suite("server-ops-operator-boundary",
              "server tests/test_ops_operator_boundary.py", "pytest", SERVER,
              _py_pytest("tests/test_ops_operator_boundary.py"), 3),
        # The other half of the scoreboard's honesty: the swallowed OSError in
        # agent_ledger that the join above could not see through. Covers the two
        # surfaces #865 did not own -- GET /api/usage's agent block and the ops
        # per-tenant rollup -- in both directions (unreadable -> unknown,
        # ABSENT -> a real zero). Measured on this tree 2026-09-01: 8.
        Suite("server-agent-ledger-unreadable",
              "server tests/test_agent_ledger_unreadable.py", "pytest", SERVER,
              _py_pytest("tests/test_agent_ledger_unreadable.py"), 8),
        # P2 telemetry (waves A + B). Floors are the measured local executed
        # counts on 2026-08-04; neither file was registered when it landed,
        # which made the whole telemetry suite invisible to PR CI (review
        # #426 round-2 blocker).
        # 20 -> 24: the anonymous client.exception row (a JS failure on a page
        # that has no principal yet) plus the three label-schema tests.
        # 24 -> 26: the mixed-version pair. A client older than this release
        # emits a 1-to-10-digit component_stack_hash, and the door has to keep
        # it while refusing the same width in the digests that are new here.
        Suite("server-telemetry", "server tests/test_telemetry.py", "pytest",
              SERVER, _py_pytest("tests/test_telemetry.py"), 26),
        Suite("server-telemetry-emits", "server tests/test_telemetry_emits.py", "pytest",
              SERVER, _py_pytest("tests/test_telemetry_emits.py"), 10),
        # The client.exception label vocabulary is mirrored BY HAND across
        # web/src/telemetry.js and routers/telemetry.py, and drift there is
        # silent (a class degrades to "Other" rather than failing). This is
        # the freeze, in the same spirit as server-auth-vocab-freeze.
        # 7 -> 9: the freeze's own parser is now proven able to FAIL (it read
        # single-quoted names only, so a double-quoted class was invisible to
        # it), and the component_stack_hash compat pair is frozen too.
        Suite("server-client-exception-vocab-freeze",
              "server tests/test_client_exception_vocab_freeze.py", "pytest",
              SERVER, _py_pytest("tests/test_client_exception_vocab_freeze.py"), 9),
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
        # Floor 46, re-measured 2026-08-07 after merging origin/main. THE MERGE
        # IS WHY THIS LINE NEEDS READING TWICE: this branch raised it 43 -> 45
        # for two new cases, main separately added
        # test_the_image_asserts_its_own_reconcilers_at_build_time to the same
        # file without raising it, and git auto-merged the two edits to 45 with
        # no conflict. The suite executes 46. A floor left silently one low is
        # precisely the drift this comment exists to prevent, so it is
        # re-MEASURED here rather than inferred from either side of the merge.
        # It sat at 7 while the suite executed 41, which is the exact hazard the
        # note at the top of this list describes: a low floor PASSes with an
        # "(executed-count drift: ...)" note, so 34 of these could have vanished
        # and the gate would still have reported green. Raising it with the
        # tests, in the same commit, is what keeps that from happening again.
        # 48 is safe on every runner because the suite is fully static -- it
        # reads files and parses AST, and carries no skipif, pytest.skip or
        # importorskip at all, so there is no environment where it executes
        # fewer. That is also why it needs no allowed_skip_reasons.
        # 46 -> 48 on 2026-08-07 with the two tests that extend the build-time
        # script-survival guard to Dockerfile.canonical-worker/broker/harness.
        # They read the repository tree (harness/scripts/) as well as file text,
        # which does not change the reasoning above: a checkout always has it,
        # and its absence would fail LOUD rather than skip.
        # 48 -> 49 on 2026-08-13, re-MEASURED. That 2026-08-07 commit added
        # THREE tests to this file (19 -> 22 defs) and raised the floor by two,
        # so it landed one low -- the same one-low shape the paragraph above
        # documents, from the same kind of arithmetic rather than a merge.
        Suite("server-postgres-container-wiring",
              "server tests/test_postgres_container_wiring.py", "pytest", SERVER,
              _py_pytest("tests/test_postgres_container_wiring.py"), 49),
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
        # The gate a LEAF_SESSIONS_STORE promotion out of dual_write_shadow will
        # rest on. Floor is the OFFLINE EXECUTED count (46, measured): the other
        # 41 need a live database and are skip-gated (87 collected), and a floor
        # of 87 would red-fail every runner without DATABASE_URL. Re-measured
        # after six review rounds added offline cases -- a floor left at 15
        # would let most of them vanish and still report green, which is the
        # exact hazard the note at the top of this list describes.
        # upload-authority-postgres.yml asserts separately that the live half
        # did not skip THERE, so the two together cover both environments.
        Suite("server-reconcile-sessions-authority",
              "server tests/test_reconcile_sessions_authority.py", "pytest",
              SERVER, _py_pytest("tests/test_reconcile_sessions_authority.py"), 46,
              allowed_skip_reasons=(
                  r"PostgreSQL integration test requires explicit DATABASE_URL",)),
        # Floor 8, re-measured when the session_annex dependency was added. It
        # was 7; the earlier added case is the one that stops an inferred default
        # default from being re-labelled as an observed setting, so letting it
        # vanish silently would retire the guard and still report green. Moves
        # in lockstep with the assertion in scripts/test_gate_runner.py.
        Suite("server-postgres-authority-inventory",
              "server tests/test_postgres_authority_inventory_contract.py", "pytest",
              SERVER, _py_pytest("tests/test_postgres_authority_inventory_contract.py"), 9),
        # The annex authority the sessions flip strands without. Fully
        # offline: the PostgreSQL halves run against a fake in place of
        # platform.db, so nothing here skips on a no-DB host and the floor
        # is the exact collected count.
        # 27 -> 28 on 2026-08-13, MEASURED. It was never the exact count: the
        # file already had 28 tests when #507 registered it at 27, so the claim
        # above was true of the intent and false of the number.
        Suite("server-session-annex-store",
              "server tests/test_session_annex_store.py", "pytest",
              SERVER, _py_pytest("tests/test_session_annex_store.py"), 28),
        # The regression fence for GitHub secret-scanning alerts #2-#7 (presigned
        # APS reportUrl credentials committed in data/*_receipt.json). Fully
        # offline: it greps the tracked tree, no DB or network. Added alongside
        # the fix rather than after it, so it never becomes a suite that "sat
        # outside the gate entirely" -- see the da-client-credentials note below
        # for what that failure mode looks like when a suite skips registration.
        Suite("server-no-presigned-credentials-tracked",
              "server tests/test_no_presigned_credentials_tracked.py", "pytest",
              SERVER, _py_pytest("tests/test_no_presigned_credentials_tracked.py"), 9),
        # --- da/ (cwd=da) --- #
        Suite("da-store", "da test_store.py", "pytest", DA,
              _py_pytest("test_store.py"), 34),
        Suite("da-multitenant", "da test_multitenant.py", "pytest", DA,
              _py_pytest("test_multitenant.py"), 10),
        # The APS scratch-key collision guard. It shipped with the client.py fix
        # but was NEVER registered here, so it gated nothing: the same defect
        # stayed live in both da/ drivers for as long as the suite "existed".
        # Fully offline — an autouse fixture stubs the credential source and the
        # two APS mint/lookup points, so it needs neither credentials nor
        # network (it used to need both, and only passed on a box holding real
        # APS credentials).
        Suite("da-scratch-key-collision", "da test_scratch_key_collision.py", "pytest", DA,
              _py_pytest("test_scratch_key_collision.py"), 23),
        # Unit tests for da/redact.py, the credential stripper behind the fence
        # above. Fully offline (pure string handling, no APS/network).
        Suite("da-redact", "da test_redact.py", "pytest", DA,
              _py_pytest("test_redact.py"), 13),
        # The ban that keeps a presigned credential out of every COMMITTED
        # *.json under data/. da/redact.py + the drivers' write-time redaction
        # are the fix; this is the gate, and it asserts on the SigV4 query
        # parameters rather than on url shape, because a live credentialed url
        # and a redacted one share a path. Offline (file reads only).
        Suite("da-receipt-no-presigned-urls", "da test_receipt_no_presigned_urls.py",
              "pytest", DA, _py_pytest("test_receipt_no_presigned_urls.py"), 26),
        # Both are fully offline (no APS, no network) but were never registered,
        # so 11 tests sat outside the gate entirely.
        Suite("da-client-credentials", "da test_client_credentials.py", "pytest", DA,
              _py_pytest("test_client_credentials.py"), 6),
        Suite("da-extract-dxf-activity", "da test_extract_dxf_activity.py", "pytest", DA,
              _py_pytest("test_extract_dxf_activity.py"), 5),
        Suite("da-mutation-apply", "da test_mutation_apply.py", "pytest", DA,
              _py_pytest("test_mutation_apply.py"), 24),
        # Windows operator hosts run the non-billable AutoCAD engine canary.
        # Linux CI must still collect the suite and may skip only when the named
        # local AutoCAD runtime or tracked demo DWG is unavailable.
        Suite("da-mutation-apply-accoreconsole",
              "da test_mutation_apply_accoreconsole.py", "pytest", DA,
              _py_pytest("test_mutation_apply_accoreconsole.py"), 1,
              allowed_skip_reasons=(
                  r"local AutoCAD 2026 console and tracked demo DWG are required",
              )),
        # --- executor/ (cwd=REPO ROOT; the instant-execution tree) --- #
        # These ~110 unit tests ran in NO CI workflow: this runner had no
        # executor suite, and build-instant-execution-image.yml's test job runs
        # only the two scripts/ harnesses. That gap let the
        # WeakValueDictionary assignment-lock no-op (fixed in #455) sit
        # undetected until a cryptography bump exposed it locally. One process
        # for the whole tree, unlike the per-file server/ suites: these are
        # hermetic unittest modules (per-test tmp dirs, ephemeral ports, no
        # shared on-disk state or env toggles) authored to co-collect;
        # executor/conftest.py + executor/bench/__init__.py fixed the
        # duplicate-basename collision that used to break exactly that.
        # cwd is the REPO ROOT because test_control_plane.py opens
        # executor/contracts/schemas/*.json relative to it. `-P` (safe path)
        # keeps that cwd OFF sys.path, so the repo's platform/ package cannot
        # shadow the stdlib during pytest plugin import — the window before
        # executor/conftest.py runs and appends the repo root for `executor.*`.
        # Floor 136 is the Windows executed count (142 collected − 2 opt-in
        # Postgres skips − 4 Windows-only environment skips), re-measured
        # 2026-08-07 on this tree.
        # Linux CI executes 140 (the POSIX/chmod/symlink probes run there) and
        # reports upward drift — the same min-across-environments convention
        # as server-customization-adversarial.
        #
        # The previous floor of 111 was set 2026-08-06 by #487 and then drifted
        # 25 tests, so for a day any executor suite could have lost 25 cases
        # without the gate noticing. Only 8 of those are the capacity-sampler
        # work (#512 plus this change); the other 17 landed between #487 and
        # 2026-08-07 without a re-measure. Measured, not computed: edd41b0
        # executed 128 and this tree executes 136.
        # The two PostgreSQL adapter integration tests gate on the suite's own
        # POSTGRES_CONTROL_PLANE_TEST_URL opt-in (never a general
        # DATABASE_URL), so they skip on the hermetic gate by design:
        # allowlisted below rather than db_gated, which would throw away the
        # 100+ DB-free tests on every DB-less runner.
        Suite("executor", "executor unit tests (bootstrap+control-plane+runtime+registry+bench)",
              "pytest", REPO,
              [sys.executable, "-P", "-m", "pytest", "executor", "-q", "--color=no",
               "-r", "s", "-p", "no:cacheprovider"], 136,
              allowed_skip_reasons=(
                  r"set POSTGRES_CONTROL_PLANE_TEST_URL to run the PostgreSQL "
                  r"integration test",
                  r"POSIX CPU timers are unavailable",
                  r"POSIX resource limits are unavailable on Windows",
                  r"Windows chmod semantics do not match Fargate mounts",
                  r"Windows symlink creation can require elevated developer mode",
              )),
        # --- tenant customization control plane (one process per file) --- #
        Suite("server-customization-authority", "server customization authority", "pytest",
              SERVER, _py_pytest("tests/test_customization_authority.py"), 7),
        Suite("server-customization-store", "server customization store", "pytest",
              SERVER, _py_pytest("tests/test_customization_store.py"), 11),
        Suite("server-customization-reconcile", "server customization reconcile", "pytest",
              SERVER, _py_pytest("tests/test_customization_reconcile.py"), 8),
        Suite("server-customization-contract", "server customization contract freeze", "pytest",
              SERVER, _py_pytest("tests/test_customization_contract_freeze.py"), 8),
        Suite("server-customization-runtime", "server customization runtime", "pytest",
              SERVER, _py_pytest("tests/test_customization_runtime.py"), 50),
        Suite("server-customization-postgres-contract",
              "server customization PostgreSQL contract", "pytest",
              SERVER, _py_pytest("tests/test_customization_postgres_contract.py"), 11),
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
        # Async stage queue: reservation/lease/worker semantics AND the terminal
        # failure-visibility guards (a harness-answered job failure must FAIL the
        # change set on attempt 1 with the harness's reason readable from stage
        # status). Was never registered, so none of it ran in CI.
        Suite("server-customization-async-stage", "server customization async stage", "pytest",
              SERVER, _py_pytest("tests/test_customization_async_stage.py"), 51),
        Suite("server-platform-release-policy", "server platform release policy", "pytest",
              SERVER, _py_pytest("tests/test_platform_release_policy.py"), 14),
        # --- operator control plane (cwd=server): each file its OWN process --- #
        #
        # REGISTERED 2026-08-29. Until this block, server/tests/test_operator_*.py
        # ran in NO CI workflow and NO Suite here -- 16 files, 271 tests, the
        # entire operator control-plane contract (production unreachability,
        # the egress boundary, authority and principals, the worker boundary),
        # invisible to PR CI. Three separate pins rotted through that hole
        # before anyone noticed: PR #746 mounted POST /api/operator/worker/cancel
        # without amending the route pin, #810 changed agent_policy.json without
        # re-pinning its content SHA, and #680 retired the production deploy's
        # two-person rule without updating the two O5 workflow-string pins.
        # #815 found the same hole from the other side: the fastapi 0.110
        # _IncludedRouter reshape had silently blinded the route walk to 6 of
        # 204 leaves, and CI was green throughout.
        #
        # FLOORS ARE MEASURED, not collected: one run of all 16 files on
        # 2026-08-29 (237 passed, 34 skipped, 0 failed), per-file executed
        # counts taken from its junit XML. Every one of the 34 skips carries
        # the SAME reason -- LEAF_OPERATOR_TEST_DATABASE_URL unset -- and no
        # workflow in this repo sets that variable, so CI executes exactly
        # these counts too. Suites with no skip machinery in their file get an
        # EMPTY skip allowlist, so any future environment-gated skip FAILS the
        # suite rather than quietly eroding its count.
        Suite("server-operator-authority",
              "server tests/test_operator_authority.py", "pytest",
              SERVER, _py_pytest("tests/test_operator_authority.py"), 7,
              allowed_skip_reasons=(r"LEAF_OPERATOR_TEST_DATABASE_URL not set",)),
        Suite("server-operator-credential-rotate",
              "server tests/test_operator_credential_rotate.py", "pytest",
              SERVER, _py_pytest("tests/test_operator_credential_rotate.py"), 19,
              allowed_skip_reasons=(r"LEAF_OPERATOR_TEST_DATABASE_URL not set",)),
        Suite("server-operator-egress-boundary",
              "server tests/test_operator_egress_boundary.py", "pytest",
              SERVER, _py_pytest("tests/test_operator_egress_boundary.py"), 27),
        Suite("server-operator-external-write",
              "server tests/test_operator_external_write.py", "pytest",
              SERVER, _py_pytest("tests/test_operator_external_write.py"), 22,
              allowed_skip_reasons=(r"LEAF_OPERATOR_TEST_DATABASE_URL not set",)),
        Suite("server-operator-identity-bindings",
              "server tests/test_operator_identity_bindings.py", "pytest",
              SERVER, _py_pytest("tests/test_operator_identity_bindings.py"), 12),
        Suite("server-operator-integration-drills",
              "server tests/test_operator_integration_drills.py", "pytest",
              SERVER, _py_pytest("tests/test_operator_integration_drills.py"), 4),
        Suite("server-operator-overlay-runbook",
              "server tests/test_operator_overlay_runbook.py", "pytest",
              SERVER, _py_pytest("tests/test_operator_overlay_runbook.py"), 11,
              allowed_skip_reasons=(r"LEAF_OPERATOR_TEST_DATABASE_URL not set",)),
        Suite("server-operator-principals",
              "server tests/test_operator_principals.py", "pytest",
              SERVER, _py_pytest("tests/test_operator_principals.py"), 3,
              allowed_skip_reasons=(r"LEAF_OPERATOR_TEST_DATABASE_URL not set",)),
        Suite("server-operator-principals-static",
              "server tests/test_operator_principals_static.py", "pytest",
              SERVER, _py_pytest("tests/test_operator_principals_static.py"), 4),
        Suite("server-operator-production-unreachable",
              "server tests/test_operator_production_unreachable.py", "pytest",
              SERVER, _py_pytest("tests/test_operator_production_unreachable.py"), 25),
        Suite("server-operator-runbooks",
              "server tests/test_operator_runbooks.py", "pytest",
              SERVER, _py_pytest("tests/test_operator_runbooks.py"), 15,
              allowed_skip_reasons=(r"LEAF_OPERATOR_TEST_DATABASE_URL not set",)),
        Suite("server-operator-secret-broker",
              "server tests/test_operator_secret_broker.py", "pytest",
              SERVER, _py_pytest("tests/test_operator_secret_broker.py"), 31),
        Suite("server-operator-stage-release",
              "server tests/test_operator_stage_release.py", "pytest",
              SERVER, _py_pytest("tests/test_operator_stage_release.py"), 22,
              allowed_skip_reasons=(r"LEAF_OPERATOR_TEST_DATABASE_URL not set",)),
        Suite("server-operator-vocab-freeze",
              "server tests/test_operator_vocab_freeze.py", "pytest",
              SERVER, _py_pytest("tests/test_operator_vocab_freeze.py"), 13),
        Suite("server-operator-worker-boundary",
              "server tests/test_operator_worker_boundary.py", "pytest",
              SERVER, _py_pytest("tests/test_operator_worker_boundary.py"), 12),
        Suite("server-operator-worker-cancel",
              "server tests/test_operator_worker_cancel.py", "pytest",
              SERVER, _py_pytest("tests/test_operator_worker_cancel.py"), 10),
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
        # host with no DATABASE_URL. The A1 migration/readiness change executes
        # 164 of 166 collected tests. Registering annotation_store_static adds
        # 8 dependency-free tests, so this tree executes 172 of 174 collected;
        # the 2 DB-gated skips remain the ones named in allowed_skip_reasons.
        # 2026-08-04. History of the floor: 96 (2026-07-28, 98 collected);
        # #256 and #401 then added 6 tests that only surfaced as a drift
        # note (#432 moved the floor to 102 for those);
        # test_overlay_store_static.py (20 dependency-free tests, added with
        # the T1 overlay lane) was registered NOWHERE until this entry picked
        # it up. gate-runner-selftest now pins this list against
        # glob("platform/tests/*_static.py") so the next *_static.py file
        # cannot silently run nowhere.
        # Card PIPE-3 added 3 tests here (2 in test_db_readiness_static.py, 1
        # in test_db_schema_proof_static.py) proving their hand-pinned
        # closed-world registry checks -- the migration manifest tail, and
        # "every shipped table has a proven readiness contract" -- still go
        # RED and now name their own file:line plus a remediation instead of
        # a bare AssertionError. Executed count on this tree is now 175, but
        # the floor stays 172: scripts/test_gate_runner.py:68 pins this exact
        # number and is out of this card's file boundary, so 175 reports as a
        # PASS with an "(executed-count drift: expected >= 172, got 175)"
        # note rather than moving the floor unilaterally. Re-measure and move
        # both together in one PR that can touch test_gate_runner.py.
        # 2026-08-28, soft-delete guard card: RE-MEASURED and both floors
        # moved together (this card does touch test_gate_runner.py). 183
        # collected across the 11 *_static.py files, 181 executed on a
        # no-DB host, 2 DATABASE_URL-gated skips (both in
        # test_db_primitives_static.py). That closes the standing 172-vs-175
        # drift as well as adding this card's 6 tests.
        Suite("platform-static", "platform/tests *_static (no DB)", "pytest", REPO_PARENT,
              _py_pytest(f"{repo_name}/platform/tests/test_ledger_static.py")
              + [f"{repo_name}/platform/tests/test_hashing_static.py",
                 f"{repo_name}/platform/tests/test_replay_static.py",
                 f"{repo_name}/platform/tests/test_evidence_freeze_static.py",
                 f"{repo_name}/platform/tests/test_db_primitives_static.py",
                 f"{repo_name}/platform/tests/test_db_readiness_static.py",
                 f"{repo_name}/platform/tests/test_db_schema_proof_static.py",
                 f"{repo_name}/platform/tests/test_overlay_store_static.py",
                 f"{repo_name}/platform/tests/test_ios_ship_schema_static.py",
                 f"{repo_name}/platform/tests/test_annotation_store_static.py",
                 # The soft-delete guard (migration 0050). Registered in the
                 # SAME change that adds the file, because
                 # test_every_platform_static_file_is_registered_in_platform_static
                 # pins this list against glob("*_static.py") -- an unregistered
                 # *_static.py runs nowhere, which is the exact vacuous-green
                 # this list exists to stop.
                 f"{repo_name}/platform/tests/test_soft_delete_guard_static.py"], 181,
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
        # 1 -> 2 on 2026-08-07: the staging relay's convergence contract
        # (a dispatched service is always accounted for, or the relay goes
        # red) is its own named test, so a scoreboard failure says which
        # invariant broke instead of pointing at the mega-test.
        # 2 -> 3 on 2026-08-07: the relay now orders its two services so the
        # one a superseded release abandoned goes first, and that one is
        # EXECUTED — the real dispatch script against a fake gh — so the named
        # test says which service the relay actually deploys first and onto
        # which tag, not merely whether its text still reads that way.
        Suite("build-platform-images-workflow",
              "scripts test_build_platform_images_workflow.py", "pytest",
              SCRIPTS_DIR,
              # 3 -> 4 (PR #519): test_staging_relay_reconciles_a_docs_only_build
              # executes the extracted manifest script against a fake gh and
              # asserts the tag a docs-only merge resolves, then feeds it to the
              # real dispatch script and asserts BOTH services deploy onto it.
              # 17 -> 19 on 2026-09-03: the relay now publishes the supply
              # evidence envelope it already dispatches, so a downstream lane
              # can reuse it (the provider refuses an envelope minted by any
              # other workflow). +1 that the published bytes come from the same
              # $evidence the dispatch output does and the publisher holds no
              # script or token, +1 that the envelope gets its OWN artifact,
              # because the finalizer reads the receipt with _zip_member and
              # that refuses any archive holding more than one file.
              _py_pytest("test_build_platform_images_workflow.py"), 19),
        # Vendored mushy-code integrity (PR #474 review, P2): the pin verifier
        # must be a CI fact, not a manual command. Registered with its suite the
        # day it shipped — no fix-then-register debt. 2 = verify READY + the
        # prove-the-checker-can-fail case.
        Suite("vendor-pin-verify", "scripts test_vendor_pin.py", "pytest",
              SCRIPTS_DIR, _py_pytest("test_vendor_pin.py"), 2),
        Suite("unit-economics-owner-report",
              "scripts test_render_unit_economics_issue.py", "pytest",
              SCRIPTS_DIR, _py_pytest("test_render_unit_economics_issue.py"), 3),
        # Actions cache bucket prune (2026-08-31). The bucket was at 92.7% of
        # its 10 GiB ceiling with 87% of the bytes in unreachable CodeQL
        # overlay-base databases, and the merge gate's 279 MB Playwright entry
        # was only surviving on read traffic. 35 = the whole decision surface
        # EXECUTED against the pure planner: the whitelist boundary (a real key
        # from the live bucket parses; the playwright and node-cache keys the
        # gate depends on do not, so they are never candidates), the
        # keep-newest-N-per-restore-key-group rule, four negative controls that
        # drive each fail-closed guard to refuse with a sabotaged plan plus a
        # positive control proving the refusals are attributable to the
        # sabotage, and the already-gone path (a 404 mid-run is idempotent
        # success, a 403 is not -- 2 of 153 deletes 404'd on the live
        # 2026-08-31 run, and failing on that would make this job red for
        # racing GitHub's own LRU). No network, no clock, no skipIf, so this
        # count is exact on every runner rather than a min across environments.
        Suite("prune-codeql-caches",
              "scripts test_prune_codeql_caches.py", "pytest",
              SCRIPTS_DIR, _py_pytest("test_prune_codeql_caches.py"), 35),
        # Prewarm staging relay (tf design W2). 20 = 13 EXECUTED cases (the
        # eligibility matrix run against the real step body with a fake gh,
        # plus the migration-surface refusal and its fail-closed preview case
        # run against real git repos) + 7 read assertions. The executed half
        # is the point: this relay stages UNMERGED code onto a task holding
        # the real staging role, so "an approval a later CHANGES_REQUESTED
        # overrode does not still buy a stage" has to be a run, not a grep.
        Suite("prewarm-staging-cutover-workflow",
              "scripts test_prewarm_staging_cutover_workflow.py", "pytest",
              SCRIPTS_DIR,
              # 20 -> 22: CI (jq 1.6) rejected `--arg label`, a jq KEYWORD, that
              # a jq 1.8 workstation accepts. Two static guards now scan for the
              # class -- a binding that shadows a jq keyword, and a field
              # suffixed onto an optional iterator -- rather than for that one
              # instance.
              # 22 -> 24: the trigger set is a secret-handling decision, so it
              # is pinned, and so is the rule that the merge preview is READ and
              # never executed while the cross-repo token is in scope.
              # 24 -> 25: STAGE_SERVICES is pinned. It read `web` alone while the
              # app deploy job was self-hosted (a run holds the staging mutex
              # while it waits for a runner); since that job moved to an
              # ephemeral CodeBuild runner it reads `web app`, and a change in
              # either direction must come back through that test.
              # 25 -> 26: the run-URL extraction is guarded (|| true) so a gh that prints no run URL falls back to the mark scan instead of aborting the loop before the next service.
              # (2026-09-05, later still) STAGE_SERVICES reads `web`: the app prewarm succeeds but its merge did not reuse it because main moved under the PR (stale stage); the pin names freshness as the way back.
              _py_pytest("test_prewarm_staging_cutover_workflow.py"), 26),
        # Merge-queue group controller (slice C: mq-review, mq-supply,
        # mq-prewarm). 32 = the executed matrix: mq-review's GraphQL
        # pagination and post-check re-read run against a fake gh (a queue
        # entry that only exists on page 2 is exactly what a broken
        # pagination loop would miss), the four-case newest-status-wins
        # matrix over a two-member group (one member with no
        # kimi-critic-review status at all must fail the whole group, not
        # just itself), and the docs-noop recompute runs against a real git
        # repository reusing the same scripts/docs_noop_filter.py the build
        # workflow does. The rest are structural pins with no local
        # executable surface: the provider-check list mirrored from
        # build-platform-images.yml's adopt job, the receipt names and
        # per-service terraform checks (service, image_tag, weights_touched
        # false), the docs-only and migration-refusal exemptions, mq-prewarm's
        # if:always() plus its explicit dependency-failure branch, the
        # concurrency key, least permissions, and that TERRAFORM_REPO_TOKEN
        # never reaches any step but the one that reads the terraform repo.
        Suite("merge-queue-workflow",
              "scripts test_merge_queue_workflow.py", "pytest",
              SCRIPTS_DIR, _py_pytest("test_merge_queue_workflow.py"), 41),
        Suite("platform-release-manifest",
              "scripts test_platform_release_manifest.py", "pytest",
              SCRIPTS_DIR, _py_pytest("test_platform_release_manifest.py"), 88),
        # The staging convergence FINALIZER's own suite. It shipped with the
        # finalizer on 2026-08-13 and was never registered, so ~1100 lines of
        # provider-evidence tests never ran on any PR. It could not run the
        # ordinary way: the subject imports `scripts.platform_release_manifest`,
        # which needs the repo root importable, and the repo root carries a
        # `platform/` package that shadows the stdlib module pytest's own
        # plugins import at load time. The suite now widens sys.path from inside
        # the test file, after its stdlib preload, so it runs from SCRIPTS_DIR
        # like every other scripts suite. Registered per the #29
        # fix-then-register rule. 61 = the 54 that existed unregistered, minus
        # the 2 arrival-source tests whose contract this PR replaced, plus the 9
        # covering the recorded-drift arrival block (exact main reads no tree,
        # product drift is recorded not refused, non-surface drift stays clean,
        # an addition into a surface is caught by blob comparison, the
        # surface-parity opt-in still refuses, unusable tree evidence and an
        # absent declared input both fail closed, the broken-ancestry matrix,
        # and the end-to-end receipt carrying arrival_source). Measured on this
        # tree 2026-09-02: 61 passed, 117 subtests passed, 0 skipped. Every test
        # is an in-process fake-provider check with no DB, network or platform
        # gate, so the count is identical in every environment. Re-measured on
        # this tree 2026-09-02 after admitting the provider's deploy_mode into
        # the frozen requested-key set and closing it to {normal, prewarm}:
        # 63 passed, 132 subtests passed, 0 skipped. Re-measured on this tree
        # 2026-09-02 after putting isinstance first on every closed-vocabulary
        # membership test a provider value reaches, so an unhashable list or
        # dict fails closed with a reason code instead of raising a bare
        # TypeError: 66 passed, 222 subtests passed, 0 skipped.
        # 66 -> 68 on 2026-09-03: a prewarm receipt is admitted as well-formed
        # (that is #945's closed set) but refused at SELECTION, once it has
        # matched THIS supply, because a prewarm leg ends before the flip. Two
        # tests: a matching prewarm refused, and an UNRELATED prewarm in the
        # same window filtered rather than fatal - the second is the regression
        # test for refusing it at shape-validation time instead, which aborted
        # the finalizer on a receipt belonging to some other release.
        # 68 -> 71 on 2026-09-03: the auto-detected frontier. A later
        # release still deploying no longer aborts it (the explicit path was
        # already immune, its API window being bounded), activity inside its own
        # window still does, and an absent frontier is now a different reason
        # from an ambiguous one. Measured on the rebased tree, not derived:
        # 71 passed, 224 subtests passed, 0 skipped.
        Suite("platform-staging-convergence",
              "scripts test_platform_staging_convergence.py", "pytest",
              SCRIPTS_DIR, _py_pytest("test_platform_staging_convergence.py"), 83),
        Suite("production-web-release",
              "scripts test_production_web_release.py", "pytest",
              SCRIPTS_DIR, _py_pytest("test_production_web_release.py"), 17),
        # Registered with its suite the day it shipped, no fix-then-register
        # debt. The reconcile planner is pure evidence resolution driving a fake
        # provider, with no DB, network or platform gate, so the count is
        # identical in every environment. Measured on this tree 2026-09-03:
        # 14 passed, 17 subtests passed, 0 skipped.
        Suite("reconcile-staging-fleet",
              "scripts test_reconcile_staging_fleet.py", "pytest",
              # 14 -> 22 on 2026-09-03 with arming: the plan now resolves the
              # relay's published envelope and says whether it may be
              # dispatched, and the lane's own shape is pinned here (it ships
              # DORMANT, exactly one step can mutate, and that step is gated on
              # the arm decision). +1 regression that every step carries the
              # release's real per-service immutable_lookup_tag: the tag used to
              # come from a CLI flag the workflow never passed, so every
              # constructed dispatch read `image_tag=`, and the provider deploys
              # whatever tag it is handed.
              # 22 -> 23 on 2026-09-03: BOTH relay envelopes gate armability.
              # The three v3 dispatch inputs travel together, so a lane holding
              # only the supply envelope is refused at the provider's
              # digest-aware consumer-contract gate before any credential.
              SCRIPTS_DIR, _py_pytest("test_reconcile_staging_fleet.py"), 23),
        # These entrypoint checks shipped with the instant-execution container
        # contract but were never registered in the aggregate gate. The mode
        # checks are hermetic file reads, so their measured counts are exact.
        Suite("instant-app-entrypoint",
              "scripts test_instant_app_entrypoint.py", "pytest",
              SCRIPTS_DIR, _py_pytest("test_instant_app_entrypoint.py"), 2),
        Suite("instant-harness-entrypoint",
              "scripts test_instant_harness_entrypoint.py", "pytest",
              SCRIPTS_DIR, _py_pytest("test_instant_harness_entrypoint.py"), 2),
        # This is a main() assert script, not a pytest collection target.
        Suite("instant-execution-container-contract",
              "scripts test_instant_execution_container_contract.py", "script",
              SCRIPTS_DIR,
              [sys.executable, "test_instant_execution_container_contract.py"], None),
        # --- the gate runner's own spawn-failure/retry behavior (this file) --- #
        # Floor 57: the 29 measured 2026-07-28, plus the 18 sharding tests
        # (partition determinism, catalog fingerprint incl. toolchain-path
        # canonicalization, the glob pin on platform/tests/*_static.py, shard
        # CLI rejections, and the fan-in verifier's accept cases + refusal of
        # every corruption class incl. below-floor PASS and ungated SKIP),
        # measured on this tree 2026-08-04; plus the 10 tree-bound gate-proof
        # tests (emission refusal on dirty/non-git checkouts, verification of
        # every forgery class, proof CLI rejections, the emit-only-on-PROVEN
        # wiring, and the test-gate.yml reuse-shape pin), measured 2026-08-05;
        # plus the executor-suite registration pin (floor mirror, cwd/-P
        # shape, opt-in Postgres allowlist, and the two collision-breaking
        # package anchors), measured 2026-08-05; plus the no-apt pin on the
        # workflow's chromium step (the shard-makespan fix, 2026-08-05).
        # Every test here is a pure in-process file/behaviour check with no DB,
        # network or platform gate, so this floor IS the count in every
        # environment, not a lowest-common-denominator across them.
        Suite("gate-runner-selftest", "scripts test_gate_runner.py", "pytest",
              # 59 -> 60 on 2026-08-17: the catalog fingerprint now normalizes
              # each cwd against REPO, so a runner that hands every job its own
              # checkout root is its own named test.
              # 60 -> 66 on 2026-09-02: three PRs added tests here and none
              # moved the floor, so the runner that exists to catch exactly
              # this drift was carrying six tests of its own untracked.
              # Recovered per-commit rather than absorbed as a lump, because
              # this file's convention is to record WHY a floor moved:
              #   #732 (3d96352f) +3  the two "real refusal is the primary
              #                       note" cases, pytest and vitest, plus the
              #                       scoreboard rendering the floor column as
              #                       a minimum and not an equality
              #   #769 (29051a82) +2  the .js-family vitest skip being
              #                       nameable/allowlistable, and a .jsx skip
              #                       that is NOT allowlisted still failing
              #   #820 (bc7cb183) +1  the operator suites pinned to their
              #                       measured floors
              # The header above still holds: no DB, network or platform gate
              # anywhere in this file, so 66 is the count in every environment
              # rather than a lowest-common-denominator across them, and that
              # is measured on both sides rather than asserted. CI
              # gate-shard-0 of runs 33663393817 (PR #929, whose merge tree is
              # main at 237b90c0) and 33662970009 (an independent branch) each
              # printed `scripts test_gate_runner.py  >=60  66  PASS` with no
              # skip note; a local run on 237b90c0 agrees at 66 executed, 0
              # skipped.
              # 66 -> 73 (fix/gate-audit-registry-outage, round 1, 2026-09-04):
              # +7 for the harness-audit-high registry-outage fix -- clean/
              # real-advisory/recovers-after-two-outages/still-UNAVAILABLE-
              # after-three cases, the named worst-case-wall constant, the
              # scoreboard's NOT PROVEN BY AUDIT line, and the shard fan-in
              # treating UNAVAILABLE as not-FAILED.
              # 73 -> 82 (same branch, round-1 REVIEW FIX, 2026-09-04): +9 for
              # the three round-1 blockers plus the nits, none of which moved
              # this floor when they landed:
              #   blocker 1 (fail-open classifier) +3: the exact EJSONPARSE/
              #     package-version false-positive repro, the four real
              #     line-adjacent transport signals, the severity-aware FAIL
              #     count (folds in nit 5)
              #   blocker 2 (UNAVAILABLE became a reusable green proof) +5:
              #     emit_gate_proof's audit_suites + lockfile_blob_sha,
              #     verify_gate_proof refusing an UNAVAILABLE audit suite, the
              #     fan-in refusing a non-npm-audit UNAVAILABLE as corruption,
              #     failing closed with no prior proof, and proving via
              #     lockfile-blob transitivity against a matching one
              #   blocker 3 (vacuous scoreboard-line test) +1: asserting the
              #     "NOT PROVEN BY AUDIT:" prefix starts its OWN printed line
              #     exactly once per row, which a real mutation (deleting the
              #     callout block) now turns red
              #   nit 4 (NOTE column ballooning the whole table) +0 test but
              #     the truncation is pinned by the blocker-3 rewrite's sibling
              #     assertion; _lockfile_blob_sha's own direct pin: +1
              # 82 -> 85 (same branch, round-2 REVIEW FIX, 2026-09-04): +3
              # for the round-2 blocker, which was that the SHIPPED CI
              # wiring ranked prior proofs backwards. test-gate.yml lists
              # candidates newest-created FIRST and copies them in that
              # order, so every file mtime is its copy time and mtime order
              # is exactly reversed; ranking now reads the artifact id out
              # of the <artifact_id>.json name the workflow writes.
              #   +1 the newest ARTIFACT ID wins over the newest mtime,
              #     written in the order the workflow copies them so the
              #     old ranking would pick the oldest candidate
              #   +1 a filename that carries no artifact id is SKIPPED, not
              #     guessed newest
              #   +1 a proof without a PASS for EVERY registered audit suite
              #     is never verified (absent, UNAVAILABLE and MISSING all
              #     refuse), which is the other half of the same blocker
              # Local run on this tree (worktree C:/tmp/leaf-gate-audit):
              # 85 passed, 0 failed, in 25.58s. Wall time is NOT a contract:
              # three earlier runs of this same suite on this host measured
              # 94s, 140s and 356s, entirely tracking how many other lanes
              # were building at the time. The count is the contract; the
              # clock is not.
              SCRIPTS_DIR, _py_pytest("test_gate_runner.py"), 85),
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
                  # B-C6 conversation e2e: Postgres-gated (needs a live
                  # DATABASE_URL / PG_SESSION_STORE_TEST_URL), one test.
                  ("test/conversation.test.ts", 1),
              )),
        # --- web unit (cwd=web) --- #
        # The web workspace had NO unit runner until the T1 overlay card; its
        # only specs were Playwright e2e, which this gate runs separately. A
        # review found the 14 card tests executing nowhere in CI, so the
        # operator-facing decision surface -- both controls, untrusted text,
        # style sinks -- was unverified on every PR.
        # 35 -> 72: a FULL re-baseline, not an interim bump. The old floor sat
        # far below the real count, which is precisely the hazard the header
        # above describes: the suite could have lost every test of this
        # feature and still reported green. No environment divergence is
        # possible here -- all 72 cases are literal and unconditional, with no
        # skips, no parametrize, and no platform branch, and a collection or
        # import failure makes vitest FAIL rather than report a lower count,
        # so the CI-vs-local gap that pins the pytest floors does not apply.
        # 72 -> 76: cross-path de-duplication (one React crash is one row, and
        # the guard is proven able to decline) plus the boundary's uncapped
        # contract.
        # 76 -> 80: de-duplication that survives a flush. The reviewer's
        # counterexample (19 queued events, then one crash split across the
        # batch boundary) plus the three the hold has to keep true -- a held
        # row still sends when no boundary comes, a late boundary still
        # retracts, and pagehide carries a held row off a dying tab.
        # 80 -> 74: DESCOPED (PR #537). Four review rounds of cross-emitter
        # de-duplication are gone -- the load-bearing assumption (window.onerror
        # and componentDidCatch see the SAME Error object instance) does not
        # hold: a probe against the real ErrorBoundary showed DEV React calling
        # the throwing render function more than once, so the two emitters
        # routinely see two DIFFERENT instances, and the WeakMap-keyed design's
        # signature+time-bucket fallback silently merged distinct same-signature
        # crashes -- data loss, not a fix. The client no longer tries: the
        # global handler and the boundary each emit independently and the specs
        # that pinned the removed machinery (exactly-one-row across three
        # flush/pagehide orderings, dedup_key sharing, same-signature
        # suppression) are gone with it. Measured on this tree 2026-08-08.
        # 74 -> 86: bulk repo-wide floor sweep (89e0de06, "test(gates): absorb
        # measured residual floors", 2026-08-14). That commit folded 38
        # drifted floors into one mechanical pass with no per-suite rationale
        # recorded; this step's provenance beyond the commit itself is
        # unrecovered.
        # 86 -> 180: the floor was never re-measured through two more
        # test-adding PRs (#661 operator console, and the annotation decision
        # projection feature) that took the real count to 160, then PR #670
        # (squash 09112cdd, the post-callback token race fix) added 20 more
        # cases -- 7 in the new authBoot.test.js, 13 in the new
        # createSessionController.test.js -- and its own commit message
        # states "Web unit suite 180/180". Measured on THAT tree 2026-08-17:
        # 24 files, 180 tests, `npm run test:unit` in web/.
        # 180 -> 672: sixteen days of unmeasured drift, found while landing
        # PR #865. 180 held while the tree grew from 24 to 77 test files, so
        # ~490 executed cases -- 73% of the suite -- could have vanished and
        # this gate would still have printed PASS behind an "(executed-count
        # drift: expected 180)" note. The entries above call this the easy
        # case with "no skips anywhere"; that is no longer true, two
        # machine-gated files now skip (see allowed_vitest_skips below). Every
        # OTHER case is still literal and unconditional, no parametrize, no
        # platform branch, and a collection/import failure makes vitest FAIL
        # rather than report a lower count. At the 6ab65b5f snapshot, 672 was
        # the exact executed count without the local wasm artifact. A machine
        # that opted in with CAD_ENGINE_REAL_WASM=1 executed 685 there.
        # Evidence, 2026-09-02, all from gate-shard-2, which is where the
        # packer puts this suite. Five CI runs track the growth monotonically
        # across five merges, and the two trees measured locally match their
        # CI counterparts exactly, so this number is CI's and not this box's:
        #   run 33645303496  #912 merge tree                645 executed
        #   run 33646288071  #911 merge tree                659
        #   run 33649092752  #913 merge tree                660
        #   run 33650443868  #915 merge tree                660
        #   run 33651020047  #914 merge tree == main 6ab65b5f  672
        # #913 and #915 are independent runs of the SAME web tree (#915
        # touched no file under web/src), and both printed
        # `web npm run test:unit (vitest)  >=180  660  PASS  13 skipped`; the
        # local run there agreed exactly at 673 collected / 13 skipped / 660
        # executed. After the right-palette tests landed and the retired
        # single-purpose client-shell tests left, the floor advances again:
        # the integrated tree was 698 collected / 13 skipped / 685 executed.
        # W4d Slice B plus the lossless-handle correction measured 748
        # collected / 17 skipped / 731 executed without the local wasm build,
        # which is the number `expected` below still carries.
        #
        # That comment then went stale while the floor stayed correct: the
        # floor is a MINIMUM (coverage_verdict rule 2), so a growing suite
        # passes with an "(executed-count drift: expected 731)" note and
        # nothing ever forced the prose to keep up. Re-measured on the
        # standardization slice-2 tree (2026-09-03, Windows, no local wasm
        # build): 883 collected / 17 skipped / 866 executed, 90 files passed
        # and 1 skipped. The immediately preceding tree (origin/main
        # 1a49766) measured 820 collected / 17 skipped / 803 executed, so
        # slice 2's +63 is entirely its own new tests.
        #
        # `expected` is deliberately NOT raised here. Raising a floor is a
        # separate decision from correcting a description of the tree, and
        # doing both in a refactor PR would hide one behind the other.
        #
        # The 18 skips are exactly the three allow-listed files below: one
        # real-wasm harness case, sixteen editing-surface cases and the W4g-6
        # batch-atomicity case. An opted-in machine (local wasm build present)
        # executes them instead.
        Suite("web-vitest", "web npm run test:unit (vitest)", "vitest", WEB,
              [_npm(), "run", "test:unit"], 731,
              allowed_vitest_skips=(
                  # Day-3 CAD engine real-build round trip: needs a compiled
                  # wasm artifact, which needs a Rust toolchain, so it exists
                  # only on a machine that ran the build and never on a runner.
                  # Opt in with CAD_ENGINE_REAL_WASM=1. One test.
                  ("src/cad/engineWasmHarness.realwasm.test.js", 1),
                  # Card F-3: the editing-surface acceptance cases drive the
                  # REAL compiled engine through a persistent-child double,
                  # gated on the same machine-local wasm artifact (never on a
                  # runner). Sixteen tests: twelve editing and persistence
                  # cases, the three W4d Draw-group cases, and the lossless
                  # u64-handle collision regression.
                  ("src/cadedit/cadEditSurface.test.jsx", 16),
                  # W4g-6: the worker's `batch` op on the REAL compiled engine
                  # (a three-step fillet in one reply, an atomic refusal, the
                  # bounds, a refused create keeping the document), gated on the
                  # same machine-local pkg-node build. One test; it ran green on
                  # the building machine and its numbers are on PR #1036.
                  ("src/cad/engineBatchAtomic.test.js", 1),
              )),
        Suite("harness-tsc-noemit", "harness npx tsc --noEmit", "tsc", HARNESS,
              [_npx(), "tsc", "--noEmit"], None),
        Suite("harness-tsc-build", "harness npx tsc -p tsconfig.build.json", "tsc", HARNESS,
              [_npx(), "tsc", "-p", "tsconfig.build.json"], None),
        # kind="npm-audit" (not "script"): the advisory-bulk registry endpoint
        # has real outages, and a plain FAIL cannot tell "the lockfile has a
        # real advisory" apart from "npmjs.org answered 503 this minute". See
        # run_npm_audit_suite / _classify_npm_audit for the classification and
        # bounded-backoff contract (worst case AUDIT_WORST_CASE_WALL_S = 140s).
        # --fetch-retries=0 + --fetch-timeout bound ONE npm attempt so this
        # runner's own backoff is what retries, not npm's (default
        # fetch-timeout=300000ms x fetch-retries=2 measured the ~700-840s
        # shard this fix exists to close). --json gives a parseable advisory
        # report so a real finding is never confused with a transport error.
        Suite("harness-audit-high", "harness npm audit (high threshold)",
              "npm-audit", HARNESS,
              [_npm(), "audit", "--audit-level=high", "--json", "--no-fund",
               "--fetch-retries=0", f"--fetch-timeout={AUDIT_FETCH_TIMEOUT_S * 1000}"],
              None),
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
        # These four scripts/test_*.mjs files had no automated consumer at all
        # (no npm script, no workflow) -- hand-run-only. That gap is why a real
        # defect (waitForInteractiveLogin resolving on the pre-navigation URL,
        # racing the operator's Universal Login typing) reached two live D1a
        # staging runs on 2026-08-17 before anyone caught it. All four are pure
        # fakes (mocked playwright/fetch, no network or browser launch), same
        # shape as web-deployed-acceptance-contract above.
        Suite("web-deployed-surface-synthetic-acceptance",
              "web deployed surface synthetic acceptance contract", "script", WEB,
              [_npm(), "run", "check:deployed-surface-synthetic-acceptance"], None),
        Suite("web-deployed-auth0-spa-origin-acceptance",
              "web D1a SPA-origin collector acceptance contract", "script", WEB,
              [_npm(), "run", "check:deployed-auth0-spa-origin-acceptance"], None),
        Suite("web-deployed-auth0-production-spa-origin-acceptance",
              "web D1a production SPA-origin collector acceptance contract", "script", WEB,
              [_npm(), "run", "check:deployed-auth0-production-spa-origin-acceptance"], None),
        Suite("web-collect-staging-auth0-principal-b",
              "web staging Auth0 synthetic principal B collector contract", "script", WEB,
              [_npm(), "run", "check:collect-staging-auth0-principal-b"], None),
        Suite("web-proof-receipt-contract",
              "web proof receipt contract", "script", WEB,
              [_npm(), "run", "check:proof-receipt"], None),
        # The seeded workbench id is the deployed acceptance driver's contract
        # with the surface: it seeds a server-canonical id and requires that
        # exact drawing to open.
        Suite("web-workbench-id-contract",
              "web live workbench drawing id rule", "script", WEB,
              [_npm(), "run", "check:workbench-id"], None),
        # web/src/**/*.test.mjs is invisible to vitest (its include is
        # src/**/*.test.{js,jsx}) and each file needs its own npm script to
        # run at all, so these nine node:test files passed by hand and gated
        # nothing. Found landing #880, whose App.jsx pins had to go in a
        # .test.js file to be seen. error-classify.test.mjs came last: its
        # ConversePanel pin grepped for an `if` branch the panel had already
        # replaced with a copy map, so the pin had to be repaired before it
        # could gate anything.
        Suite("web-app-wiring", "App.jsx structural wiring pins", "script", WEB,
              [_npm(), "run", "check:app-wiring"], None),
        # Standardization slice 13d. ribbonClusters.js has carried an HONESTY
        # CONTRACT in its file header since W4d ("a greyed control with no
        # reason is the gap ToolsPanel's lock-note closed"), and nothing
        # enforced it: a reason map missing a key renders an EMPTY reason, and
        # `undefined` is silent — no test, no build error, no console warning.
        Suite("web-honesty-ladder",
              "every disabled or absent control carries a reason", "script", WEB,
              [_npm(), "run", "check:honesty-ladder"], None),
        # Standardization slice 4b. The guided tours spotlight by data-tour
        # anchor first (className chain as the fallback) and the step ->
        # anchor map lives in the Surface Contract, so an anchor a refactor
        # drops from a shell would fail SILENTLY at runtime: the fallback
        # keeps the tour working and nobody learns the anchor is dead. This
        # gate reads the contract and both shells' source and fails on that.
        Suite("web-tour-anchors",
              "every tour anchor the contract names exists in its shell's source", "script", WEB,
              [_npm(), "run", "check:tour-anchors"], None),
        Suite("web-composer-contract",
              "web composer prompt history + queued turn state", "script", WEB,
              [_npm(), "run", "check:composer"], None),
        Suite("web-project-session-binding",
              "web project session binding controller", "script", WEB,
              [_npm(), "run", "check:project-session-binding"], None),
        Suite("web-markdown-contract", "web markdown inline parser", "script", WEB,
              [_npm(), "run", "check:markdown"], None),
        Suite("web-panel-fold-contract",
              "web transcript fold skips queue bookkeeping events", "script", WEB,
              [_npm(), "run", "check:panel-fold"], None),
        Suite("web-pending-approvals-contract",
              "web pending approvals inbox + resolve contract", "script", WEB,
              [_npm(), "run", "check:pending-approvals"], None),
        Suite("web-route-scene-contract",
              "web site route scene mapping (/ty renders as /app)", "script", WEB,
              [_npm(), "run", "check:route-scene"], None),
        Suite("web-usage-contract", "web usage formatting helpers", "script", WEB,
              [_npm(), "run", "check:usage"], None),
        Suite("web-error-classify-contract",
              "web 413 classifies as too_large with its own panel copy", "script", WEB,
              [_npm(), "run", "check:error-classify"], None),
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
        # Async authoring lifecycle: resume-one-exact-revision, pointer scoping,
        # poll-URL guards, and failure visibility (a failed stage renders the
        # server's error.message instead of an eternal authoring spinner).
        Suite("web-async-author-stage",
              "web async authoring stage lifecycle + failure visibility", "script", WEB,
              [_npm(), "run", "proof:async-author-stage"], None),
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
        # Every vitest test-file extension this repo actually uses, not just
        # .test.ts: web/ is almost entirely .test.js / .test.jsx, so the
        # ts-only pattern made a skip in ANY web test unnameable, and an
        # unnameable skip trips the "reported N but named 0" rule below on a
        # correctly-declared skip. The rule is right; the pattern was partial.
        # Alternation LONGEST-FIRST: with js before jsx, a .test.jsx path was
        # captured truncated to .test.js and could never match its allowlist
        # entry (found live on the F-3 cadedit suite).
        match = re.search(r"(\S+\.test\.(?:tsx|ts|jsx|js|mjs|cjs)).*?(\d+) skipped",
                          output_line)
        if match:
            skipped_files.append((match.group(1).replace("\\", "/"), int(match.group(2))))
    return {"passed": passed, "failed": failed, "skipped": skipped, "got": got,
            "skipped_files": skipped_files}


# --------------------------------------------------------------------------- #
# npm audit: registry-outage-tolerant classification (harness-audit-high)
# --------------------------------------------------------------------------- #
# The advisory-bulk endpoint (registry.npmjs.org/-/npm/v1/security/advisories/bulk)
# has real outages: 503 Service Unavailable, "npm error audit endpoint returned
# an error". That is NOT a lockfile finding, but before this fix the runner
# could not tell the difference: npm's OWN internal retry loop (default
# fetch-retries=2, fetch-timeout=300000ms) turned one outage into a ~700-840s
# shard, the runner's generic --retry re-ran the whole thing once more
# immediately into the same outage, and the suite reported a plain FAIL --
# measured 2026-09-04 on PR #989 (run 33840004101), PR #987 (run 33859010815,
# twice), and PR #1004 (run 33855837981), each ~15 minutes of runner time for
# a result that said nothing about the dependency tree.
#
# Fix: classify every attempt as PASS (exit 0) / FAIL (a JSON advisory report
# at/above the level) / UNAVAILABLE (registry or transport error, no usable
# JSON report) and retry ONLY the UNAVAILABLE case, bounded, entirely inside
# ONE run_suite() call so the outer --retry never re-enters the same outage.
# npm's own retry loop is disabled (--fetch-retries=0) and its per-attempt
# wait is capped (--fetch-timeout) so a single attempt cannot itself balloon;
# this runner does the backoff instead.
#
# Worst-case wall, named so a regression against it reads like a failing test.
# This MUST use the subprocess hard-kill (AUDIT_SUBPROCESS_TIMEOUT_S), never
# npm's own --fetch-timeout hint: --fetch-timeout bounds one HTTP request
# inside npm, not the child process, so it is not what actually stops a stuck
# attempt. A version of this constant computed from AUDIT_FETCH_TIMEOUT_S
# (140s) named a bound the code did not enforce (round-1 nit 1); this is the
# real one:
#   AUDIT_MAX_ATTEMPTS * AUDIT_SUBPROCESS_TIMEOUT_S + sum(AUDIT_BACKOFF_S)
#   =  3 * 90s                  + (20s + 60s)                = 350s
# against the ~1500s two attempts cost today (~4x headroom).
AUDIT_MAX_ATTEMPTS = 3
AUDIT_BACKOFF_S: tuple[int, ...] = (20, 60)   # sleep before internal attempt 2, 3
AUDIT_FETCH_TIMEOUT_S = 20                    # --fetch-timeout bound per attempt
# 45s measured too thin (round-1 nit 2): a healthy attempt through this runner
# took 22.8s, and the raw command 7.6s-15.0s; 45s left under 2x margin over a
# real, non-outage run under CI load, which would misclassify a merely SLOW
# registry as UNAVAILABLE. 90s keeps ~4x margin over the slowest healthy
# measurement while staying a small fraction of npm's own ~700-840s internal
# retry loop this fix exists to bound.
AUDIT_SUBPROCESS_TIMEOUT_S = 90                # hard safety net if npm ignores it
AUDIT_WORST_CASE_WALL_S = (AUDIT_MAX_ATTEMPTS * AUDIT_SUBPROCESS_TIMEOUT_S
                           + sum(AUDIT_BACKOFF_S))  # == 350
AUDIT_ADVISORY_ENDPOINT = "registry.npmjs.org/-/npm/v1/security/advisories/bulk"
# Real reason phrases npm itself prints next to a 5xx transport failure. Used
# ONLY as a same-LINE co-occurrence check (see _audit_transport_signal) —
# never a whole-blob substring test — specifically so this can never fire on
# an unrelated 5xx-shaped number (an EJSONPARSE byte offset, a package version
# like 1.500.0) that happens to share a digit pattern with a real HTTP status.
_AUDIT_REASON_PHRASES: tuple[tuple[str, str], ...] = (
    ("Service Unavailable", "503"),
    ("Bad Gateway", "502"),
    ("Gateway Timeout", "504"),
    ("Internal Server Error", "500"),
)
_AUDIT_ERRNOS = ("ECONNRESET", "ETIMEDOUT", "ECONNREFUSED", "EAI_AGAIN", "ENOTFOUND")
_AUDIT_HTTP5XX = re.compile(r"\b5\d\d\b")


def _audit_transport_signal(combined: str) -> Optional[str]:
    """A REAL registry/transport signal, or None. Every branch below requires
    the number or errno to sit on the SAME LINE as its own marker — never a
    whole-blob regex search — because round 1 caught exactly that shape of
    bug: `_AUDIT_HTTP5XX.search(combined)` matched a bare 500-599 number
    ANYWHERE in npm's output, so an EJSONPARSE byte offset ("...at position
    512...") or a package version ("foo@1.500.0") reclassified a real,
    non-registry failure as a registry outage. The four signal shapes here are
    exactly the ones _classify_npm_audit's docstring promises and nothing
    more; an unrecognized failure still falls through to FAIL."""
    # (a) npm's own explicit "the audit endpoint itself errored" text — this
    # phrase is npm's, never emitted for any other condition, so it needs no
    # line-adjacency check of its own.
    if "audit endpoint returned an error" in combined:
        return "audit endpoint returned an error"
    for line in combined.splitlines():
        # (b1) an HTTP status on the SAME LINE as the advisories endpoint URL.
        if AUDIT_ADVISORY_ENDPOINT in line:
            m = _AUDIT_HTTP5XX.search(line)
            if m:
                return f"HTTP {m.group(0)}"
        # (b2) an "npm warn audit" / "npm error" line carrying a real HTTP
        # reason phrase — the digit on that SAME line if npm printed one,
        # else the phrase's own mapped code.
        if "npm warn audit" in line or "npm error" in line:
            for phrase, code in _AUDIT_REASON_PHRASES:
                if phrase in line:
                    m = _AUDIT_HTTP5XX.search(line)
                    return f"HTTP {m.group(0) if m else code}"
        # (c) a known errno, but ONLY on an npm error line — the same errno
        # text inside, say, a test's own assertion message must never count.
        if "npm error" in line:
            for errno in _AUDIT_ERRNOS:
                if errno in line:
                    return f"errno {errno}"
    return None


def _classify_npm_audit(rc: int, stdout: str, stderr: str,
                        timed_out: bool) -> tuple[str, str]:
    """Classify one npm-audit attempt. Never guesses: a real JSON advisory
    report wins on its own terms regardless of exit code text, and an
    unrecognized failure with no report and no transport signal FAILS closed
    rather than being swallowed as merely unavailable."""
    if rc == 0:
        return "PASS", ""
    report = None
    if stdout.strip():
        try:
            candidate = json.loads(stdout)
        except json.JSONDecodeError:
            candidate = None
        if isinstance(candidate, dict) and (
                "vulnerabilities" in candidate or "advisories" in candidate):
            report = candidate
    if report is not None:
        # metadata.vulnerabilities carries per-severity counts PLUS its own
        # "total" key (all severities, not just at/above --audit-level) --
        # summing every value would double-count total against itself. This
        # suite's argv hardcodes --audit-level=high (see its Suite(...)
        # registration), so the count that answers "did this cross the gate's
        # own threshold" is high+critical, not the all-severities total: a
        # report with only low/moderate findings that exits non-zero for some
        # OTHER reason must never read as if it broke a threshold it did not
        # reach (round-1 nit 5).
        vulns = report.get("metadata", {}).get("vulnerabilities", {})
        if isinstance(vulns, dict) and isinstance(vulns.get("total"), int):
            total = vulns["total"]
        elif isinstance(vulns, dict):
            total = sum(v for k, v in vulns.items()
                       if k != "total" and isinstance(v, int))
        else:
            total = None
        if isinstance(vulns, dict):
            at_or_above = sum(v for k, v in vulns.items()
                              if k in ("high", "critical") and isinstance(v, int))
        else:
            at_or_above = None
        if total is not None and at_or_above is not None:
            detail = (f"{at_or_above} advisories at/above high "
                      f"({total} total across all severities)")
        elif total is not None:
            detail = f"{total} advisories reported (all severities)"
        else:
            detail = "advisories reported"
        return "FAIL", f"real advisory report: {detail}"
    combined = f"{stdout}\n{stderr}"
    if timed_out:
        return "UNAVAILABLE", (
            f"registry unavailable (no response within "
            f"{AUDIT_SUBPROCESS_TIMEOUT_S}s): {AUDIT_ADVISORY_ENDPOINT}")
    signal = _audit_transport_signal(combined)
    if signal is not None:
        return "UNAVAILABLE", f"registry unavailable ({signal}): {AUDIT_ADVISORY_ENDPOINT}"
    last = next((ln for ln in combined.strip().splitlines()[::-1] if ln.strip()), "")
    return "FAIL", f"unrecognized audit failure (exit {rc}, no JSON report): {last[:160]}"


def _cheap_lockfile_sha(cwd: Path) -> str:
    """Best-effort last commit that touched this suite's lockfile, purely to
    make the UNAVAILABLE note answer 'has the tree moved since the last green
    run' without a network call. Never raises, never blocks a real result on
    a git hiccup: any failure just reports 'unknown'."""
    lockfile = cwd / "package-lock.json"
    if not lockfile.exists():
        return "unknown"
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", "package-lock.json"],
            cwd=str(cwd), capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",
        )
        sha = out.stdout.strip()
        return sha if (out.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha)) \
            else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def _lockfile_blob_sha(cwd: Path) -> str:
    """The git BLOB hash of cwd/package-lock.json at HEAD — the exact-bytes
    identity the lockfile-transitivity rule is built on (see verify_shard_
    results' UNAVAILABLE handling), distinct from _cheap_lockfile_sha's COMMIT
    hash above: two different commits (different trees) can carry the
    byte-identical lockfile, and it is exactly that case the transitivity rule
    exists to recognize — "same audit input, already proven clean, on some
    EARLIER tree" — so a commit hash would refuse a match a blob hash allows.
    Never raises, returns '' (never a placeholder string) on any failure, so a
    caller comparing shas never gets a false-equal from two unreadable results
    both defaulting to the same sentinel."""
    lockfile = cwd / "package-lock.json"
    if not lockfile.exists():
        return ""
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "HEAD:./package-lock.json"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",
        )
        sha = out.stdout.strip()
        return sha if (out.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha)) \
            else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _run_npm_audit_attempt(argv: List[str], cwd: Path, logf, attempt: int) -> tuple:
    """One bounded npm-audit subprocess call. Returns
    (rc, stdout, stderr, timed_out, spawn_err)."""
    spawn_command, use_shell, shell_executable = normalize_spawn_command(argv)
    logf.write(f"$ (cwd={cwd}) {' '.join(argv)}\n"
              f"$ internal attempt {attempt}/{AUDIT_MAX_ATTEMPTS} "
              f"@ {time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
    logf.flush()
    timed_out = False
    spawn_err = ""
    try:
        proc = subprocess.run(
            spawn_command, cwd=str(cwd), env=clean_env(),
            capture_output=True, text=True, timeout=AUDIT_SUBPROCESS_TIMEOUT_S,
            shell=use_shell, executable=shell_executable,
            encoding="utf-8", errors="replace",
        )
        stdout, stderr, rc = proc.stdout or "", proc.stderr or "", proc.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = ((exc.stderr or "") if isinstance(exc.stderr, str) else "") + \
            f"\n[TIMEOUT >{AUDIT_SUBPROCESS_TIMEOUT_S}s]"
        rc, timed_out = 124, True
    except OSError as exc:
        spawn_err = f"{type(exc).__name__}: {str(exc)[:160]}"
        stdout, stderr, rc = "", f"[SPAWN FAILURE] {spawn_err}", 127
    logf.write(stdout + "\n" + stderr + "\n\n")
    logf.flush()
    return rc, stdout, stderr, timed_out, spawn_err


def run_npm_audit_suite(suite: Suite, log_path: Path) -> Result:
    """kind == 'npm-audit': up to AUDIT_MAX_ATTEMPTS bounded attempts with
    backoff between UNAVAILABLE (registry outage) results, all inside this one
    call so the runner's generic --retry (immediate, no backoff) never
    re-enters the same outage. A real advisory report FAILS on the first
    attempt, exactly as before; only UNAVAILABLE is retried."""
    argv = [str(a) for a in suite.argv]
    t0 = time.perf_counter()
    status, note = "FAIL", "no attempt executed"
    with open(log_path, "w", encoding="utf-8", errors="replace") as logf:
        for i in range(1, AUDIT_MAX_ATTEMPTS + 1):
            rc, stdout, stderr, timed_out, spawn_err = _run_npm_audit_attempt(
                argv, suite.cwd, logf, i)
            if spawn_err:
                status, note = "FAIL", f"spawn failure: {spawn_err}"
            else:
                status, note = _classify_npm_audit(rc, stdout, stderr, timed_out)
            if status != "UNAVAILABLE":
                break
            if i < AUDIT_MAX_ATTEMPTS:
                backoff = AUDIT_BACKOFF_S[i - 1]
                logf.write(f"[UNAVAILABLE attempt {i}/{AUDIT_MAX_ATTEMPTS}: {note}; "
                          f"retrying in {backoff}s]\n\n")
                logf.flush()
                time.sleep(backoff)
        seconds = time.perf_counter() - t0
        if status == "UNAVAILABLE":
            sha = _cheap_lockfile_sha(suite.cwd)
            note = (f"{note} after {AUDIT_MAX_ATTEMPTS} attempts; "
                    f"NOT PROVEN BY AUDIT: registry unavailable, lockfile "
                    f"unchanged since {sha}")
        elif status == "PASS" and i > 1:
            note = f"recovered from registry outage on attempt {i}/{AUDIT_MAX_ATTEMPTS}"
        logf.write((note or "(no note)") + "\n")
    return Result(suite, status,
                  {"PASS": "ok", "FAIL": "err", "UNAVAILABLE": "unavailable"}[status],
                  seconds, note=note, log_path=log_path, counts={})


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

    # npm-audit runs its OWN bounded, backed-off attempt loop (see
    # run_npm_audit_suite): a registry outage must never masquerade as a
    # lockfile finding, and it must never re-enter the same outage through the
    # generic --retry below. This bypasses the fault-injection drill and the
    # generic subprocess path entirely; a real advisory report still FAILS.
    if suite.kind == "npm-audit":
        return run_npm_audit_suite(suite, log_path)

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
        if c["failed"] or c["errors"]:
            # The real refusal goes first: a bare EXP/GOT pair on the
            # scoreboard reads as an equality check, but the floor is a
            # skip-adjusted minimum, not an equality, and a genuine test
            # failure must never be left for the numbers alone to explain.
            real_fail = f"suite FAILED: {c['failed']} failed, {c['errors']} errors"
            note = (real_fail + " " + note) if note else real_fail
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
        # A FAIL row must never reach the scoreboard with an empty note: an
        # empty note leaves the reader to infer the reason from the EXP/GOT
        # columns, which is exactly the misreading this card exists to close.
        if not passed and not note:
            note = "suite FAILED"
        return Result(suite, "PASS" if passed else "FAIL", str(c["got"]), seconds,
                      note=note.strip(), log_path=log_path, counts=c)

    if suite.kind == "vitest":
        c = parse_vitest(out)
        passed = rc == 0 and c["failed"] == 0
        note = ""
        if c["failed"]:
            note = f"suite FAILED: {c['failed']} failed"
        if c.get("skipped"):
            note = (note + " " if note else "") + f"{c['skipped']} skipped"
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
        if not passed and not note:
            note = "suite FAILED"
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

# Measured wall seconds per suite -- scheduling weights for the partition,
# nothing more. Only suites >= ~3s are listed; the long tail defaults.
#
# REFRESHED 2026-08-17 from the shard-result JSONs of runs 32051458666 and
# 32047410329 (CodeBuild, post-#649), taking the max of the two. The previous
# table was measured on ubuntu-latest on 2026-08-04/08-06 and had drifted far
# enough to defeat the packer: `web-async-author-stage` (45.8s) and
# `build-platform-images-workflow` (43.8s) were absent entirely and so
# defaulted to 2.0, while `harness-vitest` (36 -> 10.4) and
# `web-version-restore-proof` (100 -> 61.1) were weighted roughly triple and
# double their real cost. The packer therefore believed every shard carried
# 88-100s while real loads ran 58-122s, and gate-shard-1 was the critical path
# in 5 of the 5 runs sampled -- exactly the failure this comment has always
# warned about. Repacking against real durations: makespan 122s -> 84s on run
# A and 121s -> 83s on run B, a 38-39s (31%) cut against a 77s ideal.
#
# The two runs agree to a median of 0.00s and a 90th percentile of 0.20s per
# suite, so these are near-deterministic, not noisy samples. Re-measure the
# same way after any runner or cache change: a stale weight costs makespan,
# never correctness, but one shard quietly becomes the critical path.
# gate-runner-selftest pins every key here to a registered suite id so a
# rename cannot strand a weight.
_MEASURED_EST_S = {
    # Operator control-plane suites, MEASURED on the CI runner from the shard
    # logs of run 33283298381 (the registration PR's own first green run), the
    # same provenance as the 2026-08-17 entries below.
    #
    # Only production-unreachable earns an entry: at 3.4s it is the sole
    # operator suite above _DEFAULT_EST_S. It leads because its O5 behavioral
    # test spawns bash 14 times to execute the deploy workflow's own
    # approval-mode branch. The other fifteen all measured 0.4-2.0s on CI, so
    # the default already covers them.
    #
    # The first cut of this table modeled these from a LOADED WINDOWS host and
    # over-weighted five suites by 3-10x (production-unreachable at 34.8 vs the
    # real 3.4): Windows process creation dominated the model and does not
    # exist on the Linux runner. Recorded because the direction is the trap --
    # over-weighting cannot fail a gate, so nothing would have complained while
    # the packer reserved a shard's worth of phantom load.
    "server-operator-production-unreachable": 3.4,
    "web-version-restore-proof": 61.1,
    "server-checkout-crossproc": 60.3,
    "server-backbone": 45.8,
    "web-async-author-stage": 45.8,
    "build-platform-images-workflow": 43.8,
    "server-sessions-router": 27.9,
    "executor": 20.9,
    "server-turn-runner": 15.8,
    "server-task-local-state-authority": 14.0,
    "web-author-quota-gate": 14.0,
    "server-sessions-e2e": 11.2,
    "harness-vitest": 10.4,
    "server-write-loop": 8.9,
    "server-sessions-routes": 7.0,
    "server-wave5": 6.9,
    "server-wave3": 5.9,
    "server-hardening-3b": 5.8,
    # web-pending-approvals-contract carried 7.3 here until #883. Its tests
    # took 22ms; the rest was node:test waiting on telemetry.js's never-
    # unref'd flush timer, misattributed to converse.js when first measured.
    # With that unref'd the suite measures 0.23-0.29s (Windows, 3 runs,
    # 2026-09-02, against 7.33/7.42s on 575bc57 for the A/B), so it takes the
    # default like the other eight .test.mjs suites. Do not re-add it from a
    # pre-#883 measurement.
    "web-vitest": 5.4,
    "server-mcp-gateway-authority": 5.1,
    "server-platform-customize": 4.9,
    "server-wave4": 4.9,
    "harness-tsc-noemit": 4.8,
    "server-microvm": 4.7,
    "server-agent-e2e": 4.0,
    "server-jobs-connection-ownership": 4.0,
    "server-dynamic-loader": 3.9,
    "server-guest-purge": 3.9,
    "web-demo-gate": 3.9,
    "server-guest-uploads": 3.8,
    "harness-tsc-build": 3.7,
    "server-ui-wave": 3.7,
    "server-wave2": 3.6,
    "server-dependency-health": 3.2,
    "server-dwg-local-extract": 3.1,
    "server-submit-latency-metric": 3.1,
    "web-build": 3.1,
    "server-customization-adversarial": 3.0,
    "server-job-lanes": 3.0,
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


def _fingerprint_cwd(cwd: Path) -> str:
    """Every catalog cwd is built from REPO (server/, da/, the repo root, the
    repo PARENT for the platform suites), so what identifies a suite is its
    position RELATIVE to the repo — the absolute prefix in front of it is a
    property of the checkout, not of the catalog.

    Hashing that prefix raw bound the fingerprint to one checkout layout, and
    a per-build checkout root breaks it: CodeBuild-hosted runners check out
    under `/codebuild/output/src<random>/src/...`, a fresh directory PER JOB,
    so on run 32018150977 no two of the eight shards agreed with each other or
    with the fan-in and all eight were refused as "catalog fingerprint
    mismatch" while every suite in them passed. Same class as the argv[0]
    problem `_fingerprint_argv` already solves.

    Separators are normalized to "/" so the token does not depend on the OS
    that computed it, and the result stays fully sensitive: two suites with
    different cwds still hash differently, and a suite that MOVES between
    directories still changes the fingerprint."""
    try:
        rel = os.path.relpath(str(cwd), str(REPO))
    except ValueError:
        # No relative form exists (Windows: a different drive from the repo).
        # Unreachable for a catalog whose every cwd derives from REPO; keep the
        # raw path rather than collapsing two distinct roots onto one token.
        return str(cwd)
    return rel.replace("\\", "/")


def catalog_fingerprint(suites: List[Suite]) -> str:
    """SHA-256 over every field of every suite, so two runs agree on this
    value only when they partitioned the same catalog with the same floors,
    allowlists, and commands.

    It is an intra-run integrity token: compared between the shard jobs and
    the fan-in of a single CI run, or between local runs of one worktree. The
    per-JOB parts of a command are canonicalized out — toolchain LOCATION
    inside argv (see _fingerprint_argv) and the checkout root in front of each
    cwd (see _fingerprint_cwd) — so the value tracks the CATALOG and survives
    a runner that hands every job its own checkout directory."""
    entries = [{
        "id": s.id, "label": s.label, "kind": s.kind, "cwd": _fingerprint_cwd(s.cwd),
        "argv": _fingerprint_argv(s.argv), "expected": s.expected,
        "allowed_skip_reasons": list(s.allowed_skip_reasons),
        "allowed_vitest_skips": [list(pair) for pair in s.allowed_vitest_skips],
        "reset_authored": s.reset_authored, "db_gated": s.db_gated,
        "opt_in_env": s.opt_in_env, "timeout_s": s.timeout_s,
    } for s in suites]
    blob = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# tree-bound gate proof (operator decision D3, 2026-08-05)
#
# The fan-in's green verdict is bound to the git TREE hash of the checkout it
# verified, not the commit SHA. A pull_request gate runs on the PR's merge
# preview; the merge that lands on main mints a NEW commit whose tree is
# byte-identical whenever main did not move in between. Binding the proof to
# the tree lets the push-to-main build recognize "these exact bytes already
# passed the full gate" and skip re-running it, while ANY skew — main moved,
# a different merge result, a hand-edited commit — changes the tree and runs
# the full gate. Precedent: leaf_website #200 binds promotion proof to HEAD.
#
# Trust model: this file only proves CONTENT — that a proof document names the
# expected tree and the exact catalog this checkout derives. WHO minted the
# document is deliberately out of scope here: a forged file can claim any
# source, so the consuming workflow establishes provenance from the artifact
# listing's own workflow_run metadata (same-repository origin, gate-workflow
# path) BEFORE this verifier ever opens the file.
#
# SCHEMA 2 (this fix, 2026-09-04): a proof now also carries `audit_suites`, one
# entry per kind="npm-audit" suite this run observed — {id, status, and, ONLY
# when status is PASS, lockfile_blob_sha}. Two rules this buys, both enforced
# by CODE, not by convention:
#   1. verify_gate_proof REFUSES any proof whose audit_suites carries a
#      status of UNAVAILABLE. A registry outage must never become a reusable
#      green skip — this is the one check standing between "npmjs.org was
#      down for five minutes" and "the audit gate silently never runs again
#      for 30 days" (round-1 blocker 2). It runs inside the SAME function the
#      probe job and the fan-in's own re-verify step both already call, so
#      neither needed a workflow change to gain this refusal.
#   2. A proof can only ever be an ancestor for the transitivity rule in
#      verify_shard_results (an UNAVAILABLE audit this run, but the exact same
#      lockfile bytes already passed audit on some earlier tree) when its
#      OWN audit_suites entry for that suite id is PASS. An UNAVAILABLE
#      audit-suite status is recorded here too (never laundered into PASS),
#      but it carries no lockfile_blob_sha and so can never itself serve as
#      the next build's ancestor — the chain can only ever be exactly one
#      real green run long, never an indefinite string of "outage, but
#      blessed by the last outage" proofs.
# Bumping the schema also means an OLD schema-1 proof (minted before this fix)
# is refused outright rather than silently trusted with an assumption about
# fields it never wrote — the correct direction for a proof format gaining a
# new required check.
# --------------------------------------------------------------------------- #
GATE_PROOF_SCHEMA = 2
GATE_PROOF_KIND = "leaf-gate-proof"
_HEX40 = re.compile(r"[0-9a-f]{40}")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=60, check=True,
        encoding="utf-8", errors="replace",
    )
    return proc.stdout.strip()


def checkout_tree_identity(repo: Path = REPO) -> tuple[str, str, str]:
    """(tree, head, problem) for the checkout a proof would bind.

    `problem` is non-empty whenever a tree-bound proof would be dishonest:
    not a git checkout, or a working tree that differs from HEAD's committed
    tree (dirty tracked files OR untracked files — the suites ran on the
    working tree, so anything beyond the committed tree means the tree hash
    does not name what was actually tested). CI checkouts are always clean;
    this refusal exists for local runs."""
    try:
        head = _git(repo, "rev-parse", "HEAD")
        tree = _git(repo, "rev-parse", "HEAD^{tree}")
        dirty = _git(repo, "status", "--porcelain")
    except (OSError, subprocess.SubprocessError) as exc:
        return "", "", (f"not a usable git checkout: "
                        f"{type(exc).__name__}: {str(exc)[:160]}")
    if dirty:
        return "", "", ("working tree differs from HEAD (dirty or untracked "
                        "files): a tree-bound proof would attest a tree the "
                        "suites did not run on")
    if not _HEX40.fullmatch(tree) or not _HEX40.fullmatch(head):
        return "", "", "git returned a non-40-hex object id"
    return tree, head, ""


def emit_gate_proof(path: Path, *, fingerprint: str, total: int,
                    repo: Path = REPO,
                    suite_statuses: Optional[dict[str, str]] = None) -> str:
    """Write the tree-bound proof document. Returns '' on success, else the
    reason emission was REFUSED. Never raises and never fabricates: a refusal
    only costs the next identical-tree build its skip (it runs the full gate),
    so the caller must not turn a refusal into a red verdict.

    `suite_statuses` is the {suite_id: status} map this run observed (from the
    shard result files); only entries for kind="npm-audit" suites are ever
    written out, as `audit_suites`, and only a PASS entry also carries the
    CURRENT lockfile_blob_sha (see _lockfile_blob_sha) — an UNAVAILABLE entry
    is recorded honestly but with no blob sha, so it can never serve as a
    transitivity ancestor. EVERY registered kind="npm-audit" suite gets an
    entry, including MISSING when the map is None or lacks that id (an older
    call site, or a suite this run never touched): verify_gate_proof requires
    a PASS for every one of them, so an omitted entry would read as "nothing
    to refuse" and hand out a green skip. Never a fabricated PASS."""
    tree, head, problem = checkout_tree_identity(repo)
    if problem:
        return problem
    audit_suites: list[dict] = []
    statuses = suite_statuses or {}
    for s in build_suites():
        if s.kind != "npm-audit":
            continue
        # An HONEST RECORD, one entry per registered audit suite, always:
        # PASS (with the lockfile blob sha that makes transitivity possible),
        # UNAVAILABLE for a registry outage, or MISSING when this run never
        # reported the suite at all. The entry is never omitted, because
        # verify_gate_proof requires a PASS for EVERY registered audit suite
        # and an absent entry would otherwise read as "nothing to refuse".
        # Minting stays honest; refusing the REUSE is verify's job.
        status = statuses.get(s.id) or "MISSING"
        entry: dict = {"id": s.id, "status": status}
        if status == "PASS":
            sha = _lockfile_blob_sha(s.cwd)
            if sha:
                entry["lockfile_blob_sha"] = sha
        audit_suites.append(entry)
    payload = {
        "schema": GATE_PROOF_SCHEMA,
        "kind": GATE_PROOF_KIND,
        "tree": tree,
        "head_sha": head,
        "catalog_fingerprint": fingerprint,
        "total_suites": total,
        "audit_suites": audit_suites,
        # Informational ONLY. Provenance is established by the consuming
        # workflow from the artifact's workflow_run metadata; nothing may
        # trust this self-reported block.
        "source": {
            "run_id": os.environ.get("GITHUB_RUN_ID", ""),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
            "event": os.environ.get("GITHUB_EVENT_NAME", ""),
            "workflow": os.environ.get("GITHUB_WORKFLOW", ""),
            "repository": os.environ.get("GITHUB_REPOSITORY", ""),
        },
    }
    try:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    except OSError as exc:
        return f"cannot write proof: {type(exc).__name__}: {str(exc)[:160]}"
    return ""


def verify_gate_proof(proof_path: Path, expect_tree: str) -> int:
    """Exit code for --verify-gate-proof: 0 only when the document is a
    schema-2 tree-bound gate proof whose tree equals `expect_tree`, whose
    catalog fingerprint equals the one THIS checkout derives, AND whose
    audit_suites carries no UNAVAILABLE status. Runs nothing.

    The UNAVAILABLE check is requirement (1) of the round-1 blocker-2 fix: a
    registry outage must never become a reusable green skip. It runs here,
    inside the ONE function both the reuse probe and the fan-in's own
    tree-identity re-verify step call, so neither needed a workflow change —
    a proof minted while npmjs.org was down (or shown provably unchanged only
    via the lockfile-transitivity rule in verify_shard_results) can never
    itself be replayed as "the audit already ran clean here".

    The fingerprint check is not redundant with tree equality even though the
    catalog is a pure function of the tree: it is what makes that dependence
    CHECKED rather than assumed, so a proof whose recorded catalog does not
    match the one this checkout derives is refused instead of trusted on the
    strength of an equal tree hash. It deliberately does NOT refuse a proof
    minted from a differently-rooted checkout — cwds are hashed relative to
    the repo (see _fingerprint_cwd), because runners that give every job its
    own checkout directory would otherwise refuse every honest proof. Refusal
    is always safe: the caller falls back to running the full gate."""
    expect = (expect_tree or "").strip().lower()
    if not _HEX40.fullmatch(expect):
        print(f"NOT VERIFIED: --expect-tree must be a 40-hex git tree id, "
              f"got {expect_tree!r}")
        return 1
    try:
        data = json.loads(Path(proof_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"NOT VERIFIED: unreadable proof file: "
              f"{type(exc).__name__}: {str(exc)[:160]}")
        return 1
    if not isinstance(data, dict) or data.get("schema") != GATE_PROOF_SCHEMA \
            or data.get("kind") != GATE_PROOF_KIND:
        print("NOT VERIFIED: not a schema-2 leaf-gate-proof document")
        return 1
    tree = data.get("tree")
    if not (isinstance(tree, str) and _HEX40.fullmatch(tree)):
        print(f"NOT VERIFIED: proof carries no 40-hex tree (got {tree!r})")
        return 1
    if tree != expect:
        print(f"NOT VERIFIED: proof is bound to tree {tree}, expected {expect}")
        return 1
    suites = build_suites()
    if duplicate_suite_ids(suites):
        print("NOT VERIFIED: catalog has duplicate suite ids; fix "
              "build_suites() first")
        return 1
    fingerprint = catalog_fingerprint(suites)
    if data.get("catalog_fingerprint") != fingerprint:
        print("NOT VERIFIED: proof's catalog fingerprint differs from the one "
              "this checkout derives (a different catalog, or a proof minted "
              "from a differently-rooted checkout); refusing to reuse")
        return 1
    # EVERY registered audit suite must carry a PASS entry. Refusing only
    # UNAVAILABLE would let an absent entry (or an empty list) read as
    # "nothing to refuse" and hand a reusable green skip to a proof that
    # never proved the lockfile clean.
    audit_suites = data.get("audit_suites")
    audit_suites = audit_suites if isinstance(audit_suites, list) else []
    passed = {e.get("id") for e in audit_suites
              if isinstance(e, dict) and e.get("status") == "PASS"}
    not_passed = []
    for s_ in suites:
        if s_.kind != "npm-audit" or s_.id in passed:
            continue
        seen = next((e.get("status") for e in audit_suites
                     if isinstance(e, dict) and e.get("id") == s_.id), None)
        not_passed.append(f"{s_.id}={seen or 'absent'}")
    if not_passed:
        print(f"NOT VERIFIED: proof does not carry a PASS for every audit "
              f"suite ({', '.join(not_passed)}); a registry-outage or "
              f"evidence-free proof is never reused as a green skip, the "
              f"next build must re-run the gate")
        return 1
    src = data.get("source") if isinstance(data.get("source"), dict) else {}
    print(f"VERIFIED: gate proof binds tree {tree} on catalog fingerprint "
          f"{fingerprint} (self-reported mint: run {src.get('run_id') or '?'} "
          f"attempt {src.get('run_attempt') or '?'}, event "
          f"{src.get('event') or '?'} — informational; provenance comes from "
          f"the artifact's workflow_run metadata)")
    return 0


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


def _newest_proof_with_passed_audit(prior_proofs_dir: Path, suite_id: str
                                    ) -> Optional[tuple[str, str, str]]:
    """(blob_sha, source_path, tree) from the NEWEST document under
    prior_proofs_dir that is a schema-2 leaf-gate-proof carrying a PASS
    entry for suite_id in its audit_suites -- or None. Deliberately looks at
    ONLY the single newest such file, never falls back to an older one that
    might happen to match: an attacker (or an honest but stale cache) citing
    an ancient proof must never satisfy this on a technicality, and "the
    newest artifact the workflow can see" is exactly what the CI wiring
    downloads into this directory (see test-gate.yml's "Download recent
    gate-proof artifacts" step).

    NEWEST IS THE ARTIFACT ID IN THE FILE NAME, never the file mtime. That
    step downloads candidates newest-created FIRST and copies them in that
    order, so every mtime is the COPY time and mtime order is exactly
    backwards (measured 2026-09-04: the oldest-created proof won). GitHub
    artifact ids increase with creation, and the step names each file
    "<artifact_id>.json", so the id IS the creation order and it survives
    any copy. A file whose stem is not an integer cannot be ranked and is
    SKIPPED rather than guessed at: unrankable is not newest.

    blob_sha is '' (not None) when that file's
    PASS entry carries no lockfile_blob_sha -- a caller comparing shas must
    treat that as a non-match, never a wildcard. Never raises: an unreadable
    directory or a corrupt file is skipped exactly like verify_shard_results'
    own shard parse loop, and the fail-closed direction is always "no match"."""
    try:
        ranked: list[tuple[int, Path]] = []
        for candidate in prior_proofs_dir.rglob("*.json"):
            stem = candidate.stem
            if not stem.isdigit():
                print(f"prior gate proof {candidate.name} is not named "
                      f"<artifact_id>.json; cannot rank it by creation, "
                      f"skipping", file=sys.stderr)
                continue
            ranked.append((int(stem), candidate))
        paths = [c for _, c in sorted(ranked, key=lambda pair: pair[0],
                                      reverse=True)]
    except OSError:
        return None
    for p in paths:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not (isinstance(data, dict) and data.get("schema") == GATE_PROOF_SCHEMA
                and data.get("kind") == GATE_PROOF_KIND):
            continue
        for e in data.get("audit_suites", []) or []:
            if (isinstance(e, dict) and e.get("id") == suite_id
                    and e.get("status") == "PASS"):
                sha = e.get("lockfile_blob_sha")
                return (sha if isinstance(sha, str) else "", str(p),
                       str(data.get("tree", "?")))
    return None


def _shard_suite_statuses(results_dir: Path) -> dict[str, str]:
    """Best-effort {suite_id: status} straight from the schema-1 shard result
    files under results_dir, for emit_gate_proof's audit_suites block. Reads
    the same files verify_shard_results already proved complete; never
    raises, and a missing/corrupt file just costs that file's entries, same
    tolerance as the fan-in's own parse loop."""
    out: dict[str, str] = {}
    for p in sorted(results_dir.rglob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not (isinstance(data, dict) and data.get("schema") == 1):
            continue
        for e in data.get("results", []) or []:
            if isinstance(e, dict) and isinstance(e.get("id"), str) \
                    and isinstance(e.get("status"), str):
                out[e["id"]] = e["status"]
    return out


def verify_shard_results(results_dir: Path, *,
                         prior_proofs_dir: Optional[Path] = None) -> int:
    """Fan-in: prove the shard set covered the exact catalog, exactly once,
    on the same tree, and everything passed. Exit 0 only on that proof — WITH
    ONE EXCEPTION, and it is never a free pass: an UNAVAILABLE npm-audit
    suite (a registry/transport outage, never a lockfile finding — see
    run_npm_audit_suite) never prints the plain PROVEN line. Instead this
    prints "NOT PROVEN BY AUDIT: <suite> unavailable (<reason>)" and exits 0
    ONLY when the audit's own input is provably unchanged: the CURRENT git
    blob sha of the suite's package-lock.json equals the blob sha recorded in
    the NEWEST proof under `prior_proofs_dir` whose audit_suites entry for
    that suite id is PASS (see _newest_proof_with_passed_audit and emit_gate_
    proof's schema-2 note above). No prior_proofs_dir, no matching file, or a
    differing blob sha all FAIL the fan-in with the same distinct line — this
    keeps the merge honest by TRANSITIVITY (same lockfile bytes, previously
    proven clean on some earlier tree) and never by assumption. UNAVAILABLE
    on any OTHER suite kind is treated as shard corruption: that status is
    legitimate only for kind="npm-audit".

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
            # the acceptance instrument (sol-critic #436 round 1). UNAVAILABLE
            # (a registry/transport outage on an audit-style suite, never a
            # lockfile finding) is recognized alongside PASS/FAIL/SKIP; the
            # FAIL-only check below is what keeps it from ever counting as a
            # failed shard.
            unknown = [f"{e.get('id')}={e.get('status')!r}"
                       for e in entries
                       if e.get("status") not in ("PASS", "FAIL", "SKIP", "UNAVAILABLE")]
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
                elif status == "UNAVAILABLE" and suite.kind != "npm-audit":
                    problems.append(
                        f"shard {i}: {suite.id} UNAVAILABLE but is not an "
                        f"npm-audit suite (that status is legitimate only "
                        f"for a registry/transport outage on "
                        f"kind=\"npm-audit\")")
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

    entries_by_id: dict[str, dict] = {}
    for d in shards.values():
        for e in _dict_entries(d):
            if isinstance(e.get("id"), str):
                entries_by_id[e["id"]] = e
    unavailable_ids = sorted(sid for sid, e in entries_by_id.items()
                             if e.get("status") == "UNAVAILABLE")

    if unavailable_ids:
        # Never the plain PROVEN line for this run: every UNAVAILABLE suite
        # gets its own distinct callout (grep-able by the "NOT PROVEN BY
        # AUDIT:" prefix), and the fan-in's exit code depends on whether EVERY
        # one of them can cite an unbroken lockfile-blob chain back to a real
        # PASS -- never on assuming the outage is harmless.
        transitivity_ok = True
        for sid in unavailable_ids:
            entry = entries_by_id[sid]
            print(f"NOT PROVEN BY AUDIT: {sid} unavailable "
                  f"({entry.get('note') or 'no reason recorded'})")
            suite = suites_by_id.get(sid)
            current_sha = _lockfile_blob_sha(suite.cwd) if suite is not None else ""
            found = (_newest_proof_with_passed_audit(prior_proofs_dir, sid)
                     if prior_proofs_dir is not None else None)
            if not current_sha:
                transitivity_ok = False
                print(f"NOT PROVEN BY AUDIT: {sid} transitivity failed: this "
                      f"checkout's own lockfile blob sha is unreadable")
            elif found is None:
                transitivity_ok = False
                print(f"NOT PROVEN BY AUDIT: {sid} transitivity failed: no "
                      f"prior gate-proof artifact with a PASSED audit is "
                      f"visible under {prior_proofs_dir or '(none given)'}")
            else:
                candidate_sha, candidate_path, candidate_tree = found
                if candidate_sha != current_sha:
                    transitivity_ok = False
                    print(f"NOT PROVEN BY AUDIT: {sid} transitivity failed: "
                          f"lockfile blob {current_sha} differs from the "
                          f"newest passed-audit proof {candidate_path} (tree "
                          f"{candidate_tree}, blob "
                          f"{candidate_sha or '(none recorded)'})")
                else:
                    print(f"NOT PROVEN BY AUDIT: {sid} transitivity holds: "
                          f"lockfile blob {current_sha} unchanged since "
                          f"{candidate_path} (tree {candidate_tree})")
        if not transitivity_ok:
            return 1
        print("NOT PROVEN BY AUDIT: registry unavailable this run for "
              f"{', '.join(unavailable_ids)}, but every one is provably "
              "unchanged since a prior PASSED audit (lockfile-blob "
              "transitivity) -- treated as proven for THIS run only: the "
              "proof this run emits records the UNAVAILABLE status honestly "
              "and carries no fresh blob sha, so it can never itself serve "
              "as a future transitivity ancestor")
        return 0

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
        if r.suite.expected is None:
            exp = "-"
            got_cell = r.got
        else:
            # ">=N", never a bare number: a floor is a minimum, not an
            # equality, so the column must not invite a reader to compare
            # it against GOT as if EXP == GOT were the pass condition.
            exp = f">={r.suite.expected}"
            executed = executed_count(r)
            # GOT mirrors the same executed (skip-adjusted) count the floor
            # is actually checked against, so a suite that only cleared its
            # raw test count via skips cannot LOOK floor-satisfied here while
            # having FAILED the real check.
            got_cell = str(executed) if executed is not None else r.got
        rows.append((r.suite.label, exp, got_cell, r.status, f"{r.seconds:5.1f}", r.note))

    headers = ("SUITE", "EXP", "GOT", "RESULT", "SECS", "NOTE")
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    widths[0] = min(widths[0], 40)
    # A full UNAVAILABLE note ("registry unavailable (...): ... after 3
    # attempts; NOT PROVEN BY AUDIT: ...") can run past 200 characters and
    # stretch the whole table (the "="*len(line) rule) to match (round-1
    # nit 4) — cap it here and truncate in fmt() below. Nothing is lost: the
    # UNAVAILABLE callout right after this table (search "NOT PROVEN BY
    # AUDIT:") always prints the note in full, and the shard/log-dir JSON
    # never truncates it either.
    widths[5] = min(widths[5], 100)

    def fmt(row) -> str:
        cells = []
        for i, cell in enumerate(row):
            cell = str(cell)
            if i == 5 and len(cell) > widths[5]:
                cell = cell[:widths[5] - 1] + "…"
            if i in (1, 2, 3, 4):
                cells.append(cell.rjust(widths[i]))
            else:
                cells.append(cell.ljust(widths[i]))
        return "  ".join(cells).rstrip()

    line = "-" * (sum(widths) + 2 * (len(headers) - 1))
    print()
    print("=" * len(line))
    print("  leaf-web-demo -- GATE SCOREBOARD")
    print("=" * len(line))
    print(fmt(headers))
    print(line)
    for row in rows:
        print(fmt(row))
    print(line)

    npass = sum(1 for r in results if r.status == "PASS")
    nfail = sum(1 for r in results if r.status == "FAIL")
    nskip = sum(1 for r in results if r.status == "SKIP")
    nunavailable = sum(1 for r in results if r.status == "UNAVAILABLE")
    total_tests = sum(r.counts.get("passed", 0) for r in results)
    total_skipped = sum(r.counts.get("skipped", 0) for r in results)
    print(f"  suites: {npass} PASS  {nfail} FAIL  {nskip} SKIP  "
          f"{nunavailable} UNAVAILABLE   "
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
    # A registry outage is a distinct outcome, not a failed gate, and a single
    # row in a long scoreboard is exactly as easy to miss as a flake -- call it
    # out by name so the merge step (and a human skimming CI) can see it
    # without diffing every row. Every callout line is printed with the
    # literal "NOT PROVEN BY AUDIT: " prefix at its own start — never just
    # echoing the note verbatim — so it is a structurally distinct, grep-able
    # marker distinct from the row's NOTE column (which never starts a line
    # with that text; the row always starts with the suite label). round-1
    # blocker 3: the prior version printed the bare note when one was
    # present, which happened to ALSO contain this phrase mid-string (see
    # run_npm_audit_suite), so a test asserting the phrase appeared anywhere
    # in stdout passed even with this whole block deleted — the row's own
    # NOTE column already carried it.
    for r in results:
        if r.status == "UNAVAILABLE":
            detail = r.note or f"{r.suite.id} registry unavailable"
            print(f"  NOT PROVEN BY AUDIT: {detail}")
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
    ap.add_argument("--emit-proof", default=None, metavar="PATH",
                    help="with --verify-shard-results only: after the fan-in PROVES "
                         "the gate, write a tree-bound proof document (git tree hash "
                         "+ catalog fingerprint) to PATH. Emission is refused, never "
                         "fabricated, on a dirty or non-git checkout; a refusal does "
                         "not change the verify exit code.")
    ap.add_argument("--prior-proofs-dir", default=None, metavar="DIR",
                    help="with --verify-shard-results only: a directory of "
                         "previously-fetched gate-proof.json documents (the "
                         "newest by file mtime wins) the fan-in may cite as the "
                         "lockfile-transitivity ancestor for an UNAVAILABLE "
                         "npm-audit suite -- never trusted for anything else. "
                         "Optional; omitting it means an UNAVAILABLE suite "
                         "always fails the fan-in (fail closed).")
    ap.add_argument("--verify-gate-proof", default=None, metavar="FILE",
                    help="proof-check mode (with --expect-tree): exit 0 only when "
                         "FILE is a tree-bound gate proof for exactly that tree and "
                         "this checkout's exact catalog. Runs nothing. Provenance of "
                         "the file is the CALLER's job (GitHub artifact metadata).")
    ap.add_argument("--expect-tree", default=None, metavar="TREEHEX",
                    help="the 40-hex git tree id --verify-gate-proof must find in "
                         "the proof document.")
    args = ap.parse_args()

    if args.verify_gate_proof or args.expect_tree:
        if not (args.verify_gate_proof and args.expect_tree):
            print("--verify-gate-proof and --expect-tree must be used together")
            return 2
        if (args.only or args.result_json or args.shard_count != 1
                or args.shard_index is not None or args.verify_shard_results
                or args.emit_proof):
            print("--verify-gate-proof is a proof-check mode and takes no other "
                  "mode or run flags")
            return 2
        return verify_gate_proof(Path(args.verify_gate_proof), args.expect_tree)

    if args.emit_proof and not args.verify_shard_results:
        print("--emit-proof requires --verify-shard-results: the proof is the "
              "fan-in's verified verdict, never a standalone claim")
        return 2

    if args.prior_proofs_dir and not args.verify_shard_results:
        print("--prior-proofs-dir requires --verify-shard-results")
        return 2

    if args.verify_shard_results:
        if (args.only or args.result_json or args.shard_count != 1
                or args.shard_index is not None):
            print("--verify-shard-results is a fan-in mode and takes no run flags "
                  "(--only/--shard-count/--shard-index/--result-json)")
            return 2
        results_dir = Path(args.verify_shard_results)
        prior_dir = Path(args.prior_proofs_dir) if args.prior_proofs_dir else None
        rc = verify_shard_results(results_dir, prior_proofs_dir=prior_dir)
        if args.emit_proof:
            if rc == 0:
                suites = build_suites()
                problem = emit_gate_proof(
                    Path(args.emit_proof),
                    fingerprint=catalog_fingerprint(suites), total=len(suites),
                    suite_statuses=_shard_suite_statuses(results_dir))
                if problem:
                    print(f"gate proof NOT emitted ({problem}); the verdict "
                          f"above stands — the next identical-tree build just "
                          f"runs the full gate")
                else:
                    print(f"gate proof emitted -> {args.emit_proof}")
            else:
                print("gate proof NOT emitted: the fan-in did not prove the gate")
        return rc

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
        # Width 11, not 4: UNAVAILABLE (11 chars) previously just overran a
        # 4-wide field with no padding, misaligning every row after it
        # (round-1 nit 4) — PASS/FAIL/SKIP still fit inside 11 with room.
        print(f"{res.status:<11} {tail}")
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
