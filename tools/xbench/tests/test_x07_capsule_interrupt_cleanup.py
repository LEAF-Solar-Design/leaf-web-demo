from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch
import x07_jsx_failure_capsule as capsule


@unittest.skipUnless(shutil.which('node'), 'node unavailable')
class CapsuleInterruptCleanup(unittest.TestCase):
    def test_execution_cancellation_settles_before_removing_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            check = root / 'x07_cancel.cjs'; check.write_text("console.log('READY');setInterval(()=>{},1000)\n", encoding='utf-8')
            cancel = threading.Event()
            with self.assertRaises(capsule.CaptureCancelled):
                capsule.run_check(check.name, root / 'cancelled.json', checks_root=root, timeout=5,
                                  cancel_event=cancel, on_chunk=lambda label, chunk: cancel.set())
            self.assertEqual(list(root.iterdir()), [check])

    def test_publication_interrupt_leaves_no_partial_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            check = root / 'x07_interrupt.cjs'; check.write_text("console.log('DONE')\n", encoding='utf-8')
            output = root / 'interrupted.json'
            original = Path.open

            class Interrupted:
                def __init__(self, stream): self.stream = stream
                def __getattr__(self, name): return getattr(self.stream, name)
                def write(self, value):
                    self_test.assertIn('"owned_processes_settled": true', value)
                    self.stream.write(value[:12])
                    raise KeyboardInterrupt()

            self_test = self
            def opened(path, *args, **kwargs):
                stream = original(path, *args, **kwargs)
                return Interrupted(stream) if path == output and args and args[0] == 'x' else stream

            with patch.object(Path, 'open', opened), self.assertRaises(capsule.CaptureCancelled):
                capsule.run_check(check.name, output, checks_root=root, timeout=5)
            self.assertFalse(output.exists())
            report = capsule.run_check(check.name, root / 'retry.json', checks_root=root, timeout=5)
            self.assertEqual(capsule.exit_code(report), 0)
