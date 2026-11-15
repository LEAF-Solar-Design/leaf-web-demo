"""Execute the frozen CI shell sections without setup, installs, or network."""

import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest


CI = Path(__file__).resolve().parents[1] / ".codebuild" / "ci.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash unavailable")


def section(name):
    return CI.read_text(encoding="utf-8").split(f"# {name}_BEGIN\n", 1)[1].split(
        f"# {name}_END", 1
    )[0]


# This stand-in implements only the frozen helper CLI, including a missing
# timing input. The tests exercise shell trust/loading/error behavior rather
# than duplicating the partition helper's algorithm tests.
HELPER = r'''
import argparse
import json
import os
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("command", choices=["plan"])
for name in ("catalog", "only-args", "timings", "shard-count", "shard-index", "output"):
    p.add_argument("--" + name, required=True)
a = p.parse_args()
assert json.loads(Path(a.catalog).read_text())["schema"] == "leaf.ci.catalog.v1"
assert Path(a.only_args).read_bytes() == b"--only\0alpha\0--only\0beta\0"
if os.environ.get("PLAN_ERROR") == "1":
    raise SystemExit(2)
count, index = int(a.shard_count), int(a.shard_index)
shards = [["alpha"], ["beta"]] + [[] for _ in range(count - 2)]
plan = dict(schema="leaf.ci.selected-shards.v1", selected_ids=["alpha", "beta"],
            shards=shards, shard_ids=shards[index], shard_count=count, shard_index=index,
            algorithm="lpt" if Path(a.timings).exists() else "equal-count")
bad = os.environ.get("BAD_PLAN", "")
if bad == "schema": plan["schema"] = "wrong"
if bad == "index": plan["shard_index"] = 0
if bad == "count": plan["shard_count"] = 99
if bad == "local": plan["shard_ids"] = []
if bad == "missing": plan["shards"][1] = []
if bad == "duplicate": plan["shards"][1] = ["alpha"]
if bad == "shape": plan["shard_ids"] = "beta"
Path(a.output).write_text(json.dumps(plan))
'''


def run_shell(tmp_path, *, index=1, mode="shard", selected=True,
              diagnostic=False, missing_helper=False, missing_timing=False,
              plan_error=False, bad_plan="", real_helper=False):
    selection = tmp_path / "private selection"
    selection.mkdir()
    (selection / "only-args.nul").write_bytes(b"--only\0alpha\0--only\0beta\0")
    (selection / "runner-catalog.json").write_text(json.dumps({
        "schema": "leaf.ci.catalog.v1", "suites": [{"id": "alpha"}, {"id": "beta"}]
    }))
    frozen = tmp_path / "frozen-helper.py"
    frozen.write_text((CI.parents[1] / "scripts/ci/selected_shards.py").read_text()
                      if real_helper else HELPER)
    snapshot = tmp_path / "frozen-timing.json"
    snapshot.write_text('{"schema":"leaf.ci.suite-seconds.v1"}')
    source = CI.read_text(encoding="utf-8")
    runner = next(line for line in source.splitlines()
                  if line.startswith("python scripts/run-all-gates.py --jobs"))
    script = r'''
set -euo pipefail
git() {
  printf '%s\n' "$*" >> "$GIT_CALLS"
  [[ "$1" == --no-replace-objects && "$2" == show ]] || return 2
  case "$3" in
    trusted:scripts/ci/selected_shards.py)
      [[ "$MISSING_HELPER" == 0 ]] || return 1
      cat "$FROZEN_HELPER" ;;
    trusted:scripts/ci/selected-suite-seconds.json)
      [[ "$MISSING_TIMING" == 0 ]] || return 1
      cat "$FROZEN_TIMING" ;;
    *) return 2 ;;
  esac
}
python() {
  if [[ "${1:-}" == scripts/run-all-gates.py ]]; then
    printf '%s\0' "$@" > "$RUNNER_ARGS"
  else
    command REAL_PYTHON "$@"
  fi
}
npm() { printf 'setup\n' > "$NPM_MARKER"; }
TRUSTED_SHA=trusted
CI_SHARD_TOTAL_N=3
CI_SHARD_INDEX_N=TEST_INDEX
CI_SHARD_MODE=TEST_MODE
selection_dir=TEST_SELECTION
only_args=(TEST_ONLY_ARGS)
'''.replace("REAL_PYTHON", shlex.quote(sys.executable)).replace(
        "TEST_INDEX", str(index)).replace("TEST_MODE", shlex.quote(mode)).replace(
        "TEST_SELECTION", shlex.quote(str(selection))).replace(
        "TEST_ONLY_ARGS", "--only alpha --only beta" if selected else "")
    script += section("LEAF_SELECTED_SHARDS")
    script += '\nnpm ci\n'
    script += section("LEAF_SELECTED_SHARDS_GATE")
    script += '\ngate_fail_fast_args=()\ngate_tier_args=()\nLEAF_CI_SCRIPT_PATH=frozen-ci.sh\ngate_status=0\n'
    script += runner + '\nelse\n' + section("LEAF_SELECTED_SHARDS_GATE_FALLBACK")
    script += '\nfi\nexit "$gate_status"\n'
    env = dict(os.environ, FROZEN_HELPER=str(frozen), FROZEN_TIMING=str(snapshot),
               MISSING_HELPER=str(int(missing_helper)), MISSING_TIMING=str(int(missing_timing)),
               PLAN_ERROR=str(int(plan_error)), BAD_PLAN=bad_plan,
               GIT_CALLS=str(tmp_path / "git-calls"), RUNNER_ARGS=str(tmp_path / "runner-args"),
               NPM_MARKER=str(tmp_path / "npm-marker"),
               LEAF_DIAGNOSTIC_PROFILE="web-demo-suite" if diagnostic else "",
               LEAF_SELECTED_TIMINGS="/untrusted/override.json")
    result = subprocess.run([BASH, "-c", script], env=env, text=True, capture_output=True)
    args_file = tmp_path / "runner-args"
    args = args_file.read_bytes().decode().rstrip("\0").split("\0") if args_file.exists() else []
    return result, args, selection


def test_empty_nonzero_exits_before_setup(tmp_path):
    result, args, _ = run_shell(tmp_path, index=2)
    assert result.returncode == 0, result.stderr
    assert "exiting before setup" in result.stdout
    assert not args
    assert not (tmp_path / "npm-marker").exists()


@pytest.mark.parametrize("index", [0, 1])
def test_selected_shards_keep_original_union_and_frozen_flags(tmp_path, index):
    result, args, selection = run_shell(tmp_path, index=index)
    assert result.returncode == 0, result.stderr
    start = args.index("--only")
    assert args[start:start + 10] == [
        "--only", "alpha", "--only", "beta", "--shard-count", "3",
        "--shard-index", str(index), "--selected-shard-plan", str(selection / "selected-shards.json")]
    assert (selection / "selected-shards.json").stat().st_mode & 0o777 == 0o400
    assert (tmp_path / "npm-marker").exists()
    calls = (tmp_path / "git-calls").read_text().splitlines()
    assert calls == ["--no-replace-objects show trusted:scripts/ci/selected_shards.py",
                     "--no-replace-objects show trusted:scripts/ci/selected-suite-seconds.json"]


@pytest.mark.parametrize("mode", ["all", "shard"])
def test_full_mode_keeps_existing_flags(tmp_path, mode):
    result, args, _ = run_shell(tmp_path, mode=mode, selected=False)
    assert result.returncode == 0, result.stderr
    assert ("--shard-count" in args) == (mode == "shard")
    assert "--selected-shard-plan" not in args
    assert not (tmp_path / "git-calls").exists()


def test_unsharded_selection_unchanged(tmp_path):
    result, args, _ = run_shell(tmp_path, mode="all")
    assert result.returncode == 0, result.stderr
    assert args[args.index("--only"):][:4] == ["--only", "alpha", "--only", "beta"]
    assert "--shard-count" not in args
    assert not (tmp_path / "git-calls").exists()


@pytest.mark.parametrize("index", [0, 1])
def test_diagnostic_excluded(tmp_path, index):
    result, args, _ = run_shell(tmp_path, diagnostic=True, index=index)
    assert result.returncode == 0, result.stderr
    assert bool(args) == (index == 0)
    assert "--selected-shard-plan" not in args
    assert not (tmp_path / "git-calls").exists()


@pytest.mark.parametrize("failure", ["missing_helper", "plan_error"])
def test_fallback_shard_zero_retains_union(tmp_path, failure):
    result, args, _ = run_shell(tmp_path, index=0, **{failure: True})
    assert result.returncode == 0, result.stderr
    assert "conservative fallback" in result.stderr
    assert "--shard-count" not in args
    assert args[args.index("--only"):][:4] == ["--only", "alpha", "--only", "beta"]


@pytest.mark.parametrize("failure", ["missing_helper", "plan_error"])
def test_failed_partition_nonzero_never_green(tmp_path, failure):
    result, args, _ = run_shell(tmp_path, **{failure: True})
    assert result.returncode == 2
    assert "missing-slice GREEN" in result.stderr
    assert not args
    assert not (tmp_path / "npm-marker").exists()


@pytest.mark.parametrize("bad_plan", ["schema", "index", "count", "local", "missing", "duplicate", "shape"])
def test_invalid_partition_fails_closed(tmp_path, bad_plan):
    result, args, _ = run_shell(tmp_path, bad_plan=bad_plan)
    assert result.returncode == 2
    assert "partition rejected" in result.stderr
    assert not args
    assert not (tmp_path / "npm-marker").exists()


def test_missing_timing_uses_equal_partition(tmp_path):
    result, args, _ = run_shell(tmp_path, missing_timing=True)
    assert result.returncode == 0, result.stderr
    assert "algorithm=equal-count" in result.stderr
    assert "--selected-shard-plan" in args


@pytest.mark.parametrize("index", [0, 1])
def test_real_helper_shell_plan_drives_real_runner_selection(tmp_path, monkeypatch, index):
    result, args, selection = run_shell(tmp_path, index=index, real_helper=True)
    assert result.returncode == 0, result.stderr
    path = selection / "selected-shards.json"
    plan = json.loads(path.read_text())
    assert plan["selected_ids"] == ["alpha", "beta"]
    assert plan["algorithm"] == "equal-count"
    assert args[args.index("--selected-shard-plan") + 1] == str(path)

    spec = importlib.util.spec_from_file_location(
        "shell_selected_runner", CI.parents[1] / "scripts/run-all-gates.py")
    runner = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, runner)
    spec.loader.exec_module(runner)
    suites = [runner.Suite(sid, sid, "pytest", CI.parents[1], ["fake"], 1)
              for sid in ("alpha", "beta", "unselected")]
    ran = []
    monkeypatch.setattr(runner, "build_suites", lambda: suites)
    monkeypatch.setattr(runner, "migrate_gate_database", lambda _: None)
    monkeypatch.setattr(runner, "record_attempt", lambda *args: None)
    monkeypatch.setattr(runner, "print_scoreboard", lambda *args: None)

    def execute(suite, log_dir, attempt):
        ran.append(suite.id)
        return runner.Result(suite, "PASS", "1", 0.01, counts={"got": 1, "passed": 1})

    monkeypatch.setattr(runner, "run_suite_guarded", execute)
    # Feed the original union and coordinates captured at the actual handoff.
    start = args.index("--only")
    stop = args.index("--selected-shard-plan") + 2
    output = tmp_path / "execution.json"
    monkeypatch.setattr(sys, "argv", ["runner", *args[start:stop],
                                     "--log-dir", str(tmp_path), "--result-json", str(output)])
    assert runner.main() == 0
    assert ran == plan["shards"][index]
    payload = json.loads(output.read_text())
    assert payload["suite_ids"] == ran
    assert payload["mode"] == payload["selection_mode"] == payload["execution_mode"] == "selected"


def test_preflight_precedes_setup_and_shell_syntax():
    source = CI.read_text(encoding="utf-8")
    assert source.index("# LEAF_SELECTED_SHARDS_END") < source.index("LEAF_T start setup")
    result = subprocess.run([BASH, "-n", str(CI)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
