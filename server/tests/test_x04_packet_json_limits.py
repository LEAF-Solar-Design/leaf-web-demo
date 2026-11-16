"""Embedded packet CLI enforces strict JSON and exact file/stdin byte limits."""
from copy import deepcopy
import io
import json
import sys
from types import SimpleNamespace
import pytest
from test_x04_packet_cross_binding import packet
import validate_x04_evidence_packet as checker


@pytest.mark.parametrize('transport', ['file', 'stdin'])
@pytest.mark.parametrize('mode', ['duplicate', 'trailing', 'nesting', 'input-under', 'output-under', 'exact'])
def test_packet_json_boundaries_and_recovery(transport, mode, tmp_path, monkeypatch, capsys):
    value = packet(); expected = checker.validate_packet(value)
    valid = json.dumps(value).encode('utf-8')
    encoded = (json.dumps(expected, ensure_ascii=True, indent=2) + '\n').encode()
    source, output = tmp_path / 'packet.json', tmp_path / 'checked.json'
    def run(raw, extra=()):
        source.write_bytes(raw)
        with monkeypatch.context() as patch:
            if transport == 'stdin':
                patch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
            return checker.main(['-' if transport == 'stdin' else str(source), '--out', str(output), *extra])
    output.write_bytes(b'prior')
    raw, extra = valid, []
    if mode == 'duplicate':
        raw = b'{"schema":"a","schema":"b"}'
    elif mode == 'trailing':
        raw += b' {}'
    elif mode == 'nesting':
        nested = deepcopy(value); extension = 0
        for _ in range(checker.MAX_JSON_CONTAINER_DEPTH - 1):
            extension = [extension]
        nested['opaque'] = extension
        assert run(json.dumps(nested).encode()) == 0
        output.write_bytes(b'prior')
        nested['opaque'] = [extension]
        raw = json.dumps(nested).encode()
    elif mode == 'input-under':
        extra = ['--max-input-bytes', str(len(raw) - 1)]
    elif mode == 'output-under':
        extra = ['--max-output-bytes', str(len(encoded) - 1)]
    else:
        extra = ['--max-input-bytes', str(len(raw)), '--max-output-bytes', str(len(encoded))]
    status = run(raw, extra)
    captured = capsys.readouterr()
    assert captured.out == '' and 'Traceback' not in captured.err
    if mode == 'exact':
        assert status == 0 and output.read_bytes() == encoded
    else:
        assert status == 2 and 'refused' in captured.err and output.read_bytes() == b'prior'
    assert source.read_bytes() == raw and not list(tmp_path.glob('.x04-*'))
    assert run(valid) == 0 and json.loads(output.read_bytes()) == expected
