"""X04 evidence CLI outputs cannot replace their own saved inputs."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import pytest
from test_x04_untrusted_derived_counts import X04_ROOT, worked_example, export_main, validate_main


@pytest.mark.parametrize('main', [export_main, validate_main])
@pytest.mark.parametrize('alias', ['identical', 'normalized', 'relative', 'symlink', 'hardlink'])
def test_input_output_alias_refuses_before_replacement(main, alias, tmp_path, monkeypatch, capsys):
    source = tmp_path / 'source.json'
    source.write_text(json.dumps(worked_example()), encoding='utf-8')
    original = source.read_bytes()
    if alias == 'identical':
        output = source
    elif alias == 'normalized':
        (tmp_path / 'sub').mkdir()
        output = tmp_path / 'sub' / '..' / 'source.json'
    elif alias == 'relative':
        monkeypatch.chdir(tmp_path)
        output = Path('.') / 'source.json'
    else:
        output = tmp_path / 'alias.json'
        if alias == 'symlink':
            output.symlink_to(source)
        else:
            os.link(source, output)
    def forbidden_replace(*args):
        raise AssertionError('Alias must refuse before replacement')
    monkeypatch.setattr(os, 'replace', forbidden_replace)
    assert main([str(source), '--out', str(output)]) == 2
    streams = capsys.readouterr()
    assert streams.out == '' and 'different files' in streams.err
    assert source.read_bytes() == original and output.read_bytes() == original
    assert not list(tmp_path.glob('.x04-*'))


def test_moved_export_validates_after_source_removal_from_unrelated_cwd(tmp_path):
    source = tmp_path / 'source.json'
    source.write_text(json.dumps(worked_example()), encoding='utf-8')
    exported = tmp_path / 'export.json'
    export = subprocess.run([sys.executable, '-P', str(X04_ROOT / 'scripts/export_x04_offline_evidence.py'),
                             str(source), '--out', str(exported)], capture_output=True, timeout=30)
    assert export.returncode == 0 and export.stdout == export.stderr == b''
    destination = tmp_path / 'moved'
    destination.mkdir()
    moved = destination / 'evidence.json'
    shutil.move(exported, moved)
    source.unlink()
    unrelated = tmp_path / 'unrelated'
    unrelated.mkdir()
    before = moved.read_bytes()
    result = subprocess.run([sys.executable, '-P', str(X04_ROOT / 'scripts/validate_x04_offline_evidence.py'),
                             str(moved)], cwd=unrelated, capture_output=True, timeout=30)
    assert result.returncode == 0 and result.stderr == b''
    validated = json.loads(result.stdout)
    assert validated['valid'] is True
    assert validated['recomputed_result'] == {'count': 7, 'highlight_handles': ['A', 'B'],
                                               'original_bytes_integrity': 'unverified'}
    assert moved.read_bytes() == before
