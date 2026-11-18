from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import x07_profile_history_audit as audit


class AuditSpecialFiles(unittest.TestCase):
    def test_nonregular_metadata_refuses_before_store_or_connection_open(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'PRIVATE.sqlite'
            store = audit.CustomerProfileStore(path)
            store.save_version('a', {'module':{'block':'A','layer':'M'}}, expected_version=0)
            old = store.apply_to_project('old','a')
            read = audit.CustomerProfileStore(path, read_only=True)
            before = path.read_bytes()
            for mode in [stat.S_IFIFO,stat.S_IFSOCK,stat.S_IFCHR,stat.S_IFDIR,stat.S_IFLNK]:
                with self.subTest(mode=mode), patch.object(Path,'lstat', return_value=SimpleNamespace(st_mode=mode)):
                    with patch.object(read,'_connection') as connect:
                        with self.assertRaises(ValueError): audit.audit_history(read,customer_key='a')
                        connect.assert_not_called()
                    output = io.StringIO()
                    with patch.object(sys,'argv',['audit','--database',str(path),'--customer','a']), patch.object(audit,'CustomerProfileStore') as constructor, redirect_stdout(output):
                        self.assertEqual(audit.main(),2)
                    constructor.assert_not_called()
                    self.assertEqual(json.loads(output.getvalue()), {'schema':'leaf.x07.profile-history-audit.v1','error':'AUDIT_UNAVAILABLE'})
                    self.assertNotIn('PRIVATE',output.getvalue())
            self.assertEqual(read.application_record('old','a'),old)
            self.assertEqual(path.read_bytes(),before)

    def test_actual_nonregular_cli_paths_return_bounded_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sentinel = root/'sentinel'; sentinel.write_bytes(b'keep')
            paths = [root]
            if hasattr(os,'mkfifo'):
                fifo = root/'PRIVATE.fifo'; os.mkfifo(fifo); paths.append(fifo)
            before = sorted(p.name for p in root.iterdir())
            for path in paths:
                child = subprocess.run([sys.executable,audit.__file__,'--database',str(path),'--customer','a'],capture_output=True,text=True,timeout=5)
                self.assertEqual(child.returncode,2,child.stderr); self.assertEqual(child.stderr,'')
                self.assertEqual(json.loads(child.stdout),{'schema':'leaf.x07.profile-history-audit.v1','error':'AUDIT_UNAVAILABLE'})
                self.assertLess(len(child.stdout),200); self.assertNotIn(str(path),child.stdout)
            self.assertEqual(sorted(p.name for p in root.iterdir()),before)
            self.assertEqual(sentinel.read_bytes(),b'keep')
