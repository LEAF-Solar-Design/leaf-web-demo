"""Closed X04 CLI streams still terminate with a deterministic error code."""
import io
import json
import sys
import pytest
from test_x04_untrusted_derived_counts import worked_example, export_main, validate_main


class BrokenStream:
    def __init__(self, error):
        self.error = error
        self.writes = 0

    def write(self, value):
        self.writes += 1
        raise self.error('injected closed stream')


@pytest.mark.parametrize('main', [export_main, validate_main])
@pytest.mark.parametrize('error', [BrokenPipeError, ValueError])
def test_closed_stdout_returns_error_without_success_json(main, error, tmp_path, monkeypatch):
    source = tmp_path / 'input.json'
    source.write_text(json.dumps(worked_example()), encoding='utf-8')
    preserved = tmp_path / 'existing.json'
    preserved.write_bytes(b'previous complete artifact')
    before = source.read_bytes()
    broken = BrokenStream(error)
    diagnostics = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', broken)
    monkeypatch.setattr(sys, 'stderr', diagnostics)
    assert main([str(source)]) == 2
    assert broken.writes == 1 and 'refused' in diagnostics.getvalue()
    assert preserved.read_bytes() == b'previous complete artifact'
    assert source.read_bytes() == before and not list(tmp_path.glob('.x04-*'))


@pytest.mark.parametrize('main', [export_main, validate_main])
@pytest.mark.parametrize('error', [BrokenPipeError, ValueError])
def test_closed_stderr_preserves_failed_output(main, error, tmp_path, monkeypatch):
    source = tmp_path / 'invalid.json'
    source.write_text('{invalid', encoding='utf-8')
    target = tmp_path / 'existing.json'
    target.write_bytes(b'previous complete artifact')
    output = io.StringIO()
    broken = BrokenStream(error)
    monkeypatch.setattr(sys, 'stdout', output)
    monkeypatch.setattr(sys, 'stderr', broken)
    assert main([str(source), '--out', str(target)]) == 2
    assert output.getvalue() == '' and broken.writes == 1
    assert target.read_bytes() == b'previous complete artifact'
    assert not list(tmp_path.glob('.x04-*'))


@pytest.mark.parametrize('main', [export_main, validate_main])
def test_both_streams_closed_still_returns_error(main, tmp_path, monkeypatch):
    source = tmp_path / 'input.json'
    source.write_text(json.dumps(worked_example()), encoding='utf-8')
    monkeypatch.setattr(sys, 'stdout', BrokenStream(BrokenPipeError))
    monkeypatch.setattr(sys, 'stderr', BrokenStream(BrokenPipeError))
    assert main([str(source)]) == 2
    assert json.loads(source.read_text()) == worked_example()
