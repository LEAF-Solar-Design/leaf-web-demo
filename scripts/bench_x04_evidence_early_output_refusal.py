"""Observe X04 output-encoder consumption under a fixed tiny byte ceiling."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import tracemalloc
from export_x04_offline_evidence import SCHEMA, export_evidence, write_json
from drawing_dependency_report import from_pairs
from validate_x04_offline_evidence import validate_evidence


def main():
    observations = []
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / 'output.json'
        for size in (1000, 10000, 100000):
            for kind, extension in [('escaped', '東' * size), ('wide', [0] * size)]:
                pairs = [[0, 'STYLE'], [3, 'synthetic.shx']]
                payload = {'schema': SCHEMA, 'synthetic': True,
                           'drawing': {'id': 'synthetic-early-refusal', 'dependency_pairs': pairs,
                                       'future_extension': extension,
                                       'intake': {'inserts': [{'layer': 'Panels', 'name': 'Module', 'handle': 'A'}]}},
                           'observed_dependencies': from_pairs(pairs),
                           'request': {'drawing_id': 'synthetic-early-refusal', 'set_ids': ['["INSERT","Panels","Module"]']}}
                artifact = export_evidence(payload)
                before = deepcopy(artifact)
                target.write_bytes(b'previous')
                consumed = []
                original = json.JSONEncoder.iterencode
                def observed(encoder, value, *args, **kwargs):
                    for chunk in original(encoder, value, *args, **kwargs):
                        consumed.append(len(chunk))
                        yield chunk
                json.JSONEncoder.iterencode = observed
                tracemalloc.start()
                started = time.perf_counter()
                try:
                    try:
                        write_json(artifact, target, max_output_bytes=64)
                    except ValueError as error:
                        assert 'Encoded output exceeds' in str(error)
                    else:
                        raise AssertionError('Tiny output budget was accepted')
                    elapsed = time.perf_counter() - started
                    peak = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
                    json.JSONEncoder.iterencode = original
                assert target.read_bytes() == b'previous' and artifact == before
                assert not list(Path(directory).glob('.x04-*'))
                encoded = (json.dumps(artifact, ensure_ascii=True, allow_nan=False, indent=2) + '\n').encode('utf-8')
                write_json(artifact, target, max_output_bytes=len(encoded))
                assert target.read_bytes() == encoded
                recovered = validate_evidence(json.loads(encoded))
                assert recovered['valid'] and recovered['recomputed_result'] == artifact['result']
                assert artifact['result']['count'] == 1 and artifact['result']['highlight_handles'] == ['A']
                observations.append({'kind': kind, 'size': size, 'ceiling_bytes': 64,
                                     'encoder_chunks': len(consumed), 'encoder_characters': sum(consumed),
                                     'full_output_bytes': len(encoded), 'seconds': elapsed, 'peak_bytes': peak})
    print(json.dumps({'synthetic': True, 'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
