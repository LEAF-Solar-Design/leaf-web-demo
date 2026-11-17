"""Packet byte, inventory, drawing-map and semantic refusals have fixed order."""
import hashlib
import io
import json
from types import SimpleNamespace
import pytest
from test_x04_packet_drawing_arguments import invoke, two_drawing_packet
import validate_x04_evidence_packet as checker


@pytest.mark.parametrize('stdin', [False, True])
@pytest.mark.parametrize('kind', ['budget', 'stale-utf8', 'stale-json', 'matching-json',
                                 'inventory', 'unknown-drawing', 'wrong-drawing', 'forged'])
def test_packet_drawing_refusal_order_and_retry(stdin, kind, tmp_path, monkeypatch, capsys):
    value = two_drawing_packet()
    if kind == 'forged':
        value['artifacts'][0]['artifact']['result']['count'] = 999
    raw = json.dumps(value).encode()
    if kind == 'stale-utf8':
        raw += b'\xff'
    if kind in ('stale-json', 'matching-json'):
        raw = b'{broken'
    source = tmp_path / 'packet.json'
    source.write_bytes(raw)
    output = tmp_path / 'summary.json'
    output.write_bytes(b'previous')
    pin = '0' * 64 if kind == 'budget' or kind.startswith('stale') else hashlib.sha256(raw).hexdigest()
    mapping = 'a=drawing=one' if kind == 'forged' else 'unknown=drawing=one' if kind == 'unknown-drawing' else 'a=wrong'
    options = ['-' if stdin else str(source), '--out', str(output), '--expected-packet-sha256', pin,
               '--expected-artifact-id', 'a', '--expected-artifact-drawing', mapping]
    if kind != 'inventory':
        options += ['--expected-artifact-id', 'b']
    if kind == 'budget':
        options += ['--max-input-bytes', str(len(raw) - 1)]
    calls = []
    trusted = checker.validate_evidence
    def observed(item):
        calls.append(item)
        return trusted(item)
    with monkeypatch.context() as patch:
        patch.setattr(checker, 'validate_evidence', observed)
        if stdin:
            patch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
        assert checker.main(options) == 2
    message = {'budget': 'byte', 'stale-utf8': 'expected_sha256', 'stale-json': 'expected_sha256',
               'matching-json': 'Expecting property', 'inventory': 'expected inventory',
               'unknown-drawing': 'must name an embedded artifact', 'wrong-drawing': 'drawing.id exactly',
               'forged': 'Derived count claim'}[kind]
    captured = capsys.readouterr()
    assert message in captured.err and captured.out == '' and 'Traceback' not in captured.err
    assert len(calls) == int(kind == 'forged')
    assert source.read_bytes() == raw and output.read_bytes() == b'previous'
    corrected = two_drawing_packet()
    code, output = invoke(corrected, stdin, ['--expected-artifact-drawing', 'a=drawing=one'], tmp_path, monkeypatch)
    assert code == 0 and json.loads(output.read_bytes()) == checker.validate_packet(corrected)
