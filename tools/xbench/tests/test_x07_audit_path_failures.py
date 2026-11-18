from contextlib import redirect_stdout
from pathlib import Path
import io
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import x07_profile_history_audit as audit


class AuditPathFailures(unittest.TestCase):
    def test_absent_parent_directory_paths_refuse_without_creation_then_explicit_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root/'PRIVATE_DIRECTORY'; folder.mkdir()
            sentinel = folder/'owner.marker'; sentinel.write_bytes(b'owned sentinel')
            absent = root/'PRIVATE_ABSENT.sqlite'
            missing = root/'PRIVATE_PARENT'/'absent.sqlite'
            for path in [absent,missing,folder]:
                child = subprocess.run([sys.executable,audit.__file__,'--database',str(path),'--customer','a'],capture_output=True,text=True,timeout=10)
                self.assertEqual(child.returncode,2); self.assertEqual(child.stderr,'')
                self.assertEqual(json.loads(child.stdout),{'schema':'leaf.x07.profile-history-audit.v1','error':'AUDIT_UNAVAILABLE'})
                self.assertNotIn('PRIVATE',child.stdout); self.assertLess(len(child.stdout),200)
            self.assertFalse(absent.exists()); self.assertFalse(missing.parent.exists())
            self.assertEqual(sentinel.read_bytes(),b'owned sentinel')
            store = audit.CustomerProfileStore(absent)
            store.save_version('a',{'module':{'block':'A','layer':'M'}},expected_version=0)
            old = store.apply_to_project('old','a')
            before = absent.read_bytes()
            child = subprocess.run([sys.executable,audit.__file__,'--database',str(absent),'--customer','a'],capture_output=True,text=True,timeout=10)
            self.assertEqual(child.returncode,0,child.stderr)
            self.assertTrue(json.loads(child.stdout)['healthy'])
            self.assertEqual(store.application_record('old','a'),old)
            self.assertEqual(absent.read_bytes(),before)

    def test_permission_failure_is_bounded_then_removed_handler_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'PRIVATE_PATH.sqlite'
            store = audit.CustomerProfileStore(path)
            store.save_version('a',{'module':{'block':'A','layer':'M'}},expected_version=0)
            old = store.apply_to_project('old','a')
            before = path.read_bytes()
            args = ['audit','--database',str(path),'--customer','a']
            text = io.StringIO()
            with patch.object(sys,'argv',args),patch.object(audit,'CustomerProfileStore',side_effect=PermissionError('PRIVATE_PERMISSION')),redirect_stdout(text):
                self.assertEqual(audit.main(),2)
            self.assertEqual(json.loads(text.getvalue())['error'],'AUDIT_UNAVAILABLE')
            self.assertNotIn('PRIVATE',text.getvalue()); self.assertLess(len(text.getvalue()),200)
            recovered = io.StringIO()
            with patch.object(sys,'argv',args),redirect_stdout(recovered): self.assertEqual(audit.main(),0)
            self.assertTrue(json.loads(recovered.getvalue())['healthy'])
            self.assertEqual(store.application_record('old','a'),old)
            self.assertEqual(path.read_bytes(),before)
