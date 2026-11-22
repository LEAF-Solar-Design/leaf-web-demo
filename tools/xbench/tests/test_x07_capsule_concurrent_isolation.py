import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
import x07_jsx_failure_capsule as capsule
from .test_x07_capsule_early_parent_exit import running


@unittest.skipUnless(shutil.which('node'), 'node unavailable')
class CapsuleConcurrentIsolation(unittest.TestCase):
    def test_cancellation_kills_only_its_own_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / 'release-b'
            for name in ('a', 'b'):
                body = "setInterval(()=>{},1000)" if name == 'a' else "const t=setInterval(()=>{if(require('node:fs').existsSync(" + json.dumps(str(release)) + ")){console.log('SURVIVED');clearInterval(t)}},10)"
                (root / ('x07_isolation_' + name + '.cjs')).write_text("require('node:fs').writeFileSync(" + json.dumps(str(root / (name + '.pid'))) + ",String(process.pid));console.log('READY');" + body, encoding='utf-8')
            ready = {name: threading.Event() for name in ('a', 'b')}
            cancel = {name: threading.Event() for name in ('a', 'b')}
            reports, errors = {}, {}

            def capture(name):
                try:
                    reports[name] = capsule.run_check('x07_isolation_' + name + '.cjs', root / (name + '.json'), checks_root=root,
                                                      timeout=10, cancel_event=cancel[name], on_chunk=lambda label, chunk: ready[name].set())
                except BaseException as error:
                    errors[name] = error

            threads = {name: threading.Thread(target=capture, args=(name,)) for name in ('a', 'b')}
            try:
                for thread in threads.values(): thread.start()
                self.assertTrue(all(event.wait(5) for event in ready.values()))
                cancel['a'].set(); threads['a'].join(5)
                self.assertFalse(threads['a'].is_alive())
                self.assertIsInstance(errors.get('a'), capsule.CaptureCancelled)
                self.assertFalse(running(int((root / 'a.pid').read_text())))
                self.assertTrue(running(int((root / 'b.pid').read_text())), 'other capture was killed')
                self.assertFalse((root / 'a.json').exists())
                release.touch(); threads['b'].join(5)
                self.assertFalse(threads['b'].is_alive())
                self.assertNotIn('b', errors)
                self.assertEqual(capsule.exit_code(reports['b']), 0)
                self.assertIn('SURVIVED', reports['b']['stdout'])
                self.assertEqual(capsule.read_capture(root / 'b.json'), reports['b'])
                self.assertFalse(running(int((root / 'b.pid').read_text())))
            finally:
                for event in cancel.values(): event.set()
                release.touch()
                for thread in threads.values(): thread.join(10)
            self.assertTrue(all(not thread.is_alive() for thread in threads.values()))
