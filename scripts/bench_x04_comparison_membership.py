"""Observe X04 literal membership comparison without timing pass thresholds."""
from copy import deepcopy
import json
import time
import tracemalloc
from compare_x04_offline_evidence import membership_delta


def main():
    observations = []
    for size in (1000, 10000, 100000):
        for shape in ('equal', 'disjoint', 'asymmetric'):
            left_ids = list(range(size))
            right_ids = list(range(size)) if shape == 'equal' else (
                list(range(size, size * 2)) if shape == 'disjoint' else [size - 1, size])
            expected_removed = sorted(set(left_ids) - set(right_ids))
            expected_added = sorted(set(right_ids) - set(left_ids))
            for kind in ('highlight', 'dependency'):
                def value(index):
                    if kind == 'highlight': return format(index + 1, 'X')
                    return {'kind': 'font', 'name': f'../literal-{index}.shx',
                            'disposition': 'font remapped', 'remapped_to': 'sans-serif'}
                left, right = [value(i) for i in left_ids], [value(i) for i in reversed(right_ids)]
                originals = deepcopy((left, right))
                tracemalloc.start(); started = time.perf_counter()
                try:
                    delta = membership_delta(left, right)
                    seconds = time.perf_counter() - started
                    peak = tracemalloc.get_traced_memory()[1]
                finally: tracemalloc.stop()
                canonical = lambda rows: sorted(json.dumps(row, sort_keys=True) for row in rows)
                assert canonical(delta['removed']) == canonical([value(i) for i in expected_removed])
                assert canonical(delta['added']) == canonical([value(i) for i in expected_added])
                assert (left, right) == originals
                observations.append({'size': size, 'shape': shape, 'kind': kind,
                    'left': len(left), 'right': len(right), 'removed': len(expected_removed),
                    'added': len(expected_added), 'seconds': seconds, 'peak_bytes': peak})
    print(json.dumps({'schema': 'leaf.x04.comparison-membership-observations.v1',
                      'scope': 'membership helper microbenchmark; no artifact authentication',
                      'observations': observations}))


if __name__ == '__main__': main()
