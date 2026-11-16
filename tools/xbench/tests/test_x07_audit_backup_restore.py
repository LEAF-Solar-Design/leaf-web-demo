from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from x07_profile_history_audit import CustomerProfileStore, audit_history


class AuditBackupRestore(unittest.TestCase):
    def test_settled_two_customer_backup_restore_then_restored_only_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source,backup,restored = root/'source.sqlite',root/'backup.sqlite',root/'restored.sqlite'
            store = CustomerProfileStore(source)
            profiles,old = {},{}
            for customer,count in [('a',3),('b',2)]:
                for version in range(count):
                    profiles[customer] = store.save_version(customer,{'module':{'block':customer+str(version),'layer':'M'}},expected_version=version)
                    if version==0: old[customer] = store.apply_to_project('old-'+customer,customer)
            store.apply_to_project('new-a','a')
            read = CustomerProfileStore(source,read_only=True)
            before = {customer:audit_history(read,customer_key=customer) for customer in ['a','b']}
            self.assertTrue(all(value['healthy'] for value in before.values()))
            self.assertEqual((before['a']['version_count'],before['a']['pin_count']),(3,2))
            self.assertEqual((before['b']['version_count'],before['b']['pin_count']),(2,1))
            source_bytes = source.read_bytes()
            def copy_database(source_path,target_path):
                with closing(sqlite3.connect(source_path.resolve().as_uri()+'?mode=ro',uri=True)) as src, closing(sqlite3.connect(target_path)) as dst:
                    src.backup(dst)
            copy_database(source,backup)
            backup_bytes = backup.read_bytes()
            copy_database(backup,restored)
            restored_read = CustomerProfileStore(restored,read_only=True)
            restored_bytes = restored.read_bytes()
            for customer in ['a','b']:
                self.assertEqual(audit_history(restored_read,customer_key=customer),before[customer])
                self.assertEqual(restored_read.application_record('old-'+customer,customer),old[customer])
            self.assertEqual(restored.read_bytes(),restored_bytes)
            with closing(sqlite3.connect(restored)) as db, db:
                db.execute('INSERT INTO profile_versions VALUES (?,?,?,?,?)',('a',profiles['a']['profile_id'],5,'{}','synthetic'))
            changed_bytes = restored.read_bytes()
            result = audit_history(restored_read,customer_key='a')
            self.assertFalse(result['healthy'])
            self.assertEqual(result['version_count'],4)
            self.assertEqual(result['issues']['version_gaps'],[{'first':4,'last':4}])
            self.assertEqual(result['issues']['dangling_pin_versions'],0)
            self.assertEqual(audit_history(restored_read,customer_key='b'),before['b'])
            for customer in ['a','b']:
                self.assertEqual(audit_history(read,customer_key=customer),before[customer])
                self.assertEqual(restored_read.application_record('old-'+customer,customer),old[customer])
            self.assertEqual(source.read_bytes(),source_bytes)
            self.assertEqual(backup.read_bytes(),backup_bytes)
            self.assertEqual(restored.read_bytes(),changed_bytes)
