"""Packet budget, byte pin, decode, inventory and semantics have fixed order."""
from copy import deepcopy
import hashlib
import io
import json
from types import SimpleNamespace
import pytest
from test_x04_packet_cross_binding import packet
import validate_x04_evidence_packet as checker


@pytest.mark.parametrize('stdin', [False, True])
@pytest.mark.parametrize('kind', ['budget', 'stale-utf8', 'stale-json', 'matching-json', 'inventory', 'forged'])
def test_inventory_refusal_order_and_corrected_retry(stdin, kind, tmp_path, monkeypatch, capsys):
    value = packet(); forged = deepcopy(value); forged['artifacts'][0]['artifact']['result']['count'] = 999
    raw = json.dumps(forged if kind == 'forged' else value).encode()
    if kind == 'stale-utf8': raw += b'\xff'
    if kind in ('stale-json', 'matching-json'): raw = b'{broken'
    source = tmp_path / 'packet.json'; source.write_bytes(raw); output = tmp_path / 'summary.json'; output.write_bytes(b'previous')
    pin = '0' * 64 if kind in ('budget', 'stale-utf8', 'stale-json') else hashlib.sha256(raw).hexdigest()
    args = ['-' if stdin else str(source), '--out', str(output), '--expected-packet-sha256', pin, '--expected-artifact-id', 'a']
    if kind == 'forged': args += ['--expected-artifact-id', 'b']
    if kind == 'budget': args += ['--max-input-bytes', str(len(raw) - 1)]
    validate = checker.validate_evidence; calls = []
    def counted(item): calls.append(item); return validate(item)
    with monkeypatch.context() as patch:
        patch.setattr(checker, 'validate_evidence', counted)
        if stdin: patch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
        assert checker.main(args) == 2
    expected = {'budget': 'byte', 'stale-utf8': 'expected_sha256', 'stale-json': 'expected_sha256',
                'matching-json': 'Expecting property', 'inventory': 'expected inventory', 'forged': 'Derived count claim'}[kind]
    captured = capsys.readouterr()
    assert expected in captured.err and captured.out == '' and 'Traceback' not in captured.err
    assert len(captured.err.splitlines()) == 1 and len(calls) == int(kind == 'forged')
    assert source.read_bytes() == raw and output.read_bytes() == b'previous' and not list(tmp_path.glob('.x04-*'))
    valid = json.dumps(value).encode(); source.write_bytes(valid)
    if stdin: monkeypatch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(valid)))
    assert checker.main(['-' if stdin else str(source), '--out', str(output), '--expected-packet-sha256', hashlib.sha256(valid).hexdigest(),
                         '--expected-artifact-id', 'a', '--expected-artifact-id', 'b']) == 0
    assert json.loads(output.read_bytes()) == checker.validate_packet(value)
