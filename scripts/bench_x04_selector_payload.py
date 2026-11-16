"""Fixed synthetic geometry with increasing repeated and unknown selector payloads."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
from bench_x04_module_count import load_tool, measure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    tool = load_tool()
    intake = {'inserts': [{'layer': f'Set {i % 4}', 'name': 'Panel', 'handle': f'{i + 160:X}',
                          'rows': 2, 'columns': 3} for i in range(16)]}
    before = deepcopy(intake)
    listing, _ = tool.run(intake, {})
    ids = [row['set_id'] for row in listing['sets']]
    observations = []
    for size in (16, 1000, 10000, 100000):
        params = {'set_ids': [ids[index % 4] for index in range(size)]}
        original = deepcopy(params)
        (result, overlay), valid = measure(lambda: tool.run(intake, params))
        assert result['count'] == 96 and set(overlay['highlight_handles']) == {f'{i + 160:X}' for i in range(16)}
        assert len(overlay['highlight_handles']) == 16 and params == original
        bad = {'set_ids': params['set_ids'] + ['unknown-set'] * size}
        bad_before = deepcopy(bad)
        def refuse():
            try:
                tool.run(intake, bad)
            except ValueError as error:
                assert 'absent from this drawing' in str(error)
                return 'atomic refusal'
            raise AssertionError('Unknown selector returned a partial count')
        refusal, invalid = measure(refuse)
        assert refusal == 'atomic refusal' and bad == bad_before and intake == before
        observations.append({'valid_selector_entries': size, 'unknown_selector_entries': size,
            'physical_parents': 16, 'modules': 96, 'valid': valid, 'invalid': invalid})
    encoded = json.dumps({'schema': 'leaf.x04.selector-payload.v1', 'synthetic': True,
        'timing_gate': False, 'memory_scope': 'Operation allocations; fixture and snapshots excluded.',
        'observations': observations}, indent=2) + '\n'
    if args.out:
        args.out.write_text(encoded)
    print(encoded, end='')


if __name__ == '__main__':
    main()
