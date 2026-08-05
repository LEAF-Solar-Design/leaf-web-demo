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
    assert inventory.expected == 6
    assert "tests/test_postgres_authority_inventory_contract.py" in inventory.argv

    static = suites["platform-static"]
    # 124 collected across the 8 *_static.py files minus the 2 DATABASE_URL-
    # gated skips = 122 executed on a no-DB host (measured 2026-08-04). Mirrors
    # the floor in run-all-gates.py; BOTH must move together when a *_static.py
    # file gains a test (#432's 96->102 history), and only alongside a
    # re-measured run-all-gates.py floor.
    assert static.expected == 122
    assert any(
        str(arg).endswith("platform/tests/test_db_schema_proof_static.py")
        for arg in static.argv
    )
    assert any(
        str(arg).endswith("platform/tests/test_overlay_store_static.py")
        for arg in static.argv
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


def test_emit_gate_proof_writes_a_bound_schema1_document(tmp_path):
    g = _load_runner()
    repo = _scratch_git_checkout(tmp_path)
    out = tmp_path / "proofs" / "gate-proof.json"

    problem = g.emit_gate_proof(out, fingerprint="f" * 64, total=5, repo=repo)

    assert problem == ""
    data = __import__("json").loads(out.read_text(encoding="utf-8"))
    tree, head, _ = g.checkout_tree_identity(repo)
    assert data["schema"] == 1
    assert data["kind"] == "leaf-gate-proof"
    assert data["tree"] == tree
    assert data["head_sha"] == head
    assert data["catalog_fingerprint"] == "f" * 64
    assert data["total_suites"] == 5


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
    assert g.emit_gate_proof(out, fingerprint=fingerprint, total=1, repo=repo) == ""
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
    refuse(tree, "not a schema-1 leaf-gate-proof",
           lambda d: d.update(kind="something-else"))
    refuse(tree, "not a schema-1 leaf-gate-proof",
           lambda d: d.update(schema=2))
    # A proof whose own tree field is garbage.
    refuse(tree, "no 40-hex tree", lambda d: d.update(tree="not-hex"))
    # A proof for the right tree but a different catalog: same tree hash with
    # a different catalog should be impossible, so reuse is refused.
    refuse(tree, "catalog fingerprint differs",
           lambda d: d.update(catalog_fingerprint="0" * 64))
    refuse(tree, "catalog fingerprint differs",
           lambda d: d.pop("catalog_fingerprint"))

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

    def fake_emit(path, *, fingerprint, total, repo=None):
        emitted.append((str(path), fingerprint, total))
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
    assert emitted == [(str(proof_path), g.catalog_fingerprint(stubs), len(stubs))]
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
