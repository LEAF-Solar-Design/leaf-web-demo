from pathlib import Path
import copy
import importlib.util
import json
import unittest

spec = importlib.util.spec_from_file_location('mutation_reducer', Path(__file__).resolve().parents[1] / 'x07_history_reducer.py')
reducer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reducer)


class ReducerOracleMutation(unittest.TestCase):
    def history(self):
        return [{'op': op, 'nested': {'tags': [{'value': op}], 'numbers': [1, 2]}}
                for op in ['noise', 'failure', 'other']]

    def signature(self, rows):
        return 'NESTED_FAILURE' if any(row['op'] == 'failure' for row in rows) else None

    def test_nested_oracle_edits_cannot_change_later_candidates_or_replay(self):
        history = self.history()
        before = json.dumps(history, sort_keys=True)
        pure = reducer.reduce_history(history, self.signature)
        seen = []
        def mutate(rows):
            seen.append(copy.deepcopy(rows))
            answer = self.signature(rows)
            for row in rows:
                self.assertEqual(row['nested'], {'tags': [{'value': row['op']}], 'numbers': [1, 2]})
                row['nested']['tags'][0]['value'] = 'PRIVATE_MUTATION'
                row['nested']['numbers'].clear()
                row['op'] = 'PRIVATE_MUTATION'
            rows.append({'op': 'PRIVATE_MUTATION'})
            return answer
        changed = reducer.reduce_history(history, mutate)
        self.assertGreater(len(seen), 2)
        self.assertEqual(json.dumps(changed, sort_keys=True), json.dumps(pure, sort_keys=True))
        self.assertEqual(json.dumps(history, sort_keys=True), before)
        self.assertNotIn('PRIVATE_MUTATION', json.dumps(changed))

    def test_nested_mutation_before_exception_leaves_caller_and_next_run_intact(self):
        history = self.history()
        before = json.dumps(history, sort_keys=True)
        expected = reducer.reduce_history(history, self.signature)
        calls = []
        def mutate_then_raise(rows):
            calls.append(copy.deepcopy(rows))
            answer = self.signature(rows)
            for row in rows: row['nested']['tags'].append({'value': 'PRIVATE_MUTATION'})
            if len(calls) == 2: raise RuntimeError('PRIVATE_MUTATION')
            return answer
        with self.assertRaises(reducer.HistoryOracleError) as raised:
            reducer.reduce_history(history, mutate_then_raise)
        self.assertEqual(raised.exception.diagnostic['error'], 'ORACLE_EXCEPTION')
        self.assertEqual(raised.exception.diagnostic['oracle_calls'], 2)
        self.assertNotIn('PRIVATE_MUTATION', json.dumps(raised.exception.diagnostic))
        self.assertEqual(json.dumps(history, sort_keys=True), before)
        self.assertEqual(reducer.reduce_history(history, self.signature), expected)
        self.assertNotIn('PRIVATE_MUTATION', json.dumps(calls))
