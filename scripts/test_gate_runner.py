"""Gate-runner self-test: spawn failures must be retryable FAIL rows, never
runner-killing exceptions.

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
    assert static.expected == 78
    assert any(
        str(arg).endswith("platform/tests/test_db_schema_proof_static.py")
        for arg in static.argv
    )


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
