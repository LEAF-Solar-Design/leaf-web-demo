"""X07 bounded alias-map compatibility and adapter metrics on synthetic inputs."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import tracemalloc
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'server'))
from x07_customer_profile_store import CustomerProfileStore
from x07_profile_workflow import ProfileWorkflow
from x07_profile_compatibility import check_compatibility
from x07_workflow_adapter import WorkflowAdapter


def benchmark(size, mode):
    prefix = 'shared-prefix-' * 4
    key = '*U' + prefix + '0000'
    first, second = 'Panel ' + prefix + 'A', 'Panel ' + prefix + 'B'
    aliases = {'*U' + prefix + str(index).zfill(4): 'Unused ' + prefix + str(index) for index in range(size)}
    targets = [first] if mode == 'unique' else [first, second] if mode == 'ambiguous' else ['Missing']
    aliases[key] = targets
    intake = {'inserts': [dict(name=first, layer='M', x=0, y=0), dict(name=second, layer='M', x=1, y=0)]}
    profile = dict(customer_key='synthetic', profile_id='synthetic-profile', version=1,
                   roles={'module': {'block': key, 'layer': 'M'}})
    # Independent literal alias oracle has no product normalization or role lookup.
    observed = {row['name'] for row in intake['inserts'] if row['layer'] == 'M'}
    matches = set(targets) & observed
    expected = 'apply' if len(matches) == 1 else 'propose_fresh'
    before = copy.deepcopy([intake, profile, aliases])
    with tempfile.TemporaryDirectory() as directory:
        store = CustomerProfileStore(Path(directory) / 'profiles.sqlite')
        store.save_version('synthetic', profile['roles'], expected_version=0)
        copies = [dict(block=first, layer='M', design_window=[0, 0, 0, 0], oracle_count=1)]
        tracemalloc.start()
        try:
            started = time.perf_counter()
            result = check_compatibility(intake, profile, customer_key='synthetic', block_aliases=aliases)
            adapter = WorkflowAdapter(ProfileWorkflow(store), context=dict(customer_key='synthetic', project_key='p', drawing_key='d:v1'),
                                      intake=intake, copies=copies, block_aliases=aliases)
            elapsed = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        plan = adapter.load_workflow(adapter._context)
        if result['decision'] != expected or plan['compatibility']['decision'] != expected:
            raise RuntimeError('independent alias decision mismatch')
        if expected == 'apply' and result['roles']['module']['block'] != next(iter(matches)):
            raise RuntimeError('independent alias target mismatch')
    if [intake, profile, aliases] != before:
        raise RuntimeError('caller inputs changed')
    return {'aliases': size, 'mode': mode, 'decision': expected, 'oracle_matches': True,
            'elapsed_s': round(elapsed, 6), 'peak_python_bytes': peak,
            'input_sha256': hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--sizes', type=int, nargs='+', default=[1, 64, 512, 4096])
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    sizes = [1, 8] if args.smoke else args.sizes
    if len(sizes) > 8 or any(not 1 <= size <= 4096 for size in sizes):
        parser.error('at most8 alias-map sizes between1 and4096')
    result = {'schema': 'leaf.x07.alias-fanout-perf.v1',
              'measurement': 'Compatibility plus adapter construction time and Python allocation peak, excluding input and schema creation',
              'rows': [benchmark(size, mode) for size in sizes for mode in ('unique', 'missing', 'ambiguous')]}
    encoded = json.dumps(result, indent=2) + '\n'
    if args.out:
        args.out.write_text(encoded, encoding='utf-8')
    print(encoded, end='')


if __name__ == '__main__':
    main()
