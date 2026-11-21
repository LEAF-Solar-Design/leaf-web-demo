"""Equivalent JSON spellings retain exact trusted entities and dependencies."""
from copy import deepcopy
import json
import pytest
from test_x04_untrusted_derived_counts import worked_example
from drawing_dependency_report import from_pairs
from export_x04_offline_evidence import export_evidence, read_json
from validate_x04_offline_evidence import validate_evidence
from compare_x04_offline_evidence import compare_evidence


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('indent', [None, 0, 4])
@pytest.mark.parametrize('escaped', [False, True])
def test_equivalent_spellings(reverse, indent, escaped, tmp_path):
    payload = worked_example()
    payload['drawing']['id'] = payload['request']['drawing_id'] = 'synthetic-\u96ea-\u00e9'
    payload['drawing']['dependency_pairs'] = [[0, 'STYLE'], [3, '../\u96ea/\u00e9.shx']]
    payload['observed_dependencies'] = from_pairs(payload['drawing']['dependency_pairs'])
    artifact = export_evidence(payload); before = deepcopy(artifact)
    def reordered(value):
        if isinstance(value, dict):
            pairs = list(value.items())
            if reverse: pairs.reverse()
            return {key: reordered(item) for key, item in pairs}
        if isinstance(value, list): return [reordered(item) for item in value]
        return value
    encoded = (' \n\t' + json.dumps(reordered(artifact), ensure_ascii=escaped, indent=indent) + '\r\n ').encode('utf-8')
    path = tmp_path / 'variant'; path.write_bytes(encoded)
    parsed = read_json(path, len(encoded))
    assert export_evidence(parsed) == artifact
    assert validate_evidence(parsed)['recomputed_result'] == artifact['result']
    assert parsed['observed_dependencies'] == artifact['observed_dependencies']
    delta = compare_evidence(artifact, parsed)
    assert delta['count'] == {'before': 7, 'after': 7, 'delta': 0}
    for key in ('selected_ids', 'highlight_handles', 'literal_dependencies'):
        assert delta[key] == {'removed': [], 'added': []}
    assert path.read_bytes() == encoded and artifact == before
