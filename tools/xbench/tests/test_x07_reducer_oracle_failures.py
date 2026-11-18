from pathlib import Path
import importlib.util
import copy
import json
import unittest

spec = importlib.util.spec_from_file_location('oracle_failure_reducer', Path(__file__).resolve().parents[1] / 'x07_history_reducer.py')
reducer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reducer)


class ReducerOracleFailures(unittest.TestCase):
    def test_exceptions_and_invalid_signatures_are_safe_deterministic_failures(self):
        history = [{'op': 'fail'}, {'op': 'noise'}]
        before = copy.deepcopy(history)
        def raised(rows):
            rows.clear()
            raise RuntimeError('PRIVATE_ORACLE_PAYLOAD')
        for oracle, code in [(raised, 'ORACLE_EXCEPTION'), (lambda rows: {}, 'INVALID_ORACLE_SIGNATURE'),
                             (lambda rows: 'x' * 201, 'INVALID_ORACLE_SIGNATURE')]:
            diagnostics = []
            for repeat in range(2):
                with self.assertRaises(reducer.HistoryOracleError) as caught:
                    reducer.reduce_history(history, oracle, max_calls=8)
                diagnostics.append(json.dumps(caught.exception.diagnostic, sort_keys=True))
                self.assertEqual(caught.exception.diagnostic['error'], code)
                self.assertEqual(caught.exception.diagnostic['oracle_calls'], 1)
                self.assertNotIn('PRIVATE', str(caught.exception))
            self.assertEqual(diagnostics[0], diagnostics[1])
        self.assertEqual(history, before)

    def test_changing_signature_on_repeated_history_refuses_completion(self):
        history = ['a', 'b', 'c']
        before = history.copy()
        calls = []
        def changing(rows):
            calls.append(tuple(rows))
            return 'ORIGINAL' if len(calls) == 1 else 'CHANGED'
        with self.assertRaises(reducer.HistoryOracleError) as caught:
            reducer.reduce_history(history, changing, max_calls=32)
        self.assertEqual(caught.exception.diagnostic['error'], 'ORACLE_SIGNATURE_CHANGED')
        self.assertLessEqual(caught.exception.diagnostic['oracle_calls'], 32)
        self.assertEqual(history, before)
