"""Network-denied safe-path runtimes preserve optional artifact identity decisions."""
import json
import subprocess
import sys
import pytest
from test_x04_comparison_delta_algebra import artifact
from test_x04_review_subprocess_chain import BOOTSTRAP
from test_x04_untrusted_derived_counts import X04_ROOT


@pytest.mark.parametrize('stdin', [False, True])
@pytest.mark.parametrize('seed', [1, 42, 123456])
def test_expected_identity_runtime_modes_and_retry(stdin, seed, tmp_path):
    value = artifact(0); identity = 'synthetic-\u00e9'
    value['drawing']['id'] = value['request']['drawing_id'] = identity
    raw = json.dumps(value).encode(); source = tmp_path / 'artifact.json'; source.write_bytes(raw)
    output = tmp_path / 'checked.json'; other = tmp_path / 'unrelated'; other.mkdir()
    script = str(X04_ROOT / 'scripts/validate_x04_offline_evidence.py')
    def run(optimized, selected_seed, expected, out=True):
        args = ['-' if stdin else str(source)]
        if out: args += ['--out', str(output)]
        if expected is not None: args += ['--expected-drawing-id', expected]
        return subprocess.run([sys.executable, '-P', *(['-O'] if optimized else []), '-c', BOOTSTRAP, script, *args],
            input=raw if stdin else None, cwd=other, env={'PYTHONHASHSEED': str(selected_seed)},
            capture_output=True, timeout=30)
    baseline = run(False, 0, identity, False)
    assert baseline.returncode == 0 and baseline.stderr == b''
    wrong = identity + '-wrong'; refusal = None
    for optimized in (False, True):
        for expected in (identity, wrong, None):
            output.write_bytes(b'previous'); actual = run(optimized, seed, expected)
            assert actual.stdout == b'' and source.read_bytes() == raw
            if expected == wrong:
                assert actual.returncode == 2 and output.read_bytes() == b'previous'
                assert b'Expected drawing ID' in actual.stderr and b'Traceback' not in actual.stderr
                if refusal is None: refusal = actual.stderr
                assert actual.stderr == refusal
            else:
                assert actual.returncode == 0 and actual.stderr == b'' and output.read_bytes() == baseline.stdout
    assert run(True, seed, identity).returncode == 0 and output.read_bytes() == baseline.stdout
    assert json.loads(output.read_bytes())['recomputed_result'] == {
        'count': 6, 'highlight_handles': ['A'], 'original_bytes_integrity': 'unverified'}
    assert not list(tmp_path.glob('.x04-*'))
