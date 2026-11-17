"""Manifest paths are explicit local filenames, independent of process cwd."""
import json
import pytest
from test_x04_packet_assembly import manifest_files
import assemble_x04_evidence_packet as assembler


@pytest.mark.parametrize('name', ['-', '--artifact.json', 'with spaces.json', 'módulo-模块.json'])
@pytest.mark.parametrize('absolute', [False, True])
def test_literal_source_names_and_alias_refusal(tmp_path, monkeypatch, name, absolute):
    bundle = tmp_path / 'bundle'; bundle.mkdir()
    source, manifest, expected = manifest_files(bundle)
    renamed = bundle / name; (bundle / 'a.json').rename(renamed)
    manifest['artifacts'][0]['path'] = str(renamed) if absolute else name
    source.write_text(json.dumps(manifest), encoding='utf-8')
    snapshot = {p: p.read_bytes() for p in bundle.iterdir()}
    unrelated = tmp_path / 'unrelated'; unrelated.mkdir(); monkeypatch.chdir(unrelated)
    assert assembler.main([str(source), '--out', str(renamed)]) == 2
    assert all(p.read_bytes() == data for p, data in snapshot.items())
    assert not list(bundle.glob('.x04-*'))
    output = tmp_path / 'packet.json'
    assert assembler.main([str(source), '--out', str(output)]) == 0
    assert json.loads(output.read_bytes()) == expected
    assert all(p.read_bytes() == data for p, data in snapshot.items())
