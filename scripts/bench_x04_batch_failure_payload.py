"""Observe bounded JSON diagnostics for growing synthetic failed occurrences."""
import json
from pathlib import Path
import tempfile
import time
import tracemalloc
from export_x04_offline_evidence import SCHEMA, export_evidence
from drawing_dependency_report import from_pairs
from batch_validate_x04_evidence import validate_paths


def main():
    observations = []
    with tempfile.TemporaryDirectory() as directory:
        bad, good = Path(directory) / 'invalid.json', Path(directory) / 'valid.json'
        key = '\x1b\n\t<untrusted>' * 1000
        quoted = json.dumps(key)
        bad.write_text('{' + quoted + ':0,' + quoted + ':1}', encoding='utf-8')
        pairs = [[0, 'STYLE'], [3, '../synthetic-font.shx']]
        good.write_text(json.dumps(export_evidence({'schema': SCHEMA, 'synthetic': True,
            'drawing': {'id': 'synthetic-failure', 'dependency_pairs': pairs,
                'intake': {'inserts': [{'layer': 'A', 'name': 'Module', 'handle': 'A'}]}},
            'observed_dependencies': from_pairs(pairs),
            'request': {'drawing_id': 'synthetic-failure', 'set_ids': ['["INSERT","A","Module"]']}})), encoding='utf-8')
        for count in (10, 100, 1000):
            paths = [bad] * count + [good]
            tracemalloc.start(); started = time.perf_counter()
            try:
                result = validate_paths(paths)
                encoded = (json.dumps(result, ensure_ascii=True, indent=2) + '\n').encode()
                seconds = time.perf_counter() - started; peak = tracemalloc.get_traced_memory()[1]
            finally: tracemalloc.stop()
            assert (result['total'], result['passed'], result['failed']) == (count + 1, 1, count)
            assert [row['index'] for row in result['results']] == list(range(count + 1))
            for row in result['results'][:-1]:
                assert row['valid'] is False and len(row['error']) <= 131
                assert row['error'].startswith('Duplicate JSON key: ') and row['error'].endswith('...')
            assert result['results'][-1]['recomputed_result']['count'] == 1
            assert b'\x1b' not in encoded and b'\\u001b' in encoded
            assert json.loads(encoded) == result and result['original_bytes_integrity'] == 'unverified'
            observations.append({'invalid_occurrences': count, 'seconds': seconds, 'peak_bytes': peak,
                                 'report_bytes': len(encoded), 'max_error_characters': max(len(row['error']) for row in result['results'][:-1])})
    print(json.dumps({'schema': 'leaf.x04.batch-failure-payload-observations.v1', 'observations': observations}))


if __name__ == '__main__': main()
