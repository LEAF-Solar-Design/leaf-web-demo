"""Disposable scoped lookup observations, not a live latency acceptance gate."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
import tracemalloc
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'server'))
from x07_customer_profile_store import CustomerProfileStore, ProfileScopeError


def benchmark(customers, versions, reads):
    with tempfile.TemporaryDirectory(prefix='x07-profile-perf-') as directory:
        store = CustomerProfileStore(Path(directory) / 'profiles.sqlite')
        ids = {}
        for customer in range(customers):
            key = f'synthetic-{customer}'
            for version in range(1, versions + 1):
                record = store.save_version(key, {'module': {'block': f'Panel {customer}', 'layer': f'MODULES {version}'}},
                                            expected_version=version - 1)
                ids[key] = record['profile_id']
                if version == 1:
                    store.apply_to_project('project-' + key, key, expected_version=1)
        tracemalloc.start()
        try:
            started = time.perf_counter()
            for index in range(reads):
                # Fixed arithmetic sequence, no random state or customer data.
                key = f'synthetic-{(index * 7) % customers}'
                latest = store.latest(key)
                pinned = store.application_record('project-' + key, key)
                assert latest['profile_id'] == pinned['profile_id'] == ids[key]
                assert latest['customer_key'] == pinned['customer_key'] == key
                assert latest['version'] == versions and pinned['version'] == 1
                assert pinned['roles']['module']['layer'] == 'MODULES 1'
            elapsed = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert store.latest('missing-synthetic-customer') is None
        try:
            store.application_record('project-synthetic-0', 'missing-synthetic-customer')
        except ProfileScopeError:
            isolated = True
        else:
            raise AssertionError('foreign project read did not refuse')
        return {'customers': customers, 'versions_per_customer': versions, 'lookup_pairs': reads,
                'elapsed_s': round(elapsed, 6), 'peak_lookup_bytes': peak,
                'correct_latest_and_pins': True, 'foreign_scope_refused': isolated}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    sizes = [(3, 2, 12), (12, 3, 24)] if args.smoke else [(100, 5, 500), (500, 10, 1000)]
    report = {'schema': 'leaf.x07.synthetic-profile-perf.v1', 'proof_mode': 'synthetic_offline',
              'measurement': 'scoped latest and pinned read pairs; excludes SQLite setup and seeded writes',
              'rows': [benchmark(*size) for size in sizes]}
    encoded = json.dumps(report, indent=2) + '\n'
    if args.out:
        args.out.write_text(encoded, encoding='utf-8')
    print(encoded, end='')


if __name__ == '__main__':
    main()
