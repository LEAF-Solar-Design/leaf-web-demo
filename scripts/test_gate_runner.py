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
    assert static.expected == 96
    assert any(
        str(arg).endswith("platform/tests/test_db_schema_proof_static.py")
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


def test_test_gate_installs_the_locked_playwright_browser_before_running():
    workflow = (REPO / ".github" / "workflows" / "test-gate.yml").read_text(
        encoding="utf-8"
    )
    install_dependencies = workflow.index("name: Install web dependencies")
    install_browser = workflow.index(
        "run: npx playwright install --with-deps chromium"
    )
    run_gate = workflow.index("name: Run the full gate")
    assert install_dependencies < install_browser < run_gate


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
