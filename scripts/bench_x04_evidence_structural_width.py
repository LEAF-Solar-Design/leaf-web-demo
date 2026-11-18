"""Observe scalar-array width against equal-byte X04 string controls."""
from copy import deepcopy
import io
import json
import sys
import time
import tracemalloc
from types import SimpleNamespace
from export_x04_offline_evidence import SCHEMA, export_evidence, read_json
from drawing_dependency_report import from_pairs
from validate_x04_offline_evidence import validate_evidence


def measure(operation):
    tracemalloc.start()
    started = time.perf_counter()
    try:
        result = operation()
        observed = {'seconds': time.perf_counter() - started,
                    'peak_bytes': tracemalloc.get_traced_memory()[1]}
    finally:
        tracemalloc.stop()
    return result, observed


def parse_input(encoded):
    previous = sys.stdin
    try:
        sys.stdin = SimpleNamespace(buffer=io.BytesIO(encoded))
        return read_json('-')
    finally:
        sys.stdin = previous


def main():
    observations = []
    for width in (100, 10000, 100000):
        pair_sizes = []
        for kind, extension in [('array', [0] * width), ('string', 'x' * (2 * width - 1))]:
            pairs = [[0, 'STYLE'], [3, 'synthetic.shx']]
            payload = {'schema': SCHEMA, 'synthetic': True,
                       'drawing': {'id': 'synthetic-width', 'dependency_pairs': pairs,
                                   'future_extension': extension,
                                   'intake': {'inserts': [{'layer': 'Panels', 'name': 'Module', 'handle': 'A'}]}},
                       'observed_dependencies': from_pairs(pairs),
                       'request': {'drawing_id': 'synthetic-width', 'set_ids': ['["INSERT","Panels","Module"]']}}
            before = deepcopy(payload)
            encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            pair_sizes.append(len(encoded))
            parsed, parsing = measure(lambda: parse_input(encoded))
            parsed_before = deepcopy(parsed)
            artifact, exporting = measure(lambda: export_evidence(parsed))
            validated, validating = measure(lambda: validate_evidence(artifact))
            assert parsed == parsed_before and payload == before
            assert artifact['drawing']['future_extension'] == extension
            assert validated['valid'] and validated['recomputed_result'] == artifact['result']
            assert artifact['result'] == {'count': 1, 'highlight_handles': ['A'], 'original_bytes_integrity': 'unverified'}
            observations.append({'kind': kind, 'scalar_width': width, 'input_bytes': len(encoded),
                                 'output_bytes': len(json.dumps(artifact, ensure_ascii=True, indent=2)) + 1,
                                 'parse': parsing, 'export': exporting, 'validate': validating})
        assert pair_sizes[0] == pair_sizes[1]
    print(json.dumps({'synthetic': True, 'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
