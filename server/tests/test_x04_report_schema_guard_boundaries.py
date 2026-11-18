"""Optional report routing guard preserves parser, byte and semantic boundaries."""
from copy import deepcopy
import hashlib
import io
import json
from types import SimpleNamespace
import pytest
from test_x04_comparison_report_contract import comparison
from test_x04_batch_report_contract import batch
import validate_x04_review_report as checker


@pytest.mark.parametrize('extra', [
    ['--expected-report-schema', 'unknown'], ['--expected-report-schema', 'COMPARISON'],
    ['--expected-report-schema', ''], ['--expected-report-schema'],
    ['--expected-report-s', 'batch'], ['--expected-report-schem', 'comparison'],
])
def test_schema_usage_refuses_before_io(extra, tmp_path, monkeypatch, capsys):
    calls = []
    def forbidden(*args, **kwargs): calls.append(args); raise AssertionError('Usage must precede I/O')
    for name in ('check_output_path', 'read_json', 'validate_report', 'write_json'):
        monkeypatch.setattr(checker, name, forbidden)
    output = tmp_path / 'never.json'
    with pytest.raises(SystemExit) as caught:
        checker.main(['-', '--out', str(output), *extra])
    assert caught.value.code == 2 and calls == [] and not output.exists()
    captured = capsys.readouterr()
    assert 'usage:' in captured.err and 'Traceback' not in captured.err and captured.out == ''


@pytest.mark.parametrize('factory,kind', [(comparison, 'comparison'), (batch, 'batch')])
@pytest.mark.parametrize('stdin', [False, True])
def test_schema_guard_wrong_kind_default_semantics_and_retry(factory, kind, stdin, tmp_path, monkeypatch, capsys):
    value = factory(); raw = json.dumps(value).encode(); source = tmp_path / 'report.json'; source.write_bytes(raw)
    output = tmp_path / 'summary.json'; expected = checker.validate_report(value)
    def run(data, selected=None):
        if stdin: monkeypatch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(data)))
        else: source.write_bytes(data)
        args = ['-' if stdin else str(source), '--out', str(output), '--expected-report-sha256', hashlib.sha256(data).hexdigest()]
        if selected is not None: args += ['--expected-report-schema', selected]
        return checker.main(args)
    assert run(raw) == 0 and json.loads(output.read_bytes()) == expected
    previous = output.read_bytes(); wrong = 'batch' if kind == 'comparison' else 'comparison'
    with monkeypatch.context() as patch:
        def forbidden(*args): raise AssertionError('Wrong kind must not run report semantics')
        patch.setattr(checker, 'validate_report', forbidden)
        assert run(raw, wrong) == 2
    assert output.read_bytes() == previous and source.read_bytes() == raw
    assert run(raw, kind) == 0 and output.read_bytes() == previous
    forged = deepcopy(value)
    if kind == 'comparison': forged['count']['delta'] += 1
    else: forged['total'] += 1
    assert run(json.dumps(forged).encode(), kind) == 2 and output.read_bytes() == previous
    assert run(raw, kind) == 0 and output.read_bytes() == previous
    assert expected['original_bytes_integrity'] == 'unverified'
    assert expected['underlying_artifacts_revalidated'] is False
    assert not list(tmp_path.glob('.x04-*')) and capsys.readouterr().out == ''
