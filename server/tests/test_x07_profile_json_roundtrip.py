"""Payload transport proof only; no live route or customer data."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore
from x07_profile_workflow import ProfileWorkflow
from x07_profile_compatibility import check_compatibility


def roundtrip(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


class ProfileRoundtripTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'profiles.sqlite'
        self.store = CustomerProfileStore(self.path)
        self.roles = {'module': {'block': 'Panneau 雪', 'layer': 'Módules Ø'}}
        self.intake = {'inserts': [{'name': 'Panneau 雪', 'layer': 'Módules Ø', 'x': 0, 'y': 0}]}

    def test_unicode_scope_and_compatible_payload_roundtrip(self):
        profile = self.store.save_version('client-é', self.roles, expected_version=0)
        payload = check_compatibility(roundtrip(self.intake), roundtrip(profile), customer_key='client-é')
        self.assertEqual(payload, roundtrip(payload))
        self.assertEqual(payload['roles'], self.roles)
        self.assertEqual(payload['applied_profile']['customer_key'], 'client-é')

    def test_foreign_scope_cannot_be_restored_from_json(self):
        profile = roundtrip(self.store.save_version('a', self.roles))
        result = roundtrip(check_compatibility(self.intake, profile, customer_key='b'))
        self.assertEqual(result['decision'], 'propose_fresh')
        self.assertIsNone(result['applied_profile'])
        self.assertNotIn(profile['profile_id'], json.dumps(result))

    def test_old_pin_survives_transport_reopen_and_new_version(self):
        service = ProfileWorkflow(self.store)
        first = service.confirm(self.intake, customer_key='a', project_key='p1', drawing_key='d1:v1',
                                roles=roundtrip(self.roles), window=[0, 0, 0, 0], expected_version=0)
        self.store.save_version('a', self.roles, expected_version=1)
        reopened = CustomerProfileStore(self.path)
        self.assertEqual(roundtrip(first['applied_profile']), reopened.application_record('p1', 'a'))
        self.assertEqual(first, roundtrip(first))
        self.assertEqual(first['applied_profile']['version'], 1)
        self.assertEqual(reopened.latest('a')['version'], 2)

    def test_anonymous_alias_remains_explicit_after_roundtrip(self):
        roles = {'module': {'block': '*U1', 'layer': 'Módules Ø'}}
        profile = roundtrip(self.store.save_version('a', roles))
        intake = roundtrip({'inserts': [{**self.intake['inserts'][0], 'name': '*U2'}]})
        self.assertEqual(check_compatibility(intake, profile, customer_key='a')['reason'], 'ANONYMOUS_ALIAS_REQUIRED')
        applied = roundtrip(check_compatibility(intake, profile, customer_key='a', block_aliases=roundtrip({'*U1': ['*U2']})))
        self.assertEqual(applied['roles']['module']['block'], '*U2')
        self.assertEqual(profile['roles'], roles)
