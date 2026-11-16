from pathlib import Path
from threading import Event, Thread
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
import test_x07_offline_acceptance_report as fixtures
import test_x07_report_cli_integrity as cli


class ReportLinkRetarget(unittest.TestCase):
    def test_retarget_between_verification_passes_refuses_changed_and_escaping_targets(self):
        for escape in [False, True]:
            with self.subTest(escape=escape), tempfile.TemporaryDirectory() as directory:
                envelope = Path(directory)
                root = envelope / 'source'; root.mkdir()
                paths, journey, _ = fixtures.OfflineAcceptanceReport().fixtures(root)
                initial = root / 'one'; initial.mkdir()
                target = (envelope / 'external') if escape else (root / 'two'); target.mkdir()
                (initial / 'extra.py').write_bytes(b'original bytes\n')
                (target / 'extra.py').write_bytes(b'changed synthetic bytes\n')
                link = root / 'linked'; link.symlink_to(initial, target_is_directory=True)
                journey['files']['linked/extra.py'] = hashlib.sha256((initial / 'extra.py').read_bytes()).hexdigest()
                paths[0].write_text(json.dumps(journey), encoding='utf-8')
                before = [p.read_bytes() for p in paths]
                verified, retargeted = Event(), Event()
                failures = []
                def retarget():
                    try:
                        if not verified.wait(10): raise AssertionError('first verification never arrived')
                        link.unlink(); link.symlink_to(target, target_is_directory=True)
                    except BaseException as error: failures.append(error)
                    finally: retargeted.set()
                worker = Thread(target=retarget, daemon=True); worker.start()
                digest = fixtures.report.SourceBudget.digest
                def synchronized(budget, name):
                    result = digest(budget, name)
                    if name == initial / 'extra.py' and not verified.is_set():
                        verified.set()
                        self.assertTrue(retargeted.wait(10))
                    return result
                try:
                    with patch.object(fixtures.report.SourceBudget, 'digest', synchronized):
                        code, value = cli.ReportCliIntegrity().call(root, paths)
                    self.assertEqual(code, 2)
                    self.assertEqual(value['error'], 'SOURCE_PATH_ESCAPE' if escape else 'SOURCE_CHANGED_DURING_AGGREGATION')
                    self.assertEqual({s['state'] for s in value['steps']}, {'UNREADABLE'})
                    self.assertEqual([p.read_bytes() for p in paths], before)
                    self.assertEqual((initial / 'extra.py').read_bytes(), b'original bytes\n')
                    self.assertEqual((target / 'extra.py').read_bytes(), b'changed synthetic bytes\n')
                finally:
                    verified.set(); worker.join(timeout=11)
                self.assertFalse(worker.is_alive()); self.assertEqual(failures, [])
