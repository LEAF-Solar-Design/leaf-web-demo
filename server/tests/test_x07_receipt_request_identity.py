"""Member order is irrelevant. Explicit label/version/snapshot values are identity."""
from pathlib import Path
import tempfile
import unittest
from test_x07_receipt_retry_races import adapter, CONTEXT, COPIES
from x07_customer_profile_store import CustomerProfileStore


class ReceiptRequestIdentity(unittest.TestCase):
    def test_reordered_fields_replay_changed_values_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = adapter(root)
            token = current.load_workflow(CONTEXT)['plan_token']
            request = {**COPIES[0], 'expected_version': 0, 'plan_token': token}
            first = current.confirm_roles(CONTEXT, request)
            reordered = dict(reversed(list(request.items())))
            reversed_context = dict(reversed(list(CONTEXT.items())))
            self.assertEqual(adapter(root).confirm_roles(reversed_context, reordered), first)
            for changed in [{**request, 'block': 'PANEL'}, {**request, 'layer': 'm'},
                            {**request, 'expected_version': 1}, {**request, 'plan_token': 'changed-revision'}]:
                with self.subTest(fields=list(changed)):
                    with self.assertRaises(ValueError):
                        adapter(root).confirm_roles(CONTEXT, changed)
            store = CustomerProfileStore(root / 'profiles.sqlite')
            with store._connection() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM profile_versions').fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM project_applications').fetchone()[0], 1)
            self.assertEqual(store.application_record('p', 'a'), first['applied_profile'])
