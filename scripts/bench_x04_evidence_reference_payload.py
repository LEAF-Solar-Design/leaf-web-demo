"""Observe X04 dependency-name payload cost with fixed synthetic parents."""
from copy import deepcopy
import json
import time
import tracemalloc
from export_x04_offline_evidence import SCHEMA, export_evidence
from drawing_dependency_report import from_pairs
from validate_x04_offline_evidence import validate_evidence


def main():
    observations = []
    for length in (32, 1024, 65536):
        names = [str(index) + '東' * length + '.shx' for index in range(4)]
        pairs = [pair for name in names for pair in [[0, 'STYLE'], [3, name]]]
        payload = {'schema': SCHEMA, 'synthetic': True,
                   'drawing': {'id': 'synthetic-references', 'dependency_pairs': pairs,
                               'intake': {'inserts': [{'layer': 'Panels', 'name': 'Module',
                                                      'handle': format(index + 1, 'X')}
                                                     for index in range(128)]}},
                   'observed_dependencies': from_pairs(pairs),
                   'request': {'drawing_id': 'synthetic-references',
                               'set_ids': ['["INSERT","Panels","Module"]']}}
        before = deepcopy(payload)
        stages = {}
        artifact = None
        for stage in ('export', 'validate'):
            tracemalloc.start()
            started = time.perf_counter()
            result = export_evidence(payload) if stage == 'export' else validate_evidence(artifact)
            stages[stage] = {'seconds': time.perf_counter() - started,
                             'peak_bytes': tracemalloc.get_traced_memory()[1]}
            tracemalloc.stop()
            if stage == 'export':
                artifact = result
            else:
                assert result['valid'] and result['recomputed_result'] == artifact['result']
        assert artifact['result']['count'] == 128
        assert set(artifact['result']['highlight_handles']) == {format(index + 1, 'X') for index in range(128)}
        assert [row['name'] for row in artifact['observed_dependencies']['dependencies']] == names
        assert artifact['observed_dependencies'] == payload['observed_dependencies'] and payload == before
        observations.append({'name_length': len(names[0]), 'parents': 128,
                             'json_bytes': len(json.dumps(artifact, ensure_ascii=False).encode('utf-8')),
                             **stages})
    print(json.dumps({'synthetic': True, 'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
