"""Growing ignored packet bytes with pinned/unpinned validation controls."""
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
            for pinned in (False, True):
                reads = bytes_read = hashes = 0; active = False
                read, decode, sha = checker.read_json, shared.decode_json, shared.hashlib.sha256
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
                checker.read_json = observed_read; shared.decode_json, shared.hashlib.sha256 = observed_decode, observed_sha
                tracemalloc.start(); started = time.perf_counter()
                try:
                    args = [str(source), '--out', str(output)]
                    if pinned: args += ['--expected-packet-sha256', digest]
                    assert checker.main(args) == 0
                finally:
                    elapsed = time.perf_counter() - started
                    _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
                    checker.read_json = read; shared.decode_json, shared.hashlib.sha256 = decode, sha
                assert reads == 1 and bytes_read == len(raw) and hashes == int(pinned)
                assert source.read_bytes() == raw and json.loads(output.read_bytes()) == expected
                observations.append({'padding_bytes': padding, 'packet_bytes': len(raw), 'pinned': pinned,
                    'bytes_read': bytes_read, 'reads': reads, 'hash_calls': hashes, 'seconds': elapsed, 'peak_bytes': peak})
    print(json.dumps({'scope': 'fixed_packet_claims_pin_volume', 'hash_scope': 'packet decoding only',
                      'timing_gates': False, 'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
