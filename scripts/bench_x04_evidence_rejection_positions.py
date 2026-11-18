"""Observe forged dependency-claim rejection position cost for X04 evidence."""
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
        pairs = [pair for index in range(size) for pair in [[0, 'STYLE'], [3, f'synthetic-{index:05d}.shx']]]
        payload = {'schema': SCHEMA, 'synthetic': True,
                   'drawing': {'id': 'synthetic-rejections', 'dependency_pairs': pairs,
                               'intake': {'inserts': [{'layer': 'Panels', 'name': 'Module',
                                                      'handle': format(index + 1, 'X')}
                                                     for index in range(16)]}},
                   'observed_dependencies': from_pairs(pairs),
                   'request': {'drawing_id': 'synthetic-rejections',
                               'set_ids': ['["INSERT","Panels","Module"]']}}
        valid = export_evidence(payload)
        for position, index in [('first', 0), ('middle', size // 2), ('last', size - 1)]:
            forged = deepcopy(valid)
            forged['observed_dependencies']['dependencies'][index]['name'] = 'forged.shx'
            before = deepcopy(forged)
            tracemalloc.start()
            started = time.perf_counter()
            try:
                validate_evidence(forged)
            except ValueError as error:
                assert 'match embedded decoded records' in str(error)
            else:
                raise AssertionError('Forged dependency claim was accepted')
            elapsed = time.perf_counter() - started
            peak = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
            assert forged == before
            recovered = validate_evidence(valid)
            assert recovered['valid'] and recovered['recomputed_result']['count'] == 16
            assert set(recovered['recomputed_result']['highlight_handles']) == {format(value + 1, 'X') for value in range(16)}
            observations.append({'dependency_rows': size, 'position': position,
                                 'seconds': elapsed, 'peak_bytes': peak})
    print(json.dumps({'synthetic': True, 'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
