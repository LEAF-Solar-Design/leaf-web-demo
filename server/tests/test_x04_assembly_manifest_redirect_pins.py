"""A changed manifest must pass its own pin before a retained row pin is checked."""
import hashlib
import json
from pathlib import Path
import pytest
from test_x04_assembly_source_pins import pinned_files
from test_x04_comparison_delta_algebra import artifact
import assemble_x04_evidence_packet as assembler


@pytest.mark.parametrize('budgeted', [False, True])
def test_redirect_requires_manifest_and_source_pin_refresh(budgeted, tmp_path, monkeypatch, capsys):
    source, manifest, expected = pinned_files(tmp_path); source.write_text(json.dumps(manifest), encoding='utf-8')
    original = source.read_bytes(); digest = hashlib.sha256(original).hexdigest()
    alternate = tmp_path / 'alternate.json'; alternate.write_text(json.dumps(artifact(2)), encoding='utf-8')
    alternate_bytes = alternate.read_bytes(); output = tmp_path / 'packet.json'; output.write_bytes(b'previous')
    manifest['artifacts'][0]['path'] = alternate.name
    source.write_text(json.dumps(manifest), encoding='utf-8'); changed = source.read_bytes()
    original_open = Path.open; opened = []
    def observed(path, mode='r', *a, **kw):
        if mode == 'rb': opened.append(path)
        return original_open(path, mode, *a, **kw)
    args = [str(source), '--out', str(output), '--require-source-pins'] + (['--max-total-input-bytes', '1000000'] if budgeted else [])
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', observed)
        assert assembler.main(args + ['--expected-manifest-sha256', digest]) == 2
        assert opened == [source]
        opened.clear()
        assert assembler.main(args + ['--expected-manifest-sha256', hashlib.sha256(changed).hexdigest()]) == 2
        assert opened == [source, alternate]
    assert output.read_bytes() == b'previous' and alternate.read_bytes() == alternate_bytes and source.read_bytes() == changed
    assert not list(tmp_path.glob('.x04-*')) and capsys.readouterr().out == ''
    source.write_bytes(original)
    assert assembler.main(args + ['--expected-manifest-sha256', digest]) == 0
    assert json.loads(output.read_bytes()) == expected
