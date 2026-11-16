"""Read-only reference audit of settled offline stores, not a geometry replay.

Each store has its own read snapshot. The caller must provide settled stores;
this does not promise an atomic distributed snapshot during concurrent writes.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from x07_profile_history_audit import CustomerProfileStore, MAX_AUDIT_ROWS, _key, begin_audit, require_database_file, publish_stdout
from x07_workflow_adapter import ConfirmationReceiptStore, _check_connection, _distinct_store_paths


MAX_RECEIPT_BYTES=262144
MAX_RECEIPT_DEPTH=32  # root container has depth one
MAX_RECEIPT_NODES=10000  # containers and values, excluding member keys


class ReceiptBudgetError(ValueError):
    pass


def parse_receipt(encoded):
    if not isinstance(encoded,str):raise ValueError('receipt must be text')
    if len(encoded.encode('utf-8'))>MAX_RECEIPT_BYTES:raise ReceiptBudgetError('receipt byte budget')
    depth=0;quoted=False;escaped=False
    for char in encoded:
        if quoted:
            if escaped:escaped=False
            elif char=='\\':escaped=True
            elif char=='"':quoted=False
        elif char=='"':quoted=True
        elif char in '[{':
            depth+=1
            if depth>MAX_RECEIPT_DEPTH:raise ReceiptBudgetError('receipt depth budget')
        elif char in ']}':depth-=1
    value=json.loads(encoded,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
    pending=[value];nodes=0
    while pending:
        current=pending.pop();nodes+=1
        if nodes>MAX_RECEIPT_NODES:raise ReceiptBudgetError('receipt node budget')
        if isinstance(current,dict):pending.extend(current.values())
        elif isinstance(current,list):pending.extend(current)
    json.dumps(value,sort_keys=True,allow_nan=False)
    return value


def audit_receipts(store, journal, *, customer_key):
    customer_key = _key(customer_key)
    if store.read_only is not True or journal.read_only is not True: raise ValueError('read-only stores required')
    require_database_file(store.path); require_database_file(journal.path)
    _distinct_store_paths(store.path,journal.path)
    issues = {'invalid_receipts':0,'missing_references':0,'inconsistent_references':0}
    with store._connection() as db, closing(journal._connect()) as receipts:
        begin_audit(db); _check_connection(receipts)
        if receipts.in_transaction: raise ValueError('caller transaction unsupported')
        receipts.execute('BEGIN')
        rows = receipts.execute('SELECT project_key,result FROM main.confirmations WHERE customer_key=? ORDER BY project_key LIMIT ?', (customer_key,MAX_AUDIT_ROWS+1)).fetchall()
        if len(rows)>MAX_AUDIT_ROWS: raise ValueError('row budget exceeded')
        for project, encoded in rows:
            try:
                value=parse_receipt(encoded)
                if (not isinstance(value,dict) or value.get('schema')!='leaf.x07.profile-workflow.v1' or value.get('state')!='applied'
                    or value.get('customer_key')!=customer_key or value.get('project_key')!=project
                    or not isinstance(value.get('applied_profile'),dict)): raise ValueError()
            except ReceiptBudgetError:
                raise
            except (ValueError,TypeError,RecursionError,UnicodeError):
                issues['invalid_receipts']+=1; continue
            try: pin=store._application(db,project,customer_key)
            except ValueError:
                issues['inconsistent_references']+=1; continue
            if pin is None:
                issues['missing_references']+=1; continue
            if (json.dumps(value['applied_profile'],sort_keys=True,allow_nan=False)!=json.dumps(pin,sort_keys=True,allow_nan=False)
                or json.dumps(value.get('resolved_roles'),sort_keys=True,allow_nan=False)!=json.dumps(pin['roles'],sort_keys=True,allow_nan=False)):
                issues['inconsistent_references']+=1
    return {'schema':'leaf.x07.receipt-history-audit.v1','customer_key':customer_key,'receipt_count':len(rows),'healthy':not any(issues.values()),'issues':issues}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,required=True); parser.add_argument('--journal',type=Path,required=True); parser.add_argument('--customer',required=True)
    args=parser.parse_args()
    try:
        require_database_file(args.database); require_database_file(args.journal)
        result=audit_receipts(CustomerProfileStore(args.database,read_only=True),ConfirmationReceiptStore(args.journal,read_only=True),customer_key=args.customer)
    except (OSError,ValueError,sqlite3.Error):
        publish_stdout({'schema':'leaf.x07.receipt-history-audit.v1','error':'AUDIT_UNAVAILABLE'}); return 2
    if publish_stdout(result): return 2
    return 0 if result['healthy'] else 1


if __name__ == '__main__': raise SystemExit(main())
