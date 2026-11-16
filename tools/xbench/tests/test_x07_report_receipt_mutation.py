from pathlib import Path
from threading import Event, Thread
import json
import tempfile
import unittest
from unittest.mock import patch
import test_x07_offline_acceptance_report as fixtures
import test_x07_report_cli_integrity as cli


class ReportReceiptMutation(unittest.TestCase):
    def test_atomic_journey_or_proof_replacement_refuses_mixed_snapshot(self):
        for index in [0, 1]:
            with self.subTest(receipt=index), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                paths, journey, proof = fixtures.OfflineAcceptanceReport().fixtures(root)
                source = {name: (root / name).read_bytes() for name in journey['files']}
                replacement = dict(journey if index == 0 else proof, fixture_revision=2)
                encoded = (json.dumps(replacement) + '\n').encode()
                reached, replaced = Event(), Event()
                failures = []
                def replace():
                    try:
                        if not reached.wait(10): raise AssertionError('receipt was never read')
                        staged = root / 'replacement.json'
                        staged.write_bytes(encoded); staged.replace(paths[index])
                    except BaseException as error: failures.append(error)
                    finally: replaced.set()
                worker = Thread(target=replace, daemon=True); worker.start()
                original = fixtures.report.read_receipt
                def synchronized(path, **options):
                    result = original(path, **options)
                    if Path(path) == paths[index] and not reached.is_set():
                        reached.set(); self.assertTrue(replaced.wait(10))
                    return result
                try:
                    with patch.object(fixtures.report, 'read_receipt', synchronized):
                        code, value = cli.ReportCliIntegrity().call(root, paths)
                    self.assertEqual(code, 2)
                    self.assertEqual(value['error'], 'RECEIPT_CHANGED_DURING_AGGREGATION')
                    self.assertEqual({s['state'] for s in value['steps']}, {'UNREADABLE'})
                    self.assertEqual(paths[index].read_bytes(), encoded)
                    self.assertEqual({name: (root / name).read_bytes() for name in source}, source)
                finally:
                    reached.set(); worker.join(timeout=11)
                self.assertFalse(worker.is_alive()); self.assertEqual(failures, [])
