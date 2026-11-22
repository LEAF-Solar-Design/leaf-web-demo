"""Destination types preserve unrelated bytes through refusal or link replacement."""
import json
import os
import pytest
from test_x04_untrusted_derived_counts import worked_example
from compare_x04_offline_evidence import main as compare_main
from batch_validate_x04_evidence import main as batch_main


@pytest.mark.parametrize('kind', ['directory', 'missing-parent', 'symlink'])
@pytest.mark.parametrize('command', ['comparison', 'batch'])
def test_destination_types_and_retry(kind, command, tmp_path, capsys):
    source = tmp_path / 'source'; source.write_text(json.dumps(worked_example()), encoding='utf-8')
    before = source.read_bytes(); sentinel = tmp_path / 'sentinel'; sentinel.write_bytes(b'unrelated')
    output = tmp_path / 'destination'
    if kind == 'directory': output.mkdir()
    elif kind == 'missing-parent': output = output / 'missing.json'
    else: os.symlink(sentinel, output)
    main = compare_main if command == 'comparison' else batch_main
    operands = [str(source)] * (2 if command == 'comparison' else 1)
    assert main([*operands, '--out', str(output)]) == (0 if kind == 'symlink' else 2)
    streams = capsys.readouterr(); assert streams.out == '' and 'Traceback' not in streams.err
    if kind == 'symlink':
        assert streams.err == '' and not output.is_symlink() and json.loads(output.read_bytes())
    else:
        assert 'refused' in streams.err
        assert output.is_dir() if kind == 'directory' else not output.parent.exists()
    assert sentinel.read_bytes() == b'unrelated' and source.read_bytes() == before
    assert not list(tmp_path.glob('.x04-*'))
    target = tmp_path / 'retry.json'
    assert main([*operands, '--out', str(target)]) == 0
    assert json.loads(target.read_bytes())['original_bytes_integrity'] == 'unverified'
