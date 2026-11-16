from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from ._x07_receipt_audit_fixtures import audit,seed
from x07_profile_change_impact import preview_change


class ImpactForeignNoninterference(unittest.TestCase):
    def test_corrupt_overbudget_foreign_histories_cannot_change_target(self):
        with tempfile.TemporaryDirectory() as directory:
            profile,_,read,_,pins=seed(directory);store=audit.CustomerProfileStore(profile)
            saved=store.save_version('a',{'module':{'block':'EDIT','layer':'M'}},expected_version=1)
            new=store.apply_to_project('new-a','a')
            args={'customer_key':'a','profile_id':saved['profile_id'],'before_version':1,'after_version':2}
            baseline=preview_change(read,**args)
            self.assertEqual(baseline['retaining_different_roles'],[{'project_key':'old-a','version':1,'different_roles':['module']}])
            self.assertEqual(set(baseline['changed']),{'module'})
            with closing(sqlite3.connect(profile)) as db,db:
                db.executemany('INSERT INTO profile_versions VALUES (?,?,?,?,?)',(('b','PRIVATE_WRONG_PROFILE',v,'PRIVATE_MALFORMED','synthetic') for v in range(2,audit.MAX_AUDIT_ROWS+3)))
                db.executemany('INSERT INTO project_applications VALUES (?,?,?,?)',((f'foreign-{i}','b','PRIVATE_BAD_PIN','PRIVATE_VERSION') for i in range(audit.MAX_AUDIT_ROWS+1)))
            before=profile.read_bytes();actual=preview_change(read,**args)
            self.assertEqual(json.dumps(actual,sort_keys=True),json.dumps(baseline,sort_keys=True))
            self.assertNotIn('PRIVATE_WRONG',json.dumps(actual));self.assertNotIn('PRIVATE_BAD',json.dumps(actual))
            with self.assertRaisesRegex(ValueError,'row budget'):
                preview_change(read,customer_key='b',profile_id=pins['b']['profile_id'],before_version=1,after_version=1)
            for key,pin in pins.items():self.assertEqual(read.application_record('old-'+key,key),pin)
            self.assertEqual(read.application_record('new-a','a'),new)
            self.assertEqual(profile.read_bytes(),before)
