from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest
import test_x07_offline_acceptance_report as fixtures


class ReportFailureTruthTable(unittest.TestCase):
    def test_all_128_valid_assertion_outcomes_match_independent_truth_table(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, template, _ = fixtures.OfflineAcceptanceReport().fixtures(root)
            for mask in range(128):
                with self.subTest(mask=mask):
                    journey = deepcopy(template)
                    expected = []
                    for index, step in enumerate(journey['steps']):
                        good = bool(mask & (1 << index))
                        expected.append('SYNTHETIC_PASS' if good else 'SYNTHETIC_FAIL')
                        step['passed'] = good
                        # One real required assertion controls each valid false
                        # step; the other assertions and all other steps remain.
                        step['assertions'][next(iter(step['assertions']))] = good
                    paths[0].write_text(json.dumps(journey), encoding='utf-8')
                    before = [path.read_bytes() for path in paths]
                    value = fixtures.report.aggregate(paths[0], [paths[1]], fixtures.SOURCE, root)
                    self.assertEqual([step['state'] for step in value['steps']], expected)
                    self.assertEqual([step['n'] for step in value['steps']], list(range(1, 8)))
                    self.assertEqual([path.read_bytes() for path in paths], before)
