"""Seeded complete offline workflow observations, not staging performance proof."""
import argparse
import json
from pathlib import Path
import random
import sys
import tempfile
import time
import tracemalloc
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'server'))
from x07_customer_profile_store import CustomerProfileStore
from x07_profile_workflow import ProfileWorkflow
from x07_workflow_adapter import WorkflowAdapter


def benchmark(inserts, reopen_count, seed):
    rng = random.Random(seed)
    rows = [{'name': 'Panel', 'layer': 'MODULES', 'x': rng.randrange(100), 'y': rng.randrange(100)}
            for _ in range(inserts)]
    window = [20, 10, 70, 80]
    # Independent fixture oracle, without calling the product selector.
    expected = sum(20 <= row['x'] <= 70 and 10 <= row['y'] <= 80 for row in rows)
    choice = {'block': 'Panel', 'layer': 'MODULES', 'design_window': window, 'oracle_count': expected}
    context = {'customer_key': 'synthetic-a', 'project_key': 'synthetic-first', 'drawing_key': 'synthetic-first:v1'}
    with tempfile.TemporaryDirectory(prefix='x07-workflow-perf-') as directory:
        store = CustomerProfileStore(Path(directory) / 'profiles.sqlite')
        workflow = ProfileWorkflow(store)
        # Initialization and fixture construction are outside measurement.
        tracemalloc.start()
        try:
            started = time.perf_counter()
            adapter = WorkflowAdapter(workflow, context=context, intake={'inserts': rows}, copies=[choice])
            fresh = adapter.load_workflow(context)
            first = adapter.confirm_roles(context, {**choice, 'expected_version': fresh['expected_version']})
            confirm_elapsed = time.perf_counter() - started
            started = time.perf_counter()
            next_context = {**context, 'project_key': 'synthetic-next', 'drawing_key': 'synthetic-next:v1'}
            next_adapter = WorkflowAdapter(workflow, context=next_context, intake={'inserts': rows}, copies=[choice])
            assert next_adapter.load_workflow(next_context)['compatibility']['decision'] == 'apply'
            reused = next_adapter.apply_profile(next_context, choice)
            reuse_elapsed = time.perf_counter() - started
            started = time.perf_counter()
            for _ in range(reopen_count):
                reopened = next_adapter.apply_profile(next_context, choice)
                assert reopened['selection']['count'] == expected
                assert reopened['applied_profile'] == reused['applied_profile']
            reopen_elapsed = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert first['selection']['count'] == reused['selection']['count'] == expected
        assert first['applied_profile']['profile_id'] == reused['applied_profile']['profile_id']
        assert store.latest('synthetic-a')['version'] == 1
        return {'inserts': inserts, 'seed': seed, 'reopens': reopen_count, 'independent_count': expected,
                'inspect_confirm_s': round(confirm_elapsed, 6), 'inspect_apply_s': round(reuse_elapsed, 6),
                'pinned_reopens_s': round(reopen_elapsed, 6), 'peak_workflow_bytes': peak,
                'counts_match': True, 'pins_retained': True, 'profile_version': 1}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    report = {'schema': 'leaf.x07.synthetic-workflow-perf.v1', 'proof_mode': 'synthetic_offline',
              'measurement': 'adapter construction, inspect/confirm/apply and repeated pinned reopen; excludes seeded input and SQLite initialization',
              'rows': [benchmark(size, count, 707) for size, count in ([(30, 2), (300, 3)] if args.smoke else [(1000, 10), (10000, 20)])]}
    encoded = json.dumps(report, indent=2) + '\n'
    if args.out:
        args.out.write_text(encoded, encoding='utf-8')
    print(encoded, end='')


if __name__ == '__main__':
    main()
