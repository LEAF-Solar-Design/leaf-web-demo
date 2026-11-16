from pathlib import Path
import json
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'x07_history_reducer.py'
WORKER = r'''
import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('reducer',sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
script,history,output,ready,failure=sys.argv[1:]
replace=m.os.replace
def synchronized(source,target):
    Path(ready).write_text(str(source))
    if sys.stdin.buffer.read(1)!=b'g':
        raise OSError('barrier cancelled')
    if failure=='fail':
        raise OSError('PRIVATE_REPLACE_FAILURE')
    return replace(source,target)
m.os.replace=synchronized
sys.argv=[script,'--history',history,'--out',output]
raise SystemExit(m.main())
'''


class ReducerConcurrentPublish(unittest.TestCase):
    def test_synchronized_writers_and_failed_replace_clean_only_owned_temps(self):
        for first_fails in [False, True]:
            with self.subTest(first_fails=first_fails), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                output = root / 'output.json'
                output.write_bytes(b'prior output')
                foreign = root / '.x07-reducer-foreign.tmp'
                foreign.write_bytes(b'foreign owner')
                children, originals, histories = [], [], []
                try:
                    for index in range(2):
                        history, ready = root / f'history{index}.json', root / f'ready{index}'
                        payload = json.dumps([{'op': 'synthetic_failure', 'writer': index}]).encode()
                        history.write_bytes(payload)
                        histories.append(history); originals.append(payload)
                        children.append(subprocess.Popen([sys.executable, '-c', WORKER, str(SCRIPT), str(history), str(output), str(ready),
                                                          'fail' if index == 0 and first_fails else 'ok'],
                                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
                    deadline = time.monotonic() + 10
                    while not all((root / f'ready{index}').exists() for index in range(2)):
                        if time.monotonic() > deadline or any(child.poll() is not None for child in children):
                            self.fail('writer failed to reach bounded barrier')
                        time.sleep(0.01)
                    owned = [Path((root / f'ready{index}').read_text()) for index in range(2)]
                    self.assertNotEqual(*owned)
                    for path in owned:
                        self.assertTrue(json.loads(path.read_bytes())['complete'])
                    self.assertEqual(output.read_bytes(), b'prior output')
                    for index, child in enumerate(children):
                        text, error = child.communicate(input=b'g', timeout=10)
                        self.assertEqual(error, b'')
                        self.assertEqual(child.returncode, 2 if index == 0 and first_fails else 0)
                        if index == 0:
                            self.assertTrue(owned[1].exists())
                            self.assertFalse(owned[0].exists())
                            if first_fails:
                                self.assertEqual(json.loads(text)['error'], 'OUTPUT_PUBLICATION_FAILED')
                                self.assertEqual(output.read_bytes(), b'prior output')
                        self.assertNotIn(b'PRIVATE', text)
                    replay = json.loads(output.read_bytes())
                    self.assertTrue(replay['complete'])
                    self.assertEqual(replay['commands'], [{'op': 'synthetic_failure', 'writer': 1}])
                    self.assertEqual(replay['signature'], 'SYNTHETIC_FAILURE')
                    self.assertEqual([path.read_bytes() for path in histories], originals)
                    self.assertEqual(foreign.read_bytes(), b'foreign owner')
                    self.assertEqual(list(root.glob('.x07-reducer-*.tmp')), [foreign])
                finally:
                    for child in children:
                        if child.poll() is None:
                            child.kill()
                        child.communicate(timeout=5)
