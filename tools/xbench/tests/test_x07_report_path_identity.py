"""A receipt inventory uses canonical paths and one entry per physical file."""
from pathlib import Path
import json
import os
import tempfile
import unittest
import test_x07_offline_acceptance_report as fixtures
import test_x07_report_cli_integrity as cli


class ReportPathIdentity(unittest.TestCase):
    def test_dot_segments_separators_and_physical_case_aliases_refuse(self):
        for alias in ['server/./fixture.py', './server/fixture.py', 'server//fixture.py',
                      'server\\fixture.py', 'server/FIXTURE.py']:
            for conflicting in [False, True]:
                with self.subTest(alias=alias, conflicting=conflicting), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    paths, journey, _ = fixtures.OfflineAcceptanceReport().fixtures(root)
                    target = root / 'server/fixture.py'
                    if alias == 'server/FIXTURE.py':
                        other = root / alias
                        if not other.exists(): os.link(target, other)
                        self.assertTrue(other.samefile(target))
                    journey['files'][alias] = '0' * 64 if conflicting else journey['files']['server/fixture.py']
                    paths[0].write_text(json.dumps(journey), encoding='utf-8')
                    before = [path.read_bytes() for path in paths]
                    original = target.read_bytes()
                    code, value = cli.ReportCliIntegrity().call(root, paths)
                    self.assertEqual(code, 2)
                    expected = 'AMBIGUOUS_SOURCE_IDENTITY' if alias == 'server/FIXTURE.py' else 'SOURCE_PATH_NOT_CANONICAL'
                    self.assertEqual(value['error'], expected)
                    self.assertEqual({step['state'] for step in value['steps']}, {'UNREADABLE'})
                    self.assertEqual([path.read_bytes() for path in paths], before)
                    self.assertEqual(target.read_bytes(), original)

    def test_canonical_inventory_and_repeated_cross_receipt_dependencies_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, _, _ = fixtures.OfflineAcceptanceReport().fixtures(root)
            code, value = cli.ReportCliIntegrity().call(root, paths)
            self.assertEqual(code, 0)
            self.assertEqual({step['state'] for step in value['steps']}, {'SYNTHETIC_PASS'})
