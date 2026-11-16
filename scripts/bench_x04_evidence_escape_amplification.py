"""Observe literal Unicode and control escaping at fixed X04 decoded length."""
from copy import deepcopy
import json
import time
import tracemalloc
from export_x04_offline_evidence import SCHEMA, export_evidence
from drawing_dependency_report import from_pairs
from validate_x04_offline_evidence import validate_evidence


def main():
    observations = []
    for label, pattern in [('ascii', 'A'), ('cjk', '東'), ('astral', '\U0001f680'), ('controls', '\x1b\n\\"')]:
        literal = (pattern * 4096)[:4096]
        pairs = [[0, 'STYLE'], [3, 'synthetic.shx']]
        payload = {'schema': SCHEMA, 'synthetic': True,
                   'drawing': {'id': 'synthetic-escaping', 'dependency_pairs': pairs,
                               'future_extension': {'literal': literal},
                               'intake': {'inserts': [{'layer': 'Panels', 'name': 'Module', 'handle': 'A'}]}},
                   'observed_dependencies': from_pairs(pairs),
                   'request': {'drawing_id': 'synthetic-escaping', 'set_ids': ['["INSERT","Panels","Module"]']}}
        before = deepcopy(payload)
        input_bytes = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        tracemalloc.start()
        started = time.perf_counter()
        artifact = export_evidence(payload)
        output_bytes = (json.dumps(artifact, ensure_ascii=True, allow_nan=False, indent=2) + '\n').encode('utf-8')
        export_seconds = time.perf_counter() - started
        export_peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        tracemalloc.start()
        started = time.perf_counter()
        replayed = json.loads(output_bytes)
        validated = validate_evidence(replayed)
        validate_seconds = time.perf_counter() - started
        validate_peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        assert len(literal) == 4096 and replayed['drawing']['future_extension']['literal'] == literal
        assert validated['valid'] and validated['recomputed_result'] == artifact['result']
        assert artifact['result'] == {'count': 1, 'highlight_handles': ['A'], 'original_bytes_integrity': 'unverified'}
        assert payload == before
        observations.append({'kind': label, 'decoded_characters': 4096, 'input_bytes': len(input_bytes),
                             'output_bytes': len(output_bytes), 'export_seconds': export_seconds,
                             'export_peak_bytes': export_peak, 'validate_seconds': validate_seconds,
                             'validate_peak_bytes': validate_peak})
    print(json.dumps({'synthetic': True, 'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
