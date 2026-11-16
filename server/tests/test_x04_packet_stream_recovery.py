"""Packet stream short reads and unavailable or failing sinks recover honestly."""
import io
import json
import sys
from types import SimpleNamespace
import pytest
from test_x04_packet_cross_binding import packet
from test_x04_evidence_short_reads import ShortReader
import validate_x04_evidence_packet as checker


@pytest.mark.parametrize('mode', ['one-byte', 'read-error', 'no-buffer', 'flush-error'])
def test_stream_faults_and_retry(mode, tmp_path, monkeypatch, capsys):
    value = packet(); raw = json.dumps(value).encode(); expected = checker.validate_packet(value)
    output = tmp_path / 'checked.json'; output.write_bytes(b'prior')
    reader = ShortReader(raw, [1], fail_after=64 if mode == 'read-error' else None)
    class FlushFailure(io.StringIO):
        def flush(self): raise OSError('Synthetic packet stdout flush error')
    sink = FlushFailure()
    with monkeypatch.context() as patch:
        patch.setattr(sys, 'stdin', io.StringIO('') if mode == 'no-buffer' else SimpleNamespace(buffer=reader))
        if mode == 'flush-error':
            patch.setattr(sys, 'stdout', sink); status = checker.main(['-'])
        else: status = checker.main(['-', '--out', str(output)])
    captured = capsys.readouterr()
    assert captured.out == '' and 'Traceback' not in captured.err
    if mode == 'one-byte':
        assert status == 0 and json.loads(output.read_bytes()) == expected and reader.calls > 1
    else:
        assert status == 2 and 'refused' in captured.err and output.read_bytes() == b'prior'
        if mode == 'flush-error': assert json.loads(sink.getvalue()) == expected
    assert not list(tmp_path.glob('.x04-*'))
    with monkeypatch.context() as patch:
        patch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
        assert checker.main(['-', '--out', str(output)]) == 0
    assert json.loads(output.read_bytes()) == expected
