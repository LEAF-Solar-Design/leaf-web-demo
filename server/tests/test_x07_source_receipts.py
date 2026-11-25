from pathlib import Path
import hashlib
import importlib.util
import json
import tempfile
import unittest
from test_x07_offline_acceptance_report import report

spec = importlib.util.spec_from_file_location('x07_journey', Path(__file__).resolve().parents[2] / 'tools/xbench/x07_synthetic_journey.py')
journey = importlib.util.module_from_spec(spec)
spec.loader.exec_module(journey)


class SourceReceipts(unittest.TestCase):
    def test_fresh_disposable_journey_measures_source_and_satisfies_report(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            result = journey.run(temporary / 'journey.sqlite')
            self.assertRegex(result['source_sha'], '^[0-9a-f]{40}$')
            self.assertIs(type(result['source_dirty']), bool)
            for name, digest in result['files'].items():
                self.assertEqual(digest, hashlib.sha256((root / name).read_bytes()).hexdigest())
            journey_path = temporary / 'journey.json'
            proof_path = temporary / 'proof.json'
            journey_path.write_text(json.dumps(result), encoding='utf-8')
            proof_path.write_text(json.dumps({'schema': 'leaf.x07.offline-proof.v1',
                'local_source': result['source_sha'], 'files': result['files']}), encoding='utf-8')
            accepted = report.aggregate(journey_path, [proof_path], result['source_sha'], root)
            self.assertEqual({step['state'] for step in accepted['steps']}, {'SYNTHETIC_PASS'})
            self.assertEqual(accepted['source_dirty'], result['source_dirty'])

    def test_legacy_receipt_is_not_rewritten_or_attested(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / 'old.json'
            old.write_text('{"schema":"leaf.x07.synthetic-journey.v1"}', encoding='utf-8')
            before = old.read_bytes()
            with self.assertRaisesRegex(report.EvidenceError, 'MISSING_SOURCE_BINDING'):
                report.aggregate(old, [root / 'proof.json'], 'a' * 40, root)
            self.assertEqual(old.read_bytes(), before)
