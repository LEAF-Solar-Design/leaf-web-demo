"""Crash recovery: wait for the owned child to exit, verify its exact temp path
and bytes, remove only that abandoned file, then explicitly rerun publication.
Never glob-delete foreign temporary files or infer a successful replacement.
"""
from pathlib import Path
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'x07_history_reducer.py'
spec = importlib.util.spec_from_file_location('crash_reducer', SCRIPT)
reducer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reducer)
WORKER = r'''
import importlib.util,sys,json,hashlib
from pathlib import Path
spec=importlib.util.spec_from_file_location('reducer',sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
script,history,output,ready=sys.argv[1:]
def stopped(source,target):
    path=Path(source)
    Path(ready).write_text(json.dumps({'temporary':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}))
    sys.stdin.buffer.read(1)
    raise OSError('fixture unexpectedly released')
m.os.replace=stopped
sys.argv=[script,'--history',history,'--out',output]
raise SystemExit(m.main())
'''


class ReducerPublishCrash(unittest.TestCase):
    def test_killed_writer_preserves_prior_replay_and_scoped_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history, output, ready = root / 'history.json', root / 'output.json', root / 'ready.json'
            payload = b'[{"op":"synthetic_failure","writer":"new"}]'
            history.write_bytes(payload)
            prior = json.dumps(reducer.reduce_history([{'op': 'synthetic_failure', 'writer': 'old'}], lambda rows: 'SYNTHETIC_FAILURE' if rows else None)).encode()
            output.write_bytes(prior)
            foreign = root / '.x07-reducer-foreign.tmp'
            foreign.write_bytes(b'foreign owner')
            child = subprocess.Popen([sys.executable, '-c', WORKER, str(SCRIPT), str(history), str(output), str(ready)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 10
                while not ready.exists():
                    if time.monotonic() > deadline or child.poll() is not None: self.fail('writer did not reach pre-replace barrier')
                    time.sleep(0.01)
                record = json.loads(ready.read_text())
                abandoned = Path(record['temporary'])
                self.assertEqual(abandoned.resolve().parent, root.resolve())
                self.assertTrue(abandoned.name.startswith('.x07-reducer-'))
                self.assertEqual(hashlib.sha256(abandoned.read_bytes()).hexdigest(), record['sha256'])
                self.assertTrue(json.loads(abandoned.read_bytes())['complete'])
                child.kill()
                child.communicate(timeout=5)
                self.assertIsNotNone(child.returncode)
                self.assertNotEqual(child.returncode, 0)
                self.assertEqual(output.read_bytes(), prior)
                self.assertEqual(history.read_bytes(), payload)
                self.assertTrue(abandoned.exists())
                self.assertEqual(hashlib.sha256(abandoned.read_bytes()).hexdigest(), record['sha256'])
                abandoned.unlink()
                recovered = subprocess.run([sys.executable, str(SCRIPT), '--history', str(history), '--out', str(output)], capture_output=True, timeout=10)
                self.assertEqual(recovered.returncode, 0, recovered.stderr)
                self.assertEqual(recovered.stderr, b'')
                self.assertEqual(json.loads(output.read_bytes())['commands'], [{'op': 'synthetic_failure', 'writer': 'new'}])
                self.assertEqual(history.read_bytes(), payload)
                self.assertEqual(foreign.read_bytes(), b'foreign owner')
                self.assertEqual(list(root.glob('.x07-reducer-*.tmp')), [foreign])
            finally:
                if child.poll() is None: child.kill()
                child.communicate(timeout=5)
