"""Saved-report runtime modes preserve exact offline results and refusals."""
import json
import subprocess
import sys
import pytest
from test_x04_comparison_report_contract import comparison
from test_x04_batch_report_contract import batch
from test_x04_review_subprocess_chain import BOOTSTRAP
from test_x04_untrusted_derived_counts import X04_ROOT


@pytest.mark.parametrize('factory', [comparison, batch])
@pytest.mark.parametrize('seed', [1, 42, 123456])
def test_optimized_and_hash_seed_modes(factory, seed, tmp_path):
    source, target = tmp_path / 'report.json', tmp_path / 'output.json'
    unrelated = tmp_path / 'unrelated'; unrelated.mkdir()
    report = factory()
    source.write_text(json.dumps(report), encoding='utf-8')
    raw = source.read_bytes()
    script = str(X04_ROOT / 'scripts' / 'validate_x04_review_report.py')
    def run(optimized, arguments, selected_seed):
        return subprocess.run([sys.executable, '-P', *(['-O'] if optimized else []),
            '-c', BOOTSTRAP, script, *map(str, arguments)], cwd=unrelated,
            env={'PYTHONHASHSEED': str(selected_seed)}, capture_output=True, timeout=30)
    baseline = run(False, [source], 0)
    assert baseline.returncode == 0 and baseline.stderr == b''
    for optimized in (False, True):
        actual = run(optimized, [source], seed)
        assert (actual.returncode, actual.stdout, actual.stderr) == (
            baseline.returncode, baseline.stdout, baseline.stderr)
    if factory is comparison:
        report['count']['delta'] += 1
    else:
        report['results'][0]['index'] = 99
    source.write_text(json.dumps(report), encoding='utf-8')
    forged = source.read_bytes()
    target.write_bytes(b'prior output')
    baseline = run(False, [source, '--out', target], 0)
    assert baseline.returncode == 2 and baseline.stdout == b'' and b'refused' in baseline.stderr
    for optimized in (False, True):
        actual = run(optimized, [source, '--out', target], seed)
        assert (actual.returncode, actual.stdout, actual.stderr) == (
            baseline.returncode, baseline.stdout, baseline.stderr)
        assert target.read_bytes() == b'prior output' and source.read_bytes() == forged
    source.write_bytes(raw)
    assert run(True, [source, '--out', target], seed).returncode == 0
    checked = json.loads(target.read_bytes())
    assert checked['validation_scope'] == 'saved_report_internal_consistency'
    assert checked['underlying_artifacts_revalidated'] is False
    assert checked['original_bytes_integrity'] == 'unverified'
    assert not list(tmp_path.glob('.x04-*'))
