"""Report hash/decode cancellation retains identity and stream ownership."""
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_x04_comparison_report_contract import comparison
from test_x04_batch_report_contract import batch
import validate_x04_review_report as checker
import export_x04_offline_evidence as shared


@pytest.mark.parametrize('factory', [comparison, batch])
@pytest.mark.parametrize('stdin', [False, True])
@pytest.mark.parametrize('stage', ['hash', 'decode'])
def test_report_cancellation_identity_and_fresh_retry(factory, stdin, stage, tmp_path, monkeypatch, capsys):
    value = factory(); raw = json.dumps(value).encode(); digest = hashlib.sha256(raw).hexdigest()
    source = tmp_path / 'report.json'; source.write_bytes(raw)
    output = tmp_path / 'summary.json'; output.write_bytes(b'previous')
    stream = io.BytesIO(raw); streams = []; calls = []; original_open = Path.open
    sha, loads = shared.hashlib.sha256, shared.json.loads
    cancellation = KeyboardInterrupt('synthetic report cancellation')
    def opened(path, mode='r', *a, **kw):
        result = original_open(path, mode, *a, **kw)
        if path == source and mode == 'rb': streams.append(result)
        return result
    def cancelled_hash(data=b'', *a, **kw):
        if data == raw: raise cancellation
        return sha(data, *a, **kw)
    def cancelled_decode(value, *a, **kw):
        if value == raw.decode(): raise cancellation
        return loads(value, *a, **kw)
    def trusted(value): calls.append(value); raise AssertionError('Cancelled input must not be trusted')
    args = ['-' if stdin else str(source), '--out', str(output), '--expected-report-sha256', digest]
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', opened); patch.setattr(checker, 'validate_report', trusted)
        if stdin: patch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=stream))
        if stage == 'hash': patch.setattr(shared.hashlib, 'sha256', cancelled_hash)
        else: patch.setattr(shared.json, 'loads', cancelled_decode)
        with pytest.raises(KeyboardInterrupt) as caught: checker.main(args)
        assert caught.value is cancellation
    assert calls == [] and all(item.closed for item in streams)
    assert len(streams) == (0 if stdin else 1)
    if stdin: assert not stream.closed
    assert source.read_bytes() == raw and output.read_bytes() == b'previous'
    assert not list(tmp_path.glob('.x04-*')) and capsys.readouterr().out == ''
    if stdin: monkeypatch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
    assert checker.main(args) == 0 and json.loads(output.read_bytes()) == checker.validate_report(value)
    stream.close()
