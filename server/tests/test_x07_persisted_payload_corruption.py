from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore,ProfileScopeError


class PersistedCorruptionTests(unittest.TestCase):
    def test_supported_schema_invalid_payloads_refuse_reads_without_rewrite(self):
        for encoded in ['{broken','null','[]','9','true','{}','{"module":null}',
                        '{"module":{"layer":9}}','{"module":{"layer":""}}',
                        '{"module":{"layer":"M","block":false}}','{"module":{"layer":"M"},"other":{}}']:
            with self.subTest(encoded=encoded),tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'store.sqlite';store=CustomerProfileStore(path)
                # Insert malformed producer data without changing schema/triggers.
                with store._connection() as db:
                    db.execute("INSERT INTO customers VALUES ('a','profile-a')")
                    db.execute("INSERT INTO profile_versions VALUES ('a','profile-a',1,?,'fixture')",(encoded,))
                    db.execute("INSERT INTO project_applications VALUES ('p','a','profile-a',1)")
                before=path.read_bytes();reopened=CustomerProfileStore(path)
                for operation in [lambda:reopened.latest('a'),lambda:reopened.application_record('p','a'),lambda:reopened.apply_to_project('p','a')]:
                    with self.assertRaisesRegex(ValueError,'stored profile roles are invalid'):operation()
                    self.assertEqual(path.read_bytes(),before)
                with self.assertRaises(ProfileScopeError):reopened.application_record('p','b')
                self.assertIsNone(reopened.latest('b'));self.assertEqual(path.read_bytes(),before)
