"""Bounded offline measurement of receipt WAL growth and checkpoint recovery."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'server'))
from x07_customer_profile_store import CustomerProfileStore
from x07_profile_workflow import ProfileWorkflow
from x07_workflow_adapter import ConfirmationReceiptStore, WorkflowAdapter


def run(count):
    if type(count) is not int or not 1 <= count <= 256: raise ValueError('count must be1..256')
    with tempfile.TemporaryDirectory(prefix='x07-wal-growth-') as directory:
        root=Path(directory);profile=root/'profiles.sqlite';journal=root/'receipts.sqlite'
        store=CustomerProfileStore(profile);workflow=ProfileWorkflow(store);receipts=ConfirmationReceiptStore(journal)
        intake={'inserts':[dict(name='Panel',layer='M',x=x,y=0) for x in [0,1,2]]}
        copies=[dict(block='Panel',layer='M',design_window=[0,0,2,0],oracle_count=3)]
        original=receipts._connect
        def writer_connection():
            db=original();db.execute('PRAGMA wal_autocheckpoint=0');return db
        receipts._connect=writer_connection
        def bound(project, receipt_store=receipts):
            scope=dict(customer_key='a',project_key=project,drawing_key=project+':v1')
            return WorkflowAdapter(workflow,context=scope,intake=intake,copies=copies,
                confirmation_receipts=receipt_store),scope
        wal=Path(str(journal)+'-wal')
        size=lambda:wal.stat().st_size if wal.exists() else 0
        def rows():
            with closing(original()) as db:return db.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0]
        results=[]
        with closing(original()) as anchor:
            assert anchor.execute('PRAGMA journal_mode=WAL').fetchone()[0]=='wal'
            anchor.execute('BEGIN');assert anchor.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0]==0
            before=size();growth=[]
            for index in range(count):
                current,scope=bound('p'+str(index));request={**copies[0],'expected_version':index}
                result=current.confirm_roles(scope,request);results.append((scope,request,result))
                growth.append(size())
            after_new=size();assert after_new>before
            assert rows()==count
            for scope,request,result in results:
                current,_=bound(scope['project_key']);assert current.confirm_roles(scope,dict(request))==result
            after_replay=size();assert after_replay==after_new and rows()==count
            for index in range(count*2):
                current,scope=bound('abandoned-'+str(index));plan=current.load_workflow(scope)
                assert plan['application_record'] is None
            after_inspect=size();assert after_inspect==after_replay and rows()==count
            with store._connection() as db:
                assert db.execute('SELECT COUNT(*) FROM project_applications').fetchone()[0]==count
                assert db.execute('SELECT COUNT(*) FROM profile_versions').fetchone()[0]==count
            anchor.rollback()
            checkpoint=anchor.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            assert checkpoint[0]==0
            after_checkpoint=size();assert after_checkpoint==0
            readonly=ConfirmationReceiptStore(journal,read_only=True)
            for index,(scope,request,result) in enumerate(results):
                current,_=bound(scope['project_key'],readonly)
                assert current.confirm_roles(scope,request)==result
                assert store.application_record(scope['project_key'],'a')['version']==index+1
            assert rows()==count
        return {'schema':'leaf.x07.journal-wal-growth.v1','proof_mode':'synthetic_offline','state':'PASS',
            'confirmations':count,'identical_replays':count,'abandoned_inspections':count*2,
            'rows_after_each_phase':[count,count,count], 'wal_bytes_before':before,
            'wal_bytes_after_new':after_new,'wal_bytes_after_replays':after_replay,
            'wal_bytes_after_inspections':after_inspect,'wal_bytes_after_checkpoint':after_checkpoint,
            'wal_bytes_per_confirmation':growth,'old_pin_version':1,'all_pins_retained':True,
            'source_file_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--count',type=int,default=64);parser.add_argument('--smoke',action='store_true')
    parser.add_argument('--out',type=Path);args=parser.parse_args()
    encoded=json.dumps(run(min(args.count,8) if args.smoke else args.count),sort_keys=True)+'\n'
    if args.out:args.out.write_text(encoded,encoding='utf-8')
    print(encoded,end='');return 0


if __name__=='__main__':raise SystemExit(main())
