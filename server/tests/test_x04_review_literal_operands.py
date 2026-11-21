"""Literal operand spellings retain filenames and distinct stdin semantics."""
import io
import json
from types import SimpleNamespace
import pytest
from test_x04_review_subprocess_chain import run_tool
from test_x04_untrusted_derived_counts import worked_example
import compare_x04_offline_evidence as comparison
import batch_validate_x04_evidence as batch


@pytest.mark.parametrize('literal', ['./-', '-leading.json', 'two words.json', '\u96ea.json'])
@pytest.mark.parametrize('command', ['comparison', 'batch'])
def test_literal_filename_from_subprocess(literal, command, tmp_path):
    source = tmp_path / literal; encoded = json.dumps(worked_example()).encode('utf-8'); source.write_bytes(encoded)
    output = tmp_path / 'output'
    script = 'compare_x04_offline_evidence.py' if command == 'comparison' else 'batch_validate_x04_evidence.py'
    result = run_tool(script, ['--out', output, '--', literal, literal], tmp_path)
    assert result.returncode == 0 and result.stdout == result.stderr == b''
    report = json.loads(output.read_bytes())
    if command == 'comparison': assert report['count']['delta'] == 0
    else:
        assert [row['path'] for row in report['results']] == [literal, literal]
        assert [row['index'] for row in report['results']] == [0, 1] and report['passed'] == 2
    assert source.read_bytes() == encoded and report['original_bytes_integrity'] == 'unverified'


def test_dash_stdin_and_literal_dash_file_are_distinct(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    valid = json.dumps(worked_example()).encode('utf-8')
    (tmp_path / 'valid').write_bytes(valid); (tmp_path / '-').write_bytes(b'{broken')
    stream = io.BytesIO(valid); monkeypatch.setattr(comparison.sys, 'stdin', SimpleNamespace(buffer=stream))
    assert comparison.main(['-', 'valid']) == 0
    assert json.loads(capsys.readouterr().out)['count']['delta'] == 0 and stream.tell() == len(valid)
    stream = io.BytesIO(valid); monkeypatch.setattr(comparison.sys, 'stdin', SimpleNamespace(buffer=stream))
    assert comparison.main(['./-', 'valid']) == 2 and stream.tell() == 0
    assert capsys.readouterr().out == ''
    assert batch.main(['-']) == 2 and stream.tell() == 0
    assert capsys.readouterr().out == ''
    assert batch.main(['./-']) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['results'][0]['path'] == './-' and report['failed'] == 1
    assert (tmp_path / '-').read_bytes() == b'{broken'
