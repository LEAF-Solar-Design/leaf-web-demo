import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import x07_jsx_failure_capsule as capsule


@unittest.skipUnless(shutil.which('node'), 'node unavailable on execution host')
class JsxFailureCapsule(unittest.TestCase):
    def test_assertion_failure_keeps_complete_source_bound_streams(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            check = root / 'x07_capsule_assert.cjs'
            check.write_text("console.log('SYNTHETIC_STDOUT');console.error('SYNTHETIC_STDERR');require('node:assert/strict').equal(1,2)\n", encoding='utf-8')
            output = root / 'new.json'
            report = capsule.run_check(check.name, output, checks_root=root, timeout=10)
            self.assertNotEqual(report['exit_code'], 0)
            self.assertFalse(report['timed_out'])
            self.assertTrue(report['diagnostics_complete'])
            self.assertIn('SYNTHETIC_STDOUT', report['stdout'])
            self.assertIn('SYNTHETIC_STDERR', report['stderr'])
            self.assertIn('AssertionError', report['stderr'])
            self.assertEqual(report['source_file_sha256'][str(check.resolve())], hashlib.sha256(check.read_bytes()).hexdigest())
            self.assertEqual(json.loads(output.read_text(encoding='utf-8')), report)
            before = output.read_bytes()
            with self.assertRaises(FileExistsError):
                capsule.run_check(check.name, output, checks_root=root)
            self.assertEqual(output.read_bytes(), before)

    def test_hung_parent_and_descendant_cleanup_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            check = root / 'x07_capsule_hung.cjs'
            check.write_text("const {spawn}=require('node:child_process');const c=spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{stdio:'inherit'});console.log('CHILD:'+c.pid);setInterval(()=>{},1000)\n", encoding='utf-8')
            report = capsule.run_check(check.name, root / 'hung.json', checks_root=root, timeout=1)
            self.assertTrue(report['timed_out'])
            self.assertNotEqual(report['exit_code'], 0)
            self.assertIn('CHILD:', report['stdout'])
            self.assertTrue(report['owned_processes_settled'])
