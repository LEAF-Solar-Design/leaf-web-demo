"""Interleaved calls to the same source keep independent pin decisions."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import io
import json
import threading
import pytest
from test_x04_assembly_source_pins import pinned_files
import assemble_x04_evidence_packet as assembler
import export_x04_offline_evidence as shared


@pytest.mark.parametrize('budgeted', [False, True])
def test_pin_decisions_are_call_local(budgeted, tmp_path, monkeypatch, capsys):
    source, manifest, expected = pinned_files(tmp_path)
    bad = deepcopy(manifest); bad['artifacts'][0]['expected_sha256'] = '0' * 64
    paths = {'good': source, 'bad': tmp_path / 'bad-manifest.json'}
    source.write_text(json.dumps(manifest), encoding='utf-8'); paths['bad'].write_text(json.dumps(bad), encoding='utf-8')
    snapshots = {p: p.read_bytes() for p in tmp_path.iterdir()}
    outputs = {name: tmp_path / (name + '.json') for name in paths}
    for path in outputs.values(): path.write_bytes(b'previous')
    raw = (tmp_path / 'a.json').read_bytes(); barrier = threading.Barrier(2)
    local = threading.local(); decisions = {}; decode = shared.decode_json
    def interleaved(encoded, pin=None):
        if pin is not None and encoded == raw:
            decisions[local.name] = pin; barrier.wait(timeout=20)
        return decode(encoded, pin)
    original_open = io.open; opened = []
    def guarded(path, *args, **kwargs):
        if str(path).endswith('.shx'): opened.append(path); raise AssertionError('Dependency opened')
        return original_open(path, *args, **kwargs)
    def run(name):
        local.name = name
        args = [str(paths[name]), '--out', str(outputs[name])]
        if budgeted: args += ['--max-total-input-bytes', '1000000']
        return assembler.main(args)
    with monkeypatch.context() as patch:
        patch.setattr(shared, 'decode_json', interleaved); patch.setattr(assembler, 'decode_json', interleaved)
        patch.setattr(io, 'open', guarded)
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = {name: pool.submit(run, name) for name in paths}
            assert {name: job.result(timeout=30) for name, job in jobs.items()} == {'good': 0, 'bad': 2}
    assert decisions == {'good': manifest['artifacts'][0]['expected_sha256'], 'bad': '0' * 64}
    assert json.loads(outputs['good'].read_bytes()) == expected and outputs['bad'].read_bytes() == b'previous'
    assert opened == [] and all(p.read_bytes() == data for p, data in snapshots.items())
    assert capsys.readouterr().out == '' and not list(tmp_path.glob('.x04-*'))
    paths['bad'].write_bytes(source.read_bytes())
    assert assembler.main([str(paths['bad']), '--out', str(outputs['bad'])]) == 0
    assert outputs['bad'].read_bytes() == outputs['good'].read_bytes()
