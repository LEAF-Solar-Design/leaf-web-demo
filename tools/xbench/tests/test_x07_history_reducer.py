from pathlib import Path
import importlib.util
import copy
import json
import unittest

spec = importlib.util.spec_from_file_location('history_reducer', Path(__file__).resolve().parents[1] / 'x07_history_reducer.py')
reducer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reducer)


class HistoryReducer(unittest.TestCase):
    def test_preserves_signature_and_deterministic_minimal_replay(self):
        commands = [{'op': op} for op in ['noise', 'open', 'noise', 'write', 'other_failure', 'close', 'noise']]
        before = copy.deepcopy(commands)
        def oracle(rows):
            ops = [row['op'] for row in rows]
            if 'open' in ops and 'write' in ops and ops.index('open') < ops.index('write'):
                return 'COMMIT_REPLY_GAP'
            return 'OTHER_FAILURE' if 'other_failure' in ops else None
        first = reducer.reduce_history(commands, oracle)
        second = reducer.reduce_history(commands, oracle)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(first['signature'], 'COMMIT_REPLY_GAP')
        self.assertEqual(first['commands'], [{'op': 'open'}, {'op': 'write'}])
        self.assertTrue(first['complete'])
        self.assertEqual(commands, before)
        self.assertEqual(oracle(first['commands']), first['signature'])

    def test_budget_and_mutating_oracle_leave_last_known_failure(self):
        commands = [{'op': 'failure'}] * 64
        before = copy.deepcopy(commands)
        count = []
        def oracle(rows):
            count.append(True); rows.append({'op': 'private-oracle-mutation'})
            return 'STABLE_FAILURE'
        result = reducer.reduce_history(commands, oracle, max_calls=3)
        self.assertEqual(result['oracle_calls'], 3)
        self.assertEqual(len(count), 3)
        self.assertTrue(result['budget_exhausted'])
        self.assertEqual(result['signature'], 'STABLE_FAILURE')
        self.assertEqual(commands, before)
        self.assertTrue(all(row == {'op': 'failure'} for row in result['commands']))

    def test_passing_history_and_invalid_budgets_or_signatures(self):
        self.assertTrue(reducer.reduce_history([], lambda rows: None)['complete'])
        for budget in [0, 4097, True]:
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                reducer.reduce_history([], lambda rows: None, max_calls=budget)
        for signature in [{}, '', 'x' * 201]:
            with self.subTest(signature=repr(signature)[:20]), self.assertRaises(ValueError):
                reducer.reduce_history([], lambda rows: signature)
