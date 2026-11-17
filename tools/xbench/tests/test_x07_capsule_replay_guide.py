import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch
import x07_jsx_failure_capsule as capsule


@unittest.skipUnless(shutil.which('node'), 'node unavailable')
class CapsuleReplayGuide(unittest.TestCase):
    def recipe(self, name, script, *, timeout=5, mutation=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            check = root / ('x07_recipe_' + name + '.cjs')
            check.write_text(script, encoding='utf-8')
            output = root / 'new.json'
            original = capsule.run_check
            started = threading.Event()
            errors = []

            def edit():
                try:
                    if not started.wait(5):
                        raise AssertionError('recipe barrier timeout')
                    check.write_text(script + '\n// changed dependency bytes\n', encoding='utf-8')
                except BaseException as error:
                    errors.append(error)

            writer = threading.Thread(target=edit) if mutation else None
            if writer:
                writer.start()

            def run(*args, **kwargs):
                return original(*args, **kwargs, checks_root=root,
                                on_chunk=lambda label, chunk: started.set() if label == 'stdout' else None)

            import sys
            from contextlib import redirect_stdout
            import io
            stdout = io.StringIO()
            try:
                argv = ['capsule', '--check', check.name, '--out', str(output), '--timeout', str(timeout)]
                with patch.object(capsule, 'run_check', side_effect=run), patch.object(sys, 'argv', argv), redirect_stdout(stdout):
                    code = capsule.main()
            finally:
                started.set()
                if writer:
                    writer.join(10)
                    self.assertFalse(writer.is_alive())
            self.assertEqual(errors, [])
            report = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(json.loads(stdout.getvalue())['source_unchanged'], not mutation)
            self.assertTrue(report['owned_processes_settled'])
            return code, report

    def test_assertion_recipe(self):
        code, report = self.recipe('assertion', "console.error('SYNTHETIC_ASSERTION');require('node:assert/strict').equal(1,2)")
        self.assertEqual(code, 1)
        self.assertIn('AssertionError', report['stderr'])
        self.assertTrue(report['diagnostics_complete'])

    def test_timeout_recipe(self):
        code, report = self.recipe('timeout', "console.log('SYNTHETIC_HUNG');setInterval(()=>{},1000)", timeout=.5)
        self.assertEqual(code, 1)
        self.assertTrue(report['timed_out'])

    def test_overflow_recipe(self):
        code, report = self.recipe('overflow', "process.stdout.write(Buffer.alloc(" + str(capsule.MAX_STREAM_BYTES + 1) + ",83))")
        self.assertEqual(code, 1)
        self.assertFalse(report['diagnostics_complete'])
        self.assertEqual(len(report['stdout'].encode()), capsule.MAX_STREAM_BYTES)

    def test_source_recipe(self):
        code, report = self.recipe('source', "console.log('SYNTHETIC_READY');setTimeout(()=>{},1000)", mutation=True)
        self.assertEqual(code, 2)
        self.assertEqual(report['error'], 'SOURCE_CHANGED')
        self.assertNotIn('source_file_sha256', report)
