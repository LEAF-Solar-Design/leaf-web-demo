"""Embedded packet replay survives deletion of sources and refuses substitution."""
import json
from test_x04_untrusted_derived_counts import worked_example
from test_x04_review_subprocess_chain import run_tool
from validate_x04_evidence_packet import SCHEMA


def test_packet_replays_from_unrelated_network_denied_cwd(tmp_path):
    unrelated = tmp_path / 'unrelated'; unrelated.mkdir()
    source = tmp_path / 'source.json'
    a, b = tmp_path / 'a.json', tmp_path / 'b.json'
    first = worked_example()
    source.write_text(json.dumps(first), encoding='utf-8')
    assert run_tool('export_x04_offline_evidence.py', [source, '--out', a], unrelated).returncode == 0
    first['request']['set_ids'] = ['["INSERT","East","Panel"]']
    source.write_text(json.dumps(first), encoding='utf-8')
    assert run_tool('export_x04_offline_evidence.py', [source, '--out', b], unrelated).returncode == 0
    compared = run_tool('compare_x04_offline_evidence.py', [a, b], unrelated)
    batched = run_tool('batch_validate_x04_evidence.py', [a, b, a], unrelated)
    assert compared.returncode == batched.returncode == 0
    payload = {'schema': SCHEMA, 'artifacts': [
        {'id': 'a', 'artifact': json.loads(a.read_bytes())},
        {'id': 'b', 'artifact': json.loads(b.read_bytes())}],
        'comparisons': [{'id': 'c', 'before': 'a', 'after': 'b', 'report': json.loads(compared.stdout)}],
        'batches': [{'id': 'batch', 'artifact_ids': ['a', 'b', 'a'], 'report': json.loads(batched.stdout)}]}
    source.unlink(); a.unlink(); b.unlink()
    packet_path, target = tmp_path / 'packet.json', tmp_path / 'checked.json'
    packet_path.write_text(json.dumps(payload), encoding='utf-8')
    valid = packet_path.read_bytes()
    result = run_tool('validate_x04_evidence_packet.py', [packet_path, '--out', target], unrelated)
    assert result.returncode == 0 and result.stdout == result.stderr == b''
    checked = json.loads(target.read_bytes())
    assert checked['comparisons'][0]['report']['count'] == {'before': 7, 'after': 6, 'delta': -1}
    assert [row['recomputed_result']['count'] for row in checked['batches'][0]['report']['results']] == [7, 6, 7]
    previous = target.read_bytes()
    payload['artifacts'][0]['artifact'] = payload['artifacts'][1]['artifact']
    packet_path.write_text(json.dumps(payload), encoding='utf-8')
    refused = run_tool('validate_x04_evidence_packet.py', [packet_path, '--out', target], unrelated)
    assert refused.returncode == 2 and refused.stdout == b'' and b'contradict' in refused.stderr
    assert target.read_bytes() == previous and not list(tmp_path.glob('.x04-*'))
    packet_path.write_bytes(valid)
    assert run_tool('validate_x04_evidence_packet.py', [packet_path, '--out', target], unrelated).returncode == 0
