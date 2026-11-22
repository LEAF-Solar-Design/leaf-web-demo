"""Saved-report stdin short reads and sink errors refuse truthfully and recover."""
import io
import json
import sys
from types import SimpleNamespace
import pytest
from test_x04_comparison_report_contract import comparison
from test_x04_batch_report_contract import batch
from test_x04_evidence_short_reads import ShortReader
import validate_x04_review_report as validator


@pytest.mark.parametrize('factory', [comparison, batch])
@pytest.mark.parametrize('mode', ['one-byte', 'irregular', 'read-error', 'flush-error'])
def test_stream_faults_and_recovery(factory, mode, tmp_path, monkeypatch, capsys):
    report = factory()
    encoded = json.dumps(report).encode('utf-8')
    expected = validator.validate_report(report)
    target = tmp_path / 'checked.json'; target.write_bytes(b'previous')
    reader = ShortReader(encoded, [1] if mode == 'one-byte' else [1, 17, 2, 63, 5],
                         fail_after=64 if mode == 'read-error' else None)
    class FlushFailure(io.StringIO):
        def flush(self):
            raise OSError('Injected stdout flush failure')
    sink = FlushFailure()
    with monkeypatch.context() as patch:
        patch.setattr(sys, 'stdin', SimpleNamespace(buffer=reader))
        if mode == 'flush-error':
            patch.setattr(sys, 'stdout', sink)
            status = validator.main(['-'])
        else:
            status = validator.main(['-', '--out', str(target)])
    captured = capsys.readouterr()
    assert captured.out == '' and 'Traceback' not in captured.err
    if mode in ('read-error', 'flush-error'):
        assert status == 2 and 'refused' in captured.err
        assert target.read_bytes() == b'previous'
        if mode == 'flush-error':
            # Bytes reached the sink before flush failed; no durable-delivery claim.
            assert json.loads(sink.getvalue()) == expected
    else:
        assert status == 0 and captured.err == '' and reader.calls > 1
        assert json.loads(target.read_bytes()) == expected
    with monkeypatch.context() as patch:
        patch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(encoded)))
        assert validator.main(['-', '--out', str(target)]) == 0
    assert json.loads(target.read_bytes()) == expected
    assert not list(tmp_path.glob('.x04-*'))
