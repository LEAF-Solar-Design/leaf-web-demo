"""Network-denied file/stdin runtimes preserve optional inventory decisions."""
import hashlib
import json
import subprocess
import sys
import pytest
from test_x04_packet_cross_binding import packet
from test_x04_review_subprocess_chain import BOOTSTRAP
from test_x04_untrusted_derived_counts import X04_ROOT
import validate_x04_evidence_packet as checker


@pytest.mark.parametrize('stdin', [False, True])
@pytest.mark.parametrize('seed', [1, 42, 123456])
def test_inventory_runtime_matrix_and_retry(stdin, seed, tmp_path):
    value = packet(); raw = json.dumps(value).encode(); digest = hashlib.sha256(raw).hexdigest()
    source = tmp_path / 'packet.json'; source.write_bytes(raw); output = tmp_path / 'summary.json'
    other = tmp_path / 'unrelated'; other.mkdir(); script = str(X04_ROOT / 'scripts/validate_x04_evidence_packet.py')
    def run(optimized, selected_seed, ids, out=True):
        args = ['-' if stdin else str(source), '--expected-packet-sha256', digest]
        if out: args += ['--out', str(output)]
        for identity in ids or []: args += ['--expected-artifact-id', identity]
        return subprocess.run([sys.executable, '-P', *(['-O'] if optimized else []), '-c', BOOTSTRAP, script, *args],
            input=raw if stdin else None, cwd=other, env={'PYTHONHASHSEED': str(selected_seed)},
            capture_output=True, timeout=30)
    baseline = run(False, 0, ['a', 'b'], False)
    assert baseline.returncode == 0 and baseline.stderr == b''
    refusal = None
    for optimized in (False, True):
        for ids in (['b', 'a'], ['a'], None):
            output.write_bytes(b'previous'); actual = run(optimized, seed, ids)
            assert actual.stdout == b'' and source.read_bytes() == raw
            if ids == ['a']:
                assert actual.returncode == 2 and output.read_bytes() == b'previous'
                assert b'expected inventory' in actual.stderr and b'Traceback' not in actual.stderr
                if refusal is None: refusal = actual.stderr
                assert actual.stderr == refusal
            else:
                assert actual.returncode == 0 and actual.stderr == b'' and output.read_bytes() == baseline.stdout
    assert run(True, seed, ['a', 'b']).returncode == 0 and output.read_bytes() == baseline.stdout
    assert json.loads(output.read_bytes()) == checker.validate_packet(value)
    assert not list(tmp_path.glob('.x04-*'))
