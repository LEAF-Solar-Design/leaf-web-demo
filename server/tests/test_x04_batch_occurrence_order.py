"""Every supplied batch occurrence gets one ordered result through refusals."""
import builtins
import io
import json
import os
import pytest
from test_x04_untrusted_derived_counts import worked_example
from batch_validate_x04_evidence import main


@pytest.mark.parametrize('rotation', range(4))
def test_repeated_occurrences_failures_and_recovery(rotation, tmp_path, monkeypatch, capsys):
    good, missing, directory, malformed = [tmp_path / name for name in ('good', 'missing', 'directory', 'malformed')]
    good.write_text(json.dumps(worked_example()), encoding='utf-8')
    directory.mkdir()
    malformed.write_bytes(b'{broken')
    before = {good: good.read_bytes(), malformed: malformed.read_bytes()}
    paths = [good, missing, good, directory, malformed, good, missing, good]
    paths = paths[rotation:] + paths[:rotation] + [good]
    opened = []
    def guard(opener):
        def open_file(path, *args, **kwargs):
            if str(path).endswith('notes.shx'):
                opened.append(str(path))
                raise AssertionError('Dependency target opened')
            return opener(path, *args, **kwargs)
        return open_file
    for module in (builtins, io, os):
        monkeypatch.setattr(module, 'open', guard(module.open))
    assert main(list(map(str, paths))) == 1
    streams = capsys.readouterr()
    report = json.loads(streams.out)
    assert streams.err == '' and (report['total'], report['passed'], report['failed']) == (9, 5, 4)
    assert [row['index'] for row in report['results']] == list(range(9))
    assert [row['path'] for row in report['results']] == list(map(str, paths))
    assert [row['valid'] for row in report['results']] == [path == good for path in paths]
    for row in report['results']:
        if row['valid']:
            assert row['recomputed_result'] == {'count': 7, 'highlight_handles': ['A', 'B'], 'original_bytes_integrity': 'unverified'}
        else: assert row['error']
    assert not opened and {path: path.read_bytes() for path in before} == before
    assert main([str(good)]) == 0
    assert json.loads(capsys.readouterr().out)['passed'] == 1
