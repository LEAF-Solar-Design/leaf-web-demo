"""Observe equal aggregate bytes with independently limited synthetic inputs."""
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
        small, large = Path(directory) / 'small', Path(directory) / 'large'
        pairs = [[0, 'STYLE'], [3, '../synthetic.shx']]
        encoded = json.dumps(export_evidence({'schema': SCHEMA, 'synthetic': True,
            'drawing': {'id': 'synthetic-mixed', 'dependency_pairs': pairs,
                'intake': {'inserts': [{'layer': 'A', 'name': 'Module', 'handle': 'A'}]}},
            'observed_dependencies': from_pairs(pairs),
            'request': {'drawing_id': 'synthetic-mixed', 'set_ids': ['["INSERT","A","Module"]']}})).encode()
        small.write_bytes(encoded)
        for count in (10, 100, 1000):
            large_bytes = encoded + b' ' * (len(encoded) * (count - 1))
            large.write_bytes(large_bytes)
            sets = [('mixed', [small] * count + [large], count, 1), ('control', [small] * (2 * count), 2 * count, 0)]
            assert sum(path.stat().st_size for path in sets[0][1]) == sum(path.stat().st_size for path in sets[1][1])
            for shape, paths, passed, failed in sets:
                tracemalloc.start(); started = time.perf_counter()
                try:
                    report = validate_paths(paths, len(encoded) + 1)
                    seconds = time.perf_counter() - started; peak = tracemalloc.get_traced_memory()[1]
                finally: tracemalloc.stop()
                assert (report['total'], report['passed'], report['failed']) == (len(paths), passed, failed)
                assert [row['index'] for row in report['results']] == list(range(len(paths)))
                assert report['results'][0]['recomputed_result']['count'] == 1
                observations.append({'size': count, 'shape': shape, 'aggregate_bytes': 2 * count * len(encoded),
                    'input_ceiling': len(encoded) + 1, 'seconds': seconds, 'peak_bytes': peak,
                    'passed': passed, 'failed': failed})
            assert validate_paths([large], len(large_bytes))['passed'] == 1
            assert small.read_bytes() == encoded and large.read_bytes() == large_bytes
    print(json.dumps({'schema': 'leaf.x04.batch-mixed-size-observations.v1', 'observations': observations}))


if __name__ == '__main__': main()
