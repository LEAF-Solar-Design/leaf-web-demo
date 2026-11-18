"""Network-denied optimized processes preserve file/stdin schema decisions."""
import hashlib
import json
import subprocess
import sys
import pytest
from test_x04_comparison_report_contract import comparison
from test_x04_batch_report_contract import batch
from test_x04_review_subprocess_chain import BOOTSTRAP
from test_x04_untrusted_derived_counts import X04_ROOT


@pytest.mark.parametrize('factory,kind', [(comparison, 'comparison'), (batch, 'batch')])
@pytest.mark.parametrize('stdin', [False, True])
@pytest.mark.parametrize('seed', [1, 42, 123456])
def test_guard_runtime_modes_and_corrected_retry(factory, kind, stdin, seed, tmp_path):
    value = factory(); raw = json.dumps(value).encode(); digest = hashlib.sha256(raw).hexdigest()
    source = tmp_path / 'report.json'; source.write_bytes(raw)
    output = tmp_path / 'summary.json'; unrelated = tmp_path / 'unrelated'; unrelated.mkdir()
    script = str(X04_ROOT / 'scripts/validate_x04_review_report.py')
    def run(optimized, selected_seed, selected, out=True):
        args = ['-' if stdin else str(source), '--expected-report-sha256', digest]
        if out: args += ['--out', str(output)]
        if selected is not None: args += ['--expected-report-schema', selected]
        return subprocess.run([sys.executable, '-P', *(['-O'] if optimized else []), '-c', BOOTSTRAP, script, *args],
            input=raw if stdin else None, cwd=unrelated, env={'PYTHONHASHSEED': str(selected_seed)},
            capture_output=True, timeout=30)
    baseline = run(False, 0, kind, False)
    assert baseline.returncode == 0 and baseline.stderr == b''
    wrong = 'batch' if kind == 'comparison' else 'comparison'; refusal = None
    for optimized in (False, True):
        for selected in (kind, wrong, None):
            output.write_bytes(b'previous')
            actual = run(optimized, seed, selected)
            assert actual.stdout == b'' and source.read_bytes() == raw
            if selected == wrong:
                assert actual.returncode == 2 and output.read_bytes() == b'previous'
                assert b'Expected ' in actual.stderr and b'Traceback' not in actual.stderr
                if refusal is None: refusal = actual.stderr
                assert actual.stderr == refusal
            else:
                assert actual.returncode == 0 and actual.stderr == b'' and output.read_bytes() == baseline.stdout
    assert run(True, seed, kind).returncode == 0 and output.read_bytes() == baseline.stdout
    summary = json.loads(output.read_bytes())
    assert summary['original_bytes_integrity'] == 'unverified' and summary['underlying_artifacts_revalidated'] is False
    assert not list(tmp_path.glob('.x04-*'))
