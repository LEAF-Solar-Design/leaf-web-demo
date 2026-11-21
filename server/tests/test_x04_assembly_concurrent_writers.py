"""Concurrent assembly publication cleans only each writer's own temporary file."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import threading
import pytest
from test_x04_packet_assembly import manifest_files
from validate_x04_evidence_packet import validate_packet
import assemble_x04_evidence_packet as assembler
import export_x04_offline_evidence as writer


@pytest.mark.parametrize('failed', [None, 'one', 'two'])
def test_two_complete_packet_writers(failed, tmp_path, monkeypatch, capsys):
    manifests = {}; snapshots = {}
    for name in ('one', 'two'):
        directory = tmp_path / name; directory.mkdir()
        source, manifest, expected = manifest_files(directory)
        # Distinct packet IDs ensure the winning complete packet is identifiable.
        manifest['comparisons'][0]['id'] = name
        source.write_text(json.dumps(manifest), encoding='utf-8')
        manifests[name] = source
        snapshots.update({p: p.read_bytes() for p in directory.iterdir()})
    output = tmp_path / 'packet.json'; output.write_bytes(b'previous')
    barrier = threading.Barrier(2); cleaned = threading.Event(); first_done = threading.Event()
    local = threading.local(); paths = {}; encoded = {}; unlinks = []
    replace, unlink = writer.os.replace, Path.unlink
    def controlled_replace(source, target):
        name = local.name; paths[name] = Path(source); encoded[name] = paths[name].read_bytes()
        barrier.wait(timeout=20)
        assert len(set(paths.values())) == 2 and all(p.exists() for p in paths.values())
        barrier.wait(timeout=20)
        if name == failed: raise OSError('synthetic replacement failure')
        if failed:
            assert cleaned.wait(timeout=20)
            assert paths[name].exists() and not paths[failed].exists()
        elif name == 'two': assert first_done.wait(timeout=20)
        replace(source, target)
        if name == 'one': first_done.set()
    def controlled_unlink(path, *args, **kwargs):
        unlinks.append((local.name, path)); result = unlink(path, *args, **kwargs); cleaned.set(); return result
    def run(name):
        local.name = name
        return assembler.main([str(manifests[name]), '--out', str(output)])
    with monkeypatch.context() as patch:
        patch.setattr(writer.os, 'replace', controlled_replace); patch.setattr(Path, 'unlink', controlled_unlink)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {name: pool.submit(run, name) for name in manifests}
            codes = {name: future.result(timeout=30) for name, future in futures.items()}
    assert codes == {name: 2 if name == failed else 0 for name in manifests}
    winner = 'one' if failed == 'two' else 'two'
    assert output.read_bytes() == encoded[winner]
    packet = json.loads(output.read_bytes()); assert validate_packet(packet)['valid']
    assert packet['comparisons'][0]['id'] == winner
    assert unlinks == ([] if failed is None else [(failed, paths[failed])])
    assert all(p.read_bytes() == data for p, data in snapshots.items())
    assert not list(tmp_path.glob('.x04-*')) and capsys.readouterr().out == ''
