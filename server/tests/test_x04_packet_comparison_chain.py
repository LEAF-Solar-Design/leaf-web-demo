"""Signed membership differences compose across three independently valid states."""
from copy import deepcopy
import json
import pytest
from test_x04_comparison_delta_algebra import artifact
from compare_x04_offline_evidence import compare_evidence
from validate_x04_evidence_packet import SCHEMA, validate_packet


def test_composed_comparison_edges_and_stale_edge_recovery():
    values = [artifact(i) for i in range(3)]
    value = {'schema': SCHEMA, 'artifacts': [{'id': label, 'artifact': item} for label, item in zip('abc', values)],
             'comparisons': [{'id': label, 'before': before, 'after': after, 'report': compare_evidence(values[i], values[j])}
                             for label, before, after, i, j in [('ab', 'a', 'b', 0, 1), ('bc', 'b', 'c', 1, 2), ('ac', 'a', 'c', 0, 2)]],
             'batches': []}
    snapshot = deepcopy(value); result = validate_packet(value)
    reports = [row['report'] for row in result['comparisons']]
    assert [r['count']['delta'] for r in reports] == [-5, 6, 1]
    assert reports[0]['count']['delta'] + reports[1]['count']['delta'] == reports[2]['count']['delta']
    memberships = {
        'selected_ids': [v['request']['set_ids'] for v in values],
        'highlight_handles': [v['result']['highlight_handles'] for v in values],
        'literal_dependencies': [v['observed_dependencies']['dependencies'] for v in values]}
    def keys(rows): return {json.dumps(row, sort_keys=True) for row in rows}
    for field, states in memberships.items():
        sets = [keys(rows) for rows in states]
        signed = []
        for report, (i, j) in zip(reports, [(0, 1), (1, 2), (0, 2)]):
            removed, added = keys(report[field]['removed']), keys(report[field]['added'])
            assert removed == sets[i] - sets[j] and added == sets[j] - sets[i]
            signed.append({key: int(key in added) - int(key in removed) for key in set.union(*sets)})
        assert all(signed[0][key] + signed[1][key] == signed[2][key] for key in signed[2])
    value['comparisons'][2]['report'] = compare_evidence(values[0], values[0])
    bad = deepcopy(value)
    with pytest.raises(ValueError, match='contradict'): validate_packet(value)
    assert value == bad
    value['comparisons'][2] = deepcopy(snapshot['comparisons'][2])
    assert validate_packet(value) == result and value == snapshot
