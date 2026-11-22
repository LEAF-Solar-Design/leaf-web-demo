"""Bounded synthetic EXPLAIN evidence for scoped X07 reads, no live database."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'server'))
from x07_customer_profile_store import CustomerProfileStore


def inspect(customers=12,versions=3):
    with tempfile.TemporaryDirectory(prefix='x07-query-plan-') as directory:
        store=CustomerProfileStore(Path(directory)/'store.sqlite')
        for index in range(customers):
            for version in range(versions):
                store.save_version('c'+str(index),{'module':{'block':'Panel '+str(index),'layer':'M'+str(version+1)}},expected_version=version)
                if version==0:store.apply_to_project('p'+str(index),'c'+str(index))
        queries=[('latest','SELECT * FROM profile_versions WHERE customer_key=? ORDER BY version DESC LIMIT 1',('c5',)),
                 ('pin','''SELECT a.project_key,v.* FROM project_applications a JOIN profile_versions v
                     ON a.customer_key=v.customer_key AND a.version=v.version WHERE a.project_key=?''',('p5',))]
        plans=[]
        with store._connection() as db:
            for name,sql,args in queries:
                details=[row[3] for row in db.execute('EXPLAIN QUERY PLAN '+sql,args)]
                assert details and all('SEARCH' in detail and 'INDEX' in detail for detail in details)
                assert not any('SCAN' in detail or 'TEMP B-TREE' in detail for detail in details)
                plans.append({'read':name,'details':details[:4],'indexed_search':True})
        latest=store.latest('c5');pin=store.application_record('p5','c5')
        assert latest['version']==versions and latest['roles']['module']=={'block':'Panel 5','layer':'M'+str(versions)}
        assert pin['version']==1 and pin['roles']['module']=={'block':'Panel 5','layer':'M1'}
        return {'schema':'leaf.x07.synthetic-query-plan.v1','proof_mode':'synthetic_offline','customers':customers,
                'versions':versions,'plans':plans,'independent_results_match':True,'latency_claim':False}
def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--out',type=Path);args=parser.parse_args()
    encoded=json.dumps(inspect(),indent=2)+'\n'
    if args.out:args.out.write_text(encoded,encoding='utf-8')
    print(encoded,end='')
if __name__=='__main__':main()
