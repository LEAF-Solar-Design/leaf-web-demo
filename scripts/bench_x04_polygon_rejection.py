"""X04 mixed synthetic polygon listing: exact eligibility, no timing gates."""
import argparse
import hashlib
import json
from pathlib import Path
from bench_x04_module_count import load_tool, measure


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, allow_nan=False, sort_keys=True).encode()).hexdigest()


def benchmark(size, tool):
    straight = [[0, 0, 0], [2, 0, 0], [2, 1, 0], [0, 1, 0]]
    twisted = [[0, 0, 0], [2, 0, 0], [2, 1, 0], [0, 1, 1]]
    rows = []
    expected = set()
    for index in range(size):
        kind = index % 4
        row = {'handle': f'H{index:X}', 'layer': 'Synthetic', 'closed': True,
               'pts': [] if kind == 3 else twisted if kind == 2 else straight}
        if kind == 1:
            row['bulges'] = [0.5, 0, 0, 0]
        if kind == 0:
            expected.add(row['handle'])
        rows.append(row)
    intake = {'polylines': rows}
    before = fingerprint(intake)
    (listing, overlay), metrics = measure(lambda: tool.run(intake, {}))
    assert overlay is None and listing['count'] is None
    assert len(listing['sets']) == 1 and listing['sets'][0]['count'] == len(expected)
    result, selected_overlay = tool.run(intake, {'set_id': listing['sets'][0]['set_id']})
    assert result['count'] == len(expected)
    assert set(selected_overlay['highlight_handles']) == expected
    assert len(selected_overlay['highlight_handles']) == len(expected)
    assert fingerprint(intake) == before
    return {'records': size, 'valid_quads': len(expected), 'rejected': size-len(expected),
            'listing': metrics, 'unchanged_inputs': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    tool = load_tool()
    report = {'schema': 'leaf.x04.polygon-rejection.v1', 'synthetic': True, 'timing_gate': False,
              'memory_scope': 'Listing allocations only. Input, fingerprints and selected highlight checks outside tracing.',
              'rows': [benchmark(size, tool) for size in (1000, 10000, 100000)]}
    encoded = json.dumps(report, indent=2) + '\n'
    if args.out:
        args.out.write_text(encoded, encoding='utf-8')
    print(encoded, end='')


if __name__ == '__main__':
    main()
