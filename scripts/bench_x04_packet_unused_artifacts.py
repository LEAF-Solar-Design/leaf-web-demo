"""X04 unused embedded artifacts are all validated, without timing thresholds."""
from copy import deepcopy
import json
import time
import tracemalloc
import compare_x04_offline_evidence as comparison
from export_x04_offline_evidence import export_evidence
from drawing_dependency_report import from_pairs
import validate_x04_evidence_packet as checker


def example(unused):
    artifact = export_evidence({'schema': 'leaf.x04.offline-evidence.v1', 'synthetic': True,
        'drawing': {'id': 'synthetic-unused', 'intake': {'inserts': [
            {'layer': 'Modules', 'name': 'Panel', 'handle': 'A'}]}, 'dependency_pairs': []},
        'observed_dependencies': from_pairs([]),
        'request': {'drawing_id': 'synthetic-unused', 'set_ids': ['["INSERT","Modules","Panel"]']}})
    checked = checker.validate_evidence(artifact)
    return {'schema': checker.SCHEMA, 'artifacts': [{'id': 'a', 'artifact': artifact}] + [
        {'id': f'unused{i:04d}', 'artifact': deepcopy(artifact)} for i in range(unused)],
        'comparisons': [{'id': 'c', 'before': 'a', 'after': 'a',
                         'report': comparison.compare_evidence(artifact, artifact)}],
        'batches': [{'id': 'batch', 'artifact_ids': ['a'], 'report': {
            'schema': 'leaf.x04.offline-batch-validation.v1', 'total': 1, 'passed': 1, 'failed': 0,
            'original_bytes_integrity': 'unverified', 'results': [
                {'index': 0, 'path': '../../unopened.json', **checked}]}}]}


def main():
    observations = []
    for unused in (1, 10, 100):
        payload = example(unused); before = deepcopy(payload)
        original = checker.validate_evidence
        other = comparison.validate_evidence
        calls = 0
        def counted(value):
            nonlocal calls
            calls += 1
            return original(value)
        checker.validate_evidence = comparison.validate_evidence = counted
        tracemalloc.start(); started = time.perf_counter()
        try:
            checked = checker.validate_packet(payload)
        finally:
            checker.validate_evidence, comparison.validate_evidence = original, other
            elapsed = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
        assert calls == unused + 1
        assert payload == before and len(checked['validated_artifacts']) == unused + 1
        assert checked['comparisons'][0]['report']['count']['delta'] == 0
        assert checked['batches'][0]['report']['results'][0]['recomputed_result']['count'] == 1
        forged = deepcopy(payload)
        forged['artifacts'][-1]['artifact']['result']['count'] = 999
        forged_before = deepcopy(forged)
        try:
            checker.validate_packet(forged)
        except ValueError:
            pass
        else:
            raise AssertionError('Forged unused artifact accepted')
        assert forged == forged_before and checker.validate_packet(payload) == checked
        observations.append({'unused_artifacts': unused, 'total_artifacts': unused + 1,
            'fixed_report_references': 3, 'validation_calls': calls, 'seconds': elapsed,
            'peak_bytes': peak, 'unused_forgery_refused': True})
    print(json.dumps({'scope': 'embedded_artifact_report_consistency', 'timing_gates': False,
                      'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
