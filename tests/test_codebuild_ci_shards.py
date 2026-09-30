"""Pin the relay shard envelope without running CI jobs."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
CI_PATH = ROOT / ".codebuild/ci.sh"


def ci_script():
    return CI_PATH.read_text(encoding="utf-8")


def shard_block():
    return ci_script().split("# BEGIN SHARD ENV\n", 1)[1].split(
        "# END SHARD ENV", 1)[0]


def test_shard_block_present_and_fail_closed():
    script = ci_script()
    assert script.startswith("#!/usr/bin/env bash\nset -euo pipefail\n# BEGIN SHARD ENV\n")
    block = shard_block()
    assert "ci.sh: refusing unsupported shard env CI_SHARD_INDEX=" in block
    assert "CI_SHARD_TOTAL=" in block
    assert "exit 2" in block
    assert "CI_SHARD_MODE=all" in block
    assert "CI_SHARD_MODE=shard" in block
    assert "LEAF_EVENT shard mode=" in block


def test_all_mode_lines_unchanged():
    original = subprocess.run(
        ["git", "show", "HEAD:.codebuild/ci.sh"], cwd=ROOT,
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    remaining = iter(ci_script().splitlines())
    for line in original:
        assert any(candidate == line for candidate in remaining), (
            "Original line missing or reordered: " + line
        )


def test_gate_call_gets_shard_flags_only_when_unfiltered():
    script = ci_script()
    guard = 'if [[ "${CI_SHARD_MODE:-all}" == shard && ${#only_args[@]} == 0 ]]; then\n'
    flags = '  only_args+=("--shard-"count "$CI_SHARD_TOTAL_N" "--shard-"index "$CI_SHARD_INDEX_N")\n'
    assert guard + flags + "fi\n" in script
    call = next(line for line in script.splitlines()
                if line.startswith("python scripts/run-all-gates.py "))
    assert '"${only_args[@]}"' in call
    assert "--result-json /tmp/gate-results/gate-result.json" in call
    assert script.index(guard) < script.index(call)
    assert ('if [[ "${CI_SHARD_MODE:-all}" == all || ${#only_args[@]} == 0 '
            '|| "$CI_SHARD_INDEX_N" == 0 ]]; then') in script
    assert (call + '\nelse\n  echo "gate: selected mode runs on shard 0 only"'
            '\n  gate_status=0\nfi') in script


def test_gate_proof_skipped_when_sharded():
    proof = ci_script().split("# LEAF_GATE_PROOF_BEGIN\n", 1)[1].split(
        "# LEAF_GATE_PROOF_END", 1)[0]
    assert proof.startswith(
        'if [[ "${CI_SHARD_MODE:-all}" == shard ]]; then\n'
        "  echo 'INFO: gate proof skipped in sharded mode; build verdict unchanged'\n"
        "else\n"
    )
    assert proof.endswith("fi\n")
    assert "--emit-proof" in proof


@pytest.mark.parametrize("total,index,expected", [
    (None, None, "all index=0 total=1"),
    ("", "", "all index=0 total=1"),
    ("1", None, "all index=0 total=1"),
    ("1", "0", "all index=0 total=1"),
    ("3", "0", "shard index=0 total=3"),
    ("3", "2", "shard index=2 total=3"),
    ("3", "3", None),
    ("9", "0", None),
    ("0", None, None),
    (None, "1", None),
    ("abc", None, None),
    ("3", None, None),
    ("1", "1", None),
    ("3", "abc", None),
    ("3", "-1", None),
    ("8", "7", "shard index=7 total=8"),
    ("03", "02", "shard index=2 total=3"),
    ("01", "00", "all index=0 total=1"),
])
def test_shard_env_parsing_matrix(total, index, expected):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is absent; shard env execution needs bash")
    env = os.environ.copy()
    for key, value in (("CI_SHARD_TOTAL", total), ("CI_SHARD_INDEX", index)):
        env.pop(key, None)
        if value is not None:
            env[key] = value
    result = subprocess.run(
        [bash, "-c", "set -euo pipefail\n" + shard_block()],
        env=env, capture_output=True, text=True,
    )
    if expected is None:
        assert result.returncode == 2, result.stdout + result.stderr
        assert "ci.sh: refusing unsupported shard env" in result.stderr
        assert "LEAF_EVENT shard" not in result.stdout
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.strip() == "LEAF_EVENT shard mode=" + expected
        assert result.stderr == ""
