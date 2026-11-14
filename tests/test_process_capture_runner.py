"""Runner capture contracts; capture is mocked, bootstrap help needs no strace."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def runner(monkeypatch):
    for name in ("LEAF_PROCESS_CAPTURE_CONTEXT", "LEAF_PROCESS_CAPTURE_OUT",
                 "LEAF_PROCESS_CAPTURE_DIR", "LEAF_TRUSTED_CI_DIR", "LEAF_READSET_DIR",
                 "LEAF_GATE_FAULT_INJECT", "LEAF_PROOF_TRACING"):
        monkeypatch.delenv(name, raising=False)
    spec = importlib.util.spec_from_file_location("capture_runner_test", ROOT / "scripts/run-all-gates.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    previous = list(sys.path)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = previous
    return module


def enable_capture(monkeypatch, tmp_path):
    context = tmp_path / "context.json"
    context.write_text('{"capture_group":"fixture"}', encoding="utf-8")
    output = tmp_path / "capture"
    output.mkdir()
    monkeypatch.setenv("LEAF_PROCESS_CAPTURE_CONTEXT", str(context))
    monkeypatch.setenv("LEAF_PROCESS_CAPTURE_OUT", str(output))
    return context, output


def suite(runner, tmp_path, kind="pytest"):
    argv = [sys.executable, "-m", "pytest", "sample.py"] if kind == "pytest" else ["node", "sample.mjs"]
    return runner.Suite("fixture suite", "fixture", kind, tmp_path, argv, None, timeout_s=17)


@pytest.mark.parametrize("kind", ["pytest", "vitest"])
def test_capture_off_preserves_spawn(runner, monkeypatch, tmp_path, kind):
    item = suite(runner, tmp_path, kind)
    monkeypatch.setenv("LEAF_PROOF_TRACING", "1")
    expected_argv = item.argv + ["--durations=0", "--durations-min=0"] if kind == "pytest" else item.argv
    expected_command, shell, executable = runner.normalize_spawn_command(expected_argv)
    expected_env = {**runner.clean_env(), **runner.suite_trace_env(item, tmp_path, 1)}
    with mock.patch.object(runner.subprocess, "run", return_value=SimpleNamespace(
            returncode=0, stdout="1 passed", stderr="")) as spawn:
        runner.run_suite(item, tmp_path)
    spawn.assert_called_once_with(expected_command, cwd=str(item.cwd), env=expected_env,
                                  capture_output=True, text=True, timeout=17, shell=shell,
                                  executable=executable, encoding="utf-8", errors="replace")


@pytest.mark.parametrize("missing", ["context", "output", "both"])
def test_capture_requires_both_existing_paths(runner, monkeypatch, tmp_path, missing):
    context, output = enable_capture(monkeypatch, tmp_path)
    if missing in ("context", "both"):
        context.unlink()
    if missing in ("output", "both"):
        output.rmdir()
    assert not runner.process_capture_enabled()
    env = {}
    assert runner.capture_spawn(suite(runner, tmp_path), 1, ["python"], env) is None
    assert env == {}


@pytest.mark.parametrize("kind", ["pytest", "vitest", "tsc", "node", "npm", "shell", "script", "npm-audit"])
def test_predicate(runner, tmp_path, kind):
    assert runner.process_capture_eligible(suite(runner, tmp_path, kind)) is (kind == "pytest")


def test_strace_smoke_suite_is_never_wrapped(runner, tmp_path):
    # The smoke suite runs strace itself and ptrace cannot nest.
    smoke = runner.Suite("ci-trace-smoke-linux", "smoke", "pytest", tmp_path,
                         [sys.executable, "-m", "pytest", "sample.py"], None, timeout_s=17)
    assert not runner.process_capture_eligible(smoke)
    assert "ci-trace-smoke-linux" in {s.id for s in runner.build_suites()}


@pytest.mark.parametrize("trusted", [False, True])
def test_capture_exact_argv_and_nested_env(runner, monkeypatch, tmp_path, trusted):
    context, output = enable_capture(monkeypatch, tmp_path)
    helper_dir = str(tmp_path / "trusted-ci") if trusted else str(ROOT / "scripts/ci")
    if trusted:
        monkeypatch.setenv("LEAF_TRUSTED_CI_DIR", helper_dir)
    item = suite(runner, tmp_path)
    destination = output / runner.encoded_suite_id(item.id) / "2"
    with mock.patch.object(runner, "run_capture_command", return_value=SimpleNamespace(
            returncode=0, stdout="1 passed", stderr="")) as spawn:
        result = runner.run_suite(item, tmp_path, attempt=2)
    command = spawn.call_args.args[0]
    kwargs = spawn.call_args.kwargs
    original = command[command.index("--") + 1:]
    assert original[:len(item.argv)] == item.argv
    bootstrap = ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
                 "from trace_supervisor import main; sys.exit(main())")
    assert command == [sys.executable, "-I", "-B", "-c", bootstrap, helper_dir,
                       "run", "--context", str(context), "--out", str(destination),
                       "--suites-file", str(destination / "capture-suites.json"), "--sink", "file",
                       "--spool-quota-bytes", "8589934592", "--", *original]
    assert kwargs["timeout"] == 17 + runner.PROCESS_CAPTURE_DECODE_BUDGET_S
    assert kwargs["cwd"] == str(item.cwd)
    assert kwargs["shell"] is False
    assert kwargs["env"]["LEAF_PROCESS_CAPTURE_DIR"] == str(destination)
    assert "LEAF_PROCESS_CAPTURE_CONTEXT" not in kwargs["env"]
    assert "LEAF_PROCESS_CAPTURE_OUT" not in kwargs["env"]
    with mock.patch.dict(os.environ, kwargs["env"], clear=True):
        assert not runner.process_capture_enabled()
    assert result.status == "PASS"
    assert result.process_capture["process_capture_errors"]


def test_real_isolated_bootstrap_help(runner, monkeypatch, tmp_path):
    enable_capture(monkeypatch, tmp_path)
    command, _ = runner.capture_spawn(suite(runner, tmp_path), 1, ["unused"], {})
    # Exercise the production prefix and sibling imports without launching a tracer.
    result = subprocess.run(command[:6] + ["--help"], cwd=tmp_path,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "run" in result.stdout


def test_capture_leaves_vitest_unwrapped(runner, monkeypatch, tmp_path):
    enable_capture(monkeypatch, tmp_path)
    item = suite(runner, tmp_path, "vitest")
    with mock.patch.object(runner.subprocess, "run", return_value=SimpleNamespace(
            returncode=0, stdout="Tests  1 passed (1)", stderr="")) as spawn, \
            mock.patch.object(runner, "run_capture_command") as capture:
        result = runner.run_suite(item, tmp_path)
    assert spawn.call_args.args[0] == item.argv
    assert spawn.call_args.kwargs["timeout"] == item.timeout_s
    assert result.process_capture == {}
    capture.assert_not_called()


def test_nested_scrub_is_copy(runner):
    env = {"LEAF_PROCESS_CAPTURE_CONTEXT": "context", "LEAF_PROCESS_CAPTURE_OUT": "out", "KEEP": "value"}
    assert runner.scrub_process_capture_env(env) == {"KEEP": "value"}
    assert len(env) == 3


def test_receipt_preserves_command_pass_despite_capture_error(runner, monkeypatch, tmp_path):
    _, output = enable_capture(monkeypatch, tmp_path)
    item = suite(runner, tmp_path)
    reports = output / runner.encoded_suite_id(item.id) / "1" / "reports"
    reports.mkdir(parents=True)
    receipt = reports / "trace-receipt-fixture.json"
    receipt.write_text(json.dumps({"command_exit_code": 0, "capture_errors": ["decode_failed"]}))
    with mock.patch.object(runner, "run_capture_command", return_value=SimpleNamespace(
            returncode=2, stdout="1 passed", stderr="")):
        result = runner.run_suite(item, tmp_path)
    assert result.status == "PASS"
    runner.record_attempt(result, tmp_path, 1)
    row = json.loads((tmp_path / "attempts" / (runner.encoded_suite_id(item.id) + ".jsonl")).read_text())
    assert row["process_capture_receipt"] == str(receipt)
    assert row["process_capture_exit_status"] == 2
    assert row["process_capture_command_exit_status"] == 0
    assert row["process_capture_errors"] == ["decode_failed"]


def test_attempt_evidence_precedes_retry(runner, monkeypatch, tmp_path):
    enable_capture(monkeypatch, tmp_path)
    item = suite(runner, tmp_path)
    attempts = tmp_path / "attempts" / (runner.encoded_suite_id(item.id) + ".jsonl")
    calls = []

    def spawn(command, **kwargs):
        calls.append(command)
        if len(calls) == 2:
            row = json.loads(attempts.read_text())
            assert row["attempt"] == 1
            assert row["status"] == "FAIL"
            assert "process_capture_receipt" in row
            assert row["process_capture_exit_status"] == 1
        return SimpleNamespace(returncode=1 if len(calls) == 1 else 0,
                               stdout="1 failed" if len(calls) == 1 else "1 passed", stderr="")

    monkeypatch.setattr(runner, "run_capture_command", spawn)
    result, count = runner._run_parallel_suite(item, tmp_path, 1)
    assert count == 2 and result.status == "PASS"
    assert len(attempts.read_text().splitlines()) == 2


@pytest.mark.parametrize("escalate", [False, True])
def test_timeout_terms_supervisor_before_kill(runner, escalate):
    process = mock.MagicMock()
    process.__enter__.return_value = process
    effects = [subprocess.TimeoutExpired(["supervisor"], 1817)]
    if escalate:
        effects.append(subprocess.TimeoutExpired(["supervisor"], 45))
    effects.append(("1 passed", ""))
    process.communicate.side_effect = effects
    with mock.patch.object(runner.subprocess, "Popen", return_value=process), \
            pytest.raises(subprocess.TimeoutExpired):
        runner.run_capture_command(["supervisor"], capture_output=True, text=True, timeout=1817)
    process.terminate.assert_called_once()
    assert process.kill.call_count == int(escalate)
    assert process.communicate.call_args_list[1] == mock.call(timeout=runner.PROCESS_CAPTURE_TERMINATE_GRACE_S)
    if escalate:
        assert process.method_calls.index(mock.call.terminate()) < process.method_calls.index(mock.call.kill())


def test_catalog_metadata_uses_predicate(runner, monkeypatch, tmp_path):
    enable_capture(monkeypatch, tmp_path)
    items = [suite(runner, tmp_path), suite(runner, tmp_path, "vitest")]
    items[1].id = "vitest"
    monkeypatch.setattr(runner, "build_suites", lambda: items)
    rows = runner.selection_catalog(ROOT)["suites"]
    assert rows[0]["capture"] == "linux-process-tree"
    assert "capture" not in rows[1]


@pytest.mark.parametrize("capturing", [False, True])
def test_deferred_pytest_suites_writer(runner, monkeypatch, tmp_path, capturing):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/ci"))
    spec = importlib.util.spec_from_file_location("capture_pytest_plugin_test", ROOT / "scripts/ci/pytest_selection.py")
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    output = tmp_path / "capture"
    output.mkdir()
    monkeypatch.setenv("LEAF_READSET_SUITE", "fixture")
    monkeypatch.setenv("LEAF_READSET_ATTEMPT", "2")
    if capturing:
        monkeypatch.setenv("LEAF_PROCESS_CAPTURE_DIR", str(output))
    options = {"leaf_output": str(tmp_path / "reports"), "leaf_repo": str(tmp_path)}
    config = SimpleNamespace(getoption=options.get, rootpath=tmp_path, option=SimpleNamespace())
    state = plugin.SelectionPlugin(config)
    assert "LEAF_PROCESS_CAPTURE_DIR" not in os.environ
    state.collection = ["sample.py::test_one", "sample.py::test_one", "sample.py::test_two"]
    try:
        state.pytest_sessionfinish(None, 0)
    finally:
        state.pytest_unconfigure(config)
    path = output / "capture-suites.json"
    if capturing:
        rows = json.loads(path.read_text())
        assert rows == [{"suite_id": "fixture", "attempt": 2, "worker": "tree",
                         "test_ids": ["fixture::sample.py::test_one", "fixture::sample.py::test_two"],
                         "outcomes_ref": str(output / ("attempts-" + state.shard + ".jsonl"))}]
        assert Path(rows[0]["outcomes_ref"]).is_file()
    else:
        assert not path.exists()
