"""Runtime optimization leaves trusted validation and refusal behavior intact."""
import json
import subprocess
import sys
import pytest
from test_x04_review_subprocess_chain import BOOTSTRAP
from test_x04_untrusted_derived_counts import worked_example, X04_ROOT


@pytest.mark.parametrize('kind', ['comparison', 'batch'])
def test_optimized_and_normal_results(kind, tmp_path):
    good, forged, output = [tmp_path / name for name in ('good', 'forged', 'output')]
    value = worked_example(); good.write_text(json.dumps(value), encoding='utf-8')
    value['result']['count'] = 999; forged.write_text(json.dumps(value), encoding='utf-8')
    cwd = tmp_path / 'unrelated'; cwd.mkdir()
    script = 'compare_x04_offline_evidence.py' if kind == 'comparison' else 'batch_validate_x04_evidence.py'
    def run(optimized, args):
        return subprocess.run([sys.executable, '-P', *(['-O'] if optimized else []), '-c', BOOTSTRAP,
            str(X04_ROOT / 'scripts' / script), *map(str, args)], cwd=cwd,
            env={'PYTHONHASHSEED': '0'}, capture_output=True, timeout=30)
    results = []
    for optimized in (False, True):
        valid_args = [good, good] if kind == 'comparison' else [good, good, good]
        valid = run(optimized, valid_args)
        assert valid.returncode == 0 and valid.stderr == b''
        report = json.loads(valid.stdout)
        if kind == 'comparison': assert report['count'] == {'before': 7, 'after': 7, 'delta': 0}
        else: assert (report['total'], report['passed'], report['failed']) == (3, 3, 0)
        bad_args = [good, forged] if kind == 'comparison' else [good, forged, good]
        refusal = run(optimized, bad_args)
        assert refusal.returncode == (2 if kind == 'comparison' else 1)
        if kind == 'comparison': assert refusal.stdout == b'' and b'refused' in refusal.stderr
        else:
            failed = json.loads(refusal.stdout)
            assert [row['valid'] for row in failed['results']] == [True, False, True]
            assert (failed['total'], failed['passed'], failed['failed']) == (3, 2, 1)
        output.write_bytes(b'previous')
        atomic = run(optimized, [*bad_args, '--out', output, '--max-output-bytes', '1'])
        assert atomic.returncode == 2 and atomic.stdout == b'' and b'refused' in atomic.stderr
        assert output.read_bytes() == b'previous'
        results.append((valid.stdout, refusal.stdout, refusal.stderr, atomic.stderr))
    assert results[0] == results[1] and not list(tmp_path.glob('.x04-*'))
