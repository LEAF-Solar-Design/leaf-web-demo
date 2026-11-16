"""Invalid manifests and source errors cannot inject active diagnostic lines."""
import json
import pytest
from test_x04_packet_assembly import manifest_files
import assemble_x04_evidence_packet as assembler


@pytest.mark.parametrize('control', ['\r', '\n', '\x1b[31m', '\u202e'])
@pytest.mark.parametrize('kind', ['schema', 'path', 'source-error'])
def test_inert_bounded_diagnostics_and_recovery(control, kind, tmp_path, monkeypatch, capsys):
    source, manifest, expected = manifest_files(tmp_path)
    original = source.read_bytes()
    malicious = control + 'FORGED_SUCCESS true ' + control + 'x' * 1000
    if kind == 'schema': manifest['schema'] = malicious
    elif kind == 'path': manifest['artifacts'][0]['path'] = malicious
    source.write_text(json.dumps(manifest), encoding='utf-8')
    snapshots = {p: p.read_bytes() for p in tmp_path.iterdir()}
    output = tmp_path / 'packet.json'; output.write_bytes(b'previous')
    read = assembler.read_json
    def injected(path, limit):
        if path == tmp_path / 'a.json': raise OSError(malicious)
        return read(path, limit)
    with monkeypatch.context() as patch:
        if kind == 'source-error': patch.setattr(assembler, 'read_json', injected)
        assert assembler.main([str(source), '--out', str(output)]) == 2
    captured = capsys.readouterr()
    assert captured.out == '' and captured.err.startswith('X04 packet assembly refused: ')
    assert len(captured.err.splitlines()) == 1 and len(captured.err) < 900
    assert all(ord(char) < 128 for char in captured.err)
    assert '\r' not in captured.err and '\x1b' not in captured.err and '\u202e' not in captured.err
    assert 'Traceback' not in captured.err
    assert output.read_bytes() == b'previous' and not list(tmp_path.glob('.x04-*'))
    assert all(p.read_bytes() == data for p, data in snapshots.items())
    source.write_bytes(original)
    assert assembler.main([str(source), '--out', str(output)]) == 0
    assert json.loads(output.read_bytes()) == expected
