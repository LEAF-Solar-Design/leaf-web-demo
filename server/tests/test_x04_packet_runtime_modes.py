"""Packet summaries and refusals match across safe optimized/hash-seed runtimes."""
import json
import subprocess
import sys
import pytest
from test_x04_packet_cross_binding import packet
from test_x04_review_subprocess_chain import BOOTSTRAP
from test_x04_untrusted_derived_counts import X04_ROOT


@pytest.mark.parametrize('seed', [1, 42, 123456])
def test_packet_runtime_modes(seed, tmp_path):
    source, output = tmp_path / 'packet.json', tmp_path / 'checked.json'
    unrelated = tmp_path / 'unrelated'; unrelated.mkdir()
    value = packet(); source.write_text(json.dumps(value), encoding='utf-8')
    raw = source.read_bytes()
    def run(optimized, chosen_seed, args):
        return subprocess.run([sys.executable, '-P', *(['-O'] if optimized else []), '-c', BOOTSTRAP,
            str(X04_ROOT / 'scripts/validate_x04_evidence_packet.py'), *map(str, args)],
            cwd=unrelated, env={'PYTHONHASHSEED': str(chosen_seed)}, capture_output=True, timeout=30)
    baseline = run(False, 0, [source])
    assert baseline.returncode == 0 and baseline.stderr == b''
    for optimized in (False, True):
        result = run(optimized, seed, [source])
        assert (result.returncode, result.stdout, result.stderr) == (0, baseline.stdout, b'')
        assert source.read_bytes() == raw
    value['artifacts'][0]['artifact'] = value['artifacts'][1]['artifact']
    source.write_text(json.dumps(value), encoding='utf-8'); forged = source.read_bytes()
    output.write_bytes(b'prior')
    baseline = run(False, 0, [source, '--out', output])
    assert baseline.returncode == 2 and baseline.stdout == b'' and b'refused' in baseline.stderr
    for optimized in (False, True):
        result = run(optimized, seed, [source, '--out', output])
        assert (result.returncode, result.stdout, result.stderr) == (2, b'', baseline.stderr)
        assert source.read_bytes() == forged and output.read_bytes() == b'prior'
    source.write_bytes(raw)
    assert run(False, seed, [source, '--out', output]).returncode == 0
    assert json.loads(output.read_bytes()) == json.loads(run(False, 0, [source]).stdout)
