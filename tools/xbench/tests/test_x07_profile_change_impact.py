from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import x07_profile_change_impact as impact


class ProfileChangeImpact(unittest.TestCase):
    def test_preview_matches_independent_maps_and_retains_scoped_pins(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'store.sqlite'; store=impact.CustomerProfileStore(path)
            maps=[{'module':{'block':'A','layer':'M'},'roof':{'layer':'R'}},
                  {'module':{'block':'B','layer':'M'},'obstruction':{'layer':'O'}},
                  {'module':{'block':'B','layer':'M'},'obstruction':{'layer':'O'}}]
            pins=[]
            for index,roles in enumerate(maps):
                saved=store.save_version('a',roles,expected_version=index)
                pins.append(store.apply_to_project(f'p{index+1}','a'))
            foreign=store.save_version('b',{'module':{'block':'PRIVATE_FOREIGN','layer':'B'}},expected_version=0)
            old_b=store.apply_to_project('foreign','b')
            read=impact.CustomerProfileStore(path,read_only=True); before=path.read_bytes()
            for left,right in [(1,2),(2,1),(2,3),(1,1)]:
                with self.subTest(left=left,right=right):
                    a,b=maps[left-1],maps[right-1]
                    result=impact.preview_change(read,customer_key='a',profile_id=saved['profile_id'],before_version=left,after_version=right)
                    self.assertEqual(result['added'],{k:b[k] for k in b if k not in a})
                    self.assertEqual(result['removed'],{k:a[k] for k in a if k not in b})
                    self.assertEqual(result['changed'],{k:{'before':a[k],'after':b[k]} for k in a if k in b and a[k]!=b[k]})
                    expected=[]
                    for index,roles in enumerate(maps):
                        different=sorted(k for k in set(roles)|set(b) if roles.get(k)!=b.get(k))
                        if different: expected.append({'project_key':f'p{index+1}','version':index+1,'different_roles':different})
                    self.assertEqual(result['retaining_different_roles'],expected)
                    self.assertNotIn('PRIVATE',json.dumps(result))
            with self.assertRaises(ValueError): impact.preview_change(read,customer_key='a',profile_id=foreign['profile_id'],before_version=1,after_version=1)
            child=subprocess.run([sys.executable,impact.__file__,'--database',str(path),'--customer','a','--profile-id',foreign['profile_id'],'--before','1','--after','1'],capture_output=True,text=True,timeout=10)
            self.assertEqual(child.returncode,2); self.assertEqual(child.stderr,'')
            self.assertNotIn('PRIVATE',child.stdout); self.assertNotIn(foreign['profile_id'],child.stdout)
            for index,pin in enumerate(pins): self.assertEqual(read.application_record(f'p{index+1}','a'),pin)
            self.assertEqual(read.application_record('foreign','b'),old_b)
            self.assertEqual(path.read_bytes(),before)
