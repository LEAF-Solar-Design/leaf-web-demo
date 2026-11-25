"""X07 seeded synthetic discovery edge shapes with bounded observational metrics."""
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


def benchmark(shape, size, seed):
    rng = random.Random(seed)
    rows = []
    for index in range(size):
        group = 0 if shape == 'coincident' else index % max(1, size // 4)
        prefix = 'Panel ' + ('shared-' * 64 if shape == 'shared-prefix' else '')
        rows.append({'name': prefix + str(group).zfill(6), 'layer': 'MODULES',
                     'x': 0 if shape == 'coincident' else index,
                     'y': 0 if shape == 'coincident' else rng.randrange(100)})
    rng.shuffle(rows)
    intake = {'inserts': rows}
    expected = Counter((row['name'], row['layer']) for row in rows)
    before = hashlib.sha256(json.dumps(intake, sort_keys=True).encode()).hexdigest()
    tracemalloc.start()
    try:
        start = time.perf_counter()
        proposal = discover_roles(intake)
        elapsed = time.perf_counter() - start
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    actual = {(row['block'], row['layer']): row['count'] for row in proposal['module']['candidates']}
    if actual != expected or sum(actual.values()) != size:
        raise RuntimeError('independent proposal count mismatch')
    if before != hashlib.sha256(json.dumps(intake, sort_keys=True).encode()).hexdigest():
        raise RuntimeError('intake changed')
    if proposal != discover_roles({'inserts': list(reversed(rows))}):
        raise RuntimeError('proposal ordering changed')
    return {'shape': shape, 'inserts': size, 'groups': len(expected), 'seed': seed,
            'elapsed_s': round(elapsed, 6), 'peak_python_bytes': peak,
            'count_matches': True, 'order_matches': True,
            'proposal_sha256': hashlib.sha256(json.dumps(proposal, sort_keys=True).encode()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--size', type=int, default=10000)
    parser.add_argument('--seed', type=int, default=707)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    if not 1 <= args.size <= 100000:
        parser.error('size must be between 1 and 100000')
    result = {'schema': 'leaf.x07.adversarial-discovery-perf.v1',
              'measurement': 'Discovery time and Python allocation peak excluding input creation',
              'rows': [benchmark(shape, 64 if args.smoke else args.size, args.seed)
                       for shape in ('coincident', 'equal-ranked', 'shared-prefix')]}
    encoded = json.dumps(result, indent=2) + '\n'
    if args.out:
        args.out.write_text(encoded, encoding='utf-8')
    print(encoded, end='')


if __name__ == '__main__':
    main()
