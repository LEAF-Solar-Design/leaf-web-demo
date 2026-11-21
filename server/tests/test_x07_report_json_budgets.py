from pathlib import Path
import json
import tempfile
import unittest
import test_x07_offline_acceptance_report as fixtures
import test_x07_report_cli_integrity as cli


def nodes(value):
    pending = [value]
    count = 0
    while pending:
        child = pending.pop()
        count += 1
        if isinstance(child, dict):
            pending.extend(child.keys())
            pending.extend(child.values())
        elif isinstance(child, list): pending.extend(child)
    return count


class ReportJsonBudgets(unittest.TestCase):
    def test_depth_below_at_above_limits_and_very_deep_input(self):
        for arrays, limit, accepted in [(7, 9, True), (8, 9, True), (9, 9, False), (1200, 32, False)]:
            with self.subTest(arrays=arrays, limit=limit), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                paths, journey, _ = fixtures.OfflineAcceptanceReport().fixtures(root)
                raw = json.dumps(journey)[:-1] + ',"extra":' + '[' * arrays + '0' + ']' * arrays + '}'
                paths[0].write_text(raw, encoding='utf-8')
                before = [path.read_bytes() for path in paths]
                code, result = cli.ReportCliIntegrity().call(root, paths, ['--max-json-depth', str(limit)])
                self.assertEqual(code, 0 if accepted else 2)
                if not accepted: self.assertEqual(result['error'], 'JSON_STRUCTURE_BUDGET_EXCEEDED')
                self.assertEqual([path.read_bytes() for path in paths], before)

    def test_node_budget_exact_and_one_over_with_nested_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, journey, _ = fixtures.OfflineAcceptanceReport().fixtures(root)
            nested = list(range(1000))
            for _ in range(8): nested = [nested]
            journey['extra'] = nested
            paths[0].write_text(json.dumps(journey), encoding='utf-8')
            before = [path.read_bytes() for path in paths]
            exact = nodes(journey)
            for limit, expected in [(exact + 1, 0), (exact, 0), (exact - 1, 2)]:
                code, result = cli.ReportCliIntegrity().call(root, paths, ['--max-json-nodes', str(limit)])
                self.assertEqual(code, expected)
                if expected: self.assertEqual({step['state'] for step in result['steps']}, {'UNREADABLE'})
            self.assertEqual([path.read_bytes() for path in paths], before)
