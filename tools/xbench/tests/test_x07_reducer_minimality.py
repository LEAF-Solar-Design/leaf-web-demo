from itertools import product
from pathlib import Path
import importlib.util
import unittest

spec = importlib.util.spec_from_file_location('minimal_reducer', Path(__file__).resolve().parents[1] / 'x07_history_reducer.py')
reducer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reducer)


def oracle(history):
    for index, value in enumerate(history):
        if value == 'open' and 'commit' in history[index + 1:]:
            return 'ORDERED_FAILURE'
    return 'OTHER_FAILURE' if 'other' in history else None


class ReducerMinimality(unittest.TestCase):
    def test_exhaustive_completed_histories_are_independently_one_minimal(self):
        for size in range(6):
            for values in product(['noise', 'open', 'commit', 'other'], repeat=size):
                history = list(values)
                signature = oracle(history)
                result = reducer.reduce_history(history, oracle, max_calls=128)
                self.assertTrue(result['complete'], values)
                self.assertFalse(result['budget_exhausted'])
                self.assertEqual(result['signature'], signature)
                self.assertEqual(oracle(result['commands']), signature)
                if signature is not None:
                    for index in range(len(result['commands'])):
                        removed = result['commands'][:index] + result['commands'][index + 1:]
                        self.assertNotEqual(oracle(removed), signature, (values, result))
                self.assertEqual(history, list(values))

    def test_low_budgets_preserve_signature_without_false_minimality(self):
        history = ['open', 'noise', 'noise', 'commit', 'other']
        for budget in [1, 2, 3]:
            result = reducer.reduce_history(history, oracle, max_calls=budget)
            self.assertLessEqual(result['oracle_calls'], budget)
            self.assertFalse(result['complete'])
            self.assertTrue(result['budget_exhausted'])
            self.assertEqual(oracle(result['commands']), 'ORDERED_FAILURE')
