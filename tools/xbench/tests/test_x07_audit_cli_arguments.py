from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import x07_profile_history_audit as audit


class AuditCliArguments(unittest.TestCase):
    def test_missing_unknown_and_empty_scope_arguments_are_bounded_and_create_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'PRIVATE_PATH' / 'absent.sqlite'
            base = ['--database',str(path),'--customer','a']
            for args in [[],['--database'],['--customer'],['--database',str(path)],
                         [*base,'--customer'],[*base,'--PRIVATE_FLAG','PRIVATE_VALUE'],
                         ['--database',str(path),'--customer',''],['--database',str(path),'--customer',' ']]:
                with self.subTest(args=args):
                    child = subprocess.run([sys.executable,audit.__file__,*args],capture_output=True,text=True,timeout=10)
                    self.assertEqual(child.returncode,2)
                    self.assertEqual(child.stderr,'')
                    result = json.loads(child.stdout)
                    self.assertEqual(result['schema'],'leaf.x07.profile-history-audit.v1')
                    self.assertIn(result['error'],['INVALID_ARGUMENTS','AUDIT_UNAVAILABLE'])
                    self.assertLess(len(child.stdout),200); self.assertNotIn('PRIVATE',child.stdout)
                    self.assertFalse(path.exists()); self.assertFalse(path.parent.exists())

    def test_literal_metacharacter_paths_and_scope_use_argv_without_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit &;$(PRIVATE_SENTINEL)`' [literal].sqlite"
            scope = "customer &;$(literal)`'"
            store = audit.CustomerProfileStore(path)
            store.save_version(scope, {'module':{'block':'A','layer':'M'}},expected_version=0)
            old = store.apply_to_project('old',scope)
            before = path.read_bytes()
            child = subprocess.run([sys.executable,audit.__file__,'--database',str(path),'--customer',scope],capture_output=True,text=True,timeout=10)
            self.assertEqual(child.returncode,0,child.stderr)
            self.assertEqual(child.stderr,'')
            result = json.loads(child.stdout)
            self.assertEqual(result['customer_key'],scope); self.assertTrue(result['healthy'])
            self.assertEqual(store.application_record('old',scope),old)
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual(list(Path(directory).iterdir()),[path])
