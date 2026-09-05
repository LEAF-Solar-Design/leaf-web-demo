"""Gate-runner self-test: the runner must never answer a different question
than the one it was asked — spawn failures stay retryable FAIL rows rather than
runner-killing exceptions, and the `--only` selection is the one that was typed.

WHY: on 2026-07-23 three consecutive full-gate runs died scoreboard-less
mid-suite (rows logged as bare EXIT:127 by the invoking wrapper). The runner
process itself was being killed externally, but the post-mortem also showed a
real latent hole: a spawn-time OSError from subprocess.run escaped run_suite
and would abort the whole gate with every remaining suite unreported. These
tests pin the repaired contract.

Registered as the `gate-runner-selftest` suite with cwd=scripts/ — running
`python -m pytest` from the repo root would put the stdlib-shadowing
`platform/` package on sys.path (same reason the platform suite runs from the
repo parent).
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "run_all_gates", SCRIPTS / "run-all-gates.py")
    mod = importlib.util.module_from_spec(spec)
    # dataclasses resolves the runner's string annotations (it uses
    # `from __future__ import annotations`) through sys.modules — an
    # unregistered module crashes @dataclass with a NoneType AttributeError.
    sys.modules["run_all_gates"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_postgres_proof_files_are_registered_with_exact_counts():
    g = _load_runner()
    suites = {suite.id: suite for suite in g.build_suites()}

    inventory = suites["server-postgres-authority-inventory"]
    # 9 after the annotation authority record was added (was 8). Mirrors
    # the floor in run-all-gates.py; BOTH must move together when the contract
    # file gains a test, and only alongside a re-measured run-all-gates.py floor.
    assert inventory.expected == 9
    assert "tests/test_postgres_authority_inventory_contract.py" in inventory.argv

    annex = suites["server-session-annex-store"]
    # 28 collected, 0 skipped: the PostgreSQL halves run against a fake in
    # place of platform.db, so a no-DB host executes every one of them.
    # 27 -> 28 on 2026-08-13, re-MEASURED alongside the run-all-gates.py floor.
    # 27 was never the collected count -- the file already had 28 tests when
    # #507 registered it -- so this mirror asserted the wrong number from the
    # start and only the registry's silent drift note reported it.
    assert annex.expected == 28
    assert "tests/test_session_annex_store.py" in annex.argv

    static = suites["platform-static"]
    # 183 collected across the 11 *_static.py files minus the 2 DATABASE_URL-
    # gated skips = 181 executed on a no-DB host (re-measured 2026-08-28 with
    # the soft-delete guard card, which both registers
    # test_soft_delete_guard_static.py and closes the standing 172-vs-175 drift
    # the previous note left open). Mirrors the floor in run-all-gates.py; BOTH
    # must move together when a *_static.py file gains a test (#432's 96->102
    # history), and only alongside a re-measured run-all-gates.py floor.
    assert static.expected == 181
    assert any(
        str(arg).endswith("platform/tests/test_soft_delete_guard_static.py")
        for arg in static.argv
    )
    assert any(
        str(arg).endswith("platform/tests/test_db_schema_proof_static.py")
        for arg in static.argv
    )
    assert any(
        str(arg).endswith("platform/tests/test_overlay_store_static.py")
        for arg in static.argv
    )

    measured_residual_floors = {
        "da-mutation-apply": 24,
        # 17 -> 19 on 2026-09-03: the relay publishes the supply evidence
        # envelope it already dispatches, so a downstream lane can reuse it
        # (the provider refuses an envelope minted by any other workflow).
        # +1 that the published bytes come from the same $evidence the
        # dispatch output does and the publisher holds no script or token,
        # +1 that the envelope gets its OWN artifact, because the finalizer
        # reads the receipt with _zip_member and that refuses any archive
        # holding more than one file.
        # 19 -> 36 on 2026-09-05 (merge-queue slice A): the builder's group mode
        # (speculative_group_head, the live-queue GraphQL check, the exact-head
        # checkout, the duplicate-supply-set guard) and the dispatcher's merge_group
        # job carry falsifying rows. Mirrors run-all-gates.py; BOTH must move together.
        "build-platform-images-workflow": 36,
        "platform-release-manifest": 88,
        # 10 -> 17 on 2026-08-18 with the production deploy's second approval
        # mode (administrator self-authorization): 1 acceptance case plus 6
        # parametrized rejection cases for contradictory or under-privileged
        # modes. Mirrors the floor in run-all-gates.py; BOTH must move together.
        "production-web-release": 17,
        # 86 -> 180 on 2026-08-17, alongside the re-measured run-all-gates.py
        # floor (94-test drift: 89e0de06's bulk sweep set 86, never
        # re-measured through #661 + the annotation projection feature +
        # PR #670's post-callback token race fix, +20 cases).
        # 180 -> 685 on 2026-09-02, alongside the re-measured run-all-gates.py
        # floor: 505-test drift over sixteen days and 24 -> 78 test files.
        # Five CI gate-shard-2 logs track the growth 645 -> 659 -> 660 -> 660
        # -> 672, then the integrated tree measured locally at 698 collected /
        # 13 skipped / 685 executed. W4d Slice B then re-measured exact-head
        # Linux CI at 748 collected / 17 skipped / 731 executed after the
        # lossless-handle correction added one gated and one portable case.
        "web-vitest": 731,
    }
    assert {
        suite_id: suites[suite_id].expected
        for suite_id in measured_residual_floors
    } == measured_residual_floors

    assert suites["web-vitest"].allowed_vitest_skips == (
        ("src/cad/engineWasmHarness.realwasm.test.js", 1),
        ("src/cadedit/cadEditSurface.test.jsx", 16),
        ("src/cad/engineBatchAtomic.test.js", 1),
    )

    restore = suites["server-version-restore"]
    assert restore.expected == 26
    assert restore.allowed_skip_reasons == (
        r"PostgreSQL restore proof requires the EXPLICIT opt-in "
        r"LEAF_RESTORE_PG_PROOF_DB \(a disposable database URL\)\. "
        r"A generic ambient DATABASE_URL must never trigger this test: "
        r"it applies every repository migration and leaves randomized "
        r"manifest, version, and checkout rows behind, which would mutate "
        r"a staging or production database whose URL happens to be in the "
        r"environment\.",
    )


def test_executor_suite_is_registered_with_its_measured_floor():
    """The executor tree ran in NO CI until this registration (the
    WeakValueDictionary assignment-lock no-op fixed in #455 sat undetected for
    exactly that reason). This pin mirrors the floor in run-all-gates.py the
    same way the platform-static pin does: BOTH must move together, and only
    alongside a re-measured run. Floor 136 is the Windows executed count
    (142 collected - 2 opt-in Postgres skips - 4 Windows-only environment
    skips, re-measured 2026-08-07 on this tree); Linux CI executes 140 and
    reports upward drift, the min-across-environments convention.

    "Must move together" is the whole point and is what failed here: the floor
    sat at 111 from 2026-08-06 while the suite grew to 136, so 25 executor
    tests could have vanished with the gate still green. A floor only catches a
    regression from where it was last measured, so drifting above it is a
    silent loss of exactly that much coverage."""
    g = _load_runner()
    executor = {s.id: s for s in g.build_suites()}["executor"]

    assert executor.expected == 136
    # cwd is the REPO ROOT (test_control_plane.py opens
    # executor/contracts/schemas/*.json relative to it) and -P keeps that cwd
    # OFF sys.path so the repo's platform/ package cannot shadow the stdlib
    # during pytest plugin import.
    assert executor.cwd == REPO
    assert "-P" in executor.argv
    assert "executor" in executor.argv
    # The two PostgreSQL adapter tests gate on the suite's own explicit
    # opt-in, never a general ambient DATABASE_URL.
    assert any("POSTGRES_CONTROL_PLANE_TEST_URL" in r
               for r in executor.allowed_skip_reasons)
    # The collision-breaking package anchors: without executor/conftest.py the
    # runner's cwd cannot import `executor.*`, and without bench/__init__.py
    # executor/tests and executor/bench/tests both claim the top-level package
    # name `tests` and co-collection fails.
    assert (REPO / "executor" / "conftest.py").is_file()
    assert (REPO / "executor" / "bench" / "__init__.py").is_file()
    # The suite boots warm-pool servers: the default 2.0s weight would
    # understate it badly and quietly pile it onto the critical shard.
    assert "executor" in g._MEASURED_EST_S


# Floors measured 2026-08-29 in ONE run of all 16 files (237 passed, 34
# skipped, 0 failed); per-file executed counts taken from that run's junit XML.
# The 34 skips are all one reason, LEAF_OPERATOR_TEST_DATABASE_URL unset, which
# no workflow in this repo sets -- so CI executes these same counts.
_OPERATOR_FLOORS = {
    "server-operator-authority": 7,
    "server-operator-credential-rotate": 19,
    "server-operator-egress-boundary": 27,
    "server-operator-external-write": 22,
    "server-operator-identity-bindings": 12,
    "server-operator-integration-drills": 4,
    "server-operator-overlay-runbook": 11,
    "server-operator-principals": 3,
    "server-operator-principals-static": 4,
    "server-operator-production-unreachable": 25,
    "server-operator-runbooks": 15,
    "server-operator-secret-broker": 31,
    "server-operator-stage-release": 22,
    "server-operator-vocab-freeze": 13,
    "server-operator-worker-boundary": 12,
    "server-operator-worker-cancel": 10,
}

# Files whose own source carries NO conditional-skip construct. Their suites
# must keep an EMPTY skip allowlist, so a future environment-gated skip FAILS
# the suite instead of quietly eroding its floor.
_OPERATOR_NO_SKIP_FILES = frozenset({
    "server-operator-egress-boundary",
    "server-operator-identity-bindings",
    "server-operator-integration-drills",
    "server-operator-principals-static",
    "server-operator-production-unreachable",
    "server-operator-secret-broker",
    "server-operator-vocab-freeze",
    "server-operator-worker-boundary",
    "server-operator-worker-cancel",
})


def test_operator_suites_are_registered_with_their_measured_floors():
    """The operator control plane ran in NO CI until this registration.

    Sixteen files and 271 tests -- production unreachability, the egress
    boundary, authority and principals, the worker boundary -- were in no
    workflow and no Suite, and three pins rotted through that hole before
    anyone noticed: #746 mounted POST /api/operator/worker/cancel without
    amending the route pin, #810 changed agent_policy.json without re-pinning
    its content SHA, and #680 retired the production deploy's two-person rule
    without updating the two O5 workflow-string pins. #815 hit the same hole
    from the other side, finding the fastapi 0.110 _IncludedRouter reshape had
    blinded the route walk to 6 of 204 leaves under a green gate.

    This mirrors the floors in run-all-gates.py exactly as the executor and
    platform-static pins do: BOTH must move together, and only alongside a
    re-measured run. A floor only catches a regression from where it was last
    measured, so drifting above it is a silent loss of exactly that much
    coverage."""
    g = _load_runner()
    suites = {s.id: s for s in g.build_suites()}

    missing = sorted(set(_OPERATOR_FLOORS) - set(suites))
    assert not missing, f"operator suites vanished from the catalog: {missing}"

    assert {sid: suites[sid].expected for sid in _OPERATOR_FLOORS} == _OPERATOR_FLOORS

    # EVERY test_operator_*.py file is registered. A new one must join the
    # catalog in the same PR, or it lands invisible to CI exactly as these
    # sixteen did.
    on_disk = {p.name for p in (REPO / "server" / "tests").glob("test_operator_*.py")}
    registered = {
        arg.rsplit("/", 1)[-1]
        for sid in _OPERATOR_FLOORS
        for arg in suites[sid].argv
        if isinstance(arg, str) and arg.startswith("tests/test_operator_")
    }
    assert on_disk == registered, (
        "every server/tests/test_operator_*.py must be registered here; "
        f"unregistered: {sorted(on_disk - registered)}")

    for sid in _OPERATOR_FLOORS:
        suite = suites[sid]
        assert suite.cwd == REPO / "server", sid
        if sid in _OPERATOR_NO_SKIP_FILES:
            assert suite.allowed_skip_reasons == (), (
                f"{sid}'s file carries no conditional skip; an allowlist here "
                "would let a future skip erode the floor silently")
        else:
            # The ONLY sanctioned skip is the operator Postgres opt-in, which
            # no workflow sets. Any other allowance is a widened exception and
            # must be argued for on its own.
            assert suite.allowed_skip_reasons == (
                r"LEAF_OPERATOR_TEST_DATABASE_URL not set",), sid

    # The behavioral O5 test spawns bash 14 times; at the 2.0s default the
    # packer would badly understate it and quietly make it a shard's critical
    # path (the failure mode the 2026-08-17 cost refresh was written for).
    assert "server-operator-production-unreachable" in g._MEASURED_EST_S


def test_test_gate_workflow_shards_and_fans_in():
    """Pins the CI shape the completeness proof depends on: shard jobs run the
    runner with shard flags AND write result JSON; a fan-in job that KEEPS the
    job id `gate` / name `run-all-gates` (build-platform-images.yml `needs:`
    this exact reusable job — the repo's only enforceable gate edge) verifies
    the shard set; fail-fast stays off so a cancelled shard cannot destroy the
    proof; the browser install still precedes the shard run."""
    workflow = (REPO / ".github" / "workflows" / "test-gate.yml").read_text(
        encoding="utf-8"
    )
    install_dependencies = workflow.index("name: Install web dependencies")
    install_browser = workflow.index("npx playwright install")
    run_shard = workflow.index("name: Run gate shard")
    assert install_dependencies < install_browser < run_shard
    assert "--shard-count" in workflow and "--shard-index" in workflow
    assert "--result-json" in workflow
    assert "--verify-shard-results" in workflow
    assert "fail-fast: false" in workflow
    assert "name: run-all-gates" in workflow
    # The fan-in must not pass on artifact presence alone: it also requires
    # the shard matrix job itself to have succeeded.
    assert "needs.shards.result" in workflow


def test_test_gate_browser_install_makes_no_apt_call():
    """The chromium step must never shell out to apt again.

    `playwright install-deps` / `install --with-deps` runs apt-get against
    azure.archive.ubuntu.com, and across runs 31053425827, 31058750214 and
    31058651157 (2026-08-05) that mirror served 7 of 24 shard-jobs at
    35-100 kB/s. Every shard fetched the SAME 21.1 MB, so an unlucky draw
    cost that one shard 3.5 to 10 minutes for bytes its siblings got in
    under 3 seconds: gate-shard-3 ran 730s against siblings at 109-181s
    while its actual suites took 60s, the same 60s gate-shard-4 spent. It
    read as a shard-assignment imbalance and was not one; the slow index
    moved run to run (3, then 0, then 2 and 6).

    The 21.1 MB is 9 FONT packages (CJK, Thai, Cyrillic, X bitmap). Every
    library chromium needs to LAUNCH is already on the ubuntu-latest image,
    and the two suites that launch it assert on English DOM text with no
    pixel comparison, so the fonts buy this gate nothing. Restoring either
    flag restores the tail latency, so pin their absence from the step.
    """
    workflow = (REPO / ".github" / "workflows" / "test-gate.yml").read_text(
        encoding="utf-8"
    )
    # Slice the STEP, not the file: the comment above it names both flags on
    # purpose (it tells the next reader when restoring them is right).
    step = workflow.index("name: Install Chromium for browser proofs")
    end = workflow.index("name: Upload shard result and logs")
    browser_step = workflow[step:end]
    assert "npx playwright install chromium" in browser_step
    assert "--with-deps" not in browser_step
    assert "install-deps" not in browser_step


def test_test_gate_workflow_tree_identity_reuse_shape():
    """Pins the tree-identity skip's fail-closed seams (operator decision D3,
    2026-08-05). The probe may only run for exact-ref workflow_call runs and
    may never fail the build; the shards run unless a VERIFIED proof says
    otherwise; the fan-in re-verifies the bound proof itself, asserts the
    shards were skipped (not failed) on the reuse path, mints the proof only
    behind the two-condition full verdict, and exports the proven tree the
    build workflow refuses to push without."""
    workflow = (REPO / ".github" / "workflows" / "test-gate.yml").read_text(
        encoding="utf-8"
    )
    # Probe scope + can't-redden posture.
    assert "if: ${{ inputs.ref != '' }}" in workflow
    probe_at = workflow.index("\n  probe:\n")
    shards_at = workflow.index("\n  shards:\n")
    gate_at = workflow.index("\n  gate:\n")
    assert probe_at < shards_at < gate_at
    probe_block = workflow[probe_at:shards_at]
    assert "continue-on-error: true" in probe_block
    # Provenance comes from artifact metadata, never from the file: same-repo
    # origin and the gate-workflow allowlist are both checked in the probe.
    assert "head_repository_id" in probe_block
    assert ".github/workflows/test-gate.yml|.github/workflows/build-platform-images.yml" \
        in probe_block
    # The runs API answers a bare path today (verified live), but other
    # GitHub surfaces render `path@ref`; the probe strips a suffix before the
    # exact allowlist match so the skip cannot silently die on either shape.
    assert 'path="${path%%@*}"' in probe_block
    assert "select(.expired | not)" in probe_block
    # Shards skip ONLY on a verified reuse; a skipped/failed probe falls
    # through to the full gate.
    shards_block = workflow[shards_at:gate_at]
    assert "needs: probe" in shards_block
    assert "if: ${{ !cancelled() && needs.probe.outputs.reuse != 'true' }}" \
        in shards_block
    # Fan-in: reuse path re-verifies the proof against its own checkout and
    # requires the shards to have been SKIPPED, not failed.
    gate_block = workflow[gate_at:]
    assert "needs: [probe, shards]" in gate_block
    assert "--verify-gate-proof" in gate_block
    assert "--expect-tree" in gate_block
    assert 'test "$SHARD_JOB_RESULT" = "skipped"' in gate_block
    # Full path still requires both conditions and mints the tree-bound proof
    # in the same step, so a proof can never outlive a red verdict.
    assert 'test "$SHARD_JOB_RESULT" = "success"' in gate_block
    assert "--emit-proof" in gate_block
    assert "name: gate-proof-${{ steps.tree.outputs.value }}" in gate_block
    assert "overwrite: true" in gate_block
    # The proven tree is exported for build-platform-images.yml's pre-push
    # binding check.
    assert "proven_tree: ${{ steps.tree.outputs.value }}" in workflow
    assert "value: ${{ jobs.gate.outputs.proven_tree }}" in workflow
    # The probe and fan-in read cross-run artifacts through the Actions API.
    assert "actions: read" in workflow


def test_spawn_failure_is_retryable_fail_row(tmp_path):
    g = _load_runner()
    suite = g.Suite("spawn-victim", "spawn victim", "script", SCRIPTS,
                    [str(SCRIPTS / "definitely-missing-binary-xyz.exe")], None)
    res = g.run_suite(suite, tmp_path, attempt=1)  # must NOT raise
    assert res.status == "FAIL"
    assert "spawn failure" in res.note
    log_text = (tmp_path / "spawn-victim.log").read_text(encoding="utf-8")
    assert "[SPAWN FAILURE]" in log_text


def test_fault_injection_hits_first_attempt_only(tmp_path, monkeypatch):
    g = _load_runner()
    monkeypatch.setenv("LEAF_GATE_FAULT_INJECT", "fault-drill:spawn")
    ok_argv = [sys.executable, "-c", "raise SystemExit(0)"]
    suite = g.Suite("fault-drill", "fault drill", "script", SCRIPTS, ok_argv, None)
    first = g.run_suite(suite, tmp_path, attempt=1)
    second = g.run_suite(suite, tmp_path, attempt=2)
    assert first.status == "FAIL"
    assert "spawn failure" in first.note
    assert second.status == "PASS"


def test_end_to_end_injected_spawn_127_survives_as_retry(tmp_path):
    env = {**os.environ, "LEAF_GATE_FAULT_INJECT": "server-context-packet:spawn"}
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "run-all-gates.py"),
         "--only", "server-context-packet", "--log-dir", str(tmp_path)],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=300,
        encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "flaked; passed on attempt 2" in proc.stdout
    assert "1 PASS  0 FAIL" in proc.stdout


def test_clean_env_scrubs_sandbox_activation_and_credentials(monkeypatch):
    g = _load_runner()
    names = (
        "LEAF_AUTHOR_SANDBOX_PROVIDER",
        "LEAF_TOOL_SANDBOX_PROVIDER",
        "LEAF_AUTHORED_EXECUTION",
        "E2B_API_KEY_FILE",
        "LEAF_SANDBOX_BROKER_HOST",
    )
    for name in names:
        monkeypatch.setenv(name, f"ambient-{name}")
    cleaned = g.clean_env()
    for name in names:
        assert name not in cleaned


def test_windows_command_shim_uses_command_interpreter_and_windows_quoting():
    g = _load_runner()
    original = [
        r"C:\Program Files\nodejs\npx.cmd",
        "tsc",
        "--project",
        r"C:\repo with spaces\tsconfig.json",
    ]

    command, use_shell, interpreter = g.normalize_spawn_command(
        original,
        os_name="nt",
        command_interpreter=r"C:\Windows\System32\cmd.exe",
    )

    assert command == subprocess.list2cmdline(original)
    assert use_shell is True
    assert interpreter == r"C:\Windows\System32\cmd.exe"
    assert original == [
        r"C:\Program Files\nodejs\npx.cmd",
        "tsc",
        "--project",
        r"C:\repo with spaces\tsconfig.json",
    ]


def test_spawn_normalization_leaves_linux_and_native_windows_commands_unchanged():
    g = _load_runner()
    linux = ["npx", "tsc", "--noEmit"]
    windows_exe = [r"C:\nodejs\node.exe", "script.js"]

    assert g.normalize_spawn_command(linux, os_name="posix") == (linux, False, None)
    assert g.normalize_spawn_command(windows_exe, os_name="nt") == (
        windows_exe, False, None
    )


def _summary_suite(g, *, expected, reason, allowed=()):
    output = (
        f"SKIPPED [1] fake_test.py:7: {reason}\n"
        "1 passed, 1 skipped in 0.01s\n"
    )
    return g.Suite(
        "skip-victim", "skip victim", "pytest", SCRIPTS,
        [sys.executable, "-c", f"print({output!r})"], expected,
        allowed_skip_reasons=allowed,
    )


def test_non_allowlisted_pytest_skip_fails_gate(tmp_path):
    g = _load_runner()
    result = g.run_suite(
        _summary_suite(g, expected=1, reason="unexpected dependency gap"),
        tmp_path,
    )
    assert result.status == "FAIL"
    assert "non-allowlisted skip" in result.note


def test_allowlisted_skip_passes_only_when_executed_floor_is_met(tmp_path):
    g = _load_runner()
    reason = "known optional integration unavailable"
    allowed = (r"known optional integration unavailable",)

    passing = g.run_suite(
        _summary_suite(g, expected=1, reason=reason, allowed=allowed), tmp_path)
    deficient = g.run_suite(
        _summary_suite(g, expected=2, reason=reason, allowed=allowed), tmp_path)

    assert passing.status == "PASS"
    assert deficient.status == "FAIL"
    assert "executed-count regression: expected >= 2, got 1" in deficient.note


def test_all_skipped_pytest_suite_fails_even_when_reason_is_allowlisted(tmp_path):
    g = _load_runner()
    output = (
        "SKIPPED [1] fake_test.py:7: known optional integration unavailable\n"
        "1 skipped in 0.01s\n"
    )
    suite = g.Suite(
        "all-skip", "all skip", "pytest", SCRIPTS,
        [sys.executable, "-c", f"print({output!r})"], 0,
        allowed_skip_reasons=(r"known optional integration unavailable",),
    )

    result = g.run_suite(suite, tmp_path)

    assert result.status == "FAIL"
    assert "ALL skipped: no coverage" in result.note


def test_selected_script_environment_skip_is_a_failure(tmp_path):
    g = _load_runner()
    suite = g.Suite(
        "required-smoke", "required smoke", "script", SCRIPTS,
        [sys.executable, "-c", "print('SKIP no runtime'); raise SystemExit(3)"], None,
    )

    result = g.run_suite(suite, tmp_path)

    assert result.status == "FAIL"
    assert result.got == "err"


# --------------------------------------------------------------------------- #
# npm-audit: registry-outage-tolerant classification (harness-audit-high)
#
# WHY: measured 2026-09-04 on PR #989 (run 33840004101), PR #987 (run
# 33859010815, twice) and PR #1004 (run 33855837981) — the advisory-bulk
# registry endpoint answered 503, npm's own internal retry loop turned that
# into a ~700-840s shard, the runner's generic --retry re-ran it once more
# immediately into the same outage, and a real dependency-tree question came
# back red for an unrelated external outage. These pin the fixed contract: a
# real advisory report still FAILS the shard exactly as before, but a
# registry/transport error is UNAVAILABLE, retried with bounded backoff
# entirely inside run_suite(), and never counted as a failed gate.
# --------------------------------------------------------------------------- #
def _fake_npm_script(tmp_path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_npm_audit_clean_exit_zero_passes_without_retry(tmp_path, monkeypatch):
    g = _load_runner()
    sleeps: list = []
    monkeypatch.setattr(g.time, "sleep", lambda s: sleeps.append(s))
    fake = _fake_npm_script(tmp_path, "fake_npm_clean.py",
        "import json, sys\n"
        "print(json.dumps({'vulnerabilities': {}, "
        "'metadata': {'vulnerabilities': {'high': 0}}}))\n"
        "sys.exit(0)\n")
    suite = g.Suite("npm-audit-clean", "npm audit clean", "npm-audit", SCRIPTS,
                    [sys.executable, str(fake)], None)

    result = g.run_suite(suite, tmp_path)

    assert result.status == "PASS"
    assert sleeps == []


def test_npm_audit_real_advisory_report_fails_without_retry(tmp_path, monkeypatch):
    """A real finding must never be retried away or reclassified: it fails on
    the FIRST attempt, same as a plain non-zero exit did before this fix."""
    g = _load_runner()
    sleeps: list = []
    monkeypatch.setattr(g.time, "sleep", lambda s: sleeps.append(s))
    fake = _fake_npm_script(tmp_path, "fake_npm_vuln.py",
        "import json, sys\n"
        "print(json.dumps({'vulnerabilities': {'left-pad': {'severity': 'high'}}, "
        "'metadata': {'vulnerabilities': {'high': 2}}}))\n"
        "sys.exit(1)\n")
    suite = g.Suite("npm-audit-vuln", "npm audit vuln", "npm-audit", SCRIPTS,
                    [sys.executable, str(fake)], None)

    result = g.run_suite(suite, tmp_path)

    assert result.status == "FAIL"
    assert "real advisory report" in result.note
    assert sleeps == []


def test_npm_audit_real_advisory_report_counts_only_at_or_above_the_configured_level():
    """round-1 nit 5: metadata.vulnerabilities.total is ALL severities, but
    this suite's argv hardcodes --audit-level=high, so the count that answers
    'did this cross the gate's own threshold' is high+critical, never the
    all-severities total -- a low/moderate-only report exiting non-zero for
    some other reason must never read as if it broke a threshold it did not
    reach."""
    import json as _json
    g = _load_runner()
    status, note = g._classify_npm_audit(
        1, _json.dumps({"vulnerabilities": {"a": {"severity": "low"}}}), "", False)
    assert status == "FAIL" and "real advisory report" in note

    status, note = g._classify_npm_audit(
        1, _json.dumps({
            "vulnerabilities": {"a": {"severity": "low"}, "b": {"severity": "high"}},
            "metadata": {"vulnerabilities": {"low": 1, "high": 1, "total": 2}},
        }), "", False)
    assert status == "FAIL"
    assert "1 advisories at/above high (2 total across all severities)" in note


def test_npm_audit_bare_5xx_digit_in_unrelated_text_does_not_mask_a_real_failure():
    """The exact repro that caught round-1 blocker 1: a whole-blob
    `re.search(r'\\b5\\d\\d\\b')` classified ANY bare 500-599 number as a
    registry outage, so a corrupt lockfile's EJSONPARSE byte offset, or an
    unrelated package version, silently became UNAVAILABLE instead of FAIL.
    Direct classifier calls, not end-to-end, matching the round-1 reviewer's
    own inputs."""
    g = _load_runner()
    status, note = g._classify_npm_audit(
        1, "",
        "npm error code EJSONPARSE\n"
        "npm error Unexpected token } in JSON at position 512 while "
        "parsing package-lock.json",
        False)
    assert status == "FAIL", note
    assert "unrecognized audit failure" in note

    status, note = g._classify_npm_audit(
        1, "",
        "npm warn deprecated foo@1.500.0\n"
        "npm error some completely unrecognized transport failure",
        False)
    assert status == "FAIL", note


def test_npm_audit_classifies_real_5xx_signals_by_line_adjacency():
    """The three real transport shapes _audit_transport_signal recognizes,
    each requiring its number/errno to sit on the marker's OWN line -- never
    a whole-blob search, which is exactly what made the false positive above
    possible."""
    g = _load_runner()
    status, note = g._classify_npm_audit(
        1, "",
        f"npm error request to https://{g.AUDIT_ADVISORY_ENDPOINT} failed, "
        f"reason: 503", False)
    assert status == "UNAVAILABLE" and "HTTP 503" in note

    status, note = g._classify_npm_audit(
        1, "", "npm warn audit Bad Gateway", False)
    assert status == "UNAVAILABLE" and "HTTP 502" in note

    status, note = g._classify_npm_audit(
        1, "", "npm error network ECONNRESET", False)
    assert status == "UNAVAILABLE" and "ECONNRESET" in note

    # The same errno text on a line that is NOT an npm-error line (e.g. a
    # test assertion echoed to stdout) must never count.
    status, note = g._classify_npm_audit(
        1, "", "assertion failed: expected ECONNRESET, got nothing", False)
    assert status == "FAIL", note


def test_npm_audit_recovers_from_two_transient_outages_as_one_row(
        tmp_path, monkeypatch):
    """503 twice, then a clean report: ONE result row, PASS, and the runner's
    own bounded backoff (never npm's ~700s internal retry loop) is what
    crossed the outage."""
    g = _load_runner()
    sleeps: list = []
    monkeypatch.setattr(g.time, "sleep", lambda s: sleeps.append(s))
    counter = tmp_path / "attempts.txt"
    fake = _fake_npm_script(tmp_path, "fake_npm_flaky.py",
        "import json, sys\n"
        "from pathlib import Path\n"
        f"counter = Path(r'{counter}')\n"
        "n = (int(counter.read_text()) if counter.exists() else 0) + 1\n"
        "counter.write_text(str(n))\n"
        "if n <= 2:\n"
        "    print('npm warn audit 503 Service Unavailable', file=sys.stderr)\n"
        "    print('npm error audit endpoint returned an error', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "print(json.dumps({'vulnerabilities': {}, "
        "'metadata': {'vulnerabilities': {'high': 0}}}))\n"
        "sys.exit(0)\n")
    suite = g.Suite("npm-audit-flaky", "npm audit flaky", "npm-audit", SCRIPTS,
                    [sys.executable, str(fake)], None)

    result = g.run_suite(suite, tmp_path)

    assert result.status == "PASS"
    assert "recovered from registry outage on attempt 3" in result.note
    assert sleeps == [20, 60]              # AUDIT_BACKOFF_S, applied twice
    assert counter.read_text() == "3"      # exactly 3 subprocess spawns, not 2x3


def test_npm_audit_three_transient_outages_is_unavailable_not_fail(
        tmp_path, monkeypatch):
    g = _load_runner()
    sleeps: list = []
    monkeypatch.setattr(g.time, "sleep", lambda s: sleeps.append(s))
    fake = _fake_npm_script(tmp_path, "fake_npm_down.py",
        "import sys\n"
        "print('npm warn audit 503 Service Unavailable', file=sys.stderr)\n"
        "print('npm error audit endpoint returned an error', file=sys.stderr)\n"
        "sys.exit(1)\n")
    suite = g.Suite("npm-audit-down", "npm audit down", "npm-audit", SCRIPTS,
                    [sys.executable, str(fake)], None)

    result = g.run_suite(suite, tmp_path)

    assert result.status == "UNAVAILABLE"
    assert result.status != "FAIL"
    assert sleeps == [20, 60]               # bounded: 2 backoffs, 3 attempts, then stop
    assert "NOT PROVEN BY AUDIT: registry unavailable, lockfile unchanged since" \
        in result.note


def test_npm_audit_worst_case_wall_is_bounded_far_below_the_old_outage_cost():
    """The number this whole fix exists to name: 3 attempts x a bounded
    per-attempt timeout, plus the two backoff sleeps, must stay a small
    fraction of the ~1500s two attempts cost before this fix.

    round-1 nit 1: this MUST use AUDIT_SUBPROCESS_TIMEOUT_S, the timeout that
    actually fires (subprocess.run's hard kill), never AUDIT_FETCH_TIMEOUT_S
    (npm's own --fetch-timeout hint, which bounds one HTTP request inside
    npm, not the child process) — the prior version of this assertion named
    a bound (140s) the code did not enforce (the true one was 215s at the
    old 45s subprocess timeout)."""
    g = _load_runner()
    assert g.AUDIT_WORST_CASE_WALL_S == (
        g.AUDIT_MAX_ATTEMPTS * g.AUDIT_SUBPROCESS_TIMEOUT_S + sum(g.AUDIT_BACKOFF_S))
    assert g.AUDIT_WORST_CASE_WALL_S < 500   # ~4x headroom under 1500s / 2 attempts


def test_scoreboard_prints_not_proven_by_audit_line_for_unavailable_result(
        capsys, tmp_path):
    """One annotated row in a long scoreboard is easy to miss (the FLAKED and
    NO COVERAGE lines exist for the same reason) — UNAVAILABLE gets the same
    treatment, and the summary counts it separately from FAIL.

    round-1 blocker 3: the note text this asserts also sits inside the row's
    own NOTE column, so a substring-anywhere-in-stdout check passed even with
    the callout block deleted entirely (confirmed by mutation: 7 passed, 66
    deselected, on the SAME assertion this replaces). The fix counts PRINTED
    LINES whose own stripped text starts with the distinct "NOT PROVEN BY
    AUDIT:" prefix — the row line always starts with the suite label, never
    with that prefix, so only the callout block can produce one, and deleting
    it is a real mutation kill (verified below by literally deleting it)."""
    g = _load_runner()
    suite_a = g.Suite("harness-audit-high", "harness npm audit (high threshold)",
                      "npm-audit", SCRIPTS, [sys.executable, "-c", "pass"], None)
    suite_b = g.Suite("harness-audit-other", "harness npm audit (other)",
                      "npm-audit", SCRIPTS, [sys.executable, "-c", "pass"], None)
    result_a = g.Result(
        suite_a, "UNAVAILABLE", "unavailable", 42.0,
        note=("registry unavailable (HTTP 503): "
              "registry.npmjs.org/-/npm/v1/security/advisories/bulk "
              "after 3 attempts; NOT PROVEN BY AUDIT: registry unavailable, "
              "lockfile unchanged since abc123"),
        counts={})
    result_b = g.Result(
        suite_b, "UNAVAILABLE", "unavailable", 7.0,
        note="registry unavailable (HTTP 502): endpoint after 3 attempts; "
             "lockfile unchanged since def456",
        counts={})

    g.print_scoreboard([result_a, result_b], tmp_path, 49.0)
    out = capsys.readouterr().out

    callout_lines = [ln for ln in out.splitlines()
                     if ln.strip().startswith("NOT PROVEN BY AUDIT:")]
    assert len(callout_lines) == 2, out
    assert "lockfile unchanged since abc123" in callout_lines[0]
    assert "lockfile unchanged since def456" in callout_lines[1]
    assert "0 PASS  0 FAIL  0 SKIP  2 UNAVAILABLE" in out


def test_scoreboard_note_column_truncates_a_very_long_note_but_keeps_it_in_the_callout(
        capsys, tmp_path):
    """round-1 nit 4: an untruncated NOTE column let one UNAVAILABLE row's
    ~250-character note stretch the whole table's '='*len(line) rule to
    match. The column truncates; the callout line below the table (asserted
    separately above) still carries the note in full, so nothing is lost."""
    g = _load_runner()
    suite = g.Suite("harness-audit-high", "harness npm audit (high threshold)",
                    "npm-audit", SCRIPTS, [sys.executable, "-c", "pass"], None)
    long_note = "x" * 250
    result = g.Result(suite, "UNAVAILABLE", "unavailable", 1.0,
                      note=long_note, counts={})

    g.print_scoreboard([result], tmp_path, 1.0)
    out = capsys.readouterr().out

    assert all(len(ln) < 200 for ln in out.splitlines()
              if not ln.strip().startswith("NOT PROVEN BY AUDIT:")), out
    assert long_note not in out.split("NOT PROVEN BY AUDIT:")[0]  # truncated in the table
    assert long_note in out                                        # but present in the callout


def _shard_stub_suites_with_audit(g):
    """_shard_stub_suites plus one kind='npm-audit' stub whose cwd is the
    REAL harness/ directory — so _lockfile_blob_sha resolves an actual git
    blob in THIS repo rather than needing a scratch checkout, letting the
    transitivity tests compare 'the current value' against 'a recorded
    value' without caring what that value actually is."""
    return _shard_stub_suites(g) + [
        g.Suite("stub-audit", "stub audit", "npm-audit", g.HARNESS,
                [sys.executable, "-c", "pass"], None)]


def test_verifier_refuses_unavailable_on_a_non_audit_suite(
        tmp_path, monkeypatch, capsys):
    """UNAVAILABLE is legitimate ONLY for a kind='npm-audit' suite — it is
    what run_npm_audit_suite alone ever produces, for a registry/transport
    outage, never a lockfile finding. A shard reporting it for any other
    suite kind is corrupt, not a legitimate audit outage, so it must be named
    as shard-set corruption rather than silently entering the transitivity
    path (this replaces the round-1 test that treated exactly this shape as
    a free PROVEN — see blocker 2's design change)."""
    g = _load_runner()
    stubs = _shard_stub_suites(g)
    monkeypatch.setattr(g, "build_suites", lambda: stubs)
    _write_shard_files(g, stubs, tmp_path, statuses={"stub-alpha": "UNAVAILABLE"})

    rc = g.verify_shard_results(tmp_path)
    out = capsys.readouterr().out

    assert rc == 1, out
    assert "not an npm-audit suite" in out
    assert "unrecognized status" not in out


def test_verifier_unavailable_audit_suite_fails_closed_with_no_prior_proof(
        tmp_path, monkeypatch, capsys):
    """round-1 blocker 2: UNAVAILABLE must never become a reusable green
    proof by itself. With no prior_proofs_dir at all, and with one that
    exists but holds no matching candidate, the fan-in must FAIL — never the
    bare PROVEN line — while still recognizing UNAVAILABLE as a legitimate,
    non-corrupt status (not 'unrecognized status', not 'FAILED suites')."""
    g = _load_runner()
    stubs = _shard_stub_suites_with_audit(g)
    monkeypatch.setattr(g, "build_suites", lambda: stubs)
    _write_shard_files(g, stubs, tmp_path, statuses={"stub-audit": "UNAVAILABLE"})

    rc = g.verify_shard_results(tmp_path)   # prior_proofs_dir omitted
    out = capsys.readouterr().out

    assert rc == 1, out
    assert "unrecognized status" not in out
    assert "FAILED suites" not in out
    assert "PROVEN: every suite" not in out
    assert "NOT PROVEN BY AUDIT: stub-audit unavailable" in out
    assert "no prior gate-proof artifact" in out

    empty_dir = tmp_path / "empty-prior-proofs"
    empty_dir.mkdir()
    rc = g.verify_shard_results(tmp_path, prior_proofs_dir=empty_dir)
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "no prior gate-proof artifact" in out


def test_verifier_proves_via_lockfile_transitivity_with_a_matching_prior_proof(
        tmp_path, monkeypatch, capsys):
    """The one legitimate way an UNAVAILABLE audit still exits 0: the newest
    prior proof under prior_proofs_dir shows the EXACT SAME lockfile bytes
    (git blob sha, not commit sha — the whole point is a byte-identical
    lockfile on a DIFFERENT tree) already passed audit."""
    import json as _json
    g = _load_runner()
    stubs = _shard_stub_suites_with_audit(g)
    monkeypatch.setattr(g, "build_suites", lambda: stubs)
    _write_shard_files(g, stubs, tmp_path, statuses={"stub-audit": "UNAVAILABLE"})
    current_sha = g._lockfile_blob_sha(g.HARNESS)
    assert current_sha, "sanity: harness/package-lock.json must be a real git blob"

    prior_dir = tmp_path / "prior-proofs"
    prior_dir.mkdir()
    (prior_dir / "40001.json").write_text(_json.dumps({
        "schema": g.GATE_PROOF_SCHEMA, "kind": g.GATE_PROOF_KIND,
        "tree": "a" * 40, "head_sha": "b" * 40,
        "catalog_fingerprint": "f" * 64, "total_suites": 1,
        "audit_suites": [{"id": "stub-audit", "status": "PASS",
                          "lockfile_blob_sha": current_sha}],
        "source": {},
    }), encoding="utf-8")

    rc = g.verify_shard_results(tmp_path, prior_proofs_dir=prior_dir)
    out = capsys.readouterr().out

    assert rc == 0, out
    assert "PROVEN: every suite" not in out
    assert "NOT PROVEN BY AUDIT: stub-audit unavailable" in out
    assert "transitivity holds" in out


def test_verifier_fails_transitivity_when_prior_proof_blob_sha_differs(
        tmp_path, monkeypatch, capsys):
    """A prior proof that PASSED the audit on DIFFERENT lockfile bytes must
    never satisfy transitivity — the whole premise is byte-identical input,
    never 'some earlier audit passed at some point.'"""
    import json as _json
    g = _load_runner()
    stubs = _shard_stub_suites_with_audit(g)
    monkeypatch.setattr(g, "build_suites", lambda: stubs)
    _write_shard_files(g, stubs, tmp_path, statuses={"stub-audit": "UNAVAILABLE"})

    prior_dir = tmp_path / "prior-proofs"
    prior_dir.mkdir()
    (prior_dir / "40001.json").write_text(_json.dumps({
        "schema": g.GATE_PROOF_SCHEMA, "kind": g.GATE_PROOF_KIND,
        "tree": "a" * 40, "head_sha": "b" * 40,
        "catalog_fingerprint": "f" * 64, "total_suites": 1,
        "audit_suites": [{"id": "stub-audit", "status": "PASS",
                          "lockfile_blob_sha": "0" * 40}],
        "source": {},
    }), encoding="utf-8")

    rc = g.verify_shard_results(tmp_path, prior_proofs_dir=prior_dir)
    out = capsys.readouterr().out

    assert rc == 1, out
    assert "transitivity failed" in out
    assert "lockfile blob" in out


def test_lockfile_blob_sha_is_the_real_git_blob_or_empty_when_absent(tmp_path):
    """_lockfile_blob_sha reads the exact-bytes git BLOB hash (distinct from
    _cheap_lockfile_sha's COMMIT hash above it), matching `git rev-parse` from
    the real checkout, and returns '' — never a placeholder like 'unknown' —
    when there is nothing to hash, so two failures can never compare equal."""
    g = _load_runner()
    want = subprocess.run(
        ["git", "-C", str(g.HARNESS), "rev-parse", "HEAD:./package-lock.json"],
        capture_output=True, text=True, timeout=10, check=True,
        encoding="utf-8", errors="replace").stdout.strip()

    assert g._lockfile_blob_sha(g.HARNESS) == want
    assert g._HEX40.fullmatch(want)
    assert g._lockfile_blob_sha(tmp_path) == ""


def test_vitest_skip_fails_gate(tmp_path):
    g = _load_runner()
    output = (
        "test/unexpected.test.ts (2 tests | 1 skipped)\n"
        "Tests 1 passed | 1 skipped\n"
    )
    suite = g.Suite(
        "vitest-skip", "vitest skip", "vitest", SCRIPTS,
        [sys.executable, "-c", f"print({output!r})"], 1,
    )

    result = g.run_suite(suite, tmp_path)

    assert result.status == "FAIL"
    assert "non-allowlisted vitest skip" in result.note


def test_exact_vitest_file_and_skip_count_allowlist_passes(tmp_path):
    g = _load_runner()
    output = (
        "test/postgres.test.ts (5 tests | 4 skipped)\n"
        "Tests 1 passed | 4 skipped\n"
    )
    suite = g.Suite(
        "vitest-known-skip", "vitest known skip", "vitest", SCRIPTS,
        [sys.executable, "-c", f"print({output!r})"], 1,
        allowed_vitest_skips=(("test/postgres.test.ts", 4),),
    )

    result = g.run_suite(suite, tmp_path)

    assert result.status == "PASS"


def test_all_skipped_vitest_suite_fails_even_when_every_skip_is_allowlisted(tmp_path):
    """The pytest-path twin of this rule shipped first and the vitest path kept
    the hole: parse_vitest reports `got` as passed+failed+skipped, so a suite
    whose every test skipped still cleared its floor and reported PASS. Both
    paths now share coverage_verdict, so this cannot regress on one side only.
    """
    g = _load_runner()
    output = (
        "test/postgres.test.ts (4 tests | 4 skipped)\n"
        "Tests 0 passed | 4 skipped\n"
    )
    suite = g.Suite(
        "vitest-all-skip", "vitest all skip", "vitest", SCRIPTS,
        [sys.executable, "-c", f"print({output!r})"], 1,
        allowed_vitest_skips=(("test/postgres.test.ts", 4),),
    )

    result = g.run_suite(suite, tmp_path)

    assert result.status == "FAIL"
    assert "ALL skipped: no coverage" in result.note


def test_vitest_floor_counts_executed_tests_not_skipped_ones(tmp_path):
    """A vitest suite must not buy its way to the floor with skips: 2 executed
    against a floor of 4 is a coverage regression even though got == 5.
    """
    g = _load_runner()
    output = (
        "test/postgres.test.ts (5 tests | 3 skipped)\n"
        "Tests 2 passed | 3 skipped\n"
    )
    suite = g.Suite(
        "vitest-short-floor", "vitest short floor", "vitest", SCRIPTS,
        [sys.executable, "-c", f"print({output!r})"], 4,
        allowed_vitest_skips=(("test/postgres.test.ts", 3),),
    )

    result = g.run_suite(suite, tmp_path)

    assert result.status == "FAIL"
    assert "executed-count regression: expected >= 4, got 2" in result.note


def test_real_pytest_failure_names_the_real_refusal_as_primary_note(tmp_path):
    """A genuine test failure must be readable from the note alone. Leaving it
    blank forces a reader back onto the bare EXP/GOT numbers to explain a red
    row -- but the floor is a skip-adjusted minimum, not an equality, so the
    numbers alone cannot name the real refusal."""
    g = _load_runner()
    output = "1 failed, 1 passed in 0.01s\n"
    suite = g.Suite(
        "pytest-real-fail", "pytest real fail", "pytest", SCRIPTS,
        [sys.executable, "-c", f"print({output!r}); raise SystemExit(1)"], None,
    )

    result = g.run_suite(suite, tmp_path)

    assert result.status == "FAIL"
    assert result.note.startswith("suite FAILED: 1 failed, 0 errors")


def test_real_vitest_failure_names_the_real_refusal_as_primary_note(tmp_path):
    g = _load_runner()
    output = "Tests 1 passed | 1 failed\n"
    suite = g.Suite(
        "vitest-real-fail", "vitest real fail", "vitest", SCRIPTS,
        [sys.executable, "-c", f"print({output!r}); raise SystemExit(1)"], None,
    )

    result = g.run_suite(suite, tmp_path)

    assert result.status == "FAIL"
    assert result.note.startswith("suite FAILED: 1 failed")


def test_scoreboard_floor_column_renders_as_a_minimum_not_an_equality(
        capsys, tmp_path):
    """EXP must read '>=N', and GOT must be the same skip-adjusted executed
    count the floor is actually checked against. Printing the raw got (5) next
    to the floor (4) would misread as floor-satisfied (5 >= 4) even though the
    suite FAILED the floor on its executed count of 2 -- the exact
    executed-count-as-EXP/GOT-equality bug this card exists to close."""
    g = _load_runner()
    suite = g.Suite("vitest-short-floor", "vitest short floor", "vitest",
                    SCRIPTS, [sys.executable, "-c", "pass"], 4)
    counts = {"passed": 2, "failed": 0, "skipped": 3, "got": 5}
    result = g.Result(suite, "FAIL", "5", 1.0,
                      note="executed-count regression: expected >= 4, got 2",
                      counts=counts)

    g.print_scoreboard([result], tmp_path, 1.0)

    out = capsys.readouterr().out
    line = [ln for ln in out.splitlines() if "vitest short floor" in ln][0]
    normalized = " ".join(line.split())
    assert ">=4 2 FAIL" in normalized


# --------------------------------------------------------------------------- #
# --only selection
#
# WHY: on 2026-07-25 two independent sessions typed `--only a --only b` against
# the old single-value argparse option. It kept only `b`, ran one suite, and
# printed a truthful-looking `suites: 1 PASS  0 FAIL  0 SKIP`. Nothing errored
# and nothing warned; the falsehood lived entirely in the gap between the
# command typed and the run obtained. Same shape as the skipped-test and
# unregistered-file holes this runner was already hardened against.
# --------------------------------------------------------------------------- #
def _selection_suites(g):
    return [
        g.Suite(sid, sid, "script", SCRIPTS, [sys.executable, "-c", "pass"], None)
        for sid in ("server-backbone", "server-jobs-terminal-mirror-atomic",
                    "platform-static", "gate-runner-selftest")
    ]


def test_repeated_only_unions_instead_of_keeping_only_the_last():
    g = _load_runner()
    selected, dead = g.select_suites(
        _selection_suites(g),
        ["server-backbone", "server-jobs-terminal-mirror-atomic"])

    assert [s.id for s in selected] == ["server-backbone",
                                        "server-jobs-terminal-mirror-atomic"]
    assert dead == []


def test_overlapping_only_substrings_select_each_suite_once():
    g = _load_runner()
    selected, dead = g.select_suites(
        _selection_suites(g), ["server", "server-backbone"])

    assert [s.id for s in selected] == ["server-backbone",
                                        "server-jobs-terminal-mirror-atomic"]
    assert dead == []


def test_only_substring_matching_nothing_is_named_rather_than_absorbed():
    """A dead substring inside a union would re-open the original hole one
    pattern at a time: the surviving patterns still produce a green scoreboard
    for less than was asked for."""
    g = _load_runner()
    selected, dead = g.select_suites(
        _selection_suites(g), ["platform-static", "no-such-suite"])

    assert dead == ["no-such-suite"]
    assert [s.id for s in selected] == ["platform-static"]


def test_selection_description_reads_back_as_the_typed_command():
    g = _load_runner()

    assert g.describe_selection([]) == "all suites (no --only filter)"
    assert g.describe_selection(["server"]) == "--only server"
    assert g.describe_selection(["a", "b"]) == (
        "--only a --only b  (union of 2 substrings)")


def test_scoreboard_echoes_the_selection_that_produced_it(capsys, tmp_path):
    g = _load_runner()
    suite = g.Suite("server-backbone", "server backbone", "pytest", SCRIPTS,
                    [sys.executable, "-c", "pass"], None)

    g.print_scoreboard([g.Result(suite, "PASS", "12", 1.0)], tmp_path, 1.0,
                       "--only server-backbone --only server-jobs  "
                       "(union of 2 substrings)")

    out = capsys.readouterr().out
    assert "filter: --only server-backbone --only server-jobs" in out


def test_main_runs_the_union_and_carries_the_selection_into_both_outputs(
        tmp_path, monkeypatch, capsys):
    """The successful path, wired end to end through main().

    The tests above each hold one piece: select_suites is called directly, and
    the repeated-CLI test exits at the dead-substring guard before any header
    or scoreboard prints. A regression that computed the union correctly but
    stopped forwarding `selection` to either output would slip past all of
    them, which is exactly the class of silence this change exists to close.
    """
    g = _load_runner()
    stubs = _selection_suites(g)
    monkeypatch.setattr(g, "build_suites", lambda: stubs)
    ran: list[str] = []

    def fake_run(suite, log_dir, attempt):
        ran.append(suite.id)
        return g.Result(suite, "PASS", "1", 0.0)

    monkeypatch.setattr(g, "run_suite_guarded", fake_run)
    monkeypatch.setattr(sys, "argv", [
        "run-all-gates.py",
        "--only", "server-backbone",
        "--only", "server-jobs-terminal-mirror-atomic",
        "--log-dir", str(tmp_path),
    ])

    rc = g.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert ran == ["server-backbone", "server-jobs-terminal-mirror-atomic"]
    echoed = ("--only server-backbone --only server-jobs-terminal-mirror-atomic"
              "  (union of 2 substrings)  -> 2 of 4 suites")
    assert f"selection: {echoed}" in out      # pre-run header
    assert f"filter: {echoed}" in out         # scoreboard
    assert "suites: 2 PASS  0 FAIL  0 SKIP" in out


def test_cli_accepts_repeated_only_and_reports_every_dead_substring(tmp_path):
    """argparse-level guard the unit tests cannot give: with the old
    single-value `--only`, the first substring was discarded before any
    selection logic ran, so only the second would be named here.
    """
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "run-all-gates.py"),
         "--only", "no-such-suite-alpha", "--only", "no-such-suite-beta",
         "--log-dir", str(tmp_path)],
        cwd=str(REPO), capture_output=True, text=True, timeout=300,
        encoding="utf-8", errors="replace",
    )

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "no-such-suite-alpha" in proc.stdout
    assert "no-such-suite-beta" in proc.stdout


def test_duplicate_suite_id_is_named_once_in_registration_order():
    """Same id twice is reported once, however many times it repeats, and the
    unique ids around it are not implicated."""
    g = _load_runner()
    suites = _selection_suites(g) + [
        g.Suite("platform-static", "a second platform-static", "script", SCRIPTS,
                [sys.executable, "-c", "pass"], None),
        g.Suite("platform-static", "a third platform-static", "script", SCRIPTS,
                [sys.executable, "-c", "pass"], None),
    ]

    assert g.duplicate_suite_ids(suites) == ["platform-static"]
    assert g.duplicate_suite_ids(_selection_suites(g)) == []


def test_duplicates_are_ordered_by_first_registration_not_by_repeat():
    """Interleaved `alpha, beta, beta, alpha`: alpha is registered first, so it
    is reported first, even though beta's repeat is detected first. One
    duplicated id cannot tell the two orders apart, which is why this case is
    separate from the test above."""
    g = _load_runner()

    def stub(sid):
        return g.Suite(sid, sid, "script", SCRIPTS,
                       [sys.executable, "-c", "pass"], None)

    interleaved = [stub("alpha"), stub("beta"), stub("beta"), stub("alpha")]

    assert g.duplicate_suite_ids(interleaved) == ["alpha", "beta"]


def test_the_real_catalog_registers_every_suite_id_exactly_once():
    """The regression this guard exists for, asserted against the shipped
    catalog rather than a stub: on 2026-07-27 a branch registered
    `server-postgres-authority-inventory` twice, with floors 4 and 6, and one
    test file produced two scoreboard rows -- one drift-flagged, one clean."""
    g = _load_runner()

    assert g.duplicate_suite_ids(g.build_suites()) == []


def test_main_refuses_a_catalog_with_a_duplicate_id_and_runs_nothing(
        tmp_path, monkeypatch, capsys):
    g = _load_runner()
    stubs = _selection_suites(g) + [
        g.Suite("platform-static", "a second platform-static", "script", SCRIPTS,
                [sys.executable, "-c", "pass"], None),
    ]
    monkeypatch.setattr(g, "build_suites", lambda: stubs)
    ran: list[str] = []
    monkeypatch.setattr(g, "run_suite_guarded",
                        lambda suite, log_dir, attempt: ran.append(suite.id))
    monkeypatch.setattr(sys, "argv",
                        ["run-all-gates.py", "--log-dir", str(tmp_path)])

    rc = g.main()
    out = capsys.readouterr().out

    assert rc == 2, out
    assert ran == []
    assert "platform-static" in out
    assert "every suite id must be unique" in out
    # 2 is the "nothing ran" code, so no scoreboard may claim a verdict.
    assert "GATE SCOREBOARD" not in out


def test_duplicate_id_is_rejected_even_when_only_filters_it_away(
        tmp_path, monkeypatch, capsys):
    """The check runs before selection on purpose. Filtering the duplicate out
    of this run does not make the catalog sound -- the `N of M` denominator the
    scoreboard echoes is already counting one test file twice."""
    g = _load_runner()
    stubs = _selection_suites(g) + [
        g.Suite("platform-static", "a second platform-static", "script", SCRIPTS,
                [sys.executable, "-c", "pass"], None),
    ]
    monkeypatch.setattr(g, "build_suites", lambda: stubs)
    monkeypatch.setattr(sys, "argv", [
        "run-all-gates.py",
        "--only", "server-backbone",          # matches neither duplicate
        "--log-dir", str(tmp_path),
    ])

    rc = g.main()

    assert rc == 2
    assert "platform-static" in capsys.readouterr().out


def test_windows_prefers_cmd_shims_over_extensionless_node_wrappers(monkeypatch):
    g = _load_runner()
    monkeypatch.setattr(g.os, "name", "nt")

    def fake_which(name):
        return {
            "npm": r"C:\Program Files\nodejs\npm",
            "npm.cmd": r"C:\Program Files\nodejs\npm.cmd",
            "npx": r"C:\Program Files\nodejs\npx",
            "npx.cmd": r"C:\Program Files\nodejs\npx.cmd",
        }.get(name)

    monkeypatch.setattr(g.shutil, "which", fake_which)
    assert g._npm().endswith("npm.cmd")
    assert g._npx().endswith("npx.cmd")


# --------------------------------------------------------------------------- #
# sharding
#
# CI splits the catalog across isolated runner checkouts. The danger sharding
# introduces is SILENT INCOMPLETENESS: a suite that runs in no shard leaves
# every shard green and the gate proven by nothing. These tests pin the two
# halves of the defense: the partition is deterministic and exact, and the
# fan-in verifier refuses every corruption of the shard set it can name.
# --------------------------------------------------------------------------- #
def test_measured_weights_name_only_registered_suites():
    """A renamed suite must not strand its scheduling weight: a stale key
    silently rebalances shards (the renamed suite drops to the default weight
    and can pile onto the critical shard)."""
    g = _load_runner()
    ids = {s.id for s in g.build_suites()}
    stale = sorted(set(g._MEASURED_EST_S) - ids)
    assert stale == [], f"weights for unregistered suite ids: {stale}"


def test_partition_is_deterministic_disjoint_and_exact():
    g = _load_runner()
    suites = g.build_suites()
    catalog_ids = [s.id for s in suites]
    for n in (1, 2, 4, 8):
        first = g.partition_suites(suites, n)
        second = g.partition_suites(suites, n)
        assert [[s.id for s in b] for b in first] == \
               [[s.id for s in b] for b in second], f"n={n} not deterministic"
        union = [s.id for b in first for s in b]
        assert sorted(union) == sorted(catalog_ids), f"n={n} union != catalog"
        assert len(union) == len(set(union)), f"n={n} a suite landed twice"
    # One shard is exactly a full serial run.
    assert [s.id for s in g.partition_suites(suites, 1)[0]] == catalog_ids


def test_partition_keeps_catalog_order_within_a_shard():
    """A shard runs its suites in catalog order, exactly like a full run: the
    scoreboard stays readable against build_suites() and suite-order coupling
    bugs cannot hide behind LPT's weight ordering."""
    g = _load_runner()
    suites = g.build_suites()
    order = {s.id: i for i, s in enumerate(suites)}
    for shard in g.partition_suites(suites, 8):
        indexes = [order[s.id] for s in shard]
        assert indexes == sorted(indexes)


def test_partition_separates_the_two_heaviest_suites():
    """LPT sanity: the top two weights must never share a shard at n=8 —
    if they do, the weights or the assignment loop regressed and the critical
    shard quietly doubles."""
    g = _load_runner()
    heavy = sorted(g._MEASURED_EST_S, key=g._MEASURED_EST_S.get, reverse=True)[:2]
    for shard in g.partition_suites(g.build_suites(), 8):
        ids = {s.id for s in shard}
        assert not (heavy[0] in ids and heavy[1] in ids)


def test_catalog_fingerprint_is_stable_and_sensitive():
    """The fan-in trusts shards only when they hashed the same catalog. Same
    catalog -> same value; touching a floor or a command -> different value."""
    g = _load_runner()
    suites = g.build_suites()
    base = g.catalog_fingerprint(suites)
    assert base == g.catalog_fingerprint(g.build_suites())

    import dataclasses
    floor_moved = list(suites)
    floor_moved[0] = dataclasses.replace(suites[0], expected=(suites[0].expected or 0) + 1)
    assert g.catalog_fingerprint(floor_moved) != base

    argv_moved = list(suites)
    argv_moved[0] = dataclasses.replace(suites[0], argv=list(suites[0].argv) + ["-k", "x"])
    assert g.catalog_fingerprint(argv_moved) != base

    cwd_moved = list(suites)
    cwd_moved[0] = dataclasses.replace(suites[0], cwd=suites[0].cwd / "elsewhere")
    assert g.catalog_fingerprint(cwd_moved) != base


def test_every_platform_static_file_is_registered_in_platform_static():
    """The hole this closes shipped twice for real: db_primitives/db_readiness
    (pre-#29) and then test_overlay_store_static.py (T1 lane) each sat outside
    the gate because the platform-static target list is hand-written. Pin the
    list against the glob so the NEXT *_static.py cannot run nowhere."""
    g = _load_runner()
    static = {s.id: s for s in g.build_suites()}["platform-static"]
    registered = {Path(str(arg)).name for arg in static.argv if str(arg).endswith(".py")}
    on_disk = {p.name for p in (REPO / "platform" / "tests").glob("*_static.py")}
    missing = sorted(on_disk - registered)
    assert missing == [], (
        f"platform/tests/*_static.py not registered in platform-static: {missing}")


def _shard_stub_suites(g):
    suites = [
        g.Suite(sid, sid, "pytest", SCRIPTS, [sys.executable, "-c", "pass"], 1)
        for sid in ("stub-alpha", "stub-beta", "stub-gamma", "stub-delta")
    ]
    # One gated suite so a LEGITIMATE suite-level SKIP is representable:
    # the verifier allows SKIP only for suites with a suite-level skip gate.
    suites.append(g.Suite("stub-gated", "stub-gated", "pytest", SCRIPTS,
                          [sys.executable, "-c", "pass"], 1, db_gated=True))
    return suites


def test_sharded_main_runs_exactly_its_partition_and_writes_result_json(
        tmp_path, monkeypatch, capsys):
    g = _load_runner()
    stubs = _shard_stub_suites(g)
    monkeypatch.setattr(g, "build_suites", lambda: stubs)
    ran: list[str] = []

    def fake_run(suite, log_dir, attempt):
        ran.append(suite.id)
        return g.Result(suite, "PASS", "1", 0.1,
                        counts={"got": 1, "passed": 1, "skipped": 0})

    monkeypatch.setattr(g, "run_suite_guarded", fake_run)
    result_path = tmp_path / "shard0.json"
    monkeypatch.setattr(sys, "argv", [
        "run-all-gates.py", "--shard-count", "2", "--shard-index", "0",
        "--result-json", str(result_path), "--log-dir", str(tmp_path),
    ])

    rc = g.main()
    out = capsys.readouterr().out

    assert rc == 0
    want = [s.id for s in g.partition_suites(stubs, 2)[0]]
    assert ran == want
    assert "--shard-count 2 --shard-index 0" in out
    data = __import__("json").loads(result_path.read_text(encoding="utf-8"))
    assert data["schema"] == 1
    assert data["catalog_fingerprint"] == g.catalog_fingerprint(stubs)
    assert data["shard_count"] == 2 and data["shard_index"] == 0
    assert data["suite_ids"] == want
    assert data["any_fail"] is False
    assert data["executed_total"] == sum(
        e["executed"] for e in data["results"])


def test_shard_cli_rejects_incoherent_flag_combinations(tmp_path):
    """Each of these would otherwise run a set nobody asked for — the exact
    silence class the --only hardening closed. All must exit 2 running
    nothing."""
    cases = (
        ["--shard-count", "8"],                                   # no index
        ["--shard-count", "8", "--shard-index", "8"],             # out of range
        ["--shard-count", "8", "--shard-index", "-1"],            # out of range
        ["--shard-count", "0", "--shard-index", "0"],             # bad count
        ["--shard-count", "2", "--shard-index", "0",
         "--only", "platform-static"],                            # subset shard
        ["--verify-shard-results", str(tmp_path),
         "--shard-count", "2"],                                   # mixed modes
        ["--prior-proofs-dir", str(tmp_path)],                    # no fan-in
    )
    for extra in cases:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "run-all-gates.py"),
             "--log-dir", str(tmp_path)] + extra,
            cwd=str(REPO), capture_output=True, text=True, timeout=300,
            encoding="utf-8", errors="replace",
        )
        assert proc.returncode == 2, (extra, proc.stdout + proc.stderr)
        assert "GATE SCOREBOARD" not in proc.stdout, extra


def _write_shard_files(g, suites, tmp_path, *, statuses=None):
    """Round-trip helper: produce shard result files through the REAL writer,
    so the verifier tests exercise the same JSON shape CI produces."""
    import json as _json
    statuses = statuses or {}
    fingerprint = g.catalog_fingerprint(suites)
    parts = g.partition_suites(suites, 2)
    for i, part in enumerate(parts):
        results = []
        attempts = {}
        for s in part:
            status = statuses.get(s.id, "PASS")
            results.append(g.Result(
                s, status, "1" if status != "SKIP" else "skip", 0.1,
                counts=({"got": 1, "passed": 1, "skipped": 0}
                        if status != "SKIP" else {})))
            attempts[s.id] = 1
        g.write_result_json(
            str(tmp_path / f"gate-shard-{i}" / "gate-result.json"),
            fingerprint=fingerprint, total=len(suites), shard_count=2,
            shard_index=i, selection=f"--shard-count 2 --shard-index {i}",
            suites=part, results=results, attempts_by_id=attempts, wall=0.2)
    return _json


def test_verifier_accepts_a_complete_passing_shard_set(tmp_path, monkeypatch, capsys):
    g = _load_runner()
    stubs = _shard_stub_suites(g)
    monkeypatch.setattr(g, "build_suites", lambda: stubs)
    _write_shard_files(g, stubs, tmp_path)

    assert g.verify_shard_results(tmp_path) == 0
    assert "PROVEN" in capsys.readouterr().out


def test_verifier_refuses_every_corruption_of_the_shard_set(tmp_path, monkeypatch, capsys):
    g = _load_runner()
    stubs = _shard_stub_suites(g)
    monkeypatch.setattr(g, "build_suites", lambda: stubs)
    _json = _write_shard_files(g, stubs, tmp_path)

    def reload(mutate):
        """Fresh good set, one named corruption, verifier must return 1."""
        for child in tmp_path.iterdir():
            if child.is_dir():
                for f in child.iterdir():
                    f.unlink()
                child.rmdir()
            else:
                child.unlink()
        _write_shard_files(g, stubs, tmp_path)
        mutate()
        rc = g.verify_shard_results(tmp_path)
        out = capsys.readouterr().out
        assert rc == 1, out
        return out

    shard0 = tmp_path / "gate-shard-0" / "gate-result.json"
    shard1 = tmp_path / "gate-shard-1" / "gate-result.json"

    def load(p):
        return _json.loads(p.read_text(encoding="utf-8"))

    def dump(p, d):
        p.write_text(_json.dumps(d), encoding="utf-8")

    # A shard never ran.
    out = reload(lambda: shard1.unlink())
    assert "missing shard result" in out

    # The same shard reported twice (and its twin missing).
    def duplicate_index():
        d = load(shard1)
        d["shard_index"] = 0
        dump(shard1, d)
    out = reload(duplicate_index)
    assert "more than once" in out

    # A shard ran a different catalog (fingerprint mismatch).
    def poison_fingerprint():
        d = load(shard0)
        d["catalog_fingerprint"] = "0" * 64
        dump(shard0, d)
    out = reload(poison_fingerprint)
    assert "fingerprint mismatch" in out

    # A shard ran a hand-typed subset instead of its assigned slice.
    def hand_typed_subset():
        d = load(shard0)
        d["suite_ids"] = d["suite_ids"][:-1]
        d["results"] = d["results"][:-1]
        d["executed_total"] = sum(e["executed"] or 0 for e in d["results"])
        dump(shard0, d)
    out = reload(hand_typed_subset)
    assert "differs from the deterministic partition" in out

    # A shard stopped early: results do not cover its suite set.
    def early_stop():
        d = load(shard0)
        d["results"] = d["results"][:-1]
        d["executed_total"] = sum(e["executed"] or 0 for e in d["results"])
        dump(shard0, d)
    out = reload(early_stop)
    assert "do not cover its suite set" in out

    # The file's own arithmetic is broken.
    def broken_total():
        d = load(shard0)
        d["executed_total"] = 999
        dump(shard0, d)
    out = reload(broken_total)
    assert "!= per-suite sum" in out

    # A suite failed.
    def one_fail():
        d = load(shard0)
        d["results"][0]["status"] = "FAIL"
        dump(shard0, d)
    out = reload(one_fail)
    assert "FAILED suites" in out

    # Shards disagree on the shard count.
    def count_disagreement():
        d = load(shard1)
        d["shard_count"] = 3
        dump(shard1, d)
    out = reload(count_disagreement)
    assert "disagree on shard_count" in out


def test_verifier_reports_an_empty_results_dir_as_failure(tmp_path, capsys):
    g = _load_runner()
    assert g.verify_shard_results(tmp_path) == 1
    assert "no shard result files" in capsys.readouterr().out


def test_verifier_accepts_a_gated_suite_level_skip(tmp_path, monkeypatch, capsys):
    g = _load_runner()
    stubs = _shard_stub_suites(g)
    monkeypatch.setattr(g, "build_suites", lambda: stubs)
    _write_shard_files(g, stubs, tmp_path, statuses={"stub-gated": "SKIP"})

    assert g.verify_shard_results(tmp_path) == 0
    assert "PROVEN" in capsys.readouterr().out


def test_verifier_refuses_pass_below_the_suite_floor(tmp_path, monkeypatch, capsys):
    """sol-critic #436 round 2: a fabricated PASS with executed 0 (and a
    consistent executed_total) cleared every check — a suite could run
    nowhere while the gate reported green. The verifier holds the real
    catalog, so it re-checks executed against each suite's own floor."""
    g = _load_runner()
    stubs = _shard_stub_suites(g)
    monkeypatch.setattr(g, "build_suites", lambda: stubs)
    _json = _write_shard_files(g, stubs, tmp_path)

    shard0 = tmp_path / "gate-shard-0" / "gate-result.json"
    d = _json.loads(shard0.read_text(encoding="utf-8"))
    d["results"][0]["executed"] = 0
    d["executed_total"] = sum(e["executed"] or 0 for e in d["results"])
    shard0.write_text(_json.dumps(d), encoding="utf-8")

    rc = g.verify_shard_results(tmp_path)
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "below its floor" in out


def test_verifier_refuses_an_ungated_suite_level_skip(tmp_path, monkeypatch, capsys):
    """Suite-level SKIP is a runner behavior reserved for db_gated/opt-in
    suites; a result file claiming SKIP for any other suite is a suite that
    silently ran nowhere (sol-critic #436 round 2)."""
    g = _load_runner()
    stubs = _shard_stub_suites(g)
    monkeypatch.setattr(g, "build_suites", lambda: stubs)
    _write_shard_files(g, stubs, tmp_path, statuses={"stub-alpha": "SKIP"})

    rc = g.verify_shard_results(tmp_path)
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "no suite-level skip gate" in out


def test_verifier_refuses_non_integer_or_negative_executed_counts(
        tmp_path, monkeypatch, capsys):
    """sol-critic #436 round 4: bool subclasses int, so a corrupt
    `executed: true` satisfied isinstance(executed, int) and cleared a floor
    of 1; negative counts were equally acceptable to the floorless branch.
    Both must be refused as proof of nothing."""
    g = _load_runner()
    stubs = _shard_stub_suites(g)
    monkeypatch.setattr(g, "build_suites", lambda: stubs)
    _json = _write_shard_files(g, stubs, tmp_path)

    for poison in (True, -1):
        shard0 = tmp_path / "gate-shard-0" / "gate-result.json"
        d = _json.loads(shard0.read_text(encoding="utf-8"))
        d["results"][0]["executed"] = poison
        d["executed_total"] = sum(
            e["executed"] or 0 for e in d["results"])
        shard0.write_text(_json.dumps(d), encoding="utf-8")

        rc = g.verify_shard_results(tmp_path)
        out = capsys.readouterr().out
        assert rc == 1, (poison, out)
        assert "below its floor" in out, (poison, out)


def test_verifier_names_corrupt_result_entries_instead_of_crashing(
        tmp_path, monkeypatch, capsys):
    g = _load_runner()
    stubs = _shard_stub_suites(g)
    monkeypatch.setattr(g, "build_suites", lambda: stubs)
    _json = _write_shard_files(g, stubs, tmp_path)

    shard0 = tmp_path / "gate-shard-0" / "gate-result.json"
    d = _json.loads(shard0.read_text(encoding="utf-8"))
    d["results"] = [42] + d["results"][1:]
    shard0.write_text(_json.dumps(d), encoding="utf-8")

    rc = g.verify_shard_results(tmp_path)   # must NOT raise
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "non-object" in out


def test_fingerprint_ignores_toolchain_paths_but_not_targets():
    """The fan-in of run 30938231420 refused all eight shards because it
    resolved a different npm path than the shard jobs (no setup-node in the
    fan-in). Toolchain identity is part of the catalog; toolchain LOCATION is
    part of the job image."""
    g = _load_runner()

    def stub(argv):
        return [g.Suite("s", "s", "vitest", SCRIPTS, argv, 1)]

    linux_npm = g.catalog_fingerprint(stub(["/usr/local/bin/npm", "test"]))
    tool_npm = g.catalog_fingerprint(stub(["/opt/hostedtoolcache/node/20/bin/npm", "test"]))
    win_npm = g.catalog_fingerprint(stub([r"C:\Program Files\nodejs\npm.cmd", "test"]))
    assert linux_npm == tool_npm == win_npm

    other_target = g.catalog_fingerprint(stub(["/usr/local/bin/npm", "run", "e2e"]))
    assert other_target != linux_npm
    npx_instead = g.catalog_fingerprint(stub(["/usr/local/bin/npx", "test"]))
    assert npx_instead != linux_npm


def test_fingerprint_ignores_the_checkout_root_but_not_the_suite_directory(monkeypatch):
    """Run 32018150977 flipped the eight shards onto CodeBuild-hosted runners,
    which hand every job its OWN checkout root
    (/codebuild/output/src<random>/src/actions-runner/_work/...). Hashing cwd
    raw meant no two shards agreed with each other or with the ubuntu-latest
    fan-in, so all eight were refused as "catalog fingerprint mismatch" while
    every suite inside them had passed. Same class as the argv[0] problem
    above: the suite's position RELATIVE to the repo is the catalog fact, the
    root in front of it belongs to the job."""
    g = _load_runner()

    def fp(root: str, *cwds: str) -> str:
        monkeypatch.setattr(g, "REPO", Path(root))
        return g.catalog_fingerprint([
            g.Suite(f"s{i}", "s", "pytest", Path(c), ["<PYTHON>", "-m", "pytest"], 1)
            for i, c in enumerate(cwds)])

    hosted = "/home/runner/work/leaf-web-demo/leaf-web-demo"
    build_a = "/codebuild/output/src894000388/src/actions-runner/_work/leaf-web-demo/leaf-web-demo"
    build_b = "/codebuild/output/src111222333/src/actions-runner/_work/leaf-web-demo/leaf-web-demo"

    # Each layout carries an IN-repo cwd (server/) and the repo-PARENT cwd the
    # platform suites run from, so the outside-the-repo case is covered too.
    def layout(root: str) -> str:
        return fp(root, f"{root}/server", str(Path(root).parent))

    assert layout(hosted) == layout(build_a) == layout(build_b)

    # ...and the value stays fully sensitive to the catalog itself: a suite
    # that moves directory, inside the repo or out of it, still rehashes.
    assert fp(hosted, f"{hosted}/da", str(Path(hosted).parent)) != layout(hosted)
    assert fp(hosted, f"{hosted}/server", hosted) != layout(hosted)


def test_verifier_refuses_a_status_it_does_not_recognize(tmp_path, monkeypatch, capsys):
    """sol-critic #436 round 1: the verifier rejected only the literal FAIL
    status, so a corrupt result file carrying an unknown status (say NOT_RUN)
    for every suite still counted as complete coverage and printed PROVEN —
    fail-open in the acceptance instrument itself. Every status must be a
    recognized terminal value."""
    g = _load_runner()
    stubs = _shard_stub_suites(g)
    monkeypatch.setattr(g, "build_suites", lambda: stubs)
    _json = _write_shard_files(g, stubs, tmp_path)

    shard0 = tmp_path / "gate-shard-0" / "gate-result.json"
    d = _json.loads(shard0.read_text(encoding="utf-8"))
    d["results"][0]["status"] = "NOT_RUN"
    shard0.write_text(_json.dumps(d), encoding="utf-8")

    rc = g.verify_shard_results(tmp_path)
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "unrecognized status" in out


# --------------------------------------------------------------------------- #
# tree-bound gate proof (operator decision D3, 2026-08-05)
#
# The proof lets an identical tree skip the 8-shard re-run on the push-to-main
# build. The danger it introduces is a VACUOUS SKIP: a proof that binds the
# wrong tree, a fabricated document, or an emission from a checkout whose
# working tree is not the tree the suites ran on. These tests pin both halves:
# emission refuses rather than fabricates, and verification refuses every
# forgery it can name. Provenance (who uploaded the artifact) is the
# workflow's job and is pinned by the workflow-shape test above.
# --------------------------------------------------------------------------- #
def _scratch_git_checkout(tmp_path):
    repo = tmp_path / "scratch-repo"
    repo.mkdir()
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "gate", "GIT_AUTHOR_EMAIL": "gate@test",
           "GIT_COMMITTER_NAME": "gate", "GIT_COMMITTER_EMAIL": "gate@test"}

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env,
                       capture_output=True, text=True, timeout=60)

    git("init", "-q")
    (repo / "f.txt").write_text("v1\n", encoding="utf-8")
    git("add", "f.txt")
    git("commit", "-q", "-m", "v1")
    return repo


def test_checkout_tree_identity_reports_the_committed_tree(tmp_path):
    g = _load_runner()
    repo = _scratch_git_checkout(tmp_path)

    tree, head, problem = g.checkout_tree_identity(repo)

    assert problem == ""
    assert g._HEX40.fullmatch(tree) and g._HEX40.fullmatch(head)
    expect = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout.strip()
    assert tree == expect


def test_checkout_tree_identity_refuses_dirty_and_untracked_worktrees(tmp_path):
    """The suites run on the WORKING tree, so anything beyond the committed
    tree means the tree hash does not name what was tested. Both a modified
    tracked file and a fresh untracked file must refuse."""
    g = _load_runner()
    repo = _scratch_git_checkout(tmp_path)

    (repo / "f.txt").write_text("v2\n", encoding="utf-8")
    tree, head, problem = g.checkout_tree_identity(repo)
    assert (tree, head) == ("", "")
    assert "differs from HEAD" in problem

    subprocess.run(["git", "-C", str(repo), "checkout", "--", "f.txt"],
                   check=True, capture_output=True, timeout=60)
    (repo / "untracked.txt").write_text("x\n", encoding="utf-8")
    tree, head, problem = g.checkout_tree_identity(repo)
    assert (tree, head) == ("", "")
    assert "differs from HEAD" in problem


def test_checkout_tree_identity_refuses_a_non_git_directory(tmp_path):
    g = _load_runner()
    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    tree, head, problem = g.checkout_tree_identity(plain)

    assert (tree, head) == ("", "")
    assert "not a usable git checkout" in problem


def test_emit_gate_proof_writes_a_bound_schema2_document(tmp_path):
    g = _load_runner()
    repo = _scratch_git_checkout(tmp_path)
    out = tmp_path / "proofs" / "gate-proof.json"

    problem = g.emit_gate_proof(out, fingerprint="f" * 64, total=5, repo=repo)

    assert problem == ""
    data = __import__("json").loads(out.read_text(encoding="utf-8"))
    tree, head, _ = g.checkout_tree_identity(repo)
    assert data["schema"] == 2
    assert data["kind"] == "leaf-gate-proof"
    assert data["tree"] == tree
    assert data["head_sha"] == head
    assert data["catalog_fingerprint"] == "f" * 64
    assert data["total_suites"] == 5
    # No suite_statuses given: EVERY registered audit suite still gets an
    # entry, with status MISSING. An omitted entry was the fail-open hole
    # (verify_gate_proof requires a PASS for every registered audit suite, so
    # "absent" would have read as "nothing to refuse"). A MISSING entry can
    # never carry a lockfile blob sha, so it can never be a transitivity
    # ancestor either.
    audit_ids = [x.id for x in g.build_suites() if x.kind == "npm-audit"]
    assert [e["id"] for e in data["audit_suites"]] == audit_ids
    assert {e["status"] for e in data["audit_suites"]} == {"MISSING"}
    assert all("lockfile_blob_sha" not in e for e in data["audit_suites"])


def test_emit_gate_proof_records_audit_suite_status_and_blob_sha_only_for_pass(
        tmp_path, monkeypatch):
    """audit_suites carries ONLY kind='npm-audit' suites (never the other
    ~200), and only a PASS entry gets a lockfile_blob_sha — an UNAVAILABLE
    entry is recorded honestly with no fresh sha, so it can never itself
    serve as a future transitivity ancestor (see the schema-2 header note)."""
    g = _load_runner()
    repo = _scratch_git_checkout(tmp_path)
    stubs = [
        g.Suite("stub-audit", "stub audit", "npm-audit", g.HARNESS,
                [sys.executable, "-c", "pass"], None),
        g.Suite("stub-audit-down", "stub audit down", "npm-audit", g.HARNESS,
                [sys.executable, "-c", "pass"], None),
        g.Suite("stub-plain", "stub plain", "pytest", SCRIPTS,
                [sys.executable, "-c", "pass"], 1),
    ]
    monkeypatch.setattr(g, "build_suites", lambda: stubs)
    out = tmp_path / "gate-proof.json"
    want_sha = g._lockfile_blob_sha(g.HARNESS)

    problem = g.emit_gate_proof(
        out, fingerprint="f" * 64, total=3, repo=repo,
        suite_statuses={"stub-audit": "PASS", "stub-audit-down": "UNAVAILABLE",
                        "stub-plain": "PASS"})

    assert problem == ""
    data = __import__("json").loads(out.read_text(encoding="utf-8"))
    audit_by_id = {e["id"]: e for e in data["audit_suites"]}
    assert set(audit_by_id) == {"stub-audit", "stub-audit-down"}   # never stub-plain
    assert audit_by_id["stub-audit"] == {"id": "stub-audit", "status": "PASS",
                                         "lockfile_blob_sha": want_sha}
    assert audit_by_id["stub-audit-down"] == {"id": "stub-audit-down",
                                              "status": "UNAVAILABLE"}
    assert "lockfile_blob_sha" not in audit_by_id["stub-audit-down"]


def test_emit_gate_proof_refuses_rather_than_fabricates(tmp_path):
    g = _load_runner()
    repo = _scratch_git_checkout(tmp_path)
    (repo / "f.txt").write_text("dirty\n", encoding="utf-8")
    out = tmp_path / "gate-proof.json"

    problem = g.emit_gate_proof(out, fingerprint="f" * 64, total=5, repo=repo)

    assert "differs from HEAD" in problem
    assert not out.exists(), "a refused emission must write nothing"


def test_verify_gate_proof_accepts_its_own_emission(tmp_path, capsys):
    """Round trip through the REAL catalog fingerprint: the proof a green
    fan-in emits is exactly the proof the probe and the reuse-path fan-in
    accept."""
    g = _load_runner()
    repo = _scratch_git_checkout(tmp_path)
    fingerprint = g.catalog_fingerprint(g.build_suites())
    out = tmp_path / "gate-proof.json"
    assert g.emit_gate_proof(
        out, fingerprint=fingerprint, total=1, repo=repo,
        # Emission now REFUSES without a PASS for every registered audit
        # suite, so a round trip must supply them.
        suite_statuses={s.id: "PASS" for s in g.build_suites()
                        if s.kind == "npm-audit"}) == ""
    tree, _, _ = g.checkout_tree_identity(repo)

    rc = g.verify_gate_proof(out, tree)

    assert rc == 0
    assert "VERIFIED: gate proof binds tree" in capsys.readouterr().out


def test_verify_gate_proof_refuses_every_forgery(tmp_path, capsys):
    """Each mutation is one lie the skip path must not honor. All must refuse
    with a NAMED reason, and the accept case above proves the base document is
    otherwise sound (so each refusal is caused by its mutation alone)."""
    import json as _json
    g = _load_runner()
    repo = _scratch_git_checkout(tmp_path)
    fingerprint = g.catalog_fingerprint(g.build_suites())
    base_path = tmp_path / "gate-proof.json"
    assert g.emit_gate_proof(
        base_path, fingerprint=fingerprint, total=1, repo=repo) == ""
    tree, _, _ = g.checkout_tree_identity(repo)
    base = _json.loads(base_path.read_text(encoding="utf-8"))

    def refuse(expect_tree, fragment, mutate=None):
        doc = _json.loads(_json.dumps(base))
        if mutate is not None:
            mutate(doc)
        p = tmp_path / "mutated.json"
        p.write_text(_json.dumps(doc), encoding="utf-8")
        rc = g.verify_gate_proof(p, expect_tree)
        out = capsys.readouterr().out
        assert rc == 1, (fragment, out)
        assert fragment in out, (fragment, out)

    # The consumer asked about a DIFFERENT tree than the proof binds.
    refuse("0" * 40, "bound to tree")
    # A non-hex expectation is a caller bug, never a pass.
    refuse("HEAD", "must be a 40-hex git tree id")
    # Wrong document type / schema.
    refuse(tree, "not a schema-2 leaf-gate-proof",
           lambda d: d.update(kind="something-else"))
    refuse(tree, "not a schema-2 leaf-gate-proof",
           lambda d: d.update(schema=1))
    # A proof whose own tree field is garbage.
    refuse(tree, "no 40-hex tree", lambda d: d.update(tree="not-hex"))
    # A proof for the right tree but a different catalog: same tree hash with
    # a different catalog should be impossible, so reuse is refused.
    refuse(tree, "catalog fingerprint differs",
           lambda d: d.update(catalog_fingerprint="0" * 64))
    refuse(tree, "catalog fingerprint differs",
           lambda d: d.pop("catalog_fingerprint"))
    # round-1 blocker 2, requirement 1: a proof minted while (or laundered
    # from) a registry outage must never be reused as a green skip, even
    # though its tree and catalog fingerprint both check out.
    refuse(tree, "PASS for every audit suite",
           lambda d: d.update(audit_suites=[
               {"id": "harness-audit-high", "status": "UNAVAILABLE"}]))
    # ...and the same refusal covers an audit suite that is simply absent,
    # which is what an evidence-free proof looks like.
    refuse(tree, "PASS for every audit suite",
           lambda d: d.update(audit_suites=[]))

    # Unreadable file.
    missing = tmp_path / "missing.json"
    rc = g.verify_gate_proof(missing, tree)
    out = capsys.readouterr().out
    assert rc == 1 and "unreadable proof file" in out
    garbage = tmp_path / "garbage.json"
    garbage.write_text("{not json", encoding="utf-8")
    rc = g.verify_gate_proof(garbage, tree)
    out = capsys.readouterr().out
    assert rc == 1 and "unreadable proof file" in out


def test_proof_cli_rejects_incoherent_flag_combinations(tmp_path):
    """Same silence class as the shard CLI guards: each of these would
    otherwise answer a different question than the one typed. All must exit 2
    running nothing."""
    proof = tmp_path / "gate-proof.json"
    proof.write_text("{}", encoding="utf-8")
    cases = (
        ["--verify-gate-proof", str(proof)],                      # no tree
        ["--expect-tree", "0" * 40],                              # no proof
        ["--emit-proof", str(tmp_path / "out.json")],             # no fan-in
        ["--verify-gate-proof", str(proof), "--expect-tree", "0" * 40,
         "--shard-count", "2", "--shard-index", "0"],             # mixed modes
        ["--verify-gate-proof", str(proof), "--expect-tree", "0" * 40,
         "--verify-shard-results", str(tmp_path)],                # mixed modes
        ["--verify-gate-proof", str(proof), "--expect-tree", "0" * 40,
         "--emit-proof", str(tmp_path / "out.json")],             # mixed modes
    )
    for extra in cases:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "run-all-gates.py"),
             "--log-dir", str(tmp_path)] + extra,
            cwd=str(REPO), capture_output=True, text=True, timeout=300,
            encoding="utf-8", errors="replace",
        )
        assert proc.returncode == 2, (extra, proc.stdout + proc.stderr)
        assert "GATE SCOREBOARD" not in proc.stdout, extra


def test_fanin_emits_proof_only_on_a_proven_gate(tmp_path, monkeypatch, capsys):
    """The wiring main() adds around verify_shard_results: emission happens
    exactly when the fan-in PROVES the gate, an emission refusal never changes
    the verify exit code, and a red fan-in never emits. emit_gate_proof itself
    is stubbed here (its real behavior is pinned above) so this test drives
    main() without depending on the state of the developer's checkout."""
    g = _load_runner()
    stubs = _shard_stub_suites(g)
    monkeypatch.setattr(g, "build_suites", lambda: stubs)
    _write_shard_files(g, stubs, tmp_path)
    emitted = []

    def fake_emit(path, *, fingerprint, total, repo=None, suite_statuses=None):
        emitted.append((str(path), fingerprint, total, suite_statuses))
        return ""

    monkeypatch.setattr(g, "emit_gate_proof", fake_emit)
    proof_path = tmp_path / "out" / "gate-proof.json"
    monkeypatch.setattr(sys, "argv", [
        "run-all-gates.py", "--verify-shard-results", str(tmp_path),
        "--emit-proof", str(proof_path),
    ])

    rc = g.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert emitted == [(str(proof_path), g.catalog_fingerprint(stubs), len(stubs),
                        {s.id: "PASS" for s in stubs})]
    assert "gate proof emitted" in out

    # A refused emission is reported but the PROVEN verdict stands.
    monkeypatch.setattr(g, "emit_gate_proof",
                        lambda *a, **k: "checkout is dirty")
    rc = g.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "gate proof NOT emitted (checkout is dirty)" in out

    # A red fan-in must never mint.
    emitted.clear()
    monkeypatch.setattr(g, "emit_gate_proof", fake_emit)
    shard0 = tmp_path / "gate-shard-0" / "gate-result.json"
    import json as _json
    d = _json.loads(shard0.read_text(encoding="utf-8"))
    d["results"][0]["status"] = "FAIL"
    shard0.write_text(_json.dumps(d), encoding="utf-8")
    rc = g.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert emitted == []
    assert "the fan-in did not prove the gate" in out


def test_vitest_skip_in_a_js_family_file_can_be_named_and_allowlisted(tmp_path):
    """A skip in a .test.js/.jsx file must be nameable, not just .test.ts.

    web/ is almost entirely .test.js and .test.jsx, so a ts-only skip pattern
    made every skip there unnameable and tripped the "reported N but named 0"
    rule on a correctly-declared, correctly-allowlisted skip. Regression guard
    for that gap: same shape as the .test.ts case above, JS extension.
    """
    g = _load_runner()
    output = (
        "src/cad/engineWasmHarness.realwasm.test.js (1 test | 1 skipped)\n"
        "Tests 327 passed | 1 skipped\n"
    )
    suite = g.Suite(
        "vitest-js-known-skip", "vitest js known skip", "vitest", SCRIPTS,
        [sys.executable, "-c", f"print({output!r})"], 1,
        allowed_vitest_skips=(("src/cad/engineWasmHarness.realwasm.test.js", 1),),
    )

    result = g.run_suite(suite, tmp_path)

    assert result.status == "PASS", result.note


def test_vitest_skip_in_a_jsx_file_that_is_not_allowlisted_still_fails(tmp_path):
    """Widening the pattern must not weaken the rule: an undeclared skip in a
    JS-family file is now VISIBLE, and therefore must fail as non-allowlisted
    rather than pass unnoticed."""
    g = _load_runner()
    output = (
        "src/projects/states.test.jsx (19 tests | 2 skipped)\n"
        "Tests 17 passed | 2 skipped\n"
    )
    suite = g.Suite(
        "vitest-jsx-undeclared-skip", "vitest jsx undeclared skip", "vitest", SCRIPTS,
        [sys.executable, "-c", f"print({output!r})"], 1,
    )

    result = g.run_suite(suite, tmp_path)

    assert result.status == "FAIL"
    assert "non-allowlisted vitest skip" in result.note


def _write_proof(g, tmp_path, artifact_id, blob, *, status="PASS",
                 suite="harness-audit-high", fingerprint="fp", total=1):
    """A schema-2 proof named the way test-gate.yml names it."""
    import json
    entry = {"id": suite, "status": status}
    if status == "PASS":
        entry["lockfile_blob_sha"] = blob
    doc = {
        "schema": g.GATE_PROOF_SCHEMA,
        "kind": g.GATE_PROOF_KIND,
        "tree": "t" * 40,
        "head_sha": "h" * 40,
        "catalog_fingerprint": fingerprint,
        "total_suites": total,
        "audit_suites": [entry] if entry["status"] != "ABSENT" else [],
    }
    p = tmp_path / f"{artifact_id}.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def test_the_prior_proof_chosen_is_the_newest_artifact_id_not_the_newest_mtime(tmp_path):
    """test-gate.yml downloads candidates newest-created FIRST and copies them
    in that order, so every mtime is the copy time and mtime order is exactly
    backwards. Written in that same order here, the newest ARTIFACT ID wins."""
    g = _load_runner()
    newest = _write_proof(g, tmp_path, 3000, "1" * 40)   # first written, oldest mtime
    _write_proof(g, tmp_path, 2000, "2" * 40)
    _write_proof(g, tmp_path, 1000, "3" * 40)            # last written, newest mtime
    got = g._newest_proof_with_passed_audit(tmp_path, "harness-audit-high")
    assert got is not None
    assert got[0] == "1" * 40, got
    assert got[1] == str(newest), got


def test_a_prior_proof_that_cannot_be_ranked_is_skipped_not_guessed(tmp_path):
    """A file not named <artifact_id>.json carries no creation order, so it is
    skipped rather than treated as the newest."""
    import json
    g = _load_runner()
    _write_proof(g, tmp_path, 1000, "3" * 40)
    doc = json.loads((tmp_path / "1000.json").read_text(encoding="utf-8"))
    doc["audit_suites"][0]["lockfile_blob_sha"] = "9" * 40
    (tmp_path / "gate-proof-latest.json").write_text(json.dumps(doc), encoding="utf-8")
    got = g._newest_proof_with_passed_audit(tmp_path, "harness-audit-high")
    assert got is not None and got[0] == "3" * 40, got


def test_a_proof_without_a_pass_for_every_audit_suite_is_never_verified(tmp_path, capsys):
    """UNAVAILABLE, MISSING and absent all fail closed: only a PASS entry for
    every registered audit suite makes a proof reusable as a green skip."""
    import json
    g = _load_runner()
    suites = g.build_suites()
    audit_ids = [s.id for s in suites if s.kind == "npm-audit"]
    assert audit_ids, "the catalog must register at least one npm-audit suite"
    fingerprint = g.catalog_fingerprint(suites)
    # verify_gate_proof compares the DOCUMENT's tree against expect_tree; a
    # literal is enough here and keeps the test independent of the worktree
    # state (checkout_tree_identity returns '' on a dirty or unusual tree).
    tree = "a1" * 20
    for block, why in (([], "absent"),
                       ([{"id": audit_ids[0], "status": "UNAVAILABLE"}], "unavailable"),
                       ([{"id": audit_ids[0], "status": "MISSING"}], "missing")):
        doc = {
            "schema": g.GATE_PROOF_SCHEMA,
            "kind": g.GATE_PROOF_KIND,
            "tree": tree,
            "head_sha": "h" * 40,
            "catalog_fingerprint": fingerprint,
            "total_suites": len(suites),
            "audit_suites": block,
        }
        path = tmp_path / f"proof-{why}.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        assert g.verify_gate_proof(path, tree) == 1, why
        assert "PASS for every audit suite" in capsys.readouterr().out, why
