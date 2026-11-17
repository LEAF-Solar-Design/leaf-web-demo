from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore, ProfileScopeError
from x07_profile_workflow import ProfileWorkflow
from x07_workflow_adapter import WorkflowAdapter


class ScopeKeyCollisionTests(unittest.TestCase):
    def test_delimiters_unicode_internal_spaces_and_prefixes_remain_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CustomerProfileStore(Path(directory)/'store.sqlite')
            keys = ['a', 'aa', 'a:b', 'a|b', 'a/b', 'a b', 'é', 'e\u0301', 'A', 'a::b']
            ids = set()
            for key in keys:
                context = {'customer_key': key, 'project_key': 'project:'+key, 'drawing_key': 'drawing|'+key+':v1'}
                choice = {'block': 'Panel', 'layer': 'MODULES', 'design_window': [0,0,0,0], 'oracle_count': 1}
                adapter = WorkflowAdapter(ProfileWorkflow(store), context=context,
                    intake={'inserts':[{'name':'Panel','layer':'MODULES','x':0,'y':0}]}, copies=[choice])
                result = adapter.confirm_roles(context, {**choice, 'expected_version':0})
                ids.add(result['applied_profile']['profile_id'])
                for other in keys:
                    if other != key:
                        with self.assertRaises(ProfileScopeError): store.application_record(context['project_key'], other)
            self.assertEqual(len(ids), len(keys))
            self.assertNotEqual(store.latest('é')['profile_id'], store.latest('e\u0301')['profile_id'])

    def test_rejected_whitespace_empty_and_oversize_keys_do_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'store.sqlite'; store=CustomerProfileStore(path)
            for key in ['', ' a', 'a ', '\ta', 'a\n', 'x'*201, None, True]:
                for operation in [lambda: store.save_version(key,{}), lambda:store.apply_to_project(key,'valid'),
                                  lambda:ProfileWorkflow(store).inspect({'inserts':[]}, customer_key='valid',project_key='valid',drawing_key=key)]:
                    with self.subTest(key=key), self.assertRaises(ValueError): operation()
            with store._connection() as db:
                for table in ['customers','profile_versions','project_applications']:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
