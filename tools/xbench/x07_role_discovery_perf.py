"""Seeded synthetic discovery timings. No real inputs or CI registration.

Elapsed time and tracemalloc peak cover discovery only, excluding input creation.
These are observations, not a responsiveness or browser acceptance threshold.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import random
import sys
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'server'))
from solar_drawing_roles import discover_roles


def benchmark(size, seed):
    rng = random.Random(seed)
    rows = [{'name': f'Module {index % 3}', 'layer': 'SYNTHETIC MODULES',
             'x': rng.random() * 1000, 'y': rng.random() * 1000} for index in range(size)]
    expected = dict(Counter(row['name'] for row in rows))
    intake = {'inserts': rows}
    tracemalloc.start()
    try:
        started = time.perf_counter()
        result = discover_roles(intake)
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    actual = {row['block']: row['count'] for row in result['module']['candidates']}
    if actual != expected or result != discover_roles(intake):
        raise RuntimeError('synthetic discovery count or determinism failed')
    digest = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'inserts': size, 'seed': seed, 'elapsed_s': round(elapsed, 6),
            'peak_discovery_bytes': peak, 'module_counts': actual,
            'deterministic': True, 'count_matches': True, 'proposal_sha256': digest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--sizes', type=int, nargs='+', default=[1000, 10000, 100000])
    parser.add_argument('--seed', type=int, default=707)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    sizes = [30, 300] if args.smoke else args.sizes
    if any(size < 1 or size > 1000000 for size in sizes):
        parser.error('sizes must be between 1 and 1000000')
    result = {'schema': 'leaf.x07.synthetic-discovery-perf.v1',
              'measurement': 'discovery time and Python allocation peak, excluding input creation',
              'rows': [benchmark(size, args.seed) for size in sizes]}
    encoded = json.dumps(result, indent=2) + '\n'
    if args.out:
        args.out.write_text(encoded, encoding='utf-8')
    print(encoded, end='')


if __name__ == '__main__':
    main()
