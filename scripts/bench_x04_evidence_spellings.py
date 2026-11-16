"""Observe equivalent synthetic JSON spellings without timing pass thresholds."""
import hashlib
import json
from pathlib import Path
import tempfile
import time
import tracemalloc
from export_x04_offline_evidence import SCHEMA, export_evidence, read_json
from drawing_dependency_report import from_pairs
from validate_x04_offline_evidence import validate_evidence


def main():
    observations = []
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'synthetic.json'
        for count in (1000, 10000, 100000):
            pairs = [[0, 'STYLE'], [3, '../\u96ea/\u00e9.shx']]
            handles = [format(index + 1, 'X') for index in range(count)]
            artifact = export_evidence({'schema': SCHEMA, 'synthetic': True,
                'drawing': {'id': 'synthetic-\u96ea', 'dependency_pairs': pairs,
                            'intake': {'inserts': [{'layer': 'Panels', 'name': '\u00e9', 'handle': handle}
                                                   for handle in handles]}},
                'observed_dependencies': from_pairs(pairs),
                'request': {'drawing_id': 'synthetic-\u96ea', 'set_ids': [json.dumps(['INSERT', 'Panels', '\u00e9'], ensure_ascii=True, separators=(',', ':'))]}})
            for spelling, options in [('compact-unicode', {'ensure_ascii': False, 'separators': (',', ':')}),
                                      ('escaped-sorted', {'ensure_ascii': True, 'sort_keys': True}),
                                      ('indented', {'ensure_ascii': False, 'indent': 2})]:
                encoded = (' \n' + json.dumps(artifact, **options) + '\t\r\n').encode('utf-8')
                path.write_bytes(encoded)
                tracemalloc.start()
                started = time.perf_counter()
                try:
                    parsed = read_json(path, len(encoded))
                    checked = validate_evidence(parsed)
                    seconds = time.perf_counter() - started
                    peak = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
                assert checked['recomputed_result'] == artifact['result']
                assert checked['recomputed_result']['count'] == count
                assert set(checked['recomputed_result']['highlight_handles']) == set(handles)
                assert parsed['observed_dependencies'] == from_pairs(pairs)
                assert checked['recomputed_result']['original_bytes_integrity'] == 'unverified'
                assert path.read_bytes() == encoded
                observations.append({'entities': count, 'spelling': spelling, 'input_bytes': len(encoded),
                    'seconds': seconds, 'peak_bytes': peak, 'count': count,
                    'highlight_sha256': hashlib.sha256(json.dumps(sorted(handles)).encode()).hexdigest(),
                    'literal_dependency': pairs[1][1]})
    print(json.dumps({'schema': 'leaf.x04.spelling-observations.v1', 'observations': observations}))


if __name__ == '__main__':
    main()
