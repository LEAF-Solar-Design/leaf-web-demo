"""Fixed-byte saved-report pin refusal observations as batch row count grows."""
import hashlib
import json
from pathlib import Path
import tempfile
import time
import tracemalloc
import validate_x04_review_report as checker
import export_x04_offline_evidence as shared


def batch(size):
    return {'schema': checker.BATCH, 'total': size, 'passed': size, 'failed': 0,
            'original_bytes_integrity': 'unverified', 'results': [
                {'index': i, 'path': '../../unopened.json', 'valid': True,
                 'schema': 'leaf.x04.offline-validation.v1', 'drawing_id': 'synthetic-report',
                 'recomputed_result': {'count': 1, 'highlight_handles': ['A'],
                                       'original_bytes_integrity': 'unverified'}} for i in range(size)]}


def main():
    observations = []; target_bytes = 131072
    with tempfile.TemporaryDirectory(prefix='x04-report-pin-') as directory:
        root = Path(directory); source = root / 'report.json'; output = root / 'summary.json'
        for size in (1, 10, 100):
            value = batch(size); encoded = json.dumps(value).encode(); assert len(encoded) <= target_bytes
            raw = encoded + b' ' * (target_bytes - len(encoded)); source.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            for mode in ('stale', 'matching', 'unpinned'):
                output.write_bytes(b'previous'); calls = hashes = rows = 0; active = False
                decode, sha, validate, membership = shared.decode_json, shared.hashlib.sha256, checker.validate_report, checker.membership
                def observed_decode(data, pin=None):
                    nonlocal active
                    previous = active; active = pin is not None
                    try: return decode(data, pin)
                    finally: active = previous
                def observed_sha(data=b'', *args, **kwargs):
                    nonlocal hashes
                    if active: hashes += 1
                    return sha(data, *args, **kwargs)
                def checked(item):
                    nonlocal calls
                    calls += 1; return validate(item)
                def checked_membership(items, label, dependencies=False):
                    nonlocal rows
                    if label == 'recomputed_result.highlight_handles': rows += 1
                    return membership(items, label, dependencies)
                shared.decode_json, shared.hashlib.sha256 = observed_decode, observed_sha
                checker.validate_report, checker.membership = checked, checked_membership
                tracemalloc.start(); started = time.perf_counter()
                try:
                    args = [str(source), '--out', str(output)]
                    if mode != 'unpinned': args += ['--expected-report-sha256', '0' * 64 if mode == 'stale' else digest]
                    code = checker.main(args)
                finally:
                    elapsed = time.perf_counter() - started
                    _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
                    shared.decode_json, shared.hashlib.sha256 = decode, sha
                    checker.validate_report, checker.membership = validate, membership
                assert hashes == (0 if mode == 'unpinned' else 1)
                assert code == (2 if mode == 'stale' else 0) and calls == (0 if mode == 'stale' else 1)
                assert rows == (0 if mode == 'stale' else size)
                if mode == 'stale': assert output.read_bytes() == b'previous'
                else:
                    summary = json.loads(output.read_bytes())
                    assert summary == checker.validate_report(value)
                    assert summary['validation_scope'] == 'saved_report_internal_consistency'
                    assert summary['underlying_artifacts_revalidated'] is False
                    assert summary['original_bytes_integrity'] == 'unverified'
                assert all(row['recomputed_result']['count'] == 1 and row['recomputed_result']['highlight_handles'] == ['A'] for row in value['results'])
                assert source.read_bytes() == raw and not list(root.glob('.x04-*'))
                observations.append({'batch_rows': size, 'report_bytes': len(raw), 'mode': mode,
                    'hash_calls': hashes, 'report_checks': calls, 'row_checks': rows,
                    'seconds': elapsed, 'peak_bytes': peak})
    print(json.dumps({'scope': 'fixed_byte_saved_report_pin_refusal', 'hash_scope': 'report decoding only',
                      'timing_gates': False, 'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
