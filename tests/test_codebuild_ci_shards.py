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
    merge_base = subprocess.run(
        ["git", "merge-base", "HEAD", "origin/main"], cwd=ROOT,
        capture_output=True, text=True,
    )
    base = merge_base.stdout.strip() if merge_base.returncode == 0 else "HEAD"
    original = subprocess.run(
        ["git", "show", f"{base}:.codebuild/ci.sh"], cwd=ROOT,
        check=True, capture_output=True, text=True,
    ).stdout.replace('--retry 1 --fail-fast ', '--retry 1 ').replace(
        '--retry 1 "${gate_fail_fast_args[@]}" ', '--retry 1 ').splitlines()
    # Allow the fail-fast gate flag (literal or the capture-aware array) while preserving
    # the other baseline lines.
    remaining = iter(ci_script().replace('--retry 1 --fail-fast ', '--retry 1 ').replace(
        '--retry 1 "${gate_fail_fast_args[@]}" ', '--retry 1 ').splitlines())
    for line in original:
        assert any(candidate == line for candidate in remaining), (
            "Original line missing or reordered: " + line
        )


def test_gate_call_gets_shard_flags_only_when_unfiltered():
    script = ci_script()
    guard = 'if [[ "${CI_SHARD_MODE:-all}" == shard && ${#only_args[@]} == 0 ]]; then\n'
    flags = '  only_args+=(--shard-count "$CI_SHARD_TOTAL_N" --shard-index "$CI_SHARD_INDEX_N")\n'
    assert guard + flags + "fi\n" in script
    call = next(line for line in script.splitlines()
                if line.startswith("python scripts/run-all-gates.py "))
    assert '"${gate_fail_fast_args[@]}"' in call
    fail_fast = (
        'gate_fail_fast_args=(--fail-fast)\n'
        'if [[ "${process_capture_active:-0}" == 1 ]]; then\n'
        "  # Capture records every suite's reads; a failure still fails the job below.\n"
        '  gate_fail_fast_args=()\n'
        'fi\n'
    )
    assert fail_fast in script
    assert script.index(fail_fast) < script.index(call)
    assert [line for line in script.splitlines()
            if line.lstrip().startswith("gate_fail_fast_args=")] == [
        'gate_fail_fast_args=(--fail-fast)', '  gate_fail_fast_args=()',
    ]
    assert call == (
        'python scripts/run-all-gates.py --jobs "${LEAF_GATE_JOBS:-auto}" '
        '--retry 1 "${gate_fail_fast_args[@]}" '
        '--result-json /tmp/gate-results/gate-result.json '
        '--log-dir /tmp/gate-logs "${only_args[@]}" || gate_status=$?'
    )
    assert '"${only_args[@]}"' in call
    assert "--result-json /tmp/gate-results/gate-result.json" in call
    assert script.index(guard) < script.index(call)
    assert ('if [[ "${CI_SHARD_MODE:-all}" == all || ${#only_args[@]} == 0 '
            '|| "$CI_SHARD_INDEX_N" == 0 ]]; then') in script
    assert (call + '\npython -I -B scripts/ci/retain_failed_gate_logs.py \\\n'
            '  --result /tmp/gate-results/gate-result.json --logs /tmp/gate-logs '
            '--output /tmp/gate-results/failed-gate-logs \\\n'
            "  || echo 'WARNING: failed gate log retention unavailable' >&2\n"
            'if [[ "$postgres_status" != 0 && "$gate_status" == 0 ]]; then\n'
            '  echo "FATAL: the gate passed without its PostgreSQL proofs; failing the build" >&2\n'
            '  gate_status=1\nfi\n'
            'else\n  echo "gate: selected mode runs on shard 0 only"'
            '\n  gate_status=0\nfi') in script


def test_gate_proof_skipped_when_sharded():
    script = ci_script()
    proof_region = script.split("# LEAF_GATE_PROOF_BEGIN\n", 1)[1].split(
        "# LEAF_GATE_PROOF_END", 1)[0]
    proof, summary = proof_region.split("# LEAF_PROCESS_CAPTURE_SUMMARY_BEGIN\n", 1)
    assert summary.endswith("# LEAF_PROCESS_CAPTURE_SUMMARY_END\n")
    assert (script.index("# LEAF_GATE_PROOF_BEGIN\n")
            < script.index("# LEAF_PROCESS_CAPTURE_SUMMARY_BEGIN\n")
            < script.index("# LEAF_GATE_PROOF_END"))
    assert proof.startswith(
        'if [[ "${CI_SHARD_MODE:-all}" == shard ]]; then\n'
        "  echo 'INFO: gate proof skipped in sharded mode; build verdict unchanged'\n"
        "else\n"
    )
    assert proof.endswith("fi\n")
    assert "--emit-proof" in proof
    assert ('if [[ "$gate_status" == 0 ]] && python -I -B - '
            '"$selection_dir/detail.json" <<\'LEAF_GATE_PROOF_ELIGIBLE\'\n') in proof
    assert script.endswith('# LEAF_GATE_PROOF_END\nexit "$gate_status"\n')


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
