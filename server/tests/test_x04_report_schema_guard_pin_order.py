"""Budget, byte pin, decoding and optional report-kind routing have fixed order."""
import hashlib
import io
import json
from types import SimpleNamespace
import pytest
from test_x04_comparison_report_contract import comparison
import validate_x04_review_report as checker


@pytest.mark.parametrize('stdin', [False, True])
@pytest.mark.parametrize('kind', ['budget', 'stale-utf8', 'stale-json', 'matching-json', 'wrong-schema'])
def test_schema_refusal_precedence_and_recovery(stdin, kind, tmp_path, monkeypatch, capsys):
    value = comparison(); valid = json.dumps(value).encode()
    raw = valid + b'\xff' if kind == 'stale-utf8' else b'{broken' if kind in ('stale-json', 'matching-json') else valid
    source = tmp_path / 'report.json'; source.write_bytes(raw)
    output = tmp_path / 'summary.json'; output.write_bytes(b'previous'); calls = []
    pin = hashlib.sha256(raw).hexdigest() if kind in ('matching-json', 'wrong-schema') else '0' * 64
    args = ['-' if stdin else str(source), '--out', str(output), '--expected-report-sha256', pin,
            '--expected-report-schema', 'batch']
    if kind == 'budget': args += ['--max-input-bytes', str(len(raw) - 1)]
    def forbidden(*args): calls.append(args); raise AssertionError('Refusal must precede report checks')
    with monkeypatch.context() as patch:
        patch.setattr(checker, 'validate_report', forbidden)
        if stdin: patch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
        assert checker.main(args) == 2
    captured = capsys.readouterr()
    expected = 'byte' if kind == 'budget' else 'expected_sha256' if kind.startswith('stale') else 'Expecting property' if kind == 'matching-json' else 'Expected batch saved report schema'
    assert expected in captured.err and 'Traceback' not in captured.err
    assert calls == [] and captured.out == '' and len(captured.err.splitlines()) == 1
    assert source.read_bytes() == raw and output.read_bytes() == b'previous' and not list(tmp_path.glob('.x04-*'))
    source.write_bytes(valid)
    if stdin: monkeypatch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(valid)))
    assert checker.main(['-' if stdin else str(source), '--out', str(output), '--expected-report-schema', 'comparison',
                         '--expected-report-sha256', hashlib.sha256(valid).hexdigest()]) == 0
    assert json.loads(output.read_bytes()) == checker.validate_report(value)
