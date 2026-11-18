"""Each repeated explicit path occurrence must satisfy its own expected digest."""
import hashlib
import json
from pathlib import Path
import pytest
from test_x04_assembly_source_pins import pinned_files
import assemble_x04_evidence_packet as assembler


@pytest.mark.parametrize('budgeted', [False, True])
@pytest.mark.parametrize('conflicting', [False, True])
def test_repeated_source_pins_are_not_cached(budgeted, conflicting, tmp_path, monkeypatch, capsys):
    source, manifest, expected = pinned_files(tmp_path)
    target = tmp_path / 'a.json'; raw = target.read_bytes(); digest = hashlib.sha256(raw).hexdigest()
    manifest['artifacts'][1].update(path='a.json', expected_sha256='0' * 64 if conflicting else digest)
    manifest['comparisons'] = []; manifest['batches'] = []
    source.write_text(json.dumps(manifest), encoding='utf-8')
    snapshots = {p: p.read_bytes() for p in tmp_path.iterdir()}
    output = tmp_path / 'packet.json'; output.write_bytes(b'previous')
    original = Path.open; opened = []
    def observed(path, mode='r', *args, **kwargs):
        if path == target and mode == 'rb': opened.append(path)
        return original(path, mode, *args, **kwargs)
    args = [str(source), '--out', str(output)] + (['--max-total-input-bytes', '1000000'] if budgeted else [])
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', observed)
        assert assembler.main(args) == (2 if conflicting else 0)
    assert opened == [target, target]
    if conflicting: assert output.read_bytes() == b'previous'
    else:
        packet = json.loads(output.read_bytes())
        assert [r['id'] for r in packet['artifacts']] == ['a', 'b']
        assert [r['artifact']['result']['count'] for r in packet['artifacts']] == [6, 6]
    assert all(p.read_bytes() == data for p, data in snapshots.items()) and not list(tmp_path.glob('.x04-*'))
    assert capsys.readouterr().out == ''
    manifest['artifacts'][1]['expected_sha256'] = digest
    source.write_text(json.dumps(manifest), encoding='utf-8')
    assert assembler.main(args) == 0
    assert [r['artifact']['result']['count'] for r in json.loads(output.read_bytes())['artifacts']] == [6, 6]
