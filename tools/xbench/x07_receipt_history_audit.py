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
MAX_TOTAL_RECEIPT_BYTES=8*1024*1024  # scoped payload bytes before bulk reads


def require_settled_files(path):
    require_database_file(path)
    rollback=Path(str(path)+'-journal')
    if rollback.exists() and rollback.stat().st_size:
        raise ValueError('settled journal required')
    wal=Path(str(path)+'-wal')
    if wal.exists():
        require_database_file(wal)
        shared=Path(str(path)+'-shm')
        require_database_file(shared)
        # Refuse before SQLite can create a missing shared-memory index.
        with shared.open('rb') as stream:
            stream.read(1)


def file_identity(path):
    require_settled_files(path)
    metadata=Path(path).lstat()
    return (metadata.st_dev,metadata.st_ino,metadata.st_size,metadata.st_mtime_ns,metadata.st_ctime_ns)


def store_identities(profile,journal):
    return file_identity(profile),file_identity(journal)


class ReceiptBudgetError(ValueError):
    pass


class ObjectMembers(list):
    """Retain every decoded member until its values have been budgeted."""


def materialize(value):
    if isinstance(value,ObjectMembers):return {key:materialize(child) for key,child in value}
    if isinstance(value,list):return [materialize(child) for child in value]
    return value


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
    # Preserve integer signed zero as a float so typed identity/count checks do
    # not silently turn an unsupported numeric token into an ordinary zero.
    value=json.loads(encoded,object_pairs_hook=ObjectMembers,
                     parse_int=lambda token: -0.0 if token=='-0' else int(token),
                     parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
    pending=[value];nodes=0;duplicate=False
    while pending:
        current=pending.pop();nodes+=1
        if nodes>MAX_RECEIPT_NODES:raise ReceiptBudgetError('receipt node budget')
        if isinstance(current,ObjectMembers):
            keys=[key for key,_ in current]
            duplicate=duplicate or len(keys)!=len(set(keys))
            pending.extend(child for _,child in current)
        elif isinstance(current,list):pending.extend(current)
    if duplicate:raise ValueError('duplicate receipt member')
    value=materialize(value)
    json.dumps(value,sort_keys=True,allow_nan=False,ensure_ascii=False).encode('utf-8')
    return value


def audit_receipts(store, journal, *, customer_key):
    customer_key = _key(customer_key)
    if store.read_only is not True or journal.read_only is not True: raise ValueError('read-only stores required')
    identities=store_identities(store.path,journal.path)
    _distinct_store_paths(store.path,journal.path)
    issues = {'invalid_receipts':0,'missing_references':0,'inconsistent_references':0}
    receipt_connection = journal._connect()
    if receipt_connection.in_transaction:
        # An injected active transaction remains owned by its caller.
        raise ValueError('caller transaction unsupported')
    with closing(receipt_connection) as receipts, store._connection() as db:
        begin_audit(db); _check_connection(receipts)
        receipts.execute('BEGIN')
        count,total=receipts.execute('SELECT count(*),coalesce(sum(length(CAST(result AS BLOB))),0) FROM main.confirmations WHERE customer_key=?', (customer_key,)).fetchone()
        if count>MAX_AUDIT_ROWS or total>MAX_TOTAL_RECEIPT_BYTES:
            raise ReceiptBudgetError('scoped receipt budget exceeded')
        rows = receipts.execute('SELECT project_key,result FROM main.confirmations WHERE customer_key=? ORDER BY project_key LIMIT ?', (customer_key,MAX_AUDIT_ROWS+1)).fetchall()
        if len(rows)>MAX_AUDIT_ROWS: raise ValueError('row budget exceeded')
        for project, encoded in rows:
            try:
                value=parse_receipt(encoded)
                selection=value.get('selection') if isinstance(value,dict) else None
                count=selection.get('count') if isinstance(selection,dict) else None
                if (not isinstance(value,dict) or value.get('schema')!='leaf.x07.profile-workflow.v1' or value.get('state')!='applied'
                    or value.get('customer_key')!=customer_key or value.get('project_key')!=project
                    or not isinstance(value.get('applied_profile'),dict)
                    or type(count) is not int or not 0<=count<=9007199254740991): raise ValueError()
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
    if store_identities(store.path,journal.path)!=identities:
        raise ValueError('store changed during audit')
    return {'schema':'leaf.x07.receipt-history-audit.v1','customer_key':customer_key,'receipt_count':len(rows),'healthy':not any(issues.values()),'issues':issues}


class ReceiptAuditParser(argparse.ArgumentParser):
    def error(self, message):
        publish_stdout({'schema': 'leaf.x07.receipt-history-audit.v1', 'error': 'INVALID_ARGUMENTS'})
        raise SystemExit(2)


def main():
    parser=ReceiptAuditParser(description=__doc__)
    parser.add_argument('--database',type=Path,required=True); parser.add_argument('--journal',type=Path,required=True); parser.add_argument('--customer',required=True)
    args=parser.parse_args()
    try:
        identities=store_identities(args.database,args.journal)
        result=audit_receipts(CustomerProfileStore(args.database,read_only=True),ConfirmationReceiptStore(args.journal,read_only=True),customer_key=args.customer)
        if store_identities(args.database,args.journal)!=identities:
            raise ValueError('store changed after validation')
    except (OSError,ValueError,sqlite3.Error):
        publish_stdout({'schema':'leaf.x07.receipt-history-audit.v1','error':'AUDIT_UNAVAILABLE'}); return 2
    if publish_stdout(result): return 2
    return 0 if result['healthy'] else 1


if __name__ == '__main__': raise SystemExit(main())
