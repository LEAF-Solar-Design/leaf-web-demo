"""Observe synthetic explicit batch retention at fixed per-artifact size."""
import json
from pathlib import Path
import tempfile
import time
import tracemalloc
from export_x04_offline_evidence import SCHEMA, export_evidence
from drawing_dependency_report import from_pairs
from batch_validate_x04_evidence import validate_paths


def main():
    pairs = [[0, 'STYLE'], [3, '../literal-font.shx']]
    artifact = export_evidence({'schema': SCHEMA, 'synthetic': True,
        'drawing': {'id': 'synthetic-retention', 'dependency_pairs': pairs,
                    'intake': {'inserts': [{'layer': 'Panels', 'name': 'Module', 'handle': 'A'}]}},
        'observed_dependencies': from_pairs(pairs),
        'request': {'drawing_id': 'synthetic-retention', 'set_ids': ['["INSERT","Panels","Module"]']}})
    encoded = json.dumps(artifact).encode('utf-8')
    observations = []
    with tempfile.TemporaryDirectory() as directory:
        good, bad = Path(directory) / 'valid.json', Path(directory) / 'invalid.json'
        good.write_bytes(encoded)
        bad_bytes = b'?' + encoded[1:]
        bad.write_bytes(bad_bytes)
        assert good.stat().st_size == bad.stat().st_size == len(encoded)
        for count in (10, 100, 1000):
            paths = [bad if index % 5 == 2 else good for index in range(count)]
            tracemalloc.start()
            started = time.perf_counter()
            try:
                result = validate_paths(paths, len(encoded))
                output = (json.dumps(result, ensure_ascii=True, indent=2) + '\n').encode('utf-8')
                seconds = time.perf_counter() - started
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            assert [row['index'] for row in result['results']] == list(range(count))
            assert [row['path'] for row in result['results']] == list(map(str, paths))
            assert [row['valid'] for row in result['results']] == [index % 5 != 2 for index in range(count)]
            assert (result['total'], result['passed'], result['failed']) == (count, count * 4 // 5, count // 5)
            for row in result['results']:
                if row['valid']:
                    assert row['recomputed_result'] == {'count': 1, 'highlight_handles': ['A'], 'original_bytes_integrity': 'unverified'}
            assert good.read_bytes() == encoded and bad.read_bytes() == bad_bytes
            observations.append({'occurrences': count, 'artifact_bytes': len(encoded), 'seconds': seconds,
                'peak_bytes': peak, 'output_bytes': len(output), 'passed': result['passed'], 'failed': result['failed']})
    print(json.dumps({'schema': 'leaf.x04.batch-retention-observations.v1', 'observations': observations}))


if __name__ == '__main__':
    main()
