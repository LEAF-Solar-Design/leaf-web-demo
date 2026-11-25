"""Decoded report inventories agree across equivalent packet encodings."""
import hashlib
import json
import pytest
from test_x04_packet_cross_binding import packet
import validate_x04_evidence_packet as checker


@pytest.mark.parametrize('encoding', ['compact', 'reordered', 'escaped-ids', 'bom-crlf'])
def test_report_inventory_serializations(encoding, tmp_path, monkeypatch, capsys):
    value = packet()
    encodings = {
        'compact': json.dumps(value, separators=(',', ':')).encode(),
        'reordered': json.dumps(value, sort_keys=True, indent=2).encode(),
        'escaped-ids': json.dumps(value).replace('"c"', '"\\u0063"').replace('"batch"', '"\\u0062atch"').encode(),
        'bom-crlf': b'\xef\xbb\xbf' + json.dumps(value, indent=2).replace('\n', '\r\n').encode(),
    }
    assert len(set(encodings.values())) == 4
    raw = encodings[encoding]
    source = tmp_path / 'packet.json'
    source.write_bytes(raw)
    output = tmp_path / 'summary.json'

    def run(comparison, batch):
        return checker.main([str(source), '--out', str(output), '--expected-packet-sha256',
                             hashlib.sha256(raw).hexdigest(), '--expected-comparison-id', comparison,
                             '--expected-batch-id', batch])

    assert run('c', 'batch') == 0
    expected = checker.validate_packet(value)
    assert json.loads(output.read_bytes()) == expected
    previous = output.read_bytes()
    with monkeypatch.context() as patch:
        def forbidden(*args):
            raise AssertionError('Wrong decoded report inventory must precede trusted validation')
        patch.setattr(checker, 'validate_evidence', forbidden)
        assert run('wrong', 'batch') == 2
        assert output.read_bytes() == previous
        assert run('c', 'wrong') == 2
        assert output.read_bytes() == previous
    assert run('c', 'batch') == 0 and output.read_bytes() == previous
    assert source.read_bytes() == raw and not list(tmp_path.glob('.x04-*'))
    assert capsys.readouterr().out == ''
