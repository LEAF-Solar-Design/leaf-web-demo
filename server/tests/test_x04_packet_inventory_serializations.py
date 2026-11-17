"""Equivalent packet encodings retain decoded inventory with matching byte pins."""
import hashlib
import json
import pytest
from test_x04_packet_cross_binding import packet
import validate_x04_evidence_packet as checker


@pytest.mark.parametrize('encoding', ['compact', 'reordered', 'escaped-ids', 'bom-crlf'])
def test_packet_inventory_serializations(encoding, tmp_path, monkeypatch, capsys):
    value = packet()
    encodings = {
        'compact': json.dumps(value, separators=(',', ':')).encode(),
        'reordered': json.dumps(value, sort_keys=True, indent=2).encode(),
        'escaped-ids': json.dumps(value, indent=2).replace('"a"', '"\\u0061"').replace('"b"', '"\\u0062"').encode(),
        'bom-crlf': b'\xef\xbb\xbf' + json.dumps(value, indent=2).replace('\n', '\r\n').encode()}
    assert len(set(encodings.values())) == 4
    raw = encodings[encoding]; source = tmp_path / 'packet.json'; source.write_bytes(raw)
    output = tmp_path / 'summary.json'; expected = checker.validate_packet(value)
    def run(ids):
        args = [str(source), '--out', str(output), '--expected-packet-sha256', hashlib.sha256(raw).hexdigest()]
        for identity in ids: args += ['--expected-artifact-id', identity]
        return checker.main(args)
    assert run(['b', 'a']) == 0 and json.loads(output.read_bytes()) == expected
    previous = output.read_bytes(); calls = []
    def forbidden(item): calls.append(item); raise AssertionError('Inventory mismatch precedes trusted validation')
    with monkeypatch.context() as patch:
        patch.setattr(checker, 'validate_evidence', forbidden)
        assert run(['a']) == 2
    assert calls == [] and output.read_bytes() == previous and source.read_bytes() == raw
    assert run(['a', 'b']) == 0 and output.read_bytes() == previous
    assert not list(tmp_path.glob('.x04-*')) and capsys.readouterr().out == ''
