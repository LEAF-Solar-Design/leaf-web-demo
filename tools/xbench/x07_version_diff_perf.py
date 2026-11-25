"""Bounded offline version-diff metrics with an independent role-map oracle."""
import argparse
import json
from pathlib import Path
import random
import tempfile
import time
import tracemalloc
from x07_profile_version_diff import CustomerProfileStore, compare_versions


def benchmark(count, length, iterations, seed=7):
    randomizer = random.Random(seed)
    def label():
        return ''.join(randomizer.choice('abcdefghijklm') for _ in range(length))
    before = {'module': {'block': label(), 'layer': label()}}
    if count >= 2:
        before['roof'] = {'layer': label()}
    if count >= 3:
        before['obstruction'] = {'layer': label()}
    after = {'module': {'block': 'Z' * length, 'layer': before['module']['layer']},
             'obstruction': {'layer': 'Y' * length}}
    expected = {'added': {}, 'removed': {}, 'changed': {}}
    for role in sorted(set(before) | set(after)):
        if role not in before:
            expected['added'][role] = after[role]
        elif role not in after:
            expected['removed'][role] = before[role]
        elif before[role] != after[role]:
            expected['changed'][role] = {'before': before[role], 'after': after[role]}
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'store.sqlite'
        store = CustomerProfileStore(path)
        first = store.save_version('synthetic-a', before, expected_version=0)
        pin = store.apply_to_project('old', 'synthetic-a')
        store.save_version('synthetic-a', after, expected_version=1)
        read = CustomerProfileStore(path, read_only=True)
        original = path.read_bytes()
        tracemalloc.start()
        try:
            started = time.perf_counter()
            for _ in range(iterations):
                result = compare_versions(read, customer_key='synthetic-a', profile_id=first['profile_id'], before_version=1, after_version=2)
                if {key: result[key] for key in expected} != expected:
                    raise RuntimeError('independent role map mismatch')
            elapsed = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        if read.application_record('old', 'synthetic-a') != pin or path.read_bytes() != original:
            raise RuntimeError('pin or database changed')
    return {'role_count': count, 'label_characters': length, 'iterations': iterations,
            'seed': seed, 'added': len(expected['added']), 'removed': len(expected['removed']),
            'changed': len(expected['changed']), 'oracle_matches': True, 'pin_and_bytes_unchanged': True,
            'elapsed_s': round(elapsed, 6), 'peak_python_bytes': peak}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--lengths', type=int, nargs='+', default=[8, 64, 256])
    parser.add_argument('--iterations', type=int, default=20)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    lengths = [8, 32] if args.smoke else args.lengths
    if len(lengths) > 8 or any(not 1 <= length <= 1024 for length in lengths) or not 1 <= args.iterations <= 100:
        parser.error('bounded lengths and iterations required')
    result = {'schema': 'leaf.x07.version-diff-perf.v1', 'proof_mode': 'synthetic_offline',
              'measurement': 'Comparison plus independent map checks; excludes setup and final pin/byte checks',
              'rows': [benchmark(count, length, 3 if args.smoke else args.iterations)
                       for count in ([1, 3] if args.smoke else [1, 2, 3]) for length in lengths]}
    encoded = json.dumps(result, indent=2, allow_nan=False) + '\n'
    if args.out:
        args.out.write_text(encoded, encoding='utf-8')
    print(encoded, end='')


if __name__ == '__main__':
    main()
