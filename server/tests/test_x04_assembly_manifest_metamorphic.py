"""Consistent local renames and row permutations preserve explicit occurrence order."""
from copy import deepcopy
import json
import pytest
from test_x04_packet_assembly import manifest_files
from validate_x04_evidence_packet import validate_packet
import assemble_x04_evidence_packet as assembler


@pytest.mark.parametrize('reverse', [False, True])
def test_manifest_permutation_rename_and_incomplete_recovery(reverse, tmp_path):
    source, manifest, expected = manifest_files(tmp_path)
    before = validate_packet(expected)
    names = {row['id']: 'new_' + row['id'] for key in ('artifacts', 'comparisons', 'batches') for row in manifest[key]}
    for key in ('artifacts', 'comparisons', 'batches'):
        for row in manifest[key]:
            row['id'] = names[row['id']]
            if key == 'comparisons': row['before'], row['after'] = names[row['before']], names[row['after']]
            if key == 'batches': row['artifact_ids'] = [names[x] for x in row['artifact_ids']]
        if reverse: manifest[key].reverse()
    source.write_text(json.dumps(manifest), encoding='utf-8')
    snapshots = {p: p.read_bytes() for p in tmp_path.iterdir()}
    output = tmp_path / 'packet.json'
    assert assembler.main([str(source), '--out', str(output)]) == 0
    checked = validate_packet(json.loads(output.read_bytes()))
    old = {names[r['id']]: r['recomputed_result'] for r in before['validated_artifacts']}
    assert {r['id']: r['recomputed_result'] for r in checked['validated_artifacts']} == old
    assert checked['comparisons'][0]['report'] == before['comparisons'][0]['report']
    assert checked['batches'][0]['artifact_ids'] == ['new_a', 'new_b', 'new_a']
    assert [r['recomputed_result']['count'] for r in checked['batches'][0]['report']['results']] == [6, 1, 6]
    assert all(p.read_bytes() == data for p, data in snapshots.items())
    previous = output.read_bytes(); broken = deepcopy(manifest)
    broken['comparisons'][0]['before'] = 'a'
    source.write_text(json.dumps(broken), encoding='utf-8'); changed = source.read_bytes()
    assert assembler.main([str(source), '--out', str(output)]) == 2
    assert source.read_bytes() == changed and output.read_bytes() == previous and not list(tmp_path.glob('.x04-*'))
    source.write_bytes(snapshots[source])
    assert assembler.main([str(source), '--out', str(output)]) == 0 and output.read_bytes() == previous
