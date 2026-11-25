from pathlib import Path
from queue import Queue
import shutil
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
import x07_jsx_failure_capsule as capsule


@unittest.skipUnless(shutil.which('node'), 'node unavailable')
class CapsulePublicationCrash(unittest.TestCase):
    def test_terminated_writer_leaves_rejected_prefix_and_fresh_path_recovers(self):
        script = '''
import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import x07_jsx_failure_capsule as c
root=Path(sys.argv[2]);output=root/'partial.json';original=Path.open
class Interrupted:
 def __init__(self,stream):self.stream=stream
 def __getattr__(self,name):return getattr(self.stream,name)
 def write(self,value):
  assert '"owned_processes_settled": true' in value
  self.stream.write(value[:20]);self.stream.flush()
  print('PUBLICATION_HALF',flush=True);sys.stdin.readline()
  raise RuntimeError('parent must terminate this writer')
def opened(path,*args,**kwargs):
 stream=original(path,*args,**kwargs)
 return Interrupted(stream) if path==output and args and args[0]=='x' else stream
Path.open=opened
c.run_check('x07_crash.cjs',output,checks_root=root,timeout=5)
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            check = root / 'x07_crash.cjs'; check.write_text("console.log('DONE')\n", encoding='utf-8')
            foreign = root / 'foreign.json'; foreign.write_bytes(b'FOREIGN_CAPTURE')
            child = subprocess.Popen([sys.executable, '-P', '-u', '-c', script, str(Path(capsule.__file__).parent), directory], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            lines = Queue(); reader = Thread(target=lambda: lines.put(child.stdout.readline().strip()), daemon=True); reader.start()
            try:
                self.assertEqual(lines.get(timeout=15), 'PUBLICATION_HALF')
                child.kill(); child.communicate(timeout=5)
            finally:
                if child.poll() is None: child.kill()
                child.communicate(timeout=5); reader.join(5)
                child.stdin.close(); child.stdout.close(); child.stderr.close()
            self.assertFalse(reader.is_alive())
            partial = root / 'partial.json'; before = partial.read_bytes()
            self.assertEqual(len(before), 20)
            with self.assertRaises(ValueError): capsule.read_capture(partial)
            with self.assertRaises(FileExistsError): capsule.run_check(check.name, partial, checks_root=root)
            self.assertEqual(partial.read_bytes(), before)
            self.assertEqual(foreign.read_bytes(), b'FOREIGN_CAPTURE')
            report = capsule.run_check(check.name, root / 'fresh.json', checks_root=root, timeout=5)
            self.assertEqual(capsule.exit_code(capsule.read_capture(root / 'fresh.json')), 0)
            self.assertTrue(report['owned_processes_settled'])
