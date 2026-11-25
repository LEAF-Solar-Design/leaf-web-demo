"""Fixed comparison and ordered batch reports ignore hash randomization."""
import json
import pytest
from test_x04_review_subprocess_chain import run_tool
from test_x04_comparison_delta_algebra import artifact


@pytest.mark.parametrize('seed', [1, 42, 123456])
def test_fixed_reports_byte_identical_across_hashseeds(seed, tmp_path):
    cwd = tmp_path / 'unrelated'; cwd.mkdir()
    left, right, broken = [tmp_path / name for name in ('left', 'right', 'broken')]
    left.write_text(json.dumps(artifact(2)), encoding='utf-8')
    right.write_text(json.dumps(artifact(0)), encoding='utf-8')
    broken.write_bytes(b'{broken')
    for script, args, expected_status in [
        ('compare_x04_offline_evidence.py', [left, right], 0),
        ('batch_validate_x04_evidence.py', [left, broken, right, left], 1)]:
        baseline = run_tool(script, args, cwd, 0)
        result = run_tool(script, args, cwd, seed)
        assert baseline.returncode == result.returncode == expected_status
        assert baseline.stdout == result.stdout and baseline.stderr == result.stderr == b''
        parsed = json.loads(result.stdout)
        if expected_status == 0:
            assert parsed['count']['delta'] == -1 and parsed['highlight_handles'] == {'removed': ['B'], 'added': []}
        else:
            assert (parsed['total'], parsed['passed'], parsed['failed']) == (4, 3, 1)
            assert [row['index'] for row in parsed['results']] == [0, 1, 2, 3]
            assert [row['path'] for row in parsed['results']] == list(map(str, args))
