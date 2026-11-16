"""Byte ceilings precede pins, and mismatched pins precede JSON/UTF-8 decoding."""
import hashlib
import json
from pathlib import Path
import pytest
from test_x04_assembly_source_pins import pinned_files
import assemble_x04_evidence_packet as assembler


CASES = [(kind, budgeted) for kind in ('invalid-utf8', 'malformed-json', 'per-file') for budgeted in (False, True)] + [('aggregate', True)]


@pytest.mark.parametrize('kind,budgeted', CASES)
def test_failure_precedence_stops_later_inputs(kind, budgeted, tmp_path, monkeypatch, capsys):
    source, manifest, expected = pinned_files(tmp_path)
    target = tmp_path / 'a.json'; original = target.read_bytes()
    changed = original + b'\xff' if kind == 'invalid-utf8' else b'{broken' if kind == 'malformed-json' else original + b' ' * 65536
    target.write_bytes(changed)
    source.write_text(json.dumps(manifest), encoding='utf-8')
    snapshots = {p: p.read_bytes() for p in tmp_path.iterdir()}
    output = tmp_path / 'packet.json'; output.write_bytes(b'previous')
    args = [str(source), '--out', str(output)]
    if kind == 'per-file': args += ['--max-input-bytes', '65536']
    if budgeted: args += ['--max-total-input-bytes', str(len(source.read_bytes()) + len(changed) - 1 if kind == 'aggregate' else 1000000)]
    original_open = Path.open; opens = []
    def opened(path, mode='r', *a, **kw):
        if mode == 'rb': opens.append(path)
        return original_open(path, mode, *a, **kw)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', opened)
        assert assembler.main(args) == 2
    assert opens == ([source] if kind == 'per-file' and not budgeted else [source, target])
    captured = capsys.readouterr()
    assert captured.out == '' and len(captured.err.splitlines()) == 1 and len(captured.err) < 900
    assert ('byte' if kind in ('per-file', 'aggregate') else 'expected_sha256') in captured.err
    assert 'Traceback' not in captured.err
    assert all(p.read_bytes() == data for p, data in snapshots.items()) and output.read_bytes() == b'previous'
    assert not list(tmp_path.glob('.x04-*'))
    target.write_bytes(original)
    assert assembler.main([str(source), '--out', str(output)]) == 0
    assert json.loads(output.read_bytes()) == expected
