"""Batch refusal is per explicit path and cannot follow dependency targets."""
import builtins
import io
import json
import os
import pytest
from test_x04_untrusted_derived_counts import worked_example
from batch_validate_x04_evidence import main, validate_paths


@pytest.mark.parametrize('failure', ['malformed', 'count', 'missing', 'identity'])
def test_valid_invalid_valid_order_and_retry(failure, tmp_path, capsys, monkeypatch):
    paths = [tmp_path / name for name in ('first', 'broken', 'last')]
    for path in paths: path.write_text(json.dumps(worked_example()), encoding='utf-8')
    if failure == 'malformed': paths[1].write_text('{', encoding='utf-8')
    if failure == 'missing': paths[1].unlink()
    if failure in ('count', 'identity'):
        payload = worked_example()
        if failure == 'count': payload['result']['count'] = 999
        else: payload['request']['drawing_id'] = 'wrong'
        paths[1].write_text(json.dumps(payload), encoding='utf-8')
    originals = [path.read_bytes() if path.exists() else None for path in paths]
    opened = []
    def guarded(opener):
        def open_file(path, *args, **kwargs):
            if str(path).endswith('notes.shx'):
                opened.append(str(path)); raise AssertionError('Dependency target opened')
            return opener(path, *args, **kwargs)
        return open_file
    for module in (builtins, io, os): monkeypatch.setattr(module, 'open', guarded(module.open))
    assert main(list(map(str, paths))) == 1
    streams = capsys.readouterr(); report = json.loads(streams.out)
    assert streams.err == '' and (report['total'], report['passed'], report['failed']) == (3, 2, 1)
    assert [row['path'] for row in report['results']] == list(map(str, paths))
    assert [row['index'] for row in report['results']] == [0, 1, 2]
    assert [row['valid'] for row in report['results']] == [True, False, True]
    for row in (report['results'][0], report['results'][2]):
        assert row['recomputed_result'] == {'count': 7, 'highlight_handles': ['A', 'B'], 'original_bytes_integrity': 'unverified'}
    assert not opened and [path.read_bytes() if path.exists() else None for path in paths] == originals
    paths[1].write_text(json.dumps(worked_example()), encoding='utf-8')
    assert main(list(map(str, paths))) == 0
    assert json.loads(capsys.readouterr().out)['passed'] == 3


@pytest.mark.parametrize('mode', ['alias', 'budget', 'stdin'])
def test_batch_publication_refusal(mode, tmp_path, capsys):
    source = tmp_path / 'input'; source.write_text(json.dumps(worked_example()), encoding='utf-8')
    before = source.read_bytes()
    target = source if mode == 'alias' else tmp_path / 'output'
    if mode != 'alias': target.write_bytes(b'previous')
    expected = target.read_bytes()
    arguments = ['-' if mode == 'stdin' else str(source), '--out', str(target)]
    if mode == 'budget': arguments += ['--max-output-bytes', '1']
    assert main(arguments) == 2
    streams = capsys.readouterr()
    assert streams.out == '' and 'refused' in streams.err and 'Traceback' not in streams.err
    assert target.read_bytes() == expected and source.read_bytes() == before


def test_duplicates_retain_explicit_order_and_empty_list_refuses(tmp_path):
    source = tmp_path / 'input'; source.write_text(json.dumps(worked_example()), encoding='utf-8')
    report = validate_paths([source, source])
    assert report['passed'] == report['total'] == 2 and report['failed'] == 0
    assert [row['index'] for row in report['results']] == [0, 1]
    with pytest.raises(ValueError, match='explicit'): validate_paths([])
