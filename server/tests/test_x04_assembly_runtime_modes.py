"""Safe optimized/hash-seed runtimes produce identical packets and stale refusals."""
import json
import subprocess
import sys
import pytest
from test_x04_packet_assembly import manifest_files
from test_x04_review_subprocess_chain import BOOTSTRAP
from test_x04_untrusted_derived_counts import X04_ROOT
from compare_x04_offline_evidence import compare_evidence


@pytest.mark.parametrize('seed', [1, 42, 123456])
def test_assembly_runtime_equivalence(seed, tmp_path):
    source, manifest, expected = manifest_files(tmp_path)
    originals = {p: p.read_bytes() for p in tmp_path.iterdir()}
    unrelated = tmp_path / 'unrelated'; unrelated.mkdir()
    def run(script, args, optimized=False, chosen_seed=0):
        return subprocess.run([sys.executable, '-P', *(['-O'] if optimized else []), '-c', BOOTSTRAP,
            str(X04_ROOT / 'scripts' / script), *map(str, args)], cwd=unrelated,
            env={'PYTHONHASHSEED': str(chosen_seed)}, capture_output=True, timeout=30)
    args = [source, '--max-total-input-bytes', '1000000']
    baseline = run('assemble_x04_evidence_packet.py', args)
    assert baseline.returncode == 0 and baseline.stderr == b'' and json.loads(baseline.stdout) == expected
    packet = tmp_path / 'packet.json'; packet.write_bytes(baseline.stdout)
    summary = run('validate_x04_evidence_packet.py', [packet])
    assert summary.returncode == 0 and summary.stderr == b''
    assert json.loads(summary.stdout)['comparisons'][0]['report']['count'] == {'before': 6, 'after': 1, 'delta': -5}
    for optimized in (False, True):
        result = run('assemble_x04_evidence_packet.py', args, optimized, seed)
        assert (result.returncode, result.stdout, result.stderr) == (0, baseline.stdout, b'')
        checked = run('validate_x04_evidence_packet.py', [packet], optimized, seed)
        assert (checked.returncode, checked.stdout, checked.stderr) == (0, summary.stdout, b'')
    assert all(p.read_bytes() == data for p, data in originals.items())
    report = tmp_path / 'c.json'; artifact = expected['artifacts'][0]['artifact']
    report.write_text(json.dumps(compare_evidence(artifact, artifact)), encoding='utf-8')
    forged = report.read_bytes(); output = tmp_path / 'output.json'; output.write_bytes(b'previous')
    failed = run('assemble_x04_evidence_packet.py', args + ['--out', output])
    assert failed.returncode == 2 and failed.stdout == b'' and b'contradict' in failed.stderr
    for optimized in (False, True):
        result = run('assemble_x04_evidence_packet.py', args + ['--out', output], optimized, seed)
        assert (result.returncode, result.stdout, result.stderr) == (2, b'', failed.stderr)
        assert output.read_bytes() == b'previous' and report.read_bytes() == forged
    report.write_bytes(originals[report])
    assert run('assemble_x04_evidence_packet.py', args + ['--out', output]).returncode == 0
    assert output.read_bytes() == baseline.stdout and all(p.read_bytes() == data for p, data in originals.items())
