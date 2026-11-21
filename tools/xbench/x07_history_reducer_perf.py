"""Bounded X07 synthetic reducer metrics with independent signature checks."""
import argparse
import hashlib
import json
from pathlib import Path
import time
import tracemalloc
from x07_history_reducer import reduce_history


def benchmark(size, mode, budget):
    history = [{'op': 'noise', 'slot': index} for index in range(size)]
    if mode == 'sparse':
        history[size // 2]['op'] = 'failure'
    else:
        history[0]['op'], history[-1]['op'] = 'open', 'commit'
    def signature(rows):
        ops = [row['op'] for row in rows]
        if mode == 'sparse':
            return 'SPARSE_FAILURE' if 'failure' in ops else None
        return 'ORDERED_FAILURE' if any(value == 'open' and 'commit' in ops[index + 1:]
                                        for index, value in enumerate(ops)) else None
    before = json.dumps(history, sort_keys=True)
    original = signature(history)
    calls = []
    def counted(rows):
        calls.append(True)
        return signature(rows)
    tracemalloc.start()
    try:
        started = time.perf_counter()
        result = reduce_history(history, counted, max_calls=budget)
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    if signature(result['commands']) != original or result['signature'] != original:
        raise RuntimeError('retained failure signature mismatch')
    if result['oracle_calls'] != len(calls) or len(calls) > budget or json.dumps(history, sort_keys=True) != before:
        raise RuntimeError('oracle budget or input preservation mismatch')
    if result['complete']:
        for index in range(len(result['commands'])):
            if signature(result['commands'][:index] + result['commands'][index + 1:]) == original:
                raise RuntimeError('completed reducer result is not locally minimal')
    return {'commands': size, 'mode': mode, 'retained_commands': len(result['commands']),
            'oracle_calls': len(calls), 'max_calls': budget, 'complete': result['complete'],
            'signature_matches': True, 'elapsed_s': round(elapsed, 6), 'peak_python_bytes': peak,
            'replay_sha256': hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--sizes', type=int, nargs='+', default=[16, 64, 256])
    parser.add_argument('--max-calls', type=int, default=128)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    sizes = [8, 16] if args.smoke else args.sizes
    if len(sizes) > 8 or any(not 2 <= size <= 256 for size in sizes):
        parser.error('at most8 history sizes between2 and256')
    result = {'schema': 'leaf.x07.history-reducer-perf.v1',
              'measurement': 'Reduction time and Python allocation peak excluding input creation and independent verification',
              'rows': [benchmark(size, mode, args.max_calls) for size in sizes for mode in ('sparse', 'interacting')]}
    encoded = json.dumps(result, indent=2) + '\n'
    if args.out:
        args.out.write_text(encoded, encoding='utf-8')
    print(encoded, end='')


if __name__ == '__main__':
    main()
