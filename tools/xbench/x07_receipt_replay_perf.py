"""Bounded offline X07 journal replay timings over disposable real adapters."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'server'))
from x07_customer_profile_store import CustomerProfileStore
from x07_profile_workflow import ProfileWorkflow
from x07_workflow_adapter import ConfirmationReceiptStore, WorkflowAdapter


def measure(requests, iterations):
    if type(requests) is not int or not 1 <= requests <= 1000:
        raise ValueError('request count must be between 1 and 1000')
    if type(iterations) is not int or not 1 <= iterations <= 100:
        raise ValueError('iteration count must be between 1 and 100')
    with tempfile.TemporaryDirectory(prefix='x07-replay-perf-') as directory:
        root = Path(directory)
        store = CustomerProfileStore(root / 'profiles.sqlite')
        journal = ConfirmationReceiptStore(root / 'receipts.sqlite')
        intake = {'inserts': [{'name': 'Panel', 'layer': 'M', 'x': 0, 'y': 0}]}
        choice = {'block': 'Panel', 'layer': 'M', 'design_window': [0, 0, 0, 0], 'oracle_count': 1}

        def bound(index):
            context = {'customer_key': 'synthetic', 'project_key': 'p' + str(index), 'drawing_key': 'd' + str(index)}
            current = WorkflowAdapter(ProfileWorkflow(store), context=context, intake=intake,
                copies=[choice], confirmation_receipts=journal)
            return current, context

        tracemalloc.start()
        try:
            seed_start = time.perf_counter()
            for index in range(requests):
                current, context = bound(index)
                result = current.confirm_roles(context, {**choice, 'expected_version': index})
                if index == 0:
                    first = result
            seed_seconds = time.perf_counter() - seed_start
            current, context = bound(0)
            request = {**choice, 'expected_version': 0}
            replay_start = time.perf_counter()
            for _ in range(iterations):
                assert current.confirm_roles(context, request) == first
            replay_seconds = time.perf_counter() - replay_start
            conflicts = 0
            conflict_start = time.perf_counter()
            for _ in range(iterations):
                try:
                    current.confirm_roles(context, {**request, 'expected_version': 1})
                except ValueError as error:
                    assert str(error) == 'confirmation retry payload changed'
                    conflicts += 1
            conflict_seconds = time.perf_counter() - conflict_start
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert conflicts == iterations
        with store._connection() as db:
            versions = [row[0] for row in db.execute('SELECT version FROM profile_versions ORDER BY version')]
            pins = dict(db.execute('SELECT project_key,version FROM project_applications'))
            assert versions == list(range(1, requests + 1))
            assert pins == {'p' + str(index): index + 1 for index in range(requests)}
        with closing(sqlite3.connect(root / 'receipts.sqlite')) as db:
            assert db.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0] == requests
        assert store.application_record('p0', 'synthetic')['roles'] == {'module': {'block': 'Panel', 'layer': 'M'}}
        return {'requests': requests, 'iterations': iterations, 'seed_seconds': round(seed_seconds, 6),
            'replay_seconds': round(replay_seconds, 6), 'conflict_seconds': round(conflict_seconds, 6),
            'peak_traced_bytes': peak, 'versions': len(versions), 'pins': len(pins),
            'replays': iterations, 'conflicts_refused': conflicts, 'independent_records_match': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    sizes = [1, 10] if args.smoke else [1, 100, 1000]
    iterations = 3 if args.smoke else 20
    result = {'schema': 'leaf.x07.receipt-replay-perf.v1', 'proof_mode': 'synthetic_offline',
        'rows': [measure(size, iterations) for size in sizes]}
    encoded = json.dumps(result, indent=2) + '\n'
    if args.out:
        args.out.write_text(encoded, encoding='utf-8')
    print(encoded, end='')


if __name__ == '__main__':
    main()
