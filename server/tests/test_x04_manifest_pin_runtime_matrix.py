"""Doubly pinned strict assembly is deterministic across safe optimized runtimes."""
import hashlib
import json
import subprocess
import sys
import pytest
from test_x04_assembly_source_pins import pinned_files
from test_x04_review_subprocess_chain import BOOTSTRAP
from test_x04_untrusted_derived_counts import X04_ROOT


@pytest.mark.parametrize('seed', [1, 42, 123456])
def test_doubly_pinned_runtime_modes(seed, tmp_path):
    source, manifest, expected = pinned_files(tmp_path); source.write_text(json.dumps(manifest), encoding='utf-8')
    raw = source.read_bytes(); digest = hashlib.sha256(raw).hexdigest()
    snapshots = {p: p.read_bytes() for p in tmp_path.iterdir()}
    unrelated = tmp_path / 'unrelated'; unrelated.mkdir()
    def run(arguments, optimized=False, chosen_seed=0):
        return subprocess.run([sys.executable, '-P', *(['-O'] if optimized else []), '-c', BOOTSTRAP,
            str(X04_ROOT / 'scripts/assemble_x04_evidence_packet.py'), *map(str, arguments)],
            cwd=unrelated, env={'PYTHONHASHSEED': str(chosen_seed)}, capture_output=True, timeout=30)
    args = [source, '--require-source-pins', '--expected-manifest-sha256', digest]
    baseline = run(args)
    assert baseline.returncode == 0 and baseline.stderr == b'' and json.loads(baseline.stdout) == expected
    for optimized in (False, True):
        result = run(args, optimized, seed)
        assert (result.returncode, result.stdout, result.stderr) == (0, baseline.stdout, b'')
    assert all(p.read_bytes() == data for p, data in snapshots.items())
    source.write_bytes(raw + b' '); changed = source.read_bytes()
    output = tmp_path / 'packet.json'; output.write_bytes(b'previous')
    failed = run(args + ['--out', output])
    assert failed.returncode == 2 and failed.stdout == b'' and b'expected_sha256' in failed.stderr
    for optimized in (False, True):
        result = run(args + ['--out', output], optimized, seed)
        assert (result.returncode, result.stdout, result.stderr) == (2, b'', failed.stderr)
        assert output.read_bytes() == b'previous' and source.read_bytes() == changed
    args[-1] = hashlib.sha256(changed).hexdigest()
    assert run(args + ['--out', output]).returncode == 0 and output.read_bytes() == baseline.stdout
