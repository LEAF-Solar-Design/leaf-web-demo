"""Control-bearing invalid schemas cannot inject saved-report success diagnostics."""
import json
import pytest
from test_x04_comparison_report_contract import comparison
import validate_x04_review_report as validator


@pytest.mark.parametrize('control', ['\r', '\n', '\x1b[32m', '\u202e', '\u2066'])
def test_invalid_schema_control_diagnostics(control, tmp_path, capsys):
    source, output = tmp_path / 'report.json', tmp_path / 'checked.json'
    report = comparison()
    report['schema'] = control + 'SUCCESS valid=true' + control + 'x' * 1000
    source.write_text(json.dumps(report), encoding='utf-8')
    raw = source.read_bytes(); output.write_bytes(b'previous')
    assert validator.main([str(source), '--out', str(output)]) == 2
    captured = capsys.readouterr()
    assert captured.out == '' and len(captured.err) <= 250
    assert captured.err.count('\n') == 1 and all(ord(c) < 128 for c in captured.err)
    assert all(c not in captured.err for c in ('\r', '\x1b', '\u202e', '\u2066'))
    assert 'SUCCESS' not in captured.err and 'Traceback' not in captured.err
    assert output.read_bytes() == b'previous' and source.read_bytes() == raw
    assert not list(tmp_path.glob('.x04-*'))
    source.write_text(json.dumps(comparison()), encoding='utf-8')
    assert validator.main([str(source), '--out', str(output)]) == 0
    assert json.loads(output.read_bytes())['original_bytes_integrity'] == 'unverified'
