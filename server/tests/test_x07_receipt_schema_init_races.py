from pathlib import Path
from queue import Queue
import json
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from test_x07_receipt_retry_races import adapter, CONTEXT, COPIES


class ReceiptSchemaInitRaces(unittest.TestCase):
    def test_independent_process_initializers_and_restart_replay(self):
        script = '''
import json,sys,sqlite3
from contextlib import closing
sys.path.insert(0,sys.argv[2])
from x07_workflow_adapter import ConfirmationReceiptStore
print('READY',flush=True)
sys.stdin.readline()
ConfirmationReceiptStore(sys.argv[1])
with closing(sqlite3.connect(sys.argv[1])) as db:
    result=[db.execute('PRAGMA user_version').fetchone()[0],db.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0]]
print(json.dumps(result),flush=True)
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            children = []
            readers = []
            ready = Queue()
            try:
                for _ in range(2):
                    child = subprocess.Popen([sys.executable, '-u', '-c', script, str(root / 'receipts.sqlite'),
                        str(Path(__file__).resolve().parents[1])], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True)
                    children.append(child)
                    reader = Thread(target=lambda stream=child.stdout: ready.put(stream.readline()), daemon=True)
                    reader.start()
                    readers.append(reader)
                for _ in children:
                    self.assertEqual(ready.get(timeout=15).strip(), 'READY')
                for child in children:
                    child.stdin.write('GO\n')
                    child.stdin.flush()
                for child in children:
                    output, error = child.communicate(timeout=15)
                    self.assertEqual(child.returncode, 0, error[:300])
                    self.assertEqual(json.loads(output), [1, 0])
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=10)
                for reader in readers:
                    reader.join(timeout=2)
            request = {**COPIES[0], 'expected_version': 0}
            first = adapter(root).confirm_roles(CONTEXT, request)
            self.assertEqual(adapter(root).confirm_roles(CONTEXT, request), first)
