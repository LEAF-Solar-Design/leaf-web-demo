from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import x07_profile_change_impact as impact


def typed(value):
    if isinstance(value,dict): return ('object',tuple((k,typed(v)) for k,v in sorted(value.items())))
    if isinstance(value,list): return ('array',tuple(map(typed,value)))
    return (type(value).__name__,value)


class ImpactPinValueOracle(unittest.TestCase):
    def test_pins_compare_values_not_only_keys_or_python_numeric_equality(self):
        maps=[{'module':{'block':'A','layer':'M','x':1},'roof':{'layer':'R'}},
              {'module':{'block':'A','layer':'M','x':True},'roof':{'layer':'R'}},
              {'module':{'block':'EDIT','layer':'M','x':True},'roof':{'layer':'R'}},
              {'module':{'block':'EDIT','layer':'M','x':True}}]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'store.sqlite';store=impact.CustomerProfileStore(path);pins=[]
            for index,roles in enumerate(maps):
                saved=store.save_version('a',roles,expected_version=index)
                pins.append(store.apply_to_project(f'p{index+1}','a'))
            store.save_version('b',{'module':{'block':'PRIVATE_B','layer':'B'}},expected_version=0)
            foreign=store.apply_to_project('foreign','b')
            read=impact.CustomerProfileStore(path,read_only=True);before=path.read_bytes()
            for left,right in [(1,2),(2,3),(3,4),(4,1),(2,2)]:
                with self.subTest(left=left,right=right):
                    result=impact.preview_change(read,customer_key='a',profile_id=saved['profile_id'],before_version=left,after_version=right)
                    a,b=maps[left-1],maps[right-1]
                    self.assertEqual(set(result['changed']),{k for k in a.keys()&b.keys() if typed(a[k])!=typed(b[k])})
                    self.assertEqual(result['removed'],{k:a[k] for k in a.keys()-b.keys()})
                    self.assertEqual(result['added'],{k:b[k] for k in b.keys()-a.keys()})
                    expected=[]
                    for index,pin in enumerate(pins):
                        values=pin['roles']
                        different=sorted(k for k in values.keys()|b.keys() if k not in values or k not in b or typed(values[k])!=typed(b[k]))
                        if different: expected.append({'project_key':f'p{index+1}','version':index+1,'different_roles':different})
                    self.assertEqual(result['retaining_different_roles'],expected)
            for index,pin in enumerate(pins): self.assertEqual(read.application_record(f'p{index+1}','a'),pin)
            self.assertEqual(read.application_record('foreign','b'),foreign)
            self.assertEqual(path.read_bytes(),before)
