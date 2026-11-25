from pathlib import Path
import shutil
import tempfile
import threading
import unittest
import x07_jsx_failure_capsule as capsule


@unittest.skipUnless(shutil.which('node'), 'node unavailable')
class CapsuleSourceRevalidation(unittest.TestCase):
    def test_dependency_edit_during_check_refuses_mixed_attestation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dependency, release = root / 'dependency.js', root / 'release'
            dependency.write_text('before', encoding='utf-8')
            check = root / 'x07_source_barrier.cjs'
            import json
            check.write_text("const fs=require('node:fs');console.log('READY');const t=setInterval(()=>{if(fs.existsSync(" + json.dumps(str(release)) + ")){clearInterval(t)}},10)\n", encoding='utf-8')
            ready = threading.Event()
            errors = []

            def edit():
                try:
                    if not ready.wait(10):
                        raise AssertionError('output barrier timeout')
                    dependency.write_text('after', encoding='utf-8')
                    release.touch()
                except BaseException as error:
                    errors.append(error)

            thread = threading.Thread(target=edit)
            thread.start()
            try:
                report = capsule.run_check(check.name, root / 'changed.json', checks_root=root,
                                           source_dependencies=[dependency], timeout=10,
                                           on_chunk=lambda label, chunk: ready.set() if label == 'stdout' and b'READY' in chunk else None)
            finally:
                ready.set(); thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(report['exit_code'], 0)
            self.assertEqual(report['error'], 'SOURCE_CHANGED')
            self.assertFalse(report['source_unchanged'])
            self.assertNotIn('source_file_sha256', report)
            self.assertNotEqual(report['source_before']['source_file_sha256'], report['source_after']['source_file_sha256'])
            self.assertEqual(capsule.exit_code(report), 2)
            retry = capsule.run_check(check.name, root / 'retry.json', checks_root=root, source_dependencies=[dependency], timeout=10)
            self.assertTrue(retry['source_unchanged'])
            self.assertEqual(capsule.exit_code(retry), 0)
