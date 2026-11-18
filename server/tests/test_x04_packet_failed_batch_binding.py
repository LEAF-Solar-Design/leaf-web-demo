"""Standalone failure reports cannot bind to a packet of validated artifacts."""
import json
import pytest
from test_x04_packet_cross_binding import packet
from batch_validate_x04_evidence import validate_paths
from validate_x04_review_report import validate_report
import validate_x04_evidence_packet as checker


@pytest.mark.parametrize('all_failed', [False, True])
def test_genuine_failed_batch_refuses_packet_and_recovers(all_failed, tmp_path, capsys):
    value = packet(); valid = tmp_path / 'artifact.json'
    valid.write_text(json.dumps(value['artifacts'][0]['artifact']), encoding='utf-8')
    missing = tmp_path / 'missing.json'
    report = validate_paths([missing, missing] if all_failed else [valid, missing])
    assert validate_report(report)['valid'] and report['failed'] == (2 if all_failed else 1)
    value['batches'][0].update(artifact_ids=['a', 'a'], report=report)
    source, output = tmp_path / 'packet.json', tmp_path / 'checked.json'
    source.write_text(json.dumps(value), encoding='utf-8'); before = source.read_bytes()
    output.write_bytes(b'prior')
    assert checker.main([str(source), '--out', str(output)]) == 2
    captured = capsys.readouterr()
    assert captured.out == '' and 'refused' in captured.err and 'Traceback' not in captured.err
    assert output.read_bytes() == b'prior' and source.read_bytes() == before
    assert not list(tmp_path.glob('.x04-*'))
    value['batches'][0]['report'] = validate_paths([valid, valid])
    source.write_text(json.dumps(value), encoding='utf-8')
    assert checker.main([str(source), '--out', str(output)]) == 0
    assert json.loads(output.read_bytes())['batches'][0]['report']['passed'] == 2
