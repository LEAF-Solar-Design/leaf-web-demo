"""Drawing binding decisions agree in network-denied runtime modes."""
import hashlib
import json
import subprocess
import sys
import pytest
from test_x04_packet_drawing_arguments import two_drawing_packet
from test_x04_review_subprocess_chain import BOOTSTRAP
from test_x04_untrusted_derived_counts import X04_ROOT
import validate_x04_evidence_packet as checker


@pytest.mark.parametrize('stdin', [False, True])
@pytest.mark.parametrize('seed', [1, 42, 123456])
def test_drawing_binding_runtime_modes(stdin, seed, tmp_path):
    value = two_drawing_packet()
    raw = json.dumps(value).encode()
    digest = hashlib.sha256(raw).hexdigest()
    source = tmp_path / 'packet.json'
    source.write_bytes(raw)
    output = tmp_path / 'summary.json'
    other = tmp_path / 'unrelated'
    other.mkdir()
    script = str(X04_ROOT / 'scripts/validate_x04_evidence_packet.py')

    def run(optimized, mode, publish=True):
        options = ['-' if stdin else str(source), '--expected-packet-sha256', digest]
        if publish:
            options += ['--out', str(output)]
        if mode != 'omitted':
            options += ['--expected-artifact-drawing', 'a=' + ('wrong' if mode == 'wrong' else 'drawing=one')]
            if mode != 'partial':
                options += ['--expected-artifact-drawing', 'b=drawing-two']
        return subprocess.run([sys.executable, '-P', *(['-O'] if optimized else []), '-c', BOOTSTRAP,
                               script, *options], input=raw if stdin else None, cwd=other,
                              env={'PYTHONHASHSEED': str(seed)}, capture_output=True, timeout=30)

    baseline = run(False, 'matching', False)
    assert baseline.returncode == 0 and baseline.stderr == b''
    refusals = {}
    for optimized in (False, True):
        for mode in ('matching', 'wrong', 'partial', 'omitted'):
            output.write_bytes(b'previous')
            result = run(optimized, mode)
            assert result.stdout == b'' and source.read_bytes() == raw
            if mode.startswith('wrong'):
                assert result.returncode == 2 and output.read_bytes() == b'previous'
                assert b'drawing.id exactly' in result.stderr and b'Traceback' not in result.stderr
                refusals.setdefault(mode, result.stderr)
                assert result.stderr == refusals[mode]
            else:
                assert result.returncode == 0 and result.stderr == b''
                assert output.read_bytes() == baseline.stdout
    assert run(True, 'matching').returncode == 0
    assert output.read_bytes() == baseline.stdout
    assert json.loads(output.read_bytes()) == checker.validate_packet(value)
    assert not list(tmp_path.glob('.x04-*'))
