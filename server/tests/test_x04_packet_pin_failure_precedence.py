"""Input ceilings precede stale packet pins, which precede invalid text/JSON."""
import hashlib
import io
import json
from types import SimpleNamespace
import pytest
from test_x04_packet_cross_binding import packet
import validate_x04_evidence_packet as checker


@pytest.mark.parametrize('stdin', [False, True])
@pytest.mark.parametrize('kind', ['invalid-utf8', 'malformed-json', 'input-ceiling'])
def test_packet_failure_precedence_zero_trusted_calls(stdin, kind, tmp_path, monkeypatch, capsys):
    value = packet(); valid = json.dumps(value).encode()
    raw = valid + b'\xff' if kind == 'invalid-utf8' else b'{broken' if kind == 'malformed-json' else valid
    source = tmp_path / 'packet.json'; source.write_bytes(raw)
    output = tmp_path / 'summary.json'; output.write_bytes(b'previous'); calls = []
    def trusted(item): calls.append(item); raise AssertionError('Invalid bytes cannot reach trusted validation')
    args = ['-' if stdin else str(source), '--out', str(output), '--expected-packet-sha256', '0' * 64]
    if kind == 'input-ceiling': args += ['--max-input-bytes', str(len(raw) - 1)]
    with monkeypatch.context() as patch:
        patch.setattr(checker, 'validate_packet', trusted)
        if stdin: patch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
        assert checker.main(args) == 2
    captured = capsys.readouterr()
    assert calls == [] and captured.out == '' and len(captured.err.splitlines()) == 1 and len(captured.err) < 900
    assert ('byte' if kind == 'input-ceiling' else 'expected_sha256') in captured.err
    assert 'Traceback' not in captured.err and source.read_bytes() == raw and output.read_bytes() == b'previous'
    assert not list(tmp_path.glob('.x04-*'))
    source.write_bytes(valid)
    if stdin: monkeypatch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(valid)))
    assert checker.main(['-' if stdin else str(source), '--out', str(output), '--expected-packet-sha256', hashlib.sha256(valid).hexdigest()]) == 0
    assert json.loads(output.read_bytes()) == checker.validate_packet(value)
