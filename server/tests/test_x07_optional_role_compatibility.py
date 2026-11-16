from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore
from x07_profile_compatibility import check_compatibility


class OptionalRoleCompatibilityTests(unittest.TestCase):
    def check(self,change,reason,aliases=None):
        with tempfile.TemporaryDirectory() as directory:
            store=CustomerProfileStore(Path(directory)/'store.sqlite')
            roles={'module':{'block':'Panel','layer':'MODULES'},'roof':{'block':'*U1','layer':'ROOF'},'obstruction':{'layer':'OBST'}}
            profile=store.save_version('a',roles);pin=store.apply_to_project('old','a')
            intake={'inserts':[{'name':'Panel','layer':'MODULES','x':0,'y':0},{'name':'*U1','layer':'ROOF','x':0,'y':0}],
                    'polylines':[{'layer':'OBST'}]}
            change(intake);before=deepcopy(intake)
            result=check_compatibility(intake,profile,customer_key='a',block_aliases=aliases or {'*U1':'*U1'})
            self.assertEqual(result['reason'],reason);self.assertEqual(result['decision'],'apply' if reason is None else 'propose_fresh')
            self.assertEqual(intake,before);self.assertEqual(store.latest('a'),profile);self.assertEqual(store.application_record('old','a'),pin)
            return result
    def test_removed_roof_proposes_fresh(self):self.check(lambda d:d['inserts'].pop(),'ROLE_NOT_FOUND')
    def test_renamed_obstruction_layer_proposes_fresh(self):self.check(lambda d:d['polylines'][0].update(layer='RENAMED'),'ROLE_NOT_FOUND')
    def test_explicit_roof_block_alias_resolves_without_profile_edit(self):
        result=self.check(lambda d:d['inserts'][1].update(name='*U2'),None,{'*U1':'*U2'})
        self.assertEqual(result['roles']['roof']['block'],'*U2')
    def test_ambiguous_optional_alias_refuses(self):
        self.check(lambda d:d['inserts'].extend([{'name':'*U2','layer':'ROOF','x':1,'y':0}]),'AMBIGUOUS_ALIAS',{'*U1':['*U1','*U2']})
