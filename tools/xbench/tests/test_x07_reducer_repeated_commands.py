from pathlib import Path
import importlib.util
import itertools
import unittest

spec = importlib.util.spec_from_file_location('repeated_reducer', Path(__file__).resolve().parents[1] / 'x07_history_reducer.py')
reducer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reducer)


def oracle(rows):
    opens = 0
    for command in rows:
        if command == 'open':
            opens += 1
        elif command == 'commit' and opens >= 2:
            return 'TWO_OPENS_THEN_COMMIT'
    return None


class ReducerRepeatedCommands(unittest.TestCase):
    def verify(self, history, budget=128):
        original = history[:]
        result = reducer.reduce_history(history, oracle, max_calls=budget)
        self.assertEqual(history, original)
        self.assertEqual(result['signature'], oracle(original))
        self.assertEqual(oracle(result['commands']), oracle(original))
        cursor = 0
        for command in result['commands']:
            cursor = original.index(command, cursor) + 1
        if result['complete'] and result['signature'] is not None:
            for index in range(len(result['commands'])):
                self.assertNotEqual(oracle(result['commands'][:index] + result['commands'][index+1:]), result['signature'])
        return result

    def test_exhaustive_duplicate_occurrences_and_order(self):
        for size in range(7):
            for values in itertools.product(('open', 'noise', 'commit'), repeat=size):
                with self.subTest(history=values):
                    self.assertTrue(self.verify(list(values))['complete'])

    def test_exhausted_budget_preserves_valid_occurrences(self):
        for budget in [1, 2]:
            result = self.verify(['open', 'noise', 'open', 'open', 'commit', 'commit'], budget)
            self.assertFalse(result['complete'])
            self.assertTrue(result['budget_exhausted'])
            self.assertEqual(result['oracle_calls'], budget)
