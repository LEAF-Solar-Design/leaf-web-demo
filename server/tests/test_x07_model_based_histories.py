"""Independent role/version/pin model audited after every seeded command."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import random
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_workflow_adapter import WorkflowAdapter

INTAKE = {'inserts': [dict(name='Panel', layer=layer, x=x, y=0, handle=f'{layer}-{x}')
                      for layer in ['M0', 'M1', 'M2'] for x in [0, 1, 2, 10, 11, 12]]}
COPIES = [dict(block='Panel', layer=layer, design_window=[x, 0, x+2, 0], oracle_count=3)
          for layer in ['M0', 'M1', 'M2'] for x in [0, 10]]


def roles(layer): return {'module': {'block': 'Panel', 'layer': layer}}


class ModelBasedHistories(unittest.TestCase):
    def test_interleaved_customers_match_independent_immutable_history_after_every_command(self):
        for seed in [1707, 2707, 3707]:
            with self.subTest(seed=seed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); rng = random.Random(seed)
                versions = {'a': [], 'b': []}; pins = {}; receipts = {}
                frozen_versions, frozen_receipts, identifiers = {}, {}, {}
                def bound(customer, project):
                    context = dict(customer_key=customer, project_key=project, drawing_key=project+':v1')
                    return WorkflowAdapter.from_paths(profile_path=root/'profiles.sqlite', receipt_path=root/'receipts.sqlite',
                        context=context, intake=INTAKE, copies=COPIES), context
                def audit():
                    with closing(sqlite3.connect(root/'profiles.sqlite')) as db:
                        records = db.execute('SELECT customer_key,profile_id,version,roles_json,created_at FROM profile_versions ORDER BY customer_key,version').fetchall()
                        expected = [(c, v) for c in ['a', 'b'] for v in range(1, len(versions[c])+1)]
                        self.assertEqual([(row[0], row[2]) for row in records], expected)
                        for row in records:
                            c, identity, version, encoded, _ = row
                            self.assertEqual(json.loads(encoded), versions[c][version-1])
                            identifiers.setdefault(c, identity); self.assertEqual(identity, identifiers[c])
                            frozen_versions.setdefault((c, version), row)
                            self.assertEqual(row, frozen_versions[c, version])
                        self.assertEqual(len(set(identifiers.values())), len(identifiers))
                        actual_pins = db.execute('SELECT project_key,customer_key,profile_id,version FROM project_applications ORDER BY project_key').fetchall()
                        self.assertEqual([(p, c, v) for p, c, _, v in actual_pins],
                                         [(p, pins[p][0], pins[p][1]) for p in sorted(pins)])
                        for p, c, identity, v in actual_pins:
                            self.assertEqual(identity, identifiers[c])
                            self.assertEqual(json.loads(next(row[3] for row in records if row[0] == c and row[2] == v)), versions[c][v-1])
                    with closing(sqlite3.connect(root/'receipts.sqlite')) as db:
                        rows = db.execute('SELECT project_key,customer_key,digest,result FROM confirmations ORDER BY project_key').fetchall()
                    self.assertEqual([row[0] for row in rows], sorted(receipts))
                    for row in rows:
                        p, c, _, encoded = row; value = json.loads(encoded)
                        self.assertEqual(value['applied_profile']['version'], pins[p][1])
                        self.assertEqual(value['resolved_roles'], versions[c][pins[p][1]-1])
                        self.assertEqual(value['selection']['count'], 3)
                        frozen_receipts.setdefault(p, row); self.assertEqual(row, frozen_receipts[p])
                def confirm(customer, project, choice):
                    current, context = bound(customer, project)
                    request = {**deepcopy(choice), 'expected_version': len(versions[customer])}
                    value = current.confirm_roles(context, request)
                    versions[customer].append(roles(choice['layer']))
                    pins[project] = (customer, len(versions[customer]), deepcopy(choice))
                    receipts[project] = (context, deepcopy(request), deepcopy(value))
                    self.assertEqual(value['resolved_roles'], versions[customer][-1])
                    self.assertEqual(value['selection']['count'], 3)
                for c in ['a', 'b']:
                    confirm(c, 'initial-'+c, rng.choice(COPIES)); audit()
                for step in range(80):
                    operation = rng.choice(['edit', 'confirm', 'retry', 'reopen', 'reuse', 'stale', 'foreign'])
                    customer = rng.choice(['a', 'b'])
                    with self.subTest(step=step, operation=operation):
                        if operation in ['edit', 'stale']:
                            current, _ = bound(customer, 'editor-'+customer)
                            wanted = roles(rng.choice(['M0', 'M1', 'M2']))
                            expected = len(versions[customer])
                            if operation == 'stale':
                                with self.assertRaises(ValueError): current.save_profile(customer, wanted, expected_version=expected-1)
                            else:
                                saved = current.save_profile(customer, wanted, expected_version=expected)
                                versions[customer].append(wanted)
                                self.assertEqual(saved['version'], len(versions[customer]))
                        elif operation == 'confirm':
                            confirm(customer, f'{customer}-confirm-{step}', rng.choice(COPIES))
                        elif operation == 'retry':
                            project = rng.choice(list(receipts))
                            context, request, saved = receipts[project]
                            current, _ = bound(context['customer_key'], project)
                            self.assertEqual(current.confirm_roles(context, dict(reversed(list(request.items())))), saved)
                        elif operation == 'reuse':
                            project = f'{customer}-reuse-{step}'
                            choice = rng.choice([row for row in COPIES if row['layer'] == versions[customer][-1]['module']['layer']])
                            current, context = bound(customer, project)
                            result = current.apply_profile(context, choice)
                            pins[project] = (customer, len(versions[customer]), deepcopy(choice))
                            self.assertEqual(result['applied_profile']['version'], len(versions[customer]))
                        else:
                            project = rng.choice(list(pins)); owner, version, choice = pins[project]
                            if operation == 'foreign':
                                current, context = bound('b' if owner == 'a' else 'a', project)
                                with self.assertRaises(ValueError): current.load_workflow(context)
                            else:
                                current, context = bound(owner, project)
                                self.assertEqual(current.load_workflow(context)['application_record']['version'], version)
                                self.assertEqual(current.apply_profile(context, choice)['resolved_roles'], versions[owner][version-1])
                        audit()
