"""X04 saved-report CLI refuses malformed bytes without replacing output."""
import json
import pytest
from test_x04_comparison_report_contract import comparison
from validate_x04_review_report import main


@pytest.mark.parametrize('raw', [
    b'{"schema":"unknown"}',
    b'{"schema":"unknown","schema":"leaf.x04.offline-comparison.v1"}',
    b'{"schema":', b'[]', b'null', b'\xff',
])
def test_diagnostic_refusal_and_valid_recovery(tmp_path, capsys, raw):
    source, output = tmp_path / 'report.json', tmp_path / 'checked.json'
    source.write_bytes(raw)
    output.write_bytes(b'prior output')
    assert main([str(source), '--out', str(output)]) == 2
    captured = capsys.readouterr()
    assert captured.out == '' and captured.err and 'Traceback' not in captured.err
    assert source.read_bytes() == raw
    assert output.read_bytes() == b'prior output'
    source.write_text(json.dumps(comparison()), encoding='utf-8')
    assert main([str(source), '--out', str(output)]) == 0
    assert json.loads(output.read_bytes())['underlying_artifacts_revalidated'] is False
    assert not list(tmp_path.glob('.x04-*'))


def test_exact_input_and_output_byte_boundaries(tmp_path, capsys):
    source, output = tmp_path / 'report.json', tmp_path / 'checked.json'
    raw = json.dumps(comparison(), ensure_ascii=True).encode()
    source.write_bytes(raw)
    assert main([str(source), '--max-input-bytes', str(len(raw))]) == 0
    encoded = capsys.readouterr().out.encode()
    output.write_bytes(b'prior')
    for args in ([ '--max-input-bytes', str(len(raw) - 1)],
                 ['--max-output-bytes', str(len(encoded) - 1)]):
        assert main([str(source), '--out', str(output), *args]) == 2
        assert capsys.readouterr().out == ''
        assert output.read_bytes() == b'prior'
    assert main([str(source), '--out', str(output), '--max-output-bytes', str(len(encoded))]) == 0
    assert output.read_bytes() == encoded
    assert source.read_bytes() == raw


def test_embedded_paths_never_open_and_alias_refuses(tmp_path, monkeypatch, capsys):
    source = tmp_path / 'report.json'
    report = comparison()
    report['literal_dependencies'] = {'removed': [], 'added': [
        {'kind': 'xref', 'name': '../../private.dwg', 'disposition': 'detached'}]}
    source.write_text(json.dumps(report), encoding='utf-8')
    raw = source.read_bytes()
    from pathlib import Path
    original = Path.open
    opened = []
    def guarded(path, *args, **kwargs):
        opened.append(path)
        assert path == source
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', guarded)
    assert main([str(source)]) == 0
    assert opened == [source]
    assert json.loads(capsys.readouterr().out)['embedded_paths_opened'] is False
    assert main([str(source), '--out', str(source)]) == 2
    assert original(source, 'rb').read() == raw
