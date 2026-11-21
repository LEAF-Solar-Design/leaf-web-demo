"""Bounded offline scoped-audit metrics with independent issue checks."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import tracemalloc
from x07_profile_history_audit import CustomerProfileStore, MAX_AUDIT_ROWS, MAX_PROFILE_VERSION, audit_history


def benchmark(version_count, pin_count, mode, iterations):
    step = 1 if mode == 'dense' else MAX_PROFILE_VERSION // version_count
    numbers = [1 + step * index for index in range(version_count)]
    expected_gaps = [{'first': a+1, 'last': b-1} for a,b in zip(numbers,numbers[1:]) if b>a+1]
    expected = {'missing_profile': 0, 'empty_chain': 0, 'version_gaps': expected_gaps,
                'invalid_versions': 0, 'foreign_version_profiles': 0, 'dangling_pin_versions': 0, 'foreign_pin_profiles': 0}
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'store.sqlite'
        store = CustomerProfileStore(path)
        saved = store.save_version('synthetic-a', {'module': {'block': 'A', 'layer': 'M'}}, expected_version=0)
        old = store.apply_to_project('old', 'synthetic-a')
        with closing(sqlite3.connect(path)) as db, db:
            db.executemany('INSERT INTO profile_versions VALUES (?,?,?,?,?)',
                           (('synthetic-a',saved['profile_id'],version,'{"module":{"block":"A","layer":"M"}}','synthetic') for version in numbers[1:]))
            db.executemany('INSERT INTO project_applications VALUES (?,?,?,?)',
                           ((f'pin-{index}','synthetic-a',saved['profile_id'],numbers[index % version_count]) for index in range(1,pin_count)))
        read = CustomerProfileStore(path, read_only=True)
        before = path.read_bytes()
        tracemalloc.start()
        try:
            started = time.perf_counter()
            for _ in range(iterations):
                result = audit_history(read, customer_key='synthetic-a')
                if result['issues'] != expected or result['version_count'] != version_count or result['pin_count'] != pin_count or result['healthy'] != (not expected_gaps):
                    raise RuntimeError('independent record oracle mismatch')
            elapsed = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        if read.application_record('old','synthetic-a') != old or path.read_bytes() != before:
            raise RuntimeError('old pin or database bytes changed')
    return {'versions':version_count, 'pins':pin_count, 'mode':mode, 'iterations':iterations,
            'largest_version':numbers[-1], 'gap_intervals':len(expected_gaps), 'independent_issues_match':True,
            'old_pin_and_bytes_unchanged':True, 'elapsed_s':round(elapsed,6), 'peak_python_bytes':peak}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--version-counts', nargs='+', type=int, default=[16,MAX_AUDIT_ROWS-1,MAX_AUDIT_ROWS])
    parser.add_argument('--pin-counts', nargs='+', type=int, default=[16,MAX_AUDIT_ROWS-1,MAX_AUDIT_ROWS])
    parser.add_argument('--iterations', type=int, default=3)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    pairs = [(3,2),(8,5)] if args.smoke else list(zip(args.version_counts,args.pin_counts))
    if len(args.version_counts) != len(args.pin_counts) or len(pairs)>6 or any(not 1<=value<=MAX_AUDIT_ROWS for pair in pairs for value in pair) or not 1<=args.iterations<=10:
        parser.error('equal bounded count lists and iterations required')
    result = {'schema':'leaf.x07.history-audit-perf.v1', 'proof_mode':'synthetic_offline',
              'measurement':'Audit and independent issue checks; excludes setup and final pin/byte verification',
              'row_budget_per_collection':MAX_AUDIT_ROWS,
              'rows':[benchmark(versions,pins,mode,1 if args.smoke else args.iterations) for versions,pins in pairs for mode in ['dense','sparse']]}
    encoded = json.dumps(result,indent=2,allow_nan=False)+'\n'
    if len(encoded.encode())>32768: raise RuntimeError('metrics output exceeds bound')
    if args.out: args.out.write_text(encoded,encoding='utf-8')
    print(encoded,end='')


if __name__ == '__main__': main()
