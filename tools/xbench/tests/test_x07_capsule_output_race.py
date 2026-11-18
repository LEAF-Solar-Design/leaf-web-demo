import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch
import x07_jsx_failure_capsule as capsule


@unittest.skipUnless(shutil.which('node'), 'node unavailable')
class CapsuleOutputRace(unittest.TestCase):
    def test_exclusive_new_artifact_allows_one_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launches = root / 'launches'
            check = root / 'x07_race.cjs'
            check.write_text("require('node:fs').appendFileSync(" + json.dumps(str(launches)) + ",'ONE\\n');console.log('ONE_CAPTURE')\n", encoding='utf-8')
            output = root / 'winner.json'
            barrier = threading.Barrier(2)
            original = capsule.snapshot
            local = threading.local()
            reports, errors = [], []

            def snapshot(*args):
                value = original(*args)
                if not getattr(local, 'ready', False):
                    local.ready = True
                    barrier.wait(timeout=10)
                return value

            def capture():
                try:
                    reports.append(capsule.run_check(check.name, output, checks_root=root, timeout=5))
                except BaseException as error:
                    errors.append(error)

            threads = [threading.Thread(target=capture) for _ in range(2)]
            with patch.object(capsule, 'snapshot', side_effect=snapshot):
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(15)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(len(reports), 1)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], FileExistsError)
            self.assertEqual(reports[0]['stdout'], 'ONE_CAPTURE\n')
            self.assertTrue(reports[0]['source_unchanged'])
            self.assertEqual(json.loads(output.read_text(encoding='utf-8')), reports[0])
            self.assertEqual(launches.read_bytes(), b'ONE\n')
