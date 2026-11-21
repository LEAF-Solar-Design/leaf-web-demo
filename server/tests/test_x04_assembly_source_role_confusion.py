"""Explicit source roles are validated, never recursively expanded as manifests."""
import json
import pytest
from test_x04_packet_assembly import manifest_files
import assemble_x04_evidence_packet as assembler


KINDS = {'artifact': 'a.json', 'comparison': 'c.json', 'batch': 'batch.json', 'manifest': 'manifest.json'}
CASES = [(role, kind) for role in ('artifact', 'comparison', 'batch') for kind in KINDS if role != kind]


@pytest.mark.parametrize('role,kind', CASES)
def test_incompatible_explicit_source_role_refuses_and_recovers(role, kind, tmp_path, monkeypatch, capsys):
    source, manifest, expected = manifest_files(tmp_path)
    original = source.read_bytes()
    section = {'artifact': 'artifacts', 'comparison': 'comparisons', 'batch': 'batches'}[role]
    manifest[section][0]['path'] = KINDS[kind]
    source.write_text(json.dumps(manifest), encoding='utf-8')
    snapshots = {p: p.read_bytes() for p in tmp_path.iterdir()}
    output = tmp_path / 'packet.json'; output.write_bytes(b'previous')
    reads = []; read = assembler.read_json
    def observed(path, limit): reads.append(path); return read(path, limit)
    with monkeypatch.context() as patch:
        patch.setattr(assembler, 'read_json', observed)
        assert assembler.main([str(source), '--out', str(output)]) == 2
    explicit = [source] + [tmp_path / row['path'] for key in ('artifacts', 'comparisons', 'batches') for row in manifest[key]]
    assert reads == explicit  # A manifest-valued source is read once, never expanded.
    assert output.read_bytes() == b'previous' and not list(tmp_path.glob('.x04-*'))
    assert all(p.read_bytes() == data for p, data in snapshots.items())
    captured = capsys.readouterr(); assert captured.out == '' and 'Traceback' not in captured.err
    source.write_bytes(original)
    assert assembler.main([str(source), '--out', str(output)]) == 0
    assert json.loads(output.read_bytes()) == expected
