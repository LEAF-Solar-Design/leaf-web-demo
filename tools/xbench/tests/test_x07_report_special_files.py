"""X07 receipt and source paths must be regular before any data read."""
from pathlib import Path
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

root = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('special_file_fixtures', root / 'server/tests/test_x07_offline_acceptance_report.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
report = fixtures.report


class ReportSpecialFiles(unittest.TestCase):
    def test_directory_receipts_and_sources_refuse_before_open(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with patch.object(report.os, 'open', side_effect=AssertionError('must not open directory')):
                with self.assertRaisesRegex(report.EvidenceError, 'NONREGULAR_RECEIPT'):
                    report.read_receipt(path)
                with self.assertRaisesRegex(report.EvidenceError, 'NONREGULAR_SOURCE_FILE'):
                    report.SourceBudget().digest(path)

    def test_injected_nonregular_descriptor_refuses_before_stream_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'regular.json'
            path.write_bytes(b'{}\n')
            for mode in (stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR):
                with self.subTest(mode=mode), patch.object(report.os, 'fstat', return_value=SimpleNamespace(st_mode=mode)), \
                     patch.object(report.os, 'fdopen', side_effect=AssertionError('must not create a reading stream')):
                    with self.assertRaisesRegex(report.EvidenceError, 'NONREGULAR_RECEIPT'):
                        report.read_receipt(path)
                    with self.assertRaisesRegex(report.EvidenceError, 'NONREGULAR_SOURCE_FILE'):
                        report.SourceBudget().digest(path)
            self.assertEqual(path.read_bytes(), b'{}\n')

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'real POSIX FIFO is exercised on the offhost verification seat')
    def test_real_fifo_cli_refuses_without_writer_or_blocking(self):
        for location in ('receipt', 'source'):
            with self.subTest(location=location), tempfile.TemporaryDirectory() as directory:
                source = Path(directory)
                paths, journey, _ = fixtures.OfflineAcceptanceReport().fixtures(source)
                if location == 'receipt':
                    target = paths[0]
                else:
                    target = source / next(iter(journey['files']))
                target.unlink()
                os.mkfifo(target)
                before = {path: path.read_bytes() for path in source.rglob('*') if path.is_file()}
                result = subprocess.run([sys.executable, str(root / 'tools/xbench/x07_offline_acceptance_report.py'),
                    '--source-sha', fixtures.SOURCE, '--source-root', str(source),
                    '--journey', str(paths[0]), '--proof', str(paths[1])], capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 2, result.stderr[:300])
                value = json.loads(result.stdout)
                self.assertEqual(value['error'], 'NONREGULAR_RECEIPT' if location == 'receipt' else 'NONREGULAR_SOURCE_FILE')
                self.assertEqual({step['state'] for step in value['steps']}, {'UNREADABLE'})
                self.assertTrue(all(path.read_bytes() == raw for path, raw in before.items()))
