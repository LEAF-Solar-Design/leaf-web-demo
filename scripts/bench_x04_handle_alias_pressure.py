"""Fixed weighted physical parents with increasing aliases across selected sets."""
import argparse
import hashlib
import json
from pathlib import Path
from bench_x04_module_count import load_tool, measure


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    tool = load_tool()
    expected = {f'{160 + i:X}' for i in range(16)}
    observations = []
    for copies in (1, 8, 64, 512):
        rows = []
        for alias in range(copies):
            for i in range(16):
                base = f'{160 + i:X}'
                spelling = ('0x' if alias % 2 else '') + '0' * (alias % 17) + (base.lower() if alias % 3 else base)
                for layer in ('East', 'West', 'Alternate', 'Shared'):
                    rows.append({'layer': layer, 'name': 'Panel', 'handle': f' {spelling} ', 'rows': 2, 'columns': 3})
        intake = {'inserts': rows}
        before = fingerprint(intake)
        (listing, _), listed = measure(lambda: tool.run(intake, {}))
        (result, overlay), selected = measure(lambda: tool.run(intake,
            {'set_ids': [row['set_id'] for row in listing['sets']]}))
        assert len(listing['sets']) == 4 and all(row['count'] == 96 for row in listing['sets'])
        assert result['count'] == 96 and set(overlay['highlight_handles']) == expected
        assert len(overlay['highlight_handles']) == 16 and fingerprint(intake) == before
        observations.append({'aliases_per_parent_per_set': copies, 'entity_records': len(rows),
            'physical_parents': 16, 'modules': 96, 'listing': listed, 'selection': selected})
    encoded = json.dumps({'schema': 'leaf.x04.handle-alias-pressure.v1', 'synthetic': True,
        'timing_gate': False, 'memory_scope': 'Operation allocations; fixtures and fingerprints excluded.',
        'observations': observations}, indent=2) + '\n'
    if args.out:
        args.out.write_text(encoded)
    print(encoded, end='')


if __name__ == '__main__':
    main()
