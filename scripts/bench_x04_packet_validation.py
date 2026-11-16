"""X04 packet-local binding observations with equal-byte reference controls."""
from copy import deepcopy
import json
import time
import tracemalloc
import compare_x04_offline_evidence as comparison
from export_x04_offline_evidence import export_evidence
from drawing_dependency_report import from_pairs
import validate_x04_evidence_packet as packet
from validate_x04_review_report import BATCH


def artifact():
    return export_evidence({'schema': 'leaf.x04.offline-evidence.v1', 'synthetic': True,
        'drawing': {'id': 'synthetic-benchmark', 'intake': {'inserts': [
            {'layer': 'Modules', 'name': 'Panel', 'handle': 'A'}]}, 'dependency_pairs': []},
        'request': {'drawing_id': 'synthetic-benchmark', 'set_ids': ['["INSERT","Modules","Panel"]']},
        'observed_dependencies': from_pairs([])})


def main():
    observations = []
    embedded = artifact()
    checked = packet.validate_evidence(embedded)
    for size in (2, 10, 50):
        for occurrences in (size, 4 * size):
            cohort_bytes = []
            for kind in ('repeated', 'distributed'):
                refs = ['a000' if kind == 'repeated' else f'a{i % size:03d}' for i in range(occurrences)]
                rows = [{'index': i, 'path': '../../unopened.json', **deepcopy(checked)} for i in range(occurrences)]
                value = {'schema': packet.SCHEMA,
                    'artifacts': [{'id': f'a{i:03d}', 'artifact': deepcopy(embedded)} for i in range(size)],
                    'comparisons': [{'id': 'c', 'before': 'a000', 'after': 'a001',
                                     'report': comparison.compare_evidence(embedded, embedded)}],
                    'batches': [{'id': 'batch', 'artifact_ids': refs, 'report': {
                        'schema': BATCH, 'total': occurrences, 'passed': occurrences, 'failed': 0,
                        'results': rows, 'original_bytes_integrity': 'unverified'}}]}
                before = deepcopy(value)
                encoded = len(json.dumps(value, ensure_ascii=True).encode('utf-8'))
                cohort_bytes.append(encoded)
                calls = 0
                original_packet, original_compare = packet.validate_evidence, comparison.validate_evidence
                def counted(artifact):
                    nonlocal calls
                    calls += 1
                    return original_packet(artifact)
                packet.validate_evidence = comparison.validate_evidence = counted
                tracemalloc.start()
                started = time.perf_counter()
                try:
                    result = packet.validate_packet(value)
                finally:
                    packet.validate_evidence, comparison.validate_evidence = original_packet, original_compare
                    elapsed = time.perf_counter() - started
                    _, peak = tracemalloc.get_traced_memory()
                    tracemalloc.stop()
                assert calls == size + 2
                assert value == before and result['valid']
                assert len(result['validated_artifacts']) == size
                assert result['batches'][0]['artifact_ids'] == refs
                assert result['batches'][0]['report']['total'] == occurrences
                assert result['original_bytes_integrity'] == 'unverified'
                bad = deepcopy(value)
                bad['batches'][0]['report']['results'][-1]['recomputed_result']['count'] = 2
                try:
                    packet.validate_packet(bad)
                except ValueError:
                    pass
                else:
                    raise AssertionError('Cross-bound count forgery accepted')
                assert packet.validate_packet(value)['valid'] and value == before
                observations.append({'artifacts': size, 'occurrences': occurrences, 'kind': kind,
                    'encoded_bytes': encoded, 'validation_calls': calls,
                    'seconds': elapsed, 'peak_bytes': peak})
            assert cohort_bytes[0] == cohort_bytes[1]
    print(json.dumps({'scope': 'embedded_artifact_report_consistency', 'timing_gates': False,
                      'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
