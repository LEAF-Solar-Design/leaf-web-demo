"""Manifest and embedded payload depths have explicit structural boundaries."""
import json
import pytest
from test_x04_packet_assembly import manifest_files
import assemble_x04_evidence_packet as assembler


@pytest.mark.parametrize('name,maximum', [('manifest.json', 255), ('a.json', 252), ('c.json', 252), ('batch.json', 252)])
@pytest.mark.parametrize('kind', ['exact', 'one-beyond', 'parser-depth'])
def test_supported_depth_and_controlled_refusal(name, maximum, kind, tmp_path, monkeypatch, capsys):
    source, manifest, expected = manifest_files(tmp_path)
    target = tmp_path / name; original = target.read_bytes()
    depth = maximum if kind == 'exact' else maximum + 1 if kind == 'one-beyond' else 1500
    # Build deep JSON as bytes without depending on the test encoder's recursion.
    target.write_bytes(original[:-1] + b',"opaque":' + b'[' * depth + b'0' + b']' * depth + b'}')
    snapshots = {p: p.read_bytes() for p in tmp_path.iterdir()}
    output = tmp_path / 'packet.json'; output.write_bytes(b'previous')
    read = assembler.read_json; opened = []
    def observed(path, limit): opened.append(path); return read(path, limit)
    with monkeypatch.context() as patch:
        patch.setattr(assembler, 'read_json', observed)
        assert assembler.main([str(source), '--out', str(output)]) == (0 if kind == 'exact' else 2)
    if kind != 'exact': assert output.read_bytes() == b'previous'
    if name == 'manifest.json' and kind != 'exact': assert opened == [source]
    assert all(p.read_bytes() == data for p, data in snapshots.items())
    assert not list(tmp_path.glob('.x04-*'))
    captured = capsys.readouterr(); assert captured.out == '' and 'Traceback' not in captured.err
    target.write_bytes(original)
    assert assembler.main([str(source), '--out', str(output)]) == 0
    assert json.loads(output.read_bytes()) == expected
