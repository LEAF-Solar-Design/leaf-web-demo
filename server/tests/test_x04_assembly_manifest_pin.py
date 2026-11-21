"""Optional manifest pin checks exact bytes before any explicit source read."""
import hashlib
import io
import json
from pathlib import Path
import pytest
from test_x04_assembly_source_pins import pinned_files
import assemble_x04_evidence_packet as assembler


@pytest.mark.parametrize('pin', ['', 'g' * 64, 'a' * 63, 'a' * 65, ' ' + 'a' * 64, 'a' * 64 + '\n', '0x' + 'a' * 64])
def test_malformed_manifest_pin_is_usage_before_io(pin, tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs): raise AssertionError('Malformed option must precede I/O')
    monkeypatch.setattr(assembler, 'read_json', forbidden); monkeypatch.setattr(assembler.InputBudget, 'read_json', forbidden)
    monkeypatch.setattr(Path, 'resolve', forbidden)
    with pytest.raises(SystemExit) as caught:
        assembler.main(['manifest.json', '--expected-manifest-sha256', pin, '--out', str(tmp_path / 'never')])
    assert caught.value.code == 2 and not (tmp_path / 'never').exists()
    assert 'Traceback' not in capsys.readouterr().err


@pytest.mark.parametrize('budgeted', [False, True])
def test_manifest_pin_unpinned_compatibility_tamper_and_refresh(budgeted, tmp_path, monkeypatch, capsys):
    source, manifest, expected = pinned_files(tmp_path)
    source.write_bytes(b'\xef\xbb\xbf' + json.dumps(manifest).encode())
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    output = tmp_path / 'packet.json'
    args = [str(source), '--out', str(output)] + (['--max-total-input-bytes', '1000000'] if budgeted else [])
    assert assembler.main(args + ['--expected-manifest-sha256', digest.upper()]) == 0
    previous = output.read_bytes(); assert json.loads(previous) == expected
    assert assembler.main(args) == 0 and output.read_bytes() == previous
    source.write_bytes(source.read_bytes() + b' '); changed = source.read_bytes()
    original = Path.open; opened = []
    def observed(path, mode='r', *a, **kw):
        if mode == 'rb': opened.append(path)
        return original(path, mode, *a, **kw)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', observed)
        assert assembler.main(args + ['--expected-manifest-sha256', digest]) == 2
    assert opened == [source] and source.read_bytes() == changed and output.read_bytes() == previous
    assert not list(tmp_path.glob('.x04-*')) and capsys.readouterr().out == ''
    refreshed = hashlib.sha256(changed).hexdigest()
    assert assembler.main(args + ['--expected-manifest-sha256', refreshed]) == 0 and output.read_bytes() == previous


@pytest.mark.parametrize('flag,budgeted', [('--max-input-bytes', False), ('--max-input-bytes', True), ('--max-total-input-bytes', True)])
def test_manifest_byte_budget_precedes_matching_pin(flag, budgeted, tmp_path, monkeypatch, capsys):
    source, manifest, expected = pinned_files(tmp_path); source.write_text(json.dumps(manifest), encoding='utf-8')
    raw = source.read_bytes(); digest = hashlib.sha256(raw).hexdigest()
    output = tmp_path / 'packet.json'; output.write_bytes(b'previous')
    args = [str(source), '--out', str(output), '--expected-manifest-sha256', digest]
    limits = [flag, str(len(raw) - 1)]
    if budgeted and flag != '--max-total-input-bytes': limits += ['--max-total-input-bytes', '1000000']
    original = Path.open; opened = []
    def observed(path, mode='r', *a, **kw):
        if mode == 'rb': opened.append(path)
        return original(path, mode, *a, **kw)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', observed)
        assert assembler.main(args + limits) == 2
    assert opened == ([] if not budgeted else [source])
    assert output.read_bytes() == b'previous' and source.read_bytes() == raw
    assert 'byte' in capsys.readouterr().err and not list(tmp_path.glob('.x04-*'))
    assert assembler.main(args) == 0 and json.loads(output.read_bytes()) == expected


@pytest.mark.parametrize('budgeted', [False, True])
@pytest.mark.parametrize('matches', [False, True])
def test_manifest_pin_uses_actual_short_read_buffer(budgeted, matches, tmp_path, monkeypatch):
    source, manifest, expected = pinned_files(tmp_path); source.write_text(json.dumps(manifest), encoding='utf-8')
    raw = source.read_bytes(); actual = b'\xef\xbb\xbf' + raw + b' '
    pin = hashlib.sha256(actual if matches else raw).hexdigest()
    output = tmp_path / 'packet.json'; output.write_bytes(b'previous')
    original = Path.open; opens = []
    class Short(io.BytesIO):
        def read(self, amount=-1): return super().read(min(amount, 7))
    def observed(path, mode='r', *a, **kw):
        if path == source and mode == 'rb': opens.append(path); return Short(actual)
        return original(path, mode, *a, **kw)
    args = [str(source), '--out', str(output), '--expected-manifest-sha256', pin]
    if budgeted: args += ['--max-total-input-bytes', '1000000']
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', observed)
        assert assembler.main(args) == (0 if matches else 2)
    assert opens == [source] and source.read_bytes() == raw
    assert json.loads(output.read_bytes()) == expected if matches else output.read_bytes() == b'previous'
