"""Optional explicit-source hashes bind the bytes actually decoded, without reopening."""
import hashlib
import io
import json
from pathlib import Path
import pytest
from test_x04_packet_assembly import manifest_files
import assemble_x04_evidence_packet as assembler


def pinned_files(tmp_path):
    source, manifest, expected = manifest_files(tmp_path)
    for section in ('artifacts', 'comparisons', 'batches'):
        for row in manifest[section]:
            row['expected_sha256'] = hashlib.sha256((tmp_path / row['path']).read_bytes()).hexdigest()
    return source, manifest, expected


@pytest.mark.parametrize('budgeted', [False, True])
def test_all_source_pins_and_unpinned_compatibility(budgeted, tmp_path):
    source, manifest, expected = pinned_files(tmp_path)
    manifest['comparisons'][0]['expected_sha256'] = manifest['comparisons'][0]['expected_sha256'].upper()
    source.write_text(json.dumps(manifest), encoding='utf-8')
    output = tmp_path / 'packet.json'
    args = [str(source), '--out', str(output)] + (['--max-total-input-bytes', '1000000'] if budgeted else [])
    assert assembler.main(args) == 0 and json.loads(output.read_bytes()) == expected
    for section in ('artifacts', 'comparisons', 'batches'):
        for row in manifest[section]: del row['expected_sha256']
    source.write_text(json.dumps(manifest), encoding='utf-8')
    assert assembler.main(args) == 0 and json.loads(output.read_bytes()) == expected


@pytest.mark.parametrize('section', ['artifacts', 'comparisons', 'batches'])
@pytest.mark.parametrize('bad', [None, [], {}, True, 1, '', 'g' * 64, 'a' * 63, '0' * 65])
def test_malformed_pin_refuses_before_payload_reads(section, bad, tmp_path, monkeypatch):
    source, manifest, expected = pinned_files(tmp_path)
    manifest[section][-1]['expected_sha256'] = bad
    source.write_text(json.dumps(manifest), encoding='utf-8')
    output = tmp_path / 'packet.json'; output.write_bytes(b'previous')
    read = assembler.read_json; opened = []
    def guarded(path, *args):
        opened.append(path); assert path == source
        return read(path, *args)
    with monkeypatch.context() as patch:
        patch.setattr(assembler, 'read_json', guarded)
        assert assembler.main([str(source), '--out', str(output)]) == 2
    assert opened == [source] and output.read_bytes() == b'previous' and not list(tmp_path.glob('.x04-*'))
    for key in ('artifacts', 'comparisons', 'batches'):
        for row in manifest[key]: row['expected_sha256'] = hashlib.sha256((tmp_path / row['path']).read_bytes()).hexdigest()
    source.write_text(json.dumps(manifest), encoding='utf-8')
    assert assembler.main([str(source), '--out', str(output)]) == 0
    assert json.loads(output.read_bytes()) == expected


@pytest.mark.parametrize('budgeted', [False, True])
@pytest.mark.parametrize('matches', [False, True])
def test_hashes_actual_short_read_bytes_not_stat_or_reopened_file(budgeted, matches, tmp_path, monkeypatch):
    source, manifest, expected = pinned_files(tmp_path)
    target = tmp_path / 'a.json'; original = target.read_bytes(); actual = b'\xef\xbb\xbf' + original + b' '
    manifest['artifacts'][0]['expected_sha256'] = hashlib.sha256(actual if matches else original).hexdigest()
    source.write_text(json.dumps(manifest), encoding='utf-8')
    snapshots = {p: p.read_bytes() for p in tmp_path.iterdir()}
    output = tmp_path / 'packet.json'; output.write_bytes(b'previous')
    original_open = Path.open; opens = []
    class Short(io.BytesIO):
        def read(self, amount=-1): return super().read(min(amount, 7))
    def opened(path, mode='r', *args, **kwargs):
        if path == target and mode == 'rb': opens.append(path); return Short(actual)
        return original_open(path, mode, *args, **kwargs)
    args = [str(source), '--out', str(output)] + (['--max-total-input-bytes', '1000000'] if budgeted else [])
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', opened)
        assert assembler.main(args) == (0 if matches else 2)
    assert opens == [target]
    if matches: assert json.loads(output.read_bytes()) == expected
    else: assert output.read_bytes() == b'previous'
    assert all(p.read_bytes() == data for p, data in snapshots.items()) and not list(tmp_path.glob('.x04-*'))
    manifest['artifacts'][0]['expected_sha256'] = hashlib.sha256(original).hexdigest()
    source.write_text(json.dumps(manifest), encoding='utf-8')
    assert assembler.main(args) == 0 and json.loads(output.read_bytes()) == expected
