"""Seeded offline adapter construction, including copying and oracle validation."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
import tracemalloc
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'server'))
from x07_customer_profile_store import CustomerProfileStore
from x07_profile_workflow import ProfileWorkflow
from x07_workflow_adapter import WorkflowAdapter


def benchmark(inserts, copy_count):
    rows = [{'name': 'Panel', 'layer': 'MODULES', 'x': i, 'y': (i * 707) % 11} for i in range(inserts)]
    copies = []
    for i in range(copy_count):
        lo, hi = i * inserts // copy_count, (i + 1) * inserts // copy_count - 1
        count = sum(lo <= row['x'] <= hi for row in rows)
        copies.append({'block': 'Panel', 'layer': 'MODULES', 'design_window': [lo, 0, hi, 10], 'oracle_count': count})
    context = {'customer_key': 'synthetic', 'project_key': 'p', 'drawing_key': 'd:v1'}
    with tempfile.TemporaryDirectory(prefix='x07-adapter-init-') as directory:
        store = CustomerProfileStore(Path(directory) / 'profiles.sqlite')
        workflow = ProfileWorkflow(store)
        tracemalloc.start()
        try:
            start = time.perf_counter()
            adapter = WorkflowAdapter(workflow, context=context, intake={'inserts': rows}, copies=copies)
            elapsed = time.perf_counter() - start
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert adapter.load_workflow(context)['copies'] == copies
        assert sum(row['oracle_count'] for row in copies) == inserts
        assert store.latest('synthetic') is None
        return {'inserts': inserts, 'copy_candidates': copy_count, 'elapsed_s': round(elapsed, 6),
                'peak_init_bytes': peak, 'independent_counts': [row['oracle_count'] for row in copies],
                'counts_match': True, 'no_profile_written': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', action='store_true'); parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    report = {'schema': 'leaf.x07.synthetic-adapter-init-perf.v1', 'proof_mode': 'synthetic_offline',
              'measurement': 'adapter construction includes input deepcopy and each candidate oracle validation; excludes fixtures/SQLite initialization',
              'rows': [benchmark(n, c) for n, c in ([(30, 2), (300, 3)] if args.smoke else [(1000, 3), (10000, 10)])]}
    encoded = json.dumps(report, indent=2) + '\n'
    if args.out: args.out.write_text(encoded, encoding='utf-8')
    print(encoded, end='')


if __name__ == '__main__': main()
