"""Observe full trusted X04 artifact validation and comparison without time gates."""
from copy import deepcopy
import json
import time
import tracemalloc
from export_x04_offline_evidence import SCHEMA, export_evidence
from drawing_dependency_report import from_pairs
from compare_x04_offline_evidence import compare_evidence


def artifact(indices):
    pairs = [[0, 'STYLE'], [3, '../synthetic-font.shx']]
    return export_evidence({'schema': SCHEMA, 'synthetic': True,
        'drawing': {'id': 'synthetic-pipeline', 'dependency_pairs': pairs,
            'intake': {'inserts': [{'layer': 'Panels', 'name': 'Module', 'handle': format(i + 1, 'X')}
                                   for i in indices]}},
        'observed_dependencies': from_pairs(pairs),
        'request': {'drawing_id': 'synthetic-pipeline', 'set_ids': ['["INSERT","Panels","Module"]']}})


def main():
    observations = []
    for size in (100, 1000, 10000):
        for shape in ('equal', 'disjoint', 'asymmetric'):
            left_ids = list(range(size))
            right_ids = list(range(size)) if shape == 'equal' else (
                list(range(size, size * 2)) if shape == 'disjoint' else [size - 1, size])
            before, after = artifact(left_ids), artifact(right_ids)
            originals = deepcopy((before, after))
            tracemalloc.start(); started = time.perf_counter()
            try:
                delta = compare_evidence(before, after)
                seconds = time.perf_counter() - started
                peak = tracemalloc.get_traced_memory()[1]
            finally: tracemalloc.stop()
            expected_removed = {format(i + 1, 'X') for i in set(left_ids) - set(right_ids)}
            expected_added = {format(i + 1, 'X') for i in set(right_ids) - set(left_ids)}
            assert delta['count'] == {'before': size, 'after': len(right_ids), 'delta': len(right_ids) - size}
            assert set(delta['highlight_handles']['removed']) == expected_removed
            assert set(delta['highlight_handles']['added']) == expected_added
            for name in ('selected_ids', 'literal_dependencies'):
                assert delta[name] == {'removed': [], 'added': []}
            assert delta['original_bytes_integrity'] == 'unverified' and (before, after) == originals
            observations.append({'size': size, 'shape': shape, 'seconds': seconds, 'peak_bytes': peak,
                'before_count': size, 'after_count': len(right_ids), 'removed': len(expected_removed), 'added': len(expected_added)})
    print(json.dumps({'schema': 'leaf.x04.comparison-pipeline-observations.v1',
                      'scope': 'trusted validation plus comparison; artifact construction excluded', 'observations': observations}))


if __name__ == '__main__': main()
