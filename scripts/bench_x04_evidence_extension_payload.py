"""Observe accepted opaque extension cost with fixed X04 synthetic entities."""
from copy import deepcopy
import json
import time
import tracemalloc
from export_x04_offline_evidence import SCHEMA, export_evidence
from drawing_dependency_report import from_pairs
from validate_x04_offline_evidence import validate_evidence


def main():
    observations = []
    for size in (10, 1000, 10000):
        pairs = [[0, 'STYLE'], [3, 'literal-東.shx']]
        payload = {'schema': SCHEMA, 'synthetic': True,
                   'drawing': {'id': 'synthetic-extensions', 'dependency_pairs': pairs,
                               'future_extension': {'entries': [{'index': index, 'text': '東' * 32,
                                                                'values': [True, None, index / 2]}
                                                               for index in range(size)]},
                               'intake': {'inserts': [{'layer': 'Panels', 'name': 'Module',
                                                      'handle': format(index + 1, 'X')}
                                                     for index in range(128)]}},
                   'observed_dependencies': from_pairs(pairs),
                   'request': {'drawing_id': 'synthetic-extensions',
                               'set_ids': ['["INSERT","Panels","Module"]']}}
        before = deepcopy(payload)
        artifact = None
        stages = {}
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
        assert artifact['drawing']['future_extension'] == payload['drawing']['future_extension']
        assert artifact['observed_dependencies'] == payload['observed_dependencies'] and payload == before
        observations.append({'extension_entries': size, 'parents': 128, 'dependencies': 1,
                             'json_bytes': len(json.dumps(artifact, ensure_ascii=False).encode('utf-8')),
                             **stages})
    print(json.dumps({'synthetic': True, 'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
