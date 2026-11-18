"""Observe full comparison at fixed artifact bytes and varying membership churn."""
from copy import deepcopy
import json
import time
import tracemalloc
from export_x04_offline_evidence import SCHEMA, export_evidence
from drawing_dependency_report import from_pairs
from compare_x04_offline_evidence import compare_evidence


def artifact(indices):
    pairs = [[code, value] for i in indices for code, value in [(0, 'STYLE'), (3, f'../font-{i:08X}.shx')]]
    return export_evidence({'schema': SCHEMA, 'synthetic': True,
        'drawing': {'id': 'synthetic-churn', 'dependency_pairs': pairs, 'padding': '',
            'intake': {'inserts': [{'layer': 'A', 'name': 'Module', 'handle': format(i + 1, '08X')} for i in indices]}},
        'observed_dependencies': from_pairs(pairs),
        'request': {'drawing_id': 'synthetic-churn', 'set_ids': ['["INSERT","A","Module"]']}})


def main():
    size = 1000; percentages = (0, 1, 10, 100)
    before = artifact(list(range(size)))
    afters = [artifact(list(range(size, size + size * pct // 100)) + list(range(size * pct // 100, size))) for pct in percentages]
    encode = lambda value: json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode('utf-8')
    fixed_bytes = max(len(encode(value)) for value in [before, *afters])
    for value in [before, *afters]:
        value['drawing']['padding'] = ' ' * (fixed_bytes - len(encode(value)))
        assert len(encode(value)) == fixed_bytes
    observations = []
    for percent, after in zip(percentages, afters):
        original = deepcopy((before, after)); changed = size * percent // 100
        tracemalloc.start(); started = time.perf_counter()
        try:
            delta = compare_evidence(before, after)
            seconds = time.perf_counter() - started; peak = tracemalloc.get_traced_memory()[1]
        finally: tracemalloc.stop()
        assert delta['count'] == {'before': size, 'after': size, 'delta': 0}
        assert set(delta['highlight_handles']['removed']) == {format(i + 1, 'X') for i in range(changed)}
        assert set(delta['highlight_handles']['added']) == {format(i + 1, 'X') for i in range(size, size + changed)}
        assert {row['name'] for row in delta['literal_dependencies']['removed']} == {f'../font-{i:08X}.shx' for i in range(changed)}
        assert {row['name'] for row in delta['literal_dependencies']['added']} == {f'../font-{i:08X}.shx' for i in range(size, size + changed)}
        assert (before, after) == original
        observations.append({'percent': percent, 'entities': size, 'artifact_bytes': fixed_bytes,
            'seconds': seconds, 'peak_bytes': peak, 'report_bytes': len(encode(delta)), 'changed_members': changed})
    print(json.dumps({'schema': 'leaf.x04.sparse-churn-observations.v1', 'observations': observations}))


if __name__ == '__main__': main()
