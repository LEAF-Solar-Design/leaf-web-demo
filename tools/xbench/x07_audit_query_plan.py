"""Offline synthetic audit query-plan measurements. No customer input data."""
import argparse
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from x07_profile_history_audit import CustomerProfileStore, audit_history, begin_audit, publish_stdout


def fixture(path, foreign_count):
    store=CustomerProfileStore(path)
    a=store.save_version('target',{'module':{'block':'A','layer':'M'}},expected_version=0)
    old=store.apply_to_project('old','target')
    c=store.save_version('corrupt',{'module':{'block':'C','layer':'M'}},expected_version=0)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('INSERT INTO profile_versions VALUES (?,?,?,?,?)',('corrupt',c['profile_id'],3,'{}','synthetic'))
        for index in range(foreign_count):
            key=f'foreign-{index}'
            db.execute('INSERT INTO customers VALUES (?,?)',(key,key))
            db.execute('INSERT INTO profile_versions VALUES (?,?,?,?,?)',(key,key,1,'{}','synthetic'))
            db.execute('INSERT INTO project_applications VALUES (?,?,?,?)',(key,key,key,1))
    return old


def measure_plans(directory, *, smoke=False):
    cases=[]
    for foreign_count in ([0,16] if smoke else [0,100,1000]):
        with tempfile.TemporaryDirectory(dir=directory,prefix='x07-plan-') as scratch:
            path=Path(scratch)/'store.sqlite'; old=fixture(path,foreign_count)
            store=CustomerProfileStore(path,read_only=True); before=path.read_bytes()
            report=audit_history(store,customer_key='target')
            if not report['healthy'] or report['version_count']!=1 or report['pin_count']!=1: raise ValueError('oracle mismatch')
            queries={'customers':'SELECT profile_id FROM customers WHERE customer_key=?',
                     'profile_versions':'SELECT version,profile_id FROM profile_versions WHERE customer_key=? ORDER BY version LIMIT 10001',
                     'project_applications':'SELECT version,profile_id FROM project_applications WHERE customer_key=? ORDER BY project_key LIMIT 10001'}
            plans={}
            with store._connection() as db:
                begin_audit(db)
                for table,query in queries.items():
                    details=[row[3] for row in db.execute('EXPLAIN QUERY PLAN '+query,('target',))]
                    plans[table]={'details':details,'full_table_scan':any(detail.upper().startswith('SCAN ') for detail in details)}
            if store.application_record('old','target')!=old or path.read_bytes()!=before: raise ValueError('audit changed fixture')
            cases.append({'foreign_rows':foreign_count,'audit':report,'plans':plans})
    return {'schema':'leaf.x07.audit-query-plan.v1','scope':'synthetic offline','cases':cases,
            'full_foreign_table_scan':any(plan['full_table_scan'] for case in cases for plan in case['plans'].values())}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,required=True); parser.add_argument('--smoke',action='store_true')
    args=parser.parse_args()
    try: result=measure_plans(args.directory,smoke=args.smoke)
    except (OSError,ValueError,sqlite3.Error):
        publish_stdout({'schema':'leaf.x07.audit-query-plan.v1','error':'MEASUREMENT_UNAVAILABLE'}); return 2
    return publish_stdout(result)


if __name__ == '__main__': raise SystemExit(main())
