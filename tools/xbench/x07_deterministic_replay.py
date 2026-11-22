"""Canonical synthetic adapter replay in fresh processes with varied hash seeds.

Only explicit PYTHONHASHSEED is passed; environment values are never read or
copied. Random opaque IDs are mapped to fixture tokens and timestamps excluded.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'server'))
from x07_customer_profile_store import CustomerProfileStore
from x07_profile_workflow import ProfileWorkflow
from x07_workflow_adapter import WorkflowAdapter


def journey(path):
    if path.exists(): raise ValueError('replay requires a new disposable database')
    store = CustomerProfileStore(path); workflow = ProfileWorkflow(store)
    intake = {'inserts': [{'name': block, 'layer': 'MODULES', 'x': x, 'y': 0}
                         for block, points in [('Panel', [0, 1, 2, 10, 11, 12]), ('Other panel', [100, 101])]
                         for x in points], 'polylines': [{'layer': 'ROOF'}]}
    copies = [{'block': 'Panel', 'layer': 'MODULES', 'design_window': [x, 0, x + 2, 0], 'oracle_count': 3} for x in [0, 10]]
    def app(customer, project):
        context = {'customer_key': customer, 'project_key': project, 'drawing_key': project + ':v1'}
        return WorkflowAdapter(workflow, context=context, intake=intake, copies=copies), context
    first, context = app('synthetic-a', 'first')
    proposal = first.load_workflow(context)
    confirmed = first.confirm_roles(context, {**copies[1], 'expected_version': 0})
    old_pin = confirmed['applied_profile']
    refusal = []
    for scope, choice in [({**context, 'customer_key': 'synthetic-b'}, copies[0]),
                           (context, {**copies[0], 'design_window': [0, 0, 999, 0]})]:
        try: first.apply_profile(scope, choice)
        except ValueError as error: refusal.append({'type': type(error).__name__, 'message': str(error)})
        else: raise AssertionError('invalid replay request accepted')
    first.save_profile('synthetic-a', {'module': {'block': 'Panel', 'layer': 'MODULES'}, 'roof': {'layer': 'ROOF'}}, expected_version=1)
    second, next_context = app('synthetic-a', 'next')
    reused = second.apply_profile(next_context, copies[0])
    other, other_context = app('synthetic-b', 'other')
    fresh = other.load_workflow(other_context)
    assert fresh['compatibility']['applied_profile'] is None
    different = other.confirm_roles(other_context, {**copies[0], 'expected_version': 0})
    assert old_pin == store.application_record('first', 'synthetic-a')
    assert confirmed['selection']['count'] == reused['selection']['count'] == different['selection']['count'] == 3
    assert reused['applied_profile']['version'] == 2 and old_pin['version'] == 1
    ids = {store.latest(customer)['profile_id']: 'profile-' + customer for customer in ['synthetic-a', 'synthetic-b']}
    def canonical(value):
        if isinstance(value, dict): return {key: canonical(item) for key, item in value.items() if key != 'created_at'}
        if isinstance(value, list): return [canonical(item) for item in value]
        return ids.get(value, value) if isinstance(value, str) else value
    return canonical({'proposal': proposal, 'confirmed': confirmed, 'refusals': refusal, 'reused': reused,
                      'foreign_fresh': fresh, 'other_pin': different['applied_profile'], 'retained_old_pin': old_pin})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', action='store_true'); parser.add_argument('--out', type=Path)
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--database', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        if args.database is None: parser.error('child requires database')
        print(json.dumps(journey(args.database), sort_keys=True, ensure_ascii=False)); return
    seeds = [1, 77] if args.smoke else [0, 1, 17, 77, 707]
    with tempfile.TemporaryDirectory(prefix='x07-hash-replay-') as directory:
        results = []
        for seed in seeds:
            process = subprocess.run([sys.executable, '-X', 'utf8', str(Path(__file__).resolve()), '--child',
                                      '--database', str(Path(directory) / f'replay-{seed}.sqlite')],
                env={'PYTHONHASHSEED': str(seed)}, capture_output=True, text=True, encoding='utf-8', timeout=20)
            if process.returncode: raise RuntimeError('replay child failed: ' + process.stderr[:1000])
            results.append(json.loads(process.stdout))
        assert all(row == results[0] for row in results[1:]), 'canonical replay differs by hash seed'
    canonical = json.dumps(results[0], sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    report = {'schema': 'leaf.x07.synthetic-deterministic-replay.v1', 'proof_mode': 'synthetic_offline',
              'hash_seeds': seeds, 'canonical_equal': True, 'canonical_sha256': hashlib.sha256(canonical.encode()).hexdigest(),
              'excluded': ['created_at', 'opaque_random_profile_ids', 'timings', 'temporary_paths'], 'canonical': results[0]}
    encoded = json.dumps(report, indent=2, ensure_ascii=False) + '\n'
    if args.out: args.out.write_text(encoded, encoding='utf-8')
    print(encoded, end='')


if __name__ == '__main__': main()
