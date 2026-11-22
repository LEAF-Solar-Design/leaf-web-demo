"""Saved report aliases refuse before publication and retain original bytes."""
import json
import os
import pytest
from test_x04_comparison_report_contract import comparison
from test_x04_batch_report_contract import batch
import validate_x04_review_report as validator


@pytest.mark.parametrize('factory', [comparison, batch])
@pytest.mark.parametrize('mode', ['normalized', 'hardlink', 'output-symlink', 'input-symlink'])
def test_alias_matrix_and_retry(factory, mode, tmp_path, monkeypatch, capsys):
    source = tmp_path / 'report.json'
    source.write_text(json.dumps(factory()), encoding='utf-8')
    raw = source.read_bytes()
    alias = tmp_path / 'alias.json'
    if mode == 'normalized':
        (tmp_path / 'sub').mkdir()
        target = tmp_path / 'sub' / '..' / 'report.json'
    elif mode == 'hardlink':
        os.link(source, alias)
        target = alias
    elif mode == 'output-symlink':
        alias.symlink_to(source)
        target = alias
    else:
        alias.symlink_to(source)
        target, source = source, alias
    def forbidden(*args, **kwargs):
        raise AssertionError('Alias refusal must happen before publication')
    with monkeypatch.context() as patch:
        patch.setattr(validator, 'write_json', forbidden)
        assert validator.main([str(source), '--out', str(target)]) == 2
    captured = capsys.readouterr()
    assert captured.out == '' and 'refused' in captured.err and 'Traceback' not in captured.err
    assert source.read_bytes() == target.read_bytes() == raw
    assert not list(tmp_path.glob('.x04-*'))
    output = tmp_path / 'recovered.json'
    assert validator.main([str(source), '--out', str(output)]) == 0
    assert json.loads(output.read_bytes())['embedded_paths_opened'] is False
    assert source.read_bytes() == raw
