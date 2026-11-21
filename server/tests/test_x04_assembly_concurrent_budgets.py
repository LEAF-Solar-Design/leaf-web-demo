"""Interleaved assembly calls keep independent actual byte ceilings."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import threading
from test_x04_packet_assembly import manifest_files
import assemble_x04_evidence_packet as assembler


def test_concurrent_aggregate_budgets_are_call_local(tmp_path, monkeypatch, capsys):
    source, manifest, expected = manifest_files(tmp_path)
    snapshots = {p: p.read_bytes() for p in tmp_path.iterdir()}
    total = sum(len(data) for data in snapshots.values())
    outputs = {name: tmp_path / (name + '.json') for name in ('adequate', 'small')}
    for path in outputs.values(): path.write_bytes(b'previous')
    local = threading.local(); barrier = threading.Barrier(2); instances = {}
    original = assembler.InputBudget
    class Tracked(original):
        def __init__(self, ceiling):
            super().__init__(ceiling); instances[local.name] = self
        def read_json(self, path, limit):
            if path == tmp_path / 'a.json': barrier.wait(timeout=20)
            return super().read_json(path, limit)
    opened = []; original_open = io.open
    def guarded(path, *args, **kwargs):
        if str(path).endswith('.shx'):
            opened.append(path); raise AssertionError('Dependency target opened')
        return original_open(path, *args, **kwargs)
    def run(name):
        local.name = name
        ceiling = total + 512 if name == 'adequate' else total - 1
        return assembler.main([str(source), '--out', str(outputs[name]), '--max-total-input-bytes', str(ceiling)])
    with monkeypatch.context() as patch:
        patch.setattr(assembler, 'InputBudget', Tracked); patch.setattr(io, 'open', guarded)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {name: pool.submit(run, name) for name in outputs}
            assert {name: future.result(timeout=30) for name, future in futures.items()} == {'adequate': 0, 'small': 2}
    assert instances['adequate'] is not instances['small']
    assert instances['adequate'].ceiling == total + 512 and instances['small'].ceiling == total - 1
    assert instances['adequate'].used == instances['small'].used == total
    assert json.loads(outputs['adequate'].read_bytes()) == expected and outputs['small'].read_bytes() == b'previous'
    assert opened == [] and all(p.read_bytes() == data for p, data in snapshots.items())
    assert not list(tmp_path.glob('.x04-*')) and capsys.readouterr().out == ''
    assert assembler.main([str(source), '--out', str(outputs['small']), '--max-total-input-bytes', str(total)]) == 0
    assert outputs['small'].read_bytes() == outputs['adequate'].read_bytes()
