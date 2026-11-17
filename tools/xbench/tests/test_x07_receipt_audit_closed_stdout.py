from contextlib import closing
from pathlib import Path
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from ._x07_receipt_audit_fixtures import audit, seed


class ReceiptAuditClosedStdout(unittest.TestCase):
    def test_closed_reader_clean_findings_and_unavailable_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, journal, read, receipts, pins = seed(directory)
            missing = Path(directory) / 'absent.sqlite'
            for outcome in ['clean', 'findings', 'unavailable']:
                with self.subTest(outcome=outcome):
                    if outcome == 'findings':
                        with closing(sqlite3.connect(journal)) as connection, connection:
                            connection.execute("UPDATE confirmations SET result='PRIVATE_MALFORMED' WHERE project_key='old-a'")
                    if outcome != 'unavailable':
                        self.assertEqual(audit.audit_receipts(read, receipts, customer_key='a')['healthy'], outcome == 'clean')
                    before = profile.read_bytes(), journal.read_bytes()
                    reader, writer = os.pipe()
                    os.close(reader)
                    try:
                        child = subprocess.Popen([sys.executable, audit.__file__, '--database', str(profile),
                                                  '--journal', str(missing if outcome == 'unavailable' else journal), '--customer', 'a'],
                                                 stdout=writer, stderr=subprocess.PIPE)
                    finally:
                        os.close(writer)
                    with child:
                        try:
                            _, errors = child.communicate(timeout=10)
                        finally:
                            if child.poll() is None:
                                child.kill()
                                child.communicate(timeout=5)
                        self.assertEqual(child.returncode, 2)
                        self.assertEqual(errors, b'')
                    self.assertFalse(missing.exists())
                    for key, pin in pins.items():
                        self.assertEqual(read.application_record('old-' + key, key), pin)
                    self.assertEqual((profile.read_bytes(), journal.read_bytes()), before)
