from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import test_x07_offline_acceptance_report as fixtures
from test_x07_report_cli_integrity import strict_output


class ReportProofBudget(unittest.TestCase):
    def call(self, root, paths, count, budget=None):
        args = ['report', '--source-sha', fixtures.SOURCE, '--source-root', str(root), '--journey', str(paths[0])]
        for _ in range(count): args.extend(['--proof', str(paths[1])])
        if budget is not None: args.extend(['--max-proof-receipts', str(budget)])
        stdout = StringIO()
        with patch.object(sys, 'argv', args), redirect_stdout(stdout): code = fixtures.report.main()
        return code, strict_output(stdout.getvalue().encode())

    def test_exact_default_and_injected_proof_limits_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, _, _ = fixtures.OfflineAcceptanceReport().fixtures(root)
            before = [path.read_bytes() for path in paths]
            for count, budget in [(16, None), (1, 1), (2, 2)]:
                with self.subTest(count=count, budget=budget):
                    code, value = self.call(root, paths, count, budget)
                    self.assertEqual(code, 0)
                    self.assertEqual(len(value['local_proofs']), count)
            self.assertEqual([path.read_bytes() for path in paths], before)

    def test_excess_and_invalid_budgets_refuse_before_any_receipt_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, _, _ = fixtures.OfflineAcceptanceReport().fixtures(root)
            before = [path.read_bytes() for path in paths]
            for count, budget, error in [(17, None, 'MISSING_OR_EXCESS_PROOFS'),
                                         (3, 2, 'MISSING_OR_EXCESS_PROOFS'),
                                         (1, 0, 'INVALID_PROOF_BUDGET'), (1, 17, 'INVALID_PROOF_BUDGET')]:
                with self.subTest(count=count, budget=budget), patch.object(fixtures.report, 'read_receipt') as read:
                    code, value = self.call(root, paths, count, budget)
                    self.assertEqual(code, 2); self.assertEqual(value['error'], error)
                    self.assertEqual({step['state'] for step in value['steps']}, {'UNREADABLE'})
                    self.assertLess(len(str(value)), 1024)
                    read.assert_not_called()
            self.assertEqual([path.read_bytes() for path in paths], before)
