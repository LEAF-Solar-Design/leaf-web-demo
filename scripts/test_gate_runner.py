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
