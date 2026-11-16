"""Concurrent CLI calls keep independent expected manifest byte digests."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import threading
import pytest
from test_x04_assembly_source_pins import pinned_files
import assemble_x04_evidence_packet as assembler
import export_x04_offline_evidence as shared


@pytest.mark.parametrize('budgeted', [False, True])
def test_manifest_pin_decisions_are_call_local(budgeted, tmp_path, monkeypatch, capsys):
    source, manifest, expected = pinned_files(tmp_path); source.write_text(json.dumps(manifest), encoding='utf-8')
    raw = source.read_bytes(); digest = hashlib.sha256(raw).hexdigest()
    snapshots = {p: p.read_bytes() for p in tmp_path.iterdir()}
    outputs = {name: tmp_path / (name + '.json') for name in ('match', 'stale')}
    for path in outputs.values(): path.write_bytes(b'previous')
    barrier = threading.Barrier(2); local = threading.local(); decisions = {}; opened = {'match': [], 'stale': []}
    decode, original_open = shared.decode_json, Path.open
    explicit = {source, *(tmp_path / row['path'] for key in ('artifacts', 'comparisons', 'batches') for row in manifest[key])}
    def interleaved(encoded, pin=None):
        if encoded == raw and pin is not None:
            decisions[local.name] = pin; barrier.wait(timeout=20)
        return decode(encoded, pin)
    def observed(path, mode='r', *a, **kw):
        if mode == 'rb' and path in explicit: opened[local.name].append(path)
        return original_open(path, mode, *a, **kw)
    def run(name):
        local.name = name
        args = [str(source), '--out', str(outputs[name]), '--expected-manifest-sha256', digest if name == 'match' else '0' * 64,
                '--require-source-pins']
        if budgeted: args += ['--max-total-input-bytes', '1000000']
        return assembler.main(args)
    with monkeypatch.context() as patch:
        patch.setattr(shared, 'decode_json', interleaved); patch.setattr(assembler, 'decode_json', interleaved)
        patch.setattr(Path, 'open', observed)
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = {name: pool.submit(run, name) for name in outputs}
            assert {name: job.result(timeout=30) for name, job in jobs.items()} == {'match': 0, 'stale': 2}
    assert decisions == {'match': digest, 'stale': '0' * 64}
    assert opened['stale'] == [source] and len(opened['match']) == 5
    assert json.loads(outputs['match'].read_bytes()) == expected and outputs['stale'].read_bytes() == b'previous'
    assert all(p.read_bytes() == data for p, data in snapshots.items()) and not list(tmp_path.glob('.x04-*'))
    assert capsys.readouterr().out == ''
    assert assembler.main([str(source), '--expected-manifest-sha256', digest, '--out', str(outputs['stale'])]) == 0
    assert outputs['stale'].read_bytes() == outputs['match'].read_bytes()
