"""Growing ignored packet bytes with fixed embedded artifact inventory."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import tracemalloc
from bench_x04_packet_validation import artifact
from compare_x04_offline_evidence import compare_evidence
from validate_x04_review_report import BATCH
import validate_x04_evidence_packet as checker
import export_x04_offline_evidence as shared


def main():
    observations = []; embedded = artifact(); checked = checker.validate_evidence(embedded)
    base = {'schema': checker.SCHEMA, 'artifacts': [{'id': 'a', 'artifact': embedded}, {'id': 'b', 'artifact': deepcopy(embedded)}],
        'comparisons': [{'id': 'same', 'before': 'a', 'after': 'b', 'report': compare_evidence(embedded, embedded)}],
        'batches': [{'id': 'twice', 'artifact_ids': ['a', 'b'], 'report': {
            'schema': BATCH, 'total': 2, 'passed': 2, 'failed': 0, 'original_bytes_integrity': 'unverified',
            'results': [{'index': i, 'path': '../../unopened.json', **deepcopy(checked)} for i in range(2)]}}]}
    expected = checker.validate_packet(base)
    with tempfile.TemporaryDirectory(prefix='x04-packet-volume-') as directory:
        root = Path(directory); source = root / 'packet.json'; output = root / 'summary.json'
        for padding in (0, 4096, 65536):
            value = {**deepcopy(base), 'ignored_padding': 'p' * padding}
            raw = json.dumps(value).encode(); source.write_bytes(raw); digest = hashlib.sha256(raw).hexdigest()
            for mode in ('matching', 'wrong', 'omitted'):
                reads = bytes_read = hashes = calls = 0; active = False; output.write_bytes(b'previous')
                read, decode, sha, validate = checker.read_json, shared.decode_json, shared.hashlib.sha256, checker.validate_evidence
                def observed_read(*args):
                    nonlocal reads
                    reads += 1; return read(*args)
                def observed_decode(data, pin=None):
                    nonlocal bytes_read, active
                    bytes_read += len(data); previous = active; active = pin is not None
                    try: return decode(data, pin)
                    finally: active = previous
                def observed_sha(data=b'', *args, **kwargs):
                    nonlocal hashes
                    if active: hashes += 1
                    return sha(data, *args, **kwargs)
                def trusted(item):
                    nonlocal calls
                    calls += 1; return validate(item)
                checker.validate_evidence = trusted
                checker.read_json = observed_read; shared.decode_json, shared.hashlib.sha256 = observed_decode, observed_sha
                tracemalloc.start(); started = time.perf_counter()
                try:
                    args = [str(source), '--out', str(output)]
                    args += ['--expected-packet-sha256', digest]
                    ids = ['a', 'b'] if mode == 'matching' else ['a'] if mode == 'wrong' else []
                    for identity in ids: args += ['--expected-artifact-id', identity]
                    code = checker.main(args)
                finally:
                    elapsed = time.perf_counter() - started
                    _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
                    checker.validate_evidence = validate
                    checker.read_json = read; shared.decode_json, shared.hashlib.sha256 = decode, sha
                assert reads == 1 and bytes_read == len(raw) and hashes == 1
                assert code == (2 if mode == 'wrong' else 0) and calls == (0 if mode == 'wrong' else 2)
                assert source.read_bytes() == raw
                if mode == 'wrong': assert output.read_bytes() == b'previous'
                else: assert json.loads(output.read_bytes()) == expected
                assert not list(root.glob('.x04-*'))
                observations.append({'padding_bytes': padding, 'packet_bytes': len(raw), 'mode': mode, 'trusted_calls': calls,
                    'bytes_read': bytes_read, 'reads': reads, 'hash_calls': hashes, 'seconds': elapsed, 'peak_bytes': peak})
    print(json.dumps({'scope': 'fixed_packet_claims_inventory_volume', 'hash_scope': 'packet decoding only',
                      'timing_gates': False, 'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
