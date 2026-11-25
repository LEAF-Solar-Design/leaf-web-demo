"""Observe X04 synthetic export and validation cost with exact assertions."""
from copy import deepcopy
import json
import time
import tracemalloc
from export_x04_offline_evidence import SCHEMA, export_evidence
from drawing_dependency_report import from_pairs
from validate_x04_offline_evidence import validate_evidence


def main():
    observations = []
    for size in (100, 1000, 10000):
        pairs = [[0, 'STYLE'], [3, 'literal-東.shx']]
        source = {'schema': SCHEMA, 'synthetic': True,
                  'drawing': {'id': f'synthetic-{size}', 'dependency_pairs': pairs,
                              'intake': {'inserts': [{'layer': 'Panels', 'name': 'Module',
                                                     'handle': format(index + 1, 'X'),
                                                     'rows': 2, 'columns': 3} for index in range(size)]}},
                  'request': {'drawing_id': f'synthetic-{size}',
                              'set_ids': [json.dumps(['INSERT', 'Panels', 'Module'], separators=(',', ':'))]},
                  'observed_dependencies': from_pairs(pairs)}
        before = deepcopy(source)
        stages = {}
        artifact = None
        for stage in ('export', 'validate'):
            tracemalloc.start()
            started = time.perf_counter()
            result = export_evidence(source) if stage == 'export' else validate_evidence(artifact)
            elapsed = time.perf_counter() - started
            peak = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
            stages[stage] = {'seconds': elapsed, 'peak_bytes': peak}
            if stage == 'export':
                artifact = result
            else:
                assert result['valid'] and result['recomputed_result'] == artifact['result']
        assert artifact['result']['count'] == size * 6
        assert set(artifact['result']['highlight_handles']) == {format(index + 1, 'X') for index in range(size)}
        assert artifact['observed_dependencies'] == source['observed_dependencies']
        assert source == before
        observations.append({'parents': size, 'count': size * 6, 'highlight_count': size, **stages})
    print(json.dumps({'synthetic': True, 'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
