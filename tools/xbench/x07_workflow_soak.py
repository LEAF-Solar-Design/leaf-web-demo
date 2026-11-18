"""Seeded offline state transitions; abandoned inspect models cancellation.

This is not UI cancellation or live performance proof. The actual JSX race
checks supply separate UI evidence. All databases are disposable.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import random
import sqlite3
import sys
import tempfile
import tracemalloc
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'server'))
from x07_customer_profile_store import CustomerProfileStore, ProfileScopeError
from x07_profile_workflow import ProfileWorkflow
from x07_workflow_adapter import WorkflowAdapter


def run(cycles, seed):
    rng = random.Random(seed)
    with tempfile.TemporaryDirectory(prefix='x07-workflow-soak-') as directory:
        path = Path(directory) / 'profiles.sqlite'
        store = CustomerProfileStore(path); workflow = ProfileWorkflow(store)
        pins, versions = {}, {'a': 1, 'b': 1}
        def adapter(customer, project):
            rows = [{'name': 'Panel ' + customer, 'layer': 'MODULES', 'x': x, 'y': 0} for x in [0, 1, 2, 10, 11, 12]]
            copies = [{'block': 'Panel ' + customer, 'layer': 'MODULES', 'design_window': [x, 0, x + 2, 0],
                       'oracle_count': sum(x <= row['x'] <= x + 2 for row in rows)} for x in [0, 10]]
            context = {'customer_key': customer, 'project_key': project, 'drawing_key': project + ':v1'}
            return WorkflowAdapter(workflow, context=context, intake={'inserts': rows, 'polylines': [{'layer': 'ROOF'}]}, copies=copies), context, copies
        def counts():
            db = sqlite3.connect(path)
            try: return {name: db.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]
                         for name in ['customers', 'profile_versions', 'project_applications']}
            finally: db.close()
        for customer in ['a', 'b']:
            app, context, copies = adapter(customer, 'initial-' + customer)
            result = app.confirm_roles(context, {**copies[0], 'expected_version': 0})
            pins[context['project_key']] = result['applied_profile']
        samples, cancelled, edits = [], 0, 0
        tracemalloc.start()
        try:
            for index in range(cycles):
                customer = ['a', 'b'][index % 2]
                app, context, copies = adapter(customer, 'cycle-' + str(index))
                plan = app.load_workflow(context)
                assert plan['compatibility']['decision'] == 'apply'
                if index % 5 == 0:
                    before = counts()
                    app.load_workflow(context)  # cancellation: abandon before save/apply
                    assert counts() == before
                    cancelled += 1
                else:
                    if index % 7 == 0:
                        roles = deepcopy(app.load_profile(customer)['roles'])
                        if 'roof' in roles: del roles['roof']
                        else: roles['roof'] = {'layer': 'ROOF'}
                        saved = app.save_profile(customer, roles, expected_version=versions[customer])
                        versions[customer] += 1; edits += 1
                        assert saved['version'] == versions[customer]
                    choice = copies[rng.randrange(2)]
                    result = app.apply_profile(context, choice)
                    assert result['selection']['count'] == choice['oracle_count'] == 3
                    assert result['applied_profile']['customer_key'] == customer
                    assert result['applied_profile']['version'] == versions[customer]
                    pins[context['project_key']] = result['applied_profile']
                    assert app.apply_profile(context, choice)['applied_profile'] == pins[context['project_key']]
                    try: store.application_record(context['project_key'], 'b' if customer == 'a' else 'a')
                    except ProfileScopeError: pass
                    else: raise AssertionError('foreign scope did not refuse')
                if index % max(1, cycles // 10) == 0:
                    current, peak = tracemalloc.get_traced_memory()
                    samples.append({'cycle': index, 'current_bytes': current, 'peak_bytes': peak})
            current, peak = tracemalloc.get_traced_memory()
        finally: tracemalloc.stop()
        for project, record in pins.items():
            assert store.application_record(project, record['customer_key']) == record
        retained = counts()
        assert retained == {'customers': 2, 'profile_versions': sum(versions.values()), 'project_applications': len(pins)}
        assert len(pins) == 2 + cycles - cancelled
        return {'schema': 'leaf.x07.synthetic-workflow-soak.v1', 'proof_mode': 'synthetic_offline',
                'cycles': cycles, 'seed': seed, 'cancelled_inspects': cancelled, 'profile_edits': edits,
                'retained_records': retained, 'latest_versions': versions, 'all_old_pins_retained': True,
                'scope_refusals': True, 'independent_counts_match': True, 'memory_samples': samples,
                'final_current_bytes': current, 'peak_bytes': peak, 'memory_threshold_claim': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', action='store_true'); parser.add_argument('--cycles', type=int, default=200)
    parser.add_argument('--seed', type=int, default=707); parser.add_argument('--out', type=Path)
    args = parser.parse_args(); cycles = 20 if args.smoke else args.cycles
    if not 1 <= cycles <= 1000: parser.error('cycles must be between 1 and 1000')
    encoded = json.dumps(run(cycles, args.seed), indent=2) + '\n'
    if args.out: args.out.write_text(encoded, encoding='utf-8')
    print(encoded, end='')


if __name__ == '__main__': main()
