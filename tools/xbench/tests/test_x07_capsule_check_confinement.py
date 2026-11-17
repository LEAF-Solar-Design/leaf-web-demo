import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import x07_jsx_failure_capsule as capsule


@unittest.skipUnless(shutil.which('node'), 'node unavailable')
class CapsuleConfinement(unittest.TestCase):
    def test_path_and_link_escapes_refuse_before_spawn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); checks = root / 'checks'; checks.mkdir()
            sentinel = root / 'x07_sentinel.cjs'; sentinel.write_bytes(b'UNCHANGED_SENTINEL')
            hard = checks / 'x07_hard.cjs'; os.link(sentinel, hard)
            names = ['../x07_sentinel.cjs', '..\\x07_sentinel.cjs', str(sentinel.resolve()), 'C:\\outside\\x07_sentinel.cjs', hard.name]
            for name in names:
                with self.subTest(name=name), patch.object(capsule.subprocess, 'Popen', side_effect=AssertionError('spawn before confinement')):
                    with self.assertRaises(ValueError):
                        capsule.run_check(name, root / 'refused.json', checks_root=checks)
                self.assertFalse((root / 'refused.json').exists())
            local = checks / 'x07_local.cjs'; local.write_text("console.log('LOCAL')\n", encoding='utf-8')
            real = Path.is_symlink
            with patch.object(Path, 'is_symlink', lambda path: True if path == local else real(path)), patch.object(capsule.subprocess, 'Popen', side_effect=AssertionError('link spawned')):
                with self.assertRaises(ValueError):
                    capsule.run_check(local.name, root / 'refused.json', checks_root=checks)
            report = capsule.run_check(local.name, root / 'valid.json', checks_root=checks, timeout=5)
            self.assertEqual(capsule.exit_code(report), 0)
            self.assertEqual(sentinel.read_bytes(), b'UNCHANGED_SENTINEL')
