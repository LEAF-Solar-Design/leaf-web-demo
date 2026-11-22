from pathlib import Path
import importlib.util
import json
import hashlib
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('x07_report', Path(__file__).resolve().parents[2] / 'tools/xbench/x07_offline_acceptance_report.py')
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)
SOURCE = 'a' * 40


class OfflineAcceptanceReport(unittest.TestCase):
    def fixtures(self, root):
        (root / 'server').mkdir()
        (root / 'server/fixture.py').write_bytes(b'synthetic source\n')
        digest = hashlib.sha256(b'synthetic source\n').hexdigest()
        files = {'server/fixture.py': digest}
        for name in report.REQUIRED_SOURCE_FILES:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'synthetic dependency\n')
            files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        journey = {'schema': 'leaf.x07.synthetic-journey.v1', 'proof_mode': 'synthetic_offline', 'source_sha': SOURCE,
            'source_dirty': False, 'files': files,
            'steps': [{'n': number, 'passed': True, 'assertions': {name: True for name in report.REQUIRED_ASSERTIONS[number]}} for number in range(1, 8)]}
        proof = {'schema': 'leaf.x07.offline-proof.v1', 'local_source': SOURCE, 'files': dict(files)}
        paths = [root / 'journey.json', root / 'proof.json']
        for path, value in zip(paths, [journey, proof]):
            path.write_text(json.dumps(value), encoding='utf-8')
        return paths, journey, proof

    def test_seven_source_bound_steps_are_explicitly_synthetic(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, _, _ = self.fixtures(Path(directory))
            result = report.aggregate(paths[0], [paths[1]], SOURCE, Path(directory))
            self.assertEqual(result['proof_mode'], 'synthetic_offline')
            self.assertEqual([step['n'] for step in result['steps']], list(range(1, 8)))
            self.assertEqual({step['state'] for step in result['steps']}, {'SYNTHETIC_PASS'})
            self.assertEqual(result['local_proofs'][0]['file_digests'], len(report.REQUIRED_SOURCE_FILES) + 1)

    def test_missing_malformed_and_mismatched_receipts_refuse(self):
        for fault in ['missing', 'malformed', 'foreign_source', 'missing_source', 'bad_digest', 'missing_step']:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                paths, journey, proof = self.fixtures(Path(directory))
                if fault == 'missing':
                    paths[1].unlink()
                elif fault == 'malformed':
                    paths[1].write_text('{invalid', encoding='utf-8')
                else:
                    if fault == 'foreign_source': proof['local_source'] = 'c' * 40
                    if fault == 'missing_source': journey.pop('source_sha')
                    if fault == 'bad_digest': proof['files']['server/fixture.py'] = 'invalid'
                    if fault == 'missing_step': journey['steps'].pop()
                    paths[0].write_text(json.dumps(journey), encoding='utf-8')
                    paths[1].write_text(json.dumps(proof), encoding='utf-8')
                with self.assertRaises(report.EvidenceError):
                    report.aggregate(paths[0], [paths[1]], SOURCE, Path(directory))

    def test_failed_assertion_stays_a_synthetic_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, journey, _ = self.fixtures(Path(directory))
            journey['steps'][3]['passed'] = False
            journey['steps'][3]['assertions']['both_counts_match'] = False
            paths[0].write_text(json.dumps(journey), encoding='utf-8')
            result = report.aggregate(paths[0], [paths[1]], SOURCE, Path(directory))
            self.assertEqual(result['steps'][3]['state'], 'SYNTHETIC_FAIL')

    def test_changed_missing_and_escaping_source_paths_refuse(self):
        for fault in ['changed', 'missing', 'escape']:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                paths, journey, _ = self.fixtures(root)
                if fault == 'changed': (root / 'server/fixture.py').write_bytes(b'changed bytes')
                elif fault == 'missing': (root / 'server/fixture.py').unlink()
                else:
                    journey['files']['../outside.py'] = 'b' * 64
                    paths[0].write_text(json.dumps(journey), encoding='utf-8')
                with self.assertRaises(report.EvidenceError):
                    report.aggregate(paths[0], [paths[1]], SOURCE, root)

    def test_omitted_and_empty_inventories_produce_unreadable(self):
        from contextlib import redirect_stdout
        from io import StringIO
        import sys
        from unittest.mock import patch
        for target in ['journey', 'proof']:
            for empty in [False, True]:
                with self.subTest(target=target, empty=empty), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    paths, journey, proof = self.fixtures(root)
                    value = journey if target == 'journey' else proof
                    if empty: value['files'] = {}
                    else: value['files'].pop('server/x07_role_selection.py')
                    for path, content in zip(paths, [journey, proof]):
                        path.write_text(json.dumps(content), encoding='utf-8')
                    output = StringIO()
                    args = ['report', '--source-sha', SOURCE, '--source-root', str(root),
                            '--journey', str(paths[0]), '--proof', str(paths[1])]
                    with patch.object(sys, 'argv', args), redirect_stdout(output):
                        self.assertEqual(report.main(), 2)
                    self.assertEqual({step['state'] for step in json.loads(output.getvalue())['steps']}, {'UNREADABLE'})
