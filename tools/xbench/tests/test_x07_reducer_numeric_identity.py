from pathlib import Path
import importlib.util
import json
import struct
import unittest

spec = importlib.util.spec_from_file_location('numeric_reducer', Path(__file__).resolve().parents[1] / 'x07_history_reducer.py')
reducer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reducer)


def identity(value):
    return (type(value).__name__, struct.pack('!d', value).hex() if type(value) is float else str(value))


class ReducerNumericIdentity(unittest.TestCase):
    def test_oracle_distinct_json_values_remain_distinct(self):
        for left, right in [(True, 1), (1, 1.0), (-0.0, 0.0)]:
            for target, other in [(left, right), (right, left)]:
                with self.subTest(target=identity(target), other=identity(other)):
                    history = [other, 'noise', target]
                    original = json.dumps(history)
                    observed = set()
                    def oracle(rows):
                        values = {identity(value) for value in rows}
                        observed.update(values)
                        return 'TARGET' if identity(target) in values else 'OTHER' if identity(other) in values else None
                    result = reducer.reduce_history(history, oracle)
                    self.assertTrue(result['complete'])
                    self.assertEqual(result['signature'], 'TARGET')
                    self.assertEqual([identity(value) for value in result['commands']], [identity(target)])
                    self.assertIn(identity(other), observed)
                    self.assertNotEqual(oracle([]), result['signature'])
                    self.assertEqual(json.dumps(history), original)

    def test_nonfinite_values_refuse_before_oracle(self):
        for value in [float('nan'), float('inf'), -float('inf')]:
            calls = []
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'finite JSON'):
                reducer.reduce_history([value], lambda rows: calls.append(rows))
            self.assertEqual(calls, [])
