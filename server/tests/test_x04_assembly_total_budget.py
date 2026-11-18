"""Actual aggregate bytes include the manifest, repeated sources and growth."""
import io
import json
from pathlib import Path
import pytest
from test_x04_packet_assembly import manifest_files
import assemble_x04_evidence_packet as assembler


def test_exact_total_repeated_reads_and_retry(tmp_path, capsys):
    source, manifest, expected = manifest_files(tmp_path)
    manifest['artifacts'].append({'id': 'again', 'path': 'a.json'})
    source.write_text(json.dumps(manifest), encoding='utf-8')
    expected['artifacts'].append({'id': 'again', 'artifact': expected['artifacts'][0]['artifact']})
    total = source.stat().st_size + sum((tmp_path / row['path']).stat().st_size
            for section in ('artifacts', 'comparisons', 'batches') for row in manifest[section])
    output = tmp_path / 'packet.json'; output.write_bytes(b'previous')
    args = [str(source), '--out', str(output), '--max-total-input-bytes']
    assert assembler.main(args + [str(total - 1)]) == 2 and output.read_bytes() == b'previous'
    assert not list(tmp_path.glob('.x04-*')) and capsys.readouterr().out == ''
    assert assembler.main(args + [str(total)]) == 0
    assert json.loads(output.read_bytes()) == expected
    previous = output.read_bytes()
    assert assembler.main(args + [str(total), '--max-input-bytes', '1']) == 2
    assert output.read_bytes() == previous
    assert assembler.main(args + [str(total)]) == 0


@pytest.mark.parametrize('role', ['manifest.json', 'a.json'])
def test_growth_is_counted_from_actual_reads_and_later_sources_stop(tmp_path, monkeypatch, role):
    source, manifest, expected = manifest_files(tmp_path)
    target = tmp_path / role
    data = target.read_bytes()
    ceiling = source.stat().st_size + (len(data) if role == 'a.json' else 0)
    output = tmp_path / 'packet.json'; output.write_bytes(b'previous')
    original = Path.open; opened = []; delivered = []
    class Growing(io.BytesIO):
        def read(self, amount=-1):
            chunk = super().read(min(amount, 11)); delivered.append(len(chunk)); return chunk
    def open_file(path, mode='r', *args, **kwargs):
        if mode == 'rb':
            opened.append(path)
            if path == target: return Growing(data + b' ' * 4096)
        return original(path, mode, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', open_file)
        assert assembler.main([str(source), '--out', str(output), '--max-total-input-bytes', str(ceiling)]) == 2
    assert sum(delivered) == len(data) + 1
    assert opened == ([source] if role == 'manifest.json' else [source, target])
    assert output.read_bytes() == b'previous' and target.read_bytes() == data
    assert assembler.main([str(source), '--out', str(output)]) == 0
    assert json.loads(output.read_bytes()) == expected


def test_exhausted_manifest_budget_does_not_open_first_source(tmp_path, monkeypatch):
    source, manifest, expected = manifest_files(tmp_path)
    original = Path.open; opened = []
    def guarded(path, mode='r', *args, **kwargs):
        if mode == 'rb':
            opened.append(path)
            assert path == source
        return original(path, mode, *args, **kwargs)
    output = tmp_path / 'packet.json'; output.write_bytes(b'previous')
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', guarded)
        assert assembler.main([str(source), '--out', str(output), '--max-total-input-bytes', str(source.stat().st_size)]) == 2
    assert opened == [source] and output.read_bytes() == b'previous'
    assert assembler.main([str(source), '--out', str(output)]) == 0
