"""Matching packet hashes never bypass trusted embedded count/report checks."""
import hashlib
import io
import json
from types import SimpleNamespace
import pytest
from test_x04_packet_cross_binding import packet
from compare_x04_offline_evidence import compare_evidence
from validate_x04_review_report import validate_report
import validate_x04_evidence_packet as checker


@pytest.mark.parametrize('stdin', [False, True])
@pytest.mark.parametrize('kind', ['count', 'comparison', 'batch'])
def test_matching_packet_pin_still_refuses_semantic_forgery(kind, stdin, tmp_path, monkeypatch, capsys):
    valid = packet(); value = packet()
    if kind == 'count': value['artifacts'][0]['artifact']['result']['count'] = 999
    elif kind == 'comparison':
        a = value['artifacts'][0]['artifact']; value['comparisons'][0]['report'] = compare_evidence(a, a)
        assert validate_report(value['comparisons'][0]['report'])['valid']
    else:
        value['batches'][0]['artifact_ids'] = ['b', 'a', 'b']
        assert validate_report(value['batches'][0]['report'])['valid']
    raw = json.dumps(value).encode(); source = tmp_path / 'packet.json'; source.write_bytes(raw)
    output = tmp_path / 'summary.json'; output.write_bytes(b'previous')
    args = ['-' if stdin else str(source), '--out', str(output), '--expected-packet-sha256', hashlib.sha256(raw).hexdigest()]
    if stdin: monkeypatch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
    assert checker.main(args) == 2
    captured = capsys.readouterr(); assert captured.out == '' and 'expected_sha256' not in captured.err
    assert source.read_bytes() == raw and output.read_bytes() == b'previous' and not list(tmp_path.glob('.x04-*'))
    repaired = json.dumps(valid).encode(); source.write_bytes(repaired); args[-1] = hashlib.sha256(repaired).hexdigest()
    if stdin: monkeypatch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(repaired)))
    assert checker.main(args) == 0 and json.loads(output.read_bytes()) == checker.validate_packet(valid)
