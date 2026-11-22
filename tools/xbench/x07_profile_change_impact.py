"""Offline read-only role preview. Pins remain on their immutable versions."""
import copy
import json
from pathlib import Path
import sqlite3
from x07_profile_history_audit import CustomerProfileStore, MAX_AUDIT_ROWS, MAX_PROFILE_VERSION, _key, begin_audit, require_database_file, publish_stdout
from x07_profile_version_diff import JsonArgumentParser


def same_role(left, right):
    return json.dumps(left,sort_keys=True,allow_nan=False)==json.dumps(right,sort_keys=True,allow_nan=False)


def preview_change(store, *, customer_key, profile_id, before_version, after_version):
    customer_key, profile_id = _key(customer_key), _key(profile_id)
    if store.read_only is not True or any(type(v) is not int or not 1 <= v <= MAX_PROFILE_VERSION for v in (before_version,after_version)):
        raise ValueError('read-only store and safe versions required')
    require_database_file(store.path)
    with store._connection() as db:
        begin_audit(db)
        root = db.execute('SELECT profile_id FROM main.customers WHERE customer_key=?',(customer_key,)).fetchone()
        if root is None or root[0] != profile_id: raise ValueError('scoped profile unavailable')
        rows = db.execute('SELECT * FROM main.profile_versions WHERE customer_key=? ORDER BY version LIMIT ?', (customer_key,MAX_AUDIT_ROWS+1)).fetchall()
        pins = db.execute('SELECT * FROM main.project_applications WHERE customer_key=? ORDER BY project_key LIMIT ?', (customer_key,MAX_AUDIT_ROWS+1)).fetchall()
        if max(len(rows),len(pins)) > MAX_AUDIT_ROWS: raise ValueError('row budget exceeded')
        if any(row['profile_id'] != profile_id for row in rows): raise ValueError('inconsistent profile')
        versions = {row['version']:store._profile(row)['roles'] for row in rows}
        if before_version not in versions or after_version not in versions: raise ValueError('scoped versions unavailable')
        before, after = versions[before_version], versions[after_version]
        retaining = []
        for pin in pins:
            version = pin['version']
            if type(version) is not int or version not in versions or pin['profile_id'] != profile_id: raise ValueError('inconsistent pin')
            roles = versions[version]
            different = sorted(k for k in roles.keys() | after.keys() if not same_role(roles.get(k),after.get(k)))
            if different: retaining.append({'project_key':pin['project_key'],'version':version,'different_roles':different})
    return {'schema':'leaf.x07.profile-change-impact.v1','customer_key':customer_key,'profile_id':profile_id,
            'before_version':before_version,'after_version':after_version,
            'added':{k:copy.deepcopy(after[k]) for k in sorted(after.keys()-before.keys())},
            'removed':{k:copy.deepcopy(before[k]) for k in sorted(before.keys()-after.keys())},
            'changed':{k:{'before':copy.deepcopy(before[k]),'after':copy.deepcopy(after[k])} for k in sorted(before.keys()&after.keys()) if not same_role(before[k],after[k])},
            'retaining_different_roles':retaining}


def main():
    parser = JsonArgumentParser(result_schema='leaf.x07.profile-change-impact.v1', description=__doc__)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--customer',required=True); parser.add_argument('--profile-id',required=True)
    parser.add_argument('--before',type=int,required=True); parser.add_argument('--after',type=int,required=True)
    args = parser.parse_args()
    try:
        require_database_file(args.database)
        result = preview_change(CustomerProfileStore(args.database,read_only=True),customer_key=args.customer,profile_id=args.profile_id,before_version=args.before,after_version=args.after)
    except (OSError,ValueError,sqlite3.Error):
        publish_stdout({'schema':'leaf.x07.profile-change-impact.v1','error':'SCOPED_PREVIEW_UNAVAILABLE'}); return 2
    return publish_stdout(result)


if __name__ == '__main__': raise SystemExit(main())
