"""Strict regression verdicts exercised through the registered gate row."""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_gate_runner import _load_runner

REPO = Path(__file__).resolve().parent.parent
WRAPPER = REPO / "web/e2e/regressions/runGate.mjs"


@pytest.mark.parametrize("drill,status,verdict,selected", [
    ("pass", "PASS", "passed", 1),
    ("failure", "FAIL", "failed", 1),
    ("skip", "FAIL", "INVALID_SELECTION", 1),
    ("empty", "FAIL", "INVALID_SELECTION", 0),
])
def test_drill_scoreboard_uses_registered_run_suite(
        drill, status, verdict, selected, tmp_path, monkeypatch, capsys):
    g = _load_runner()
    real = next(s for s in g.build_suites() if s.id == "studio-walk-regressions")
    suite = dataclasses.replace(real, argv=[*real.argv, "--drill", drill])
    log_dir = Path(os.environ.get("LEAF_W3E_DRILL_LOG_DIR", str(tmp_path))) / drill
    log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LEAF_REGRESSION_SPEC", str(WRAPPER))
    monkeypatch.setenv("LEAF_REGRESSION_REPORT", str(tmp_path / "must-not-write.json"))
    monkeypatch.delenv("LEAF_GATE_FAULT_INJECT", raising=False)
    # Keep selection, scoreboard and gate exit logic; only replace the catalog
    # with a local copy of the real row.
    monkeypatch.setattr(g, "build_suites", lambda: [suite])
    actual_run = g.run_suite
    results = []

    def record(*args, **kwargs):
        result = actual_run(*args, **kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(g, "run_suite", record)
    monkeypatch.setattr(sys, "argv", [
        "run-all-gates.py", "--only", real.id, "--jobs", "1", "--retry", "0",
        "--log-dir", str(log_dir),
    ])
    rc = g.main()
    scoreboard = capsys.readouterr().out
    (log_dir / "scoreboard.txt").write_text(scoreboard, encoding="utf-8")
    assert len(results) == 1, scoreboard
    result = results[0]
    log = result.log_path.read_text(encoding="utf-8")
    assert result.status == status, log
    assert rc == (0 if status == "PASS" else 1), scoreboard + log
    assert "GATE SCOREBOARD" in scoreboard and status in scoreboard, scoreboard
    reports = [json.loads(line) for line in log.splitlines() if line.startswith('{"selected":')]
    assert len(reports) == 1, log
    assert reports[0]["verdict"] == verdict, log
    assert reports[0]["selected"] == selected, log
    if drill == "pass":
        assert reports[0]["results"][0]["status"] == "passed", log
    if drill == "failure":
        assert "W3E_GATE_FAILURE_DRILL" in log
    if drill == "skip":
        assert reports[0]["results"][0]["status"] == "skipped", log
    assert not (tmp_path / "must-not-write.json").exists()


def test_wrapper_environment_selection_and_child_failures():
    # Inject only the process boundary. The production invocation and wrapper
    # verdict logic are exercised without starting the product stack.
    script = r"""
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
const { gateInvocation, runGate } = await import(process.argv[1])
const normal = gateInvocation([], {
  LEAF_REGRESSION_SPEC: 'external.spec.mjs',
  LEAF_REGRESSION_REPORT: 'external-report.json',
})
assert.equal(normal.options.env.LEAF_REGRESSION_SPEC, undefined)
assert.equal(normal.options.env.LEAF_REGRESSION_REPORT, undefined)
assert.equal(normal.options.shell, false)
assert.equal(normal.args.length, 4)
assert.equal(normal.args[1], 'test')
assert.equal(normal.args[2], '--config')
assert.ok(normal.args[3].endsWith('playwright.regressions.config.mjs'))
for (const drill of ['pass', 'failure', 'skip', 'empty']) {
  assert.ok(gateInvocation(['--drill', drill]).options.env.LEAF_REGRESSION_SPEC.endsWith(drill + '.case.mjs'))
}
for (const argv of [['--drill'], ['--drill', 'other'], ['--list'], ['--drill', 'pass', '--grep', 'one']]) {
  assert.throws(() => gateInvocation(argv))
}
const checkCLI = async () => {}
for (const code of [0, 1, 3, 4, 75, 76]) {
  const spawnChild = () => {
    const child = new EventEmitter()
    queueMicrotask(() => child.emit('close', code, null))
    return child
  }
  assert.equal(await runGate([], { spawnChild, checkCLI }), code)
}
assert.equal(await runGate([], {
  checkCLI: async () => { throw new Error('missing Playwright') },
  spawnChild: () => { throw new Error('must not spawn') },
}), 1)
assert.equal(await runGate([], {
  checkCLI, spawnChild: () => { throw new Error('spawn exception') },
}), 1)
assert.equal(await runGate([], {
  checkCLI, spawnChild: () => {
    const child = new EventEmitter()
    queueMicrotask(() => child.emit('error', new Error('spawn event')))
    return child
  },
}), 1)
assert.equal(await runGate([], {
  checkCLI, spawnChild: () => {
    const child = new EventEmitter()
    queueMicrotask(() => child.emit('close', null, 'SIGTERM'))
    return child
  },
}), 1)
assert.equal(await runGate([], {
  checkCLI, timeoutMs: 5, spawnChild: () => {
    const child = new EventEmitter()
    child.kill = (signal) => { child.emit('close', null, signal); return true }
    return child
  },
}), 124)
assert.equal(await runGate([], {
  checkCLI, timeoutMs: 5, spawnChild: () => {
    const child = new EventEmitter()
    child.kill = () => false
    return child
  },
}), 124)
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, WRAPPER.as_uri()],
        cwd=REPO, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
