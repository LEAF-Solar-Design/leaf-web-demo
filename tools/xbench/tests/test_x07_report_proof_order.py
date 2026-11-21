from itertools import permutations
from pathlib import Path
import json
import tempfile
import unittest
from ._x07_report_fixtures import fixtures


class ReportProofOrder(unittest.TestCase):
    def test_proof_and_digest_order_preserve_decisions_and_bad_digest_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, _, proof = fixtures.OfflineAcceptanceReport().fixtures(root)
            proofs = [root / f'proof-{number}.json' for number in range(3)]
            baseline = None
            for reverse in [False, True]:
                for number, path in enumerate(proofs):
                    data = dict(proof)
                    entries = list(proof['files'].items())
                    if (number % 2 == 1) != reverse: entries.reverse()
                    data['files'] = dict(entries)
                    path.write_text(json.dumps(data), encoding='utf-8')
                before = [path.read_bytes() for path in [paths[0], *proofs]]
                for order in permutations(proofs):
                    with self.subTest(reverse=reverse, order=[p.name for p in order]):
                        result = fixtures.report.aggregate(paths[0], order, fixtures.SOURCE, root)
                        if baseline is None: baseline = result['steps']
                        self.assertEqual(result['steps'], baseline)
                        self.assertEqual({row['state'] for row in result['steps']}, {'SYNTHETIC_PASS'})
                        self.assertEqual(sorted(row['receipt'] for row in result['local_proofs']), sorted(p.name for p in proofs))
                self.assertEqual([path.read_bytes() for path in [paths[0], *proofs]], before)
                bad = json.loads(proofs[1].read_text())
                bad['files']['server/fixture.py'] = '0' * 64
                proofs[1].write_text(json.dumps(bad), encoding='utf-8')
                before = [path.read_bytes() for path in [paths[0], *proofs]]
                for order in permutations(proofs):
                    with self.subTest(bad_order=[p.name for p in order]):
                        with self.assertRaisesRegex(fixtures.report.EvidenceError, '^SOURCE_DIGEST_MISMATCH$'):
                            fixtures.report.aggregate(paths[0], order, fixtures.SOURCE, root)
                self.assertEqual([path.read_bytes() for path in [paths[0], *proofs]], before)
