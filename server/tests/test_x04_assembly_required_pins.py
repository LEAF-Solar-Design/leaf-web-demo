"""Strict mode checks every explicit source row before any payload read."""
import json
from pathlib import Path
import pytest
from test_x04_assembly_source_pins import pinned_files
import assemble_x04_evidence_packet as assembler


@pytest.mark.parametrize('budgeted', [False, True])
@pytest.mark.parametrize('section,index', [('artifacts', 0), ('artifacts', 1), ('comparisons', 0), ('batches', 0)])
def test_strict_all_pinned_and_missing_occurrence(section, index, budgeted, tmp_path, monkeypatch, capsys):
    source, manifest, expected = pinned_files(tmp_path); source.write_text(json.dumps(manifest), encoding='utf-8')
    output = tmp_path / 'packet.json'
    args = [str(source), '--out', str(output)] + (['--max-total-input-bytes', '1000000'] if budgeted else [])
    assert assembler.main(args + ['--require-source-pins']) == 0
    previous = output.read_bytes(); assert json.loads(previous) == expected
    pin = manifest[section][index].pop('expected_sha256')
    source.write_text(json.dumps(manifest), encoding='utf-8')
    snapshots = {p: p.read_bytes() for p in tmp_path.iterdir() if p != output}
    opened = []; original = Path.open
    def observed(path, mode='r', *a, **kw):
        if mode == 'rb': opened.append(path)
        return original(path, mode, *a, **kw)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', observed)
        assert assembler.main(args + ['--require-source-pins']) == 2
    assert opened == [source] and output.read_bytes() == previous
    assert all(p.read_bytes() == raw for p, raw in snapshots.items()) and not list(tmp_path.glob('.x04-*'))
    assert capsys.readouterr().out == ''
    assert assembler.main(args) == 0 and output.read_bytes() == previous
    manifest[section][index]['expected_sha256'] = pin
    source.write_text(json.dumps(manifest), encoding='utf-8')
    assert assembler.main(args + ['--require-source-pins']) == 0 and output.read_bytes() == previous
